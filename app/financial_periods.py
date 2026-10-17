"""Explicit period arithmetic, independent of the legacy annual fact ledger.

An excerpt match establishes provenance only, never semantic verification of a
model's extraction. All arithmetic preserves this distinction and input IDs.
"""
from collections import defaultdict
from datetime import date
from decimal import Decimal
import hashlib
import re
import uuid

from fastapi import APIRouter, Body
from . import store
from .finance import number, growth

router = APIRouter()
METRICS = {
    'revenue': ('营业收入', 'flow'),
    'n_income_attr_p': ('归母净利润', 'flow'),
    'deducted_profit': ('扣非归母净利润', 'flow'),
    'n_cashflow_act': ('经营现金流净额', 'flow'),
    'accounts_receivable': ('应收账款', 'balance'),
    'inventory': ('存货', 'balance'),
    'total_assets': ('总资产', 'balance'),
    'total_liabilities': ('总负债', 'balance'),
}
UNITS = {'元': Decimal(1), '千元': Decimal(1000), '万元': Decimal(10000), '亿元': Decimal(100000000)}
PERIODS = {'Q1': 1, 'Q2': 2, 'H1': 2, 'Q3': 3, '9M': 3, 'Q4': 4, 'FY': 4}
STATUS_LABELS = {'source_matched': '原文已匹配 · 数值口径待复核', 'user_unverified': '用户录入 · 未核验', 'source_changed': '来源变化 · 暂停计算'}
NOTE = '原文匹配只证明摘录来自已读材料，不代表金额、期间、单位或归因已经核验；手工录入及其计算结果始终标为未核验。'


def _text(value, label, maximum=500, required=True):
    if not isinstance(value, str) or len(value) > maximum or required and not value.strip():
        raise ValueError(label + '需为有效文本')
    return value.strip()


def _day(value, label):
    try:
        value = str(value)
        if len(value) == 8 and value.isdigit(): value = value[:4] + '-' + value[4:6] + '-' + value[6:]
        return date.fromisoformat(value).isoformat()
    except (ValueError, TypeError):
        raise ValueError(label + '需为 YYYY-MM-DD 日期') from None


def _normal(value):
    return re.sub(r'\s+', '', str(value or ''))


def _catalog(run):
    from .questions import catalog
    # Derived extension artifacts can never establish a new extracted input.
    # Exclude them while obtaining originals, so extension exporters can also
    # revalidate this ledger without recursively rebuilding their own sources.
    originals = {**run, 'financial_period_records': [], 'valuation_sensitivity_history': [], 'comparable_analyses': []}
    return catalog(originals)


def _source(record, run, refs, origin):
    source_type = record.get('source_type', 'existing')
    if source_type not in ('existing', 'user_input'): raise ValueError('来源类型仅支持 existing 或 user_input')
    if source_type == 'user_input' and origin != 'human':
        raise ValueError('模型只能绑定已取得的来源，不可将生成数值冒充用户手动录入')
    sid = _text(record.get('source_id'), '来源 ID', 250)
    quote = _text(record.get('quote'), '原文摘录', 4000)
    announced = _day(record.get('announced_date'), '公告日期')
    if announced > _day(run['request']['as_of'], '研究截止日') or announced > date.today().isoformat():
        raise ValueError('公告日期晚于研究截止日或今天，不得使用未来信息')
    result = {'source_type': source_type, 'source_id': sid, 'quote': quote, 'announced_date': announced}
    if source_type == 'user_input':
        return {**result, 'source_status': 'user_unverified', 'source_title': _text(record.get('source_title', sid), '来源标题'), 'source_hash': None}
    source = refs.get(sid)
    if not source: raise ValueError('来源 ID 不存在或晚于截止日；请先读取来源，手工数据请选择用户录入')
    text = source.get('text', '')
    if len(_normal(quote)) < 4 or _normal(quote) not in _normal(text):
        raise ValueError('原文摘录未匹配已读取的来源，请复制连续原文，不要改写或拼接')
    policy = source.get('eligibility', {})
    if not policy.get('supports_answer'):
        raise ValueError('来源尚无可用正文、日期或存在冲突，不能作为期间计算依据：' + '；'.join(policy.get('reasons', [])))
    actual_date = _day(str(source.get('published_at') or source.get('announced_date') or '')[:10], '来源公告日期')
    if actual_date != announced: raise ValueError('录入公告日期与实际来源日期不一致')
    return {**result, 'source_status': 'source_matched', 'source_title': source.get('title') or source.get('locator') or sid,
            'source_hash': hashlib.sha256(text.encode()).hexdigest()}


def normalize(record, run, refs=None, origin='human'):
    if not isinstance(record, dict): raise ValueError('财务期间记录需为对象')
    metric = record.get('metric')
    if metric not in METRICS: raise ValueError('不支持的财务指标')
    year = record.get('year')
    if isinstance(year, bool) or not isinstance(year, int) or not 2000 <= year <= 2100: raise ValueError('年度需为 2000—2100 的整数')
    period = record.get('period')
    if period not in PERIODS: raise ValueError('期间需为 Q1、H1、9M、FY 或 Q2/Q3/Q4')
    period_type = record.get('period_type')
    kind = METRICS[metric][1]
    if kind == 'balance' and period_type != 'point': raise ValueError('资产负债类指标必须使用 point 时点口径，不能做差拆季度')
    if kind == 'flow' and period_type not in ('single', 'cumulative'): raise ValueError('流量指标必须明确 single 单季或 cumulative 累计口径')
    if period in ('FY', 'H1', '9M') and period_type == 'single': raise ValueError('FY/H1/9M 为累计期间，单季度请填写 Q1—Q4')
    value = number(record.get('value'))
    if value is None: raise ValueError('金额必须是有限数值')
    if abs(value) > Decimal('1e18'): raise ValueError('金额超出支持范围，请核对单位')
    unit = record.get('unit')
    if unit not in UNITS: raise ValueError('金额单位必须明确为元、千元、万元或亿元')
    currency = record.get('currency')
    if currency not in ('CNY', 'HKD', 'USD', 'EUR'): raise ValueError('币种必须为 CNY、HKD、USD 或 EUR；不会自动汇兑')
    scope = record.get('scope')
    if scope not in ('consolidated', 'parent'): raise ValueError('报表范围必须为 consolidated 合并或 parent 母公司')
    if metric in ('n_income_attr_p', 'deducted_profit') and scope != 'consolidated':
        raise ValueError('归母及扣非归母指标使用合并报表口径，不能与母公司净利润混用')
    restatement = record.get('restatement', 'original')
    if restatement not in ('original', 'restated'): raise ValueError('重述状态应为 original 或 restated')
    comparable = _text(record.get('comparability_key', 'reported'), '可比口径标识', 160)
    restatement_note = _text(record.get('restatement_note', ''), '重述说明', 1200, False)
    if restatement == 'restated' and (comparable == 'reported' or not restatement_note):
        raise ValueError('重述数据须提供明确可比口径标识与调整说明，不能自动与原披露数据混算')
    quarter = PERIODS[period]
    end = f'{year}-{["03-31", "06-30", "09-30", "12-31"][quarter - 1]}'
    source = _source(record, run, refs if refs is not None else _catalog(run), origin)
    if source['announced_date'] < end: raise ValueError('公告日期早于报告期末，请核对年份和期间')
    # Match the original amount, not a model's silent unit conversion. Even a
    # successful match remains an extraction awaiting semantic review.
    if source['source_type'] == 'existing':
        numeric_quote = re.sub(r'[（(]\s*(\d[\d,]*(?:\.\d+)?)\s*[）)]', r'-\1', source['quote'])
        tokens = re.findall(r'(?<![A-Za-z0-9_.+−-])[-+−]?\d[\d,]*(?:\.\d+)?(?:[eE][-+]?\d+)?', numeric_quote)
        seen = {number(token.replace('−', '-')) for token in tokens}
        if value not in seen: raise ValueError('原始金额未在绑定摘录中找到，请保留原文金额及单位，不要先换算')
    units_seen = set(re.findall(r'亿元|万元|千元|(?<![亿万千美港欧日])元', source['quote']))
    if len(units_seen) == 1 and unit not in units_seen: raise ValueError('录入单位与原文摘录单位不一致，请核对后保存')
    return {'id': record.get('id') or 'fp-' + uuid.uuid4().hex[:12], 'metric': metric, 'label': METRICS[metric][0],
            'kind': kind, 'year': year, 'period': period, 'quarter': quarter, 'period_type': period_type,
            'period_end': end, 'value': str(value), 'unit': unit, 'value_base': str(value * UNITS[unit]),
            'currency': currency, 'scope': scope, 'attribution': 'attributable' if metric in ('n_income_attr_p', 'deducted_profit') else 'all_owners',
            'restatement': restatement, 'restatement_note': restatement_note, 'comparability_key': comparable,
            **source, 'origin': origin, 'recorded_at': store.now()}


def save_records(run_id, records, origin='human'):
    if not isinstance(records, list) or not 1 <= len(records) <= 150: raise ValueError('每次保存 1—150 条期间记录')
    def change(run):
        old = list(run.get('financial_period_records', [])); known = {r['id']: r for r in old}
        refs = _catalog(run); prepared = [normalize(r, run, refs, origin) for r in records]
        ids = [r['id'] for r in prepared]
        if len(ids) != len(set(ids)): raise ValueError('同一批次记录 ID 不得重复')
        for raw, new in zip(records, prepared):
            if raw.get('id') and raw['id'] not in known: raise ValueError('更新记录 ID 不存在，请新增时省略 ID')
            if raw.get('id'):
                previous = known[raw['id']]
                new['history'] = previous.get('history', []) + [{k:v for k,v in previous.items() if k != 'history'}]
            known[new['id']] = new
        if len(known) > 500: raise ValueError('单项研究最多保存 500 条期间记录')
        run['financial_period_records'] = list(known.values())
    return snapshot(store.update_run(run_id, change))


def delete_record(run_id, record_id):
    def change(run):
        current = run.get('financial_period_records', [])
        if not any(r['id'] == record_id for r in current): raise ValueError('期间记录不存在')
        removed = next(r for r in current if r['id'] == record_id)
        run.setdefault('financial_period_deleted', []).append({**removed, 'deleted_at': store.now()})
        run['financial_period_records'] = [r for r in current if r['id'] != record_id]
    return snapshot(store.update_run(run_id, change))


def _basis(r):
    return (r['metric'], r['currency'], r['scope'], r['comparability_key'], r['restatement'])


def _status(rows):
    states = {r['source_status'] for r in rows}
    return 'source_changed' if 'source_changed' in states else 'user_unverified' if 'user_unverified' in states else 'source_matched'


def _point(rows, identifier, label, value, formula, **extra):
    return {**{k: rows[0][k] for k in ('metric', 'currency', 'scope', 'comparability_key', 'restatement', 'year', 'quarter')},
            'id': identifier, 'label': rows[0]['label'], 'period_label': label, 'value': str(value) if value is not None else None,
            'unit': '元', 'inputs': list(dict.fromkeys(r['id'] for r in rows)), 'source_ids': list(dict.fromkeys(r['source_id'] for r in rows)),
            'source_status': _status(rows), 'formula': formula, **extra}


def _period_label(year, quarter, period_type):
    suffix = (f'Q{quarter}期末' if period_type == 'point' else f'Q{quarter}单季' if period_type == 'single'
              else ['一季度累计', '上半年累计', '前三季度累计', '全年'][quarter - 1])
    return f'{year}年{suffix}'


def analyze(records):
    """No I/O: usable and blocked derived values retain complete input lineage."""
    buckets = defaultdict(list); issues = []; quarters = []; cumulative = []; balances = []
    for r in records:
        typ = 'point' if r['kind'] == 'balance' else r['period_type']
        buckets[(_basis(r), r['year'], r['quarter'], typ)].append(r)
    nodes = {}
    for key, rows in buckets.items():
        values = {r['value_base'] for r in rows}
        reason = '同一期间及口径存在金额冲突' if len({Decimal(v) for v in values}) > 1 else '关联来源已失效或变化' if _status(rows) == 'source_changed' else None
        basis, year, q, typ = key
        nid = 'fp-value-' + hashlib.sha256(repr(key).encode()).hexdigest()[:12]
        label = _period_label(year, q, typ)
        node = _point(rows, nid, label, None if reason else Decimal(rows[0]['value_base']), '原始金额 × 单位倍率', period_type=typ, reason=reason)
        nodes[key] = node
        if reason: issues.append({'code': 'conflict' if len(values)>1 else 'source_changed', 'message': reason, 'inputs':node['inputs']})
        if typ == 'point': balances.append(node)
        if typ == 'cumulative': cumulative.append(node)
    groups = {(key[0], key[1]) for key in nodes if key[3] != 'point'}
    for basis, year in sorted(groups):
        for q in range(1, 5):
            direct = nodes.get((basis, year, q, 'single'))
            current = nodes.get((basis, year, q, 'cumulative'))
            previous = nodes.get((basis, year, q-1, 'cumulative')) if q > 1 else None
            if not direct and not current: continue
            deriv = None
            if current:
                selected = [current] + ([previous] if previous else [])
                reason = current.get('reason') or (previous.get('reason') if previous else None)
                if q > 1 and not previous: reason = '缺少同年、同口径的上一累计期间，无法拆分单季'
                val = None if reason else Decimal(current['value']) - (Decimal(previous['value']) if previous else Decimal(0))
                deriv = {**current, 'id': current['id'] + '-q', 'period_label': f'{year}年Q{q}单季', 'period_type': 'single',
                         'value': str(val) if val is not None else None, 'reason': reason,
                         'inputs': list(dict.fromkeys(i for n in selected for i in n['inputs'])),
                         'source_ids': list(dict.fromkeys(i for n in selected for i in n['source_ids'])), 'source_status': _status(selected),
                         'formula': 'Q1 单季 = 一季度累计' if q == 1 else f'Q{q} 单季 = 本年截至 Q{q} 累计 − 本年截至 Q{q-1} 累计'}
            result = dict(direct or deriv)
            if direct and deriv and deriv.get('reason') and '冲突' in deriv['reason']:
                result.update(value=None, reason='累计来源存在冲突，不能用直接单季记录绕过核验', inputs=list(dict.fromkeys(direct['inputs']+deriv['inputs'])), source_status=_status([direct,deriv]))
            if direct and deriv and direct['value'] is not None and deriv['value'] is not None and Decimal(direct['value']) != Decimal(deriv['value']):
                result.update(value=None, reason='直接披露的单季金额与累计拆分结果冲突', inputs=list(dict.fromkeys(direct['inputs']+deriv['inputs'])), source_status=_status([direct,deriv]))
                issues.append({'code':'quarter_conflict','message':result['reason'],'inputs':result['inputs']})
            quarters.append(result)
    comparisons = []
    for values, kind in ((quarters, 'quarter'), (cumulative, 'cumulative'), (balances, 'balance')):
        lookup = {(_basis(n), n['year'], n['quarter']): n for n in values}
        for current in values:
            targets = [('yoy',current['year']-1,current['quarter'])]
            if kind in ('quarter', 'balance'):
                targets.append(('qoq',current['year'] if current['quarter']>1 else current['year']-1,current['quarter']-1 if current['quarter']>1 else 4))
            for typ, year, q in targets:
                prev = lookup.get((_basis(current),year,q))
                reason = current.get('reason') or (prev.get('reason') if prev else '缺少同币种、报表范围、可比及重述口径的基期')
                g = {'value':None,'change':None,'reason':reason} if reason else growth(current['value'], prev['value'])
                comparisons.append({'id':current['id']+'-'+typ,'metric':current['metric'],'label':current['label'],'period_label':current['period_label'],
                    'year':current['year'],'quarter':current['quarter'],'kind':typ,'period_type':kind,'current':current['value'],
                    'previous':prev['value'] if prev else None,'previous_period':prev['period_label'] if prev else _period_label(year,q,current['period_type']),
                    'currency':current['currency'],'scope':current['scope'],'comparability_key':current['comparability_key'],'restatement':current['restatement'],
                    'inputs':list(dict.fromkeys(current['inputs']+(prev['inputs'] if prev else []))),
                    'source_status':_status([current]+([prev] if prev else [])),
                    'formula':'本期 − 基期；基期 > 0 时 (本期 / 基期 − 1) × 100',**g})
    bridges = []
    lookup = {(n['metric'],n['year'],n['quarter'],n['period_type'],*(_basis(n)[1:])):n for n in quarters+cumulative}
    for current in quarters+cumulative:
        if current['metric'] != 'n_income_attr_p': continue
        other = lookup.get(('deducted_profit',current['year'],current['quarter'],current['period_type'],*(_basis(current)[1:])))
        reason = current.get('reason') or (other.get('reason') if other else '缺少同期间、同口径扣非归母净利润')
        bridges.append({'id':current['id']+'-nonrecurring','period_label':current['period_label'],'period_type':current['period_type'],
            'value':str(Decimal(current['value'])-Decimal(other['value'])) if not reason else None,'reason':reason,
            'currency':current['currency'],'unit':'元','formula':'非经常性损益归母影响 = 归母净利润 − 扣非归母净利润',
            'inputs':list(dict.fromkeys(current['inputs']+(other['inputs'] if other else []))),
            'source_status':_status([current]+([other] if other else [])),
            'note':'仅计算同口径差额，不等于已解释具体非经常性损益项目。'})
    signals = []
    cash = {(c['year'],c['quarter'],c['kind'],c['period_type'],c['currency'],c['scope'],c['comparability_key'],c['restatement']):c for c in comparisons if c['metric']=='n_cashflow_act'}
    for p in comparisons:
        if p['metric'] != 'n_income_attr_p': continue
        c = cash.get((p['year'],p['quarter'],p['kind'],p['period_type'],p['currency'],p['scope'],p['comparability_key'],p['restatement']))
        if not c or p.get('change') is None or c.get('change') is None: continue
        if Decimal(p['change'])*Decimal(c['change']) < 0:
            signals.append({'kind':'profit_cash_divergence','period_label':p['period_label'],'comparison':p['kind'],
                'message':'归母利润与经营现金流金额变化方向相反，仅为待核验信号；需继续检查营运资本、税费及其他现金项目，不能据此认定原因或财务造假。',
                'profit_change':p['change'],'cashflow_change':c['change'],'currency':p['currency'],
                'inputs':list(dict.fromkeys(p['inputs']+c['inputs'])),'source_status':_status([p,c])})
    return {'quarters':quarters,'cumulative':cumulative,'balances':balances,'comparisons':comparisons,'nonrecurring_bridges':bridges,'signals':signals,'issues':issues}


def validated_records(run, refs=None):
    refs = _catalog(run) if refs is None else refs
    records = []
    for stored in run.get('financial_period_records', []):
        r = dict(stored)
        if r.get('source_type') == 'existing':
            src = refs.get(r['source_id'])
            digest = hashlib.sha256(src.get('text','').encode()).hexdigest() if src else None
            source_day = str(src.get('published_at') or src.get('announced_date') or '')[:10] if src else ''
            if len(source_day)==8 and source_day.isdigit(): source_day=source_day[:4]+'-'+source_day[4:6]+'-'+source_day[6:]
            if not src or digest != r.get('source_hash') or source_day != r.get('announced_date') or not src.get('eligibility',{}).get('supports_answer'):
                r['source_status'] = 'source_changed'
        r['source_status_label'] = STATUS_LABELS[r['source_status']]
        records.append(r)
    return records


def snapshot(run):
    refs = _catalog(run); records = validated_records(run, refs)
    sources = [{'id':sid,'title':s.get('title') or s.get('locator') or sid,'published_at':s.get('published_at') or s.get('announced_date'),
                'usable':bool(s.get('eligibility',{}).get('supports_answer'))} for sid,s in refs.items()]
    return {'artifact':'financial_periods','run_id':run['id'],'request':{k:run['request'].get(k) for k in ('ticker','year','as_of')},
            'records':records,'analysis':analyze(records),'source_catalog':sources,'note':NOTE}


@router.get('/api/runs/{run_id}/financial-periods')
def get_financial_periods(run_id: str):
    return snapshot(store.get_run(run_id))


@router.post('/api/runs/{run_id}/financial-periods')
def post_financial_periods(run_id: str, payload: dict = Body(...)):
    return save_records(run_id, payload.get('records'), origin='human')


@router.delete('/api/runs/{run_id}/financial-periods/{record_id}')
def delete_financial_period(run_id: str, record_id: str):
    return delete_record(run_id, record_id)
