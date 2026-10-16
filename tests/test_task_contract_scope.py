"""Human refocusing changes obligations, without erasing the original task."""
from copy import deepcopy

import pytest

from app import agent, coverage_view, resume_plan, store, task_contract, turn_constraints


@pytest.fixture
def chat(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    run = store.create_run({'ticker': '300750.SZ', 'year': 2025, 'as_of': '2026-10-16',
                            'mode': 'sample', 'question': '收入与经营现金流能否持续？'})
    run['research_questions'][0].update(status='partial', next_action='查阅供应商账期变化')
    store.save_run(run)
    return store.new_conversation(run['id'])


def begin(chat, text, previous=None):
    return store.begin_turn(chat['id'], text, 'stub', 'documents', resume_from_turn_id=previous)


def unfinished(chat):
    current = begin(chat, '联网搜索并阅读关键原文，开展深度研究，生成完整研究备忘录。')
    agent.finish_error(chat['id'], current['turn_id'], '仍有资料缺口，报告未保存', 'failed')
    return current


@pytest.mark.parametrize('text', [
    '继续未完成部分，但本轮只解释已有结论，不要使用工具，不修改报告。',
    '继续，本轮不使用任何工具。',
    '这轮仅讨论现有证据的含义，不保存任何内容。',
])
def test_refocus_discussion_has_no_old_research_obligations(chat, text):
    old = unfinished(chat)
    current = begin(chat, text, old['turn_id'])
    contract = task_contract.current_contract(current)
    assert contract['scope'] == 'refocus'
    assert contract['intent'] == 'discussion'
    assert contract['original_request'] == old['messages'][-1]['content']
    assert contract['active_request'] == text
    assert contract['obligation_turn_ids'] == [current['turn_id']]
    assert contract['task_turn_ids'] == [old['turn_id'], current['turn_id']]
    assert not contract['completion']['required_artifacts']
    assert not contract['completion']['require_body']
    plan = resume_plan.plan(current, store.get_run(current['run_id']))
    assert plan['pending_obligations'] == []
    assert plan['open_question_count'] > 0
    assert not plan['track_open_questions']
    assert resume_plan.recovery(current, store.get_run(current['run_id']), plan) is None
    assert agent.delivery_state(current, store.get_run(current['run_id']))['status'] == 'completed'


def test_plain_continue_keeps_original_obligations(chat):
    old = unfinished(chat)
    current = begin(chat, '继续', old['turn_id'])
    contract = task_contract.current_contract(current)
    assert contract['scope'] == 'continue'
    assert contract['active_request'] == contract['original_request']
    assert contract['completion']['require_search']
    assert contract['completion']['require_body']
    assert contract['completion']['track_open_questions']
    assert contract['completion']['required_artifacts'] == ['report']
    assert len(contract['obligation_turn_ids']) == 2
    assert {'search', 'body', 'question_progress'} <= set(resume_plan.plan(current, store.get_run(current['run_id']))['pending_obligations'])


def test_continue_after_refocus_does_not_resurrect_original_deep_task(chat):
    old = unfinished(chat)
    focused = begin(chat, '本轮只解释结论，不用工具。', old['turn_id'])
    agent.finish_error(chat['id'], focused['turn_id'], '只差解释', 'failed')
    current = begin(chat, '继续', focused['turn_id'])
    contract = task_contract.current_contract(current)
    assert contract['active_request'] == focused['messages'][-1]['content']
    assert contract['original_request'] == old['messages'][-1]['content']
    assert len(contract['task_turn_ids']) == 3
    assert contract['obligation_turn_ids'] == [focused['turn_id'], current['turn_id']]
    assert not contract['completion']['track_open_questions']
    assert agent.delivery_state(current, store.get_run(current['run_id']))['status'] == 'completed'


def test_historical_coverage_uses_its_own_contract_without_mutating_history(chat):
    old = unfinished(chat)
    focused = begin(chat, '本轮只解释现有结论，不用工具。', old['turn_id'])
    before = deepcopy(focused)
    original = coverage_view.turn_observation(focused, turn_id=old['turn_id'])
    latest = coverage_view.turn_observation(focused)
    assert original['require_body'] and original['status'] == 'partial'
    assert not latest['require_body'] and latest['status'] == 'observed'
    assert latest['task_turn_count'] == 1 and latest['task_history_turn_count'] == 2
    assert focused == before


def test_legacy_history_does_not_take_later_user_or_persistent_constraints():
    chat = {'turn_id': 'new', 'messages': [
        {'role': 'user', 'turn_id': 'old', 'content': '阅读关键原文。'},
        {'role': 'user', 'turn_id': 'new', 'content': '以后不用任何工具。'}],
        'turn_contexts': {'old': {'user_text': '阅读关键原文。', 'original_request': '阅读关键原文。',
                                  'rules': turn_constraints.constraints('阅读关键原文。'), 'task_turn_ids': ['old']}},
        'persistent_constraints': {'no_tools': True}}
    assert coverage_view.turn_observation(chat, turn_id='old')['require_body']
    assert not coverage_view.turn_observation(chat)['require_body']


@pytest.mark.parametrize('text,expected', [
    ('不要开展深度研究，只修订报告。', False),
    ('仅修改完整研究备忘录中的一处数字。', False),
    ('继续开展深度研究，补齐问题后修订报告。', True),
    ('全面研究公司经营状况。', True),
])
def test_research_intent_does_not_swallow_negation(text, expected):
    assert task_contract.research_requested(text) is expected


@pytest.mark.parametrize('text,blocked,allowed', [
    ('不补读PDF，只修订报告。', ['read_document', 'search_documents', 'read_public_pdf', 'build_investment_memo'], ['read_webpage', 'refine_investment_memo']),
    ('不要读取网页。', ['read_webpage', 'read_web_page', 'build_investment_memo'], ['read_document', 'read_public_pdf']),
    ('不要补读网页或PDF。', ['read_document', 'read_public_pdf', 'read_webpage', 'build_investment_memo'], ['refine_investment_memo']),
    ('只读，不修改任何内容。', ['attach_document', 'fetch_annual_report', 'start_research'], ['read_document', 'read_webpage']),
])
def test_material_permissions_apply_at_tool_boundary(text, blocked, allowed):
    rules = turn_constraints.constraints(text)
    for tool in blocked:
        assert turn_constraints.prohibited_reason(rules, tool, {}), tool
    for tool in allowed:
        assert turn_constraints.prohibited_reason(rules, tool, {}) is None, tool


def test_page_and_document_bans_have_precise_scope():
    rules = turn_constraints.constraints('不要读取文档abcdef0123456789第7页。')
    deny = turn_constraints.prohibited_reason
    assert not rules['no_pdf_read']
    assert deny(rules, 'read_document', {'document_id': 'abcdef0123456789', 'page': 7})
    assert deny(rules, 'read_document', {'document_id': 'abcdef0123456789', 'page': 6, 'count': 2})
    assert deny(rules, 'read_document', {'document_id': 'abcdef0123456789', 'page': 8}) is None
    assert deny(rules, 'read_document', {'document_id': 'different', 'page': 7}) is None
    assert deny(rules, 'search_documents', {'queries': ['营业收入']})
    assert deny(rules, 'search_documents', {'queries': ['营业收入'], 'document_id': 'different'}) is None
    assert deny(rules, 'build_investment_memo', {'focus': '经营现金流'})


def test_url_ban_keeps_other_web_pages_available():
    rules = turn_constraints.constraints('不要读取网页https://example.com/blocked。')
    # URL punctuation is a clause terminator, not part of the material identity.
    assert turn_constraints.prohibited_reason(rules, 'read_webpage', {'url': 'https://example.com/blocked'})
    assert turn_constraints.prohibited_reason(rules, 'read_webpage', {'url': 'https://example.com/other'}) is None
    assert turn_constraints.prohibited_reason(rules, 'read_document', {'document_id': 'doc', 'page': 7}) is None


def test_continuation_keeps_scoped_bans_of_both_material_kinds():
    first = turn_constraints.resolve_turn_policy({}, '不要读取PDF第7页。')['rules']
    second = turn_constraints.resolve_turn_policy({}, '不要读取网页https://example.com/blocked。', first)['rules']
    assert {x['kind'] for x in second['read_restrictions']} == {'pdf', 'web'}
    released = turn_constraints.resolve_turn_policy({}, '请阅读PDF原文。', second)['rules']
    assert [x['kind'] for x in released['read_restrictions']] == ['web']


def test_permission_for_another_page_does_not_lift_existing_page_ban():
    first = turn_constraints.resolve_turn_policy({}, '不要读取PDF第7页。')['rules']
    continued = turn_constraints.resolve_turn_policy({}, '继续，请阅读PDF第8页。', first)['rules']
    assert turn_constraints.prohibited_reason(continued, 'read_document', {'page': 7})
    assert turn_constraints.prohibited_reason(continued, 'read_document', {'page': 8}) is None


def test_recovery_skips_forbidden_page_and_uses_allowed_candidate(chat):
    current = begin(chat, '阅读关键原文，但不要读取PDF第7页。')
    run = store.get_run(current['run_id'])
    run['research_sources'] = [
        {'id': 'blocked', 'url': '/api/documents/doc/file#page=7', 'text': '禁止页'},
        {'id': 'allowed', 'url': '/api/documents/doc/file#page=8', 'text': '允许页'}]
    plan = resume_plan.plan(current, run)
    action = resume_plan.recovery(current, run, plan)
    assert action['tool'] == 'read_document' and action['arguments']['page'] == 8


def test_explicit_tool_permission_can_follow_discussion_refocus(chat):
    old = unfinished(chat)
    focused = begin(chat, '本轮只解释结论，不用工具。', old['turn_id'])
    agent.finish_error(chat['id'], focused['turn_id'], '已解释', 'failed')
    renewed = begin(chat, '新的任务：请使用工具阅读PDF第8页，核对原文。', focused['turn_id'])
    contract = task_contract.current_contract(renewed)
    assert contract['scope'] == 'refocus'
    assert not contract['rules']['no_tools']
    assert contract['completion']['require_body']
    assert turn_constraints.prohibited_reason(contract['rules'], 'read_document', {'page': 8}) is None


def test_discussion_with_explicit_retrieval_keeps_only_new_retrieval_obligations(chat):
    old = unfinished(chat)
    current = begin(chat, '本轮只解释估值方法，请联网搜索可比公司的披露并阅读原文，不保存报告。', old['turn_id'])
    contract = task_contract.current_contract(current)
    assert contract['scope'] == 'refocus' and contract['intent'] == 'discussion'
    assert contract['completion']['require_search'] and contract['completion']['require_body']
    assert not contract['completion']['required_artifacts']
    assert not contract['completion']['track_open_questions']
    assert set(resume_plan.plan(current, store.get_run(current['run_id']))['pending_obligations']) == {'search', 'body'}


def test_unrelated_report_ban_does_not_erase_requested_download():
    assert task_contract.requested_artifacts('请下载年报，不要生成报告。') == {'document'}
    assert task_contract.requested_artifacts('解释如何下载年报，不要实际操作。') == set()


def test_legacy_mirrors_cannot_override_versioned_contract(chat):
    current = begin(chat, '不使用工具，只解释已有结论。')
    ctx = current['turn_contexts'][current['turn_id']]
    ctx['rules'] = turn_constraints.constraints('允许使用工具。')
    ctx['required_artifacts'] = ['report']
    contract = task_contract.current_contract(current)
    assert contract['rules']['no_tools']
    assert contract['completion']['required_artifacts'] == []
    with pytest.raises(ValueError, match='不使用工具'):
        turn_constraints.enforce(current, 'attach_document', {'document_id': 'doc'})
