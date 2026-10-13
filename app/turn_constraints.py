"""User limits and observed coverage, independent of model-authored tool arguments."""
import re
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from .tool_capabilities import TOOLS
from .evidence_policy import eligibility

NEG = r'(?:不要|不得|不能|不用|无需|不必|禁止|别|暂不|先不|不再|不)'
REPORT = r'(?:报告|研报|底稿|备忘录|研究结果)'


def latest_user(chat):
    return next((m.get('content', '') for m in reversed(chat.get('messages', [])) if m.get('role') == 'user'), '')


def constraints(text):
    clauses = re.split(r'[。；;\n，,、]', text)
    no_tools = bool(re.search(r'(?:不要|不用|不使用|不调用|无需|不必|不需要|禁止|别|不得|不能)(?:再)?(?:使用|调用|用)?\s*(?:任何|所有)?\s*工具', text))
    read_only = (bool(re.search(r'只读|(?:只|仅)(?:讨论|解释)|(?:不要|禁止|不需要)(?:保存|修改)任何(?:内容|数据|记录|结果)', text))
                 or any(re.search(NEG+r'(?:再)?(?:保存|写入|修改)(?:任何(?:内容|数据|记录|结果)?)?\s*$',c) for c in clauses))
    saved_only = bool(re.search(r'(?:只|仅)(?:能|用|依据|根据|使用|基于).{0,24}(?:已存|已保存|已经保存|已有|现有|保存的).{0,8}(?:材料|资料|来源|研究|内容)', text))
    no_network = saved_only or any(re.search(NEG + r'.{0,6}(?:联网|上网|搜索|查新资料|检索新资料|补充新资料)', c) for c in clauses)
    no_report = read_only or any(re.search(NEG + r'.{0,4}(?:生成|保存|更新|修改|修订|改写|重写|改|写).{0,12}' + REPORT, c) or re.search(REPORT + r'.{0,4}' + NEG + r'.{0,4}(?:改|写|保存|更新|修订)', c) for c in clauses)
    no_calculation = any(re.search(NEG + r'.{0,6}(?:计算|重算|估值)', c) for c in clauses)
    no_valuation = read_only or any(re.search(NEG + r'(?:做|进行|保存)?估值', c) for c in clauses)
    no_question_writes = read_only or any(re.search(NEG + r'.{0,5}(?:保存|修改|更新|建立|记录).{0,8}(?:问题|研究记录)', c) for c in clauses)
    require_body = any(re.search(r'(?:打开|读取|阅读|查阅|核对).{0,10}(?:正文|原文|全文)', c) and not re.search(NEG + r'.{0,8}(?:打开|读取|阅读|查阅|核对)', c) for c in clauses)
    match = re.search(r'最多(?:用|使用)?\s*([一二两三四五六七八九\d]+)\s*(?:组|次)(?:联网)?搜索', text)
    number = {'一':1, '二':2, '两':2, '三':3, '四':4, '五':5, '六':6, '七':7, '八':8, '九':9}
    search_limit = (int(match[1]) if match[1].isdigit() else number.get(match[1], 2)) if match else None
    return dict(no_tools=no_tools, saved_only=saved_only, no_network=no_network, no_report=no_report,
                no_calculation=no_calculation, no_valuation=no_valuation, no_question_writes=no_question_writes,
                require_body=require_body, search_limit=search_limit)


RULE_LABELS = {'no_tools':'不使用工具', 'saved_only':'仅用已有材料', 'no_network':'不联网',
    'no_report':'不修改报告', 'no_calculation':'不计算', 'no_valuation':'不做估值',
    'no_question_writes':'不修改研究问题', 'require_body':'需查阅正文'}
PERSISTENT = r'以后|今后|从现在起|从此|始终|一直|所有后续|后续每轮'


def explicit_permissions(text):
    """Explicit affirmative requests can override a standing restriction this turn."""
    allowed = set()
    for clause in re.split(r'[。；;\n，,、]', text):
        if re.search(r'取消|解除|撤销',clause):
            if re.search(r'所有限制|全部限制',clause): allowed.update(RULE_LABELS)
            if '工具' in clause: allowed.add('no_tools')
            if re.search(r'联网|搜索|已有材料',clause): allowed.update(('no_network','saved_only'))
            if re.search(REPORT,clause): allowed.add('no_report')
            if '估值' in clause: allowed.update(('no_calculation','no_valuation'))
            if '计算' in clause: allowed.add('no_calculation')
            if re.search(r'问题|研究记录',clause): allowed.add('no_question_writes')
        if re.search(NEG, clause) and not re.search(r'取消|解除|撤销', clause):
            continue
        if re.search(r'(?:联网|上网|搜索|查新资料)', clause): allowed.update(('no_network','saved_only'))
        if re.search(r'(?:使用|调用|用).{0,4}工具', clause): allowed.add('no_tools')
        if re.search(r'(?:生成|保存|更新|修改|修订|写入|重写).{0,12}'+REPORT, clause): allowed.add('no_report')
        if re.search(r'(?:计算|重算|执行|进行).{0,10}(?:估值|情景|桥接|FCFF)|计算', clause, re.I): allowed.add('no_calculation')
        if re.search(r'(?:计算|重算|执行|进行|提出|生成|保存).{0,10}(?:估值|情景|方案)', clause): allowed.add('no_valuation')
        if re.search(r'(?:保存|修改|更新|建立|记录).{0,8}(?:问题|研究记录)', clause): allowed.add('no_question_writes')
    return allowed


def resolve_turn_policy(chat, text, inherited=None):
    """Only explicit standing instructions persist; continuation keeps its task policy."""
    persistent = dict(chat.get('persistent_constraints', {}))
    for clause in re.split(r'[。；;\n，,、]', text):
        if not re.search(PERSISTENT + r'|取消.*限制|解除.*限制|撤销.*限制', clause): continue
        for key in explicit_permissions(clause): persistent.pop(key, None)
        if not re.search(r'取消|解除|撤销', clause):
            persistent.update({k:v for k,v in constraints(clause).items() if v and k in RULE_LABELS})
    rules = constraints('') | persistent
    if inherited: rules.update(inherited)
    for key in explicit_permissions(text): rules[key] = False
    current = constraints(text)
    # A request to cancel a named restriction is permission, not a fresh prohibition.
    if re.search(r'取消|解除|撤销', text):
        for key in explicit_permissions(text): current[key] = False
    rules.update({k:v for k,v in current.items() if v is not False and v is not None})
    labels = [label for key,label in RULE_LABELS.items() if rules.get(key)]
    if rules.get('search_limit') is not None: labels.append(f"最多搜索 {rules['search_limit']} 次")
    return {'scope':'continuation' if inherited is not None else 'turn', 'rules':rules, 'labels':labels,
            'persistent_rules':persistent,
            'persistent_labels':[label for key,label in RULE_LABELS.items() if persistent.get(key)]}


def effective_rules(chat):
    current = chat.get('turn_contexts', {}).get(chat.get('turn_id'), {})
    # Legacy conversations and synthetic test fixtures still resolve from the actual user.
    if current.get('user_text') == latest_user(chat) and current.get('rules'):
        return current['rules']
    return resolve_turn_policy(chat, latest_user(chat))['rules']


def task_request(chat):
    current = chat.get('turn_contexts', {}).get(chat.get('turn_id'), {})
    if current.get('resume_from_turn_id'):
        return current.get('original_request', latest_user(chat))
    return latest_user(chat)


def task_steps(chat):
    current = chat.get('turn_contexts', {}).get(chat.get('turn_id'), {})
    ids = set(current.get('task_turn_ids', [chat.get('turn_id')]))
    return [s for s in chat.get('steps', []) if s.get('turn_id') in ids]


def prohibited_reason(rules, name):
    if rules.get('no_tools'):
        return '本轮用户明确要求不使用工具，未执行此工具。'
    if name not in TOOLS:
        return '未知工具，请使用工具列表中的名称。'
    caps = TOOLS[name]
    if rules.get('no_network') and 'network' in caps:
        return '本轮用户限定只用已有材料或禁止联网，未执行此工具；可查阅已接入材料或说明缺口。'
    if rules.get('no_report') and 'report' in caps:
        return '本轮用户明确禁止生成或修改报告，未执行保存。'
    if rules.get('no_calculation') and 'calculation' in caps:
        return '本轮用户明确禁止计算或保存估值，未执行计算。'
    if rules.get('no_valuation') and 'valuation' in caps:
        return '本轮用户明确禁止估值，未生成估值方案。'
    if rules.get('no_question_writes') and 'questions' in caps:
        return '本轮只读要求禁止修改研究问题记录。'
    return None


def enforce(chat, name):
    rules = effective_rules(chat)
    reason = prohibited_reason(rules, name)
    if reason:
        raise ValueError(reason)
    if name == 'web_search' and rules['search_limit'] is not None:
        done = sum(s.get('tool') == 'web_search' and s.get('state') == 'completed' and not s.get('reused_from_step_id') for s in task_steps(chat))
        if done >= rules['search_limit']:
            raise ValueError('已达到用户限定的搜索次数，请查阅已找到的正文或说明缺口。')
    return rules


def canonical_url(url):
    p = urlsplit(url)
    return urlunsplit(('https' if p.scheme in ('http','https') else p.scheme, p.netloc.lower(), p.path.rstrip('/'),
                       urlencode([(k,v) for k,v in parse_qsl(p.query) if not k.lower().startswith('utm_')]), ''))


def coverage(steps, text='', saved_sources=(), message='', as_of=None, run=None, require_body=None):
    """A successful call is not necessarily a body: require returned usable text."""
    sources = {}; search_calls = 0; search_results = 0; local_pages = set(); attempts = 0
    for step in steps:
        if step.get('reused_from_step_id'): continue
        name = step.get('tool'); result = step.get('result') or {}; args = step.get('arguments') or {}
        if not isinstance(result, dict): continue
        if name == 'web_search':
            if step.get('state') == 'completed': search_calls += 1
            search_results += len(result.get('results', []))
        if name in ('read_webpage','read_web_page','read_public_pdf'): attempts += 1
        if name == 'read_document' and not result.get('error'):
            local_pages.update(s.get('url') for s in result.get('snippets', [])
                               if eligibility(dict(s,read_status=s.get('extraction','pdf_text')),run,as_of)['readable_body'])
        rows = result.get('results', []) if name in ('web_search','read_public_pdf') else [result] if name in ('read_webpage','read_web_page') else result.get('observed_sources',[]) if name=='build_investment_memo' else []
        if name=='build_investment_memo':
            search_calls += len(result.get('search_runs',[]))
            search_results += sum(s.get('result_count',0) for s in result.get('search_runs',[]))
        if name in ('read_webpage','read_web_page','read_public_pdf') and not rows:
            rows = [{'url':args.get('url'), 'read_status':'failed'}]
        for row in rows:
            url = row.get('url') or args.get('url', '')
            if not url or not url.startswith(('http://','https://')): continue
            key = canonical_url(url)
            source=dict(row)
            if name=='web_search': source['read_status']='search_snippet'
            elif not source.get('read_status'): source['read_status']=source.get('extraction','body_read')
            policy=eligibility(source,run,as_of)
            body = (name != 'web_search' and step.get('state') not in ('failed','cancelled') and not result.get('error')
                    and result.get('status') not in ('error','excluded','excluded_after_cutoff','needs_ocr','document_link')
                    and policy['readable_body'] and policy['historical_eligibility']!='after_cutoff')
            status = 'body_read' if body else 'failed' if name not in ('web_search','build_investment_memo') else 'search_snippet'
            prior = sources.get(key)
            value = {k:row.get(k) for k in ('title','published_at','source_type')}
            value.update(url=url, read_status=status, cited=url in message or key in message,eligibility=policy)
            if prior and prior['read_status'] == 'body_read': continue
            if prior and status == 'search_snippet': continue
            sources[key] = value
    bodies = sum(s['read_status'] == 'body_read' for s in sources.values())
    cited_urls = {canonical_url(url) for url in re.findall(r'''(?:https?://|/api/documents/)[^\s<>()\[\]"'，。；]+''', message)}
    read_this_turn = {url for url, source in sources.items() if source['read_status'] == 'body_read'}
    reused_urls = {canonical_url(s['url']) for s in saved_sources if s.get('url')
                   and eligibility(s,run,as_of)['readable_body'] and eligibility(s,run,as_of)['historical_eligibility']!='after_cutoff'
                   and (s['url'] in message or canonical_url(s['url']) in cited_urls)}
    reused = len(reused_urls - read_this_turn)
    if require_body is None: require_body = constraints(text)['require_body']
    unmet = require_body and not (bodies or local_pages or reused)
    gaps = ['本轮尚未取得可用正文；搜索摘要不满足正文查证要求。'] if unmet else []
    unread = sum(s['read_status'] != 'body_read' for s in sources.values())
    return dict(search_calls=search_calls, search_results=search_results, body_sources=bodies, document_pages=len(local_pages),
                body_attempts=attempts, reused_cited_sources=reused, saved_sources_available=len(saved_sources),
                unread_sources=unread, failed_sources=sum(s['read_status']=='failed' for s in sources.values()),
                require_body=require_body, status='partial' if unmet else 'observed', gaps=gaps,
                sources=sorted(sources.values(), key=lambda s:(s['read_status']!='body_read',not s['cited'])))
