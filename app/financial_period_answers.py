"""Narrow final-answer checks against this turn's financial-period analysis.

This checks transcription of labelled amounts, not the accounting correctness
of a source. It never reads the run, changes a report, or upgrades verification.
Only unambiguous CNY consolidated observations from analyze_financial_periods
are used. Forecasts, segment figures, ambiguous periods and cross-company
answers are deliberately left to research review.
"""
from collections import defaultdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re

from .financial_units import AMOUNT, UNITS, number


_LABELS = {
    'revenue': '营业收入', 'n_income_attr_p': '归母净利润',
    'deducted_profit': '扣非归母净利润', 'n_cashflow_act': '经营现金流净额',
    'accounts_receivable': '应收账款', 'inventory': '存货',
    'total_assets': '总资产', 'total_liabilities': '总负债',
}
_BALANCES = {'accounts_receivable', 'inventory', 'total_assets', 'total_liabilities'}
_METRIC = re.compile(
    r'(?P<other>营业总收入|主营(?:业务)?收入|其他业务收入|扣非净利润|营业利润|净利润总额)'
    r'|(?P<deducted_profit>扣(?:除)?非(?:经常性损益)?(?:后的?)?归母净利润)'
    r'|(?P<n_income_attr_p>归母净利润|归属于(?:上市公司|母公司)(?:股东|所有者)的?净利润)'
    r'|(?P<n_cashflow_act>经营活动(?:产生的)?现金流量净额|经营现金流(?:量)?(?:净额)?)'
    r'|(?P<revenue>营业收入|营收|收入)'
    r'|(?P<accounts_receivable>应收账款)|(?P<inventory>存货)'
    r'|(?P<total_assets>资产总(?:额|计)|总资产)|(?P<total_liabilities>负债总(?:额|计)|总负债)')
_YEAR = re.compile(r'(?<!\d)(20\d{2})\s*年?')
_OTHER_SCOPE = re.compile(
    r'母公司(?!的?(?:股东|所有者))'
    r'|主营|分部|分产品|分地区|子公司|单体|境内|境外|海外|其他业务|营业总收入'
    r'|同行|可比公司|其他公司|另一公司|业务收入|产品收入')
_NON_FACT = re.compile(r'假设|预测|预计|估计|情景|目标|未来|测算|推算|假定|如果|若|预算|计划'
                       r'|误(?:写|称|用)|错误|原答复|并非|不是|而非|不等于|不能写为')
_DELTA = re.compile(r'差额|增量|增长额|增加额|减少额|变动额|(?:增加|减少|下降|增长)(?!至|到)')


def _snapshot(results):
    """Latest observed analysis per run; multiple companies are not inferred."""
    latest = {}
    for row in results or []:
        if not isinstance(row, dict) or row.get('tool') != 'analyze_financial_periods':
            continue
        result = row.get('result')
        if (not isinstance(result, dict) or result.get('artifact') != 'financial_periods'
                or not isinstance(result.get('analysis'), dict) or result.get('error')):
            continue
        latest[str(result.get('run_id') or '')] = result
    return next(iter(latest.values())) if len(latest) == 1 else None


def _key(node):
    return node.get('metric'), node.get('year'), node.get('quarter'), node.get('period_type')


def _observations(results):
    snapshot = _snapshot(results)
    if not snapshot:
        return {}, None
    grouped = defaultdict(list)
    analysis = snapshot['analysis']
    for category in ('cumulative', 'quarters', 'balances'):
        for node in analysis.get(category, []):
            if (not isinstance(node, dict) or node.get('metric') not in _LABELS
                    or node.get('currency') != 'CNY' or node.get('scope') != 'consolidated'
                    or node.get('unit') != '元'):
                continue
            grouped[_key(node)].append(node)
    observations = {}
    for key, nodes in grouped.items():
        # Never choose a convenient amount from conflicting/changed evidence,
        # or guess which restatement/comparability basis prose intended.
        if any(n.get('reason') or n.get('value') is None or n.get('source_status') == 'source_changed'
               or n.get('restatement') != 'original' or n.get('comparability_key') != 'reported'
               for n in nodes):
            continue
        try:
            values = {Decimal(str(n['value'])) for n in nodes}
        except (InvalidOperation, ValueError):
            continue
        if len(values) != 1 or not all(v.is_finite() for v in values):
            continue
        observations[key] = nodes[0]
    return observations, snapshot


def _period(context, metric):
    """No default from an explicit but unsupported period to an annual value."""
    if re.search(r'TTM|滚动|下半年|近\s*\d+\s*月|[1一]至[123456789一二三四五六七八九]月', context, re.I):
        return None
    if re.search(r'上半年|半年(?:度)?|H1|[1一][—–~～-]6月', context, re.I):
        return 2, 'point' if metric in _BALANCES else 'cumulative'
    if re.search(r'前三季|首三季|9M|[1一][—–~～-]9月', context, re.I):
        return 3, 'point' if metric in _BALANCES else 'cumulative'
    match = re.search(r'Q([1-4])|第?([一二三四1234])季', context, re.I)
    if match:
        token = match.group(1) or match.group(2)
        quarter = int(token) if token.isdigit() else '一二三四'.index(token) + 1
        if metric in _BALANCES:
            return quarter, 'point'
        if re.search(r'单季|当季|本季', context):
            return quarter, 'single'
        if re.search(r'累计|截至', context) or quarter == 1:
            return quarter, 'cumulative'
        return None  # Q2/Q3/Q4 alone may mean a single or cumulative period.
    if re.search(r'季度|单季|[月季]|\d\s*[—–~～-]\s*\d', context):
        return None
    return 4, 'point' if metric in _BALANCES else 'cumulative'


def _claims(message):
    # A heading's metric may bind two comparison-year amounts within its line,
    # but never leaks to another sentence/list item or an unlabelled calculation.
    message = str(message or '').replace('**', '').replace('`', '')
    for sentence in re.split(r'[。；;\n]', message):
        previous_end = 0
        inherited_metric = None
        inherited_context = ''
        inherited_year = None
        for match in AMOUNT.finditer(sentence):
            local = sentence[previous_end:match.start()]
            previous_end = match.end()
            metric_matches = list(_METRIC.finditer(local))
            years = list(_YEAR.finditer(local))
            year = int(years[-1].group(1)) if years else inherited_year
            if years:
                inherited_year = year
            if len(metric_matches) > 1:
                inherited_metric = None  # "收入和归母净利润分别为" is ambiguous.
                continue
            if metric_matches:
                metric = metric_matches[0].lastgroup
                # Scope labels in an earlier comma-delimited discussion must
                # not suppress a later explicitly labelled total observation.
                start = metric_matches[0].start()
                boundary = max(local.rfind('，', 0, start), local.rfind(',', 0, start))
                context = local[boundary + 1:]
                inherited_metric, inherited_context = metric, context
            else:
                metric, context = inherited_metric, inherited_context + local
                # Inheritance is only for an explicitly dated comparative
                # amount, never "增加 2.43 亿元" or an unrelated trailing value.
                if not years or len(local) > 70:
                    continue
            # Explicit scope/period notes immediately after an amount qualify
            # that amount too: "收入...亿元（主营口径/上半年）" is not FY revenue.
            suffix = re.match(r'\s*[（(]([^）)]{0,60})[）)]', sentence[match.end():])
            if suffix:
                context += suffix.group(1)
                inherited_context = context
            if (metric not in _LABELS or year is None or _OTHER_SCOPE.search(context)
                    or _NON_FACT.search(context) or _DELTA.search(local)):
                continue
            period = _period(context, metric)
            if period:
                yield metric, year, period, match.group(1), match.group(2)


def findings(message, results):
    """Return mismatches only for explicit amounts with one compatible basis."""
    observed, _ = _observations(results)
    issues = []
    for metric, year, (quarter, typ), raw, unit in _claims(message):
        node = observed.get((metric, year, quarter, typ))
        if not node:
            continue
        shown = number(raw)
        precision = Decimal(1).scaleb(-len(raw.split('.')[1])) if '.' in raw else Decimal(1)
        expected = Decimal(node['value']) / UNITS[unit]
        try:
            equal = expected.quantize(precision, rounding=ROUND_HALF_UP) == shown
        except InvalidOperation:
            continue
        if not equal:
            issues.append({'metric': metric, 'label': _LABELS[metric], 'year': year,
                           'period': node.get('period_label'), 'claimed': raw, 'unit': unit,
                           'expected': format(expected.quantize(precision, rounding=ROUND_HALF_UP), 'f'),
                           'value_base': node['value'], 'source_ids': list(node.get('source_ids', [])),
                           'record_ids': list(node.get('inputs', []))})
    return issues


def feedback(message, results):
    """Empty means this narrow check found no mismatch, not full verification."""
    issues = findings(message, results)
    if not issues:
        return ''
    details = []
    for issue in issues[:8]:
        details.append(f"{issue['period']}{issue['label']}写为{issue['claimed']}{issue['unit']}，"
                       f"本轮合并人民币期间底稿为{issue['value_base']}元"
                       f"（按该精度应为{issue['expected']}{issue['unit']}）")
    return '；'.join(details) + '。请区分营业收入与主营、分部及母公司收入，保留本轮底稿的原核验状态。'


def deterministic_summary(results):
    """Small auditable fallback from observed analysis; no model or state I/O."""
    observed, snapshot = _observations(results)
    if not observed or not snapshot:
        return ''
    records = {r.get('id'): r for r in snapshot.get('records', []) if isinstance(r, dict)}
    lines = ['本轮财务期间分析底稿（合并报表、人民币；以下直接取自工具结果）：']
    ordered = sorted(observed.values(), key=lambda n: (n['metric'], n['year'], n['quarter'], n['period_type']))
    for node in ordered[:24]:
        statuses = set()
        for rid in node.get('inputs', []):
            row = records.get(rid, {})
            label = (row.get('imported_fact_verification') or {}).get('label')
            statuses.add(label or row.get('source_status_label') or row.get('source_status') or node.get('source_status', '待核验'))
        rounded = (Decimal(node['value']) / UNITS['亿元']).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        source_ids = '、'.join(node.get('source_ids', [])) or '见期间底稿'
        state = '、'.join(sorted(statuses)) or node.get('source_status', '待核验')
        lines.append(f"- {node['period_label']}{_LABELS[node['metric']]}：{node['value']}元"
                     f"（{rounded}亿元）；来源：{source_ids}；核验状态：{state}。")
    if len(ordered) > 24:
        lines.append('其余期间请查看已保存财务期间底稿。')
    blocked = [n for key in ('quarters', 'cumulative', 'balances')
               for n in snapshot['analysis'].get(key, []) if n.get('reason')]
    if blocked:
        reasons = list(dict.fromkeys(n['reason'] for n in blocked))
        lines.append('无法计算的期间：' + '；'.join(reasons[:4]) + '。')
    lines.append('这是已取得底稿的确定性摘录，不提升原始来源可信度，也不表示已解释变化原因；已保存报告不受影响。')
    return '\n'.join(lines)
