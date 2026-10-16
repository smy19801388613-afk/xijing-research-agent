"""Provider recovery contracts, entirely stubbed: no live or paid model calls."""
import copy
import json

import httpx
import pytest

from app import connections, providers, store
from app.research import Cancelled


@pytest.fixture
def provider(monkeypatch):
    cfg = {"provider": "deepseek", "protocol": "openai", "key": "private-api-key",
           "base": "https://example.com", "id": "test", "model": "deepseek-v4-pro", "json_mode": True}
    monkeypatch.setattr(connections, "resolve_model", lambda _: cfg)
    monkeypatch.setattr(providers.competition, "check_network", lambda *args: None)
    audit = []
    monkeypatch.setattr(providers.competition, "audit", lambda kind, **data: audit.append((kind, copy.deepcopy(data))))
    calls = []
    replies = []

    def post(url, **kwargs):
        calls.append(copy.deepcopy(kwargs))
        response = replies.pop(0)
        if isinstance(response, BaseException): raise response
        if callable(response): response = response(kwargs)
        return httpx.Response(200, json=response, request=httpx.Request("POST", url))

    monkeypatch.setattr(providers.httpx, "post", post)
    return cfg, calls, replies, audit


def completion(content='{"ok":true}', reason="stop", usage=None):
    return {"choices": [{"message": {"content": content, "reasoning_content": "private-reasoning-text"},
                         "finish_reason": reason}], "usage": usage or {}}


@pytest.mark.parametrize('base,reason', [
    ('https://api.deepseek.com', 'DeepSeek 账户余额不足'),
    ('https://example.com', '计费或账户额度限制'),
])
def test_payment_failure_is_not_misreported_as_local_token_budget(provider, base, reason):
    cfg, calls, replies, audit = provider
    cfg['base'] = base
    request = httpx.Request('POST', base + '/chat/completions')
    response = httpx.Response(402, json={'error': {'message': 'private-api-key'}}, request=request)
    replies.append(httpx.HTTPStatusError('rejected', request=request, response=response))
    with pytest.raises(providers.ProviderError, match=reason) as caught:
        providers.model_json('test', {}, 3400)
    assert len(calls) == 1
    assert '本轮 Token 或时间预算不同' in str(caught.value)
    assert caught.value.diagnostics['attempts'][0]['http_status'] == 402
    assert 'private-api-key' not in str(caught.value) + json.dumps(audit)


def test_successful_non_reasoning_call_keeps_existing_budget_and_request(provider):
    _, calls, replies, _ = provider
    replies.append(completion())
    result, meta = providers.model_json("test", {}, 3400)
    assert result == {"ok": True} and len(calls) == 1
    body = calls[0]["json"]
    assert body["max_tokens"] == 3400 and body["thinking"] == {"type": "disabled"}
    assert body["temperature"] == 0.1
    assert meta["requested_output_tokens"] == meta["actual_limit"] == 3400
    assert meta["attempt_count"] == 1 and meta["status"] == "completed"


def test_thinking_reserves_generation_space_and_records_only_safe_usage(provider):
    _, calls, replies, audit = provider
    usage = {"prompt_tokens": 108548, "completion_tokens": 11087, "total_tokens": 119635,
             "completion_tokens_details": {"reasoning_tokens": 3296, "text": "private-reasoning-text"},
             "api_key": "private-api-key", "diagnostic": "private-reasoning-text"}
    replies.append(completion(usage=usage))
    _, meta = providers.model_json("test", {}, 7800, reasoning=True)
    assert calls[0]["json"]["max_tokens"] == 32768
    assert "temperature" not in calls[0]["json"]
    assert meta["requested_output_tokens"] == 7800 and meta["actual_limit"] == 32768
    assert meta["usage"]["completion_tokens_details"] == {"reasoning_tokens": 3296}
    assert meta["usage_total"]["completion_tokens"] == 11087
    saved = json.dumps((meta, audit))
    assert "private-reasoning-text" not in saved and "private-api-key" not in saved


def test_length_regenerates_once_from_original_input_never_from_truncated_draft(provider):
    _, calls, replies, audit = provider
    # Even valid JSON with a length finish reason is not a completed answer.
    replies.extend([completion('{"discarded":"partial-secret-draft"}', "length",
                               {"completion_tokens": 32768, "completion_tokens_details": {"reasoning_tokens": 27000}}),
                    completion('{"complete":true}', usage={"completion_tokens": 12000})])
    value, meta = providers.model_json("report", {"facts": [1, 2]}, 7800, reasoning=True)
    assert value == {"complete": True}
    assert [c["json"]["max_tokens"] for c in calls] == [32768, 49152]
    assert calls[0]["json"]["messages"][1] == calls[1]["json"]["messages"][1]
    assert "partial-secret-draft" not in json.dumps((calls, audit, meta))
    assert "fresh, complete JSON" in calls[1]["json"]["messages"][0]["content"]
    assert meta["attempt_count"] == 2 and meta["usage_total"]["completion_tokens"] == 44768
    assert meta["attempts"][0]["truncation_cause"] == "generation_budget"
    assert meta["attempts"][1]["status"] == "completed"


def test_repeated_length_is_bounded_and_has_failure_diagnostics(provider):
    _, calls, replies, audit = provider
    replies.extend([completion('{"partial":', "length", {"completion_tokens": 32768}),
                    completion('{"partial":', "length", {"completion_tokens": 49152})])
    with pytest.raises(providers.ProviderError, match="两次") as caught:
        providers.model_json("report", {}, 7800, reasoning=True)
    meta = caught.value.diagnostics
    assert len(calls) == 2 and meta["status"] == "length_exhausted"
    assert meta["actual_limit"] == 49152 and meta["usage_total"]["completion_tokens"] == 81920
    assert all(a["status"] == "truncated" for a in meta["attempts"])
    assert audit[-1][0] == "model_failure"
    assert "private-reasoning-text" not in json.dumps((audit, meta))


def test_short_length_response_does_not_misdiagnose_total_context_as_answer_budget(provider):
    _, _, replies, _ = provider
    replies.extend([completion("", "length", {"prompt_tokens": 127000, "completion_tokens": 20}), completion()])
    _, meta = providers.model_json("test", {}, 3400)
    assert meta["attempts"][0]["truncation_cause"] == "output_or_context_limit"


@pytest.mark.parametrize("provider_name,protocol,token_key", [
    ("openai", "openai", "max_completion_tokens"), ("custom", "openai", "max_tokens"),
    ("anthropic", "anthropic", "max_tokens")])
def test_length_recovery_respects_provider_protocol(provider, provider_name, protocol, token_key):
    cfg, calls, replies, _ = provider
    cfg.update(provider=provider_name, protocol=protocol)
    if protocol == "anthropic":
        replies.extend([{"content": [{"type": "text", "text": '{"partial":'}], "stop_reason": "max_tokens", "usage": {"output_tokens": 2400}},
                        {"content": [{"type": "text", "text": '{"ok":true}'}], "stop_reason": "end_turn"}])
    else:
        replies.extend([completion("", "length"), completion()])
    result, meta = providers.model_json("test", {}, 2400)
    assert result["ok"] and [c["json"][token_key] for c in calls] == [2400, 4800]
    assert meta["attempt_count"] == 2


@pytest.mark.parametrize("body", [completion('{"unfinished":'), completion("[]"), {"choices": []}, {"choices": None}, []])
def test_invalid_response_is_not_retried_or_accepted(provider, body):
    _, calls, replies, _ = provider
    replies.append(body)
    with pytest.raises(providers.ProviderError) as caught:
        providers.model_json("test", {})
    assert len(calls) == 1 and caught.value.diagnostics["status"] == "invalid_response"


def test_provider_interruption_is_not_treated_as_complete_json(provider):
    _, calls, replies, _ = provider
    replies.append(completion('{"ok":true}', "content_filter"))
    with pytest.raises(providers.ProviderError) as caught:
        providers.model_json("test", {})
    assert len(calls) == 1 and caught.value.diagnostics["status"] == "provider_interrupted"


def test_cancellation_before_request_does_not_contact_provider(provider):
    _, calls, _, _ = provider
    def cancel(): raise Cancelled()
    with pytest.raises(Cancelled): providers.model_json("test", {}, cancel_check=cancel)
    assert calls == []


def test_cancellation_after_truncated_request_prevents_recovery(provider):
    _, calls, replies, _ = provider
    replies.append(completion("", "length"))
    def cancel():
        if calls: raise Cancelled()
    with pytest.raises(Cancelled): providers.model_json("test", {}, cancel_check=cancel)
    assert len(calls) == 1


def test_worker_stopping_exception_is_not_swallowed(provider, monkeypatch):
    _, calls, _, _ = provider
    def stopped(): raise store.ExecutionStopped()
    monkeypatch.setattr(store, "check_execution", stopped)
    with pytest.raises(store.ExecutionStopped): providers.model_json("test", {})
    assert calls == []


def test_deadline_before_request_has_diagnostics_but_no_contact(provider, monkeypatch):
    _, calls, _, _ = provider
    monkeypatch.setattr(providers.time, "monotonic", lambda: 100)
    with pytest.raises(providers.ProviderError) as caught:
        providers.model_json("test", {}, deadline=99)
    assert calls == [] and caught.value.diagnostics["status"] == "deadline_exceeded"


def test_deadline_after_first_response_does_not_start_recovery(provider, monkeypatch):
    _, calls, replies, _ = provider
    clock = [100]
    monkeypatch.setattr(providers.time, "monotonic", lambda: clock[0])
    def slow(_):
        clock[0] = 111
        return completion("", "length", {"completion_tokens": 2400})
    replies.append(slow)
    with pytest.raises(providers.ProviderError) as caught:
        providers.model_json("test", {}, deadline=110)
    assert len(calls) == 1 and calls[0]["timeout"] == 10
    assert caught.value.diagnostics["status"] == "deadline_exceeded"
    assert caught.value.diagnostics["usage_total"]["completion_tokens"] == 2400


@pytest.mark.parametrize("error,status", [(httpx.ReadTimeout("private-api-key"), "timeout"),
                                         (httpx.ConnectError("private-api-key"), "network_error")])
def test_network_failures_have_distinct_safe_diagnostics(provider, error, status):
    _, calls, replies, audit = provider
    replies.append(error)
    with pytest.raises(providers.ProviderError) as caught: providers.model_json("test", {})
    assert len(calls) == 1 and caught.value.diagnostics["status"] == status
    assert "private-api-key" not in str(caught.value) + json.dumps(audit)


def test_http_rejection_does_not_leak_body_or_attempt_unbounded_budget_guessing(provider):
    _, calls, replies, audit = provider
    request = httpx.Request("POST", "https://example.com")
    response = httpx.Response(400, json={"error": "private-api-key"}, request=request)
    replies.append(httpx.HTTPStatusError("private-api-key", request=request, response=response))
    with pytest.raises(providers.ProviderError, match="HTTP 400") as caught:
        providers.model_json("test", {}, reasoning=True)
    assert len(calls) == 1 and caught.value.diagnostics["attempts"][0]["http_status"] == 400
    assert "private-api-key" not in str(caught.value) + json.dumps(audit)


def test_retry_never_exceeds_application_generation_cap(provider):
    _, calls, replies, _ = provider
    replies.extend([completion("", "length"), completion()])
    providers.model_json("test", {}, 50000, reasoning=True)
    assert [call["json"]["max_tokens"] for call in calls] == [65536, 65536]
