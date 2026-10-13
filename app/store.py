"""SQLite stores atomic snapshots; files contain immutable provider responses."""
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from .config import DATA,VERSION

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
    return c

def create_run(request, parent=None):
    run_id = uuid.uuid4().hex[:16]
    run = {"id": run_id, "created_at": now(), "updated_at": now(), "state": "queued", "progress": 0,
           "request": request, "parent_id": parent, "company": request["ticker"], "facts": [], "evidence": [],
           "calculations": [], "checks": [], "gaps": [], "events": [], "memo": None,
           "model": {"status": "not_requested"}, "scenario_history": [], "valuation_history": [], "memo_revisions": [], "report_revision": 1, "version": VERSION}
    from .questions import create as create_question
    run["research_questions"]=[create_question(request.get("question","当前研究还有哪些证据缺口？"))]
    with connection() as c:
        c.execute("BEGIN IMMEDIATE")
        active = c.execute("SELECT COUNT(*) FROM runs WHERE state IN ('queued','running')").fetchone()[0]
        if active >= 2:
            raise ValueError("已有两项研究正在进行，请等待完成或取消其中一项")
        c.execute("INSERT INTO runs (id,created,state,payload) VALUES (?,?,?,?)", (run_id,run["created_at"],run["state"],json.dumps(run,ensure_ascii=False)))
    from . import competition
    competition.audit("run_created",run_id=run_id,request=request,policy=competition.POLICY)
    return run

def get_run(run_id):
    with connection() as c:
        row = c.execute("SELECT payload FROM runs WHERE id=? AND NOT EXISTS (SELECT 1 FROM trash WHERE kind='run' AND trash.id=runs.id)", (run_id,)).fetchone()
    if not row:
        raise KeyError(run_id)
    return json.loads(row[0])

def save_run(run):
    from . import competition
    competition.audit("run_snapshot",run_id=run["id"],snapshot=run)
    run["updated_at"] = now()
    with connection() as c:
        c.execute("UPDATE runs SET state=?,payload=? WHERE id=?", (run["state"],json.dumps(run,ensure_ascii=False,allow_nan=False),run["id"]))

def update_run(run_id,change):
    with connection() as c:
        c.execute("BEGIN IMMEDIATE")
        row=c.execute("SELECT payload FROM runs WHERE id=? AND NOT EXISTS (SELECT 1 FROM trash WHERE kind='run' AND trash.id=runs.id)",(run_id,)).fetchone()
        if not row: raise KeyError(run_id)
        run=json.loads(row[0]);change(run);run["updated_at"]=now()
        c.execute("UPDATE runs SET state=?,payload=? WHERE id=?",(run["state"],json.dumps(run,ensure_ascii=False,allow_nan=False),run_id))
    from . import competition
    competition.audit("run_update",run_id=run_id,snapshot=run)
    return run

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
        row=c.execute("SELECT payload FROM conversations WHERE id=? AND NOT EXISTS (SELECT 1 FROM trash WHERE kind='conversation' AND trash.id=conversations.id)",(cid,)).fetchone()
        if not row: raise KeyError(cid)
        value=json.loads(row[0]);change(value);value["updated_at"]=now()
        c.execute("UPDATE conversations SET updated=?,payload=? WHERE id=?",(value["updated_at"],json.dumps(value,ensure_ascii=False,allow_nan=False),cid))
    return value

def begin_turn(cid,text,profile_id,data_source,resume_from_turn_id=None):
    from .turn_constraints import resolve_turn_policy
    with connection() as c:
        c.execute("BEGIN IMMEDIATE")
        row=c.execute("SELECT payload FROM conversations WHERE id=? AND NOT EXISTS (SELECT 1 FROM trash WHERE kind='conversation' AND trash.id=conversations.id)",(cid,)).fetchone()
        if not row: raise KeyError(cid)
        value=json.loads(row[0])
        if value["state"]=="running": raise ValueError("请等待当前回复完成，或先停止")
        active=c.execute("SELECT payload FROM conversations").fetchall()
        if sum(json.loads(r[0])["state"]=="running" for r in active)>=2: raise ValueError("已有两个 Agent 正在运行，请等待完成")
        previous = next((m for m in reversed(value['messages']) if m.get('role')=='assistant'), None)
        # The button and an exact continuation request use the same explicit contract.
        if not resume_from_turn_id and text.strip().rstrip('。.!！') in ('继续','继续未完成部分','继续查证','继续未完成的研究'):
            if previous and previous.get('delivery',{}).get('can_resume'):
                resume_from_turn_id = previous['turn_id']
        inherited = None
        prior_context = {}
        if resume_from_turn_id:
            if not previous or previous.get('turn_id')!=resume_from_turn_id or value.get('turn_id')!=resume_from_turn_id or not previous.get('delivery',{}).get('can_resume'):
                raise ValueError('只能继续最近一轮尚未完成的研究，请刷新对话后再试')
            prior_context = value.get('turn_contexts',{}).get(resume_from_turn_id,{})
            if not prior_context or prior_context.get('run_id')!=value.get('run_id'):
                raise ValueError('研究上下文已变化，请提出新的研究请求')
            inherited = prior_context.get('rules',{})
        policy = resolve_turn_policy(value,text,inherited)
        tid=uuid.uuid4().hex[:12]
        run_row = c.execute('SELECT payload FROM runs WHERE id=?',(value.get('run_id'),)).fetchone() if value.get('run_id') else None
        run = json.loads(run_row[0]) if run_row else {}
        task_ids = list(prior_context.get('task_turn_ids',[])) + [tid]
        context = {'task_turn_id':prior_context.get('task_turn_id',tid),'task_turn_ids':task_ids,
            'original_request':prior_context.get('original_request',text),'user_text':text,
            'resume_from_turn_id':resume_from_turn_id,'run_id':value.get('run_id'),
            'report_revision_before':prior_context.get('report_revision_before',run.get('report_revision')),
            'rules':policy['rules'],'required_artifacts':prior_context.get('required_artifacts',[])}
        value.setdefault('turn_contexts',{})[tid]=context
        value['effective_constraints']=policy
        value['persistent_constraints']=policy['persistent_rules']
        value.update(state="running",turn_id=tid,profile_id=profile_id,data_source=data_source,updated_at=now())
        if not value["messages"]: value["title"]=text[:35]
        value["messages"].append({"id":uuid.uuid4().hex[:12],"role":"user","content":text,"time":now(),"turn_id":tid})
        c.execute("UPDATE conversations SET cancel=0,updated=?,payload=? WHERE id=?",(value["updated_at"],json.dumps(value,ensure_ascii=False),cid))
    return value

def cancel_conversation(cid):
    get_conversation(cid)
    with connection() as c: c.execute("UPDATE conversations SET cancel=1 WHERE id=?",(cid,))

def conversation_cancelled(cid):
    with connection() as c: row=c.execute("SELECT cancel FROM conversations WHERE id=?",(cid,)).fetchone()
    return bool(row and row[0])

def list_runs():
    with connection() as c:
        rows = c.execute("SELECT payload FROM runs WHERE NOT EXISTS (SELECT 1 FROM trash WHERE kind='run' AND trash.id=runs.id) ORDER BY created DESC").fetchall()
    return [{k:v for k,v in json.loads(row[0]).items() if k in {"id","created_at","state","progress","company","request","parent_id"}} for row in rows]

def cancelled(run_id):
    with connection() as c:
        row = c.execute("SELECT cancel FROM runs WHERE id=?",(run_id,)).fetchone()
    return bool(row and row[0])

def cancel_run(run_id):
    get_run(run_id)
    with connection() as c:
        c.execute("UPDATE runs SET cancel=1 WHERE id=?",(run_id,))

def put_document(doc):
    with connection() as c:
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
