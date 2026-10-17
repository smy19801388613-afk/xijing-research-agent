"""Explicit research decisions, grounded in frozen inputs rather than model prices."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import re

from .evidence_policy import eligibility, parsed_date, source_fingerprint

ACTIONS = {'buy': '买入', 'hold': '持有', 'reduce': '减持', 'avoid': '回避', 'watch': '观察'}
STATUSES = {'actionable': '可形成当前研究建议', 'conditional': '条件性建议', 'insufficient_evidence': '暂不决策'}


def _text(value, field, limit=2000):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('投资建议缺少' + field)
    if len(value.strip()) > limit:
        raise ValueError('投资建议的' + field + '过长，请保留影响决策的具体理由')
    return value.strip()


def _items(value, field, required=False):
    if not isinstance(value, list) or len(value) > 12:
        raise ValueError('投资建议的' + field + '必须为不超过12项的数组')
    result = list(dict.fromkeys(_text(v, field, 1200) for v in value))
    if required and not result: raise ValueError('投资建议缺少' + field)
    return result


def _decimal(value):
    try:
        if isinstance(value, bool): return None
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except (InvalidOperation, ValueError, TypeError): return None


def _numeric_equal(a, b):
    if isinstance(a, dict) and isinstance(b, dict):
        return set(a) == set(b) and all(_numeric_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_numeric_equal(x, y) for x, y in zip(a, b))
    na, nb = _decimal(a), _decimal(b)
    return na == nb if na is not None and nb is not None else a == b


def market_basis(run, catalog):
    """Keep a real quote visible even when it is too old for a current decision."""
    snapshot = deepcopy(run.get('market_snapshot') or {'status': 'missing', 'gaps': ['尚未取得截至研究时点的市场价格']})
    reasons = []
    cutoff = parsed_date(run.get('request', {}).get('as_of'))
    trading = parsed_date(snapshot.get('trade_date'))
    price = _decimal(snapshot.get('price'))
    if snapshot.get('status') != 'available' or price is None or price <= 0:
        reasons.append('缺少可靠的市场价格')
    if snapshot.get('ticker') != run.get('request', {}).get('ticker'):
        reasons.append('市场价格证券代码不匹配')
    if snapshot.get('currency') != 'CNY':
        reasons.append('市场价格与现有人民币估值的币种不能直接比较')
    ref = catalog.get(snapshot.get('source_id'))
    if not ref or not eligibility(ref, run)['supports_claim']:
        reasons.append('市场价格缺少日期有效的实际来源')
    if not cutoff or not trading or trading > cutoff:
        reasons.append('市场交易日期缺失或晚于研究截止日')
    if parsed_date(snapshot.get('as_of')) != cutoff:
        reasons.append('市场快照不是按本次研究截止日查询')
    if snapshot.get('query_as_of') and parsed_date(snapshot['query_as_of']) != cutoff:
        reasons.append('市场快照查询日期早于本次研究截止日')
    if cutoff and trading and (cutoff - trading).days > 7:
        reasons.append('按本产品保守口径，距研究截止日超过7天的价格不作为当前行动依据；长假、停牌或数据迟延原因需核实')
    snapshot['decision_eligible'] = not reasons
    snapshot['decision_gaps'] = list(dict.fromkeys(reasons))
    return snapshot


def _scenarios(ids, run):
    from .valuation import calculate, basis_fingerprint
    history = {v.get('id'): v for v in run.get('valuation_history', [])}
    selected = []
    for sid in ids:
        scenario = history.get(sid)
        if not scenario:
            raise ValueError('投资建议引用了不存在的估值情景：' + sid + '；使用已有情景的裸ID，不添加valuation-前缀')
        if scenario.get('basis_fingerprint') and scenario['basis_fingerprint'] != basis_fingerprint(run, scenario.get('fact_ids', [])):
            raise ValueError('投资建议引用的估值基础已变化，请先重算情景：' + sid)
        try:
            current = calculate(run, scenario['method'], scenario['assumptions'])
        except (ValueError, KeyError) as error:
            raise ValueError('投资建议的估值情景无法按当前资料复核：' + sid + '；' + str(error)) from None
        if not _numeric_equal(current['output'], scenario.get('output')):
            raise ValueError('投资建议引用的估值输出与当前输入不一致，请先重算情景：' + sid)
        selected.append({'scenario_id': sid, **{k: deepcopy(scenario.get(k)) for k in
            ('method', 'method_name', 'assumptions', 'output', 'formula', 'fact_ids', 'basis_fingerprint')}})
    return selected


def _assert_action_consistency(value):
    """Reject obvious contradictions without claiming to assess investment merit."""
    buying = r'追加仓位|增加仓位|建立仓位|买入|加仓|建仓|开仓'
    listed_action = '(?:' + buying + r'|减持|卖出|回避|退出|持有)'
    negated_list = re.compile(r'(?:不采取|不进行|不建议|避免|禁止|不要|不得)'
        r'(?:(?:立即|直接|现在|盲目|贸然|进一步|继续|新)\s*){0,3}' + listed_action +
        r'(?:\s*(?:、|或|及|和|与|以及)\s*' + listed_action + r')*(?:动作|操作|行为)?')
    for field in ('summary', 'new_position', 'existing_position'):
        for clause in re.split(r'[。；;\n]', value[field]):
            negated_spans = [m.span() for m in negated_list.finditer(clause)]
            for match in re.finditer(buying, clause):
                if re.match(r'条件|标准|门槛', clause[match.end():]):
                    continue  # Referring to a decision criterion is not an order.
                if any(start <= match.start() < end for start, end in negated_spans):
                    continue
                # A later exit condition cannot authorize an earlier current
                # purchase. Likewise “风险待核实” is a gap, not “待核实后再买”.
                condition_text = re.sub(r'(?:若|如果)(?:已经|已|目前)?(?:持有|持仓|为未持仓者|尚未持仓)', '', clause[:match.start()])
                conditional = re.search(r'若(?!干)|如果|(?:等待|待|条件满足|条件达成|确认|解除|验证|核实|改善|兑现).{0,80}(?:后|时|再|才)', condition_text)
                conditional = conditional or re.search(r'(?:仅当|仅在|只有在|当).{0,80}(?:满足|达成|核验|验证).{0,30}(?:后|时)', condition_text)
                if conditional: continue
                prefix = clause[max(0, match.start() - 24):match.start()]
                if re.search(r'(?:不|未|暂不|不宜|避免|停止|禁止|无需|不要|不得|放弃|不能|不建议|不考虑)'
                             r'(?:(?:采取|进行|立即|直接|现在|盲目|贸然|新增|新|继续|进一步|主动|立刻|再|考虑|建议)\s*){0,3}$', prefix):
                    continue
                immediate = re.search(r'立即|现在|现价|当前|直接|立刻|马上|即刻', prefix)
                affirmative = re.search(r'建议|可以|应当|应|考虑|宜', prefix) or immediate
                if value['status'] != 'actionable' and immediate:
                    raise ValueError('条件性/暂不决策建议不能同时要求立即买入或加仓；写清先满足条件后再行动')
                if value['action'] in ('avoid', 'watch', 'reduce') and affirmative:
                    raise ValueError('回避、观察或减持建议不能同时要求当前买入建仓；买入仅可作为明确未来条件满足后的重新评估动作')


def freshness(run):
    """Check frozen recommendation inputs, including after an unrelated title edit."""
    recommendation = (run.get('memo') or {}).get('investment_recommendation')
    if not isinstance(recommendation, dict):
        return {'status': 'absent', 'needs_update': False, 'reasons': []}
    from .deep_research import reference_catalog
    catalog = reference_catalog(run)
    reasons = []
    saved = recommendation.get('source_fingerprints')
    if not isinstance(saved, dict):
        return {'status': 'untracked', 'needs_update': True, 'reasons': ['投资建议未保存完整依据指纹，需明确重评后建立关联']}
    for sid, fingerprint in saved.items():
        if sid not in catalog or fingerprint != source_fingerprint(catalog[sid], run):
            reasons.append('投资建议来源已变化或失效：' + sid)
    previous = recommendation.get('market_snapshot') or {}
    current = market_basis(run, catalog)
    fields = ('status', 'ticker', 'price', 'currency', 'trade_date', 'as_of', 'query_as_of', 'source_id', 'decision_eligible')
    if any(not _numeric_equal(previous.get(k), current.get(k)) for k in fields):
        reasons.append('市场报价或可用状态已变化；原标题/正文修订未重新判断投资建议')
    if recommendation.get('as_of') != run.get('request', {}).get('as_of'):
        reasons.append('投资建议的研究截止日已变化')
    try:
        checked = _scenarios(recommendation.get('valuation_scenario_ids', []), run)
        frozen = recommendation.get('valuation_basis', [])
        if not _numeric_equal(checked, frozen): reasons.append('投资建议引用的估值输入或结果已变化')
        latest = {v.get('method'): v for v in run.get('valuation_history', [])}
        for basis in frozen:
            current_scenario = latest.get(basis.get('method'), {})
            if any(not _numeric_equal(basis.get(key), current_scenario.get(key)) for key in ('assumptions', 'output', 'formula')):
                reasons.append('投资建议尚未评估该方法的最新估值情景：' + str(basis.get('method')))
    except ValueError as error:
        reasons.append(str(error))
    return {'status': 'stale' if reasons else 'current', 'needs_update': bool(reasons),
            'reasons': list(dict.fromkeys(reasons))}


def normalize(candidate, catalog, run):
    if not isinstance(candidate, dict):
        raise ValueError('完整备忘录必须包含结构化 investment_recommendation 投资建议；证据不足时明确暂不决策及改变判断的条件')
    action, status, confidence = (candidate.get(k) for k in ('action', 'status', 'confidence'))
    if action not in ACTIONS or status not in STATUSES or confidence not in ('low', 'medium', 'high'):
        raise ValueError('投资建议 action=buy/hold/reduce/avoid/watch，status=actionable/conditional/insufficient_evidence，confidence=low/medium/high')
    if status == 'insufficient_evidence' and (action != 'watch' or confidence == 'high'):
        raise ValueError('证据不足时使用 action=watch 且不能声称高置信度；需具体列出缺口和转变条件')
    if action == 'watch' and status == 'actionable':
        raise ValueError('观察不等于行动就绪；请使用conditional或insufficient_evidence并列出改变判断的条件')
    clean = {'schema_version': 1, 'action': action, 'status': status, 'confidence': confidence,
             'as_of': run.get('request', {}).get('as_of')}
    basis = candidate.get('basis', 'evidence_gap' if status == 'insufficient_evidence' else 'valuation')
    if basis not in ('valuation', 'fundamental_risk', 'evidence_gap'):
        raise ValueError('投资建议 basis 仅支持 valuation（估值判断）、fundamental_risk（经营或治理风险排除）、evidence_gap（证据不足）')
    if basis == 'fundamental_risk' and action not in ('avoid', 'watch'):
        raise ValueError('经营或治理风险排除可形成回避或观察建议；现价买入、持有、减持判断仍须价格和估值依据')
    if status == 'actionable' and basis == 'evidence_gap':
        raise ValueError('证据缺口不能作为当前行动建议的基础')
    clean['basis'] = basis
    for key, label in (('summary', '结论与理由'), ('horizon', '适用期限'),
                       ('new_position', '未持仓者建议'), ('existing_position', '已有持仓者建议')):
        clean[key] = _text(candidate.get(key), label)
    for key, label in (('buy_conditions', '买入或加仓条件'), ('exit_conditions', '退出或减仓条件'),
                       ('revisit_triggers', '重新评估触发条件'), ('key_risks', '核心风险'), ('blocking_gaps', '阻碍决策的证据缺口')):
        clean[key] = _items(candidate.get(key, []), label,
            required=key in ('revisit_triggers', 'key_risks', 'exit_conditions') or key == 'buy_conditions' and action in ('buy', 'watch')
                or key == 'blocking_gaps' and status == 'insufficient_evidence')
    if status == 'actionable' and clean['blocking_gaps']:
        raise ValueError('仍存在阻碍决策的证据缺口时不能标记actionable；改为条件性建议并明确条件')
    if status == 'conditional' and not clean['buy_conditions'] and not clean['blocking_gaps']:
        raise ValueError('条件性建议必须说明待满足的条件或阻碍决策的缺口')
    for field in ('new_position', 'existing_position'):
        if re.search(r'(?:你|您)(?:目前|当前|已经|已)?(?:持有|持仓|买入)|(?:你|您)的(?:当前)?仓位', clean[field]):
            raise ValueError('投资建议不得假定用户实际持仓；分别使用“未持仓者”和“若已持有”的情境表达')
    ids = _items(candidate.get('evidence_ids', []), '来源ID', required=status != 'insufficient_evidence')
    for sid in ids:
        if sid not in catalog or not eligibility(catalog[sid], run)['supports_inference']:
            raise ValueError('投资建议来源未取得、已失效、晚于截止日或仅为搜索摘要：' + sid)
    if status != 'insufficient_evidence' and not any(eligibility(catalog[s], run)['supports_claim'] and
            catalog[s].get('kind') not in ('calculation', 'valuation') and catalog[s].get('source_type') != 'market_data' for s in ids):
        raise ValueError('明确投资判断需引用至少一项日期有效的原文或财务事实，不能只有估值假设')
    scenario_ids = _items(candidate.get('valuation_scenario_ids', []), '估值情景ID')
    scenarios = _scenarios(scenario_ids, run)
    market = market_basis(run, catalog)
    comparisons = []
    if market['decision_eligible']:
        price = _decimal(market['price'])
        for scenario in scenarios:
            estimate = _decimal(scenario['output'].get('price'))
            if estimate is not None and estimate > 0:
                comparisons.append({'scenario_id': scenario['scenario_id'], 'scenario_price': str(estimate),
                    'current_price': str(price), 'currency': market['currency'], 'trade_date': market['trade_date'],
                    'upside_pct': format((estimate / price - 1) * 100, '.4f'), 'assumption_status': 'user_scenario'})
    risk_exclusion = action == 'avoid' and basis == 'fundamental_risk'
    if risk_exclusion and not any(eligibility(catalog[s], run)['supports_claim'] and catalog[s].get('text') and
            (catalog[s].get('kind') == 'pdf' or catalog[s].get('source_type') in
             ('company_disclosure', 'official_disclosure', 'government', 'regulator')) for s in ids):
        raise ValueError('明确风险回避需要已读、日期有效的原始披露或监管材料；摘要与估值假设不能代替经营/治理风险证据')
    if status == 'actionable' and not risk_exclusion and (not market['decision_eligible'] or not comparisons):
        raise ValueError('当前行动建议需要可靠的截至时点价格及已保存、可按同单位比较的每股估值情景；缺少价格、股数、股权桥接或关键估值依据时使用conditional/insufficient_evidence，并列明缺口')
    if not market['decision_eligible'] and status != 'insufficient_evidence' and not risk_exclusion and not clean['blocking_gaps']:
        raise ValueError('缺少可用市场价格时，条件性建议须在blocking_gaps明确价格/时点缺口')
    narrative = '\n'.join(clean[k] for k in ('summary', 'new_position', 'existing_position'))
    narrative += '\n' + '\n'.join(clean['buy_conditions'] + clean['exit_conditions'])
    if (not market['decision_eligible'] or not comparisons) and re.search(
            r'(?:当前|目前|现价|市价).{0,8}(?:明显|显著|已经|仍然|具有|具备|存在)?(?:低估|高估|便宜|昂贵|安全边际)', narrative):
        # A denial or an explicit conditional is not an unsupported market claim.
        for clause in re.split(r'[。；;\n]', narrative):
            if re.search(r'(?:当前|目前|现价|市价).{0,8}(?:低估|高估|便宜|昂贵|安全边际)', clause) and not re.search(r'无法|不能|不足以|尚不|尚未|不应|不代表|若|如果|假设|待', clause):
                raise ValueError('缺少可靠价格与同单位估值依据，不能声称当前低估、高估或已有安全边际；应写成明确的待验证条件')
    _assert_action_consistency(clean)
    from .report_dates import past_catalysts
    date_issues = past_catalysts({'sections': [{'key': 'catalysts', 'body': clean['horizon'] + '\n' + '\n'.join(clean['revisit_triggers'])}]}, clean['as_of'])
    if date_issues: raise ValueError('投资建议重评日期不能把过去事件列为未来条件：' + date_issues[0])
    from .research_tracking import _assert_valuation_units
    for field in ('summary', 'new_position', 'existing_position', 'buy_conditions', 'exit_conditions'):
        values = clean[field] if isinstance(clean[field], list) else [clean[field]]
        for value in values:
            _assert_valuation_units({'name': '投资建议', 'definition': '', 'support_condition': value,
                                     'invalidate_condition': '', 'baseline': {}}, run)
    from .fact_state import assert_claims
    assert_claims('\n'.join(str(v) for v in clean.values()), run, ids)
    clean.update(evidence_ids=list(dict.fromkeys(ids + ([market['source_id']] if market['decision_eligible'] else [])
                                                + ['valuation-' + sid for sid in scenario_ids])),
                 valuation_scenario_ids=scenario_ids, market_snapshot=market,
                 valuation_basis=scenarios, price_comparisons=comparisons,
                 basis_note='投资建议属于研究判断；情景价与空间由已保存假设计算，不是承诺收益或自动交易指令。')
    clean['source_fingerprints'] = {sid: source_fingerprint(catalog[sid], run) for sid in clean['evidence_ids']}
    return clean
