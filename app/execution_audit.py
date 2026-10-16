"""Shared execution records, independent of the optional access policy.

New records are always written. Legacy controlled-workspace logs remain readable;
export only selects records attributable to the requested research, never a whole
workspace or all past turns of a conversation.
"""
import json
import hashlib
import re
import sqlite3
import time
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from . import store

_context = ContextVar('execution_audit_context', default={})
_secret = re.compile(r'key|token|secret|authorization|password', re.I)
DATABASE = 'execution-audit.sqlite3'


def clean(value):
    if isinstance(value, dict):
        from .providers import _USAGE_COUNTERS, _USAGE_DETAILS
        def token_metric(key, item):
            if key in _USAGE_COUNTERS | {'max_tokens', 'max_completion_tokens', 'requested_output_tokens',
                    'generation_tokens', 'reported_tokens', 'estimated_tokens', 'reserved_tokens',
                    'used_tokens', 'remaining_tokens'}:
                return isinstance(item, int) and not isinstance(item, bool) and item >= 0
            return key in _USAGE_DETAILS | {'execution_token_usage'} and isinstance(item, dict)
        return {str(k): ('[REDACTED]' if _secret.search(str(k)) and not token_metric(str(k), v) else clean(v))
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str):
        value = re.sub(r'(?i)(bearer\s+)[\w.-]+', r'\1[REDACTED]', value)
        value = re.sub(r'\bsk-[A-Za-z0-9_-]{8,}', '[REDACTED]', value)
        value = re.sub(r'(?i)([?&](?:api[_-]?key|access_token|token|secret|password|auth)=)[^&#\s]+',
                       r'\1[REDACTED]', value)
        value = re.sub(r'(https?://)[^/\s@]+:[^/\s@]+@', r'\1[REDACTED]@', value)
        return value
    return value


def _conversation_context(identity):
    path = store.DATA / 'research.sqlite3'
    if not path.exists():
        return {}
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=15) as connection:
        row = connection.execute('SELECT payload FROM conversations WHERE id=?', (identity,)).fetchone()
    if not row:
        return {}
    chat = json.loads(row[0])
    return {key: chat.get(key) for key in ('run_id', 'turn_id') if chat.get(key)}


def audit(kind, **fields):
    context = dict(_context.get())
    execution = store._execution.get()
    if execution:
        context.setdefault('run_id' if execution['kind'] == 'run' else 'conversation_id', execution['id'])
        if execution.get('turn_id'):
            context.setdefault('turn_id', execution['turn_id'])
    context.update(fields)
    if context.get('conversation_id'):
        # Resolve at event time: a chat can create or switch its associated run.
        current = _conversation_context(context['conversation_id'])
        if not context.get('turn_id') or context['turn_id'] == current.get('turn_id'):
            for key, item in current.items():
                if context.get(key) is None:
                    context[key] = item
    value = clean({'time': store.now(), 'kind': kind, **context})
    material_id = value.get('material_id') or (value.get('material') or {}).get('id')
    store.DATA.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(store.DATA / DATABASE, timeout=15) as connection:
        connection.execute('''CREATE TABLE IF NOT EXISTS audit (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, conversation_id TEXT,
            turn_id TEXT, material_id TEXT, payload TEXT NOT NULL)''')
        connection.execute('CREATE INDEX IF NOT EXISTS audit_run ON audit(run_id)')
        connection.execute('CREATE INDEX IF NOT EXISTS audit_turn ON audit(conversation_id,turn_id)')
        connection.execute('CREATE INDEX IF NOT EXISTS audit_material ON audit(material_id)')
        connection.execute('INSERT INTO audit(run_id,conversation_id,turn_id,material_id,payload) VALUES (?,?,?,?,?)',
            (value.get('run_id'), value.get('conversation_id'), value.get('turn_id'), material_id,
             json.dumps(value, ensure_ascii=False, default=str)))


@contextmanager
def scope(**fields):
    token = _context.set({**_context.get(), **fields})
    try:
        yield
    finally:
        _context.reset(token)


def traced(kind, identity=None):
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            fields = {identity: str(args[0])} if identity and args else {}
            if kind == 'tool' and len(args) >= 3 and isinstance(args[0], dict):
                chat = args[0]
                fields.update(tool=args[1], conversation_id=chat.get('id'), turn_id=chat.get('turn_id'))
                if not chat.get('id'):
                    fields['run_id'] = chat.get('run_id')
            if identity == 'conversation_id' and fields.get(identity):
                fields['turn_id'] = _conversation_context(fields[identity]).get('turn_id')
            started = time.monotonic()
            with scope(**{k: v for k, v in fields.items() if v is not None}):
                audit(kind + '_start', arguments=kwargs)
                try:
                    result = fn(*args, **kwargs)
                    audit(kind + '_result', elapsed_ms=int((time.monotonic() - started) * 1000), result=result)
                    return result
                except Exception as error:
                    audit(kind + '_error', error_type=type(error).__name__, message=str(error)[:1000])
                    raise
        return wrapped
    return decorate


def _read(path, where='', params=()):
    if not path.exists():
        return []
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=15) as connection:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit'").fetchone():
            return []
        return [json.loads(row[0]) for row in connection.execute('SELECT payload FROM audit ' + where + ' ORDER BY seq', params)]


def events_for_run(run):
    """Include exact run records and its unbound preparation turns/material imports.

    A conversation match alone is never sufficient: it may have researched other
    companies. Explicitly attributed events from another run always stay out.
    """
    identity = run['id']
    path = store.DATA / DATABASE
    direct = _read(path, 'WHERE run_id=?', (identity,))
    legacy = _read(store.DATA / 'competition-audit.sqlite3')
    direct += [event for event in legacy if event.get('run_id') == identity]
    turns = {(event.get('conversation_id'), event.get('turn_id')) for event in direct
             if event.get('conversation_id') and event.get('turn_id')}
    owner = run.get('execution_owner') or {}
    if owner.get('conversation_id') and owner.get('turn_id'):
        turns.add((owner['conversation_id'], owner['turn_id']))
    associated = []
    for conversation_id, turn_id in sorted(turns):
        associated += _read(path, 'WHERE run_id IS NULL AND conversation_id=? AND turn_id=?',
                            (conversation_id, turn_id))
    material_ids = set(run.get('request', {}).get('material_ids', []))
    if material_ids:
        marks = ','.join('?' for _ in material_ids)
        associated += _read(path, f'WHERE run_id IS NULL AND material_id IN ({marks})', tuple(sorted(material_ids)))
    for event in legacy:
        if event.get('run_id'):
            continue
        material_id = event.get('material_id') or (event.get('material') or {}).get('id')
        if ((event.get('conversation_id'), event.get('turn_id')) in turns or
                (event.get('kind') == 'material_import' and material_id in material_ids)):
            associated.append(event)
    return sorted((public_event(event) for event in direct + associated), key=lambda event: event.get('time', ''))


def public_event(event):
    """Conversation-wide model context is not safe to include in a run export.

    Keep the redacted full request in the local audit database, while the shared
    view fingerprints the request. We do not claim full prompt replay from ZIP.
    """
    value = clean(event)
    if value.get('kind') == 'model_request':
        for key in ('payload', 'system', 'messages', 'body', 'prompt'):
            if key not in value:
                continue
            content = value[key]
            raw = json.dumps(content, ensure_ascii=False, sort_keys=True, default=str).encode('utf-8')
            value[key] = {'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw),
                          'content_omitted': True,
                          'reason': '模型输入可能含跨轮对话；完整脱敏请求仅保留在本地执行记录中。'}
    return value
