import copy
import json
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app import deep_research as deep, research_tracking as tracking, store


def metric():
    return {'id': 'cash-recovery', 'name': '经营现金回收',
        'definition': '合并经营活动现金流净额，同期口径，单位万元', 'thesis': '现金回收仍待验证',
        'baseline': {'status': 'observed', 'value': '100万元', 'period': '2025年度',
            'source_id': 'cash-source', 'quote': '2025年度经营活动现金流净额为100万元'},
        'frequency': '定期报告披露后', 'next_check': '下一份定期报告披露同口径数据后核查',
        'support_condition': '经营现金流改善且与主要客户回款证据一致',
        'invalidate_condition': '持续净流出且应收款逾期增加', 'threshold_basis': 'qualitative',
        'next_action': '核对附注并重新评估现金回收论点', 'status': 'monitoring'}


@pytest.fixture
def run(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    value = store.create_run({'ticker': '600000.SH', 'year': 2025, 'as_of': '2026-10-17',
                              'mode': 'live', 'question': '现金回收'})
    value.update(state='completed', company='测试公司', research_sources=[{
        'id': 'cash-source', 'kind': 'web', 'title': '年度经营披露',
        'text': '原文：2025年度经营活动现金流净额为100万元。主要客户按期付款。',
        'url': 'https://example.com/annual', 'published_at': '2026-04-30', 'read_status': 'body_read'}])
    value['memo'] = {'title': '现金回收研究', 'summary': '继续核查',
                     'theses': [{'title': '现金回收仍待验证'}], 'tracking_metrics': [metric()]}
    store.save_run(value)
    return value


def catalog(run):
    return {s['id']: s for s in run['research_sources']}


def normalize(run, item=None):
    return tracking.normalize_metrics([item or metric()], catalog(run), run, run['memo']['theses'])


def payload(run):
    return {'report_identity': tracking.snapshot(run)['report_identity'], 'source_id': 'cash-source',
            'quote': '主要客户按期付款', 'observation': '回款描述支持原论点，仍需核实金额与账龄。',
            'assessment': 'supporting'}


def test_valid_baseline_keeps_exact_original_and_does_not_mutate(run):
    before = copy.deepcopy(run)
    result = normalize(run)[0]
    assert result['baseline'] == metric()['baseline']
    assert result['threshold_confirmed'] is False
    assert result['status'] == 'monitoring'
    assert run == before


@pytest.mark.parametrize('value,period',[
    ('53,345,930千元（2025年）','FY2025'),
    ('53,345,930千元（2025年）','2025年度'),
    ('2025年12月31日：53,345,930千元','2025-12-31'),
    ('53,345,930千元（12月31日）','2025年12月31日'),
])
def test_explicit_matching_date_note_is_not_mistaken_for_baseline_amount(run,value,period):
    quote='经营活动产生的现金流量净额 53,345,930 60,511,572'
    run['research_sources'][0]['text']=quote
    item=metric();item['baseline'].update(value=value,period=period,quote=quote)
    result=normalize(run,item)[0]
    assert result['baseline']['value']==value and result['baseline']['period']==period
    assert result['baseline']['quote']==quote


@pytest.mark.parametrize('value,period,unmatched',[
    ('2025千元','FY2025','2025'),
    ('53,345,930千元（2025）','FY2025','2025'),
    ('53,345,930千元（2024年）','FY2025','2024'),
    ('53,345,930千元（12月）','FY2025','12'),
    ('53,345,930千元（2025年11月31日）','2025-12-31','11'),
    ('53,345,931千元（2025年）','FY2025','53345931'),
    ('5,334,593千元（2025年）','FY2025','5334593'),
    ('53,345,930千元（-2025年）','FY2025','-2025'),
])
def test_date_note_exception_never_hides_amount_or_unmatched_time(run,value,period,unmatched):
    quote='经营活动产生的现金流量净额 53,345,930 60,511,572'
    run['research_sources'][0]['text']=quote
    item=metric();item['baseline'].update(value=value,period=period,quote=quote)
    with pytest.raises(ValueError) as error: normalize(run,item)
    assert '不匹配值 '+unmatched in str(error.value)
    assert 'baseline.period' in str(error.value)


def total_value_grid(run, shares=None):
    from app import valuation, valuation_analysis
    inputs={'book_equity_100m':100,'multiple':10}
    if shares: inputs['shares_100m']=shares
    base=valuation.save_result(run['id'],'pb',inputs)
    valuation_analysis.save_sensitivity(run['id'],{'base_scenario_id':base['id'],
        'axis_x':[90,110],'axis_y':[8,12]})
    return store.get_run(run['id'])


@pytest.mark.parametrize('field,condition',[
    ('support_condition','市场价格落在敏感性区间且隐含PB接近用户假设'),
    ('invalidate_condition','市场价格远低于或高于情景包络'),
    ('support_condition','收盘价进入敏感性区间'),
    ('invalidate_condition','股价明显偏离估值区间'),
    ('support_condition','价格位于敏感性包络内'),
    ('support_condition','不能取得总股本，市场价格高于敏感性包络'),
    ('support_condition','股价不能低于总价值敏感性包络'),
    ('invalidate_condition','市场价格不得高于敏感性区间'),
])
def test_share_price_cannot_be_compared_to_actual_total_value_grid(run,field,condition):
    current=total_value_grid(run)
    assert 'price' not in current['valuation_sensitivity_history'][-1]['envelope']
    item=metric();item['definition']='最近交易日收盘价和总股本';item[field]=condition
    with pytest.raises(ValueError,match='必须比较同单位'):
        normalize(current,item)


@pytest.mark.parametrize('condition',[
    '取得数据后，将市场价格乘以总股本换算市值，再与敏感性股权价值包络比较',
    '先将股权总价值除以总股数得到每股价值，再与市场价格比较敏感性区间',
    '总股权价值÷总股本换算为每股价值后，收盘价低于敏感性区间下沿',
    '同口径总市值落在敏感性股权价值包络内',
    '不能将股价直接与总价值敏感性包络比较，当前缺少市值数据，待补',
])
def test_explicit_conversion_or_same_unit_pending_plan_is_permitted(run,condition):
    current=total_value_grid(run)
    item=metric();item['support_condition']=condition
    assert normalize(current,item)[0]['support_condition']==condition


@pytest.mark.parametrize('condition',[
    '市场价格除以总股本得到每股价值，再与敏感性区间比较',
    '总股权价值乘总股本得到每股价值后，股价落在敏感性区间',
    '不先将股价乘总股本换算市值，市场价格落在敏感性区间',
])
def test_wrong_or_negated_conversion_does_not_bypass_unit_guard(run,condition):
    current=total_value_grid(run);item=metric();item['support_condition']=condition
    with pytest.raises(ValueError,match='必须比较同单位'):normalize(current,item)


def test_actual_per_share_envelope_allows_price_comparison(run):
    current=total_value_grid(run,shares=2);item=metric()
    item['support_condition']='市场价格落在PB敏感性每股价格区间'
    assert normalize(current,item)[0]['support_condition']==item['support_condition']


def test_guard_uses_referenced_method_not_unrelated_per_share_grid(run):
    from app import valuation, valuation_analysis
    current=total_value_grid(run)
    ddm=valuation.save_result(run['id'],'ddm',{'dividend_per_share':1,'cost_of_equity_pct':8,'terminal_growth_pct':2})
    valuation_analysis.save_sensitivity(run['id'],{'base_scenario_id':ddm['id'],'axis_x':[7,9],'axis_y':[1,3]})
    item=metric();item['support_condition']='市场价格落在PB敏感性区间'
    with pytest.raises(ValueError,match='必须比较同单位'):normalize(store.get_run(run['id']),item)


def test_no_grid_does_not_invent_total_value_output_or_relax_baseline_quotes(run):
    item=metric();item['support_condition']='取得每股敏感性结果后，将收盘价与敏感性区间比较'
    assert normalize(run,item)
    current=total_value_grid(run,shares=2);item['baseline']['quote']='虚构的原文金额100万元'
    with pytest.raises(ValueError,match='实际取得的原文中匹配'):normalize(current,item)


def test_enterprise_value_requires_equity_bridge_before_per_share_comparison(run):
    from app import valuation, valuation_analysis
    base=valuation.save_result(run['id'],'dcf',{'base_fcff_100m':10,'growth_pct':5,'years':3,'wacc_pct':10,'terminal_growth_pct':2})
    valuation_analysis.save_sensitivity(run['id'],{'base_scenario_id':base['id'],'axis_x':[8,10],'axis_y':[1,2]})
    current=store.get_run(run['id']);item=metric()
    item['support_condition']='总价值除以总股数得到每股价值后，市场价格低于DCF敏感性区间'
    with pytest.raises(ValueError,match='企业价值还需先桥接'):normalize(current,item)
    item['support_condition']='企业价值扣除净债务桥接为股权价值，总值除以总股数得到每股价值后，市场价格低于DCF敏感性区间'
    assert normalize(current,item)


@pytest.mark.parametrize('change', [
    {'source_id': 'invented'}, {'quote': '经营活动现金流100万元'}, {'value': '1000万元'},
    {'quote': '100'}, {'period': ''}, {'status': 'verified'},
])
def test_invalid_or_invented_baseline_rejected(run, change):
    item = metric(); item['baseline'].update(change)
    with pytest.raises(ValueError): normalize(run, item)


@pytest.mark.parametrize('change', [
    {'read_status': 'search_snippet'}, {'read_status': 'windows_ocr'},
    {'published_at': None}, {'published_at': '2026-10-18'}, {'status': 'conflict'}, {'text': ''},
])
def test_unread_unknown_future_conflict_and_ocr_sources_rejected(run, change):
    run['research_sources'][0].update(change)
    with pytest.raises(ValueError, match='日期有效|已确认基线|匹配'): normalize(run)


def test_missing_baseline_has_explicit_gap_and_cannot_smuggle_source(run):
    item = metric(); item['baseline'] = {'status': 'missing', 'value': '尚未取得同口径现金流数据',
        'period': '2026年第三季度', 'source_id': 'invented', 'quote': '虚构原文'}
    result = normalize(run, item)[0]
    assert result['status'] == 'needs_baseline'
    assert result['baseline']['source_id'] is None and result['baseline']['quote'] == ''


def test_numeric_trigger_cannot_claim_user_confirmation(run):
    item = metric(); item.update(support_condition='经营现金流超过200万元', threshold_confirmed=True)
    result = normalize(run, item)[0]
    assert result['threshold_basis'] == 'unconfirmed_assumption'
    assert result['threshold_confirmed'] is False


def test_duplicate_id_and_unlinked_thesis_rejected(run):
    with pytest.raises(ValueError, match='唯一'):
        tracking.normalize_metrics([metric(), metric()], catalog(run), run)
    item = metric(); item['thesis'] = '不存在的论点'
    with pytest.raises(ValueError, match='实际核心论点'): normalize(run, item)


def test_next_check_cannot_present_elapsed_date_as_future(run):
    item = metric(); item['next_check'] = '2026年4月30日检查新披露的年度报告'
    with pytest.raises(ValueError, match='下次跟踪检查'): normalize(run, item)


def test_observation_preserves_report_and_attaches_immutable_version_snapshot(run):
    before = copy.deepcopy(run['memo'])
    result = tracking.record_observation(run['id'], 'cash-recovery', payload(run), 'model')
    saved = store.get_run(run['id'])
    assert saved['memo'] == before and saved['report_revision'] == 1
    entry = result['observations'][0]
    assert entry['metric_snapshot'] == before['tracking_metrics'][0]
    assert entry['report_identity'] == tracking.snapshot(run)['report_identity']
    assert entry['origin'] == 'model' and entry['assessment'] == 'supporting'
    result['observations'][0]['metric_snapshot']['thesis'] = '仅修改返回副本'
    assert store.get_run(run['id'])['tracking_observations'][0]['metric_snapshot']['thesis'] == '现金回收仍待验证'


def test_stale_version_and_cross_run_metric_rejected_atomically(run):
    old = payload(run)
    store.update_run(run['id'], lambda r: r.update(report_revision=2))
    with pytest.raises(ValueError, match='报告版本已变化'):
        tracking.record_observation(run['id'], 'cash-recovery', old)
    current = store.get_run(run['id'])
    with pytest.raises(ValueError, match='没有此跟踪指标'):
        tracking.record_observation(run['id'], 'other-metric', payload(current))
    assert 'tracking_observations' not in store.get_run(run['id'])


def test_same_revision_different_memo_hash_is_also_stale(run):
    old = payload(run)
    store.update_run(run['id'], lambda r: r['memo'].update(summary='新结论'))
    with pytest.raises(ValueError, match='报告版本已变化'):
        tracking.record_observation(run['id'], 'cash-recovery', old)


def test_new_report_does_not_silently_reuse_prior_observation(run):
    tracking.record_observation(run['id'], 'cash-recovery', payload(run))
    store.update_run(run['id'], lambda r: r.update(report_revision=2))
    view = tracking.snapshot(store.get_run(run['id']))
    assert view['observations'] == []
    assert len(view['previous_report_observations']) == 1


@pytest.mark.parametrize('change', [{'read_status': 'search_snippet'}, {'published_at': '2026-10-18'}, {'published_at': None}])
def test_observation_provenance_revalidated_at_write_time(run, change):
    store.update_run(run['id'], lambda r: r['research_sources'][0].update(change))
    with pytest.raises(ValueError, match='已读'):
        tracking.record_observation(run['id'], 'cash-recovery', payload(run))
    assert 'tracking_observations' not in store.get_run(run['id'])


def test_legacy_read_is_nonmutating_and_export_has_no_invented_plan(run):
    store.update_run(run['id'], lambda r: r['memo'].pop('tracking_metrics'))
    before = store.get_run(run['id'])
    assert tracking.snapshot(before)['metrics'] == []
    assert tracking.markdown(before) == ''
    assert store.get_run(run['id']) == before


def test_export_contains_plan_assumption_and_current_observation(run):
    item = metric(); item['threshold_basis'] = 'unconfirmed_assumption'
    store.update_run(run['id'], lambda r: r['memo'].update(tracking_metrics=[item]))
    current = store.get_run(run['id'])
    tracking.record_observation(run['id'], 'cash-recovery', payload(current))
    output = tracking.markdown(store.get_run(run['id']))
    assert all(text in output for text in ('当前基线', '更新安排', '未获用户确认', '本版本后续观察', '主要客户按期付款'))


def test_router_roundtrip_uses_isolated_data_and_preserves_memo(run):
    app = FastAPI(); app.include_router(tracking.router)
    client = TestClient(app)
    response = client.get(f"/api/runs/{run['id']}/tracking")
    assert response.status_code == 200 and response.json()['metrics'][0]['id'] == 'cash-recovery'
    response = client.post(f"/api/runs/{run['id']}/tracking/cash-recovery/observations", json=payload(run))
    assert response.status_code == 200 and len(response.json()['observations']) == 1
    assert store.get_run(run['id'])['memo'] == run['memo']


def memo_candidate():
    return {'title': '现金研究', 'summary': '现金回收需核实', 'stance': '继续跟踪',
        'theses': [{'title': name, 'body': '现金回收仍需核实', 'evidence_ids': ['cash-source'],
                   'counter_evidence': '收款描述并不等于回款全额', 'invalidate_if': '逾期明显增加', 'status': 'inference'}
                  for name in ('现金回收仍待验证', '客户质量需要持续核查')],
        'sections': [{'key': key, 'body': '事实与推断分开', 'evidence_ids': ['cash-source'], 'status': 'inference'}
                     for key in deep.SECTION_TITLES], 'questions': [], 'limitations': []}


def test_new_report_requires_tracking_while_legacy_validator_keeps_absent(run):
    draft = memo_candidate()
    assert 'tracking_metrics' not in deep.validate_memo(draft, run)
    with pytest.raises(ValueError, match='持续跟踪指标'):
        deep.validate_memo(draft, run, require_tracking=True)
    draft['tracking_metrics'] = [metric()]
    saved = deep.validate_memo(draft, run, require_tracking=True)
    assert saved['tracking_metrics'][0]['baseline']['source_id'] == 'cash-source'
    assert json.dumps(saved, ensure_ascii=False).count('现金回收仍待验证') >= 2


def test_tracking_only_evidence_row_is_added_to_frozen_report_sources(run):
    run['evidence'] = [{'id': 'raw-cash-row', 'kind': 'api', 'quote': '2025年度经营活动现金流净额为100万元',
                       'announced_date': '2026-04-30', 'source_url': 'https://example.com/raw'}]
    draft = memo_candidate(); item = metric(); item['baseline']['source_id'] = 'raw-cash-row'
    draft['tracking_metrics'] = [item]
    result = deep.validate_memo(draft, run, require_tracking=True)
    assert any(s['id'] == 'raw-cash-row' for s in result['sources'])


def test_refinement_repairs_accidentally_dropped_existing_tracking_plan(run, monkeypatch):
    original = memo_candidate(); original['tracking_metrics'] = [metric()]
    store.update_run(run['id'], lambda r: r.update(memo=deep.validate_memo(original, run)))
    chat = store.new_conversation(run['id'])
    calls = []
    def model(system, body, *args, **kwargs):
        calls.append(body)
        return (memo_candidate() if len(calls) == 1 else original), {'model': 'stub'}
    monkeypatch.setattr(deep, 'model_json', model)
    deep.refine(run['id'], '基于已读资料修订报告', chat)
    saved = store.get_run(run['id'])
    assert len(calls) == 2 and '持续跟踪指标' in calls[-1]['validation_feedback']
    assert saved['memo']['tracking_metrics'][0]['id'] == 'cash-recovery'
    assert saved['memo_revisions'][0]['memo']['tracking_metrics'][0]['id'] == 'cash-recovery'


def test_local_thesis_rename_keeps_unchanged_original_tracking_link(run, monkeypatch):
    original = memo_candidate(); original['tracking_metrics'] = [metric()]
    store.update_run(run['id'], lambda r: r.update(memo=deep.validate_memo(original, run)))
    chat = store.new_conversation(run['id'])
    chat = store.begin_turn(chat['id'], '仅修改第1个论点标题，其他内容保持不变。', 'stub', 'documents')
    monkeypatch.setattr(deep, 'model_json', lambda *a, **kw: ({'edits': [
        {'path': '/theses/0/title', 'value': '经营现金回收有待验证'}]}, {'model': 'stub'}))
    before = store.get_run(run['id'])
    deep.refine(run['id'], '局部修改标题', chat)
    after = store.get_run(run['id'])
    assert after['memo']['theses'][0]['title'] == '经营现金回收有待验证'
    assert after['memo']['tracking_metrics'] == before['memo']['tracking_metrics']


def test_changed_tracking_cannot_use_legacy_link_escape_hatch(run):
    changed = metric(); changed['name'] = '新指标'
    with pytest.raises(ValueError, match='实际核心论点'):
        tracking.normalize_metrics([changed], catalog(run), run, [{'title': '新论点'}], allow_existing_thesis=True)
