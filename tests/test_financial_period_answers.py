"""Answer/period alignment uses deterministic in-memory snapshots only."""
import copy
from decimal import Decimal

import pytest

from app import financial_period_answers as answers, financial_periods


def record(metric='revenue', year=2025, value='115636231250.05', **overrides):
    return {'id': f'{metric}-{year}', 'metric': metric, 'label': financial_periods.METRICS[metric][0],
            'kind': financial_periods.METRICS[metric][1], 'year': year, 'quarter': 4,
            'period_type': 'point' if financial_periods.METRICS[metric][1] == 'balance' else 'cumulative',
            'value_base': value, 'currency': 'CNY', 'scope': 'consolidated',
            'comparability_key': 'reported', 'restatement': 'original',
            'source_status': 'fact_ledger', 'source_id': f'fact-ledger-{metric}-{year}',
            'imported_fact_verification': {'label': '接口单源'}, **overrides}


def results(rows=None, **overrides):
    rows = rows if rows is not None else [record(), record(year=2024, value='115393310976.69'),
        record('n_income_attr_p', value='11565166497.81'), record('n_income_attr_p', 2024, '8452859993.18'),
        record('n_cashflow_act', value='14343920490.32'), record('n_cashflow_act', 2024, '21739740393.38')]
    value = {'artifact': 'financial_periods', 'run_id': 'synthetic-run', 'records': rows,
             'analysis': financial_periods.analyze(rows), **overrides}
    return [{'tool': 'analyze_financial_periods', 'result': value}]


def test_exact_live_answer_revenue_mismatch_not_cash_profit_or_changes():
    message = '''财务期间底稿已完成。
- 经营现金流净额：2025 年 143.44 亿元，较 2024 年 217.40 亿元下降 34.02%，减少 73.96 亿元。
- 归母净利润：2025 年 115.65 亿元，较 2024 年 84.53 亿元增长 36.82%，增加 31.12 亿元。
- 收入：2025 年 1,145.45 亿元，较 2024 年 1,143.02 亿元增长 0.21%，增加 2.43 亿元。'''
    issues = answers.findings(message, results())
    assert [(i['metric'], i['year'], i['expected']) for i in issues] == [
        ('revenue', 2025, '1156.36'), ('revenue', 2024, '1153.93')]
    assert issues[0]['source_ids'] == ['fact-ledger-revenue-2025']
    assert '115636231250.05元' in answers.feedback(message, results())
    assert '主营' in answers.feedback(message, results())


@pytest.mark.parametrize('text', [
    '营业收入：2025年1156.36亿元，2024年1153.93亿元。',
    '2025年营业收入约1156亿元。',
    '2025年营业收入1156.3623亿元。',
    '2025年营业收入115,636,231,250.05元。',
    '2025年营业收入115，636，231，250.05元。',
    '2025年营收115636231.25千元。',
    '2025年收入11563623.13万元。',
    '2025年营业收入115636.23百万元。',
    '2025年营业收入0.115636万亿元。',
    '**营业收入**：2025 年 **1,156.36 亿元**。',
    '2025年营业收入1156.36亿元，归母净利润115.65亿元。',
    '经营现金流净额2025年143.44亿元。',
    '经营活动产生的现金流量净额2025年143.44亿元。',
    '归属于上市公司股东的净利润2025年115.65亿元。',
    '归属于母公司所有者的净利润2025年115.65亿元。',
])
def test_valid_conversion_rounding_and_explicit_aliases(text):
    assert answers.feedback(text, results()) == ''


@pytest.mark.parametrize('text', [
    '2025年主营业务收入1145.45亿元。',
    '主营收入：2025年1145.45亿元，较2024年1143.02亿元增加。',
    '2025年营业总收入1159.31亿元。',
    '2025年分部收入1145.45亿元。',
    '2025年分产品营业收入1145.45亿元。',
    '2025年母公司营业收入1048.99亿元。',
    '母公司口径：2025年营业收入1048.99亿元。',
    '母公司2025年营业收入1048.99亿元。',
    '2025年收入1145.45亿元（主营业务口径），2024年收入1143.02亿元（主营业务口径）。',
    '2025年营业收入1200亿元（预测值）。',
    '2025年营业收入500亿元（上半年）。',
    '2025年境外收入50亿元。',
    '2025年液体乳业务收入704亿元。',
    '2025年归母净利润预计120亿元。',
    '假设2025年营业收入1200亿元。',
    '2025年收入增加2.43亿元。',
    '2025年收入同比减少2.43亿元。',
    '2025年营业收入增长额2.43亿元。',
    '2025年营业收入与归母净利润分别为1156.36亿元、115.65亿元。',
    '2025年归母净利润不是110亿元。',
    '原答复误写收入2025年1145.45亿元。',
    '2025年营业收入1156.36亿元，归母净利润与扣非归母净利润不同。',
    '2025年营业收入1156.36亿元。随后指出收入50亿元。',
    '2025年营业收入1156.36亿元\n收入50亿元。',
    '2025年营业收入1156.36亿元，每股现金流2元/股。',
])
def test_different_scope_forecast_calculations_and_ambiguity_are_not_relabelled(text):
    assert answers.feedback(text, results()) == ''


@pytest.mark.parametrize('period', ['上半年', '前三季度', 'Q3', 'Q4单季', '第一季度', '下半年',
                                    '1-9月', 'TTM', '第二季度累计', '近12月'])
def test_different_period_cannot_match_annual_amount(period):
    assert answers.feedback(f'2025年{period}营业收入500亿元。', results()) == ''


def test_explicit_h1_and_single_quarter_match_only_corresponding_node():
    data = results([record(quarter=2, value='10000000000'),
                    record(quarter=2, period_type='single', value='4000000000')])
    assert answers.feedback('2025年上半年营业收入100亿元。', data) == ''
    assert answers.feedback('2025年Q2单季营业收入40亿元。', data) == ''
    assert answers.findings('2025年上半年营业收入90亿元。', data)[0]['expected'] == '100'
    assert answers.findings('2025年Q2单季营业收入50亿元。', data)[0]['expected'] == '40'
    assert answers.feedback('2025年Q2营业收入90亿元。', data) == ''


def test_negative_zero_and_half_up_rounding():
    data = results([record('n_income_attr_p', value='-125000000')])
    assert answers.feedback('归母净利润2025年-1.3亿元。', data) == ''
    assert answers.findings('归母净利润2025年1.3亿元。', data)
    assert answers.findings('归母净利润2025年0亿元。', data)


def test_no_implicit_read_of_prior_turn_or_other_tool():
    row = results()[0]
    text = '2025年收入1000亿元。'
    for data in ([], [row['result']], [{**row, 'tool': 'record_financial_periods'}],
                 [{'validation_feedback': row}], [{'tool': row['tool'], 'result': {'error': 'unavailable'}}]):
        assert answers.feedback(text, data) == ''
        assert answers.deterministic_summary(data) == ''


@pytest.mark.parametrize('overrides', [
    {'scope': 'parent'}, {'currency': 'USD'}, {'source_status': 'source_changed'},
    {'restatement': 'restated'}, {'comparability_key': 'new-gaap'},
])
def test_incompatible_or_unusable_bases_do_not_drive_assertion(overrides):
    data = results([record(**overrides)])
    assert answers.feedback('2025年营业收入999亿元。', data) == ''


def test_conflicts_and_competing_restatement_do_not_choose_convenient_value():
    for rows in ([record(), record(value='100')], [record(), record(value='100', restatement='restated')]):
        assert answers.feedback('2025年营业收入999亿元。', results(rows)) == ''


def test_last_observed_analysis_replaces_old_snapshot_but_other_run_is_ambiguous():
    old = results([record(value='100000000')])
    new = results([record(value='200000000')])
    assert answers.feedback('2025年营业收入2亿元。', old + new) == ''
    assert answers.findings('2025年营业收入1亿元。', old + new)
    another = results([record(value='200000000')], run_id='another-company')
    assert answers.feedback('2025年营业收入999亿元。', old + another) == ''


def test_independent_metric_does_not_inherit_earlier_main_business_scope():
    issues = answers.findings('2025年主营收入1145.45亿元，2025年营业收入1145.45亿元。', results())
    assert len(issues) == 1 and issues[0]['metric'] == 'revenue'


def test_snapshot_never_mutated_and_fallback_preserves_provenance_status_and_gaps():
    data = results()
    before = copy.deepcopy(data)
    summary = answers.deterministic_summary(data)
    assert '115636231250.05元（1156.36亿元）' in summary
    assert '115393310976.69元（1153.93亿元）' in summary
    assert 'fact-ledger-revenue-2025' in summary
    assert '接口单源' in summary and '不提升' in summary
    assert '无法拆分单季' in summary
    assert answers.feedback(summary, data) == ''
    assert data == before


def test_balance_amount_is_point_in_time_not_annual_flow():
    data = results([record('total_assets', value='100000000000')])
    assert answers.feedback('2025年末总资产1000亿元。', data) == ''
    assert answers.findings('2025年末总资产900亿元。', data)[0]['expected'] == '1000'


def test_missing_derived_periods_are_not_invented_in_fallback():
    text = answers.deterministic_summary(results())
    assert 'Q4单季营业收入' not in text
    assert '2025年全年营业收入' in text
