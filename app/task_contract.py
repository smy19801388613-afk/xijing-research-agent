"""Versioned human task scope shared by planning, permissions and delivery.

Natural-language parsing is an input adapter. Consumers use this resolved value
instead of independently choosing the original request or the latest message.
Resolving a legacy conversation is read-only and never rewrites its history.
"""
from copy import deepcopy
import re

SCHEMA_VERSION = 1
PURE_CONTINUATIONS = {'继续', '继续未完成部分', '继续查证', '继续未完成的研究'}


def is_pure_continuation(text):
    return text.strip().rstrip('。.!！') in PURE_CONTINUATIONS


def search_requested(text):
    """An affirmative execution request, not a capability question or ban."""
    from .turn_constraints import NEG
    for clause in re.split(r'[。；;\n，,]', text):
        match = re.search(r'(?:联网|上网)?搜索|联网查证|联网查找|联网检索', clause)
        if not match:
            continue
        if re.search(NEG + r'.{0,12}$', clause[:match.start()]):
            continue
        capability = re.search(r'(?:如何|怎么|怎样).{0,8}(?:使用|配置|开启|设置|调用)|'
                               r'(?:是否|能否|能不能).{0,6}(?:联网|搜索)|'
                               r'(?:你|系统|[Aa]gent).{0,5}(?:能|可以|支持).{0,6}(?:联网|搜索)', clause)
        if capability and not re.search(r'帮我|替我|搜索一下|查一下', clause):
            continue
        if re.search(r'(?:介绍|解释|说明).{0,10}(?:联网|搜索)(?:功能|能力)|(?:联网|搜索)(?:功能|能力).{0,8}(?:是什么|怎么|如何)', clause):
            continue
        return True
    return False


def research_requested(text):
    from .turn_constraints import NEG
    for clause in re.split(r'[。；;\n，,]', text):
        match = re.search(r'(?:(?:开展|进行|重新|继续|完成|补完|补齐).{0,8})?'
                          r'(?:深度.{0,6}研究|完整.{0,6}研究|全面.{0,6}研究|完整.{0,6}备忘录)|重新研究', clause)
        if match and not re.search(NEG + r'.{0,10}$', clause[:match.start()]):
            # Editing the existing complete memo is different from researching it.
            if re.search(r'更新|修订|改写|修改|更正|纠正', clause) and not re.search(r'开展|进行|重新研究|继续.*研究', clause):
                continue
            return True
    return False


def report_edit_requested(text):
    from .turn_constraints import NEG
    for clause in re.split(r'[。；;\n，,]', text):
        match = re.search(r'修订|改写|修改|更正|纠正|定点编辑|精确接续|'
                          r'(?:更新|保存).{0,10}(?:报告|备忘录)|'
                          r'(?:报告|备忘录).{0,8}(?:更新|保存)', clause)
        if match and not re.search(NEG + r'.{0,8}$', clause[:match.start()]):
            return True
    return False


def requested_artifacts(text, rules=None):
    """One set of execution postconditions, also used by the legacy wrapper."""
    from .turn_constraints import constraints
    required = set()
    for clause in re.split(r'[。；;\n，,]', text):
        if re.search(r'怎么|如何|是否|能否|不要|暂不|先不|无需|不必|禁止', clause):
            continue
        if re.search(r'(?:查找|找到|下载|获取|补充|找).{0,20}(?:年报|年度报告)|补充数据', clause):
            required.add('document')
        if re.search(r'(?:读取|阅读|查阅|检索|查看).{0,20}(?:PDF|pdf|文件|年报)|(?:根据|从).{0,20}(?:上传|PDF|pdf).{0,20}(?:查|找|分析)', clause):
            required.add('document_search')
    for clause in re.split(r'[。；\n，,]', text):
        reading = re.search(r'(?:读取|阅读|查阅).{0,60}第\s*\d+\s*页|逐页(?:读取|阅读|查阅)', clause)
        if reading and not re.search(r'不|别|无需|怎么|如何|是否|能否', clause[:reading.start()]):
            required.add('document_read')
    valuation = bool(re.search(r'估值|情景|倍数|\b(?:PE|PS|PB|DCF|DDM|EBITDA)\b', text, re.I))
    for clause in re.split(r'[。；;\n]', text):
        if re.search(r'不要|暂不|先不|不必|无需|如何|怎么|是否', clause): continue
        if re.search(r'(?:计算|生成|保存|执行).{0,25}敏感性|敏感性.{0,15}(?:计算|生成|保存)', clause): required.add('sensitivity')
        if re.search(r'(?:整理|保存|生成).{0,25}可比(?:公司|倍数|依据)', clause): required.add('comparables')
        if re.search(r'(?:登记|保存).{0,15}财务期间', clause): required.add('financial_period_records')
        elif re.search(r'分析.{0,15}财务期间', clause): required.add('financial_period_analysis')
        if re.search(r'(?:拆分|计算).{0,15}(?:各季度|单季|季度同比|季度环比|扣非桥)', clause): required.add('financial_period_analysis')
        if re.search(r'(?:保存|记录|追加).{0,12}(?:跟踪观察|观察记录)', clause): required.add('tracking_observation')
    if valuation and re.search(r'(?:整理|提出|生成|设计|给出).{0,30}(?:方案|参数)|方案卡片', text):
        if not re.search(r'不要.{0,8}(?:方案|卡片)|暂不.{0,8}(?:方案|卡片)', text):
            required.add('proposal')
    for clause in re.split(r'[。；;\n，,、]', text):
        report = re.search(r'(?:生成|撰写|更新|修改|修订|改写|重写|写进|写入|保存).{0,18}(?:报告|研报|底稿|备忘录|研究结果)|(?:报告|研报|底稿|备忘录).{0,8}(?:更新|修改|修订|改为|改成|重写|保存)', clause)
        if report:
            prefix = clause[max(0, report.start()-8):report.start()]
            if not re.search(r'不要|暂不|先不|不必|无需|别|如何|怎么|是否', prefix+report.group(0)[:3]) and not re.search(r'不(?:再|直接|自动|擅自)?\s*$', prefix):
                required.add('report')
        calculation = re.search(r'重新计算|重算|计算并保存|实际计算|直接计算', clause)
        if valuation and calculation and not re.search(r'敏感性|可比', clause) and not re.search(r'不要|暂不|先不|不必|无需|别|不(?:再|直接|自动|擅自)?\s*$', clause[max(0, calculation.start()-8):calculation.start()]):
            required.add('valuation')
    if report_edit_requested(text) and re.search(r'报告|备忘录|refine_investment_memo', text):
        if re.search(r'保存新版本|保存新的一版|调用\s*refine_investment_memo|报告保存', text):
            required.add('report')
    return permitted_artifacts(required, rules or constraints(text))


def permitted_artifacts(required, rules):
    result = set(required)
    if rules.get('no_tools'):
        return set()
    if rules.get('no_report'): result.discard('report')
    if rules.get('no_calculation') or rules.get('no_valuation'): result.discard('valuation')
    if rules.get('no_calculation') or rules.get('no_valuation'): result.difference_update(('sensitivity','comparables'))
    if rules.get('no_calculation'): result.difference_update(('financial_period_records','financial_period_analysis'))
    if rules.get('no_material_writes'): result.discard('financial_period_records')
    if rules.get('no_question_writes'): result.discard('tracking_observation')
    if rules.get('no_valuation'): result.discard('proposal')
    if rules.get('no_network') or rules.get('no_material_writes'): result.discard('document')
    if rules.get('no_pdf_read'):
        result.difference_update(('document_search', 'document_read'))
    return result


def resolve_contract(chat, text, prior_context=None, turn_id=None):
    """Return the contract plus policy metadata for a new or continued turn."""
    from .turn_constraints import constraints, resolve_turn_policy
    prior = prior_context or {}
    previous = prior.get('task_contract') or {}
    prior_active = previous.get('active_request') or prior.get('original_request', '')
    local = constraints(text)
    pure = bool(prior and is_pure_continuation(text))
    focused = bool(prior and not pure and (
        local.get('no_tools') or local.get('discussion_only') or
        re.search(r'新的?.{0,6}(?:任务|范围)|本轮(?:仅|只)|这轮(?:仅|只)|定点', text) or
        (report_edit_requested(text) and not research_requested(text))))
    scope = 'new' if not prior else 'continue' if pure else 'refocus' if focused else 'steer'
    inherited = (previous.get('rules') or prior.get('rules')) if prior and not focused else None
    policy = resolve_turn_policy(chat, text, inherited)
    rules = policy['rules']
    original = previous.get('original_request') or prior.get('original_request') or text
    active = prior_active if pure else text if scope in ('new', 'refocus') else prior_active + '\n\n本轮补充要求：\n' + text
    task_ids = list(prior.get('task_turn_ids', []))
    if turn_id and turn_id not in task_ids: task_ids.append(turn_id)
    obligation_ids = list(previous.get('obligation_turn_ids') or prior.get('task_turn_ids', [])) if scope in ('continue', 'steer') else []
    if turn_id and turn_id not in obligation_ids: obligation_ids.append(turn_id)
    if pure and previous.get('completion'):
        completion = deepcopy(previous['completion'])
        completion['required_artifacts'] = sorted(permitted_artifacts(completion['required_artifacts'], rules))
    else:
        completion = {'required_artifacts': sorted(requested_artifacts(active, rules)),
                      'require_search': search_requested(active) and not rules.get('no_network') and not rules.get('no_tools'),
                      'require_body': rules.get('require_body', False) and not rules.get('no_tools'),
                      'track_open_questions': research_requested(active) and not rules.get('no_tools') and not local.get('discussion_only')}
    intent = 'discussion' if local.get('no_tools') or local.get('discussion_only') else 'research' if completion['track_open_questions'] else 'report_edit' if report_edit_requested(active) else 'other'
    if pure: intent = previous.get('intent', intent)
    contract = {'schema_version': SCHEMA_VERSION, 'scope': scope, 'intent': intent,
                'original_request': original, 'active_request': active, 'user_request': text,
                'task_turn_ids': task_ids, 'obligation_turn_ids': obligation_ids,
                'rules': deepcopy(rules), 'completion': completion,
                'history_note': '原任务与全部轮次保留；本轮完成仅按当前已解析目标和限制判断。'}
    return _normalize_completion(contract), policy


def _normalize_completion(value):
    rules, completion = value['rules'], value['completion']
    completion['required_artifacts'] = sorted(permitted_artifacts(completion.get('required_artifacts', []), rules))
    discussion = value['intent'] == 'discussion'
    from .turn_constraints import requires_body_read
    body_requested = requires_body_read(value['active_request']) if discussion else rules.get('require_body')
    completion['require_body'] = bool(body_requested and not rules.get('no_tools')
        and not (rules.get('no_pdf_read') and (rules.get('no_web_read') or rules.get('no_network'))))
    completion['require_search'] = bool(search_requested(value['active_request']) and not rules.get('no_network')
                                        and not rules.get('no_tools'))
    completion['track_open_questions'] = bool(completion.get('track_open_questions') and not rules.get('no_tools')
                                               and not discussion)
    return value


def current_contract(chat, turn_id=None):
    """Resolve one authoritative view, with an adapter for old stored contexts."""
    from .turn_constraints import latest_user
    tid = turn_id or chat.get('turn_id')
    ctx = chat.get('turn_contexts', {}).get(tid, {})
    historical = turn_id is not None and turn_id != chat.get('turn_id')
    matching = next((m.get('content', '') for m in reversed(chat.get('messages', []))
                     if m.get('role') == 'user' and m.get('turn_id') == tid), '')
    text = (ctx.get('user_text') or matching) if historical else latest_user(chat) or ctx.get('user_text', '')
    saved = ctx.get('task_contract')
    if saved and saved.get('schema_version') == SCHEMA_VERSION and saved.get('user_request') == text:
        # Legacy fields are output mirrors once a versioned contract exists.
        # They cannot silently change permissions or completion postconditions.
        return _normalize_completion(deepcopy(saved))
    prior = chat.get('turn_contexts', {}).get(ctx.get('resume_from_turn_id'), {})
    if ctx.get('resume_from_turn_id') and not prior:
        prior = {k: ctx[k] for k in ('original_request', 'task_turn_ids', 'rules') if k in ctx}
        prior['task_turn_ids'] = [x for x in prior.get('task_turn_ids', []) if x != tid]
    # Historical views use the restrictions saved at that time, never a future
    # standing preference. The adapter is read-only and does not migrate the DB.
    policy_chat = {**chat, 'persistent_constraints': {}} if historical else chat
    value, _ = resolve_contract(policy_chat, text or ctx.get('original_request', ''), prior, tid)
    # Older records stored only their shared task lineage (some predate the
    # explicit resume pointer). Do not reset consumed limits or source novelty.
    if not saved and ctx.get('task_turn_ids'):
        value['task_turn_ids'] = list(ctx['task_turn_ids'])
        if value['scope'] != 'refocus':
            value['obligation_turn_ids'] = list(ctx['task_turn_ids'])
    # Legacy task context may contain tool requirements not inferable from a
    # short continuation message. Preserve them only when the scope continues.
    if ctx.get('rules') and value['scope'] in ('new', 'continue', 'steer'):
        value['rules'] = deepcopy(ctx['rules'])
    if value['scope'] in ('continue', 'steer') and ctx.get('required_artifacts'):
        value['completion']['required_artifacts'] = sorted(permitted_artifacts(ctx['required_artifacts'], value['rules']))
    return _normalize_completion(value)


def required_artifacts(chat):
    return set(current_contract(chat)['completion']['required_artifacts'])


def set_required(context, required):
    """Keep the legacy display mirror aligned with the versioned contract."""
    context['required_artifacts'] = sorted(required)
    if context.get('task_contract'):
        context['task_contract']['completion']['required_artifacts'] = sorted(required)
