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
            'open_question_count': len(open_questions), 'track_open_questions': deep_request, 'pending_obligations': obligations,
            'task_observation': {k: observed.get(k) for k in ('search_calls', 'search_results', 'body_sources',
                'document_pages', 'telemetry_incomplete', 'status')},
            'next_action': (pending_questions[0].get('next_action') or pending_questions[0].get('question')) if pending_questions else None,
            'instruction': '继续已经授权的未完成事项。优先检查现有原文能否回答关键缺口并更新问题；资料不足再定向检索。'
                           '报告已保存不代表研究完成；不要重复请求继续或保存授权。每轮有界推进，不要求强行回答所有问题。'}


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
          'question_progress': '关键研究问题仍待查证，本轮尚未取得新的查证进展'}
