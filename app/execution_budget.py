"""Validated execution limits, frozen per turn and shared with nested model calls."""
from contextlib import contextmanager
from contextvars import ContextVar
import math
import time


DEFAULTS = {'generation_tokens': 49152, 'time_minutes': 15, 'max_decisions': 8}
BOUNDS = {
    'generation_tokens': {'min': 4096, 'max': 65536, 'step': 1024},
    'time_minutes': {'min': 1, 'max': 60, 'step': 1},
    'max_decisions': {'min': 2, 'max': 32, 'step': 1},
}
_LABELS = {'generation_tokens': '单次生成额度', 'time_minutes': '本轮时间上限', 'max_decisions': '模型决策次数'}
_current = ContextVar('research_execution_budget', default=None)


def normalize(value=None):
    """Return a fresh full budget; reject coercions and partial overrides."""
    if value is None:
        return dict(DEFAULTS)
    if not isinstance(value, dict) or set(value) != set(DEFAULTS):
        raise ValueError('执行额度必须完整包含 generation_tokens、time_minutes 和 max_decisions，且不能包含其他字段')
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


@contextmanager
def scope(value, deadline_at=None):
    budget = normalize(value)
    if deadline_at is None:
        deadline_at = time.monotonic() + budget['time_minutes'] * 60
    elif isinstance(deadline_at, bool) or not isinstance(deadline_at, (int, float)) or not math.isfinite(deadline_at):
        raise ValueError('执行截止时间必须是有限的单调时钟时间')
    token = _current.set({'budget': budget, 'deadline': float(deadline_at)})
    try:
        yield dict(budget)
    finally:
        _current.reset(token)
