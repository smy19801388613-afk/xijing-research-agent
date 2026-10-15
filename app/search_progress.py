"""Detect query-rewording loops by observed sources, not argument spelling."""
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .turn_constraints import canonical_url, task_steps


NOTICE = ('连续三次搜索没有取得新的来源线索，本轮暂时停用联网搜索。'
          '请从已保存结果中选择与问题确实相关的原文继续阅读，或查阅已有PDF、更新问题、修订并保存已授权报告。'
          '没有相关可读来源时，如实说明搜索障碍与尚未核实的事项；不要改写关键词重复搜索，也不要读取无关网页凑进度。')


def source_key(source):
    url = source.get('url')
    if isinstance(url, str) and url.strip():
        try:
            parts = urlsplit(canonical_url(url.strip()))
            return ('url', urlunsplit((parts.scheme, parts.netloc, parts.path,
                    urlencode(sorted(parse_qsl(parts.query))), '')))
        except ValueError:
            return None
    sid = source.get('id') or source.get('source_id')
    return ('id', str(sid)) if sid else None


def status(chat):
    """Prior task sources seed novelty; only this turn increments its streak."""
    seen, events, event_ids = set(), [], set()
    for step in task_steps(chat):
        for event in step.get('retrieval_events', []):
            if event.get('id') and event['id'] in event_ids: continue
            if event.get('id'): event_ids.add(event['id'])
            events.append(dict(event, turn_id=step.get('turn_id'),
                               tool='web_search' if event.get('kind') == 'search' else event.get('kind')))
        # The outer report aggregates sources produced by its child searches.
        # Account for children first, or the aggregate would make every child
        # appear to have returned sources that were already seen.
        events.append(step)
    streak = attempts = last_new = 0
    blocked = False
    for step in events:
        if step.get('state') not in ('completed', 'failed'): continue
        result = step.get('result') or {}
        if not isinstance(result, dict): continue
        sources = list(result.get('results') or []) + list(result.get('observed_sources') or [])
        if result.get('url'): sources.append(result)
        keys = {key for source in sources if isinstance(source, dict) and (key := source_key(source))}
        new = keys - seen
        seen.update(keys)
        if (step.get('tool') != 'web_search' or step.get('turn_id') != chat.get('turn_id')
                or step.get('search_dispatched') is False):
            continue
        attempts += 1
        last_new = len(new)
        streak = 0 if new else streak + 1
        # Once blocked, other useful tools do not silently reopen the search
        # loop; a fresh human turn gets its own three-attempt allowance.
        blocked = blocked or streak >= 3
    return {'searches_this_turn': attempts, 'last_new_sources': last_new,
            'consecutive_no_new_sources': streak, 'web_search_paused': blocked,
            'reason': NOTICE if blocked else ''}
