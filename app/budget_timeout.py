"""Freeze one turn's deadline and stop its own worker even during blocking I/O."""
from datetime import datetime, timezone
import json
import logging
import os
import threading
import uuid

from . import store


def deadline_for(chat, turn_id):
    """The per-turn snapshot wins over mutable defaults or the latest chat fields."""
    if chat.get('turn_id') != turn_id:
        return None
    context = chat.get('turn_contexts', {}).get(turn_id, {})
    stamp = context.get('deadline_at') or chat.get('execution_deadline_at')
    if not stamp:
        return None
    try:
        value = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
        return value.astimezone(timezone.utc) if value.tzinfo else None
    except (TypeError, ValueError):
        return None


def expire_conversation(cid, turn_id, deadline_at=None):
    """Atomically revoke an expired turn; a late callback cannot change its successor.

    This intentionally bypasses the worker's write guard, like cancellation: the
    transaction revokes that guard for every subsequent tool/model result.
    """
    with store.connection() as connection:
        connection.execute('BEGIN IMMEDIATE')
        row = connection.execute('SELECT payload FROM conversations WHERE id=?', (cid,)).fetchone()
        if not row:
            raise KeyError(cid)
        chat = json.loads(row[0])
        if chat.get('turn_id') != turn_id or chat.get('state') != 'running':
            return chat
        deadline = deadline_for(chat, turn_id)
        if deadline is None or (deadline_at is not None and deadline != deadline_at):
            return chat
        if datetime.now(timezone.utc) < deadline:
            return chat
        stopped_at = store.now()
        context = chat.get('turn_contexts', {}).get(turn_id, {})
        minutes = context.get('execution_budget', {}).get('time_minutes')
        limit = f' {minutes} 分钟' if minutes is not None else ''
        message = f'本轮已达到{limit}时间上限，已保留完成的结果。可继续未完成部分，或调整下一轮预算。'
        exhausted = {'reason': 'time_limit', 'deadline_at': deadline.isoformat(),
                     'stopped_at': stopped_at, 'time_minutes': minutes}
        for rid, payload in connection.execute("SELECT id,payload FROM runs WHERE state IN ('queued','running')").fetchall():
            run = json.loads(payload)
            if run.get('execution_owner') != {'conversation_id': cid, 'turn_id': turn_id}:
                continue
            run.update(state='failed', stop_reason='time_limit', updated_at=stopped_at,
                       budget_exhausted=exhausted)
            run.setdefault('events', []).append({'time': stopped_at, 'label': '达到本轮时间上限',
                'detail': '已保留完成的研究结果，可继续未完成部分', 'kind': 'warning'})
            connection.execute('UPDATE runs SET cancel=1,state=?,payload=? WHERE id=?',
                ('failed', json.dumps(run, ensure_ascii=False), rid))
        run_row = connection.execute('SELECT payload FROM runs WHERE id=?', (chat.get('run_id'),)).fetchone()
        run = json.loads(run_row[0]) if run_row else None
        chat.update(state='failed', stop_reason='time_limit', budget_exhausted=exhausted,
                    cancel_requested=False, updated_at=stopped_at)
        for step in chat.get('steps', []):
            if step.get('turn_id') == turn_id and step.get('state') == 'running':
                step.update(state='failed', finished_at=stopped_at, stop_reason='time_limit',
                            result={'error': '本轮达到时间上限，该步骤尚未完成'})
        from .agent import delivery_state, progress_message
        observed = chat.get('turn_coverage', {}).get(turn_id)
        delivery = delivery_state(chat, run, 'failed', observed)
        delivery['stop_reason'] = 'time_limit'
        chat['messages'].append({'id': uuid.uuid4().hex[:12], 'role': 'assistant', 'turn_id': turn_id,
            'content': message + '\n\n' + progress_message(delivery), 'time': stopped_at,
            'error': True, 'incomplete': True, 'stop_reason': 'time_limit', 'delivery': delivery})
        connection.execute('UPDATE conversations SET cancel=1,updated=?,payload=? WHERE id=?',
            (stopped_at, json.dumps(chat, ensure_ascii=False), cid))
    return chat


def watch_turn(cid, turn_id, exit_process=None):
    """Return a stop event for a daemon bound to this process and this frozen turn.

    The daemon exits only its own worker. No PID lookup can accidentally select
    the worker of a newer turn. Callers must set the event in their finally block.
    """
    stopped = threading.Event()
    chat = store.get_conversation(cid)
    deadline = deadline_for(chat, turn_id)
    if deadline is None or chat.get('state') != 'running':
        return stopped
    exit_process = exit_process or os._exit
    # Event.wait uses a monotonic clock; changing settings cannot extend a turn.
    seconds = max(0.0, (deadline - datetime.now(timezone.utc)).total_seconds())

    def expire():
        if stopped.wait(seconds):
            return
        while not stopped.is_set():
            try:
                saved = expire_conversation(cid, turn_id, deadline)
                if saved.get('turn_id') != turn_id or saved.get('state') != 'running':
                    exit_process(124)
                    return
                # Only a backwards wall-clock adjustment can make the fixed
                # monotonic timer fire before the persisted UTC deadline.
            except Exception:
                logging.exception('Unable to persist deadline for turn %s; retrying', turn_id)
            if stopped.wait(0.1):
                return

    threading.Thread(target=expire, name='agent-turn-deadline', daemon=True).start()
    return stopped
