"""A frozen turn deadline must interrupt blocking I/O without losing progress."""
from datetime import datetime, timedelta, timezone
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app import budget_timeout, competition, connections, runtime, store


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    for module in (store, connections):
        monkeypatch.setattr(module, 'DATA', tmp_path)
    monkeypatch.setattr(competition, 'POLICY', {'enabled': False, 'model_origins': [], 'retrieval_origins': []})
    monkeypatch.delenv('RESEARCH_COMPETITION_MODE', raising=False)
    monkeypatch.delenv('RESEARCH_COMPETITION_POLICY_JSON', raising=False)
    return tmp_path


def new_turn(run_id=None, seconds=-1):
    chat = store.new_conversation(run_id)
    chat = store.begin_turn(chat['id'], '根据材料完成研究', 'deadline-test', 'documents')
    deadline = datetime.now(timezone.utc) + timedelta(seconds=seconds)

    def freeze(value):
        value['turn_contexts'][value['turn_id']].update(
            deadline_at=deadline.isoformat(),
            execution_budget={'generation_tokens': 49152, 'time_minutes': 1, 'max_decisions': 8})
        value['execution_deadline_at'] = deadline.isoformat()

    return store.update_conversation(chat['id'], freeze)


def new_run():
    return store.create_run({'ticker': '601899.SH', 'year': 2025, 'as_of': '2026-10-14', 'question': '研究经营'})


def test_expiry_preserves_completed_results_reports_and_can_resume(workspace):
    run = new_run()
    store.update_run(run['id'], lambda r: r.update(state='completed', memo={'title': '已保存报告'}))
    chat = new_turn(run['id'])
    tid = chat['turn_id']

    def steps(value):
        value['steps'] = [
            {'id': 'done', 'turn_id': tid, 'tool': 'read_document', 'state': 'completed', 'result': {'text': '已读原文'}},
            {'id': 'waiting', 'turn_id': tid, 'tool': 'build_investment_memo', 'state': 'running'},
        ]

    store.update_conversation(chat['id'], steps)
    saved = budget_timeout.expire_conversation(chat['id'], tid)
    assert saved['state'] == 'failed'
    assert saved['stop_reason'] == 'time_limit'
    assert not saved['cancel_requested']
    assert saved['steps'][0]['result'] == {'text': '已读原文'}
    assert saved['steps'][0]['state'] == 'completed'
    assert saved['steps'][1]['state'] == 'failed'
    assert saved['steps'][1]['stop_reason'] == 'time_limit'
    assert '时间上限' in saved['messages'][-1]['content']
    assert '取消' not in saved['messages'][-1]['content']
    assert saved['messages'][-1]['delivery']['can_resume']
    assert saved['messages'][-1]['delivery']['completed'][0]['step_id'] == 'done'
    assert store.get_run(run['id'])['memo'] == {'title': '已保存报告'}
    assert budget_timeout.expire_conversation(chat['id'], tid) == saved
    resumed = store.begin_turn(chat['id'], '继续未完成部分', 'deadline-test', 'documents', tid)
    assert resumed['state'] == 'running'
    assert resumed['run_id'] == run['id']
    assert resumed['turn_id'] != tid
    assert 'stop_reason' not in resumed
    assert 'budget_exhausted' not in resumed


def test_expiry_only_revokes_runs_owned_by_exact_turn(workspace):
    existing = new_run()
    chat = new_turn(existing['id'])
    with store.execution_scope('conversation', chat['id'], chat['turn_id']):
        owned = new_run()
    budget_timeout.expire_conversation(chat['id'], chat['turn_id'])
    assert store.get_run(existing['id'])['state'] == 'queued'
    assert not store.cancelled(existing['id'])
    assert store.get_run(owned['id'])['state'] == 'failed'
    assert store.get_run(owned['id'])['stop_reason'] == 'time_limit'
    assert store.cancelled(owned['id'])


def test_late_or_wrong_deadline_cannot_expire_new_turn(workspace):
    old = new_turn()
    original = budget_timeout.deadline_for(old, old['turn_id'])
    unchanged = budget_timeout.expire_conversation(old['id'], old['turn_id'], original - timedelta(seconds=1))
    assert unchanged['state'] == 'running'
    budget_timeout.expire_conversation(old['id'], old['turn_id'])
    current = store.begin_turn(old['id'], '新问题', 'deadline-test', 'documents')
    assert budget_timeout.expire_conversation(old['id'], old['turn_id'], original) == current
    assert not store.conversation_cancelled(old['id'])


def test_not_due_and_mutable_chat_defaults_do_not_change_frozen_deadline(workspace):
    chat = new_turn(seconds=60)
    original = budget_timeout.deadline_for(chat, chat['turn_id'])
    changed = store.update_conversation(chat['id'], lambda c: c.update(
        execution_budget={'time_minutes': 60},
        execution_deadline_at=(original + timedelta(hours=1)).isoformat()))
    assert budget_timeout.deadline_for(changed, chat['turn_id']) == original
    assert budget_timeout.expire_conversation(chat['id'], chat['turn_id']) == changed


def test_expired_inflight_worker_cannot_write_after_resume(workspace):
    run = new_run()
    chat = new_turn(run['id'])
    with store.execution_scope('conversation', chat['id'], chat['turn_id']):
        budget_timeout.expire_conversation(chat['id'], chat['turn_id'])
        current = store.begin_turn(chat['id'], '新问题', 'deadline-test', 'documents')
        for write in (
            lambda: store.update_conversation(chat['id'], lambda c: c.update(state='idle')),
            lambda: store.update_run(run['id'], lambda r: r.update(memo={'title': '过期结果'})),
            lambda: store.save_run(run),
            lambda: store.put_document({'id': 'stale'}),
        ):
            with pytest.raises(store.ExecutionStopped):
                write()
    assert store.get_conversation(chat['id'])['turn_id'] == current['turn_id']
    assert store.get_run(run['id'])['memo'] is None
    assert not store.list_documents()


def test_watchdog_retries_transient_persistence_failure(workspace, monkeypatch):
    chat = new_turn()
    real_expire = budget_timeout.expire_conversation
    calls = []
    exited = threading.Event()

    def temporary_failure(*args):
        calls.append(True)
        if len(calls) == 1:
            raise OSError('temporary database failure')
        return real_expire(*args)

    monkeypatch.setattr(budget_timeout, 'expire_conversation', temporary_failure)
    watchdog = budget_timeout.watch_turn(chat['id'], chat['turn_id'], exit_process=lambda code: exited.set())
    try:
        assert exited.wait(2)
        assert len(calls) == 2
        assert store.get_conversation(chat['id'])['stop_reason'] == 'time_limit'
    finally:
        watchdog.set()


def test_watchdog_completion_disarms_timeout(workspace):
    chat = new_turn(seconds=0.1)
    exited = threading.Event()
    watchdog = budget_timeout.watch_turn(chat['id'], chat['turn_id'], exit_process=lambda code: exited.set())
    watchdog.set()
    assert not exited.wait(0.2)
    assert store.get_conversation(chat['id'])['state'] == 'running'


def test_old_watchdog_exits_only_itself_without_touching_new_turn(workspace):
    chat = new_turn(seconds=0.1)
    exited = threading.Event()
    watchdog = budget_timeout.watch_turn(chat['id'], chat['turn_id'], exit_process=lambda code: exited.set())
    try:
        store.cancel_conversation(chat['id'], chat['turn_id'])
        current = store.begin_turn(chat['id'], '新的研究问题', 'deadline-test', 'documents')
        assert exited.wait(1)
        assert store.get_conversation(chat['id']) == current
        assert not store.conversation_cancelled(chat['id'])
    finally:
        watchdog.set()


def test_real_worker_deadline_interrupts_continuously_streaming_local_http(workspace):
    entered = threading.Event()
    release = threading.Event()

    class SlowModel(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get('Content-Length', '0')))
            entered.set()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', '999999')
            self.end_headers()
            try:
                # Keep the socket active so per-read HTTP timeouts cannot be
                # mistaken for the hard end-to-end turn deadline.
                while not release.wait(0.05):
                    self.wfile.write(b' ')
                    self.wfile.flush()
            except (ConnectionError, OSError):
                pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), SlowModel)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connections.save({'active_model': 'deadline-test', 'data_source': 'documents', 'tushare_legacy': False,
        'profiles': [{'id': 'deadline-test', 'provider': 'ollama', 'name': 'Deadline test',
                     'base': f'http://127.0.0.1:{server.server_port}/v1', 'model': 'local-fixture',
                     'protocol': 'openai', 'json_mode': True, 'legacy_key': False}]})
    chat = new_turn(seconds=3)
    deadline = budget_timeout.deadline_for(chat, chat['turn_id'])
    process = None
    try:
        process = runtime.launch('conversation', chat['id'], chat['turn_id'])
        assert entered.wait(2.5), 'worker never reached the isolated model before its deadline'
        process.wait(timeout=4)
        elapsed_after_deadline = (datetime.now(timezone.utc) - deadline).total_seconds()
        assert process.returncode == 124
        assert elapsed_after_deadline < 1.5
        assert not release.is_set()
        saved = store.get_conversation(chat['id'])
        assert saved['state'] == 'failed'
        assert saved['stop_reason'] == 'time_limit'
        assert saved['messages'][-1]['delivery']['can_resume']
    finally:
        release.set()
        if process and process.poll() is None:
            process.terminate()
            process.wait(timeout=2)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
