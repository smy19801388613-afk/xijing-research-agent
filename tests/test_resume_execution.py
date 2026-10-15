"""Regression for saved-report continuations that previously did no research."""
import copy

import pytest

from app import agent, store, deep_research, resume_plan, questions


@pytest.fixture
def chat(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    run = store.create_run({'ticker': '601899.SH', 'year': 2025, 'as_of': '2026-10-15',
                            'mode': 'sample', 'question': '资本开支与现金流能否匹配？'})
    run.update(state='completed_with_gaps', company='紫金矿业', research_sources=[],
               memo={'title': '已有报告', 'summary': '资本开支仍待查证', 'theses': []})
    run['research_questions'][0].update(priority='high', next_action='查阅资本开支现金流明细')
    store.save_run(run)
    return store.new_conversation(run['id'])


def begin(chat, text, **kwargs):
    return store.begin_turn(chat['id'], text, 'stub', 'documents', **kwargs)


def saved_report_task(chat, request='联网搜索并阅读关键原文，开展深度研究，生成完整研究备忘录。', steps=()):
    first = begin(chat, request)
    store.update_conversation(chat['id'], lambda c: c['turn_contexts'][first['turn_id']].update(
        required_artifacts=sorted(agent.requested_artifacts(request))))
    store.update_run(chat['run_id'], lambda r: r.update(report_revision=2))
    fingerprint = agent.report_input_fingerprint(store.get_run(chat['run_id']))
    report_step = {'id': 'saved-report', 'turn_id': first['turn_id'], 'tool': 'refine_investment_memo',
                   'state': 'completed', 'arguments': {}, 'result': {'artifact': 'report', 'revision': 2},
                   'report_input_fingerprint': fingerprint}
    store.update_conversation(chat['id'], lambda c: c['steps'].extend([
        *[dict(s, turn_id=first['turn_id']) for s in steps], report_step]))
    agent.finish_error(chat['id'], first['turn_id'], '还有研究事项未完成', 'failed')
    return begin(chat, '继续未完成部分', resume_from_turn_id=first['turn_id'])


def test_saved_report_does_not_satisfy_unexecuted_retrieval(chat, monkeypatch):
    continued = saved_report_task(chat)
    assert agent.reusable_step(continued, 'build_investment_memo', {}) is None
    assert agent.reusable_step(continued, 'refine_investment_memo', {})['id'] == 'saved-report'
    assert agent.memo_tool_for_request('build_investment_memo', continued,
        [{'tool': 'build_investment_memo', 'result': {'error': 'temporary failure'}}]) == 'build_investment_memo'
    calls = []
    url = 'https://example.com/capital'
    monkeypatch.setattr(agent.web_research, 'search', lambda *a, **k: calls.append('search') or {
        'results': [{'url': url, 'title': '资本开支披露', 'snippet': '披露原文', 'published_at': '2026-03-01'}]})
    monkeypatch.setattr(agent.web_research, 'read', lambda *a, **k: calls.append('read') or {
        'url': url, 'title': '资本开支披露', 'text': '资本开支需要与经营现金流按相同口径核对。',
        'read_status': 'body_read', 'published_at': '2026-03-01'})

    def refine(rid, *args, **kwargs):
        calls.append('report')
        store.update_run(rid, lambda r: r.update(report_revision=3))
        return {'artifact': 'report', 'revision': 3}

    monkeypatch.setattr(deep_research, 'refine', refine)
    seen = []
    def model(system, payload, *args, **kwargs):
        seen.append(copy.deepcopy(payload))
        if payload['tool_results'] and 'validation_feedback' in payload['tool_results'][-1]:
            return {'action': 'tool', 'tool': 'refine_investment_memo', 'arguments': {}}, {'model': 'stub'}
        return {'action': 'respond', 'message': '报告已经保存，仍保留资本开支缺口。'}, {'model': 'stub'}
    monkeypatch.setattr(agent, 'model_json', model)
    agent.execute(chat['id'])
    saved = store.get_conversation(chat['id'])
    assert calls == ['search', 'read', 'report'], (saved['state'], saved['messages'][-1]['content'], saved['steps'])
    assert seen[0]['research_plan']['previous_pending']
    assert seen[0]['research_plan']['pending_questions'][0]['next_action'] == '查阅资本开支现金流明细'
    assert saved['messages'][-1]['delivery']['report_revision_before'] == 2
    assert saved['messages'][-1]['delivery']['report_revision_after'] == 3
    assert saved['messages'][-1]['coverage']['search_calls'] == 1
    assert saved['messages'][-1]['coverage']['body_sources'] == 1


def test_new_turn_can_reach_saved_source_without_new_search_steps(chat):
    source = {'id': 'source', 'url': 'https://example.com/annual', 'title': '已保存原文',
              'text': '资本开支明细见附注', 'read_status': 'body_read', 'published_at': '2026-03-01'}
    store.update_run(chat['run_id'], lambda r: r.update(research_sources=[source]))
    continued = saved_report_task(chat, '阅读关键原文，开展深度研究，生成完整研究备忘录。')
    plan = resume_plan.plan(continued, store.get_run(chat['run_id']))
    assert 'body' in plan['pending_obligations']
    assert 'question_progress' in plan['pending_obligations']
    action = resume_plan.recovery(continued, store.get_run(chat['run_id']), plan)
    assert action['tool'] == 'read_webpage' and action['arguments']['url'] == source['url']


def test_completed_task_reading_does_not_require_duplicate_reading(chat, monkeypatch):
    source = {'url': 'https://example.com/annual', 'text': '经营资料原文', 'read_status': 'body_read',
              'published_at': '2026-03-01'}
    continued = saved_report_task(chat, '阅读关键原文并生成报告。', steps=[{
        'id': 'read', 'tool': 'read_webpage', 'arguments': {'url': source['url']},
        'state': 'completed', 'result': source}])
    monkeypatch.setattr(agent, 'model_json', lambda *a, **k: ({'action': 'respond', 'message': '已整理前轮取得的原文和报告。'}, {'model': 'stub'}))
    monkeypatch.setattr(agent, 'tool', lambda *a, **k: pytest.fail('completed task repeated retrieval'))
    agent.execute(chat['id'])
    last = store.get_conversation(chat['id'])['messages'][-1]
    assert last['coverage']['body_sources'] == 0
    assert last['coverage']['task_coverage']['body_sources'] == 1
    assert last['delivery']['status'] == 'completed'
    assert not last['delivery']['report_changed']
    assert last['delivery']['task_report_revision_before'] == 1


def test_failed_retrieval_recovery_is_bounded(chat, monkeypatch):
    begin(chat, '联网搜索并阅读关键原文。')
    calls = []
    def fail(*a, **k):
        calls.append(1)
        raise agent.ProviderError('搜索服务暂不可用')
    monkeypatch.setattr(agent.web_research, 'search', fail)
    monkeypatch.setattr(agent, 'model_json', lambda *a, **k: ({'action': 'respond', 'message': '搜索服务不可用，保留缺口。'}, {'model': 'stub'}))
    agent.execute(chat['id'])
    saved = store.get_conversation(chat['id'])
    assert len(calls) == 2
    assert saved['state'] == 'idle'
    assert saved['messages'][-1]['delivery']['status'] == 'partial'
    assert len(saved['model_calls']) == 3


@pytest.mark.parametrize('restriction', ['本轮不使用任何工具。', '本轮不联网。', '本轮最多0次搜索。'])
def test_continuation_guard_honors_restrictions(chat, monkeypatch, restriction):
    continued = saved_report_task(chat, '联网搜索并阅读关键原文，开展深度研究。')
    agent.finish_error(chat['id'], continued['turn_id'], '尚未完成', 'failed')
    begin(chat, '继续未完成部分，' + restriction, resume_from_turn_id=continued['turn_id'])
    monkeypatch.setattr(agent.web_research, 'search', lambda *a, **k: pytest.fail('restricted search'))
    monkeypatch.setattr(agent.web_research, 'read', lambda *a, **k: pytest.fail('no source may be invented'))
    monkeypatch.setattr(agent, 'model_json', lambda *a, **k: ({'action': 'respond', 'message': '当前限制下未能补充原文，缺口保留。'}, {'model': 'stub'}))
    agent.execute(chat['id'])
    saved = store.get_conversation(chat['id'])
    assert not [s for s in saved['steps'] if s['turn_id'] == saved['turn_id']]
    assert saved['messages'][-1]['delivery']['can_resume']


def test_recovery_leaves_final_decision_for_answer(chat, monkeypatch):
    begin(chat, '联网搜索并阅读关键原文。', execution_budget={'generation_tokens': 49152, 'time_minutes': 15, 'max_decisions': 2})
    monkeypatch.setattr(agent.web_research, 'search', lambda *a, **k: {'results': [
        {'url': 'https://example.com/new', 'title': '线索', 'snippet': '待阅读', 'published_at': '2026-03-01'}]})
    monkeypatch.setattr(agent.web_research, 'read', lambda *a, **k: pytest.fail('last decision cannot start a read'))
    monkeypatch.setattr(agent, 'model_json', lambda *a, **k: ({'action': 'respond', 'message': '已取得线索，正文待阅读。'}, {'model': 'stub'}))
    agent.execute(chat['id'])
    saved = store.get_conversation(chat['id'])
    assert len(saved['model_calls']) == 2
    assert [s['tool'] for s in saved['steps']] == ['web_search']
    assert saved['messages'][-1]['delivery']['status'] == 'partial'


@pytest.mark.parametrize('text', ['如何使用联网搜索？', '你能联网搜索吗？', '这个系统是否支持联网搜索？',
                                 '请说明联网搜索功能。', '怎么配置联网搜索？'])
def test_search_capability_question_does_not_execute_search(chat, monkeypatch, text):
    current = begin(chat, text)
    assert 'search' not in resume_plan.plan(current, store.get_run(chat['run_id']))['pending_obligations']
    monkeypatch.setattr(agent, 'tool', lambda *a, **k: pytest.fail('capability inquiry started retrieval'))
    monkeypatch.setattr(agent, 'model_json', lambda *a, **k: ({'action': 'respond', 'message': '可以联网检索并阅读公开原文。'}, {'model': 'stub'}))
    agent.execute(chat['id'])
    assert store.get_conversation(chat['id'])['messages'][-1]['delivery']['status'] == 'completed'


def test_explicit_search_help_remains_actionable(chat):
    current = begin(chat, '请帮我联网搜索紫金矿业的资本开支。')
    assert 'search' in resume_plan.plan(current, store.get_run(chat['run_id']))['pending_obligations']


def test_question_gaps_remain_resumable_after_search_progress(chat):
    source = {'url': 'https://example.com/annual', 'text': '年报经营资料原文', 'read_status': 'body_read',
              'published_at': '2026-03-01'}
    continued = saved_report_task(chat, steps=[
        {'id': 'search', 'tool': 'web_search', 'state': 'completed', 'arguments': {'queries': ['紫金矿业年报']},
         'result': {'results': [{'url': source['url'], 'snippet': '原文线索'}]}},
        {'id': 'read', 'tool': 'read_webpage', 'state': 'completed', 'arguments': {'url': source['url']}, 'result': source}])
    before = resume_plan.plan(continued, store.get_run(chat['run_id']))
    assert before['pending_obligations'] == ['question_progress']
    store.update_conversation(chat['id'], lambda c: c['steps'].append({
        'id': 'new-search', 'turn_id': continued['turn_id'], 'tool': 'web_search', 'state': 'completed',
        'arguments': {'queries': ['紫金矿业资本开支']}, 'result': {'results': [{'url': 'https://example.com/new', 'snippet': '新线索'}]}}))
    current = store.get_conversation(chat['id'])
    after = resume_plan.plan(current, store.get_run(chat['run_id']))
    assert after['pending_obligations'] == []  # Bounded progress, not forced completion.
    delivery = agent.delivery_state(current, store.get_run(chat['run_id']))
    assert delivery['status'] == 'partial' and delivery['can_resume']
    assert any('研究问题待查证' in gap for gap in delivery['pending'])


def test_no_tools_does_not_create_search_execution_obligation(chat):
    current = begin(chat, '不要使用任何工具，讨论联网搜索的思路。')
    assert 'search' not in resume_plan.plan(current, store.get_run(chat['run_id']))['pending_obligations']


def test_final_response_is_preserved_when_report_remains_unfinished(chat, monkeypatch):
    begin(chat, '联网搜索并生成报告。', execution_budget={'generation_tokens': 49152, 'time_minutes': 15, 'max_decisions': 2})
    monkeypatch.setattr(agent.web_research, 'search', lambda *a, **k: {'results': [
        {'url': 'https://example.com/new', 'title': '线索', 'snippet': '待阅读'}]})
    monkeypatch.setattr(agent, 'model_json', lambda *a, **k: ({'action': 'respond', 'message': '已找到原文线索，尚缺资本开支明细。'}, {'model': 'stub'}))
    agent.execute(chat['id'])
    saved = store.get_conversation(chat['id'])
    last = saved['messages'][-1]
    assert saved['state'] == 'idle' and not last.get('error')
    assert '已找到原文线索，尚缺资本开支明细' in last['content']
    assert '保存研究报告' in last['delivery']['pending']
    assert last['delivery']['status'] == 'partial'


def test_last_decision_cannot_start_a_long_report_tool(chat, monkeypatch):
    begin(chat, '请生成报告。', execution_budget={'generation_tokens': 49152, 'time_minutes': 15, 'max_decisions': 2})
    decisions = iter([{'action': 'respond', 'message': '准备研究。'},
                      {'action': 'tool', 'tool': 'build_investment_memo', 'arguments': {}}])
    monkeypatch.setattr(agent, 'model_json', lambda *a, **k: (next(decisions), {'model': 'stub'}))
    monkeypatch.setattr(agent, 'tool', lambda *a, **k: pytest.fail('last decision started long work'))
    agent.execute(chat['id'])
    saved = store.get_conversation(chat['id'])
    assert saved['state'] == 'idle' and not saved['steps']
    assert '未启动新的研究步骤' in saved['messages'][-1]['content']
    assert saved['messages'][-1]['delivery']['can_resume']


def test_old_document_searches_do_not_exhaust_new_turn_allowance(chat, monkeypatch):
    old = [{'id': str(n), 'tool': 'search_documents', 'state': 'completed', 'arguments': {'queries': [q]},
            'result': {'snippets': [{'text': q + '的已有原文'}]}}
           for n, q in enumerate(['营业收入', '净利润'])]
    saved_report_task(chat, '检索已接入材料并生成报告。', old)
    seen = []
    def tool(c, name, args, *rest):
        seen.append((name, args))
        return {'snippets': [{'text': '新关键词对应的资本开支原文'}]}
    monkeypatch.setattr(agent, 'tool', tool)
    decisions = iter([{'action': 'tool', 'tool': 'search_documents', 'arguments': {'queries': ['资本开支']}},
                      {'action': 'respond', 'message': '新检索结果已保留。'}])
    monkeypatch.setattr(agent, 'model_json', lambda *a, **k: (next(decisions), {'model': 'stub'}))
    agent.execute(chat['id'])
    assert seen == [('search_documents', {'queries': ['资本开支']})]
    assert store.get_conversation(chat['id'])['steps'][-1]['state'] == 'completed'


def test_nested_report_retrieval_counts_as_current_progress(chat):
    continued = saved_report_task(chat)
    source = {'url': 'https://example.com/new', 'text': '经营原文', 'read_status': 'body_read', 'published_at': '2026-03-01'}
    events = [{'id': 'nested-search', 'kind': 'search', 'state': 'completed', 'arguments': {'queries': ['资本开支']},
               'result': {'results': [{'url': source['url'], 'snippet': '原文线索'}]}},
              {'id': 'nested-read', 'kind': 'read_webpage', 'state': 'completed', 'arguments': {'url': source['url']},
               'result': source}]
    store.update_conversation(chat['id'], lambda c: c['steps'].append({
        'id': 'nested-build', 'turn_id': continued['turn_id'], 'tool': 'build_investment_memo',
        'state': 'failed', 'arguments': {}, 'result': {'error': '后续报告生成失败'}, 'retrieval_events': events}))
    current = store.get_conversation(chat['id'])
    run = store.get_run(chat['run_id'])
    plan = resume_plan.plan(current, run)
    assert plan['pending_obligations'] == []
    assert plan['task_observation']['search_calls'] == 1 and plan['task_observation']['body_sources'] == 1
    assert resume_plan.recovery(current, run, plan) is None
    assert agent.delivery_state(current, run)['can_resume']  # Open research questions remain.


def test_changed_evidence_reopens_previously_supported_question(chat):
    source = {'id': 'disclosure', 'kind': 'web', 'url': 'https://example.com/disclosure',
              'text': '本期资本开支覆盖建设项目。', 'read_status': 'body_read', 'published_at': '2026-03-01'}
    store.update_run(chat['run_id'], lambda r: r.update(research_sources=[source]))
    qid = store.get_run(chat['run_id'])['research_questions'][0]['id']
    questions.update(chat['run_id'], [{'question_id': qid, 'status': 'supported', 'answer': '资本开支覆盖建设项目。',
        'bindings': [{'source_id': source['id'], 'quote': '本期资本开支覆盖建设项目。', 'relation': 'support', 'reason': '原文披露资本开支用途'}],
        'gaps': [], 'counter_evidence': '披露用途不代表实际现金支出已完成。', 'next_action': ''}])
    store.update_run(chat['run_id'], lambda r: r['research_sources'][0].update(text='该项数据已更正，原披露不再适用。'))
    current = begin(chat, '开展深度买方研究。')
    run = store.get_run(chat['run_id'])
    assert run['research_questions'][0]['status'] == 'supported'
    plan = resume_plan.plan(current, run)
    assert plan['open_question_count'] == 1
    assert plan['pending_questions'][0]['status'] != 'supported'
    assert agent.delivery_state(current, run)['can_resume']


def test_report_edit_does_not_require_closing_all_research_questions(chat):
    current = begin(chat, '请更新并保存完整研究备忘录。')
    plan = resume_plan.plan(current, store.get_run(chat['run_id']))
    assert plan['open_question_count'] == 1
    assert not plan['track_open_questions']
