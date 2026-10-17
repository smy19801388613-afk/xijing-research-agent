"""Read-only reconciliation of annual facts and the active period ledger.

Neither ledger silently replaces the other. A disagreement on the same
reported annual basis must be resolved before its value supports calculations.
"""
from collections import defaultdict
from decimal import Decimal
from itertools import combinations

from .finance import METRICS, number
from .financial_units import UNITS


def amount_tolerance(value, original_value, original_unit):
    """Half a displayed unit, retaining decimal precision from the source.

    This is the existing unit-reconciliation rule (e.g. 500 yuan for integer
    thousands), narrowed when the source actually provides decimal places.
    Inconsistent metadata cannot make an amount more tolerant.
    """
    original = number(original_value)
    multiplier = UNITS.get(original_unit)
    if original is None or multiplier is None or original * multiplier != number(value):
        return Decimal(0)
    step = Decimal((0, (1,), min(0, original.as_tuple().exponent)))
    return step * multiplier / 2


def fact_tolerance(fact):
    return amount_tolerance(fact.get('value'), fact.get('original_value', fact.get('value')),
                            fact.get('original_unit', '元'))


def period_tolerance(row, fact_by_id=None):
    imported = (fact_by_id or {}).get(row.get('imported_fact_id'))
    if imported and number(row.get('value_base')) == number(imported.get('value')):
        # Importing a rounded disclosure as yuan is a copy, not extra precision.
        return fact_tolerance(imported)
    precision = row.get('comparison_precision', {}) if row.get('imported_fact_id') else {}
    return amount_tolerance(row.get('value_base'), precision.get('original_value', row.get('value')),
                            precision.get('original_unit', row.get('unit')))


def compatible_amounts(amounts):
    """Compare every pair; a coarse third record must not bridge a conflict."""
    return all(abs(left[0] - right[0]) <= max(left[1], right[1])
               for left, right in combinations(amounts, 2))


def conflicts(run):
    facts = defaultdict(list)
    periods = defaultdict(list)
    fact_by_id = {f.get('id'): f for f in run.get('facts', [])}
    for fact in run.get('facts', []):
        if (fact.get('period_type') == 'annual' and fact.get('currency') == 'CNY'
                and fact.get('scope') == 'consolidated'
                and fact.get('restatement', 'original') == 'original'
                and fact.get('comparability_key', 'reported') == 'reported'
                and number(fact.get('value')) is not None):
            facts[(fact.get('metric'), fact.get('year'))].append(fact)
    for row in run.get('financial_period_records', []):
        imported = fact_by_id.get(row.get('imported_fact_id'))
        if imported and number(row.get('value_base')) != number(imported.get('value')):
            # An old deterministic import is the same source's old version,
            # not independent contradictory evidence. Normal source validation
            # blocks that period record until import_facts refreshes it.
            continue
        if (row.get('period') in ('FY', 'Q4') and row.get('period_type') == 'cumulative'
                and row.get('currency') == 'CNY' and row.get('scope') == 'consolidated'
                and row.get('restatement', 'original') == 'original'
                and row.get('comparability_key', 'reported') == 'reported'
                and number(row.get('value_base')) is not None):
            periods[(row.get('metric'), row.get('year'))].append(row)
    result = []
    for key in sorted(facts.keys() & periods.keys()):
        original = facts[key]
        rows = periods[key]
        amounts = [(number(f['value']), fact_tolerance(f)) for f in original]
        amounts += [(number(r['value_base']), period_tolerance(r, fact_by_id)) for r in rows]
        if compatible_amounts(amounts):
            continue
        metric, year = key
        label = METRICS.get(metric, {}).get('label', metric)
        result.append({'code': 'annual_basis_conflict', 'metric': metric, 'year': year,
            'currency': 'CNY', 'scope': 'consolidated',
            'fact_ids': [f['id'] for f in original], 'record_ids': [r['id'] for r in rows],
            'fact_values': [str(number(f['value'])) for f in original],
            'period_values': [str(number(r['value_base'])) for r in rows],
            'message': f'{year} 年{label}的年度事实与财务期间底稿存在同口径金额冲突；请核对来源、修正或移除错误记录后再计算，系统不会自动选择其中一个数值。'})
    return result


def assert_usable(run, metric, year):
    issues = [i for i in conflicts(run) if i['metric'] == metric and i['year'] == year]
    if issues:
        raise ValueError('；'.join(i['message'] for i in issues))
