"""Read-only recovery of unpublished report work within one conversation task.

The returned material is an unvalidated working copy, never a published report.
Callers must apply the current constraints and validate all citations again.
"""
from copy import deepcopy
from datetime import datetime


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
