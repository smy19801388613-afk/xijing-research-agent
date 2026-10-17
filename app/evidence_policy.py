"""Shared evidence eligibility, not an assessment of a claim's financial truth."""
import hashlib
import json
from datetime import date

BODY_STATES = {'body_read', 'article_text', 'pdf_text', 'public_pdf_text', 'original_record'}
LEAD_STATES = {'search_snippet', 'snippet_only', 'pdf_link_only', 'document_link'}
FAILED_STATES = {'failed', 'error', 'blocked', 'excluded', 'excluded_after_cutoff', 'needs_ocr'}


def parsed_date(value):
    if not isinstance(value, str):
        return None
    try:
        value = value.strip()
        if len(value) == 8 and value.isdigit():
            value = f'{value[:4]}-{value[4:6]}-{value[6:]}'
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _associated_facts(source, run):
    """Follow stored calculation inputs without reentering source catalogs."""
    sid = source.get('id')
    calculations = {c['id']: c for c in run.get('calculations', []) if c.get('id')}
    pending = [sid] if isinstance(sid, str) else []
    # The stored graph wins over a compact or caller-supplied reference. Other
    # derived artifacts may provide their own input IDs without being annual
    # calculations; do not interpret their JSON text as trusted dependencies.
    if source.get('kind') == 'calculation' and sid not in calculations:
        pending += [i for i in source.get('inputs', []) if isinstance(i, str)]
    seen = set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        calc = calculations.get(current)
        if calc:
            pending.extend(i for i in calc.get('inputs', []) if isinstance(i, str) and i not in seen)
    return [f for f in run.get('facts', []) if f.get('id') in seen
            or any(i in seen for i in f.get('evidence_ids', []))]


def eligibility(source, run=None, as_of=None):
    """Explain what an observed record permits, without promoting its provenance.

    Unknown-date bodies/OCR may inform explicitly conditional inferences, but
    cannot complete a research question or support a confirmed historical claim.
    Old material is not stale merely because of its age at a fixed cutoff.
    """
    from .fact_state import verification
    run = run or {}
    cutoff = parsed_date(as_of or run.get('request', {}).get('as_of'))
    published = parsed_date(source.get('published_at') or source.get('announced_date'))
    after = bool(cutoff and published and published > cutoff)
    historical = 'after_cutoff' if after else 'known_date' if published else 'unknown_date'
    kind = source.get('kind')
    state = source.get('read_status') or source.get('extraction')
    content = str(source.get('text') or source.get('quote') or '').strip()
    ocr = state in ('windows_ocr', 'ocr') or source.get('extraction') in ('windows_ocr', 'ocr')
    failed = state in FAILED_STATES or source.get('status') in FAILED_STATES
    lead = state in LEAD_STATES
    original = kind in ('api', 'document', 'material')
    body = bool(content) and not failed and not lead and (state in BODY_STATES or ocr or original)
    derived = kind in ('calculation', 'valuation')
    associated = _associated_facts(source, run)
    states = [verification(f, run.get('evidence', []), str(cutoff) if cutoff else None) for f in associated]
    if not states and isinstance(source.get('verification'), dict):
        states = [source['verification']]
    conflict = source.get('status') == 'conflict' or any(s.get('state') == 'conflict' for s in states)
    if associated and run.get('financial_period_records'):
        from .financial_basis import conflicts
        blocked = {fid for issue in conflicts(run) for fid in issue['fact_ids']}
        conflict = conflict or any(f.get('id') in blocked for f in associated)
    ocr = ocr or any(s.get('state') == 'ocr_pending' for s in states)
    unusable = any(s.get('state') in ('missing', 'unverified') for s in states)
    financial = kind == 'financial_fact'
    has_record = body or lead and bool(content) or financial and source.get('value') is not None or derived
    basis = ('conflict' if conflict else 'ocr' if ocr else 'lead' if lead else
             'financial_fact' if financial else 'derived' if derived else 'body' if body else 'missing')
    can_cite = bool(has_record and not after and not failed)
    inference = can_cite and basis not in ('lead', 'missing', 'conflict') and not unusable
    supported = bool(inference and not ocr and not source.get('inference_only') and (published is not None or derived))
    reasons = []
    if after: reasons.append('来源晚于研究截止日，不可用于本次历史研究')
    elif not published and not derived: reasons.append('来源日期未知，不能证明截止日内已知')
    if lead: reasons.append('搜索摘要或文件线索不构成已读取的正文')
    if ocr: reasons.append('OCR来源需核对原图，不能作为已确认的支持证据')
    if conflict: reasons.append('关联财务事实存在来源冲突')
    if failed or not has_record: reasons.append('尚未取得可引用的实际内容')
    if unusable: reasons.append('关联财务事实缺失或来源未确认')
    if derived: reasons.append('确定性计算或估值情景不是独立的原文证据')
    if source.get('inference_only'): reasons.append('含未核验输入或探索假设，仅能支持条件推论')
    return {'schema_version': 1, 'basis': basis, 'readable_body': body,
            'can_cite': can_cite, 'supports_claim': supported,
            'supports_answer': supported and not derived, 'supports_inference': bool(inference),
            'historical_eligibility': historical, 'reasons': reasons}


def source_fingerprint(source, run=None):
    """Exclude retrieval timestamps so rereading identical material is idempotent."""
    value = {k: source.get(k) for k in ('id', 'kind', 'text', 'quote', 'published_at', 'announced_date',
                                      'read_status', 'extraction', 'status', 'value', 'sha256')}
    if 'inputs' in source:
        value['inputs'] = source['inputs']
    value['eligibility'] = eligibility(source, run)
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
