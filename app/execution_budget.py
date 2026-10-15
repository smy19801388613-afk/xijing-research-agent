"""Per-turn time and token budgets shared by every nested model request."""
from contextlib import contextmanager
from contextvars import ContextVar
import math
import threading
import time


DEFAULTS = {'total_tokens': 1_000_000, 'time_minutes': 15}
BOUNDS = {
    'total_tokens': {'min': 50_000, 'max': 5_000_000, 'step': 50_000},
    'time_minutes': {'min': 1, 'max': 60, 'step': 1},
}
_LABELS = {'total_tokens': '本轮累计 Token 额度', 'time_minutes': '本轮时间上限'}
_LEGACY = {'generation_tokens': (4096, 65536), 'time_minutes': (1, 60), 'max_decisions': (2, 32)}
_current = ContextVar('research_execution_budget', default=None)


class BudgetExhausted(Exception):
    """No further model request can fit within the shared turn budget."""
    def __init__(self, message='本轮剩余 Token 额度不足以发起下一次模型请求，已保留完成的研究，可调整额度后继续', diagnostics=None):
        super().__init__(message)
        self.diagnostics = diagnostics or snapshot() or {}


def normalize(value=None):
    """Validate canonical budgets; migrate the old *per-call* limit explicitly."""
    if value is None:
        return dict(DEFAULTS)
    if isinstance(value, dict) and set(value) == set(_LEGACY):
        for key, (low, high) in _LEGACY.items():
            if type(value[key]) is not int or not low <= value[key] <= high:
                raise ValueError('旧版执行额度格式无效，无法迁移')
        # An old generation limit never represented a whole-turn token budget.
        return {'total_tokens': DEFAULTS['total_tokens'], 'time_minutes': value['time_minutes']}
    if not isinstance(value, dict) or set(value) != set(DEFAULTS):
        raise ValueError('执行额度必须完整包含 total_tokens 和 time_minutes，且不能包含其他字段')
    result = {}
    for key, bounds in BOUNDS.items():
        number = value[key]
        if type(number) is not int or not bounds['min'] <= number <= bounds['max']:
            raise ValueError(f"{_LABELS[key]}必须是 {bounds['min']} 至 {bounds['max']} 之间的整数")
        result[key] = number
    return result


def current():
    value = _current.get()
    return dict(value['budget']) if value else None


def deadline():
    value = _current.get()
    return value['deadline'] if value else None


def initial_usage(value=None):
    budget = normalize(value)
    return {'total_tokens': budget['total_tokens'], 'reported_tokens': 0, 'estimated_tokens': 0,
            'reserved_tokens': 0, 'used_tokens': 0, 'remaining_tokens': budget['total_tokens'],
            'request_count': 0, 'completed_requests': 0, 'uncertain_requests': 0}


def interrupted_usage(value):
    """Finalize an in-flight reservation after a worker is killed, without a bill."""
    result = dict(value or {})
    reserved = result.get('reserved_tokens', 0)
    if type(reserved) is int and reserved > 0:
        result['estimated_tokens'] = result.get('estimated_tokens', 0) + reserved
        result['used_tokens'] = result.get('reported_tokens', 0) + result['estimated_tokens']
        result['reserved_tokens'] = 0
        result['remaining_tokens'] = max(0, result.get('total_tokens', 0) - result['used_tokens'])
        result['uncertain_requests'] = result.get('uncertain_requests', 0) + max(1, result.get('request_count', 0) - result.get('completed_requests', 0))
        result['completed_requests'] = result.get('request_count', 0)
        result['accounting_status'] = 'interrupted_estimate'
    return result


def _snapshot(state):
    ledger = state['ledger']
    reserved = sum(r['input_tokens'] + r['output_tokens'] for r in ledger['reservations'].values())
    used = ledger['reported_tokens'] + ledger['estimated_tokens']
    return {'total_tokens': ledger['limit'], 'reported_tokens': ledger['reported_tokens'],
            'estimated_tokens': ledger['estimated_tokens'], 'reserved_tokens': reserved,
            'used_tokens': used, 'remaining_tokens': max(0, ledger['limit'] - used - reserved),
            'request_count': ledger['request_count'], 'completed_requests': ledger['completed_requests'],
            'uncertain_requests': ledger['uncertain_requests']}


def snapshot():
    state = _current.get()
    if not state:
        return None
    with state['ledger']['lock']:
        return _snapshot(state)


def _notify(state):
    if state.get('on_usage'):
        state['on_usage'](_snapshot(state))


def estimate_input_tokens(system, message):
    """Conservative UTF-8 byte bound, including a reserve for message framing.

    Tokenizers differ across compatible providers. This is an admission estimate,
    never presented as reported usage, and is replaced by provider counts later.
    """
    return len(system.encode('utf-8')) + len(message.encode('utf-8')) + 256


def reserve(input_tokens, requested_output, min_output=256):
    """Reserve input plus an output ceiling before dispatch; None outside a turn."""
    state = _current.get()
    if not state:
        return None
    if type(input_tokens) is not int or input_tokens < 0 or type(requested_output) is not int or requested_output < 1:
        raise ValueError('Token 预留值必须为有效整数')
    ledger = state['ledger']
    with ledger['lock']:
        available = _snapshot(state)['remaining_tokens']
        output = min(requested_output, available - input_tokens)
        if output < min(requested_output, min_output):
            diagnostics = _snapshot(state) | {'status': 'token_budget_exhausted', 'next_input_estimate': input_tokens}
            raise BudgetExhausted(diagnostics=diagnostics)
        ledger['request_count'] += 1
        reservation = {'id': ledger['request_count'], 'input_tokens': input_tokens, 'output_tokens': output}
        ledger['reservations'][reservation['id']] = reservation
        _notify(state)
        return dict(reservation)


def _count(value):
    return value if type(value) is int and value >= 0 else None


def settle(reservation, usage=None, *, status='completed'):
    """Account once per HTTP attempt, including retries and failed responses.

    Provider totals already contain reasoning/cached tokens; they must not be
    added twice. Anthropic input_tokens excludes its separate cache counters.
    Missing counts retain that part of the reservation as an explicit estimate:
    an interrupted request may still have been billed.
    """
    state = _current.get()
    if not state or not reservation:
        return None
    ledger = state['ledger']
    with ledger['lock']:
        reserved = ledger['reservations'].pop(reservation['id'], None)
        if reserved is None:
            return _snapshot(state)
        usage = usage if isinstance(usage, dict) else {}
        total = _count(usage.get('total_tokens'))
        reported = estimated = 0
        if total is not None:
            reported = total
        else:
            input_count = _count(usage.get('prompt_tokens'))
            if input_count is None:
                input_count = _count(usage.get('input_tokens'))
                if input_count is not None:
                    input_count += sum(_count(usage.get(key)) or 0 for key in ('cache_creation_input_tokens', 'cache_read_input_tokens'))
            output_count = _count(usage.get('completion_tokens'))
            if output_count is None:
                output_count = _count(usage.get('output_tokens'))
            for actual, fallback in ((input_count, reserved['input_tokens']), (output_count, reserved['output_tokens'])):
                if actual is None:
                    estimated += fallback
                else:
                    reported += actual
        ledger['reported_tokens'] += reported
        ledger['estimated_tokens'] += estimated
        ledger['completed_requests'] += 1
        if estimated:
            ledger['uncertain_requests'] += 1
        _notify(state)
        return _snapshot(state)


@contextmanager
def scope(value, deadline_at=None, *, on_usage=None):
    budget = normalize(value)
    if deadline_at is None:
        deadline_at = time.monotonic() + budget['time_minutes'] * 60
    elif isinstance(deadline_at, bool) or not isinstance(deadline_at, (int, float)) or not math.isfinite(deadline_at):
        raise ValueError('执行截止时间必须是有限的单调时钟时间')
    parent = _current.get()
    if parent:
        ledger = parent['ledger']
        budget['total_tokens'] = ledger['limit']
        deadline_at = min(deadline_at, parent['deadline'])
        on_usage = parent.get('on_usage') or on_usage
    else:
        ledger = {'limit': budget['total_tokens'], 'reported_tokens': 0, 'estimated_tokens': 0,
                  'request_count': 0, 'completed_requests': 0, 'uncertain_requests': 0,
                  'reservations': {}, 'lock': threading.RLock()}
    state = {'budget': budget, 'deadline': float(deadline_at), 'ledger': ledger, 'on_usage': on_usage}
    token = _current.set(state)
    try:
        _notify(state)
        yield dict(budget)
    finally:
        _current.reset(token)
