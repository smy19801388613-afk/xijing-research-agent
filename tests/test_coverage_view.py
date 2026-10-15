import copy
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import budget_timeout, main, store
from app.coverage_view import conversation_view, coverage_notice, turn_observation


BODY = {'id': 'web-annual', 'kind': 'web', 'url': 'https://example.com/annual', 'title': '经营披露', 'text': '已取得的经营资料正文。',
        'published_at': '2026-01-01', 'read_status': 'body_read'}


def recorded_step(tid):
    return {'id': 'deep', 'turn_id': tid, 'tool': 'build_investment_memo', 'state': 'running',
            'retrieval_events_schema': 1, 'retrieval_events': [
                {'id': 'search', 'kind': 'search', 'state': 'completed', 'arguments': {'queries': ['公司']},
                 'result': {'results': [{**BODY, 'text': '', 'read_status': 'search_snippet'}]}},
                {'id': 'read', 'kind': 'read_webpage', 'state': 'completed', 'result': BODY},
                {'id': 'waiting', 'kind': 'read_webpage', 'state': 'running',
                 'arguments': {'url': 'https://example.com/waiting'}}]}


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    run = store.create_run({'ticker': '601899.SH', 'year': 2025, 'as_of': '2026-10-15', 'question': '经营情况'})
    store.update_run(run['id'], lambda r: r.update(state='completed', research_sources=[BODY]))
    chat = store.new_conversation(run['id'])
    return store.begin_turn(chat['id'], '联网搜索并阅读原文', 'stub', 'documents')


def test_continuation_separates_current_activity_from_task(workspace):
    first = workspace
    store.update_conversation(first['id'], lambda c: c['steps'].append(recorded_step(c['turn_id'])))
    store.cancel_conversation(first['id'])
    resumed = store.begin_turn(first['id'], '继续未完成部分', 'stub', 'documents')
    run = store.get_run(first['run_id'])
    observed = turn_observation(resumed, run)
    assert observed['search_calls'] == observed['body_sources'] == 0
    assert observed['task_coverage']['search_calls'] == observed['task_coverage']['body_sources'] == 1
    assert observed['saved_body_sources'] == 1
    assert '本任务此前已取得原文' in coverage_notice(observed)
    store.cancel_conversation(first['id'])
    fresh = store.begin_turn(first['id'], '新的研究问题：阅读原文', 'stub', 'documents')
    assert turn_observation(fresh, run)['task_coverage']['search_calls'] == 0


@pytest.mark.parametrize('stop', ['cancel', 'deadline'])
def test_stopping_retains_completed_nested_activity(workspace, stop):
    chat = workspace
    def prepare(c):
        c['steps'].append(recorded_step(c['turn_id']))
        c['turn_contexts'][c['turn_id']]['deadline_at'] = (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
    store.update_conversation(chat['id'], prepare)
    saved = (store.cancel_conversation(chat['id']) if stop == 'cancel' else
             budget_timeout.expire_conversation(chat['id'], chat['turn_id']))
    observed = saved['messages'][-1]['coverage']
    assert observed['search_calls'] == observed['body_sources'] == 1
    assert observed['body_attempts'] == 2
    assert saved['steps'][0]['retrieval_events'][1]['state'] == 'completed'


def test_resume_freezes_turn_report_baseline_and_pending(workspace):
    chat = workspace
    store.update_run(chat['run_id'], lambda r: r.update(report_revision=2))
    stopped = store.cancel_conversation(chat['id'])
    resumed = store.begin_turn(chat['id'], '继续未完成部分', 'stub', 'documents')
    ctx = resumed['turn_contexts'][resumed['turn_id']]
    assert ctx['report_revision_before'] == 1
    assert ctx['turn_report_revision_before'] == 2
    assert ctx['resume_pending'] == stopped['messages'][-1]['delivery']['pending']


def test_old_failed_deep_statistics_are_unknown_without_rewriting_history(workspace):
    tid = workspace['turn_id']
    def legacy(c):
        c['state'] = 'idle'
        c['steps'] = [{'id': 'old', 'turn_id': tid, 'tool': 'build_investment_memo', 'state': 'failed',
                       'progress': [{'label': '联网查找并读取来源正文'}], 'result': {'error': '网络失败'}}]
        c['messages'].append({'role': 'assistant', 'turn_id': tid, 'content': '旧答复：本轮搜索 0 次。',
                              'coverage': {'search_calls': 0, 'status': 'partial'}})
    before = store.update_conversation(workspace['id'], legacy)
    original = copy.deepcopy(before)
    view = conversation_view(before, store.get_run(workspace['run_id']))
    observed = view['coverage_views'][tid]
    assert observed['telemetry_incomplete']
    assert observed['status'] == 'unknown'
    assert '无法还原' in coverage_notice(observed)
    assert before == original
    api_view = TestClient(main.app).get('/api/conversations/' + workspace['id']).json()
    assert api_view['coverage_views'][tid]['telemetry_incomplete']
    assert api_view['messages'] == original['messages']
    assert store.get_conversation(workspace['id']) == original
