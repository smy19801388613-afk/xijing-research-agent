from copy import deepcopy
from datetime import date

import pytest

from app import deep_research as deep, investment_recommendation as recommendations
from app import market_data, report_edit, report_evidence, store, valuation


@pytest.fixture
def run(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    monkeypatch.setattr(market_data, '_today', lambda: date(2026, 10, 17))
    value = store.create_run({'ticker': '600000.SH', 'year': 2025, 'as_of': '2026-10-17',
                              'mode': 'live', 'question': '合成测试，不是真实投资建议'})
    value.update(state='completed', company='合成公司', facts=[], calculations=[], research_sources=[{
        'id': 'official-risk', 'kind': 'pdf', 'title': '合成经营风险披露', 'source_type': 'company_disclosure',
        'text': '合成公司主要客户连续逾期，经营活动现金流为负，持续经营能力存在重大不确定性。',
        'url': 'https://example.com/synthetic.pdf', 'published_at': '2026-09-30', 'read_status': 'pdf_text'}])
    value['market_snapshot'] = {'status': 'available', 'ticker': '600000.SH', 'price': '10',
        'currency': 'CNY', 'trade_date': '2026-10-16', 'as_of': '2026-10-17', 'query_as_of': '2026-10-17',
        'source_id': 'market-synthetic', 'text': '合成行情：600000.SH 2026-10-16 收盘价10元/股',
        'source_url': 'https://example.com/synthetic-quote', 'provider': 'tushare'}
    scenario = valuation.calculate(value, 'ddm', {'dividend_per_share': '1', 'cost_of_equity_pct': '10',
                                                 'terminal_growth_pct': '2'})
    scenario['id'] = 'synthetic-ddm'
    value['valuation_history'] = [scenario]
    store.save_run(value)
    return value


def suggestion(**changes):
    return {'action': 'buy', 'status': 'actionable', 'basis': 'valuation', 'confidence': 'medium',
        'summary': '在既定股利假设下有价格空间，关键是验证股利持续性；该假设不能替代经营判断。',
        'horizon': '未来十二个月，下一次定期报告披露后重审',
        'new_position': '未持仓者在确认股利政策及风险承受能力后考虑买入。',
        'existing_position': '若已持有，可维持并按股利兑现情况复核。',
        'buy_conditions': ['股利可由经常性现金流覆盖且既定假设获得持续支持'],
        'exit_conditions': ['股利停止且经营现金流持续不足'],
        'revisit_triggers': ['下一次定期报告披露实际现金流和分红方案'],
        'key_risks': ['股利不可持续会使情景价值失效'], 'blocking_gaps': [],
        'evidence_ids': ['official-risk'], 'valuation_scenario_ids': ['synthetic-ddm'], **changes}


def normalize(run, value=None):
    return recommendations.normalize(value or suggestion(), deep.reference_catalog(run), run)


def memo(value=None):
    result = {'title': '合成研究', 'summary': '按实际证据判断', 'stance': '明确条件与风险',
        'theses': [{'title': title, 'body': '披露信息支持研究推断', 'counter_evidence': '现金恢复可能推翻判断',
                    'invalidate_if': '取得现金恢复证据', 'status': 'inference', 'evidence_ids': ['official-risk']}
                   for title in ('现金质量', '股利持续性')],
        'sections': [{'key': key, 'body': '原文与判断分开', 'status': 'inference', 'evidence_ids': ['official-risk']}
                     for key in deep.SECTION_TITLES], 'questions': [], 'limitations': []}
    if value is not None: result['investment_recommendation'] = value
    return result


def test_actual_inputs_are_frozen_and_price_space_is_program_computed(run):
    before = deepcopy(run)
    value = normalize(run, suggestion(market_snapshot={'price': '999'}, price_comparisons=[{'upside_pct': '999'}]))
    assert value['market_snapshot']['price'] == '10'
    assert value['price_comparisons'] == [{'scenario_id': 'synthetic-ddm', 'scenario_price': '12.75',
        'current_price': '10', 'currency': 'CNY', 'trade_date': '2026-10-16',
        'upside_pct': '27.5000', 'assumption_status': 'user_scenario'}]
    assert set(value['evidence_ids']) == {'official-risk', 'market-synthetic', 'valuation-synthetic-ddm'}
    assert value['valuation_basis'][0]['assumptions']['cost_of_equity_pct'] == '10'
    assert run == before


@pytest.mark.parametrize('change', [
    {'status': 'missing'}, {'price': 'NaN'}, {'price': 0}, {'price': True}, {'ticker': '600001.SH'},
    {'currency': 'USD'}, {'trade_date': '2026-10-18'}, {'trade_date': '2026-09-30'},
    {'as_of': '2026-10-10'}, {'query_as_of': '2026-10-15'}, {'source_id': None}, {'text': ''},
])
def test_bad_or_old_market_cannot_support_actionable_valuation(run, change):
    run['market_snapshot'].update(change)
    with pytest.raises(ValueError, match='当前行动建议'):
        normalize(run)


def test_condition_buy_is_explicit_even_without_market_quote(run):
    run.pop('market_snapshot')
    value = normalize(run, suggestion(status='conditional', blocking_gaps=['待取得有效收盘价以核对假设空间'],
        summary='待有效股价和股利持续性均完成核实后考虑买入，不判断现价是否便宜。'))
    assert value['action'] == 'buy' and value['price_comparisons'] == []
    assert not value['market_snapshot']['decision_eligible']


def test_primary_risk_can_support_explicit_avoid_without_price_or_valuation(run):
    run.pop('market_snapshot'); run['valuation_history'] = []
    value = normalize(run, suggestion(action='avoid', basis='fundamental_risk',
        summary='连续逾期与持续经营不确定性尚未解除，明确回避新增投资；不对现价高低作判断。',
        new_position='未持仓者回避新增投资。', existing_position='若已持有，应核对风险承受能力并重新评估退出安排。',
        buy_conditions=['逾期风险解除并获得现金回收的原始披露'], valuation_scenario_ids=[]))
    assert value['status'] == 'actionable' and value['action'] == 'avoid'
    assert value['price_comparisons'] == []


def test_risk_exclusion_needs_primary_evidence(run):
    run['research_sources'][0].update(kind='web', source_type='news')
    with pytest.raises(ValueError, match='原始披露或监管'):
        normalize(run, suggestion(action='avoid', basis='fundamental_risk'))


@pytest.mark.parametrize('change', [
    {'published_at': '2026-10-18'}, {'read_status': 'search_snippet'}, {'status': 'conflict'}, {'text': ''},
])
def test_unknown_stale_or_unread_evidence_cannot_support_recommendation(run, change):
    run['research_sources'][0].update(change)
    with pytest.raises(ValueError, match='来源未取得'):
        normalize(run)


def test_unknown_date_cannot_support_action_ready_primary_judgment(run):
    run['research_sources'][0]['published_at'] = None
    with pytest.raises(ValueError, match='日期有效的原文'):
        normalize(run)


@pytest.mark.parametrize('change', [
    {'status': 'insufficient_evidence'}, {'action': 'watch'}, {'blocking_gaps': ['缺少关键回款证据']},
    {'action': 'sell'}, {'confidence': 'certain'}, {'basis': 'model_guess'},
    {'exit_conditions': []}, {'revisit_triggers': []}, {'key_risks': []},
    {'new_position': '你已经持有该股票，应继续买入。'},
])
def test_inconsistent_or_empty_decisions_are_rejected(run, change):
    with pytest.raises(ValueError): normalize(run, suggestion(**change))


def test_insufficient_evidence_requires_gap_and_switch_condition(run):
    value = suggestion(action='watch', status='insufficient_evidence', basis='evidence_gap', confidence='low',
        evidence_ids=[], valuation_scenario_ids=[], blocking_gaps=['未取得完整现金流及股利支付证据'])
    assert normalize(run, value)['status'] == 'insufficient_evidence'
    for field in ('blocking_gaps', 'buy_conditions', 'revisit_triggers'):
        with pytest.raises(ValueError): normalize(run, dict(value, **{field: []}))


def test_stale_tampered_or_unknown_scenarios_cannot_be_selected(run):
    with pytest.raises(ValueError, match='不存在'):
        normalize(run, suggestion(valuation_scenario_ids=['fake']))
    run['valuation_history'][0]['output']['price'] = '999'
    with pytest.raises(ValueError, match='输出与当前输入不一致'): normalize(run)
    run['valuation_history'][0]['basis_fingerprint'] = 'obsolete'
    with pytest.raises(ValueError, match='基础已变化'): normalize(run)


def test_no_shares_cannot_make_total_value_a_per_share_target(run):
    scenario = valuation.calculate(run, 'pb', {'book_equity_100m': 100, 'multiple': 2})
    run['valuation_history'] = [scenario]
    with pytest.raises(ValueError, match='同单位比较的每股估值'):
        normalize(run, suggestion(valuation_scenario_ids=[scenario['id']]))


def test_missing_quote_cannot_be_replaced_by_assertion_of_current_cheapness(run):
    run.pop('market_snapshot')
    with pytest.raises(ValueError, match='不能声称当前'):
        normalize(run, suggestion(status='conditional', summary='当前明显低估，因此应该建仓。',
            blocking_gaps=['价格需要更新']))
    value = normalize(run, suggestion(status='conditional', summary='当前无法判断是否低估，待取得价格再评估。',
        blocking_gaps=['价格需要更新']))
    assert value['action'] == 'buy'


def test_new_report_requires_decision_legacy_validation_does_not_migrate(run):
    assert 'investment_recommendation' not in deep.validate_memo(memo(), run)
    with pytest.raises(ValueError, match='investment_recommendation'):
        deep.validate_memo(memo(), run, require_recommendation=True)
    result = deep.validate_memo(memo(suggestion()), run, require_recommendation=True)
    assert {'market-synthetic', 'valuation-synthetic-ddm'}.issubset({s['id'] for s in result['sources']})


def test_local_other_edit_preserves_frozen_advice_without_refetching_price(run):
    previous = deep.validate_memo(memo(suggestion()), run)
    run['memo'] = previous
    run['market_snapshot'] = {'status': 'missing'}
    changed = deepcopy(previous); changed['title'] = '只改标题'
    saved = deep.validate_memo(changed, run, require_recommendation=True, preserve_recommendation=True)
    assert saved['investment_recommendation'] == previous['investment_recommendation']
    assert next(s for s in saved['sources'] if s['id'] == 'market-synthetic')['value'] == '10'


def test_recommendation_edit_scope_and_evidence_retention(run):
    chat = {'turn_id': 't', 'messages': [{'turn_id': 't', 'role': 'user',
        'content': '只修改投资建议，其他内容保持不变。'}]}
    previous = deep.validate_memo(memo(suggestion()), run)
    assert report_edit.resolve_scope(chat, previous).paths == ('/investment_recommendation',)
    run['memo'] = previous
    ids, _ = report_evidence.priorities(run, deep.reference_catalog(run))
    assert {'market-synthetic', 'valuation-synthetic-ddm'}.issubset(ids)


def test_recommendation_risks_remain_inside_recommendation_edit_scope(run):
    chat = {'turn_id': 't', 'messages': [{'turn_id': 't', 'role': 'user',
        'content': '只修改投资建议中的风险和买入条件，其他内容保持不变。'}]}
    assert report_edit.resolve_scope(chat, memo()).paths == ('/investment_recommendation',)


def test_market_quote_and_user_valuation_do_not_substitute_for_company_evidence(run):
    with pytest.raises(ValueError, match='原文或财务事实'):
        normalize(run, suggestion(evidence_ids=['market-synthetic']))


@pytest.mark.parametrize('change', [
    {'status': 'conditional', 'new_position': '建议立即买入。'},
    {'action': 'avoid', 'new_position': '建议建仓。'},
    {'action': 'reduce', 'existing_position': '现在应当加仓。'},
    {'action': 'avoid', 'existing_position': '若已持有，应立即加仓。'},
])
def test_obvious_action_contradictions_are_rejected(run, change):
    with pytest.raises(ValueError, match='不能同时要求'):
        normalize(run, suggestion(**change))


def test_conditional_actions_and_negative_commands_are_not_contradictions(run):
    assert normalize(run, suggestion(status='conditional', new_position='不要立即买入；待股利验证后再考虑建仓。'))['status'] == 'conditional'
    assert normalize(run, suggestion(action='avoid', basis='fundamental_risk',
        new_position='不建议买入；若风险解除后再考虑建仓。'))['action'] == 'avoid'


@pytest.mark.parametrize('text', [
    '不建议新建仓。等待经核验的同业/历史估值区间、H股价格、季度经营现金流和出口管制影响评估后再决定。',
    '暂不考虑新建仓。',
    '不建议现在新开仓。',
    '不要立即新建仓。',
    '不建议进一步加仓。',
])
def test_negated_new_position_language_does_not_become_buy_instruction(run, text):
    value=suggestion(action='watch',status='insufficient_evidence',basis='evidence_gap',confidence='low',
        new_position=text,blocking_gaps=['待核实可比倍数与现金流依据'])
    assert normalize(run,value)['new_position']==text


@pytest.mark.parametrize('text', [
    '建议新建仓。', '建议现在新开仓。', '应立即新建仓。', '不建议追高，但现在可以建仓。',
])
def test_affirmative_new_position_still_conflicts_with_watch(run,text):
    value=suggestion(action='watch',status='insufficient_evidence',basis='evidence_gap',confidence='low',
        new_position=text,blocking_gaps=['待核实可比倍数与现金流依据'])
    with pytest.raises(ValueError,match='不能同时要求'): normalize(run,value)


@pytest.mark.parametrize('text', [
    '已持仓者当前动作：不追加仓位，继续持有并密切跟踪。',
    '已持仓者当前动作：继续持有并密切跟踪，不追加仓位。',
    '当前建议保持观察，不采取买入、加仓、减持或回避动作。',
    '当前建议为观察，不采取买入或加仓动作。',
    '当前不建议立即买入或加仓。',
    '仅当买入条件同时满足并重新核验后，才考虑由观察转为买入或加仓。',
    '仅在以下买入条件全部满足并重新核验后，才重新评估是否建立仓位。',
])
def test_real_position_negation_lists_and_future_conditions_are_not_current_buy(run,text):
    value=suggestion(action='watch',status='conditional',existing_position=text,
        blocking_gaps=['待核实可比倍数与现金流依据'])
    assert normalize(run,value)['existing_position']==text


@pytest.mark.parametrize('text', [
    '已持仓者当前动作：立即追加仓位，继续持有并密切跟踪。',
    '现在建议增加仓位。',
    '立即建立仓位。',
    '不采取买入、加仓动作，但现在建议新建仓。',
    '不建议买入或加仓，但是立即追加仓位。',
    '当前条件已经满足，现在立即加仓。',
])
def test_negative_list_cannot_hide_separate_current_buy_instruction(run,text):
    value=suggestion(action='watch',status='conditional',existing_position=text,
        blocking_gaps=['待核实可比倍数与现金流依据'])
    with pytest.raises(ValueError,match='不能同时要求'): normalize(run,value)


@pytest.mark.parametrize('field,text', [
    ('new_position', '未持仓者现在立即买入，若下一季现金流恶化再退出。'),
    ('new_position', '未持仓者现在立即买入，风险仍待核实。'),
    ('existing_position', '若已持有，现在立即加仓，如果股利停止再减持。'),
    ('existing_position', '风险仍待核实，现在立即增加仓位。'),
    ('summary', '当前建议立即建仓，等待下一季核对风险。'),
])
def test_later_condition_or_unresolved_gap_cannot_hide_current_buy(run,field,text):
    value=suggestion(action='watch',status='conditional',blocking_gaps=['待核实可比倍数与现金流依据'])
    value[field]=text
    with pytest.raises(ValueError,match='不能同时要求'): normalize(run,value)


@pytest.mark.parametrize('text', [
    '若股利持续性得到验证，再考虑买入；当前不建仓。',
    '待核实风险后再考虑买入，若现金流恶化则退出。',
    '等待下一期披露核实股利，再评估是否建仓。',
    '若已持有，待现金流核实后再考虑加仓。',
    '当前买入条件尚未满足，不建议立即买入或加仓。',
])
def test_condition_applies_to_future_action_and_negations_remain_allowed(run,text):
    value=suggestion(action='watch',status='conditional',new_position=text,
        blocking_gaps=['待核实可比倍数与现金流依据'])
    assert normalize(run,value)['new_position']==text


def test_past_event_cannot_be_promoted_to_future_revisit_date(run):
    with pytest.raises(ValueError, match='过去事件'):
        normalize(run, suggestion(revisit_triggers=['将于2026年5月1日公布股利时重新评估']))


def test_unrelated_local_edit_does_not_refresh_old_recommendation_inputs(run):
    run['memo'] = deep.validate_memo(memo(suggestion()), run)
    assert recommendations.freshness(run)['status'] == 'current'
    run['market_snapshot'].update(price='11', source_id='market-new', text='合成新价格为11元/股')
    old_advice = deepcopy(run['memo']['investment_recommendation'])
    changed = deepcopy(run['memo']); changed['title'] = '仅标题改变'
    run['memo'] = deep.validate_memo(changed, run, preserve_recommendation=True)
    assert run['memo']['investment_recommendation'] == old_advice
    status = recommendations.freshness(run)
    assert status['needs_update'] and status['status'] == 'stale'
    assert any('市场报价' in reason for reason in status['reasons'])


def test_recommendation_only_evidence_change_is_stale(run):
    run['memo'] = deep.validate_memo(memo(suggestion()), run)
    before = deepcopy(run)
    run['research_sources'][0]['text'] += '新增与旧论点冲突的原始披露。'
    status = recommendations.freshness(run)
    assert status['needs_update'] and any('official-risk' in r for r in status['reasons'])
    assert run['memo'] == before['memo']


def test_new_same_method_valuation_requires_advice_review(run):
    run['memo'] = deep.validate_memo(memo(suggestion()), run)
    new_scenario = valuation.calculate(run, 'ddm', {'dividend_per_share': '2', 'cost_of_equity_pct': '10',
                                                   'terminal_growth_pct': '2'})
    run['valuation_history'].append(new_scenario)
    status = recommendations.freshness(run)
    assert status['needs_update'] and any('最新估值' in r for r in status['reasons'])


def test_full_refine_cannot_drop_existing_recommendation(run, monkeypatch):
    original = memo(suggestion()); validated = deep.validate_memo(original, run)
    store.update_run(run['id'], lambda r: r.update(memo=validated))
    chat = store.new_conversation(run['id'])
    calls = []
    def model(system, payload, *args, **kwargs):
        calls.append(payload)
        return (memo() if len(calls) == 1 else original), {'model': 'synthetic'}
    monkeypatch.setattr(deep, 'model_json', model)
    deep.refine(run['id'], '完整修订报告', chat)
    assert len(calls) == 2 and 'investment_recommendation' in calls[1]['validation_feedback']
    saved = store.get_run(run['id'])
    assert saved['memo']['investment_recommendation']['action'] == 'buy'
    assert saved['memo_revisions'][0]['memo'] == validated
