"""Shared, bounded research inputs. Extension sources are derived, never facts."""
from copy import deepcopy
import hashlib
import json
from decimal import Decimal


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def sources(run):
    # validated_records obtains the catalog with extension collections removed,
    # so current source validation cannot recurse back into this function.
    from .financial_periods import analyze, validated_records
    from .valuation_analysis import summary
    refs = {}
    records = validated_records(run) if run.get('financial_period_records') else []
    originals = {s['id']: s for s in run.get('research_sources', []) + run.get('evidence', []) + run.get('facts', []) if s.get('id')}
    def add(sid, title, body, stale=False):
        refs[sid] = {'id': sid, 'kind': 'calculation', 'title': title,
            'text': json.dumps(body, ensure_ascii=False, default=str), 'inference_only': True,
            'read_status': 'read', 'status': 'conflict' if stale else 'derived',
            'provenance_note': '程序派生分析；引用不代表输入及假设已核验，不能作为独立事实或独立证据。'}
    if records:
        semantic = [{k: v for k, v in r.items() if k not in ('history', 'recorded_at')} for r in records]
        inputs = {r['source_id']: _hash(originals.get(r['source_id'])) for r in records if r.get('source_type') == 'existing'}
        add('period-analysis-' + _hash(semantic)[:16], '财务期间分析（派生）',
            {'records': semantic, 'analysis': analyze(records), 'current_input_fingerprints': inputs,
             'note': '期间、单位和原文匹配不等于数字已核验；需结合原文逐项复核。'},
            any(r.get('source_status')=='source_changed' for r in records))
    view = summary(run) if run.get('valuation_sensitivity_history') or run.get('comparable_analyses') else {}
    for s in view.get('sensitivities', []):
        add(s['id'], '估值敏感性情景（派生）', {k: s.get(k) for k in
            ('method', 'base_scenario_id', 'base_fingerprint', 'axes', 'fixed_assumptions', 'envelope', 'valid_cells', 'invalid_cells', 'notes', 'stale')}, s.get('stale', False))
    for s in view.get('comparables', []):
        add(s['id'], '可比倍数依据（未核验输入）', {k: s.get(k) for k in
            ('method', 'period_kind', 'period_label', 'valuation_date', 'currency', 'rows', 'statistics', 'notes', 'source_changed')}, s.get('source_changed', False))
    return refs


def context(run):
    from .financial_periods import snapshot
    from .valuation_analysis import summary
    from .research_tracking import snapshot as tracking_snapshot
    value = {}
    if run.get('market_snapshot'):
        value['market_snapshot'] = deepcopy(run['market_snapshot'])
    if run.get('financial_period_records'):
        periods = snapshot(run)
        analysis = periods['analysis']
        # Preserve the full ledger in storage and exports, while keeping model
        # context independent of years of accumulated comparison rows.
        preview = {}
        for key, rows in analysis.items():
            if isinstance(rows,list):
                preview[key] = sorted(rows,key=lambda r:bool(r.get('reason')) if isinstance(r,dict) else False)[:16]
            else: preview[key] = rows
        value['financial_periods'] = {'analysis': preview,
            'analysis_counts': {k:len(v) for k,v in analysis.items() if isinstance(v,list)},
            'context_note':'模型上下文为节选，完整公式与输入保留在财务期间页面及证据包中。', 'note': periods['note'],
            'records': [{k: r.get(k) for k in ('id', 'metric', 'year', 'period', 'period_type', 'value', 'unit', 'currency', 'scope', 'source_id', 'source_status')} for r in periods['records'][-30:]]}
    if run.get('valuation_sensitivity_history') or run.get('comparable_analyses'):
        view = summary(run)
        value['sensitivity'] = [{k: s.get(k) for k in ('id', 'method', 'base_scenario_id', 'fixed_assumptions', 'axes', 'envelope', 'valid_cells', 'invalid_cells', 'stale', 'notes')} for s in view['sensitivities'][-3:]]
        value['comparables'] = [{**{k: s.get(k) for k in ('id', 'method', 'period_kind', 'period_label', 'valuation_date', 'currency', 'statistics', 'source_changed', 'notes')},
            'rows': [{k: r.get(k) for k in ('company','ticker','multiple_value','included','exclusion_reason','source_refs')} for r in s.get('rows',[])]} for s in view['comparables'][-3:]]
    if (run.get('memo') or {}).get('tracking_metrics'):
        tracking = tracking_snapshot(run)
        value['tracking'] = {k: tracking[k] for k in ('report_identity', 'metrics', 'observations')}
        value['tracking']['observations'] = value['tracking']['observations'][-8:]
    return value


def dependencies(run):
    """Explicit extension inputs only; a read does not migrate legacy reports."""
    fields = ('financial_period_records', 'valuation_sensitivity_history', 'comparable_analyses')
    result = {key: deepcopy(run[key]) for key in fields if run.get(key)}
    if result:
        from .evidence_policy import source_fingerprint
        result['current_source_fingerprints'] = {sid: source_fingerprint(s, run) for sid, s in sources(run).items()}
    # Observations are immutable evidence inputs, even after a report revision.
    # Including all IDs avoids a new report invalidating its own saved snapshot
    # when the current report identity changes at save time.
    if run.get('tracking_observations'):
        result['tracking_observations'] = deepcopy(run['tracking_observations'])
    if run.get('market_snapshot'):
        result['market_snapshot'] = {k: deepcopy(run['market_snapshot'].get(k)) for k in
            ('status','ticker','price','currency','trade_date','as_of','source_id','text','provider','gaps','price_age_days','age_days')}
    return result


def markdown(run, audit=False):
    from .financial_periods import snapshot, STATUS_LABELS
    from .valuation_analysis import markdown_sections
    from .research_tracking import markdown as tracking_markdown
    lines = []
    if run.get('financial_period_records'):
        view = snapshot(run)
        lines += ['## 财务期间工作底稿', '以下为当前工作底稿；新增分析不会自动改写上方已保存报告。', view['note'],
            '| 指标 | 年度期间 | 类型 | 数值 | 单位 | 币种/主体 | 来源状态 |', '|---|---|---|---:|---|---|---|']
        for r in view['records']:
            lines.append(f"| {r['label']} | {r['year']} {r['period']} | {r['period_type']} | {r['value']} | {r['unit']} | {r['currency']}/{r['scope']} | {r['source_status_label']} |")
        def money(value):
            return '—' if value is None else f'{Decimal(str(value))/Decimal(100000000):,.4f}'
        def cell(value):
            return str(value or '').replace('|', '／').replace('\n', ' ')
        lines += ['### 同比与环比', '金额统一为亿元，币种与报表范围逐行列示。',
            '| 指标／期间 | 比较 | 本期／基期 | 变化或阻断原因 | 来源状态 |', '|---|---|---:|---|---|']
        comparisons=sorted(view['analysis']['comparisons'],key=lambda c:bool(c.get('reason')))
        for c in comparisons:
            change=(f"{Decimal(c['value']):.2f}%" if c.get('value') is not None else
                f"金额变化 {money(c['change'])} 亿元；{c.get('reason') or '不计算增长率'}" if c.get('change') is not None else c.get('reason') or '缺少基期')
            scope='合并' if c['scope']=='consolidated' else '母公司'
            lines.append(f"| {cell(c['label'])}／{cell(c['period_label'])} | {'同比' if c['kind']=='yoy' else '环比'} · {c['currency']} · {scope} | {money(c.get('current'))}／{money(c.get('previous'))} | {cell(change)} | {STATUS_LABELS.get(c['source_status'],c['source_status'])} |")
        lines += ['- ' + cell(i) for i in view['analysis']['issues']]
        if audit:
            lines += ['```json', json.dumps(view, ensure_ascii=False, indent=2), '```']
    val = markdown_sections(run, audit=audit)
    if val:
        lines += ['以下估值分析属于当前工作底稿，是否纳入正文以报告引用为准。', val]
    tracking = tracking_markdown(run)
    if tracking: lines.append(tracking)
    return '\n\n'.join(lines)
