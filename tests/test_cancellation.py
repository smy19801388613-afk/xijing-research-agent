"""Cancellation must stop waiting now and reject obsolete writes afterwards."""
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from app import agent, competition, connections, main, runtime, store


@pytest.fixture
def workspace(tmp_path,monkeypatch):
    for module in (store,main,connections): monkeypatch.setattr(module,'DATA',tmp_path)
    monkeypatch.setattr(competition,'POLICY',{'enabled':False,'model_origins':[],'retrieval_origins':[]})
    monkeypatch.delenv('RESEARCH_COMPETITION_MODE',raising=False)
    monkeypatch.delenv('RESEARCH_COMPETITION_POLICY_JSON',raising=False)
    return tmp_path


def new_turn(run_id=None):
    chat=store.new_conversation(run_id)
    return store.begin_turn(chat['id'],'请根据材料回答','local-test','documents')


def create_run():
    return store.create_run({'ticker':'601899.SH','year':2025,'as_of':'2026-10-14','question':'研究经营'})


def test_cancel_is_immediate_idempotent_and_keeps_completed_results(workspace):
    chat=new_turn();tid=chat['turn_id']
    def steps(value):
        value['steps']=[{'id':'done','turn_id':tid,'tool':'read_document','state':'completed','result':{'text':'已读原文'}},
                        {'id':'waiting','turn_id':tid,'tool':'build_investment_memo','state':'running'}]
    store.update_conversation(chat['id'],steps)
    client=TestClient(main.app)
    result=client.post(f"/api/conversations/{chat['id']}/cancel",json={'turn_id':tid})
    assert result.status_code==200
    saved=result.json()
    assert saved['state']=='cancelled'
    assert saved['steps'][0]['result']=={'text':'已读原文'}
    assert saved['steps'][0]['state']=='completed'
    assert saved['steps'][1]['state']=='cancelled'
    assert saved['messages'][-1]['delivery']['completed'][0]['step_id']=='done'
    assert saved['messages'][-1]['delivery']['can_resume']
    repeated=client.post(f"/api/conversations/{chat['id']}/cancel",json={'turn_id':tid}).json()
    assert repeated==saved


def test_late_cancel_and_old_worker_do_not_touch_new_turn(workspace,monkeypatch):
    old=new_turn();store.cancel_conversation(old['id'],old['turn_id'])
    current=store.begin_turn(old['id'],'继续未完成部分','local-test','documents',old['turn_id'])
    response=TestClient(main.app).post(f"/api/conversations/{old['id']}/cancel",json={'turn_id':old['turn_id']})
    assert response.json()['turn_id']==current['turn_id']
    assert response.json()['state']=='running'
    assert not store.conversation_cancelled(old['id'])
    monkeypatch.setattr(agent,'model_json',lambda *a,**kw:pytest.fail('obsolete worker called model'))
    agent.execute(old['id'],old['turn_id'])
    agent.finish_error(old['id'],old['turn_id'],'late failure','failed')
    assert store.get_conversation(old['id'])['state']=='running'


def test_cancel_only_stops_runs_created_by_this_turn(workspace):
    existing=create_run()
    store.update_run(existing['id'],lambda run:run.update(state='completed_with_gaps'))
    chat=new_turn(existing['id'])
    other=new_turn(existing['id'])
    with store.execution_scope('conversation',chat['id'],chat['turn_id']):
        owned=create_run()
    store.cancel_conversation(chat['id'],chat['turn_id'])
    assert store.get_run(existing['id'])['state']=='completed_with_gaps'
    assert not store.cancelled(existing['id'])
    assert store.get_conversation(other['id'])['state']=='running'
    assert store.get_run(owned['id'])['state']=='cancelled'
    assert store.cancelled(owned['id'])


def test_obsolete_inflight_worker_cannot_save_any_result(workspace):
    run=create_run();chat=new_turn(run['id'])
    with store.execution_scope('conversation',chat['id'],chat['turn_id']):
        store.cancel_conversation(chat['id'],chat['turn_id'])
        current=store.begin_turn(chat['id'],'新问题','local-test','documents')
        for write in (
            lambda:store.update_conversation(chat['id'],lambda c:c.update(state='idle')),
            lambda:store.update_run(run['id'],lambda r:r.update(memo={'title':'过期结果'})),
            lambda:store.save_run(run),
            lambda:store.put_document({'id':'stale'}),
            create_run,
        ):
            with pytest.raises(store.ExecutionStopped): write()
    assert store.get_conversation(chat['id'])['turn_id']==current['turn_id']
    assert store.get_run(run['id'])['memo'] is None
    assert not store.list_documents()


def test_standalone_cancelled_worker_cannot_resurrect_run(workspace):
    run=create_run()
    with store.execution_scope('run',run['id']):
        store.cancel_run(run['id'])
        run['state']='completed_with_gaps'
        with pytest.raises(store.ExecutionStopped): store.save_run(run)
    assert store.get_run(run['id'])['state']=='cancelled'


def test_cancel_during_new_research_can_resume_in_the_attached_context(workspace,monkeypatch):
    chat=new_turn()
    material=competition.register_material(json.dumps([{'metric':'revenue','year':2025,'value':100,
        'unit':'元','currency':'CNY','scope':'consolidated','period_type':'annual',
        'announced_date':'2026-03-31'}]).encode(),'cancel-test.json','601899.SH',2025)
    monkeypatch.setattr(agent.research,'execute',lambda rid:store.cancel_conversation(chat['id'],chat['turn_id']))
    with store.execution_scope('conversation',chat['id'],chat['turn_id']):
        result=agent.tool(chat,'start_research',{'ticker':'601899.SH','year':2025,'question':'研究经营',
                                              'material_ids':[material['id']]})
    saved=store.get_conversation(chat['id'])
    assert saved['turn_contexts'][chat['turn_id']]['run_id']==result['run_id']
    assert saved['messages'][-1]['delivery']['can_resume']
    resumed=store.begin_turn(chat['id'],'继续未完成部分','local-test','documents',chat['turn_id'])
    assert resumed['state']=='running'
    assert resumed['run_id']==result['run_id']


def test_late_termination_receipt_does_not_change_new_turn(workspace):
    chat=new_turn();store.cancel_conversation(chat['id'],chat['turn_id'])
    current=store.begin_turn(chat['id'],'新问题','local-test','documents')
    late=store.record_termination(chat['id'],chat['turn_id'],{'status':'pending','retryable':True,'turn_id':chat['turn_id']})
    assert late==current
    assert 'termination' not in late


def sleeping_process():
    return subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],
                            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))


def test_pid_creation_time_mismatch_does_not_terminate_unrelated_process(workspace):
    process=sleeping_process()
    try:
        birth=runtime.process_birth(process.pid)
        assert birth
        assert not runtime.terminate_verified(process.pid,birth+'invalid')
        assert process.poll() is None
        assert runtime.terminate_verified(process.pid,birth)
        process.wait(timeout=2)
        assert process.returncode is not None
    finally:
        if process.poll() is None: process.terminate();process.wait(timeout=2)


def test_termination_failure_retains_exact_identity_and_can_retry(workspace,monkeypatch):
    chat=new_turn();process=sleeping_process()
    try:
        birth=runtime.process_birth(process.pid)
        with store.connection() as c:
            c.execute('INSERT INTO workers VALUES (?,?,?,?,?)',('conversation',chat['id'],chat['turn_id'],process.pid,birth))
        client=TestClient(main.app)
        endpoint=f"/api/conversations/{chat['id']}/cancel"
        with monkeypatch.context() as patch:
            patch.setattr(runtime,'terminate_verified',lambda *a:False)
            saved=client.post(endpoint,json={'turn_id':chat['turn_id']}).json()
        assert saved['state']=='cancelled'
        assert saved['termination']['status']=='pending'
        assert saved['termination']['retryable']
        assert process.poll() is None
        with store.connection() as c:
            assert c.execute('SELECT pid,birth FROM workers').fetchone()==(process.pid,birth)
        retried=client.post(endpoint,json={'turn_id':chat['turn_id']}).json()
        assert retried['termination']['status']=='stopped'
        assert not retried['termination']['retryable']
        process.wait(timeout=2)
        with store.connection() as c: assert not c.execute('SELECT * FROM workers').fetchall()
        assert len(retried['messages'])==len(saved['messages'])
        assert client.post(endpoint,json={'turn_id':chat['turn_id']}).json()==retried
    finally:
        if process.poll() is None: process.terminate();process.wait(timeout=2)


def test_cancel_racing_with_worker_registration_stops_late_process(workspace,monkeypatch):
    chat=new_turn();process=sleeping_process()
    def spawn(*args,**kwargs):
        store.cancel_conversation(chat['id'],chat['turn_id'])
        return process
    monkeypatch.setattr(runtime.subprocess,'Popen',spawn)
    try:
        runtime.launch('conversation',chat['id'],chat['turn_id'])
        process.wait(timeout=2)
        assert store.get_conversation(chat['id'])['state']=='cancelled'
        with store.connection() as c: assert not c.execute('SELECT * FROM workers').fetchall()
    finally:
        if process.poll() is None: process.terminate();process.wait(timeout=2)


def test_real_worker_cancel_interrupts_slow_local_http(workspace):
    entered=threading.Event();release=threading.Event()
    class SlowModel(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_POST(self):
            self.rfile.read(int(self.headers.get('Content-Length','0')))
            entered.set()
            release.wait(12)
            body=json.dumps({'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'action':'respond','message':'迟到的回答'})}}]}).encode()
            try:
                self.send_response(200);self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
            except (ConnectionError,OSError): pass
    server=ThreadingHTTPServer(('127.0.0.1',0),SlowModel)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    connections.save({'active_model':'local-test','data_source':'documents','tushare_legacy':False,'profiles':[
        {'id':'local-test','provider':'ollama','name':'Cancellation test','base':f'http://127.0.0.1:{server.server_port}/v1',
         'model':'local-fixture','protocol':'openai','json_mode':True,'legacy_key':False}]})
    chat=new_turn();process=None
    try:
        process=runtime.launch('conversation',chat['id'],chat['turn_id'])
        assert entered.wait(10),'worker never reached the isolated local model'
        started=time.monotonic()
        response=TestClient(main.app).post(f"/api/conversations/{chat['id']}/cancel",json={'turn_id':chat['turn_id']})
        elapsed=time.monotonic()-started
        assert response.status_code==200
        assert response.json()['state']=='cancelled'
        assert elapsed<1.5,f'cancel took {elapsed:.3f}s while HTTP was waiting'
        process.wait(timeout=2)
        assert process.returncode is not None
        assert not release.is_set(),'cancel must finish before the model returns'
        assert '迟到的回答' not in json.dumps(store.get_conversation(chat['id']),ensure_ascii=False)
    finally:
        release.set()
        if process and process.poll() is None: process.terminate();process.wait(timeout=2)
        server.shutdown();server.server_close();thread.join(timeout=2)
