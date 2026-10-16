"""Isolated entry-point budgets, process recovery and stale snapshot protection."""
import json
import subprocess
import sys
import threading
import time
from datetime import datetime,timedelta,timezone
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer

import pytest

from app import budget_timeout,competition,connections,deep_research,documents,execution_budget as budgets
from app import main,providers,research,runtime,store
from app.schemas import RunRequest


@pytest.fixture
def workspace(tmp_path,monkeypatch):
    for module in (store,research,main,connections): monkeypatch.setattr(module,'DATA',tmp_path)
    monkeypatch.setattr(competition,'POLICY',{'enabled':False,'model_origins':[],'retrieval_origins':[]})
    monkeypatch.delenv('RESEARCH_COMPETITION_MODE',raising=False)
    monkeypatch.delenv('RESEARCH_COMPETITION_POLICY_JSON',raising=False)
    return tmp_path


def new_run(**kw):
    return store.create_run(RunRequest(**kw).model_dump(mode='json'))


def age_run(run_id):
    return store.update_run(run_id,lambda r:r.update(created_at=(datetime.now(timezone.utc)-timedelta(minutes=2)).isoformat()))


def test_new_research_freezes_default_or_explicit_budget(workspace,monkeypatch):
    default={'total_tokens':150000,'time_minutes':3}
    monkeypatch.setattr(connections,'load',lambda:{'execution_budget':default,'active_model':'local','data_source':'documents'})
    monkeypatch.setattr(runtime,'launch',lambda *a:None)
    first=main.create(RunRequest(use_model=False))
    second=main.create(RunRequest(use_model=False,execution_budget={'total_tokens':50000,'time_minutes':1}))
    default['total_tokens']=500000
    assert store.get_run(first['id'])['execution_budget']=={'total_tokens':150000,'time_minutes':3}
    assert second['execution_budget']=={'total_tokens':50000,'time_minutes':1}


def test_standalone_nested_model_attempts_share_persisted_token_budget(workspace,monkeypatch):
    documents.seed()
    run=new_run(mode='live',data_source='documents',execution_budget={'total_tokens':50000,'time_minutes':2})
    def build(*a,**kw):
        first=budgets.reserve(30000,10000)
        budgets.settle(first,{'total_tokens':35000})
        with budgets.scope(budgets.DEFAULTS):
            budgets.reserve(30000,10000)
    monkeypatch.setattr(deep_research,'build',build)
    with pytest.raises(budgets.BudgetExhausted):
        with store.execution_scope('run',run['id']): research.execute(run['id'])
    saved=store.get_run(run['id'])
    assert saved['state']=='failed' and saved['stop_reason']=='token_limit'
    assert saved['budget_usage']['reported_tokens']==35000
    assert saved['budget_usage']['remaining_tokens']==15000
    assert saved['budget_usage']['request_count']==1
    assert saved['facts'] and saved['memo']


def test_usage_receipts_do_not_conflict_with_business_snapshot_or_get_overwritten(workspace):
    run=new_run();version=run['_write_version']
    with budgets.scope(run['execution_budget'],on_usage=lambda usage:store.record_run_budget_usage(run['id'],usage)):
        reservation=budgets.reserve(2000,1000)
        budgets.settle(reservation,{'total_tokens':1400})
        assert store.get_run(run['id'])['_write_version']==version
        research.step(run,'保存后续业务步骤',50)
    saved=store.get_run(run['id'])
    assert saved['budget_usage']['reported_tokens']==1400
    assert saved['budget_usage']['reserved_tokens']==0
    assert saved['_write_version']==version+1


def test_complete_model_path_keeps_budget_and_saves_terminal_run(workspace,monkeypatch):
    documents.seed();requests=[]
    class Model(BaseHTTPRequestHandler):
        def log_message(self,*a): pass
        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            payload=json.dumps({'choices':[{'finish_reason':'stop','message':{'content':'{"ok":true}'}}],
                                'usage':{'prompt_tokens':900,'completion_tokens':100,'total_tokens':1000}}).encode()
            self.send_response(200);self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
    server=ThreadingHTTPServer(('127.0.0.1',0),Model)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    connections.save({'active_model':'complete-test','data_source':'documents','tushare_legacy':False,'profiles':[
        {'id':'complete-test','provider':'ollama','name':'Local completion','base':f'http://127.0.0.1:{server.server_port}/v1',
         'model':'local-fixture','protocol':'openai','json_mode':True,'legacy_key':False}]})
    run=new_run(mode='live',data_source='documents',model_profile='complete-test',execution_budget={'total_tokens':50000,'time_minutes':1})
    def build(*a,**kw):
        for _ in range(2):
            value,meta=providers.model_json('controlled fixture',{'question':'test'},100,profile_id='complete-test')
            assert value=={'ok':True}
    monkeypatch.setattr(deep_research,'build',build)
    try:
        with store.execution_scope('run',run['id']): research.execute(run['id'])
        saved=store.get_run(run['id'])
        assert saved['state']=='completed_with_gaps' and saved['model']['status']=='completed'
        assert saved['budget_usage']['reported_tokens']==2000 and saved['budget_usage']['request_count']==2
        assert saved['budget_usage']['reserved_tokens']==0 and len(requests)==2
    finally:
        server.shutdown();server.server_close();thread.join(timeout=2)


def test_inline_research_inherits_chat_ledger_and_deadline(workspace):
    chat=store.begin_turn(store.new_conversation()['id'],'读取材料','test','documents')
    limit={'total_tokens':50000,'time_minutes':1}
    with store.execution_scope('conversation',chat['id'],chat['turn_id']):
        with budgets.scope(limit,deadline_at=time.monotonic()+20):
            reservation=budgets.reserve(5000,1000);budgets.settle(reservation,{'total_tokens':4500})
            run=new_run(execution_budget={'total_tokens':1000000,'time_minutes':30})
            @research.with_research_budget
            def nested(run_id):
                assert budgets.snapshot()['used_tokens']==4500
                r=budgets.reserve(1000,1000);budgets.settle(r,{'total_tokens':1200})
            nested(run['id'])
            assert budgets.snapshot()['used_tokens']==5700
    assert run['execution_budget']==limit
    assert (budget_timeout.run_deadline(run)-datetime.now(timezone.utc)).total_seconds()<21


def test_legacy_snapshot_migrates_and_stale_snapshot_cannot_drop_new_writes(workspace):
    run=new_run();run.pop('_write_version')
    with store.connection() as c:
        c.execute('UPDATE runs SET payload=? WHERE id=?',(json.dumps(run),run['id']))
    legacy=store.get_run(run['id']);store.save_run(legacy)
    assert legacy['_write_version']==1
    store.update_run(run['id'],lambda r:r['events'].append({'label':'newer event'}))
    legacy['progress']=50
    with pytest.raises(store.ConcurrentRunUpdate): store.save_run(legacy)
    assert store.get_run(run['id'])['events']==[{'label':'newer event'}]


def test_research_write_conflict_retains_newer_state_and_reports_interruption(workspace,monkeypatch):
    documents.seed();run=new_run()
    original=research.step
    def step(value,label,*a,**kw):
        if label=='读取历史快照':
            store.update_run(run['id'],lambda r:r['events'].append({'label':'external edit'}))
        return original(value,label,*a,**kw)
    monkeypatch.setattr(research,'step',step)
    research.execute(run['id'])
    saved=store.get_run(run['id'])
    assert saved['state']=='failed' and saved['stop_reason']=='write_conflict'
    assert any(e['label']=='external edit' for e in saved['events'])


def test_provider_error_after_external_write_never_saves_old_snapshot(workspace,monkeypatch):
    documents.seed();run=new_run()
    original=research.step
    def step(value,label,*a,**kw):
        if label=='读取历史快照':
            store.update_run(run['id'],lambda r:r['events'].append({'label':'external edit'}))
            raise providers.ProviderError('controlled failure after concurrent write')
        return original(value,label,*a,**kw)
    monkeypatch.setattr(research,'step',step)
    research.execute(run['id'])
    saved=store.get_run(run['id'])
    assert saved['state']=='failed' and saved['stop_reason']=='write_conflict'
    assert any(e['label']=='external edit' for e in saved['events'])


def test_spawn_failure_is_terminal_and_releases_capacity(workspace,monkeypatch):
    run=new_run()
    def failed(*a,**kw): raise OSError('controlled Popen failure')
    monkeypatch.setattr(runtime.subprocess,'Popen',failed)
    with pytest.raises(ValueError,match='无法启动'): main.launch(run)
    saved=store.get_run(run['id'])
    assert saved['state']=='failed' and saved['stop_reason']=='launch_failed'
    with store.connection() as c: assert store.active_execution_count(c)==0
    assert not runtime.reconcile()


def test_registration_failure_stops_only_newly_spawned_process(workspace,monkeypatch):
    run=new_run()
    process=subprocess.Popen([sys.executable,'-c','import time;time.sleep(20)'],creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    monkeypatch.setattr(runtime.subprocess,'Popen',lambda *a,**kw:process)
    def missing_identity(*a): raise OSError('controlled registration query failure')
    monkeypatch.setattr(runtime,'process_birth',missing_identity)
    try:
        with pytest.raises(OSError,match='controlled'): runtime.launch('run',run['id'])
        process.wait(timeout=2)
        assert store.get_run(run['id'])['stop_reason']=='launch_failed'
    finally:
        if process.poll() is None: process.terminate();process.wait(timeout=2)


def test_duplicate_launch_does_not_revoke_live_worker(workspace,monkeypatch):
    run=new_run()
    process=subprocess.Popen([sys.executable,'-c','import time;time.sleep(20)'],creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    monkeypatch.setattr(runtime.subprocess,'Popen',lambda *a,**kw:process)
    try:
        runtime.launch('run',run['id'])
        before=store.get_run(run['id'])
        with pytest.raises(ValueError,match='已启动'): runtime.launch('run',run['id'])
        assert process.poll() is None and store.get_run(run['id'])==before
        assert runtime.reconcile()==[]
    finally:
        store.cancel_run(run['id']);runtime.stop('run',run['id']);process.wait(timeout=2)


def test_configured_long_run_is_not_cut_off_by_old_hardcoded_fifteen_minutes(workspace):
    run=new_run(execution_budget={'total_tokens':50000,'time_minutes':60})
    run['started_at']=(datetime.now(timezone.utc)-timedelta(minutes=20)).isoformat()
    with budgets.scope(run['execution_budget']): research.step(run,'continue within chosen limit',50)
    assert store.get_run(run['id'])['progress']==50


def test_two_entry_points_share_slots_but_inline_run_uses_owner_slot(workspace):
    standalone=new_run()
    chat=store.begin_turn(store.new_conversation()['id'],'读取材料','test','documents')
    with pytest.raises(ValueError): new_run()
    with pytest.raises(ValueError): store.begin_turn(store.new_conversation()['id'],'读取材料','test','documents')
    with store.execution_scope('conversation',chat['id'],chat['turn_id']): owned=new_run()
    with store.connection() as c: assert store.active_execution_count(c)==2
    assert owned['execution_owner']['conversation_id']==chat['id']


def test_reconcile_only_old_unregistered_active_work(workspace):
    run=age_run(new_run()['id'])
    terminal=new_run();store.update_run(terminal['id'],lambda r:r.update(state='cancelled',memo={'summary':'keep'}))
    frozen=store.get_run(terminal['id'])
    assert runtime.reconcile()==[{'kind':'run','id':run['id'],'turn_id':''}]
    assert store.get_run(run['id'])['stop_reason']=='worker_exit'
    assert store.get_run(terminal['id'])==frozen
    fresh=new_run()
    assert runtime.reconcile()==[]
    assert store.get_run(fresh['id'])['state']=='queued'


@pytest.mark.parametrize('state',['current','unknown'])
def test_reconcile_preserves_live_or_unqueryable_registered_identity(workspace,monkeypatch,state):
    run=age_run(new_run()['id'])
    with store.connection() as c: c.execute('INSERT INTO workers VALUES (?,?,?,?,?)',('run',run['id'],'',12345,'birth'))
    monkeypatch.setattr(runtime,'process_state',lambda *a:state)
    before=store.get_run(run['id'])
    assert runtime.reconcile()==[] and store.get_run(run['id'])==before


def test_reconcile_rechecks_registration_before_revoking(workspace,monkeypatch):
    run=age_run(new_run()['id'])
    with store.connection() as c: c.execute('INSERT INTO workers VALUES (?,?,?,?,?)',('run',run['id'],'',12345,'old'))
    def state(*a):
        with store.connection() as c: c.execute('UPDATE workers SET birth=? WHERE target=?',('new',run['id']))
        return 'exited'
    monkeypatch.setattr(runtime,'process_state',state)
    assert runtime.reconcile()==[] and store.get_run(run['id'])['state']=='queued'


def test_reconcile_does_not_revoke_a_launch_that_started_after_its_snapshot(workspace,monkeypatch):
    run=age_run(new_run()['id']);original=store.fail_execution
    def racing_launch(*a,**kw):
        store.update_run(run['id'],lambda r:r.update(execution_launch={'nonce':'new-launch','requested_at':store.now(),'turn_id':''}))
        return original(*a,**kw)
    monkeypatch.setattr(store,'fail_execution',racing_launch)
    assert runtime.reconcile()==[] and store.get_run(run['id'])['state']=='queued'


def test_real_dead_worker_recovery_preserves_chat_progress_and_budget(workspace):
    chat=store.begin_turn(store.new_conversation()['id'],'读取资料并保存报告','test','documents')
    tid=chat['turn_id']
    def prepare(c):
        c['steps']=[{'id':'done','turn_id':tid,'tool':'read_document','state':'completed','result':{'text':'已读原文'}},
                    {'id':'pending','turn_id':tid,'tool':'build_investment_memo','state':'running'}]
        c['budget_usage'].update(reserved_tokens=1000,request_count=1)
    store.update_conversation(chat['id'],prepare)
    process=subprocess.Popen([sys.executable,'-c','import time;time.sleep(.3)'],creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    birth=runtime.process_birth(process.pid)
    assert birth
    with store.connection() as c: c.execute('INSERT INTO workers VALUES (?,?,?,?,?)',('conversation',chat['id'],tid,process.pid,birth))
    process.wait(timeout=3)
    assert runtime.reconcile()
    saved=store.get_conversation(chat['id'])
    assert saved['state']=='failed' and saved['stop_reason']=='worker_exit'
    assert saved['steps'][0]['result']['text']=='已读原文'
    assert saved['messages'][-1]['delivery']['can_resume']
    assert saved['budget_usage']['estimated_tokens']==1000
    assert runtime.reconcile()==[]


def test_run_expiry_freezes_fence_usage_and_saved_report(workspace):
    run=new_run()
    def prepare(r):
        r.update(execution_deadline_at=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(),memo={'summary':'saved'})
        r['budget_usage'].update(reserved_tokens=2000,request_count=1)
    store.update_run(run['id'],prepare)
    saved=budget_timeout.expire_run(run['id'])
    assert saved['state']=='failed' and saved['stop_reason']=='time_limit'
    assert saved['memo']=={'summary':'saved'} and saved['budget_usage']['estimated_tokens']==2000
    with pytest.raises(store.ExecutionStopped):
        with store.execution_scope('run',run['id']): store.update_run(run['id'],lambda r:r.update(progress=99))
    assert budget_timeout.expire_run(run['id'])==saved


def test_real_standalone_deadline_interrupts_continuously_streaming_model(workspace):
    documents.seed();entered=threading.Event();release=threading.Event()
    class SlowModel(BaseHTTPRequestHandler):
        def log_message(self,*a): pass
        def do_POST(self):
            self.rfile.read(int(self.headers.get('Content-Length','0')))
            entered.set();self.send_response(200);self.send_header('Content-Length','999999');self.end_headers()
            try:
                while not release.wait(.05): self.wfile.write(b' ');self.wfile.flush()
            except (ConnectionError,OSError): pass
    server=ThreadingHTTPServer(('127.0.0.1',0),SlowModel)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    connections.save({'active_model':'run-test','data_source':'documents','tushare_legacy':False,'profiles':[
        {'id':'run-test','provider':'ollama','name':'Local run test','base':f'http://127.0.0.1:{server.server_port}/v1',
         'model':'local-fixture','protocol':'openai','json_mode':True,'legacy_key':False}]})
    run=new_run(mode='live',data_source='documents',model_profile='run-test',execution_budget={'total_tokens':50000,'time_minutes':1})
    store.update_run(run['id'],lambda r:r.update(execution_deadline_at=(datetime.now(timezone.utc)+timedelta(seconds=4)).isoformat()))
    process=None
    try:
        process=runtime.launch('run',run['id'])
        assert entered.wait(3),'standalone worker did not reach controlled local model'
        process.wait(timeout=4)
        assert process.returncode==124 and not release.is_set()
        saved=store.get_run(run['id'])
        assert saved['stop_reason']=='time_limit' and saved['state']=='failed'
        assert saved['facts'] and saved['budget_usage']['estimated_tokens']>0
        assert runtime.reconcile()==[]
    finally:
        release.set()
        if process and process.poll() is None: process.terminate();process.wait(timeout=2)
        server.shutdown();server.server_close();thread.join(timeout=2)
