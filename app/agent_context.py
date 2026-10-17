"""Small, read-only views for the agent's next decision.

The database remains the evidence archive. Model decisions need current state,
locators and a few excerpts, not every previous report, question revision and
repeated PDF result. All limits operate on JSON values, never serialized JSON.
"""
from copy import deepcopy
import hashlib
import json

from . import questions
from .fact_state import fact_view
from .report_state import report_valuation_status


def size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(',', ':'), default=str))


def _clip(value, limit):
    if not isinstance(value, str):
        return deepcopy(value)
    return value if len(value) <= limit else value[:max(0, limit - 15)] + '…[上下文节选，完整记录保留]'


def _fields(value, names, text_limit=500):
    locators = {'id', 'document_id', 'source_id', 'run_id', 'proposal_id', 'question_id',
                'url', 'source_url', 'locator', 'reused_from_step_id'}
    return {k: deepcopy(value[k]) if k in locators or k.endswith('_id') else _clip(value[k], text_limit)
            for k in names if k in value}


def _fit(value, limit):
    """Bound a projected block; prefer shortening prose to dropping records."""
    value = deepcopy(value)
    prose = {'text', 'quote', 'body', 'summary', 'answer', 'reason', 'rationale',
             'counter_evidence', 'content', 'message', 'note', 'change_note', 'explanation'}
    while size(value) > limit:
        strings, lists = [], []

        def visit(node):
            if isinstance(node, dict):
                for key, child in node.items():
                    if key in prose and isinstance(child, str) and len(child) > 80:
                        strings.append((len(child), node, key))
                    visit(child)
            elif isinstance(node, list):
                if len(node) > 1:
                    lists.append((size(node), node))
                for child in node:
                    visit(child)

        visit(value)
        if strings:
            length, parent, key = max(strings, key=lambda item: item[0])
            parent[key] = _clip(parent[key], max(80, length // 2))
        elif lists:
            # Projectors order important/current records first.
            _, items = max(lists, key=lambda item: item[0])
            items.pop()
        else:
            # Pathological oversized opaque values must not produce malformed JSON.
            return {'context_omitted': True, 'note': '该区块超过上下文预算；完整记录仍保留。'}
    return value


SOURCE_FIELDS = ('id', 'kind', 'title', 'url', 'source_url', 'document_id', 'page',
                 'locator', 'published_at', 'announced_date', 'read_status', 'extraction',
                 'source_type', 'eligibility', 'limitations', 'error')


def compact_sources(sources, limit=4500, count=16, excerpt=450):
    seen, selected = set(), []
    for source in reversed(sources or []):
        if not isinstance(source, dict):
            continue
        key = source.get('id') or (source.get('url'), source.get('page'))
        if key in seen:
            continue
        seen.add(key)
        item = _fields(source, SOURCE_FIELDS, 300)
        text = source.get('text') or source.get('quote') or source.get('snippet')
        if text and excerpt:
            item['text'] = text[:excerpt]
            item['excerpt_only'] = bool(source.get('excerpt_only') or source.get('truncated') or len(text) > excerpt)
        selected.append(item)
        if len(selected) >= count:
            break
    return _fit(selected, limit)


def _question(item, detailed=True):
    result = _fields(item, ('id', 'question', 'completion_criteria', 'status', 'priority',
                           'needs_review', 'archived', 'revision'), 1500)
    if detailed:
        result.update(_fields(item, ('answer', 'counter_evidence', 'next_action'), 450))
        result['gaps'] = [_clip(g, 260) for g in item.get('gaps', [])[:4]]
        result['bindings'] = [_fields(b, ('source_id', 'quote', 'relation', 'reason',
             'url', 'page', 'read_status', 'qualifying', 'eligibility'), 250)
             for b in item.get('bindings', [])[:3]]
    return result


def _coverage(view, limit=6500):
    active = [q for q in view.get('items', []) if not q.get('archived')]
    ordered = sorted(active, key=lambda q: (q.get('status') == 'supported' and not q.get('needs_review'),
                     {'high': 0, 'medium': 1, 'low': 2}.get(q.get('priority'), 1)))
    result = _fields(view, ('total', 'archived_count', 'counts', 'complete', 'note'))
    result['items'] = []
    for index, q in enumerate(ordered):
        item = _question(q, False)
        item['next_action'] = q.get('next_action', '')
        if index < 5:
            item['answer'] = _clip(q.get('answer', ''), 250)
            item['gaps'] = [_clip(g, 180) for g in q.get('gaps', [])[:2]]
            item['bindings'] = [_fields(b, ('source_id', 'quote', 'relation', 'qualifying', 'url', 'page'), 140)
                                for b in q.get('bindings', [])[:1]]
        result['items'].append(item)
    result['context_note'] = '仅列当前问题状态及有限摘录，修订历史和完整证据仍保留；未展示不代表已完成。'
    result['available_question_count'] = len(active)
    fitted = _fit(result, limit)
    # All active question texts and next actions stay visible. A larger question
    # inventory may exceed the soft target rather than silently lose pending work.
    by_id = {q.get('id'): q for q in fitted.get('items', [])}
    fitted['items'] = [by_id.get(q['id'], _fields(q, ('id', 'question', 'completion_criteria',
                      'status', 'priority', 'needs_review', 'next_action'), 1500)) for q in result['items']]
    return fitted


def _memo(memo):
    if not isinstance(memo, dict):
        return _clip(memo, 1500)
    result = _fields(memo, ('title', 'summary', 'stance', 'as_of', 'created_at', 'schema_version'), 1000)
    result['sections'] = [_fields(s, ('id', 'title', 'status', 'evidence_ids'), 150)
                          for s in (memo.get('theses', []) + memo.get('sections', []))[:16]]
    result['limitations'] = [_clip(item, 250) for item in memo.get('limitations', [])[:4]]
    result['context_note'] = '已有报告仅展示摘要与章节索引，不重复注入长正文及冻结历史。'
    return _fit(result, 2500)


def bounded_context(chat, run=None, max_chars=18000):
    """Return current research state without accessing or changing storage."""
    if not run or not chat.get('run_id'):
        return None
    result = _fields(run, ('id', 'company', 'request', 'state', 'report_revision'), 1500)
    result['facts'] = [_fit(fact_view(f, run), 1700) for f in run.get('facts', [])[:15]]
    result['calculations'] = _fit(run.get('calculations', []), 2000)
    result['gaps'] = [_clip(g, 300) for g in run.get('gaps', [])[:10]]
    result['memo'] = _memo(run.get('memo'))
    result['valuations'] = _fit(list(reversed(run.get('valuation_history', [])[-4:])), 1800)
    result['report_valuation_status'] = _fit(report_valuation_status(run), 1600)
    from .research_extensions import context as extension_context
    extensions = extension_context(run)
    if extensions: result['research_extensions'] = _fit(extensions, 4500)
    view = questions.snapshot(run)
    result['question_coverage'] = _coverage(view)
    # Exact source IDs and page locators remain addressable. There is one source
    # view, rather than repeating all of it again in question_evidence_catalog.
    catalog = questions.catalog(run)
    research_ids = [s.get('id') for s in run.get('research_sources', [])]
    refs = [s for sid, s in catalog.items() if sid not in research_ids]
    refs += [catalog[sid] for sid in research_ids if sid in catalog]
    result['research_sources'] = compact_sources(refs, limit=4500)
    result['source_count'] = len(refs)
    result['unread_web_links'] = compact_sources(run.get('research_links', []), limit=1400, count=6, excerpt=0)
    result['context_note'] = '上下文仅保留当前状态及节选；原始研究记录未修改。需要完整原文时按来源网址或文件页码读取。'
    coverage = result.pop('question_coverage')
    fitted = _fit(result, max(2000, max_chars - size(coverage)))
    fitted['question_coverage'] = coverage
    return fitted


def compact_conversation(messages, max_chars=5000):
    """Keep recent intent and the first request, omitting long old answers."""
    rows = [m for m in messages or [] if m.get('role') in ('user', 'assistant')]
    selected = rows[-6:]
    first = next((m for m in rows if m.get('role') == 'user'), None)
    if first and not any(m is first for m in selected):
        selected = [first] + selected
    latest_user = next((m for m in reversed(selected) if m['role'] == 'user'), None)
    result = [{'role': m['role'], 'content': (m.get('content', '') if m is latest_user else
               _clip(m.get('content', ''), 2200 if m['role'] == 'user' else 700)),
               '_context_order': index} for index, m in enumerate(selected)]
    protected = next((m for m in reversed(result) if m['role'] == 'user'), None)
    if size(result) > max_chars:
        others = [m for m in reversed(result) if m is not protected]
        kept = _fit(others, max(200, max_chars - size(protected)))
        result = (kept if isinstance(kept, list) else []) + ([protected] if protected else [])
        result.sort(key=lambda m: m['_context_order'])
    for item in result:
        item.pop('_context_order', None)
    return result


EXTENSION_GROUPS = {
    'get_tracking_metrics': 'tracking', 'record_tracking_observation': 'tracking',
    'record_financial_periods': 'periods', 'analyze_financial_periods': 'periods',
    'calculate_sensitivity': 'sensitivity', 'save_comparables': 'comparables',
}


def _analysis_prose(value, limit):
    """Shorten prose only: never silently drop a metric, identity or numeric input."""
    result = deepcopy(value)
    prose = {'definition', 'thesis', 'support_condition', 'invalidate_condition', 'next_action',
             'next_check', 'frequency', 'observation', 'reason', 'message', 'note', 'status_note',
             'exclusion_reason', 'multiple_basis', 'price_basis', 'denominator_basis', 'restatement_note'}
    while size(result) > limit:
        found = []
        def visit(node):
            if isinstance(node, dict):
                for key, child in node.items():
                    if key in prose and isinstance(child, str) and len(child) > 60: found.append((len(child), node, key))
                    if key == 'quote' and isinstance(child, str) and len(child) > 80: found.append((len(child), node, key))
                    visit(child)
            elif isinstance(node, list):
                for child in node: visit(child)
        visit(result)
        if not found: break  # Bounded records/locators may exceed the soft target.
        length, parent, key = max(found, key=lambda x: x[0])
        if key == 'quote':
            parent[key] = parent[key][:max(80, length // 2)]
            parent['quote_excerpt_only'] = True
        else: parent[key] = _clip(parent[key], max(60, length // 2))
    return result


def _tracking_projection(result):
    output = _fields(result, ('artifact', 'report_identity', 'report_unchanged', 'error'))
    output['metrics'] = []
    for metric in result.get('metrics', [])[:12]:
        item = _fields(metric, ('id', 'name', 'definition', 'thesis', 'frequency', 'next_check',
            'support_condition', 'invalidate_condition', 'threshold_basis', 'threshold_confirmed', 'next_action', 'status'), 600)
        baseline = metric.get('baseline') or {}
        item['baseline'] = _fields(baseline, ('status', 'value', 'period', 'source_id'), 250)
        item['baseline']['quote'] = str(baseline.get('quote') or '')[:500]
        item['baseline']['quote_excerpt_only'] = bool(baseline.get('quote_excerpt_only') or len(str(baseline.get('quote') or '')) > 500)
        output['metrics'].append(item)
    observations = result.get('observations', [])
    output['observations'] = []
    for observation in observations[-3:]:
        item = _fields(observation, ('id', 'metric_id', 'report_identity', 'recorded_at', 'observation',
            'assessment', 'assessment_label', 'origin', 'semantic_status', 'source_id'), 500)
        item['quote'] = str(observation.get('quote') or '')[:500]
        item['quote_excerpt_only'] = bool(observation.get('quote_excerpt_only') or len(str(observation.get('quote') or '')) > 500)
        item['source_snapshot'] = _fields(observation.get('source_snapshot') or {}, SOURCE_FIELDS, 200)
        output['observations'].append(item)
    output['metric_count'] = result.get('metric_count', len(result.get('metrics', [])))
    output['observation_count'] = result.get('observation_count', len(observations))
    output['previous_report_observation_count'] = result.get('previous_report_observation_count', len(result.get('previous_report_observations', [])))
    output['context_note'] = '保留本版本全部跟踪指标和报告标识；原文可能节选，观察仅列最新三条。未展示历史不代表未记录，节选不代表重新阅读全文。'
    return _analysis_prose(output, 10000)


def _period_projection(result):
    output = _fields(result, ('artifact', 'run_id', 'operation', 'request', 'note'), 400)
    records = result.get('records', [])
    already_projected = bool(result.get('analysis_counts'))
    selected = records[:20] if already_projected else records[-20:][::-1]
    output['records'] = [_fields(row, ('id', 'metric', 'label', 'year', 'quarter', 'period', 'period_type',
        'period_end', 'value', 'unit', 'currency', 'scope', 'restatement', 'comparability_key', 'source_id',
        'source_status', 'source_status_label', 'source_title', 'source_type', 'announced_date', 'locator', 'source_url'), 200)
        for row in selected]
    by_id = {row.get('id'): row for row in records}
    for row in output['records']:
        original = by_id.get(row.get('id'), {})
        row['quote'] = str(original.get('quote') or '')[:180]
        row['quote_excerpt_only'] = bool(original.get('quote_excerpt_only') or len(str(original.get('quote') or '')) > 180)
    analysis = result.get('analysis') or {}
    output['analysis'] = {key: deepcopy(analysis.get(key, [])[:12] if already_projected else analysis.get(key, [])[-12:][::-1]) for key in
        ('comparisons', 'nonrecurring_bridges', 'signals', 'issues', 'quarters', 'cumulative', 'balances')}
    # Derived numeric rows are small; remove only surplus rows, never truncate
    # their inputs or change a null/blocked comparison into a calculated value.
    counts = result.get('analysis_counts') or {key: len(rows) for key, rows in analysis.items() if isinstance(rows, list)}
    output['analysis_counts'] = deepcopy(counts)
    output['record_count'] = result.get('record_count', len(records))
    output = _analysis_prose(output, 8000)
    tables = [output['records']] + list(output['analysis'].values())
    while size(output) > 8000:
        candidates = [rows for rows in tables if len(rows) > 1]
        if not candidates: break
        max(candidates, key=size).pop()
    output['context_note'] = '期间记录与分析是有界工作集，record_count/analysis_counts给出完整数量；省略不表示缺失。保留期间、单位、口径、公式及来源定位，派生结果不升级输入可信度。'
    return output


def _valuation_projection(result, group):
    output = _fields(result, ('id', 'artifact', 'analysis_kind', 'revision', 'created_at', 'method',
        'base_scenario_id', 'base_fingerprint', 'base_scenario_fingerprint', 'axes', 'envelope',
        'fixed_assumptions', 'valid_cells', 'invalid_cells', 'stale', 'status_note',
        'period_kind', 'period_label', 'valuation_date', 'currency', 'statistics', 'source_changed'), 400)
    output['notes'] = [_clip(note, 300) for note in result.get('notes', [])[:6]]
    if group == 'sensitivity':
        cells = result.get('cell_preview')
        if cells is None: cells = [cell for row in result.get('cells', []) for cell in row]
        output['cell_preview'] = deepcopy(cells[:8])
        output['cell_count'] = result.get('cell_count', len(cells))
        output['context_note'] = '轴值、固定假设与包络完整保留；矩阵只展示部分单元格，结果单位由输出字段决定，非市场报价。'
    else:
        rows = result.get('rows', [])
        output['rows'] = [_fields(row, ('company', 'ticker', 'multiple', 'multiple_value', 'included',
            'exclusion_reason', 'verification', 'verification_label', 'price', 'denominator_per_share',
            'multiple_basis', 'price_basis', 'denominator_basis', 'source_ids', 'source_refs',
            'calculation', 'method', 'period_kind', 'period_label', 'valuation_date', 'currency'), 250) for row in rows[:15]]
        output['row_count'] = result.get('row_count', len(rows))
        output['context_note'] = '统计为完整样本结果，可比公司列表为有界摘要；来源关联不代表数值或经济可比性已经核验。'
    return _analysis_prose(output, 6500)


def _tool_result(result, recent, preserve_read=False, tool=None):
    if not isinstance(result, dict):
        return _clip(result, 800)
    group = EXTENSION_GROUPS.get(tool)
    if group and not result.get('error'):
        if group == 'tracking': return _tracking_projection(result)
        if group == 'periods': return _period_projection(result)
        return _valuation_projection(result, group)
    item = _fields(result, ('artifact', 'run_id', 'revision', 'report_revision', 'id', 'document_id',
                           'proposal_id', 'status', 'error', 'summary', 'message', 'change_note',
                           'method', 'method_name', 'value', 'assumptions', 'result', 'note',
                           'reused', 'state', 'source_count', 'source_ids', 'company', 'fact_count',
                           'gaps', 'document_count', 'page_count', 'pages_without_text', 'warnings'), 700 if recent else 250)
    for key in ('document', 'proposal'):
        if isinstance(result.get(key), dict):
            item[key] = _fit(result[key], 1800)
    for key in ('snippets', 'results', 'observed_sources'):
        if isinstance(result.get(key), list):
            if preserve_read and key in ('snippets', 'results'):
                item[key] = [dict(_fields(s, SOURCE_FIELDS, 300), text=s.get('text', ''),
                             excerpt_only=bool(s.get('truncated') or s.get('excerpt_only'))) for s in result[key] if isinstance(s, dict)]
            else:
                item[key] = compact_sources(result[key], limit=2600 if recent else 650,
                                            count=4 if recent else 2, excerpt=800 if recent else 80)
    if result.get('url'):
        item.update(_fields(result, SOURCE_FIELDS, 200))
        item['text'] = result.get('text', '') if preserve_read else result.get('text', '')[:1800 if recent else 160]
        item['excerpt_only'] = bool(result.get('excerpt_only') or result.get('truncated') or
                                    len(result.get('text', '')) > len(item['text']))
    if 'items' in result:
        item['items'] = [_fields(r, ('id', 'question', 'status', 'next_action', 'ts_code', 'name',
                      'symbol', 'ticker', 'year'), 500) for r in result.get('items', [])[:12] ]
    if isinstance(result.get('coverage'), dict):
        item['coverage'] = _fields(result['coverage'], ('queries', 'web_sources', 'web_bodies',
                        'pdf_pages', 'search_calls', 'search_results', 'body_sources', 'status'))
    if result.get('methods'):
        item['methods'] = deepcopy(result['methods'])
    return item if preserve_read else _fit(item, 3400 if recent else 1000)


READ_TOOLS = {'read_document', 'read_public_pdf', 'read_webpage', 'read_web_page'}
UNREAD_STATUSES = {'error', 'failed', 'excluded', 'excluded_after_cutoff', 'needs_ocr'}


def _read_sources(row):
    result = row.get('result')
    if (row.get('tool') not in READ_TOOLS or not isinstance(result, dict) or
            result.get('error') or result.get('status') in UNREAD_STATUSES):
        return []
    sources = [s for key in ('snippets', 'results') for s in result.get(key, [])
               if isinstance(s, dict)]
    if result.get('text'):
        sources.append(result)
    return [s for s in sources if str(s.get('text') or '').strip() and not s.get('error')
            and s.get('status') not in UNREAD_STATUSES]


def _read_key(source):
    # Document/page identity also deduplicates overlapping multi-page reads.
    location = source.get('document_id') or (source.get('url') or source.get('source_url') or '').split('#')[0]
    return (location, source.get('page')) if location else (source.get('id') or
        hashlib.sha256(source.get('text', '').encode()).hexdigest(), source.get('page'))


def compact_tool_results(results, max_chars=10000):
    """Deduplicate identical observations but retain errors and artifact status."""
    seen, unique = {}, []
    preserved_read = False
    # Cross-page comparisons require more than one full observation. Keep a
    # bounded working set in addition to the latest read (which remains intact).
    # This is a soft input target, not another decision or execution limit.
    comparison_remaining, comparison_reads = 16000, 0
    full_page_keys, read_inventory, inventory_keys = set(), [], set()
    current_analyses = set()
    for row in reversed(results or []):
        inherited = row.get('read_inventory', {}).get('sources', [])
        for source in _read_sources(row) + inherited:
            key = _read_key(source)
            if key in inventory_keys:
                continue
            inventory_keys.add(key)
            read_inventory.append(_fields(source, ('id', 'document_id', 'url', 'source_url',
                                                   'page', 'read_status', 'excerpt_only', 'truncated'), 100))
    for index, row in reversed(list(enumerate(results or []))):
        signature = hashlib.sha256(json.dumps({'tool': row.get('tool'), 'result': row.get('result'),
                       'validation_feedback': row.get('validation_feedback'), 'error': row.get('error')},
                       ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
        if signature in seen:
            seen[signature]['repeated_observations'] = seen[signature].get('repeated_observations', 1) + 1
            continue
        item = _fields(row, ('tool', 'reused_from_step_id', 'validation_feedback', 'error'), 1200)
        item['_context_order'] = index
        if 'result' in row:
            sources = _read_sources(row)
            first_read = bool(sources and not preserved_read)
            fresh = [s for s in sources if _read_key(s) not in full_page_keys]
            additional_read = bool(not first_read and fresh and comparison_reads < 3 and
                                   size(fresh) <= comparison_remaining)
            full_read = first_read or additional_read
            read_result = row['result']
            if additional_read:
                read_result = deepcopy(read_result)
                for key in ('snippets', 'results'):
                    if isinstance(read_result.get(key), list):
                        read_result[key] = [s for s in read_result[key] if s in fresh]
                comparison_remaining -= size(fresh)
                comparison_reads += 1
            item['result'] = _tool_result(read_result, len(unique) < 2, full_read, row.get('tool'))
            group = EXTENSION_GROUPS.get(row.get('tool'))
            if group and group not in current_analyses and isinstance(read_result, dict) and not read_result.get('error'):
                item['structured_analysis_observation'] = True
                current_analyses.add(group)
            if full_read:
                item['full_read_observation'] = True
                preserved_read = True
                full_page_keys.update(_read_key(s) for s in sources)
                if first_read:
                    item['read_inventory'] = {
                        'sources': read_inventory[:60],
                        'omitted_source_count': max(0, len(read_inventory) - 60),
                        'note': '这些来源已取得正文，未展示全文不表示未读。先用已保留正文完成跨页对照；仅在缺少所需原句时按定位重新读取。节选不代表整页已完整读取。',
                    }
        if row.get('repeated_observations'):
            item['repeated_observations'] = row['repeated_observations']
        seen[signature] = item
        unique.append(item)
    # Old successful artifacts and errors are more important than old read excerpts.
    important = [r for r in unique[2:] if r.get('structured_analysis_observation') or r.get('error') or (isinstance(r.get('result'), dict) and
                 (r['result'].get('error') or r['result'].get('artifact') not in (None, 'document_read', 'document_search', 'web_search')))]
    ordered = unique[:2] + important + [r for r in unique[2:] if r not in important]
    def state_record(row):
        result = row.get('result')
        return bool(row.get('error') or (isinstance(result, dict) and (result.get('error') or
                    result.get('artifact') not in (None, 'document_search', 'document_read', 'web_search'))))

    # Older side-effect outcomes remain a small ledger even if a new PDF needs
    # most of this input. This prevents a short context from implying a report,
    # attachment or valuation was never saved.
    for row in ordered[2:]:
        if row.get('full_read_observation') or row.get('structured_analysis_observation') or not state_record(row) or not isinstance(row.get('result'), dict):
            continue
        original = row['result']
        row['result'] = _fields(original, ('artifact', 'run_id', 'revision', 'report_revision', 'id',
              'document_id', 'proposal_id', 'status', 'state', 'error', 'method', 'summary', 'note'), 250)
        for key in ('document', 'proposal', 'result'):
            if isinstance(original.get(key), dict):
                row['result'][key] = _fields(original[key], ('id', 'run_id', 'title', 'method', 'state', 'status'), 150)
        row['context_summary'] = True
    protected = [r for r in ordered if r.get('full_read_observation') or r.get('structured_analysis_observation') or r.get('validation_feedback') or state_record(r)]
    ordinary = [r for r in ordered if r not in protected]
    bounded = _fit(ordinary, max(500, max_chars - size(protected)))
    if not isinstance(bounded, list):
        bounded = []
    bounded += protected
    bounded.sort(key=lambda item: item['_context_order'])
    for item in bounded:
        item.pop('_context_order', None)
    return bounded


def compact_research_plan(plan, max_chars=6000):
    result = deepcopy(plan or {})
    result['pending_questions'] = [_question(q) for q in result.get('pending_questions', [])[:5]]
    # The original request already has its own top-level field.
    result.pop('original_request', None)
    result['previous_pending'] = [_clip(p, 350) for p in result.get('previous_pending', [])[:8]]
    pending = result.pop('pending_questions')
    result = _fit(result, max(500, max_chars - size(pending)))
    result['pending_questions'] = pending
    return result


def compact_payload(payload, max_chars=35000):
    """Final next-decision projection; policy and execution fields stay intact."""
    result = deepcopy(payload)
    result['tool_results'] = compact_tool_results(result.get('tool_results', []))
    result['conversation'] = compact_conversation(result.get('conversation', []))
    result['research_plan'] = compact_research_plan(result.get('research_plan', {}))
    result['free_research_sources'] = compact_sources(result.get('free_research_sources', []), 1800, 5, 350)
    result['proposed_scenarios'] = _fit(list(reversed(result.get('proposed_scenarios', []))), 2000)
    # bounded_context already projects its input; preserve the active inventory.
    result['context_compaction'] = {'applied': True, 'note': '完整记录保存在研究中；旧报告、修订历史和重复工具结果不重复发送。'}
    # Shrink only evidence/old prose blocks; never truncate active constraints,
    # original_request, available tools, model budgets or product capabilities.
    for field, floor in (('context', 7000), ('tool_results', 3500), ('conversation', 2000),
                         ('research_plan', 3500), ('free_research_sources', 500), ('proposed_scenarios', 500)):
        excess = size(result) - max_chars
        if excess <= 0:
            break
        current = size(result.get(field))
        target = max(floor, current - excess - 100)
        if field == 'tool_results':
            result[field] = compact_tool_results(result[field], target)
        elif field == 'conversation':
            result[field] = compact_conversation(result[field], target)
        elif field == 'research_plan':
            result[field] = compact_research_plan(result[field], target)
        elif field == 'context' and isinstance(result.get(field), dict):
            coverage = result[field].pop('question_coverage', None)
            result[field] = _fit(result[field], max(1000, target - size(coverage)))
            result[field]['question_coverage'] = coverage
        else:
            result[field] = _fit(result.get(field), target)
    return result
