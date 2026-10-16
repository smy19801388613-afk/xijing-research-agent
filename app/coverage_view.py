"""Read-only activity views; saved evidence is not proof of a completed answer."""
from .turn_constraints import coverage
from .task_contract import current_contract


def turn_observation(chat, run=None, message='', turn_id=None):
    tid = turn_id or chat.get('turn_id')
    contract = current_contract(chat, tid)
    text = contract['active_request']
    required = contract['completion']['require_body']
    sources = (run or chat).get('research_sources', [])
    steps = chat.get('steps', [])
    current = coverage([s for s in steps if s.get('turn_id') == tid], text, sources, message,
                       run=run, require_body=required)
    task_ids = set(contract['obligation_turn_ids'] or [tid])
    task = coverage([s for s in steps if s.get('turn_id') in task_ids], text, sources, message,
                    run=run, require_body=required)
    current['task_coverage'] = {k: v for k, v in task.items() if k not in ('sources', 'question_coverage')}
    current['task_turn_count'] = len(task_ids)
    current['task_history_turn_count'] = len(contract['task_turn_ids'] or [tid])
    current['contract_scope'] = contract['scope']
    current['schema_version'] = 2
    return current


def coverage_notice(observed):
    """Never describe missing telemetry, or reuse, as a failed search."""
    task = observed.get('task_coverage', observed)
    if observed.get('telemetry_incomplete'):
        return '旧版深度研究未保存完整的检索统计，无法还原本轮精确搜索次数；请结合研究过程及已保存来源核对。'
    if observed.get('status') != 'partial':
        return ''
    if task.get('body_sources') or task.get('document_pages'):
        return ('本轮未新增原文查阅；本任务此前已取得原文。以下答复仍需给出与结论对应的来源，'
                '不能仅因已有报告就视为剩余问题已完成。')
    if task.get('telemetry_incomplete'):
        return '本轮未新增原文查阅；此前深度研究的检索统计不完整，不能把本轮的零新增解释为整个任务没有搜索或阅读。'
    if observed.get('saved_body_sources'):
        return ('本轮未新增原文查阅；当前资料库已有可读正文，但这不代表本轮已核对相关问题。'
                '需要引用对应原文或继续查证。')
    return (f"正文查证未完成：本轮已记录搜索 {observed['search_calls']} 次、结果 {observed['search_results']} 条，"
            '尚未取得可用于本次查证的正文；搜索摘要只能作为线索。')


def conversation_view(chat, run=None):
    """Recompute coverage for display without rewriting old messages or history."""
    views = {}
    for msg in chat.get('messages', []):
        tid = msg.get('turn_id')
        if msg.get('role') != 'assistant' or not tid:
            continue
        observed = turn_observation(chat, run, msg.get('content', ''), tid)
        old = msg.get('coverage') or chat.get('turn_coverage', {}).get(tid, {})
        if old.get('question_coverage'):
            observed['question_coverage'] = old['question_coverage']
        views[tid] = observed
    if chat.get('state') == 'running':
        views[chat['turn_id']] = turn_observation(chat, run)
    return {**chat, 'coverage_views': views}
