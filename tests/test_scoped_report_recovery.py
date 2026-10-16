"""A completed targeted edit must not reopen every long-lived research gap."""
import copy

import pytest

from app import agent, questions, resume_plan, store


@pytest.fixture
def research(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    run = store.create_run({'ticker': '600588.SH', 'year': 2025, 'as_of': '2026-10-16',
                            'mode': 'sample', 'question': '资本开支和订阅业务是否可持续？'})
    run.update(state='completed_with_gaps', company='用友网络', report_revision=2,
               memo={'title': '原深研', 'summary': '保留待查明细',
                     'theses': [{'evidence_ids': ['source']}]})
    run['research_questions'][0].update(status='partial', next_action='读取无形资产附注和利润表研发费用明细')
    store.save_run(run)
    return store.new_conversation(run['id'])


def begin(chat, text, **kwargs):
    return store.begin_turn(chat['id'], text, 'stub', 'documents', **kwargs)


def source_read(chat, tid, name='read_webpage'):
    source = {'id': 'source', 'url': 'https://example.com/disclosure', 'text': '本期经营与资本开支披露原文。',
              'read_status': 'body_read', 'published_at': '2026-03-01'}
    store.update_run(chat['run_id'], lambda r: r.update(research_sources=[source]))
    store.update_conversation(chat['id'], lambda c: c['steps'].append({
        'id': 'actual-read', 'turn_id': tid, 'tool': name, 'state': 'completed',
        'arguments': {'url': source['url']}, 'result': source}))


def save_report(chat, *, state='completed', reused=False):
    store.update_run(chat['run_id'], lambda r: r.update(report_revision=3))
    r = store.get_run(chat['run_id'])
    step = {'id': 'current-edit', 'turn_id': chat['turn_id'], 'tool': 'refine_investment_memo',
            'state': state, 'arguments': {}, 'result': {'artifact': 'report', 'revision': 3},
            'report_input_fingerprint': agent.report_input_fingerprint(r),
            'report_request_fingerprint': agent.report_request_fingerprint(chat)}
    if reused:
        step['reused_from_step_id'] = 'old-report'
    if state != 'completed':
        step['result'] = {'error': 'save validation failed'}
    store.update_conversation(chat['id'], lambda c: c['steps'].append(step))
    return store.get_conversation(chat['id']), r


YONYOU_EDIT = ('继续未完成的报告保存。接续上一轮已保留的最新 repaired_draft，保持已经改正的风险条件判断、'
               '用友BIP2026H1收入及已确认PS参数。沿用已读资料和两项估值，不扩大研究范围。'
               '现在直接调用 refine_investment_memo 精确接续修订稿、核验引用并保存新版本，同步已有问题答案。'
               '不要重复全量联网研究；如果没有新增资料，明确这轮主要是基于已有材料修订。')


def inherited_edit(chat, text=YONYOU_EDIT):
    first = begin(chat, '阅读关键原文，开展深度研究，生成完整研究备忘录。')
    source_read(chat, first['turn_id'])
    agent.finish_error(chat['id'], first['turn_id'], '报告尚未保存；有长期研究缺口', 'failed')
    return begin(chat, text, resume_from_turn_id=first['turn_id'])


def test_real_scoped_edit_stops_old_partial_research_after_matching_save(research):
    chat = inherited_edit(research)
    before = resume_plan.plan(chat, store.get_run(chat['run_id']))
    assert 'question_progress' not in before['pending_obligations']
    assert agent.delivery_state(chat, store.get_run(chat['run_id']))['status'] == 'partial'
    saved, run = save_report(chat)
    after = resume_plan.plan(saved, run)
    assert agent.report_step_for_input(saved, run)
    assert after['completed_report_edit']
    assert after['pending_obligations'] == []
    assert not after['track_open_questions']
    assert after['open_question_count'] == 1 and after['pending_questions']
    assert after['next_action'] is None
    assert resume_plan.recovery(saved, run, after) is None
    # Even a plan created before saving must not force a new source read.
    assert resume_plan.recovery(saved, run, before) is None
    assert agent.delivery_state(saved, run)['status'] == 'completed'


def test_scoped_completion_survives_pure_continue(research):
    edit = inherited_edit(research)
    saved, run = save_report(edit)
    agent.finish_error(research['id'], saved['turn_id'], '只差最后答复', 'failed')
    continued = begin(research, '继续', resume_from_turn_id=saved['turn_id'])
    assert agent.report_step_for_input(continued, run)
    result = resume_plan.plan(continued, run)
    assert result['completed_report_edit']
    assert result['pending_obligations'] == []


@pytest.mark.parametrize('state,reused', [('failed', False), ('completed', True)])
def test_failed_or_reused_result_cannot_satisfy_current_edit(research, state, reused):
    chat = inherited_edit(research)
    saved, run = save_report(chat, state=state, reused=reused)
    result = resume_plan.plan(saved, run)
    assert not result['completed_report_edit']
    assert 'question_progress' not in result['pending_obligations']
    assert agent.delivery_state(saved, run)['status'] == 'partial'


def test_old_request_fingerprint_cannot_satisfy_new_correction(research):
    chat = inherited_edit(research)
    saved, run = save_report(chat)
    changed = copy.deepcopy(saved)
    changed['turn_contexts'][changed['turn_id']]['user_text'] = '纠正海外占比同比方向并保存报告。'
    changed['messages'][-1]['content'] = changed['turn_contexts'][changed['turn_id']]['user_text']
    assert not agent.report_step_for_input(changed, run)
    assert not resume_plan.plan(changed, run)['completed_report_edit']


def test_changed_original_evidence_invalidates_completed_edit(research):
    chat = inherited_edit(research)
    saved, run = save_report(chat)
    changed = copy.deepcopy(run)
    changed['research_sources'][0]['text'] = '原披露已经更正，需要重新核对。'
    assert not agent.report_step_for_input(saved, changed)
    assert not resume_plan.plan(saved, changed)['completed_report_edit']


@pytest.mark.parametrize('user_request,obligations', [
    ('联网搜索并修订报告。', ['search']),
    ('阅读关键原文并修订报告。', ['body']),
    ('逐页核对2025年报营业收入、归母净利润及同比，再修订报告。', ['document_targets']),
])
def test_saved_edit_preserves_explicit_unexecuted_retrieval(research, user_request, obligations):
    chat = begin(research, user_request)
    saved, run = save_report(chat)
    result = resume_plan.plan(saved, run)
    assert result['completed_report_edit']
    for obligation in obligations:
        assert obligation in result['pending_obligations']
    assert not ('不要因此新增检索' in result['instruction'])


def test_full_research_continuation_still_requires_question_progress(research):
    chat = inherited_edit(research, '继续开展深度研究，补齐关键问题后修订并保存报告。')
    saved, run = save_report(chat)
    result = resume_plan.plan(saved, run)
    assert not result['completed_report_edit']
    assert result['track_open_questions']
    assert 'question_progress' in result['pending_obligations']


def test_forbidden_search_does_not_fall_through_to_unrequested_local_pdf(research):
    chat = begin(research, '无需新搜索或读其他页，只纠正报告中的同比方向并保存。')
    saved, run = save_report(chat)
    run['research_sources'] = [{'id': 'pdf', 'url': '/api/documents/annual/file#page=116', 'text': '待核对收入'}]
    stale = {'pending_obligations': ['search'], 'pending_questions': [{'question': '旧的长期问题'}],
             'original_request': '搜索旧问题'}
    assert resume_plan.recovery(saved, run, stale) is None


def test_agent_finishes_matching_edit_without_new_tool_round(research, monkeypatch):
    chat = inherited_edit(research)
    saved, run = save_report(chat)
    monkeypatch.setattr(agent, 'model_json', lambda *a, **k: (
        {'action': 'respond', 'message': '已按本轮要求修订并保存第3版，长期研究缺口保留。'}, {'model': 'stub'}))
    monkeypatch.setattr(agent, 'tool', lambda *a, **k: pytest.fail('targeted edit triggered another tool'))
    agent.execute(research['id'])
    final = store.get_conversation(research['id'])
    assert final['state'] == 'idle'
    assert final['messages'][-1]['delivery']['status'] == 'completed'
    assert len(final['steps']) == len(saved['steps'])
