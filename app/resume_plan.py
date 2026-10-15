"""Observable research obligations shared by planning and continuation guards.

Saved reports are artifacts, not proof that retrieval or unresolved questions
were investigated. These guards require bounded progress, never an invented
answer or indefinitely repeated research.
"""
import re

from .turn_constraints import coverage, effective_rules, enforce, task_request, task_steps, canonical_url
from . import questions as research_questions

RESEARCH_TOOLS = {'web_search', 'read_webpage', 'read_web_page', 'read_public_pdf',
                  'search_documents', 'read_document', 'update_research_question'}

# These are discovery targets, never a financial verification engine. A matched
# row only means there is material to inspect; the model must retain the entity,
# period, unit and consolidated/parent scope when answering.
DOCUMENT_METRICS = (
    ('营业收入', r'营业收入|营收'),
    ('归母净利润', r'归母净利润|归属于(?:上市公司|母公司)(?:股东|所有者)(?:的)?净利润'),
    ('经营现金流', r'经营现金流|经营(?:活动)?(?:产生的)?现金流量净额'),
    ('资本开支', r'资本开支|购建固定资产.{0,12}支付的现金'),
    ('合同负债', r'合同负债'),
    ('总资产', r'总资产|资产总计'),
    ('总负债', r'总负债|负债合计'),
)


def document_checks(chat):
    """Track each requested PDF metric instead of treating one page as done."""
    text = task_request(chat)
    if not re.search(r'年报|报告|PDF|pdf|原文|已接入.{0,4}材料', text): return []
    if not re.search(r'核对|逐页|按页|查阅|读取|列出', text): return []
    if re.search(r'怎么|如何|怎样|方法是什么', text) and not re.search(r'请帮|帮我|替我|逐页', text): return []
    selected = [(label, pattern) for label, pattern in DOCUMENT_METRICS if re.search(pattern, text)]
    if len(selected) < 2: return []
    years = list(dict.fromkeys(re.findall(r'20\d{2}', text)))
    comparison = len(years) > 1 or bool(re.search(r'同比|上年|去年|两年', text))
    bodies = []
    for step in activity_steps(task_steps(chat)):
        if step.get('tool') not in ('read_document', 'read_public_pdf') or step.get('state') != 'completed': continue
        result = step.get('result') or {}
        if result.get('error') or result.get('status') in ('error', 'failed', 'needs_ocr'): continue
        bodies.extend(re.sub(r'\s+', '', s.get('text', '')) for s in result.get('snippets', []) if s.get('text'))
    checks = []
    for label, pattern in selected:
        # A comparison needs at least two numeric cells near the requested row;
        # an audit paragraph mentioning a single revenue figure is insufficient.
        found = any((not comparison or (years and all(y in body for y in years)) or re.search(r'上期|上年|去年|同期', body))
                    and len(re.findall(r'(?<!\d)[-−]?\d[\d,]*(?:\.\d+)?', body[m.end():m.end()+160])) >= (2 if comparison else 1)
                    for body in bodies for m in re.finditer(pattern, body))
        checks.append({'metric': label, 'pattern': pattern, 'years': years,
                       'comparison_required': comparison, 'read_candidate_available': found})
    return checks


def unfinished_response(message):
    """A final answer cannot promise that the same idle worker will continue."""
    if re.search(r'确认后|提供后|收到后|如果你|如需|若你', message): return False
    return bool(re.search(r'(?:下一步|接下来|随后).{0,10}(?:我会|我将|我继续|我再)|'
                          r'(?:还|仍)(?:需|需要).{0,6}继续(?:查阅|读取|核对|检索)|'
                          r'请稍等.{0,12}(?:继续|查阅|核对)', message))


def clarification_response(message):
    # Missing scope/identity is different from requesting permission to keep
    # doing work the user already authorized.
    return bool(re.search(r'(?:请确认|您指|你指).{0,50}(?:哪|母公司|合并|年度|期间|口径|公司名称|证券代码).{0,35}[？?]', message))


def document_recovery(chat, run, current, all_steps):
    """Find missing targets locally; never invent pages or relax user policy."""
    from . import documents
    docs = documents.attached_documents(chat)
    allowed_ids = {doc['id'] for doc in docs}
    if not allowed_ids: return None
    pending = [c for c in document_checks(chat) if not c['read_candidate_available']]
    searches = [s for s in current if s.get('tool') == 'search_documents']
    tried = set()
    for step in all_steps:
        if step.get('tool') != 'read_document': continue
        if step.get('turn_id') != chat.get('turn_id') and step.get('state') != 'completed': continue
        args = step.get('arguments', {})
        page = args.get('page')
        if isinstance(page, int):
            tried.update((args.get('document_id'), page + i) for i in range(args.get('count', 1)))
    candidates = []
    for step in reversed(all_steps):
        if step.get('tool') == 'search_documents':
            candidates.extend((step.get('result') or {}).get('snippets', []))
    # Keep saved sources as available page pointers, not as proof this new task
    # has already inspected the corresponding row.
    candidates.extend((run or chat).get('research_sources', []))
    ranked = []
    for source in candidates:
        match = re.fullmatch(r'/api/documents/([\w-]+)/file#page=(\d+)', source.get('url', ''))
        docid = source.get('document_id') or (match[1] if match else None)
        page = source.get('page') or (int(match[2]) if match else None)
        if docid not in allowed_ids or not isinstance(page, int) or (docid, page) in tried: continue
        body = re.sub(r'\s+', '', source.get('text', ''))
        score = sum(bool(re.search(c['pattern'], body)) for c in pending)
        if score: ranked.append((score, docid, page))
    # First use a targeted search to avoid the old arbitrary first-source read.
    if searches and ranked:
        _, docid, page = sorted(ranked, key=lambda row: -row[0])[0]
        try: enforce(chat, 'read_document')
        except ValueError: return None
        return {'action': 'tool', 'tool': 'read_document',
                'arguments': {'document_id': docid, 'page': page, 'count': 1},
                'message': '继续逐项核对尚缺财务指标的年报原文和对比期间'}
    if len(searches) < 2:
        query_names = {'归母净利润': '归属于上市公司股东的净利润', '经营现金流': '经营活动产生的现金流量净额'}
        queries = [query_names.get(c['metric'], c['metric']) for c in pending][:6]
        used = {q for step in searches for q in step.get('arguments', {}).get('queries', [])}
        queries = [q for q in queries if q not in used]
        if queries:
            try: enforce(chat, 'search_documents')
            except ValueError: return None
            return {'action': 'tool', 'tool': 'search_documents', 'arguments': {'queries': queries},
                    'message': '定位各项待核对指标的原文页，避免只读到一项就结束'}
    if ranked:
        _, docid, page = sorted(ranked, key=lambda row: -row[0])[0]
        try: enforce(chat, 'read_document')
        except ValueError: return None
        return {'action': 'tool', 'tool': 'read_document',
                'arguments': {'document_id': docid, 'page': page, 'count': 1},
                'message': '继续读取已有资料中尚未核对的指标页'}
    return None


def activity_steps(steps):
    """Individual completed retrievals survive an enclosing report failure."""
    expanded, seen = [], set()
    for step in steps:
        if step.get('reused_from_step_id'): continue
        expanded.append(step)
        for event in step.get('retrieval_events', []):
            if event.get('id') and event['id'] in seen: continue
            if event.get('id'): seen.add(event['id'])
            expanded.append(dict(event, turn_id=step.get('turn_id'),
                tool='web_search' if event.get('kind') == 'search' else event.get('kind')))
    return expanded


def search_requested(text):
    """Recognize execution, excluding questions about the search capability."""
    for clause in re.split(r'[。；;\n，,]', text):
        if not re.search(r'(?:联网|上网)?搜索|联网查证|联网查找|联网检索', clause):
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


def plan(chat, run=None):
    run = run or {}
    ctx = chat.get('turn_contexts', {}).get(chat.get('turn_id'), {})
    rules = effective_rules(chat)
    text = task_request(chat)
    steps = task_steps(chat)
    current = [s for s in activity_steps(steps) if s.get('turn_id') == chat.get('turn_id')]
    observed = coverage(steps, text, run.get('research_sources', chat.get('research_sources', [])),
                        run=run or None, require_body=rules['require_body'])
    previous = ctx.get('resume_pending')
    if previous is None:
        previous = next((m.get('delivery', {}).get('pending', []) for m in reversed(chat.get('messages', []))
                         if m.get('role') == 'assistant' and m.get('turn_id') == ctx.get('resume_from_turn_id')), [])
    question_items = research_questions.snapshot(run)['items'] if run.get('request') else []
    open_questions = [q for q in question_items
                      if not q.get('archived') and (q.get('status') != 'supported' or q.get('needs_review'))]
    ordered = sorted(open_questions, key=lambda q: {'high': 0, 'medium': 1, 'low': 2}.get(q.get('priority'), 1))
    pending_questions = [{k: q.get(k) for k in ('id', 'question', 'status', 'priority', 'answer', 'gaps', 'next_action', 'bindings')}
                         for q in ordered[:5]]
    obligations = []
    explicit_search = search_requested(text)
    if explicit_search and not rules['no_network'] and not rules['no_tools'] and not observed['search_calls']:
        obligations.append('search')
    # Existing source inventory alone does not prove that this task read it.
    if rules['require_body'] and not (observed['body_sources'] or observed['document_pages']):
        obligations.append('body')
    checks = document_checks(chat)
    if any(not c['read_candidate_available'] for c in checks):
        obligations.append('document_targets')
    def useful(step):
        result = step.get('result') or {}
        if step.get('state') != 'completed' or result.get('error') or result.get('status') in ('empty', 'error', 'failed', 'needs_ocr'):
            return False
        name = step.get('tool')
        if name == 'update_research_question': return True
        if name == 'web_search': return bool(result.get('results'))
        if name in ('read_webpage', 'read_web_page'): return bool(result.get('text'))
        if name in ('read_public_pdf', 'read_document', 'search_documents'):
            return any(s.get('text') for s in result.get('snippets', result.get('results', [])))
        return False
    progressed = any(useful(s) for s in current)
    report_edit_only = bool(re.search(r'更新|修订|改写|修改', text) and
                            not re.search(r'开展|进行.*研究|深入|联网|搜索|补充|重新研究', text))
    deep_request = bool(re.search(r'深度.*研究|完整.*研究|完整.*备忘录', text)) and not report_edit_only
    if ctx.get('resume_from_turn_id') and previous and deep_request and pending_questions and not progressed:
        obligations.append('question_progress')
    return {'original_request': text, 'previous_pending': previous or [], 'pending_questions': pending_questions,
            'document_checks': [{k: v for k, v in c.items() if k != 'pattern'} for c in checks],
            'open_question_count': len(open_questions), 'track_open_questions': deep_request, 'pending_obligations': obligations,
            'task_observation': {k: observed.get(k) for k in ('search_calls', 'search_results', 'body_sources',
                'document_pages', 'telemetry_incomplete', 'status')},
            'next_action': (pending_questions[0].get('next_action') or pending_questions[0].get('question')) if pending_questions else None,
            'instruction': '继续已经授权的未完成事项。优先检查现有原文能否回答关键缺口并更新问题；资料不足再定向检索。'
                           '报告已保存不代表研究完成；不要重复请求继续或保存授权。每轮有界推进，不要求强行回答所有问题。'
                           'document_checks 仅表示读到了待核对的候选行，不能据此声称核验。逐项回答所需年度、同比、单位、合并/母公司口径及物理页码；'
                           '未回答事项仍有可读材料时继续查阅。资料真实缺失、歧义或不可用时明确阻碍；最终回复后不会自动继续后台执行。'}


def recovery(chat, run, research_plan):
    """Choose at most two actual attempts per kind, only within human limits."""
    pending = research_plan['pending_obligations']
    if not pending:
        return None
    rules = effective_rules(chat)
    if rules['no_tools']:
        return None
    all_steps = activity_steps(task_steps(chat))
    current = [s for s in all_steps if s.get('turn_id') == chat.get('turn_id') and not s.get('reused_from_step_id')]
    if 'document_targets' in pending:
        # Each distinct objective can need a different page. The old two-tool
        # correction allowance must not count one successful read as all goals.
        return document_recovery(chat, run, current, all_steps)
    attempts = sum(s.get('tool') in RESEARCH_TOOLS for s in current)
    # Stop after two corrective retrieval attempts; the model can report the
    # exact remaining gaps rather than repeatedly retrying inaccessible sites.
    if attempts >= 2:
        return None

    def permitted(name):
        try:
            enforce(chat, name)
            return True
        except ValueError:
            return False

    def search_action():
        company = (run or {}).get('company') or (run or {}).get('request', {}).get('ticker', '')
        questions = research_plan['pending_questions']
        used = {q for s in all_steps if s.get('tool') == 'web_search' and (s.get('result') or {}).get('results')
                for q in s.get('arguments', {}).get('queries', [])}
        queries = [(company + ' ' + (q.get('next_action') or q['question']))[:240].strip() for q in questions]
        if not queries: queries = [(company + ' ' + research_plan['original_request'])[:240].strip()]
        query = next((q for q in queries if q and q not in used), None)
        return ({'action': 'tool', 'tool': 'web_search', 'arguments': {'queries': [query], 'max_results': 8},
                 'message': '继续检索尚未查证的关键问题，取得可阅读的原文线索'} if query else None)

    if 'search' in pending and permitted('web_search'):
        action = search_action()
        if action: return action

    read_tools = {'read_webpage', 'read_web_page', 'read_public_pdf', 'read_document'}
    prior_reads = [s for s in all_steps if s.get('turn_id') == chat.get('turn_id') or
                   (s.get('state') == 'completed' and not (s.get('result') or {}).get('error'))]
    tried_urls = {canonical_url(s.get('arguments', {}).get('url', '')) for s in prior_reads if s.get('tool') in read_tools}
    tried_pages = {(s.get('arguments', {}).get('document_id'), s.get('arguments', {}).get('page'))
                   for s in prior_reads if s.get('tool') == 'read_document'}
    # Fresh search hits first, then existing evidence and links. A continuation
    # with no new steps therefore still has real, already observed candidates.
    candidates = []
    for step in reversed(all_steps):
        result = step.get('result') or {}
        if step.get('tool') in ('web_search', 'search_documents'):
            candidates.extend(result.get('results', result.get('snippets', [])))
    holder = run or chat
    candidates.extend(holder.get('research_links', []))
    candidates.extend(holder.get('research_sources', []))
    for source in candidates:
        url = source.get('url', '')
        local = re.fullmatch(r'/api/documents/([\w-]+)/file#page=(\d+)', url)
        if local and permitted('read_document'):
            docid, page = local[1], int(local[2])
            if (docid, page) not in tried_pages:
                return {'action': 'tool', 'tool': 'read_document', 'arguments': {'document_id': docid, 'page': page, 'count': 1},
                        'message': '继续核对已保存原文，补充未完成问题的依据'}
        if not url.startswith(('https://', 'http://')) or canonical_url(url) in tried_urls:
            continue
        name = 'read_public_pdf' if re.search(r'\.pdf(?:[?#]|$)', url, re.I) else 'read_webpage'
        if permitted(name):
            return {'action': 'tool', 'tool': name, 'arguments': {'url': url},
                    'message': '继续阅读已取得的原文线索，核对未完成的研究问题'}
    if 'question_progress' in pending and permitted('web_search'):
        return search_action()
    return None


LABELS = {'search': '尚未完成本任务要求的联网搜索', 'body': '尚未确认本任务已查阅可用原文',
          'document_targets': '部分指定财务指标或对比期间尚未逐项查阅原文，不能以读过一页代替全部核对',
          'question_progress': '关键研究问题仍待查证，本轮尚未取得新的查证进展'}
