"""Completed empty provider responses recover once, without private reasoning."""
import copy
import json

import httpx
import pytest

from app import connections, execution_budget, providers
from app.research import Cancelled


@pytest.fixture
def provider(monkeypatch):
    cfg = {"provider": "deepseek", "protocol": "openai", "key": "fixture-key",
           "base": "https://model.example", "id": "fixture", "model": "fixture", "json_mode": True}
    monkeypatch.setattr(connections, "resolve_model", lambda _: cfg)
    monkeypatch.setattr(providers.competition, "check_network", lambda *args: None)
    calls, replies, audit = [], [], []
    monkeypatch.setattr(providers.competition, "audit", lambda kind, **data: audit.append((kind, copy.deepcopy(data))))

    def post(url, **kwargs):
        calls.append(copy.deepcopy(kwargs))
        reply = replies.pop(0)
        if callable(reply): reply = reply()
        return httpx.Response(200, json=reply, request=httpx.Request("POST", url))

    monkeypatch.setattr(providers.httpx, "post", post)
    return cfg, calls, replies, audit


def response(content="", finish="stop", usage=None, refusal=None):
    return {"choices": [{"message": {"content": content, "reasoning_content": "PRIVATE_CHAIN_NOT_FORWARDED",
                                      "refusal": refusal}, "finish_reason": finish}], "usage": usage or {}}


@pytest.mark.parametrize("empty", [None, "", " \n\t"])
def test_completed_empty_retries_from_original_evidence_once(provider, empty):
    _, calls, replies, audit = provider
    replies.extend([response(empty, usage={"prompt_tokens": 12000, "completion_tokens": 7740,
        "completion_tokens_details": {"reasoning_tokens": 7740}, "total_tokens": 19740}),
        response('{"ok":true}', usage={"prompt_tokens": 12000, "completion_tokens": 42, "total_tokens": 12042})])
    result, meta = providers.model_json("report", {"evidence": [{"id": "source-one"}]}, 7800, reasoning=True)
    assert result == {"ok": True} and len(calls) == 2
    assert [c['json']['max_tokens'] for c in calls] == [32768, 32768]
    assert calls[0]['json']['messages'][1] == calls[1]['json']['messages'][1]
    recovery_prompt = calls[1]['json']['messages'][0]['content']
    assert 'completed without a final answer' in recovery_prompt
    assert 'exceeded' not in recovery_prompt
    assert meta['attempts'][0]['status'] == 'empty_response'
    assert meta['attempts'][1]['recovery_reason'] == 'empty_response'
    assert meta['usage_total']['total_tokens'] == 31782
    assert meta['usage_total']['completion_tokens_details']['reasoning_tokens'] == 7740
    assert 'PRIVATE_CHAIN_NOT_FORWARDED' not in json.dumps((calls, audit, meta))


def test_two_empty_replies_have_precise_terminal_diagnostics(provider):
    _, calls, replies, audit = provider
    replies.extend([response(usage={"total_tokens": 1000}), response(usage={"total_tokens": 2000})])
    with pytest.raises(providers.ProviderError) as caught:
        providers.model_json('report', {})
    meta = caught.value.diagnostics
    assert len(calls) == 2 and meta['attempt_count'] == 2
    assert meta['status'] == 'empty_response'
    assert [a['status'] for a in meta['attempts']] == ['empty_response', 'empty_response']
    assert meta['usage_total']['total_tokens'] == 3000
    assert '未返回正文' in str(caught.value)
    assert '超时' not in str(caught.value) and '输出' not in str(caught.value)
    assert 'PRIVATE_CHAIN_NOT_FORWARDED' not in json.dumps((audit, meta))


@pytest.mark.parametrize('first,second,status', [
    ('length', 'stop', 'empty_response'), ('stop', 'length', 'length_exhausted')])
def test_length_and_empty_share_one_recovery_attempt(provider, first, second, status):
    _, calls, replies, _ = provider
    replies.extend([response(finish=first), response(finish=second)])
    with pytest.raises(providers.ProviderError) as caught:
        providers.model_json('report', {}, reasoning=True)
    assert len(calls) == caught.value.diagnostics['attempt_count'] == 2
    assert caught.value.diagnostics['status'] == status


def test_empty_recovery_obeys_cumulative_token_ledger(provider):
    _, calls, replies, _ = provider
    replies.extend([response(usage={'prompt_tokens': 12000, 'completion_tokens': 7740, 'total_tokens': 19740,
                                   'completion_tokens_details': {'reasoning_tokens': 7740}}),
                    response('{"ok":true}', usage={'prompt_tokens': 12000, 'completion_tokens': 42, 'total_tokens': 12042})])
    with execution_budget.scope({'total_tokens': 50000, 'time_minutes': 15}):
        _, meta = providers.model_json('report', {}, 7800, reasoning=True)
        usage = execution_budget.snapshot()
    assert usage['reported_tokens'] == usage['used_tokens'] == 31782
    assert usage['request_count'] == usage['completed_requests'] == 2
    assert usage['reserved_tokens'] == usage['estimated_tokens'] == 0
    assert meta['execution_token_usage'] == usage
    assert calls[1]['json']['max_tokens'] < 30260


def test_exhausted_token_budget_prevents_empty_recovery(provider):
    _, calls, replies, _ = provider
    replies.append(response(usage={'total_tokens': 50000}))
    with execution_budget.scope({'total_tokens': 50000, 'time_minutes': 15}):
        with pytest.raises(execution_budget.BudgetExhausted):
            providers.model_json('report', {}, reasoning=True)
        assert execution_budget.snapshot()['reported_tokens'] == 50000
        assert execution_budget.snapshot()['reserved_tokens'] == 0
    assert len(calls) == 1


def test_cancellation_after_empty_response_prevents_recovery(provider):
    _, calls, replies, _ = provider
    replies.append(response(usage={'total_tokens': 2000}))
    def cancel():
        if calls: raise Cancelled()
    with execution_budget.scope({'total_tokens': 50000, 'time_minutes': 15}):
        with pytest.raises(Cancelled):
            providers.model_json('report', {}, cancel_check=cancel)
        assert execution_budget.snapshot()['reported_tokens'] == 2000
        assert execution_budget.snapshot()['reserved_tokens'] == 0
    assert len(calls) == 1


def test_deadline_after_empty_response_prevents_recovery(provider, monkeypatch):
    _, calls, replies, _ = provider
    clock = [100]
    monkeypatch.setattr(providers.time, 'monotonic', lambda: clock[0])
    def finished():
        clock[0] = 112
        return response(usage={'total_tokens': 2000})
    replies.append(finished)
    with pytest.raises(providers.ProviderError) as caught:
        providers.model_json('report', {}, deadline=110)
    assert len(calls) == 1
    assert caught.value.diagnostics['status'] == 'deadline_exceeded'
    assert caught.value.diagnostics['usage_total']['total_tokens'] == 2000


@pytest.mark.parametrize('body,status', [
    (response(finish='content_filter'), 'provider_interrupted'),
    (response(refusal='declined'), 'provider_interrupted'),
    (response(finish='length', refusal='declined'), 'provider_interrupted'),
    (response(finish=None), 'empty_response'),
    (response('not json'), 'invalid_response'),
    (response([]), 'invalid_response'),
    (response(False), 'invalid_response'),
    (response('```json\n\n```'), 'invalid_response'),
])
def test_empty_recovery_does_not_expand_other_failure_policies(provider, body, status):
    _, calls, replies, _ = provider
    replies.append(body)
    with pytest.raises(providers.ProviderError) as caught:
        providers.model_json('report', {})
    assert len(calls) == 1
    assert caught.value.diagnostics['status'] == status


def test_anthropic_completed_empty_content_recovers_without_thinking_block(provider):
    cfg, calls, replies, audit = provider
    cfg.update(provider='anthropic', protocol='anthropic')
    replies.extend([{'content': [{'type': 'thinking', 'thinking': 'PRIVATE_CHAIN_NOT_FORWARDED'}],
                     'stop_reason': 'end_turn', 'usage': {'input_tokens': 500, 'output_tokens': 1000}},
                    {'content': [{'type': 'text', 'text': '{"ok":true}'}], 'stop_reason': 'end_turn',
                     'usage': {'input_tokens': 500, 'output_tokens': 30}}])
    result, meta = providers.model_json('report', {}, 2400)
    assert result['ok'] and len(calls) == 2
    assert [c['json']['max_tokens'] for c in calls] == [2400, 2400]
    assert meta['usage_total']['input_tokens'] == 1000
    assert meta['usage_total']['output_tokens'] == 1030
    assert 'PRIVATE_CHAIN_NOT_FORWARDED' not in json.dumps((calls, audit, meta))
