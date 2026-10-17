"""Read-only recovery of unpublished report work within one conversation task.

The returned material is an unvalidated working copy, never a published report.
Callers must apply the current constraints and validate all citations again.
"""
from copy import deepcopy
from datetime import datetime
import re


REPORT_TOOLS = {'build_investment_memo', 'refine_investment_memo'}
UNSAVED_STATES = {'failed', 'cancelled', 'interrupted'}


def _time(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def _question(value):
    return ' '.join(str(value or '').split())


def select_working_draft(run, chat):
    """Select the newest reliably owned failed draft in the current task lineage.

    Normal new turns have a one-turn lineage and cannot inherit another task.
    Legacy attempts must match exactly one failed report step by timestamp and
    question (or an explicit step/attempt link). Missing or contradictory
    ownership is not repaired by guessing from the most recent run draft.
    """
    run_id, chat_id, turn_id = run.get('id'), chat.get('id'), chat.get('turn_id')
    if not run_id or not chat_id or not turn_id or chat.get('run_id') != run_id:
        return None
    contexts = chat.get('turn_contexts') or {}
    current = contexts.get(turn_id) or {}
    if current.get('run_id') not in (None, run_id):
        return None
    task_id = current.get('task_turn_id') or turn_id
    # The server constructs a multi-turn lineage only after explicit resumption.
    lineage = {turn_id}
    if current.get('resume_from_turn_id'):
        from .task_contract import current_contract
        if current.get('task_contract') or current.get('user_text') or chat.get('messages'):
            lineage.update(current_contract(chat).get('obligation_turn_ids') or [])
        else:
            # Old attempt archives can omit user text entirely. Their explicitly
            # stored resume lineage remains valid under the ownership checks.
            lineage.update(current.get('task_turn_ids') or [])
    steps = [s for s in chat.get('steps', [])
             if s.get('turn_id') in lineage and s.get('tool') in REPORT_TOOLS]
    successful = [s for s in steps if s.get('state') == 'completed'
                  and (s.get('result') or {}).get('artifact') == 'report']
    latest_saved = max((_time(s.get('finished_at')) or 0 for s in successful),
                       default=0)
    # A different chat can publish to this same run. Its newer report also
    # supersedes a legacy draft even when this chat has no successful step.
    latest_saved = max(latest_saved, _time(run.get('memo_updated_at')) or 0)
    failed = [s for s in steps if s.get('state') in UNSAVED_STATES]
    candidates = []
    for attempt in run.get('deep_research_attempts', []):
        stage = next((k for k in ('repaired_draft', 'draft')
                      if isinstance(attempt.get(k), dict) and attempt[k]), None)
        if stage is None or attempt.get('saved_report_revision') is not None:
            continue
        if (attempt.get('base_report_revision') is not None
                and attempt['base_report_revision'] != run.get('report_revision')):
            continue
        if (attempt.get('run_id') not in (None, run_id)
                or attempt.get('chat_id') not in (None, chat_id)
                or attempt.get('task_turn_id') not in (None, task_id)
                or (attempt.get('turn_id') is not None and attempt['turn_id'] not in lineage)):
            continue
        started = _time(attempt.get('started_at') or attempt.get('id'))
        if started is None or started <= latest_saved:
            continue
        matches = []
        for step in failed:
            if attempt.get('turn_id') not in (None, step.get('turn_id')):
                continue
            step_id = step.get('id')
            linked_attempt = (step.get('result') or {}).get('attempt_id') or step.get('attempt_id')
            if attempt.get('step_id') is not None and attempt['step_id'] != step_id:
                continue
            if linked_attempt is not None and linked_attempt != attempt.get('id'):
                continue
            explicit = (bool(step_id) and attempt.get('step_id') == step_id) or (
                linked_attempt is not None and linked_attempt == attempt.get('id'))
            start, end = _time(step.get('started_at')), _time(step.get('finished_at'))
            context = contexts.get(step.get('turn_id')) or {}
            if context.get('run_id') not in (None, run_id):
                continue
            if context.get('task_turn_id') not in (None, task_id):
                continue
            questions = [_question((step.get('arguments') or {}).get('question')),
                         _question(context.get('original_request')),
                         _question(context.get('user_text'))]
            timed = start is not None and end is not None and start <= started <= end
            same_question = bool(_question(attempt.get('question'))) and _question(attempt['question']) in questions
            if explicit or (timed and same_question):
                matches.append(step)
        if len(matches) == 1:
            candidates.append((started, attempt, stage, matches[0]))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    # Equal creation times do not establish which of two attempts was latest.
    if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
        return None
    _, attempt, stage, step = candidates[0]
    review = next((a['review'] for _, a, _, _ in candidates
                   if isinstance(a.get('review'), dict) and a['review']), {})
    return deepcopy({
        'attempt_id': attempt.get('id'), 'run_id': run_id, 'chat_id': chat_id,
        'turn_id': step.get('turn_id'), 'task_turn_id': task_id, 'step_id': step.get('id'),
        'stage': stage, 'working_draft': attempt[stage], 'review': review,
        'question': attempt.get('question', ''), 'unpublished': True,
        'validation_feedback': attempt.get('validation_error') or (step.get('result') or {}).get('error')
                               or (attempt.get('failure') or {}).get('message'),
    })


def _review_recovery_request(chat):
    """Only actual current user text can authorize archival issue recovery."""
    turn_id = chat.get('turn_id')
    texts = [m.get('content', '') for m in chat.get('messages', [])
             if m.get('role') == 'user' and m.get('turn_id') == turn_id]
    if not texts:
        texts = [(chat.get('turn_contexts') or {}).get(turn_id, {}).get('user_text', '')]
    text = '\n'.join(t for t in texts if isinstance(t, str))
    for clause in re.split(r'[，,。；;\n]', text):
        if re.search(r'(?:不|不要|不再|不得|无需|禁止)(?:需要|应当|应该)?\s*(?:恢复|复用|保留|继承|沿用|接续|使用|找回)', clause):
            continue
        if (re.search(r'(?:恢复|复用|保留|继承|沿用|找回).{0,32}(?:反方审阅|反方意见|审阅意见|复核意见)', clause)
                or re.search(r'(?:复用|接续|恢复|使用).{0,16}(?:未发布|未保存).{0,16}(?:草稿|初稿)', clause)):
            return text
    return ''


def select_recoverable_review(run, chat):
    """Recover genuine archived issues without reviving superseded prose.

    Explicit current authorization may cross task/revision boundaries for
    review issues only. It requires a same-run/same-chat attempt with an exact
    failed step link and timestamps; legacy ambiguous ownership is not guessed.
    The working-draft selector's stricter isolation rules remain unchanged.
    """
    request = _review_recovery_request(chat)
    run_id, chat_id = run.get('id'), chat.get('id')
    if not request or not run_id or not chat_id or chat.get('run_id') != run_id:
        return None
    candidates = []
    for attempt in run.get('deep_research_attempts', []):
        review = attempt.get('review')
        if (not isinstance(review, dict) or not review.get('issues')
                or not isinstance(attempt.get('id'), str) or not attempt['id']
                or attempt.get('saved_report_revision') is not None
                or attempt.get('mode') == 'source_refinement'
                or attempt.get('run_id') != run_id or attempt.get('chat_id') != chat_id
                or not attempt.get('turn_id') or not attempt.get('step_id')
                or not any(isinstance(attempt.get(k), dict) and attempt[k] for k in ('draft', 'repaired_draft'))):
            continue
        started = _time(attempt.get('started_at') or attempt.get('id'))
        if started is None:
            continue
        linked = [s for s in chat.get('steps', []) if s.get('id') == attempt['step_id']
                  and s.get('turn_id') == attempt['turn_id'] and s.get('tool') == 'build_investment_memo'
                  and s.get('state') in UNSAVED_STATES]
        if len(linked) != 1:
            continue
        step = linked[0]
        start, end = _time(step.get('started_at')), _time(step.get('finished_at'))
        context = (chat.get('turn_contexts') or {}).get(attempt['turn_id'], {})
        if (start is None or end is None or not start <= started <= end
                or context.get('run_id') not in (None, run_id)
                or context.get('task_turn_id') not in (None, attempt.get('task_turn_id'))):
            continue
        candidates.append((started, attempt))
    if not candidates:
        return None
    # An exact attempt reference is authoritative. Otherwise "existing/latest
    # review" means the most recent provably owned independent review.
    named_ids = {a['id'] for a in run.get('deep_research_attempts', [])
                 if isinstance(a.get('id'), str) and a['id'] and a['id'] in request}
    if named_ids:
        candidates = [pair for pair in candidates if pair[1]['id'] in named_ids]
        if not candidates:
            return None
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
        return None
    attempt = candidates[0][1]
    from . import review_state
    recovered = review_state.normalize(attempt['review'])
    origin = {k: attempt.get(k) for k in ('run_id', 'chat_id', 'turn_id', 'step_id', 'base_report_revision')}
    origin.update(attempt_id=attempt['id'], attempt_started_at=attempt.get('started_at') or attempt['id'])
    for issue in recovered['issues']:
        issue.update(status='needs_review', origin_attempt_id=attempt['id'], origin_turn_id=attempt['turn_id'],
                     origin_step_id=attempt['step_id'], origin_base_report_revision=attempt.get('base_report_revision'),
                     origin='unpublished_independent_review')
        issue.setdefault('first_seen_revision', run.get('report_revision', 1) + 1)
    recovered.update(independent_review_performed=False, reviewed_revision=None,
                     recovered_from_unpublished_attempts=[origin],
                     semantic_status='从未发布草稿恢复实际历史反方意见，均待复核；本轮未重新独立审阅当前报告。')
    return deepcopy(recovered)
