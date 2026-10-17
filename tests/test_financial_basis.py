from copy import deepcopy
from decimal import Decimal

import pytest

from app import deep_research, financial_basis, financial_periods as periods, fact_state, research, store, valuation
from app.evidence_policy import eligibility
from app.finance import compute, comparison_check
from app.investment_recommendation import _scenarios
from app.valuation_analysis import calculate_sensitivity


@pytest.fixture
def run(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    value = store.create_run({'ticker': '600000.SH', 'year': 2025, 'as_of': '2026-10-17', 'mode': 'live'})
    value['state'] = 'completed'
    for year in (2024, 2025):
        research.add_api_facts(value, 'income', year, {'revenue': '1000000000',
            'n_income_attr_p': '100000000', 'ann_date': f'{year+1}0420'}, 'synthetic')
    value['calculations'] = compute(value['facts'], 2025)
    value['checks'].append(comparison_check(value['calculations']))
    value['research_sources'] = [{'id': 'synthetic-disclosure', 'kind': 'pdf',
        'title': '合成公司披露', 'published_at': '2026-04-20', 'read_status': 'pdf_text',
        'text': '2025年合并报表归母净利润120000000元，营业收入1200000000元。'}]
    store.save_run(value)
    return value


def row(metric='n_income_attr_p', **changes):
    return {'metric': metric, 'year': 2025, 'period': 'FY', 'period_type': 'cumulative',
        'value': '120000000' if metric == 'n_income_attr_p' else '1200000000', 'unit': '元',
        'currency': 'CNY', 'scope': 'consolidated', 'announced_date': '2026-04-20',
        'source_type': 'existing', 'source_id': 'synthetic-disclosure',
        'quote': '2025年合并报表归母净利润120000000元，营业收入1200000000元。', **changes}


@pytest.mark.parametrize('metric,method', [('n_income_attr_p', 'pe'), ('revenue', 'ps')])
def test_conflict_propagates_to_both_ledgers_valuation_and_citations_without_rewriting_history(run, metric, method):
    original = deepcopy(run)
    scenario = valuation.save_result(run['id'], method, {'growth_pct': 0, 'multiple': 20, 'shares_100m': 1})
    periods.import_facts(run['id'], [f'{metric}-2025'])
    view = periods.save_records(run['id'], [row(metric)], origin='model')
    current = store.get_run(run['id'])
    assert any(i['code'] == 'annual_basis_conflict' for i in view['analysis']['issues'])
    assert next(n for n in view['analysis']['cumulative'] if n['metric'] == metric)['value'] is None
    option = next(o for o in valuation.options(current) if o['id'] == method)
    assert not option['ready_base'] and '冲突' in option['basis_note']
    with pytest.raises(ValueError, match='同口径金额冲突'):
        valuation.save_result(run['id'], method, {'growth_pct': 0, 'multiple': 20, 'shares_100m': 1})
    with pytest.raises(ValueError, match='同口径金额冲突'):
        _scenarios([scenario['id']], current)
    with pytest.raises(ValueError, match='同口径金额冲突'):
        calculate_sensitivity(current, {'base_scenario_id': scenario['id'], 'axis_x': [0, 5], 'axis_y': [15, 20]})
    displayed = fact_state.enrich(current)
    fact = next(f for f in displayed['facts'] if f['id'] == f'{metric}-2025')
    assert not fact['verification']['usable_for_calculation']
    assert not eligibility({'id': fact['id'], 'kind': 'financial_fact', 'value': fact['value'],
        'published_at': fact['announced_date']}, current)['supports_inference']
    assert next(c for c in displayed['calculations'] if c['metric'] == metric)['value'] is None
    assert store.get_run(run['id'])['facts'] == original['facts']
    assert store.get_run(run['id'])['calculations'] == original['calculations']
    assert store.get_run(run['id'])['valuation_history'] == [scenario]


def test_period_only_record_still_checks_annual_ledger_and_resolves_after_wrong_record_removed(run):
    saved = periods.save_records(run['id'], [row()], origin='model')
    current = store.get_run(run['id'])
    assert financial_basis.conflicts(current)
    periods.delete_record(run['id'], saved['records'][0]['id'])
    repaired = store.get_run(run['id'])
    assert not financial_basis.conflicts(repaired)
    assert valuation.calculate(repaired, 'pe', {'growth_pct': 0, 'multiple': 20})['output']['equity_value_100m'] == '20'


@pytest.mark.parametrize('change', [
    {'period': 'H1'}, {'period': 'Q4', 'period_type': 'single'}, {'currency': 'USD'},
    {'year': 2023}, {'restatement': 'restated', 'comparability_key': 'restated-2025', 'restatement_note': '合成重述'},
    {'comparability_key': 'adjusted'},
])
def test_distinct_period_or_accounting_basis_does_not_block_original_annual_base(run, change):
    periods.save_records(run['id'], [row(**change)], origin='model')
    current = store.get_run(run['id'])
    assert not financial_basis.conflicts(current)
    assert valuation.calculate(current, 'pe', {'growth_pct': 0, 'multiple': 20})['output']['equity_value_100m'] == '20'


def test_parent_revenue_does_not_conflict_with_consolidated_revenue(run):
    periods.save_records(run['id'], [row('revenue', scope='parent')], origin='model')
    assert not financial_basis.conflicts(store.get_run(run['id']))


def test_old_import_is_invalidated_and_can_refresh_without_becoming_independent_conflict(run):
    periods.import_facts(run['id'], ['n_income_attr_p-2025'])
    def change(value):
        fact = next(f for f in value['facts'] if f['id'] == 'n_income_attr_p-2025')
        fact.update(value='120000000', original_value='120000000')
    current = store.update_run(run['id'], change)
    assert not financial_basis.conflicts(current)
    assert periods.snapshot(current)['records'][0]['source_status'] == 'source_changed'
    refreshed = periods.import_facts(run['id'], ['n_income_attr_p-2025'])
    assert refreshed['records'][0]['value_base'] == '120000000'
    assert refreshed['records'][0]['source_status'] == 'fact_ledger'


def test_legacy_pe_endpoint_cannot_bypass_reconciliation(run):
    from app.main import calculate_scenario
    from app.schemas import ScenarioRequest
    periods.save_records(run['id'], [row()], origin='model')
    with pytest.raises(ValueError, match='同口径金额冲突'):
        calculate_scenario(run['id'], ScenarioRequest(growth_pct=0, pe=20, shares_100m=1))
    assert not store.get_run(run['id'])['scenario_history']


def _profit_source(run, value, original_value=None, original_unit='元'):
    """Replace only synthetic fixture data, preserving its actual source text."""
    def change(current):
        fact = next(f for f in current['facts'] if f['id'] == 'n_income_attr_p-2025')
        original = value if original_value is None else original_value
        fact.update(value=value, original_value=original, original_unit=original_unit,
                    status='document_only', evidence_ids=['synthetic-profit-source'])
        current['evidence'].append({'id': 'synthetic-profit-source', 'kind': 'document',
            'announced_date': '20260420', 'quote': f'2025年合并报表归母净利润 {original} {original_unit}。'})
        current['calculations'] = compute(current['facts'], 2025)
    return store.update_run(run['id'], change)


def _rounded_row(run, value, unit):
    quote = f'2025年合并报表归母净利润 {value} {unit}。'
    store.update_run(run['id'], lambda current: current['research_sources'].append({
        'id': 'rounded-disclosure', 'kind': 'pdf', 'title': '合成年度披露',
        'published_at': '2026-04-20', 'read_status': 'pdf_text', 'text': quote}))
    return row(value=value, unit=unit, source_id='rounded-disclosure', quote=quote)


@pytest.mark.parametrize('import_fact', [False, True])
@pytest.mark.parametrize('exact,rounded,unit,compatible', [
    ('100000123.45', '100000', '千元', True),
    ('100000500.00', '100000', '千元', True),
    ('100000500.01', '100000', '千元', False),
    ('100000125.00', '100000.12', '千元', True),
    ('100000125.01', '100000.12', '千元', False),
    ('100000120.50', '100000.120', '千元', True),
    ('100000120.51', '100000.120', '千元', False),
    ('100000004.99', '10000.000', '万元', True),
    ('100000005.01', '10000.000', '万元', False),
    ('100004999.99', '1.0000', '亿元', True),
    ('100005000.01', '1.0000', '亿元', False),
    ('100000123.45', '1E+5', '千元', True),
    ('100000500.01', '1E+5', '千元', False),
])
def test_original_precision_controls_annual_reconciliation(run, exact, rounded, unit, compatible, import_fact):
    _profit_source(run, exact)
    if import_fact:
        periods.import_facts(run['id'], ['n_income_attr_p-2025'])
    record = _rounded_row(run, rounded, unit)
    before = deepcopy(store.get_run(run['id']))
    view = periods.save_records(run['id'], [record], origin='model')
    current = store.get_run(run['id'])
    assert bool(financial_basis.conflicts(current)) is not compatible
    node = next(n for n in view['analysis']['cumulative'] if n['metric'] == 'n_income_attr_p')
    assert (node['value'] is not None) is compatible
    assert bool(eligibility(deep_research.reference_catalog(current)['yoy-n_income_attr_p'], current)['supports_inference']) is compatible
    if compatible:
        assert valuation.calculate(current, 'pe', {'growth_pct': 0, 'multiple': 20})['output']['equity_value_100m']
        if import_fact:
            assert Decimal(node['value']) == Decimal(exact)
            assert node['precision_reconciliation']['status'] == 'rounding_compatible'
            assert len(node['inputs']) == len(node['source_ids']) == 2
            assert '不代表该来源更准确' in node['formula']
            assert node['source_status'] == 'fact_ledger'
    else:
        with pytest.raises(ValueError, match='同口径金额冲突'):
            valuation.calculate(current, 'pe', {'growth_pct': 0, 'multiple': 20})
    assert current['facts'] == before['facts'] and current['calculations'] == before['calculations']
    assert current['financial_period_records'][-1]['value'] == str(Decimal(rounded))
    assert current['financial_period_records'][-1]['unit'] == unit
    assert 'comparison_precision' not in current['financial_period_records'][0]


def test_imported_thousand_unit_fact_keeps_original_precision_and_external_raw_amount(run):
    _profit_source(run, '100000000', '100000', '千元')
    periods.import_facts(run['id'], ['n_income_attr_p-2025'])
    view = periods.save_records(run['id'], [_rounded_row(run, '100000123.45', '元')], origin='model')
    stored = store.get_run(run['id'])
    before = deepcopy(stored)
    imported, external = view['records']
    assert imported['value'] == '100000000' and imported['unit'] == '元'
    assert imported['comparison_precision'] == {'original_value': '100000', 'original_unit': '千元'}
    node = view['analysis']['cumulative'][0]
    assert node['value'] == '100000123.45'
    assert node['precision_reconciliation']['selected_record_id'] == external['id']
    assert set(node['inputs']) == {imported['id'], external['id']}
    assert not financial_basis.conflicts(stored)
    # Read views do not rewrite the stored unit, numeric value or history.
    assert periods.snapshot(stored)['analysis'] == view['analysis']
    assert store.get_run(run['id']) == before


def test_coarse_disclosure_cannot_bridge_two_precise_conflicting_amounts(run):
    _profit_source(run, '100000100.00')
    periods.import_facts(run['id'], ['n_income_attr_p-2025'])
    periods.save_records(run['id'], [_rounded_row(run, '100000', '千元')], origin='model')
    # Both precise values lie inside the coarse disclosure's 500-yuan band,
    # but disagree with one another far beyond their own 0.005-yuan precision.
    precise = row(value='100000200.00', source_type='user_input', source_id='manual-precise',
                  quote='2025年合并报表归母净利润 100000200.00 元。')
    view = periods.save_records(run['id'], [precise], origin='human')
    assert financial_basis.conflicts(store.get_run(run['id']))
    assert view['analysis']['cumulative'][0]['value'] is None


def test_inconsistent_original_unit_metadata_cannot_expand_tolerance(run):
    current = deepcopy(run)
    fact = next(f for f in current['facts'] if f['id'] == 'n_income_attr_p-2025')
    fact.update(original_value='100000000', original_unit='亿元')
    current['financial_period_records'] = [dict(row(value='100000001'), id='raw', value_base='100000001')]
    assert financial_basis.conflicts(current)


def _calculation_draft(reference, status='supported'):
    item = {'title': '财务变化', 'body': '根据已存财务事实计算同比变化，经营原因仍需核对。',
        'evidence_ids': [reference], 'status': status, 'counter_evidence': '金额与经营原因需要分别核对。',
        'invalidate_if': '底层金额被更正。'}
    return {'title': '财务变化研究', 'summary': '核对财务变化与来源。', 'stance': '继续研究经营原因。',
        'theses': [deepcopy(item), {**item, 'title': '后续验证'}],
        'sections': [{**item, 'key': key} for key in deep_research.SECTION_TITLES],
        'questions': [], 'limitations': []}


@pytest.mark.parametrize('status', ['supported', 'inference'])
def test_conflicted_growth_citation_is_rejected_by_actual_report_validation(run, status):
    before = deepcopy(run)
    periods.save_records(run['id'], [row()], origin='model')
    current = store.get_run(run['id'])
    reference = deep_research.reference_catalog(current)['yoy-n_income_attr_p']
    assert reference['inputs'] == ['n_income_attr_p-2025', 'n_income_attr_p-2024']
    policy = eligibility(reference, current)
    assert policy['basis'] == 'conflict' and policy['can_cite']
    assert not policy['supports_claim'] and not policy['supports_inference']
    with pytest.raises(ValueError, match='冲突'):
        deep_research.validate_memo(_calculation_draft(reference['id'], status), current)
    # Naming the unresolved issue remains allowed, and unrelated revenue is
    # not blocked just because another metric in the research has a conflict.
    assert deep_research.validate_memo(_calculation_draft(reference['id'], 'gap'), current)
    assert deep_research.validate_memo(_calculation_draft('yoy-revenue', status), current)
    assert current['facts'] == before['facts'] and current['calculations'] == before['calculations']


def test_stored_calculation_lineage_wins_over_omitted_or_forged_reference_inputs(run):
    saved = periods.save_records(run['id'], [row()], origin='model')
    current = store.get_run(run['id'])
    current['calculations'].append({'id': 'calc-layer-2', 'inputs': ['calc-layer-3', 'yoy-n_income_attr_p'], 'value': '0'})
    current['calculations'].append({'id': 'calc-layer-3', 'inputs': ['calc-layer-2'], 'value': '0'})
    before = deepcopy(current)
    for sid in ('yoy-n_income_attr_p', 'calc-layer-2', 'calc-layer-3'):
        for inputs in (None, [], ['revenue-2025']):
            ref = {'id': sid, 'kind': 'calculation'}
            if inputs is not None:
                ref['inputs'] = inputs
            assert not eligibility(ref, current)['supports_inference']
    assert current == before
    periods.delete_record(run['id'], saved['records'][0]['id'])
    repaired = store.get_run(run['id'])
    assert eligibility(deep_research.reference_catalog(repaired)['yoy-n_income_attr_p'], repaired)['supports_inference']


def test_derived_inputs_do_not_turn_original_source_fingerprints_into_different_sources(run):
    from app.evidence_policy import source_fingerprint
    import hashlib
    import json
    source = run['research_sources'][0]
    previous_shape = {k: source.get(k) for k in ('id', 'kind', 'text', 'quote', 'published_at', 'announced_date',
        'read_status', 'extraction', 'status', 'value', 'sha256')}
    previous_shape['eligibility'] = eligibility(source, run)
    assert source_fingerprint(source, run) == hashlib.sha256(json.dumps(
        previous_shape, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
    a = {'id': 'external-calculation', 'kind': 'calculation', 'inputs': ['revenue-2025']}
    assert source_fingerprint(a, run) != source_fingerprint({**a, 'inputs': ['revenue-2024']}, run)
