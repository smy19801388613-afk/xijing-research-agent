"""Execution settings must be explicit, validated and immutable for a running turn."""
import copy
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import budget_timeout, competition, connections, deep_research, execution_budget, main, store
from app.schemas import PreferencesRequest, TurnRequest


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    for module in (store, main, connections):
        monkeypatch.setattr(module, 'DATA', tmp_path)
    monkeypatch.setattr(competition, 'POLICY', {'enabled': False, 'model_origins': [], 'retrieval_origins': []})
    monkeypatch.setattr(connections, 'legacy_settings', lambda: {'token': '', 'key': '', 'base': 'https://api.deepseek.com', 'model': 'test-model'})
    connections.save({'active_model': 'local', 'data_source': 'documents', 'tushare_legacy': False,
                      'profiles': [{'id': 'local', 'name': 'Local test', 'provider': 'ollama',
                                    'model': 'stub', 'base': 'http://127.0.0.1:11434/v1',
                                    'protocol': 'openai', 'json_mode': True}]})
    monkeypatch.setattr(main.runtime, 'launch', lambda *args, **kwargs: None)
    return tmp_path


def budget(**changes):
    return dict(execution_budget.DEFAULTS, **changes)


@pytest.mark.parametrize('invalid', [
    {}, [], {'total_tokens': 1000000}, {'generation_tokens': 49152, 'time_minutes': 15},
    dict(execution_budget.DEFAULTS, extra=1),
    budget(total_tokens=True), budget(time_minutes=1.0), budget(max_decisions='8'),
    budget(total_tokens=49999), budget(total_tokens=5000001),
    budget(time_minutes=0), budget(time_minutes=61), budget(generation_tokens=49152),
])
def test_budget_rejects_partial_coerced_and_out_of_range_values(invalid):
    with pytest.raises(ValueError):
        execution_budget.normalize(invalid)
    for request_type, args in ((PreferencesRequest, {}), (TurnRequest, {'message': '研究问题'})):
        with pytest.raises(ValidationError):
            request_type(execution_budget=invalid, **args)


def test_budget_defaults_and_range_values_are_fresh():
    value = execution_budget.normalize()
    value['total_tokens'] = 50000
    assert execution_budget.DEFAULTS['total_tokens'] == 1000000
    # Slider steps are presentation guidance, not a reason to reject a precise API value.
    assert execution_budget.normalize(budget(total_tokens=50001))['total_tokens'] == 50001
    assert execution_budget.normalize(budget(total_tokens=5000000, time_minutes=60))
    assert PreferencesRequest().execution_budget is None
    assert TurnRequest(message='问题', execution_budget=None).execution_budget is None


def test_budget_scope_restores_outer_context_and_deadline(monkeypatch):
    monkeypatch.setattr(execution_budget.time, 'monotonic', lambda: 100.0)
    assert execution_budget.current() is None and execution_budget.deadline() is None
    outer = budget(time_minutes=2)
    with execution_budget.scope(outer):
        assert execution_budget.deadline() == 220.0
        execution_budget.current()['time_minutes'] = 60
        assert execution_budget.current() == outer
        with pytest.raises(RuntimeError):
            with execution_budget.scope(budget(time_minutes=1), deadline_at=150.0):
                assert execution_budget.deadline() == 150.0
                raise RuntimeError('exit inner scope')
        assert execution_budget.current() == outer and execution_budget.deadline() == 220.0
    assert execution_budget.current() is None and execution_budget.deadline() is None


def test_old_settings_get_defaults_and_saving_is_separate_from_turns(workspace):
    assert connections.load()['execution_budget'] == execution_budget.DEFAULTS
    settings = connections.public_settings()
    assert settings['execution_budget_defaults'] == execution_budget.DEFAULTS
    assert settings['execution_budget_bounds'] == execution_budget.BOUNDS
    selected = budget(total_tokens=1500000, time_minutes=30)
    response = TestClient(main.app).post('/api/connections/preferences', json={'execution_budget': selected})
    assert response.status_code == 200
    assert response.json()['execution_budget'] == selected
    assert json.loads((workspace / 'connections.json').read_text(encoding='utf-8'))['execution_budget'] == selected
    # Other preference changes keep the current default.
    assert connections.save_preferences({'data_source': 'documents'})['execution_budget'] == selected


def test_begin_turn_snapshots_budget_and_utc_deadline_without_mutating_old_turn(workspace):
    chat = store.new_conversation()
    selected = budget(time_minutes=2, total_tokens=150000)
    before = datetime.now(timezone.utc)
    first = store.begin_turn(chat['id'], '研究问题', 'local', 'documents', execution_budget=selected)
    context = copy.deepcopy(first['turn_contexts'][first['turn_id']])
    started = datetime.fromisoformat(context['started_at'])
    deadline = datetime.fromisoformat(context['deadline_at'])
    assert before <= started <= datetime.now(timezone.utc)
    assert started.utcoffset() == timedelta(0)
    assert deadline - started == timedelta(minutes=2)
    assert first['execution_deadline_at'] == context['deadline_at']
    assert context['execution_budget'] == selected == first['execution_budget']
    assert first['budget_usage']['decisions_used'] == first['budget_usage']['used_tokens'] == 0
    assert first['budget_usage']['total_tokens'] == 150000
    assert 'max_decisions' not in first['budget_usage']
    selected['time_minutes'] = 60
    assert store.get_conversation(chat['id'])['execution_budget']['time_minutes'] == 2
    store.cancel_conversation(chat['id'], first['turn_id'])
    store.update_conversation(chat['id'], lambda value: value.update(stop_reason='time_limit', budget_exhausted={'reason': 'time_limit'},
                                                                    budget_usage={'decisions_used': 3, 'max_decisions': 3}))
    second = store.begin_turn(chat['id'], '继续未完成部分', 'local', 'documents', resume_from_turn_id=first['turn_id'],
                              execution_budget=budget(time_minutes=4))
    assert second['turn_contexts'][first['turn_id']] == context
    assert second['execution_budget']['time_minutes'] == 4
    assert second['turn_contexts'][second['turn_id']]['execution_budget']['time_minutes'] == 4
    assert second['budget_usage']['decisions_used'] == second['budget_usage']['used_tokens'] == 0
    assert second['budget_usage']['total_tokens'] == execution_budget.DEFAULTS['total_tokens']
    assert 'stop_reason' not in second and 'budget_exhausted' not in second


def test_turn_api_uses_current_default_or_explicit_override_and_freezes_it(workspace):
    client = TestClient(main.app)
    chat = store.new_conversation()
    first_default = budget(time_minutes=9, total_tokens=600000)
    connections.save_preferences({'execution_budget': first_default})
    first = client.post(f"/api/conversations/{chat['id']}/messages", json={'message': '研究经营情况'})
    assert first.status_code == 202
    assert first.json()['execution_budget'] == first_default
    connections.save_preferences({'execution_budget': budget(time_minutes=12)})
    assert store.get_conversation(chat['id'])['execution_budget'] == first_default
    store.cancel_conversation(chat['id'], first.json()['turn_id'])
    override = budget(total_tokens=100000, time_minutes=3)
    resumed = client.post(f"/api/conversations/{chat['id']}/messages", json={
        'message': '继续未完成部分', 'resume_from_turn_id': first.json()['turn_id'], 'execution_budget': override})
    assert resumed.status_code == 202
    assert resumed.json()['execution_budget'] == override
    assert connections.load()['execution_budget'] == budget(time_minutes=12)
    store.cancel_conversation(chat['id'], resumed.json()['turn_id'])
    third = client.post(f"/api/conversations/{chat['id']}/messages", json={'message': '讨论一下业务'})
    assert third.status_code == 202
    assert third.json()['execution_budget'] == budget(time_minutes=12)


def test_invalid_settings_and_turn_requests_do_not_mutate_state(workspace):
    client = TestClient(main.app)
    original = (workspace / 'connections.json').read_text(encoding='utf-8')
    chat = store.new_conversation()
    response = client.post('/api/connections/preferences', json={'execution_budget': budget(max_decisions=True)})
    assert response.status_code == 422
    assert (workspace / 'connections.json').read_text(encoding='utf-8') == original
    response = client.post(f"/api/conversations/{chat['id']}/messages", json={
        'message': '研究一下', 'execution_budget': budget(time_minutes=1.5)})
    assert response.status_code == 422
    assert store.get_conversation(chat['id'])['state'] == 'idle'


def test_legacy_conversation_without_budget_starts_with_default(workspace):
    chat = store.new_conversation()
    assert 'execution_budget' not in chat
    started = store.begin_turn(chat['id'], '新一轮', 'local', 'documents')
    assert started['execution_budget'] == execution_budget.DEFAULTS


def test_legacy_generation_and_decision_settings_migrate_without_reinterpreting_tokens(workspace):
    old={'generation_tokens':65536,'time_minutes':30,'max_decisions':32}
    config=connections.load()
    config['execution_budget']=old
    connections.save(config)
    normalized=connections.public_settings()['execution_budget']
    assert normalized=={'total_tokens':1000000,'time_minutes':30}
    assert json.loads((workspace/'connections.json').read_text(encoding='utf8'))['execution_budget']==old
    chat=store.new_conversation()
    response=TestClient(main.app).post(f"/api/conversations/{chat['id']}/messages",
        json={'message':'研究材料','execution_budget':old})
    assert response.status_code==202
    assert response.json()['execution_budget']==normalized


@pytest.mark.parametrize('broken',[{'generation_tokens':True,'time_minutes':15,'max_decisions':8},
    {'generation_tokens':65536,'time_minutes':15,'max_decisions':33}])
def test_legacy_invalid_limits_are_not_silently_accepted(broken):
    with pytest.raises(ValueError): execution_budget.normalize(broken)


def test_token_ledger_is_frozen_per_turn_and_cancel_finalizes_only_outstanding_estimates(workspace):
    chat=store.new_conversation()
    first=store.begin_turn(chat['id'],'读取资料','local','documents')
    tid=first['turn_id']
    usage=execution_budget.initial_usage(first['execution_budget'])
    usage.update(reported_tokens=100,used_tokens=100,reserved_tokens=900,request_count=1)
    saved=store.record_budget_usage(chat['id'],tid,usage)
    assert saved['turn_contexts'][tid]['budget_usage']['reported_tokens']==100
    stopped=store.cancel_conversation(chat['id'],tid)
    assert stopped['budget_usage']['reserved_tokens']==0
    assert stopped['budget_usage']['reported_tokens']==100
    assert stopped['budget_usage']['estimated_tokens']==900
    assert stopped['budget_usage']['used_tokens']==1000
    next_turn=store.begin_turn(chat['id'],'继续未完成部分','local','documents')
    assert next_turn['budget_usage']['used_tokens']==0
    assert next_turn['turn_contexts'][tid]['budget_usage']==stopped['budget_usage']
    with pytest.raises(store.ExecutionStopped): store.record_budget_usage(chat['id'],tid,usage)
    assert store.get_conversation(chat['id'])['budget_usage']['used_tokens']==0


def owned_running_research():
    chat = store.new_conversation()
    chat = store.begin_turn(chat['id'], '研究产品并保存报告', 'local', 'documents')
    with store.execution_scope('conversation', chat['id'], chat['turn_id']):
        run = store.create_run({'ticker': '601899.SH', 'year': 2025, 'as_of': '2026-10-14',
                                'mode': 'live', 'question': '研究产品'})
        store.update_run(run['id'], lambda value: value.update(state='running', memo={'title': '原报告'},
            research_sources=[{'id': 'web-product', 'kind': 'web', 'title': '产品资料',
                'url': 'https://example.com/product', 'text': '产品名称及客户使用场景',
                'published_at': '2026-09-01', 'read_status': 'body_read'}]))
        def bind(value):
            value['run_id'] = run['id']
            value['turn_contexts'][chat['turn_id']]['run_id'] = run['id']
        chat = store.update_conversation(chat['id'], bind)
    return chat, run['id']


@pytest.mark.parametrize('stop_kind', ['cancelled', 'time_limit'])
@pytest.mark.parametrize('legacy_context', [False, True])
def test_owned_stopped_research_can_resume_from_materials_without_reviving_old_worker(workspace, monkeypatch, stop_kind, legacy_context):
    chat, run_id = owned_running_research()
    if legacy_context:
        store.update_conversation(chat['id'], lambda value: value['turn_contexts'][chat['turn_id']].pop('task_turn_ids'))
    if stop_kind == 'cancelled':
        store.cancel_conversation(chat['id'], chat['turn_id'])
    else:
        past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        store.update_conversation(chat['id'], lambda value: value['turn_contexts'][chat['turn_id']].update(deadline_at=past))
        budget_timeout.expire_conversation(chat['id'], chat['turn_id'])
    before = copy.deepcopy(store.get_run(run_id))
    assert store.cancelled(run_id)
    continued = store.begin_turn(chat['id'], '继续未完成部分', 'local', 'documents', resume_from_turn_id=chat['turn_id'])
    assert not store.cancelled(run_id)
    assert store.get_run(run_id) == before
    # The cancelled worker cannot overwrite data even though this new turn is
    # allowed to use the same run's material and preserve its failure history.
    with pytest.raises(store.ExecutionStopped):
        with store.execution_scope('conversation', chat['id'], chat['turn_id']):
            store.update_run(run_id, lambda value: value.update(memo={'title': '迟到的旧草稿'}))
    with pytest.raises(store.ExecutionStopped):
        with store.execution_scope('run', run_id):
            store.update_run(run_id, lambda value: value.update(state='running'))
    draft = {'title': '继续研究的报告', 'summary': '产品场景已取得原文，仍需核对回款', 'stance': '继续核实',
        'theses': [{'title': title, 'body': '产品资料支持经营场景，独立盈利尚未核实',
                    'evidence_ids': ['web-product'], 'counter_evidence': '场景不证明收入',
                    'invalidate_if': '取得新的原文反证', 'status': 'inference'} for title in ('产品场景', '收入缺口')],
        'sections': [{'key': key, 'body': '原始资料与推断分开，盈利和回款仍需进一步核实。',
                      'evidence_ids': ['web-product'], 'status': 'inference'} for key in deep_research.SECTION_TITLES],
        'questions': ['核对产品收入'], 'limitations': ['收入贡献尚未独立核实']}
    monkeypatch.setattr(deep_research, 'model_json', lambda *args, **kwargs: (copy.deepcopy(draft), {'model': 'fixture'}))
    with store.execution_scope('conversation', chat['id'], continued['turn_id']):
        result = deep_research.refine(run_id, '继续研究产品', continued)
    assert result['artifact'] == 'report'
    saved = store.get_run(run_id)
    assert saved['memo']['title'] == draft['title']
    assert saved['state'] == before['state']
    assert saved.get('stop_reason') == before.get('stop_reason')
    assert saved['facts'] == before['facts']


@pytest.mark.parametrize('owner_kind', ['other_conversation', 'other_task', 'unowned'])
def test_resume_does_not_unlock_a_run_owned_by_another_execution(workspace, owner_kind):
    chat, run_id = owned_running_research()
    owner = {'conversation_id': chat['id'], 'turn_id': chat['turn_id']}
    if owner_kind == 'other_conversation': owner['conversation_id'] = 'someone-else'
    elif owner_kind == 'other_task': owner['turn_id'] = 'unrelated-turn'
    else: owner = None
    store.update_run(run_id, lambda value: value.update(execution_owner=owner))
    store.cancel_run(run_id)
    store.cancel_conversation(chat['id'], chat['turn_id'])
    before = copy.deepcopy(store.get_run(run_id))
    store.begin_turn(chat['id'], '继续未完成部分', 'local', 'documents', resume_from_turn_id=chat['turn_id'])
    assert store.cancelled(run_id)
    assert store.get_run(run_id) == before
