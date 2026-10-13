"""User limits and observed coverage, independent of model-authored tool arguments."""
import re
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

NEG = r'(?:不要|不得|不能|不用|无需|不必|禁止|别|暂不|先不|不再|不)'
REPORT = r'(?:报告|研报|底稿|备忘录|研究结果)'


def latest_user(chat):
    return next((m.get('content', '') for m in reversed(chat.get('messages', [])) if m.get('role') == 'user'), '')


def constraints(text):
    clauses = re.split(r'[。；;\n，,、]', text)
    no_tools = bool(re.search(r'(?:不(?:使用|用|调用)|不要(?:调用|使用)|无需(?:调用|使用)|禁止(?:调用|使用))(?:任何)?工具', text))
    saved_only = bool(re.search(r'(?:只|仅)(?:能|用|依据|根据|使用|基于).{0,24}(?:已存|已保存|已经保存|已有|现有|保存的).{0,8}(?:材料|资料|来源|研究|内容)', text))
    no_network = saved_only or any(re.search(NEG + r'.{0,6}(?:联网|上网|搜索|查新资料|检索新资料|补充新资料)', c) for c in clauses)
    no_report = any(re.search(NEG + r'.{0,4}(?:生成|保存|更新|修改|修订|改写|重写|改|写).{0,12}' + REPORT, c) or re.search(REPORT + r'.{0,4}' + NEG + r'.{0,4}(?:改|写|保存|更新|修订)', c) for c in clauses)
    no_calculation = any(re.search(NEG + r'.{0,6}(?:计算|重算|估值)', c) for c in clauses)
    no_valuation = any(re.search(NEG + r'(?:做|进行|保存)?估值', c) for c in clauses)
    require_body = any(re.search(r'(?:打开|读取|阅读|查阅|核对).{0,10}(?:正文|原文|全文)', c) and not re.search(NEG + r'.{0,8}(?:打开|读取|阅读|查阅|核对)', c) for c in clauses)
    match = re.search(r'最多(?:用|使用)?\s*([一二两三四五六七八九\d]+)\s*(?:组|次)(?:联网)?搜索', text)
    number = {'一':1, '二':2, '两':2, '三':3, '四':4, '五':5, '六':6, '七':7, '八':8, '九':9}
    search_limit = (int(match[1]) if match[1].isdigit() else number.get(match[1], 2)) if match else None
    return dict(no_tools=no_tools, saved_only=saved_only, no_network=no_network, no_report=no_report,
                no_calculation=no_calculation, no_valuation=no_valuation, require_body=require_body, search_limit=search_limit)


def prohibited_reason(rules, name):
    if rules['no_tools']:
        return '本轮用户明确要求不使用工具，未执行此工具。'
    if rules['no_network'] and name in {'search_company','start_research','fetch_annual_report','web_search','read_webpage','read_web_page','read_public_pdf','build_investment_memo'}:
        return '本轮用户限定只用已有材料或禁止联网，未执行此工具；可查阅已接入材料或说明缺口。'
    if rules['no_report'] and name in {'build_investment_memo','refine_investment_memo','revise_report','start_research'}:
        return '本轮用户明确禁止生成或修改报告，未执行保存。'
    if (rules['no_calculation'] or rules['no_valuation']) and name in {'calculate_valuation','calculate_proposal'}:
        return '本轮用户明确禁止计算或保存估值，未执行计算。'
    if rules['no_valuation'] and name == 'propose_valuation':
        return '本轮用户明确禁止估值，未生成估值方案。'
    return None


def enforce(chat, name):
    rules = constraints(latest_user(chat))
    reason = prohibited_reason(rules, name)
    if reason:
        raise ValueError(reason)
    if name == 'web_search' and rules['search_limit'] is not None:
        done = sum(s.get('turn_id') == chat.get('turn_id') and s.get('tool') == 'web_search' and s.get('state') == 'completed' for s in chat.get('steps', []))
        if done >= rules['search_limit']:
            raise ValueError('已达到用户限定的搜索次数，请查阅已找到的正文或说明缺口。')
    if name in ('plan_research_questions','update_research_question') and re.search(r'只读|不(?:要)?保存|不(?:要)?修改',latest_user(chat)):
        raise ValueError('本轮只读要求禁止修改研究问题记录')
    return rules


def canonical_url(url):
    p = urlsplit(url)
    return urlunsplit(('https' if p.scheme in ('http','https') else p.scheme, p.netloc.lower(), p.path.rstrip('/'),
                       urlencode([(k,v) for k,v in parse_qsl(p.query) if not k.lower().startswith('utm_')]), ''))


def coverage(steps, text='', saved_sources=(), message=''):
    """A successful call is not necessarily a body: require returned usable text."""
    sources = {}; search_calls = 0; search_results = 0; local_pages = set(); attempts = 0
    for step in steps:
        name = step.get('tool'); result = step.get('result') or {}; args = step.get('arguments') or {}
        if not isinstance(result, dict): continue
        if name == 'web_search':
            if step.get('state') == 'completed': search_calls += 1
            search_results += len(result.get('results', []))
        if name in ('read_webpage','read_web_page','read_public_pdf'): attempts += 1
        if name == 'read_document' and not result.get('error'):
            local_pages.update(s.get('url') for s in result.get('snippets', []) if s.get('text', '').strip())
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
            body = name != 'web_search' and step.get('state') not in ('failed','cancelled') and bool(row.get('text','').strip()) and not result.get('error') and result.get('status') not in ('error','excluded','excluded_after_cutoff','needs_ocr','document_link') and row.get('read_status') not in ('failed','excluded','pdf_link_only','search_snippet','snippet_only')
            status = 'body_read' if body else 'failed' if name not in ('web_search','build_investment_memo') else 'search_snippet'
            prior = sources.get(key)
            value = {k:row.get(k) for k in ('title','published_at','source_type')}
            value.update(url=url, read_status=status, cited=url in message or key in message)
            if prior and prior['read_status'] == 'body_read': continue
            if prior and status == 'search_snippet': continue
            sources[key] = value
    bodies = sum(s['read_status'] == 'body_read' for s in sources.values())
    cited_urls = {canonical_url(url) for url in re.findall(r'''(?:https?://|/api/documents/)[^\s<>()\[\]"'，。；]+''', message)}
    read_this_turn = {url for url, source in sources.items() if source['read_status'] == 'body_read'}
    reused_urls = {canonical_url(s['url']) for s in saved_sources if s.get('url') and s.get('text')
                   and s.get('read_status') in ('body_read','article_text','public_pdf_text','pdf_text','windows_ocr')
                   and (s['url'] in message or canonical_url(s['url']) in cited_urls)}
    reused = len(reused_urls - read_this_turn)
    require_body = constraints(text)['require_body']
    unmet = require_body and not (bodies or local_pages or reused)
    gaps = ['本轮尚未取得可用正文；搜索摘要不满足正文查证要求。'] if unmet else []
    unread = sum(s['read_status'] != 'body_read' for s in sources.values())
    return dict(search_calls=search_calls, search_results=search_results, body_sources=bodies, document_pages=len(local_pages),
                body_attempts=attempts, reused_cited_sources=reused, saved_sources_available=len(saved_sources),
                unread_sources=unread, failed_sources=sum(s['read_status']=='failed' for s in sources.values()),
                require_body=require_body, status='partial' if unmet else 'observed', gaps=gaps,
                sources=sorted(sources.values(), key=lambda s:(s['read_status']!='body_read',not s['cited'])))
