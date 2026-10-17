"""Keep calculated annual answers when unavailable quarter rows fill the ledger."""
from copy import deepcopy

import pytest

from app import agent_context as ctx, financial_periods as periods


def observation():
    values = {
        'revenue': ('115393310976.69', '115636231250.05'),
        'n_income_attr_p': ('8452859993.18', '11565166497.81'),
        'n_cashflow_act': ('21739740393.38', '14343920490.32'),
    }
    records = []
    for metric, amounts in values.items():
        for year, value in zip((2024, 2025), amounts):
            source_id = f'fact-ledger-{metric}-{year}'
            records.append({'id': f'fp-{metric}-{year}', 'metric': metric, 'label': periods.METRICS[metric][0],
                'year': year, 'quarter': 4, 'period': 'FY', 'period_type': 'cumulative', 'kind': 'flow',
                'value': value, 'value_base': value, 'unit': '元', 'currency': 'CNY', 'scope': 'consolidated',
                'comparability_key': 'reported', 'restatement': 'original', 'source_status': 'fact_ledger',
                'source_id': source_id, 'original_source_ids': [f'api-{year}'], 'source_type': 'existing',
                'source_status_label': '已存事实复用 · 保留原核验状态',
                'quote': f'已保存年度财务事实 {metric}-{year}；金额 {value} 元；接口单源。' + '事实账本映射不是PDF摘录。' * 20,
                'announced_date': f'{year + 1}-04-01'})
    return {'artifact': 'financial_periods', 'run_id': 'synthetic-period-projection', 'records': records,
            'analysis': periods.analyze(records), 'operation': 'analyzed', 'note': periods.NOTE}


@pytest.mark.parametrize('tool', ['record_financial_periods', 'analyze_financial_periods'])
def test_real_six_fact_shape_keeps_all_three_annual_comparisons(tool):
    raw = observation(); before = deepcopy(raw)
    expected = {row['metric']: row for row in raw['analysis']['comparisons'] if row['value'] is not None}
    assert len(raw['analysis']['comparisons']) == 18 and len(expected) == 3
    first = ctx.compact_tool_results([{'tool': tool, 'result': raw}])
    second = ctx.compact_tool_results(first, max_chars=500)
    assert first == second and raw == before
    result = second[0]['result']
    rows = [r for r in result['analysis']['comparisons'] if r['value'] is not None]
    assert {r['metric'] for r in rows} == set(expected)
    for row in rows:
        for key in ('metric', 'year', 'current', 'previous', 'value', 'change', 'formula', 'inputs', 'source_status'):
            assert row[key] == expected[row['metric']][key]
        assert row['unit'] == '元' and row['value_unit'] == '%'
        assert {f'api-2024', f'api-2025'} <= set(row['source_ids'])
    assert result['record_count'] == 6 and result['analysis_counts']['comparisons'] == 18
    assert result['comparison_counts'] == {'calculated': 3, 'shown_calculated': 3, 'omitted_calculated': 0}
    assert ctx.size(result) < 9000


def test_final_payload_retains_exact_revenue_despite_other_income_text():
    raw = observation()
    payload = {'tool_results': [{'tool': 'analyze_financial_periods', 'result': raw},
        {'tool': 'read_document', 'result': {'artifact': 'document_read', 'snippets': [
            {'document_id': 'synthetic-pdf', 'page': 1, 'text': '主营收入1145.45亿元、1143.02亿元。' * 100}]}}],
        'conversation': [], 'context': {'memo': {'summary': '长旧稿' * 5000}}, 'research_plan': {}}
    result = ctx.compact_payload(payload, max_chars=7000)
    kept = next(r['result'] for r in result['tool_results'] if r.get('tool') == 'analyze_financial_periods')
    revenue = next(r for r in kept['analysis']['comparisons'] if r['metric'] == 'revenue' and r['value'] is not None)
    assert revenue['current'] == '115636231250.05'
    assert revenue['previous'] == '115393310976.69'
    assert kept['comparison_counts']['shown_calculated'] == 3


def test_more_than_twelve_calculated_comparisons_exposes_omission_and_preserves_latest():
    raw = observation(); records = []
    for year in range(2019, 2026):
        for template in raw['records'][1::2]:
            row = deepcopy(template)
            row.update(id=f"fp-{row['metric']}-{year}", year=year,
                source_id=f"fact-ledger-{row['metric']}-{year}", original_source_ids=[f'api-{year}'])
            records.append(row)
    raw['records'] = records; raw['analysis'] = periods.analyze(records)
    kept = ctx.compact_tool_results([{'tool': 'analyze_financial_periods', 'result': raw}])[0]['result']
    assert kept['comparison_counts'] == {'calculated': 18, 'shown_calculated': 12, 'omitted_calculated': 6}
    calculated = [r for r in kept['analysis']['comparisons'] if r['value'] is not None]
    assert len(calculated) == 12 and {r['year'] for r in calculated} == {2022, 2023, 2024, 2025}
    assert all(r['value'] == '0' for r in calculated)
    assert ctx.size(kept) < 16000
    assert ctx.compact_tool_results([{'tool': 'analyze_financial_periods', 'result': kept}])[0]['result'] == kept


def test_blocked_sources_never_become_calculated_comparisons():
    raw = observation()
    for row in raw['records']: row['source_status'] = 'source_changed'
    raw['analysis'] = periods.analyze(raw['records'])
    kept = ctx.compact_tool_results([{'tool': 'analyze_financial_periods', 'result': raw}])[0]['result']
    assert kept['comparison_counts']['calculated'] == 0
    assert all(row['value'] is None and row['change'] is None for row in kept['analysis']['comparisons'])
