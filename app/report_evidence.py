"""Keep cited observed passages available while a report is edited.

Priority is a context-selection hint, not verification of the draft's claims.
Only sources already admitted by the source catalog can be selected.
"""
from collections import defaultdict
import re


# These are literal accounting labels, not inferred financial answers. A label
# must occur in an active human/model research question before it can affect
# selection, and a candidate must contain a real numerical row or note table.
METRIC_LABELS = {
    '营业收入': ('营业收入',), '合同负债': ('合同负债',),
    '应收账款': ('应收账款',), '存货': ('存货',), '货币资金': ('货币资金',),
    '经营活动产生的现金流量净额': ('经营活动产生的现金流量净额', '经营现金流', '经营活动现金流'),
    '归属于上市公司股东的净利润': ('归母净利润', '归母净利', '归属于上市公司股东的净利润'),
    '销售费用': ('销售费用',), '管理费用': ('管理费用',), '研发费用': ('研发费用',),
    '信用减值损失': ('信用减值损失',), '资产减值损失': ('资产减值损失',),
}


def metric_passages(run, refs):
    """Find at most two literal statement/note passages per active metric.

    This is a retrieval hint only: preserve source scope and ask the model to
    verify it. Mentioning a metric in an audit procedure is not a value row.
    """
    active = [q for q in run.get('research_questions', []) if not q.get('archived')]
    intent = '\n'.join(str(q.get(k) or '') for q in active for k in ('question', 'completion_criteria'))
    year = str(run.get('request', {}).get('year') or '')
    amount = r'[-+]?\d[\d,]*\.\d{2}(?!\d)'
    results = []
    for label, aliases in METRIC_LABELS.items():
        if not any(alias in intent for alias in aliases):
            continue
        candidates = []
        for sid, source in refs.items():
            if source.get('kind') != 'pdf' or source.get('read_status') not in (
                    'pdf_text', 'public_pdf_text', 'windows_ocr'):
                continue
            body = source.get('text', '') or ''
            lines = body.splitlines(keepends=True)
            offset = 0
            best = None
            for i, line in enumerate(lines):
                # Whole line starts with the metric and then the optional note
                # number and amounts; exclude related-party names and prose.
                row = bool(re.match(r'^\s*' + re.escape(label) + r'\s+(?:\d+\s+)?' + amount, line)
                           and len(re.findall(amount, line)) >= 2)
                heading = bool(re.fullmatch(r'\s*(?:\d+[、.．]\s*)?' + re.escape(label) + r'\s*', line))
                block = ''.join(lines[i:i+9]) if heading else line
                table = heading and ('单位' in block or '期末' in block or '本期' in block) and len(re.findall(amount, block)) >= 2
                if row or table:
                    score = 2 if table else 1
                    if best is None or score > best[0]:
                        best = (score, body[offset:offset+len(block)])
                offset += len(line)
            if best:
                title_and_header = str(source.get('title') or '') + body[:200]
                annual = bool(year and re.search(re.escape(year) + r'\s*年?\s*年度报告', title_and_header))
                candidates.append(((annual, best[0], source.get('last_used_at') or source.get('retrieved_at') or ''),
                                   sid, best[1], re.sub(r'\s+', '', body)))
        seen = set()
        for _, sid, quote, fingerprint in sorted(candidates, reverse=True):
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            results.append((sid, quote))
            if len(seen) == 2:
                break
    return results


def priorities(run, refs, drafts=()):
    ids, quotes = [], defaultdict(list)
    sequence = lambda value: value if isinstance(value, (list, tuple)) else ()

    def add(sid, quote=None):
        if not isinstance(sid, str):
            return
        source = refs.get(sid)
        if source is None:
            return
        if sid not in ids:
            ids.append(sid)
        # Never carry an invented or stale draft quote into the evidence text.
        if isinstance(quote, str) and quote and quote in source.get('text', ''):
            if quote not in quotes[sid]:
                quotes[sid].append(quote)

    def report(value):
        if not isinstance(value, dict):
            return
        theses, sections = value.get('theses'), value.get('sections')
        if isinstance(sections, dict):
            sections = list(sections.values())
        entries = (theses if isinstance(theses, list) else []) + (sections if isinstance(sections, list) else [])
        for item in entries:
            if isinstance(item, dict):
                for sid in sequence(item.get('evidence_ids')):
                    add(sid)
        for answer in sequence(value.get('question_answers')):
            if isinstance(answer, dict):
                for binding in sequence(answer.get('bindings')):
                    if isinstance(binding, dict):
                        add(binding.get('source_id'), binding.get('quote'))
        for metric in sequence(value.get('tracking_metrics')):
            if isinstance(metric, dict):
                for sid in sequence(metric.get('evidence_ids')):
                    add(sid)
                baseline = metric.get('baseline')
                if isinstance(baseline, dict):
                    for sid in sequence(baseline.get('evidence_ids')):
                        add(sid, baseline.get('quote'))
                    add(baseline.get('source_id'), baseline.get('quote'))

    for draft in drafts:
        report(draft)
    for sid, quote in metric_passages(run, refs):
        add(sid, quote)
    for question in run.get('research_questions', []):
        if question.get('archived'):
            continue
        for binding in sequence(question.get('bindings')):
            if isinstance(binding, dict):
                add(binding.get('source_id'), binding.get('quote'))
    report(run.get('memo'))
    # A previous repair may already have dropped a correctly cited passage.
    # Recover the latest observed source IDs, never the draft's numerical claims.
    attempts = run.get('deep_research_attempts', [])
    prior = next((a for a in reversed(attempts) if
                  any(isinstance(a.get(k), dict) and a[k] for k in ('draft', 'repaired_draft'))), None)
    if prior:
        report(prior.get('draft'))
        report(prior.get('repaired_draft'))

    # A continuing table may put its units/column dates on the preceding page.
    # Include that page only if it was actually read and passed date filtering.
    by_page = {((s.get('url') or '').split('#')[0], s.get('page')): s['id']
               for s in refs.values() if s.get('kind') == 'pdf' and s.get('url') and isinstance(s.get('page'), int)}
    for sid in list(ids):
        source = refs[sid]
        page = source.get('page')
        if source.get('kind') == 'pdf' and isinstance(page, int):
            prior = by_page.get(((source.get('url') or '').split('#')[0], page - 1))
            if prior:
                add(prior)
    return ids, quotes


def excerpt(source, limit, quotes=()):
    """Return literal bounded text, retaining cited passages beyond the prefix."""
    body = source.get('text', '') or ''
    limit = max(0, limit)
    if len(body) <= limit:
        return {'text': body}
    separator = '\n\n[原文中间部分省略；不可跨段拼接引用]\n\n'
    valid = sorted({body.find(q): q for q in quotes
                    if isinstance(q, str) and q and q in body}.items())
    # A prefix preserves the page heading/date/unit when available. A single
    # contiguous window around each quote preserves the actual row semantics.
    ranges = [(0, min(500, limit // 3))] if valid else [(0, limit)]
    remaining = limit - sum(b - a for a, b in ranges)
    for position, quote in valid:
        if any(a <= position and position + len(quote) <= b for a, b in ranges):
            continue
        available = remaining - len(separator)
        if available < len(quote):
            continue
        start = max(ranges[-1][1], position - min(180, (available - len(quote)) // 2))
        end = min(len(body), start + min(available, max(800, len(quote) + 360)))
        if start <= position and end >= position + len(quote):
            ranges.append((start, end))
            remaining -= end - start + len(separator)
    # Use remaining room for the prefix without displacing a selected quote.
    start, end = ranges[0]
    extension = min(remaining, (ranges[1][0] if len(ranges) > 1 else len(body)) - end)
    ranges[0] = (start, end + extension)
    text = separator.join(body[a:b] for a, b in ranges if b > a)
    return {'text': text, 'text_partial': True, 'original_text_chars': len(body),
            'excerpt_ranges': [{'start': a, 'end': b} for a, b in ranges if b > a],
            'excerpt_note': '仅省略上下文，不代表未取得原文；数字须保持原文期间、单位与口径，缺表头应明确待核对。'}
