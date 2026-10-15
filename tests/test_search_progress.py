"""Search novelty guards must preserve useful work after query rewording stalls."""
import pytest

from app import agent, execution_budget, search_progress, store


def search_step(n, urls=(), turn='now'):
    return {'id': str(n), 'turn_id': turn, 'tool': 'web_search', 'state': 'completed',
            'search_dispatched': True, 'arguments': {'queries': [f'公告关键词变体{n}']},
            'result': {'results': [{'id': f'varying-{n}-{i}', 'url': url} for i, url in enumerate(urls)]}}


def record(steps, turn='now'):
    return {'turn_id': turn, 'steps': steps,
            'turn_contexts': {turn: {'task_turn_ids': ['old', turn]}}}


def test_reworded_same_urls_and_tracking_variants_pause_only_search():
    steps = [search_step(0, ['https://example.com/disclosure?x=1&y=2'])]
    steps += [search_step(i, [f'http://EXAMPLE.com/disclosure/?y=2&x=1&utm_source={i}#fragment']) for i in range(1, 4)]
    state = search_progress.status(record(steps))
    assert state['web_search_paused'] and state['consecutive_no_new_sources'] == 3
    assert state['last_new_sources'] == 0


def test_new_source_resets_streak_and_many_useful_searches_are_allowed():
    steps = [search_step(0, ['https://example.com/0'])]
    for n in range(1, 40):
        steps.extend([search_step(n, [f'https://example.com/{n-1}']), search_step(n, [f'https://example.com/{n}'])])
    state = search_progress.status(record(steps))
    assert not state['web_search_paused'] and state['last_new_sources'] == 1
    assert state['searches_this_turn'] == 79


def test_continuation_starts_fresh_streak_but_knows_old_sources():
    old = [search_step(n, ['https://example.com/annual'], 'old') for n in range(4)]
    state = search_progress.status(record(old + [search_step(4, ['https://example.com/annual'])]))
    assert state['consecutive_no_new_sources'] == 1 and not state['web_search_paused']
    state = search_progress.status(record(old + [search_step(n, ['https://example.com/annual']) for n in range(4, 7)]))
    assert state['web_search_paused']


def test_empty_results_and_failed_searches_do_not_reset_progress():
    steps = [search_step(n) for n in range(3)]
    steps[1].update(state='failed', result={'error': 'search unavailable'})
    assert search_progress.status(record(steps))['web_search_paused']


def test_ids_are_fallback_only_and_nondispatched_calls_do_not_count():
    steps = [search_step(n) for n in range(4)]
    for row in steps: row['result'] = {'results': [{'id': 'same-source'}]}
    steps[-1]['search_dispatched'] = False
    assert not search_progress.status(record(steps))['web_search_paused']


def test_report_aggregate_does_not_preempt_nested_search_novelty():
    sources = [{'url': f'https://example.com/new-{n}'} for n in range(4)]
    nested = [{'id': f'event-{n}', 'kind': 'search', 'state': 'completed',
               'result': {'results': [source]}} for n, source in enumerate(sources)]
    report = {'id': 'report', 'turn_id': 'now', 'tool': 'build_investment_memo',
              'state': 'completed', 'result': {'observed_sources': sources}, 'retrieval_events': nested}
    state = search_progress.status(record([report]))
    assert state['searches_this_turn'] == 4
    assert state['last_new_sources'] == 1 and not state['web_search_paused']
    # The same sources really are old when subsequent external searches repeat.
    repeats = [search_step(n + 5, [sources[-1]['url']]) for n in range(3)]
    assert search_progress.status(record([report, *repeats]))['web_search_paused']


def test_failed_report_preserves_successful_nested_search_progress():
    report = {'id': 'report', 'turn_id': 'now', 'tool': 'build_investment_memo',
              'state': 'failed', 'result': {'error': 'report generation failed'},
              'retrieval_events': [{'id': 'nested', 'kind': 'search', 'state': 'completed',
                                    'result': {'results': [{'url': 'https://example.com/annual'}]}}]}
    state = search_progress.status(record([report, search_step(2, ['https://example.com/annual'])]))
    assert state['searches_this_turn'] == 2
    assert state['consecutive_no_new_sources'] == 1 and not state['web_search_paused']


@pytest.fixture
def chat(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    run = store.create_run({'ticker': '600001.SH', 'year': 2025, 'as_of': '2026-10-15', 'mode': 'sample', 'question': '分红公告核对'})
    store.update_run(run['id'], lambda r: r.update(state='completed_with_gaps', company='甲公司', memo={'title': '已有报告'}))
    current = store.new_conversation(run['id'])
    return store.begin_turn(current['id'], '联网搜索分红公告，并用已有材料修订保存报告。', 'stub', 'documents')


def test_stalled_search_can_still_read_different_pages_and_refine_report(chat, monkeypatch):
    decisions = []
    calls = []
    def model(system, payload, *args, **kwargs):
        n = len(decisions)
        decisions.append(payload)
        if n < 5:  # The fifth request ignores the tool mask and must not dispatch.
            if n == 4:
                assert 'web_search' not in payload['available_tools']
                assert 'refine_investment_memo' in payload['available_tools']
            return {'action': 'tool', 'tool': 'web_search', 'arguments': {'queries': [f'分红公告变体{n}']}}, {'model': 'stub'}
        if n < 7:
            return {'action': 'tool', 'tool': 'read_document', 'arguments': {'document_id': 'annual', 'page': n}}, {'model': 'stub'}
        if n == 7:
            return {'action': 'tool', 'tool': 'refine_investment_memo', 'arguments': {}}, {'model': 'stub'}
        return {'action': 'respond', 'message': '已依据现有原文修订保存报告；搜索没有取得新的公告线索，实施状态仍未核实。'}, {'model': 'stub'}
    def tool(c, name, args, *rest):
        calls.append(name)
        if name == 'web_search':
            store.update_conversation(c['id'], lambda value: value['steps'][-1].update(search_dispatched=True))
            return {'results': [{'url': 'https://example.com/same', 'title': '同一条公告线索'}]}
        if name == 'read_document': return {'snippets': [{'page': args['page'], 'text': '已保存年报中的不同原文页'}]}
        assert name == 'refine_investment_memo'
        store.update_run(c['run_id'], lambda r: r.update(report_revision=3))
        return {'artifact': 'report', 'revision': 3}
    monkeypatch.setattr(agent, 'model_json', model)
    monkeypatch.setattr(agent, 'tool', tool)
    agent.execute(chat['id'])
    saved = store.get_conversation(chat['id'])
    assert calls == ['web_search'] * 4 + ['read_document', 'read_document', 'refine_investment_memo']
    assert saved['state'] == 'idle' and not saved.get('stop_reason')
    assert store.get_run(chat['run_id'])['report_revision'] == 3


@pytest.mark.parametrize('ending', ['cancel', 'budget'])
def test_search_mask_keeps_cancellation_and_token_budget_effective(chat, monkeypatch, ending):
    store.update_conversation(chat['id'], lambda c: c['steps'].extend([
        dict(search_step(n), turn_id=chat['turn_id']) for n in range(3)]))
    def model(system, payload, *args, **kwargs):
        assert 'web_search' not in payload['available_tools']
        if ending == 'cancel': raise agent.research.Cancelled()
        raise execution_budget.BudgetExhausted()
    monkeypatch.setattr(agent, 'model_json', model)
    monkeypatch.setattr(agent, 'tool', lambda *a, **k: pytest.fail('no tool may run after cancellation/budget stop'))
    agent.execute(chat['id'])
    saved = store.get_conversation(chat['id'])
    if ending == 'cancel': assert saved['state'] == 'cancelled'
    else: assert saved['state'] == 'failed' and saved['stop_reason'] == 'token_limit'
