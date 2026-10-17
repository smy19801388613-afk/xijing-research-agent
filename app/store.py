"""SQLite stores atomic snapshots; files contain immutable provider responses."""
import json
import sqlite3
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from datetime import datetime, timezone, timedelta
from .config import DATA,VERSION

class ExecutionStopped(BaseException):
    """An obsolete/cancelled worker must not be converted into a tool failure."""

class ConcurrentRunUpdate(ValueError):
    """A stale whole-run snapshot may not overwrite a newer atomic update."""

_execution = ContextVar('research_execution', default=None)
_UNSET = object()


def _check_execution(c):
    owner = _execution.get()
    if not owner: return
    if owner['kind'] == 'conversation':
        row = c.execute('SELECT payload,cancel FROM conversations WHERE id=?',(owner['id'],)).fetchone()
        value = json.loads(row[0]) if row else {}
        if not row or row[1] or value.get('turn_id') != owner['turn_id'] or value.get('state') != 'running':
            raise ExecutionStopped()
    else:
        row = c.execute('SELECT state,cancel FROM runs WHERE id=?',(owner['id'],)).fetchone()
        if not row or row[1] or row[0] not in ('queued','running'):
            raise ExecutionStopped()


def check_execution():
    if _execution.get():
        with connection() as c: _check_execution(c)


@contextmanager
def execution_scope(kind, identity, turn_id=None):
    token = _execution.set({'kind':kind,'id':identity,'turn_id':turn_id})
    try:
        check_execution()
        yield
    finally:
        _execution.reset(token)


def conversation_execution(fn):
    @wraps(fn)
    def wrapped(cid, turn_id=None):
        tid = turn_id or get_conversation(cid).get('turn_id')
        try:
            with execution_scope('conversation',cid,tid): return fn(cid,tid)
        except ExecutionStopped:
            # The API has already finalized cancellation, or a new turn owns the chat.
            return
    return wrapped

def now():
    return datetime.now(timezone.utc).isoformat()

def connection():
    DATA.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DATA / "research.sqlite3", timeout=15)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, created TEXT, state TEXT, payload TEXT, cancel INTEGER DEFAULT 0)")
    c.execute("CREATE TABLE IF NOT EXISTS documents (id TEXT PRIMARY KEY, payload TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS conversations (id TEXT PRIMARY KEY, updated TEXT, payload TEXT, cancel INTEGER DEFAULT 0)")
    c.execute("CREATE TABLE IF NOT EXISTS trash (kind TEXT, id TEXT, deleted TEXT, batch TEXT, PRIMARY KEY(kind,id))")
    c.execute("CREATE TABLE IF NOT EXISTS workers (kind TEXT, target TEXT, turn_id TEXT, pid INTEGER, birth TEXT, PRIMARY KEY(kind,target,turn_id))")
    return c

def create_run(request, parent=None):
    from . import execution_budget
    budget=execution_budget.current() or execution_budget.normalize(request.get('execution_budget'))
    started=datetime.now(timezone.utc)
    deadline=started+timedelta(minutes=budget['time_minutes'])
    if execution_budget.deadline() is not None:
        import time
        deadline=min(deadline,started+timedelta(seconds=execution_budget.deadline()-time.monotonic()))
    run_id = uuid.uuid4().hex[:16]
    run = {"id": run_id, "created_at": now(), "updated_at": now(), "state": "queued", "progress": 0,
           "request": request, "parent_id": parent, "company": request["ticker"], "facts": [], "evidence": [],
           "calculations": [], "checks": [], "gaps": [], "events": [], "memo": None,
           "model": {"status": "not_requested"}, "scenario_history": [], "valuation_history": [], "memo_revisions": [], "report_revision": 1, "version": VERSION,
           '_write_version':0, 'execution_budget':budget, 'budget_usage':execution_budget.initial_usage(budget),
           'execution_deadline_at':deadline.isoformat()}
    from .questions import create as create_question
    run["research_questions"]=[create_question(request.get("question","当前研究还有哪些证据缺口？"))]
    with connection() as c:
        c.execute("BEGIN IMMEDIATE")
        _check_execution(c)
        owner = _execution.get()
        if owner and owner['kind']=='conversation':
            run['execution_owner']={'conversation_id':owner['id'],'turn_id':owner['turn_id']}
        if not (owner and owner['kind']=='conversation') and active_execution_count(c)>=2:
            raise ValueError("已有两项研究正在进行，请等待完成或取消其中一项")
        c.execute("INSERT INTO runs (id,created,state,payload) VALUES (?,?,?,?)", (run_id,run["created_at"],run["state"],json.dumps(run,ensure_ascii=False)))
    from . import competition
    competition.audit("run_created",run_id=run_id,request=request,policy=competition.POLICY)
    return run


def active_execution_count(c):
    """Inline run work uses its owning chat slot, not a second quota."""
    chats=[json.loads(row[0]) for row in c.execute('SELECT payload FROM conversations')]
    owners={(chat['id'],chat.get('turn_id')) for chat in chats if chat.get('state')=='running'}
    runs=[json.loads(row[0]) for row in c.execute("SELECT payload FROM runs WHERE state IN ('queued','running')")]
    return len(owners)+sum((r.get('execution_owner',{}).get('conversation_id'),
                           r.get('execution_owner',{}).get('turn_id')) not in owners for r in runs)

def get_run(run_id):
    with connection() as c:
        row = c.execute("SELECT payload FROM runs WHERE id=? AND NOT EXISTS (SELECT 1 FROM trash WHERE kind='run' AND trash.id=runs.id)", (run_id,)).fetchone()
    if not row:
        raise KeyError(run_id)
    return json.loads(row[0])

def save_run(run):
    with connection() as c:
        c.execute('BEGIN IMMEDIATE')
        _check_execution(c)
        row=c.execute('SELECT payload FROM runs WHERE id=?',(run['id'],)).fetchone()
        if not row: raise KeyError(run['id'])
        current=json.loads(row[0])
        if current.get('_write_version',0)!=run.get('_write_version',0):
            raise ConcurrentRunUpdate('研究已被其他操作更新，旧快照未写入；请基于最新状态重试')
        # Usage receipts change during model I/O, independently of the business
        # snapshot version. Whole-run saves must retain the latest ledger.
        if 'budget_usage' in current: run['budget_usage']=current['budget_usage']
        run['_write_version']=current.get('_write_version',0)+1
        run["updated_at"] = now()
        c.execute("UPDATE runs SET state=?,payload=? WHERE id=?", (run["state"],json.dumps(run,ensure_ascii=False,allow_nan=False),run["id"]))
    from . import competition
    competition.audit("run_snapshot",run_id=run["id"],snapshot=run)

def update_run(run_id,change):
    with connection() as c:
        c.execute("BEGIN IMMEDIATE")
        _check_execution(c)
        row=c.execute("SELECT payload FROM runs WHERE id=? AND NOT EXISTS (SELECT 1 FROM trash WHERE kind='run' AND trash.id=runs.id)",(run_id,)).fetchone()
        if not row: raise KeyError(run_id)
        run=json.loads(row[0]);version=run.get('_write_version',0);change(run);run["updated_at"]=now()
        run['_write_version']=version+1
        c.execute("UPDATE runs SET state=?,payload=? WHERE id=?",(run["state"],json.dumps(run,ensure_ascii=False,allow_nan=False),run_id))
    from . import competition
    competition.audit("run_update",run_id=run_id,snapshot=run)
    return run


def ensure_run_budget(run_id):
    """Freeze legacy runs on first execution without changing newer defaults."""
    from .execution_budget import normalize, initial_usage
    run=get_run(run_id)
    if run.get('execution_budget') and run.get('execution_deadline_at'): return run
    def change(value):
        budget=normalize(value.get('execution_budget') or value.get('request',{}).get('execution_budget'))
        value.setdefault('execution_budget',budget)
        value.setdefault('execution_deadline_at',(datetime.now(timezone.utc)+timedelta(minutes=budget['time_minutes'])).isoformat())
        value.setdefault('budget_usage',initial_usage(budget))
    return update_run(run_id,change)


def record_run_budget_usage(run_id,usage):
    with connection() as c:
        c.execute('BEGIN IMMEDIATE')
        _check_execution(c)
        row=c.execute('SELECT payload FROM runs WHERE id=?',(run_id,)).fetchone()
        if not row: raise KeyError(run_id)
        run=json.loads(row[0]);run.update(budget_usage=dict(usage),updated_at=now())
        c.execute('UPDATE runs SET payload=? WHERE id=?',(json.dumps(run,ensure_ascii=False,allow_nan=False),run_id))
    return run


def _fail_run(c,run,reason,message,extra=None):
    run.update(state='failed',stop_reason=reason,updated_at=now(),**(extra or {}))
    run['_write_version']=run.get('_write_version',0)+1
    finalize_budget_usage(run)
    run.setdefault('events',[]).append({'time':now(),'label':'研究执行已停止','detail':message,'kind':'warning'})
    c.execute('UPDATE runs SET cancel=1,state=?,payload=? WHERE id=?',
              ('failed',json.dumps(run,ensure_ascii=False),run['id']))


def fail_execution(kind,target,turn_id='',*,reason='worker_exit',message=None,expected_worker=None,expected_launch=_UNSET):
    """Revoke only the still-active execution observed by the launcher/reconciler."""
    message=message or '后台执行进程已退出，已保留取得的材料与已保存结果；可继续未完成部分。'
    with connection() as c:
        c.execute('BEGIN IMMEDIATE')
        if expected_worker is not None:
            worker=c.execute('SELECT pid,birth FROM workers WHERE kind=? AND target=? AND turn_id=?',(kind,target,turn_id)).fetchone()
            if tuple(worker or ())!=tuple(expected_worker): return False
        table='conversations' if kind=='conversation' else 'runs'
        row=c.execute(f'SELECT payload FROM {table} WHERE id=?',(target,)).fetchone()
        if not row: return False
        value=json.loads(row[0])
        if expected_launch is not _UNSET and value.get('execution_launch',{}).get('nonce')!=expected_launch: return False
        if kind=='run':
            if value.get('state') not in ('queued','running'): return False
            _fail_run(c,value,reason,message)
        else:
            if value.get('state')!='running' or value.get('turn_id')!=turn_id: return False
            for rid,payload in c.execute("SELECT id,payload FROM runs WHERE state IN ('queued','running')").fetchall():
                owned=json.loads(payload)
                if owned.get('execution_owner')=={'conversation_id':target,'turn_id':turn_id}:
                    _fail_run(c,owned,reason,message)
            value.update(state='failed',stop_reason=reason,updated_at=now())
            finalize_budget_usage(value)
            for step in value.get('steps',[]):
                if step.get('turn_id')==turn_id and step.get('state')=='running':
                    step.update(state='failed',finished_at=now(),result={'error':message})
            from .agent import delivery_state,progress_message
            from .coverage_view import turn_observation
            linked=c.execute('SELECT payload FROM runs WHERE id=?',(value.get('run_id'),)).fetchone()
            run=json.loads(linked[0]) if linked else None
            observed=turn_observation(value,run)
            delivery=delivery_state(value,run,'failed',observed)
            value.setdefault('turn_coverage',{})[turn_id]=observed
            value.setdefault('messages',[]).append({'id':uuid.uuid4().hex[:12],'role':'assistant','turn_id':turn_id,
                'content':message+'\n\n'+progress_message(delivery),'time':now(),'error':True,'incomplete':True,
                'stop_reason':reason,'delivery':delivery,'coverage':observed})
            c.execute('UPDATE conversations SET cancel=1,updated=?,payload=? WHERE id=?',
                      (value['updated_at'],json.dumps(value,ensure_ascii=False),target))
        return True

def new_conversation(run_id=None):
    if run_id: get_run(run_id)
    value={"id":uuid.uuid4().hex[:16],"run_id":run_id,"title":"新的研究对话","state":"idle",
           "created_at":now(),"updated_at":now(),"messages":[],"steps":[],"model_calls":[],
           "turn_contexts":{},"persistent_constraints":{}}
    with connection() as c:
        c.execute("INSERT INTO conversations(id,updated,payload) VALUES (?,?,?)",(value["id"],value["updated_at"],json.dumps(value,ensure_ascii=False)))
    return value

def get_conversation(cid):
    with connection() as c:
        row=c.execute("SELECT payload FROM conversations WHERE id=? AND NOT EXISTS (SELECT 1 FROM trash WHERE kind='conversation' AND trash.id=conversations.id)",(cid,)).fetchone()
    if not row: raise KeyError(cid)
    return json.loads(row[0])

def conversations(run_id=None):
    with connection() as c:
        rows=c.execute("SELECT payload FROM conversations WHERE NOT EXISTS (SELECT 1 FROM trash WHERE kind='conversation' AND trash.id=conversations.id) ORDER BY updated DESC").fetchall()
    values=[json.loads(r[0]) for r in rows]
    return [{k:v[k] for k in ("id","run_id","title","state","updated_at")} for v in values if run_id is None or v["run_id"]==run_id]

def update_conversation(cid,change):
    with connection() as c:
        c.execute("BEGIN IMMEDIATE")
        _check_execution(c)
        row=c.execute("SELECT payload FROM conversations WHERE id=? AND NOT EXISTS (SELECT 1 FROM trash WHERE kind='conversation' AND trash.id=conversations.id)",(cid,)).fetchone()
        if not row: raise KeyError(cid)
        value=json.loads(row[0]);change(value);value["updated_at"]=now()
        c.execute("UPDATE conversations SET updated=?,payload=? WHERE id=?",(value["updated_at"],json.dumps(value,ensure_ascii=False,allow_nan=False),cid))
    return value

def begin_turn(cid,text,profile_id,data_source,resume_from_turn_id=None,execution_budget=None):
    from .task_contract import resolve_contract
    from .execution_budget import normalize, initial_usage
    budget=normalize(execution_budget)
    with connection() as c:
        c.execute("BEGIN IMMEDIATE")
        row=c.execute("SELECT payload FROM conversations WHERE id=? AND NOT EXISTS (SELECT 1 FROM trash WHERE kind='conversation' AND trash.id=conversations.id)",(cid,)).fetchone()
        if not row: raise KeyError(cid)
        value=json.loads(row[0])
        if value["state"]=="running": raise ValueError("请等待当前回复完成，或先停止")
        if active_execution_count(c)>=2: raise ValueError("已有两个研究执行正在运行，请等待完成")
        previous = next((m for m in reversed(value['messages']) if m.get('role')=='assistant'), None)
        # The button and an exact continuation request use the same explicit contract.
        if not resume_from_turn_id and text.strip().rstrip('。.!！') in ('继续','继续未完成部分','继续查证','继续未完成的研究'):
            if previous and previous.get('delivery',{}).get('can_resume'):
                resume_from_turn_id = previous['turn_id']
        prior_context = {}
        if resume_from_turn_id:
            if not previous or previous.get('turn_id')!=resume_from_turn_id or value.get('turn_id')!=resume_from_turn_id or not previous.get('delivery',{}).get('can_resume'):
                raise ValueError('只能继续最近一轮尚未完成的研究，请刷新对话后再试')
            prior_context = value.get('turn_contexts',{}).get(resume_from_turn_id,{})
            if not prior_context or prior_context.get('run_id')!=value.get('run_id'):
                raise ValueError('研究上下文已变化，请提出新的研究请求')
        tid=uuid.uuid4().hex[:12]
        contract,policy = resolve_contract(value,text,prior_context,tid)
        started=datetime.now(timezone.utc)
        started_at=started.isoformat()
        deadline_at=(started+timedelta(minutes=budget['time_minutes'])).isoformat()
        run_row = c.execute('SELECT payload FROM runs WHERE id=?',(value.get('run_id'),)).fetchone() if value.get('run_id') else None
        run = json.loads(run_row[0]) if run_row else {}
        if resume_from_turn_id and run:
            owner=run.get('execution_owner') or {}
            prior_turn_ids=set(prior_context.get('task_turn_ids',[])) | {resume_from_turn_id}
            stopped=run.get('state')=='cancelled' or (run.get('state')=='failed' and run.get('stop_reason')=='time_limit')
            if stopped and owner.get('conversation_id')==cid and owner.get('turn_id') in prior_turn_ids:
                # Permit the new turn to use saved materials. Keep the terminal
                # run state and payload: old run/conversation workers remain
                # unable to write under their original execution scopes.
                c.execute('UPDATE runs SET cancel=0 WHERE id=?',(run['id'],))
        task_ids = list(prior_context.get('task_turn_ids',[])) + [tid]
        context = {'task_turn_id':prior_context.get('task_turn_id',tid),'task_turn_ids':task_ids,
            'original_request':prior_context.get('original_request',text),'user_text':text,
            'resume_from_turn_id':resume_from_turn_id,'run_id':value.get('run_id'),
            'report_revision_before':prior_context.get('report_revision_before',run.get('report_revision')),
            'turn_report_revision_before':run.get('report_revision'),
            'resume_pending':list((previous or {}).get('delivery',{}).get('pending',[])) if resume_from_turn_id else [],
            'rules':contract['rules'],'required_artifacts':contract['completion']['required_artifacts'],
            'task_contract':contract,
            'execution_budget':dict(budget),'started_at':started_at,'deadline_at':deadline_at}
        context['budget_usage']=initial_usage(budget)|{'decisions_used':0}
        value.setdefault('turn_contexts',{})[tid]=context
        value['effective_constraints']=policy
        value['persistent_constraints']=policy['persistent_rules']
        value.pop('termination',None)
        value.pop('execution_launch',None)
        value.pop('stop_reason',None)
        value.pop('budget_exhausted',None)
        value.update(state="running",turn_id=tid,profile_id=profile_id,data_source=data_source,updated_at=now(),cancel_requested=False,
                     execution_budget=dict(budget),execution_deadline_at=deadline_at,
                     budget_usage=dict(context['budget_usage']))
        if not value["messages"]: value["title"]=text[:35]
        value["messages"].append({"id":uuid.uuid4().hex[:12],"role":"user","content":text,"time":now(),"turn_id":tid})
        c.execute("UPDATE conversations SET cancel=0,updated=?,payload=? WHERE id=?",(value["updated_at"],json.dumps(value,ensure_ascii=False),cid))
    return value

def record_budget_usage(cid,turn_id,usage):
    """Persist each reservation/settlement before an obsolete worker can reuse it."""
    def change(value):
        if value.get('turn_id')!=turn_id or value.get('state')!='running':
            raise ExecutionStopped()
        value['budget_usage']={**value.get('budget_usage',{}),**usage}
        value['budget_usage'].pop('max_decisions',None)
        value.setdefault('turn_contexts',{}).setdefault(turn_id,{})['budget_usage']=dict(value['budget_usage'])
    return update_conversation(cid,change)


def finalize_budget_usage(value):
    """A killed request has no final provider receipt; retain a labelled estimate."""
    from .execution_budget import interrupted_usage
    if 'total_tokens' not in value.get('budget_usage',{}): return
    value['budget_usage']=interrupted_usage(value['budget_usage'])
    tid=value.get('turn_id')
    if tid in value.get('turn_contexts',{}):
        value['turn_contexts'][tid]['budget_usage']=dict(value['budget_usage'])


def cancel_conversation(cid,turn_id=None):
    """Finalize this turn atomically before killing its waiting worker.

    Only a run created by this exact turn is owned by the cancellation. Merely
    discussing an existing run must not stop another chat's work on that run.
    """
    with connection() as c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute('SELECT payload FROM conversations WHERE id=?',(cid,)).fetchone()
        if not row: raise KeyError(cid)
        value=json.loads(row[0])
        if turn_id and value.get('turn_id') != turn_id: return value
        if value.get('state') != 'running':
            # Also support cooperative callers operating without an agent turn.
            c.execute('UPDATE conversations SET cancel=1 WHERE id=?',(cid,))
            return value
        tid=value['turn_id']
        for rid,payload in c.execute('SELECT id,payload FROM runs WHERE state IN (\'queued\',\'running\')').fetchall():
            run=json.loads(payload)
            if run.get('execution_owner')=={'conversation_id':cid,'turn_id':tid}:
                _mark_run_cancelled(c,run)
        run_row=c.execute('SELECT payload FROM runs WHERE id=?',(value.get('run_id'),)).fetchone()
        run=json.loads(run_row[0]) if run_row else None
        value.update(state='cancelled',cancel_requested=True,updated_at=now())
        finalize_budget_usage(value)
        for step in value['steps']:
            if step.get('turn_id')==tid and step.get('state')=='running':
                step.update(state='cancelled',finished_at=now())
        from .agent import delivery_state,progress_message
        from .coverage_view import turn_observation
        observed=turn_observation(value,run)
        value.setdefault('turn_coverage',{})[tid]=observed
        delivery=delivery_state(value,run,'cancelled',observed)
        value['messages'].append({'id':uuid.uuid4().hex[:12],'role':'assistant','turn_id':tid,
            'content':'已停止本轮执行，已经完成的工具结果仍然保留。\n\n'+progress_message(delivery),
            'time':now(),'error':True,'incomplete':True,'delivery':delivery,'coverage':observed})
        c.execute('UPDATE conversations SET cancel=1,updated=?,payload=? WHERE id=?',
                  (value['updated_at'],json.dumps(value,ensure_ascii=False),cid))
    return value

def conversation_cancelled(cid):
    with connection() as c: row=c.execute("SELECT cancel FROM conversations WHERE id=?",(cid,)).fetchone()
    return bool(row and row[0])


def record_termination(cid,turn_id,result):
    with connection() as c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute('SELECT payload FROM conversations WHERE id=?',(cid,)).fetchone()
        if not row: raise KeyError(cid)
        value=json.loads(row[0])
        # A late OS result must not overwrite any field of a newer turn.
        if value.get('turn_id')!=turn_id or value.get('state')!='cancelled': return value
        previous=value.get('termination',{})
        if previous==result or (result.get('status')=='not_registered' and previous.get('status') in ('stopped','already_exited')):
            return value
        value['termination']=result
        value['updated_at']=now()
        c.execute('UPDATE conversations SET updated=?,payload=? WHERE id=?',
                  (value['updated_at'],json.dumps(value,ensure_ascii=False),cid))
    return value

def list_runs():
    with connection() as c:
        rows = c.execute("SELECT payload FROM runs WHERE NOT EXISTS (SELECT 1 FROM trash WHERE kind='run' AND trash.id=runs.id) ORDER BY created DESC").fetchall()
    return [{k:v for k,v in json.loads(row[0]).items() if k in {"id","created_at","state","progress","company","request","parent_id"}} for row in rows]

def cancelled(run_id):
    with connection() as c:
        row = c.execute("SELECT cancel FROM runs WHERE id=?",(run_id,)).fetchone()
    return bool(row and row[0])

def research_cancelled(run_id):
    """An old run's stop flag fences its execution, not every future discussion.

    Only a verified new conversation execution bound to the saved research may
    reuse a terminal run. Keep its original flag, state, budget and audit intact;
    stale run workers and stale conversation turns still fail the write guard.
    """
    with connection() as c:
        _check_execution(c)
        row=c.execute('SELECT payload,cancel FROM runs WHERE id=?',(run_id,)).fetchone()
        if not row or not row[1]: return False
        run=json.loads(row[0]);owner=_execution.get()
        if owner and owner['kind']=='conversation' and run.get('state') in ('cancelled','failed'):
            current={'conversation_id':owner['id'],'turn_id':owner['turn_id']}
            if run.get('execution_owner')!=current:
                chat=c.execute('SELECT payload FROM conversations WHERE id=?',(owner['id'],)).fetchone()
                if chat and json.loads(chat[0]).get('run_id')==run_id:
                    return False
        return True


def cancel_run(run_id):
    with connection() as c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute('SELECT payload FROM runs WHERE id=?',(run_id,)).fetchone()
        if not row: raise KeyError(run_id)
        run=json.loads(row[0])
        if run['state'] in ('queued','running'): _mark_run_cancelled(c,run)
    return run


def _mark_run_cancelled(c,run):
    run.update(state='cancelled',updated_at=now())
    run['_write_version']=run.get('_write_version',0)+1
    finalize_budget_usage(run)
    run['events'].append({'time':now(),'label':'研究已取消','detail':'保留已经完成的研究结果','kind':'warning'})
    c.execute('UPDATE runs SET cancel=1,state=?,payload=? WHERE id=?',
              ('cancelled',json.dumps(run,ensure_ascii=False),run['id']))

def put_document(doc):
    with connection() as c:
        c.execute('BEGIN IMMEDIATE')
        _check_execution(c)
        c.execute("INSERT OR REPLACE INTO documents VALUES (?,?)",(doc["id"],json.dumps(doc,ensure_ascii=False)))

def get_document(doc_id):
    with connection() as c:
        row=c.execute("SELECT payload FROM documents WHERE id=?",(doc_id,)).fetchone()
    if not row:
        raise KeyError(doc_id)
    return json.loads(row[0])

def list_documents():
    with connection() as c:
        rows=c.execute("SELECT payload FROM documents").fetchall()
    return [json.loads(r[0]) for r in rows]


def trash_item(kind,item_id):
    if kind not in ("run","conversation"): raise ValueError("不支持的历史类型")
    with connection() as c:
        c.execute("BEGIN IMMEDIATE")
        table="runs" if kind=="run" else "conversations"
        row=c.execute(f"SELECT payload FROM {table} WHERE id=?",(item_id,)).fetchone()
        if not row: raise KeyError(item_id)
        value=json.loads(row[0]);items=[(kind,item_id,value)]
        if kind=="run":
            for cid,payload in c.execute("SELECT id,payload FROM conversations").fetchall():
                chat=json.loads(payload)
                if chat.get("run_id")==item_id: items.append(("conversation",cid,chat))
        if any(v.get("state") in ("queued","running") for _,_,v in items):
            raise ValueError("研究或关联对话仍在执行，请先停止并等待结束，再删除")
        batch=uuid.uuid4().hex;timestamp=now()
        for item_kind,identity,_ in items:
            c.execute("INSERT OR IGNORE INTO trash VALUES (?,?,?,?)",(item_kind,identity,timestamp,batch))
    return {"status":"deleted","detail":"已移入回收站，可恢复；原始资料仍在资料库"}


def list_trash():
    result=[]
    with connection() as c:
        for kind,item_id,deleted,batch in c.execute("SELECT kind,id,deleted,batch FROM trash ORDER BY deleted DESC").fetchall():
            table="runs" if kind=="run" else "conversations"
            row=c.execute(f"SELECT payload FROM {table} WHERE id=?",(item_id,)).fetchone()
            if not row: continue
            v=json.loads(row[0])
            result.append({"kind":kind,"id":item_id,"deleted_at":deleted,
                "title":v.get("title") if kind=="conversation" else v["company"]+" · "+str(v["request"]["year"]),
                "run_id":v.get("run_id"),"batch":batch})
    return result


def restore_item(kind,item_id):
    if kind not in ("run","conversation"): raise ValueError("不支持的历史类型")
    with connection() as c:
        c.execute("BEGIN IMMEDIATE")
        row=c.execute("SELECT batch FROM trash WHERE kind=? AND id=?",(kind,item_id)).fetchone()
        if not row: raise KeyError(item_id)
        if kind=="conversation":
            chat=json.loads(c.execute("SELECT payload FROM conversations WHERE id=?",(item_id,)).fetchone()[0])
            if chat.get("run_id") and c.execute("SELECT 1 FROM trash WHERE kind='run' AND id=?",(chat["run_id"],)).fetchone():
                raise ValueError("请先恢复这条对话所属的公司研究")
            c.execute("DELETE FROM trash WHERE kind=? AND id=?",(kind,item_id))
        else:
            c.execute("DELETE FROM trash WHERE batch=?",(row[0],))
    return {"status":"restored"}
