"""Complete isolated saves: provenance versions and unreviewed observations."""
from copy import deepcopy

import pytest

from app import agent_context, deep_research as deep, questions, report_edit, store
from app import research_extensions as extensions, research_tracking as tracking
from test_investment_recommendation import run, memo, suggestion
from test_research_tracking import metric


def publish(run, candidate=None):
    draft = candidate or memo(suggestion())
    deep.save_memo(run['id'], deep.validate_memo(draft, run), '合成初稿')
    return store.get_run(run['id'])


def tracked_report(run):
    source = {'id': 'baseline-only', 'kind': 'web', 'title': '合成现金披露',
        'text': '2025年度经营活动现金流净额为100万元。主要客户按期付款。',
        'url': 'https://example.com/cash', 'published_at': '2026-04-30', 'read_status': 'body_read'}
    run = store.update_run(run['id'], lambda r: r.setdefault('research_sources', []).append(source))
    draft = memo(suggestion()); row = metric()
    row['thesis'] = draft['theses'][0]['title']
    row['baseline']['source_id'] = 'baseline-only'
    draft['tracking_metrics'] = [row]
    return publish(run, draft)


def observe(run, text='按期付款仍需结合金额与账龄核实。'):
    result = tracking.record_observation(run['id'], 'cash-recovery', {
        'report_identity': tracking.snapshot(run)['report_identity'], 'source_id': 'baseline-only',
        'quote': '主要客户按期付款', 'observation': text, 'assessment': 'supporting'})
    return result['observations'][-1]['id']


def refine(run, monkeypatch, prompt, response):
    chat = store.new_conversation(run['id'])
    chat = store.begin_turn(chat['id'], prompt, 'stub', 'documents')
    calls = []
    def model(system, body, *args, **kwargs):
        calls.append(deepcopy(body))
        return deepcopy(response), {'model': 'stub'}
    monkeypatch.setattr(deep, 'model_json', model)
    deep.refine(run['id'], prompt, chat)
    assert len(calls) == 1
    return store.get_run(run['id']), calls[0]


@pytest.mark.parametrize('change', [{'text': '原披露已撤回。'}, {'status': 'conflict'}, {'published_at': None}])
def test_baseline_only_source_invalidates_saved_report(run, change):
    saved = tracked_report(run)
    assert questions.report_sync(saved)['status'] == 'current'
    assert 'baseline-only' in saved['memo']['question_snapshot']['report_sources']
    changed = store.update_run(run['id'], lambda r: r['research_sources'][-1].update(change))
    sync = questions.report_sync(changed)
    assert sync['status'] == 'stale' and sync['changed_source_ids'] == ['baseline-only']
    assert sync['recommendation_freshness']['status'] == 'current'
    assert changed['memo'] == saved['memo']


def test_baseline_change_during_generation_cannot_be_saved_as_current(run):
    saved = tracked_report(run)
    validated = deep.validate_memo(deepcopy(saved['memo']), saved)
    changed = store.update_run(run['id'], lambda r: r['research_sources'][-1].update(text='披露撤回。'))
    with pytest.raises(ValueError, match='生成期间问题或引用来源已变化'):
        deep.save_memo(run['id'], validated, '并发旧稿不应保存')
    assert store.get_run(run['id']) == changed


def test_title_edit_keeps_unprocessed_observation_visible_and_stale(run, monkeypatch):
    saved = tracked_report(run)
    oid = observe(saved)
    before = store.get_run(run['id'])
    updated, _ = refine(before, monkeypatch, '只修改报告标题，其他内容保持不变。',
        {'edits': [{'path': '/title', 'value': '合成研究：仅改标题'}]})
    assert updated['memo']['sections'] == before['memo']['sections']
    assert updated['memo']['investment_recommendation'] == before['memo']['investment_recommendation']
    assert updated['memo_revisions'][-1]['memo'] == before['memo']
    sync = questions.report_sync(updated)
    assert sync['status'] == 'stale' and sync['pending_observation_ids'] == [oid]
    view = tracking.snapshot(updated)
    assert view['observations'] == [] and view['previous_report_observations'][0]['id'] == oid
    assert view['pending_observations'][0]['id'] == oid
    assert extensions.context(updated)['tracking']['observations'][0]['id'] == oid
    rows = [{'tool': 'get_tracking_metrics', 'result': view}]
    projected = agent_context.compact_tool_results(agent_context.compact_tool_results(rows))[0]['result']
    assert projected['pending_observations'][0]['id'] == oid
    assert projected['pending_observation_count'] == 1
    # Read-only status/context calls do not migrate the old published version.
    assert store.get_run(run['id']) == updated


def test_full_refine_records_only_actual_bounded_input_and_eventually_catches_up(run, monkeypatch):
    saved = tracked_report(run)
    ids = [observe(saved, f'合成观察{i}：回款仍需核实。') for i in range(10)]
    current = store.get_run(run['id'])
    draft = {k: v for k, v in current['memo'].items() if k in report_edit.EDITORIAL_FIELDS}
    first, payload = refine(current, monkeypatch, '请依据已读材料和跟踪观察完整更新整份研究报告。', draft)
    assert [o['id'] for o in payload['research_extensions']['tracking']['observations']] == ids[:8]
    assert first['memo']['tracking_observation_context']['included_ids'] == ids[:8]
    assert questions.report_sync(first)['pending_observation_ids'] == ids[8:]
    assert questions.report_sync(first)['status'] == 'stale'
    second, payload = refine(first, monkeypatch, '请依据其余跟踪观察完整更新整份研究报告。', draft)
    assert [o['id'] for o in payload['research_extensions']['tracking']['observations']] == ids[8:]
    assert second['memo']['tracking_observation_context']['included_ids'] == ids
    assert questions.report_sync(second)['status'] == 'current'
    assert '不等于独立核验' in second['memo']['tracking_observation_context']['semantic_status']
    assert second['tracking_observations'] == first['tracking_observations']


def test_title_edit_preserves_existing_observation_input_receipt(run, monkeypatch):
    saved = tracked_report(run); oid = observe(saved)
    current = store.get_run(run['id'])
    draft = {k: v for k, v in current['memo'].items() if k in report_edit.EDITORIAL_FIELDS}
    analyzed, _ = refine(current, monkeypatch, '请结合观察完整更新研究报告。', draft)
    receipt = deepcopy(analyzed['memo']['tracking_observation_context'])
    new_id = observe(analyzed, '新增不同观察，尚未纳入正文。')
    updated, _ = refine(store.get_run(run['id']), monkeypatch, '只修改报告标题，其他内容保持不变。',
        {'edits': [{'path': '/title', 'value': '标题修订'}]})
    assert updated['memo']['tracking_observation_context'] == receipt
    assert receipt['included_ids'] == [oid]
    assert questions.report_sync(updated)['pending_observation_ids'] == [new_id]


def test_full_build_includes_observations_but_does_not_acknowledge_concurrent_arrival(run, monkeypatch):
    saved = tracked_report(run); first_id = observe(saved)
    current = store.get_run(run['id'])
    draft = {k: v for k, v in current['memo'].items() if k in report_edit.EDITORIAL_FIELDS}
    chat = store.new_conversation(run['id'])
    monkeypatch.setattr(deep.documents, 'attached_documents', lambda _: [])
    def unavailable(*args, **kwargs):
        raise ValueError('合成测试无联网年报')
    monkeypatch.setattr(deep.disclosures, 'fetch_annual', unavailable)
    monkeypatch.setattr(deep.web_research, 'search', lambda queries, **kwargs: {'queries': queries, 'results': []})
    calls, new_ids = [], []
    def model(system, payload, *args, **kwargs):
        calls.append(deepcopy(payload))
        if len(calls) < 3:
            return {'queries': [], 'questions': [], 'pdf_queries': []}, {'model': 'stub'}
        if len(calls) == 3:
            new_ids.append(observe(store.get_run(run['id']), '模型生成期间新到的观察。'))
            return deepcopy(draft), {'model': 'stub'}
        return {'issues': [], 'requires_revision': False}, {'model': 'stub'}
    monkeypatch.setattr(deep, 'model_json', model)
    result = deep.build(run['id'], '依据材料完成整稿研究', chat)
    assert result['artifact'] == 'report' and len(calls) == 4
    updated = store.get_run(run['id'])
    assert [o['id'] for o in calls[2]['research_extensions']['tracking']['observations']] == [first_id]
    assert updated['memo']['tracking_observation_context']['included_ids'] == [first_id]
    assert questions.report_sync(updated)['pending_observation_ids'] == new_ids


def test_reading_more_of_same_pdf_does_not_replace_old_advice_evidence_or_new_prose(run, monkeypatch):
    # Both reads use the real source-registration path and the same PDF/page ID.
    monkeypatch.setattr(store, 'get_document', lambda _: {'announced_date': '2026-09-30'})
    chat = store.new_conversation(run['id'])
    base = {'document_id': 'local', 'page': 1, 'title': '合成经营披露',
        'url': 'https://example.com/local.pdf#page=1', 'text': '合成披露：客户回款存在不确定性。'}
    deep.register_sources(chat, {'snippets': [base]})
    draft = memo(suggestion(evidence_ids=['pdf-local-p1']))
    for item in draft['sections'] + draft['theses']: item['evidence_ids'] = ['pdf-local-p1']
    saved = publish(store.get_run(run['id']), draft)
    old_advice = deepcopy(saved['memo']['investment_recommendation'])
    old_sources = deepcopy(saved['memo']['recommendation_sources'])
    new_text = base['text'] + '新增已读段落：公司主营产品为工业设备。'
    deep.register_sources(chat, {'snippets': [{**base, 'text': new_text}]})
    updated, _ = refine(store.get_run(run['id']), monkeypatch, '只修改业务章节正文，其他内容保持不变。',
        {'edits': [{'path': '/sections/business/body', 'value': '新取得的原文披露主营产品为工业设备。'}]})
    assert next(s for s in updated['memo']['sources'] if s['id'] == 'pdf-local-p1')['text'] == new_text
    assert updated['memo']['recommendation_sources'] == old_sources
    assert next(s for s in old_sources if s['id'] == 'pdf-local-p1')['text'] == base['text']
    assert updated['memo']['investment_recommendation'] == old_advice
    assert updated['memo_revisions'][-1]['memo'] == saved['memo']
    assert questions.report_sync(updated)['recommendation_freshness']['status'] == 'stale'


@pytest.mark.parametrize('missing', [False, True])
def test_legacy_advice_fallback_uses_saved_source_or_reports_missing_never_latest(run, monkeypatch, missing):
    saved = publish(run)
    frozen = deepcopy(saved['memo']['recommendation_sources'])
    def legacy(r):
        r['memo'].pop('recommendation_sources')
        r['memo'].pop('recommendation_source_gaps')
        r['memo']['sources'] = [s for s in frozen if not (missing and s['id'] == 'official-risk')]
    saved = store.update_run(run['id'], legacy)
    original = deepcopy(saved['memo'])
    store.update_run(run['id'], lambda r: r['research_sources'][0].update(text='同ID的最新披露，不能冒充旧建议原文。'))
    updated, _ = refine(store.get_run(run['id']), monkeypatch, '只修改报告标题，其他内容保持不变。',
        {'edits': [{'path': '/title', 'value': '旧版建议保留'}]})
    advice_sources = {s['id']: s for s in updated['memo']['recommendation_sources']}
    if missing:
        assert 'official-risk' not in advice_sources
        assert updated['memo']['recommendation_source_gaps'] == ['official-risk']
        assert any('缺少当时冻结' in s for s in questions.report_sync(updated)['recommendation_freshness']['reasons'])
    else:
        assert advice_sources['official-risk']['text'] == frozen[0]['text']
        assert updated['memo']['recommendation_source_gaps'] == []
    assert updated['memo']['sources'][0]['text'] == '同ID的最新披露，不能冒充旧建议原文。'
    assert updated['memo_revisions'][-1]['memo'] == original


def test_model_cannot_invent_frozen_recommendation_sources_or_tracking_receipt(run):
    draft = memo(suggestion())
    draft['recommendation_sources'] = [{'id': 'official-risk', 'text': '伪造原文'}]
    draft['tracking_observation_context'] = {'schema_version': 1, 'included_ids': ['invented']}
    clean = deep.validate_memo(draft, run)
    assert clean['recommendation_sources'][0]['text'] == run['research_sources'][0]['text']
    assert 'tracking_observation_context' not in clean


def test_explicit_empty_frozen_sources_do_not_fall_back_to_current_prose(run, monkeypatch):
    saved = publish(run)
    store.update_run(run['id'], lambda r: r['memo'].update(recommendation_sources=[]))
    updated, _ = refine(store.get_run(run['id']), monkeypatch, '只修改报告标题，其他内容保持不变。',
        {'edits': [{'path': '/title', 'value': '保留空快照的缺口'}]})
    assert updated['memo']['recommendation_sources'] == []
    assert updated['memo']['recommendation_source_gaps'] == saved['memo']['investment_recommendation']['evidence_ids']
    assert questions.report_sync(updated)['recommendation_freshness']['needs_update']
