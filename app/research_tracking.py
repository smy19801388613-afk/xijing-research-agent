"""Evidence-bound monitoring plans and append-only observations of a saved report.

Observations never revise the report, its original thesis, or the research cutoff.
A source match proves provenance, not the financial meaning of an observation.
"""
from copy import deepcopy
import hashlib
import re
import uuid
from fastapi import APIRouter, Body
from . import store, competition
from .evidence_policy import eligibility
from .report_state import report_identity

router = APIRouter()
ASSESSMENTS = {'supporting': '支持原论点', 'challenging': '挑战原论点', 'inconclusive': '仍无法判断'}


def _text(value, label, limit=1500, required=True):
    if not isinstance(value, str) or len(value) > limit or required and not value.strip():
        raise ValueError(label + '需为有效文本')
    return value.strip()


def _normal(value):
    return re.sub(r'\s+', '', value)


def _amount_text(value, period):
    """Exclude only explicit date annotations also specified by baseline.period.

    A bare four-digit number, a monetary value such as 2025 元, and a year
    inconsistent with period remain subject to exact numeric provenance checks.
    """
    components = {'年': set(), '月': set(), '日': set()}
    for year in re.findall(r'(?<![\d.])(?:19|20|21)\d{2}(?![\d.])', period):
        components['年'].add(int(year))
    for number, unit in re.findall(r'(?<![\d.])([0-9]{1,2})\s*([月日])', period):
        if 1 <= int(number) <= (12 if unit == '月' else 31):
            components[unit].add(int(number))
    for match in re.finditer(r'(?<!\d)((?:19|20|21)\d{2})[-/]([0-9]{1,2})(?:[-/]([0-9]{1,2}))?(?!\d)', period):
        if 1 <= int(match[2]) <= 12: components['月'].add(int(match[2]))
        if match[3] and 1 <= int(match[3]) <= 31: components['日'].add(int(match[3]))
    def annotation(match):
        return '' if int(match[1]) in components[match[2]] else match.group(0)
    return re.sub(r'(?<![\d.\-+])([0-9]{1,4})\s*([年月日])', annotation, value)


def _source(catalog, sid, quote, run):
    sid = _text(sid, '跟踪来源 ID', 160)
    quote = _text(quote, '跟踪原文摘录', 2000)
    ref = catalog.get(sid)
    if not ref or not eligibility(ref, run)['supports_claim']:
        raise ValueError('跟踪基线与观察需引用已读、日期有效且无冲突的来源；搜索摘要、待核对 OCR 和日期未知资料不能作为已确认基线')
    body = str(ref.get('text') or ref.get('quote') or '')
    if len(_normal(quote)) < 4 or _normal(quote) not in _normal(body):
        raise ValueError('跟踪引用摘录未在实际取得的原文中匹配；使用连续原文，不拼接或改写数字')
    return sid, quote, ref


def _assert_valuation_units(metric, run):
    """Catch direct share-price comparisons against observed total-value grids.

    This is a narrow dimensional guard grounded in the run's actual outputs,
    not a general assessment of a model's valuation interpretation.
    """
    history = (run or {}).get('valuation_sensitivity_history', [])
    if not history: return
    context = ' '.join(metric.get(k, '') for k in ('name', 'definition', 'support_condition', 'invalidate_condition'))
    context += ' ' + str(metric.get('baseline', {}).get('source_id') or '')
    linked = [s for s in history if s.get('id') and s['id'] in context]
    if not linked:
        latest = {s.get('method'): s for s in history}
        names = {'pe': 'PE', 'ps': 'PS', 'pb': 'PB', 'dcf': 'DCF', 'ddm': 'DDM', 'ev_ebitda': r'EV\s*[/／]\s*EBITDA'}
        methods = [key for key, pattern in names.items() if re.search(r'(?<![A-Za-z])' + pattern + r'(?![A-Za-z])', context, re.I)]
        linked = [latest[m] for m in methods if m in latest] if methods else list(latest.values())
    totals = {key for s in linked for key in s.get('envelope', {}) if key in ('equity_value_100m', 'enterprise_value_100m')}
    if not totals or any(s.get('envelope', {}).get('price') for s in linked): return
    price = r'(?:股价|收盘价|市场价格|每股(?:价格|市价)|交易价格)'
    if re.search(price, metric['definition']): price = r'(?:股价|收盘价|市场价格|每股(?:价格|市价)|交易价格|价格)'
    interval = r'(?:敏感性(?:分析)?(?:区间|包络)?|估值区间|情景区间|包络)'
    compare = r'(?:落在|落入|进入|位于|处于|高于|低于|超出|超过|偏离|接近|小于|大于|介于|比较|对比|相比|区间内|区间外)'
    shares = r'(?:摊薄后)?(?:总股本|总股数|股数)'
    value = r'(?:股权(?:总)?价值|总股权价值|股权总值|估值总额|总价值|总值)'
    to_cap = price + r'.{0,10}(?:乘以|乘|×|\*).{0,10}' + shares + r'.{0,20}(?:市值|总股权价值)'
    to_share = value + r'.{0,12}(?:除以|除|÷|/).{0,10}' + shares + r'.{0,20}(?:每股|元/股)'
    for field in ('support_condition', 'invalidate_condition'):
        condition = metric[field]
        if not (re.search(price, condition) and re.search(interval, condition) and re.search(compare, condition)): continue
        clauses = re.split(r'[。；;\n]', condition)
        comparisons = [c for c in clauses if re.search(price, c) and re.search(interval, c) and re.search(compare, c)]
        for clause in comparisons:
            denial = r'(?:不能|不得|不可|不应|禁止|不要)'
            compare_action = r'(?:比较|对比|相比|比照)'
            if (re.search(denial + r'(?:直接)?(?:将|把|以|用|拿)?' + price + r'[^，,；;。]{0,35}' + compare_action, clause)
                    or re.search(price + r'[^，,；;。]{0,12}' + denial + r'[^，,；;。]{0,20}' + compare_action, clause)
                    or re.search(denial + r'(?:直接)?' + compare_action + r'[^，,；;。]{0,4}' + price, clause)):
                continue
            conversion = metric['definition'] + '；' + clause
            # A negated conversion cannot license an otherwise invalid relation.
            conversion = re.sub(r'(?:不|未|无需|无须|不要|不能|不得|不可|不应)[^，,；;。]*', '', conversion)
            converted = bool(re.search(to_cap, conversion) or re.search(to_share, conversion))
            if totals == {'enterprise_value_100m'}:
                converted = converted and bool(re.search(r'(?:企业价值.{0,20}(?:减去|扣除).{0,12}净债务|桥接.{0,10}股权价值)', conversion))
            if not converted:
                label = '支持条件' if field == 'support_condition' else '推翻条件'
                raise ValueError('跟踪指标“' + metric['name'] + '”的' + label + '混用了每股价格与总价值：当前关联敏感性仅有总价值包络（亿元），没有每股价格结果。'
                    '必须比较同单位：股价乘总股数换算总市值后与股权总价值比较，或股权总价值除以股数得到每股价值后再比较；企业价值还需先桥接股权价值。'
                    '仅提到收盘价和总股本不等于已换算；无市值、股数或价格数据时明确待补，不得写市场价格直接落入/高于/低于总价值包络。')


def normalize_metrics(candidate, catalog, run=None, theses=None, allow_existing_thesis=False):
    """Strictly validate a newly supplied plan; an absent legacy plan stays absent."""
    if not isinstance(candidate, list) or not 1 <= len(candidate) <= 12:
        raise ValueError('持续跟踪指标需为 1—12 项结构化数组')
    result, known = [], set()
    titles = {t.get('title') for t in theses or [] if isinstance(t, dict)}
    previous = {m.get('id'): m for m in ((run or {}).get('memo') or {}).get('tracking_metrics', [])}
    for item in candidate:
        if not isinstance(item, dict): raise ValueError('每项跟踪指标必须是对象')
        clean = {field: _text(item.get(field), label, limit) for field, label, limit in (
            ('name', '指标名称', 160), ('definition', '指标定义和口径', 1200),
            ('thesis', '关联论点', 300), ('frequency', '更新频率', 300),
            ('next_check', '下次检查条件', 600), ('support_condition', '支持条件', 1000),
            ('invalidate_condition', '推翻条件', 1000), ('next_action', '后续动作', 1000))}
        unchanged = allow_existing_thesis and item == previous.get(item.get('id'))
        if titles and clean['thesis'] not in titles and not unchanged:
            raise ValueError('跟踪指标的 thesis 必须复制本报告实际核心论点的 title')
        if run and run.get('request', {}).get('as_of'):
            from .report_dates import past_catalysts
            stale = past_catalysts({'sections': [{'key': 'catalysts', 'body': clean['next_check']}]}, run['request']['as_of'])
            if stale: raise ValueError('下次跟踪检查条件不能把截止日前的日期当未来事件：' + stale[0])
        identity = item.get('id') or 'tm-' + hashlib.sha256((clean['name'] + clean['definition'] + clean['thesis']).encode()).hexdigest()[:12]
        if not isinstance(identity, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', identity) or identity in known:
            raise ValueError('跟踪指标 ID 需唯一，且仅包含字母、数字、下划线或短横线')
        known.add(identity)
        baseline = item.get('baseline')
        if not isinstance(baseline, dict): raise ValueError('跟踪指标需要 baseline 对象；缺失时明确 status=missing')
        if baseline.get('status') == 'missing':
            reason = _text(baseline.get('value'), '基线缺失说明', 1000)
            clean['baseline'] = {'status': 'missing', 'value': reason,
                'period': _text(baseline.get('period', ''), '基线期间', 200, False), 'source_id': None, 'quote': ''}
            clean['status'] = 'needs_baseline'
        elif baseline.get('status') == 'observed':
            sid, quote, ref = _source(catalog, baseline.get('source_id'), baseline.get('quote'), run)
            value = _text(baseline.get('value'), '基线原值及单位', 1000)
            period = _text(baseline.get('period'), '基线期间', 200)
            # Avoid untraceable model arithmetic. Converted/derived baselines need
            # their own deterministic source; the original quote remains exact.
            number = lambda s: re.findall(r'-?\d+(?:\.\d+)?', s.replace(',', '').replace('，', ''))
            missing = list(dict.fromkeys(n for n in number(_amount_text(value, period)) if n not in number(quote)))
            if missing:
                raise ValueError('跟踪基线数字未在引用摘录中找到：指标“' + clean['name'] + '”，不匹配值 ' + '、'.join(missing)
                    + '。baseline.value 只填原披露数值与单位，时间写入 baseline.period；明确的年/月/日注记仅在与 period 一致时免于金额匹配，派生值须先保存可复核计算')
            clean['baseline'] = {'status': 'observed', 'value': value,
                'period': period, 'source_id': sid, 'quote': quote}
            clean['status'] = 'monitoring'
        else:
            raise ValueError('跟踪基线 status 只能为 observed 或 missing')
        _assert_valuation_units(clean, run)
        numeric = bool(re.search(r'\d', clean['support_condition'] + clean['invalidate_condition']))
        basis = item.get('threshold_basis', 'qualitative')
        if basis not in ('qualitative', 'unconfirmed_assumption'):
            raise ValueError('跟踪阈值仅支持定性条件或尚未获用户确认的研究假设')
        # A model cannot confer user confirmation on a newly generated threshold.
        clean.update(id=identity, threshold_basis='unconfirmed_assumption' if numeric else basis,
                     threshold_confirmed=False)
        result.append(clean)
    return result


def snapshot(run):
    identity = report_identity(run)
    observations = deepcopy(run.get('tracking_observations', []))
    current = [o for o in observations if o.get('report_identity') == identity]
    return {'report_identity': identity, 'metrics': deepcopy((run.get('memo') or {}).get('tracking_metrics', [])),
            'observations': current, 'previous_report_observations': [o for o in observations if o not in current],
            'report_unchanged': True}


@competition.traced('tracking_observation', 'run_id')
def record_observation(run_id, metric_id, payload, origin='human'):
    if not isinstance(payload, dict): raise ValueError('观察记录需要结构化对象')
    if origin not in ('human', 'model'): raise ValueError('未知观察记录来源')
    expected = payload.get('report_identity')
    if not isinstance(expected, dict) or not isinstance(expected.get('memo_hash'), str):
        raise ValueError('请提供当前 report_identity，防止将观察绑定到其他报告版本')
    def change(run):
        if expected != report_identity(run):
            raise ValueError('报告版本已变化，请刷新并确认新版本指标；观察未写入')
        metric = next((m for m in (run.get('memo') or {}).get('tracking_metrics', []) if m.get('id') == metric_id), None)
        if metric is None: raise ValueError('当前报告中没有此跟踪指标')
        from .questions import catalog
        sid, quote, source = _source(catalog(run), payload.get('source_id'), payload.get('quote'), run)
        assessment = payload.get('assessment')
        if assessment not in ASSESSMENTS: raise ValueError('观察判断需为 supporting、challenging 或 inconclusive')
        entry = {'id': uuid.uuid4().hex[:16], 'metric_id': metric_id,
            'report_identity': deepcopy(expected), 'metric_snapshot': deepcopy(metric),
            'recorded_at': store.now(), 'source_id': sid, 'quote': quote,
            'source_snapshot': {k: source.get(k) for k in ('id', 'title', 'url', 'kind', 'published_at', 'read_status')},
            'observation': _text(payload.get('observation'), '观察解释', 2000),
            'assessment': assessment, 'assessment_label': ASSESSMENTS[assessment],
            'origin': origin, 'semantic_status': '观察解释与支持关系仍需研究员复核'}
        run.setdefault('tracking_observations', []).append(entry)
    saved = store.update_run(run_id, change)
    competition.audit('tracking_observation_saved', run_id=run_id, metric_id=metric_id,
                      observation_id=saved['tracking_observations'][-1]['id'], origin=origin)
    return snapshot(saved)


def markdown(run):
    metrics = (run.get('memo') or {}).get('tracking_metrics', [])
    if not metrics: return ''
    lines = ['## 持续跟踪指标', '', '观察记录独立于已保存报告；支持关系仍需研究员复核。', '']
    for metric in metrics:
        b = metric['baseline']
        lines += ['### ' + metric['name'], '', '关联论点：' + metric['thesis'], '', '定义与口径：' + metric['definition'], '',
                  '当前基线：' + ('待补充 · ' if b['status'] == 'missing' else '') + b['value'] + ('（' + b['period'] + '）' if b['period'] else ''), '']
        if b.get('source_id'): lines += ['来源：' + b['source_id'], '', '> ' + b['quote'].replace('\n', '\n> '), '']
        lines += ['更新安排：' + metric['frequency'] + '；' + metric['next_check'], '',
                  '支持条件：' + metric['support_condition'], '', '推翻条件：' + metric['invalidate_condition'], '',
                  '后续动作：' + metric['next_action'], '']
        if metric.get('threshold_basis') == 'unconfirmed_assumption':
            lines += ['阈值说明：研究员建议的观察阈值，未获用户确认。', '']
    observations = snapshot(run)['observations']
    if observations:
        lines += ['### 本版本后续观察', '']
        for item in observations:
            lines += [f"- {item['recorded_at']} · {item['metric_snapshot']['name']} · {item['assessment_label']}：{item['observation']}（来源 {item['source_id']}）",
                      '> ' + item['quote'].replace('\n', '\n> '), '']
    return '\n'.join(lines)


@router.get('/api/runs/{run_id}/tracking')
def get_tracking(run_id: str):
    return snapshot(store.get_run(run_id))


@router.post('/api/runs/{run_id}/tracking/{metric_id}/observations')
def post_observation(run_id: str, metric_id: str, payload: dict = Body(...)):
    return record_observation(run_id, metric_id, payload)
