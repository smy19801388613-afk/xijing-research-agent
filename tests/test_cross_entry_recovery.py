"""A new conversation can reuse stopped research without reviving its worker."""
from datetime import datetime, timedelta, timezone
import copy
import time

import pytest

from app import agent, budget_timeout, competition, deep_research, execution_budget, store
from app.providers import ProviderError
from app.schemas import RunRequest
from recommendation_fixture import watch_recommendation


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    monkeypatch.setattr(competition, 'audit', lambda *a, **kw: None)


def stopped_run(reason):
    run = store.create_run(RunRequest(execution_budget={'total_tokens': 50000, 'time_minutes': 1}).model_dump(mode='json'))
    def prepare(value):
        value['budget_usage']['reported_tokens'] = 900
        value['budget_usage']['used_tokens'] = 900
        value['execution_deadline_at'] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        value['research_sources'] = [{'id': 'saved-source', 'kind': 'web', 'title': '已读经营披露',
                                     'url': 'https://example.com/disclosure', 'text': '已取得的经营资料。',
                                     'published_at': '2026-03-01', 'read_status': 'body_read'}]
    store.update_run(run['id'], prepare)
    if reason == 'cancelled':
        store.cancel_run(run['id'])
    else:
        budget_timeout.expire_run(run['id'])
    return store.get_run(run['id'])


@pytest.mark.parametrize('reason', ['cancelled', 'time_limit'])
@pytest.mark.parametrize('operation', ['build', 'refine'])
def test_new_chat_reaches_real_deep_research_with_new_budget_and_preserves_old_execution(workspace, monkeypatch, reason, operation):
    old = stopped_run(reason)
    budget = {'total_tokens': 500000, 'time_minutes': 15}
    chat = store.begin_turn(store.new_conversation(old['id'])['id'], '基于已有材料继续深研并保存报告',
                            'isolated', 'documents', execution_budget=budget)
    calls = []
    def model(*args, **kwargs):
        calls.append((execution_budget.current(), execution_budget.deadline() - time.monotonic()))
        reserved = execution_budget.reserve(400, 100)
        execution_budget.settle(reserved, {'total_tokens': 300})
        raise ProviderError('isolated planning reached; no network or paid call')
    monkeypatch.setattr(deep_research, 'model_json', model)

    @agent.with_turn_budget
    def execute(cid, tid):
        return getattr(deep_research, operation)(old['id'], '继续深研', store.get_conversation(cid))

    with store.execution_scope('conversation', chat['id'], chat['turn_id']):
        with pytest.raises(ProviderError, match='isolated planning reached'):
            execute(chat['id'], chat['turn_id'])
    assert len(calls) == 1 and calls[0][0] == budget and 850 < calls[0][1] <= 900
    saved = store.get_run(old['id'])
    for field in ('state', 'stop_reason', 'execution_budget', 'execution_deadline_at', 'budget_usage', 'events'):
        assert saved.get(field) == old.get(field)
    assert store.cancelled(old['id'])
    assert store.get_conversation(chat['id'])['budget_usage']['reported_tokens'] == 300
    assert saved['deep_research_attempts'][-1]['failure']['message'].startswith('isolated planning reached')
    with pytest.raises(store.ExecutionStopped):
        with store.execution_scope('run', old['id']):
            store.update_run(old['id'], lambda r: r.update(memo={'title': 'obsolete'}))


def test_only_current_bound_conversation_can_reuse_old_cancelled_research(workspace):
    old = stopped_run('cancelled')
    assert store.research_cancelled(old['id'])  # No verified execution context.
    unbound = store.begin_turn(store.new_conversation()['id'], '讨论研究', 'isolated', 'documents')
    with store.execution_scope('conversation', unbound['id'], unbound['turn_id']):
        assert store.research_cancelled(old['id'])
    bound = store.begin_turn(store.new_conversation(old['id'])['id'], '继续深研', 'isolated', 'documents')
    with store.execution_scope('conversation', bound['id'], bound['turn_id']):
        assert not store.research_cancelled(old['id'])
        store.cancel_conversation(bound['id'], bound['turn_id'])
        with pytest.raises(store.ExecutionStopped):
            store.research_cancelled(old['id'])
        with pytest.raises(store.ExecutionStopped):
            store.update_run(old['id'], lambda r: r.update(memo={'title': 'late result'}))


def test_current_owner_stop_remains_effective_during_cancel_handoff(workspace):
    chat = store.begin_turn(store.new_conversation()['id'], '建立研究', 'isolated', 'documents')
    with store.execution_scope('conversation', chat['id'], chat['turn_id']):
        owned = store.create_run(RunRequest().model_dump(mode='json'))
        store.update_conversation(chat['id'], lambda c: c.update(run_id=owned['id']))
        store.cancel_run(owned['id'])  # API has not yet revoked the owning chat.
        assert store.research_cancelled(owned['id'])
    store.cancel_conversation(chat['id'], chat['turn_id'])
    new = store.begin_turn(store.new_conversation(owned['id'])['id'], '重新查阅并深研', 'isolated', 'documents')
    with store.execution_scope('conversation', new['id'], new['turn_id']):
        assert not store.research_cancelled(owned['id'])
        store.update_run(owned['id'], lambda r: r.update(recovery_note='new conversation write'))
    with pytest.raises(store.ExecutionStopped):
        with store.execution_scope('conversation', chat['id'], chat['turn_id']):
            store.update_run(owned['id'], lambda r: r.update(recovery_note='obsolete conversation'))
    assert store.get_run(owned['id'])['recovery_note'] == 'new conversation write'
    assert store.cancelled(owned['id'])


@pytest.mark.parametrize('reason', ['cancelled', 'time_limit'])
def test_new_chat_can_save_report_on_stopped_standalone_research(workspace, monkeypatch, reason):
    old = stopped_run(reason)
    chat = store.begin_turn(store.new_conversation(old['id'])['id'], '根据已读材料生成并保存完整报告',
                            'isolated', 'documents')
    draft = {'title': '新会话继续研究的报告', 'summary': '经营原文已取得，回款仍需核实。', 'stance': '继续核实',
             'theses': [{'title': title, 'body': '经营资料支持业务场景，独立盈利尚未核实。',
                         'evidence_ids': ['saved-source'], 'counter_evidence': '场景不证明收入',
                         'invalidate_if': '取得新的原文反证', 'status': 'inference'} for title in ('经营场景', '现金缺口')],
             'sections': [{'key': key, 'body': '原始资料与推断分开，回款与盈利仍需核实。',
                           'evidence_ids': ['saved-source'], 'status': 'inference'} for key in deep_research.SECTION_TITLES],
             'questions': ['核对现金回款'], 'limitations': ['收入贡献尚未独立核实'],
             'investment_recommendation': watch_recommendation()}
    monkeypatch.setattr(deep_research, 'model_json', lambda *a, **kw: (copy.deepcopy(draft), {'model': 'isolated'}))
    with store.execution_scope('conversation', chat['id'], chat['turn_id']):
        result = deep_research.refine(old['id'], '根据已读材料生成完整报告', chat)
    saved = store.get_run(old['id'])
    assert result['artifact'] == 'report' and saved['report_revision'] == old['report_revision'] + 1
    assert saved['memo']['title'] == draft['title']
    for field in ('state', 'stop_reason', 'execution_budget', 'execution_deadline_at', 'budget_usage'):
        assert saved.get(field) == old.get(field)
    assert saved['events'][:len(old['events'])] == old['events']
    assert store.cancelled(old['id'])
    with pytest.raises(store.ExecutionStopped):
        with store.execution_scope('run', old['id']):
            store.save_run(old)
