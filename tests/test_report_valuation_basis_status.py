"""Scenario matching cannot make a conflicting financial basis usable."""
from copy import deepcopy

import pytest

from app import deep_research, financial_periods, store, valuation
from app.financial_basis import conflicts
from app.report_state import report_valuation_status
from test_financial_basis import run, row


@pytest.mark.parametrize('metric,method', [('n_income_attr_p', 'pe'), ('revenue', 'ps')])
def test_saved_matching_scenario_blocks_on_actual_ledger_conflict_without_mutation(run, metric, method):
    scenario = valuation.save_result(run['id'], method, {'growth_pct': 0, 'multiple': 20})
    ddm = valuation.save_result(run['id'], 'ddm', {'dividend_per_share': 1,
        'cost_of_equity_pct': 10, 'terminal_growth_pct': 2})
    memo = {'title': '合成报告', 'summary': '历史研究判断', 'sections': [], 'theses': [],
        'sources': [{'kind': 'valuation', 'id': 'valuation-' + scenario['id']}]}
    deep_research.save_memo(run['id'], memo, '隔离测试初稿')
    before = store.get_run(run['id'])
    assert report_valuation_status(before)['state'] == 'current'
    financial_periods.save_records(run['id'], [row(metric)], origin='model')
    current = store.get_run(run['id']); unchanged = deepcopy(current)
    state = report_valuation_status(current)
    assert state['state'] == 'blocked' and state['scenario_match'] == 'current'
    items = {i['method']: i for i in state['items']}
    assert items[method]['state'] == 'blocked'
    assert items[method]['blocking_issues'][0]['metric'] == metric
    assert items['ddm']['state'] == 'current' and items['ddm']['blocking_issues'] == []
    assert items['ddm']['latest_id'] == ddm['id']
    assert '财务基准存在冲突' in state['message']
    assert current == unchanged == store.get_run(run['id'])
    assert current['memo'] == before['memo'] and current['valuation_history'] == before['valuation_history']
    financial_periods.delete_record(run['id'], current['financial_period_records'][0]['id'])
    assert report_valuation_status(store.get_run(run['id']))['state'] == 'current'


def synthetic_run(*, method='pe', scenario_year=2025, conflict_year=2025, linking='fact'):
    metric = {'pe': 'n_income_attr_p', 'ps': 'revenue'}.get(method)
    scenario = {'id': 'synthetic-' + method, 'method': method, 'assumptions': {'multiple': '20'},
        'fact_ids': [f'{metric}-{scenario_year}'] if linking in ('fact', 'missing_fact') else [],
        'output': {'equity_value_100m': '20'}, 'formula': 'synthetic'}
    if linking == 'explicit': scenario['base_year'] = scenario_year
    fact = {'id': 'n_income_attr_p-' + str(conflict_year), 'metric': 'n_income_attr_p',
        'year': conflict_year, 'period_type': 'annual', 'currency': 'CNY', 'scope': 'consolidated', 'value': '100000000'}
    facts = [fact]
    if linking == 'fact' and scenario['fact_ids'][0] != fact['id']:
        facts.append({**fact, 'id': scenario['fact_ids'][0], 'metric': metric, 'year': scenario_year})
    return {'request': {'year': 2025}, 'facts': facts,
        'financial_period_records': [{'id': 'conflicting-period', 'metric': 'n_income_attr_p',
            'year': conflict_year, 'period': 'FY', 'period_type': 'cumulative', 'currency': 'CNY',
            'scope': 'consolidated', 'value_base': '120000000'}],
        'valuation_history': [scenario], 'memo': {'valuation_context': {'schema_version': 1,
            'scenarios': [deepcopy(scenario)], 'referenced_ids': [scenario['id']]}}}


@pytest.mark.parametrize('method,year,linking', [
    ('ps', 2025, 'fact'), ('pe', 2024, 'fact'), ('pe', 2024, 'missing_fact'),
    ('pe', 2024, 'explicit'), ('ddm', 2025, 'none'), ('pb', 2025, 'none'),
    ('dcf', 2025, 'none'), ('ev_ebitda', 2025, 'none'),
])
def test_unrelated_metric_year_and_user_input_methods_are_not_blocked(method, year, linking):
    run = synthetic_run(method=method, scenario_year=year, linking=linking)
    assert len(conflicts(run)) == 1
    before = deepcopy(run)
    state = report_valuation_status(run)
    assert state['state'] == 'current' and state['items'][0]['state'] == 'current'
    assert run == before


def test_reported_older_basis_is_checked_independently_of_newer_scenario_year():
    run = synthetic_run(scenario_year=2024, conflict_year=2024)
    current = deepcopy(run['valuation_history'][0])
    current.update(id='new-2025', fact_ids=['n_income_attr_p-2025'])
    run['facts'].append({**run['facts'][0], 'id': 'n_income_attr_p-2025', 'year': 2025})
    run['valuation_history'].append(current)
    state = report_valuation_status(run)
    assert state['state'] == 'blocked' and state['scenario_match'] == 'changed'
    assert state['items'][0]['latest_id'] == 'new-2025'
    assert state['items'][0]['blocking_issues'][0]['year'] == 2024


def test_legacy_scenario_without_year_linkage_uses_research_base_year():
    state = report_valuation_status(synthetic_run(linking='none'))
    assert state['state'] == 'blocked' and state['scenario_match'] == 'current'
