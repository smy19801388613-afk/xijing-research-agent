from copy import deepcopy
from decimal import Decimal

import pytest

from app.finance import compute, comparison_check, comparison_view


def facts(previous, current):
    return [{'id':f'{metric}-{year}', 'metric':metric, 'year':year, 'value':str(value), 'status':'api_only'}
            for metric in ('revenue','n_income_attr_p','n_cashflow_act')
            for year,value in ((2024,previous),(2025,current))]


@pytest.mark.parametrize('previous,current,change', [
    ('-2061309357','-1389222622','672086735'),
    ('-273649390','812386051','1086035441'),
    ('0','0','0'), ('0','-200','-200'), ('-10','-20','-10'),
])
def test_amount_change_is_a_valid_comparison(previous,current,change):
    calculations=compute(facts(previous,current),2025)
    assert comparison_check(calculations)['passed']
    for c in calculations:
        assert c['value'] is None and Decimal(c['change']) == Decimal(change)
        assert c['formula']=='本期值 - 上期值' and c['unit']=='元'


def test_positive_baseline_keeps_ratio():
    calculations=compute(facts('100','120'),2025)
    assert comparison_check(calculations)['passed']
    assert all(Decimal(c['value'])==20 and c['unit']=='%' for c in calculations)


def test_missing_conflicting_and_nonfinite_results_still_fail():
    for state in ('missing','conflict'):
        values=facts('100','120')
        values[0].update(status=state,value=None if state=='missing' else '100')
        assert not comparison_check(compute(values,2025))['passed']
    assert not comparison_check([])['passed']
    assert not comparison_check([{'value':'NaN','change':None}])['passed']


def test_legacy_view_corrects_metadata_without_mutating_history():
    calculations=compute(facts('-100','-50'),2025)
    for c in calculations: c.update(unit='%',formula='(本期值 / 上期值 - 1) × 100')
    run={'calculations':calculations,'checks':[{'kind':'calculation','label':'核心同比计算','passed':False}],
         'gaps':[{'code':c['id'],'reason':c['reason']} for c in calculations]+[{'code':'unrelated'}]}
    original=deepcopy(run)
    view=comparison_view(run)
    assert run==original
    assert view['checks'][0]['passed']
    assert all(c['unit']=='元' and c['formula']=='本期值 - 上期值' for c in view['calculations'])
    assert view['gaps']==[{'code':'unrelated'}]


def test_amount_comparisons_remain_citable_in_deep_report():
    from app.deep_research import reference_catalog
    calculations = compute(facts('-100', '-50'), 2025)
    calculations.append({'id': 'missing', 'value': None, 'change': None})
    refs = reference_catalog({'facts': [], 'calculations': calculations})
    assert 'missing' not in refs
    assert all(refs[c['id']]['kind'] == 'calculation' for c in calculations[:-1])
    assert not reference_catalog({'facts': [], 'calculations': [{'value': None, 'change': '50'}]})
