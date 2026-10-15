"""Whole-turn accounting, including nested drafts and retries; no paid calls."""
import copy

import httpx
import pytest

from app import connections, execution_budget as budgets, providers


SMALL = {'total_tokens': 50000, 'time_minutes': 15}


@pytest.fixture
def model(monkeypatch):
    cfg = {'provider': 'deepseek', 'protocol': 'openai', 'key': 'fixture',
           'base': 'https://model.example', 'id': 'fixture', 'model': 'fixture', 'json_mode': True}
    monkeypatch.setattr(connections, 'resolve_model', lambda _: cfg)
    monkeypatch.setattr(providers.competition, 'check_network', lambda *args: None)
    monkeypatch.setattr(providers.competition, 'audit', lambda *args, **kw: None)
    calls, replies = [], []

    def post(url, **kw):
        calls.append(copy.deepcopy(kw))
        reply = replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return httpx.Response(200, json=reply, request=httpx.Request('POST', url))

    monkeypatch.setattr(providers.httpx, 'post', post)
    return calls, replies


def response(usage=None, finish='stop', content='{"ok":true}'):
    return {'choices': [{'message': {'content': content}, 'finish_reason': finish}], 'usage': usage or {}}


def test_legacy_single_generation_budget_migrates_to_turn_default_not_same_number():
    assert budgets.normalize({'generation_tokens': 65536, 'time_minutes': 52, 'max_decisions': 21}) == {
        'total_tokens': 1000000, 'time_minutes': 52}
    with pytest.raises(ValueError):
        budgets.normalize({'generation_tokens': True, 'time_minutes': 52, 'max_decisions': 21})


def test_reported_total_already_includes_reasoning_and_cached_tokens():
    with budgets.scope(SMALL):
        reservation = budgets.reserve(1000, 2000)
        budgets.settle(reservation, {'prompt_tokens': 100, 'completion_tokens': 200, 'total_tokens': 300,
            'prompt_tokens_details': {'cached_tokens': 80}, 'completion_tokens_details': {'reasoning_tokens': 150}})
        assert budgets.snapshot()['reported_tokens'] == 300
        assert budgets.snapshot()['estimated_tokens'] == budgets.snapshot()['reserved_tokens'] == 0
        # A repeated settlement cannot charge the same attempt twice.
        budgets.settle(reservation, {'total_tokens': 300})
        assert budgets.snapshot()['used_tokens'] == 300


def test_anthropic_cache_inputs_count_but_reasoning_details_do_not():
    with budgets.scope(SMALL):
        reservation = budgets.reserve(2000, 2000)
        budgets.settle(reservation, {'input_tokens': 100, 'cache_creation_input_tokens': 200,
            'cache_read_input_tokens': 300, 'output_tokens': 400,
            'output_tokens_details': {'reasoning_tokens': 350}})
        assert budgets.snapshot()['reported_tokens'] == 1000


def test_nested_scopes_share_usage_and_cannot_extend_deadline_or_budget():
    snapshots = []
    with budgets.scope(SMALL, deadline_at=1000, on_usage=snapshots.append):
        budgets.settle(budgets.reserve(500, 1000), {'total_tokens': 1400})
        with budgets.scope({'total_tokens': 1000000, 'time_minutes': 60}, deadline_at=2000):
            assert budgets.deadline() == 1000
            assert budgets.current()['total_tokens'] == SMALL['total_tokens']
            budgets.settle(budgets.reserve(600, 1000), {'total_tokens': 1500})
        assert budgets.snapshot()['used_tokens'] == 2900
        assert budgets.snapshot()['request_count'] == 2
    assert snapshots[-1]['used_tokens'] == 2900
    assert any(item['reserved_tokens'] > 0 for item in snapshots)
    assert budgets.snapshot() is None


def test_missing_usage_and_hard_stop_are_explicit_conservative_estimates():
    with budgets.scope(SMALL):
        budgets.settle(budgets.reserve(1000, 2000), status='network_error')
        budgets.reserve(400, 600)
        persisted = budgets.snapshot()
    assert persisted['reported_tokens'] == 0
    assert persisted['estimated_tokens'] == 3000
    assert persisted['reserved_tokens'] == 1000
    final = budgets.interrupted_usage(persisted)
    assert final['used_tokens'] == final['estimated_tokens'] == 4000
    assert final['reserved_tokens'] == 0
    assert final['uncertain_requests'] == 2
    assert final['accounting_status'] == 'interrupted_estimate'
    assert budgets.interrupted_usage(final) == final
    assert persisted['reserved_tokens'] == 1000


def test_partially_reported_usage_does_not_erase_unknown_input():
    with budgets.scope(SMALL):
        budgets.settle(budgets.reserve(1000, 2000), {'completion_tokens': 300})
        assert budgets.snapshot()['reported_tokens'] == 300
        assert budgets.snapshot()['estimated_tokens'] == 1000
        assert budgets.snapshot()['used_tokens'] == 1300


def test_provider_preflight_rejects_input_that_cannot_fit_without_contact(model):
    calls, _ = model
    with budgets.scope(SMALL):
        with pytest.raises(budgets.BudgetExhausted) as caught:
            providers.model_json('资料' * 10000, {}, 2400)
        assert budgets.snapshot()['request_count'] == 0
    assert not calls
    assert caught.value.diagnostics['next_input_estimate'] > SMALL['total_tokens']


def test_remaining_allowance_caps_actual_provider_output_and_next_call_stops(model):
    calls, replies = model
    replies.append(response({'total_tokens': 3000}))
    with budgets.scope(SMALL):
        budgets.settle(budgets.reserve(100, 100), {'total_tokens': 47000})
        _, meta = providers.model_json('test', {}, 6000)
        assert 256 <= calls[0]['json']['max_tokens'] < 3000
        assert meta['execution_token_usage']['used_tokens'] == 50000
        with pytest.raises(budgets.BudgetExhausted):
            providers.model_json('test', {}, 6000)
    assert len(calls) == 1


def test_retry_and_following_calls_consume_one_shared_ledger(model):
    calls, replies = model
    replies.extend([response({'prompt_tokens': 5000, 'completion_tokens': 10000}, 'length'),
                    response({'prompt_tokens': 5000, 'completion_tokens': 7000}),
                    response({'total_tokens': 10000})])
    with budgets.scope(SMALL):
        _, first = providers.model_json('report', {}, 7800, reasoning=True)
        _, second = providers.model_json('decision', {}, 2400)
        assert first['execution_token_usage']['used_tokens'] == 27000
        assert second['execution_token_usage']['used_tokens'] == 37000
        assert budgets.snapshot()['request_count'] == 3
    assert len(calls) == 3
    assert calls[1]['json']['max_tokens'] < 35000


def test_budget_exhaustion_between_retries_preserves_first_actual_usage(model):
    calls, replies = model
    replies.append(response({'total_tokens': 50000}, 'length'))
    with budgets.scope(SMALL):
        with pytest.raises(budgets.BudgetExhausted):
            providers.model_json('report', {}, 7800, reasoning=True)
        assert budgets.snapshot()['reported_tokens'] == 50000
        assert budgets.snapshot()['reserved_tokens'] == 0
    assert len(calls) == 1


def test_network_error_without_usage_is_estimated_and_persisted_before_failure(model):
    calls, replies = model
    replies.append(httpx.ReadTimeout('fixture'))
    snapshots = []
    with budgets.scope(SMALL, on_usage=snapshots.append):
        with pytest.raises(providers.ProviderError) as caught:
            providers.model_json('decision', {}, 2400)
        final = budgets.snapshot()
    assert final['reported_tokens'] == 0 and final['estimated_tokens'] > 2400
    assert final['reserved_tokens'] == 0
    assert snapshots[1]['reserved_tokens'] > 2400
    assert caught.value.diagnostics['execution_token_usage'] == final
    assert len(calls) == 1


def test_invalid_json_still_accounts_actual_reported_usage(model):
    _, replies = model
    replies.append(response({'total_tokens': 4000}, content='not json'))
    with budgets.scope(SMALL):
        with pytest.raises(providers.ProviderError):
            providers.model_json('test', {})
        assert budgets.snapshot()['reported_tokens'] == 4000
        assert budgets.snapshot()['estimated_tokens'] == 0
