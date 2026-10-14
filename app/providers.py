from . import competition
import json
import time
import httpx
from .config import settings

class ProviderError(Exception):
    """A user-safe error with optional numeric, credential-free diagnostics."""
    def __init__(self, message, diagnostics=None):
        super().__init__(message)
        self.diagnostics = diagnostics or {}


def tushare(api,params,fields):
    cfg=settings()
    if not cfg["token"]:
        raise ProviderError("未配置 Tushare Token")
    for attempt in range(2):
        try:
            competition.check_network("https://api.tushare.pro")
            response=httpx.post("https://api.tushare.pro",json={"api_name":api,"token":cfg["token"],"params":params,"fields":fields},timeout=25)
            response.raise_for_status()
            payload=response.json()
            if payload.get("code")!=0:
                raise ProviderError(f"Tushare {api} 未返回数据（权限、额度或参数错误，code={payload.get('code')}）")
            data=payload.get("data") or {}
            return [dict(zip(data.get("fields",[]),r)) for r in data.get("items",[])]
        except (httpx.TimeoutException,httpx.NetworkError):
            if attempt: raise ProviderError(f"Tushare {api} 网络请求失败，两次尝试后停止") from None
        except (httpx.HTTPStatusError,ValueError):
            raise ProviderError(f"Tushare {api} 响应异常，未取得有效数据") from None
    return []

# These are application cost bounds, not claims about every provider's capacity.
# DeepSeek counts hidden reasoning and the final answer against the same limit.
MAX_GENERATION_TOKENS = 65536
_USAGE_COUNTERS = frozenset(("prompt_tokens", "completion_tokens", "total_tokens",
    "input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
    "prompt_cache_hit_tokens", "prompt_cache_miss_tokens", "cached_tokens", "reasoning_tokens",
    "audio_tokens", "accepted_prediction_tokens", "rejected_prediction_tokens"))
_USAGE_DETAILS = frozenset(("prompt_tokens_details", "completion_tokens_details",
    "input_tokens_details", "output_tokens_details"))


def _safe_usage(raw):
    """Only provider token counts belong in public call metadata, never response text."""
    if not isinstance(raw, dict): return {}
    result = {}
    for key, value in raw.items():
        if key in _USAGE_COUNTERS and isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            result[key] = value
        elif key in _USAGE_DETAILS and isinstance(value, dict):
            result[key] = _safe_usage(value)
    return result


def _usage_total(attempts):
    def add(total, part):
        for key, value in part.items():
            if isinstance(value, dict): add(total.setdefault(key, {}), value)
            else: total[key] = total.get(key, 0) + value
    total = {}
    for attempt in attempts: add(total, attempt.get("usage", {}))
    return total


def model_json(system, payload, max_tokens=2400, profile_id=None, reasoning=False,
               *, cancel_check=None, deadline=None):
    """Generate one complete JSON object, with at most one length recovery.

    max_tokens is the desired answer budget. DeepSeek thinking needs a separate
    reserve within its combined generation limit. A retry always starts from the
    original evidence; no incomplete JSON or private reasoning is reused.
    deadline is an optional absolute time.monotonic() deadline.
    """
    from .connections import resolve_model, validate_base
    from . import store, execution_budget
    cfg = resolve_model(profile_id)
    if not cfg["key"] and cfg["provider"] != "ollama":
        raise ProviderError("当前模型连接尚未配置 API Key，请在连接设置中填写")
    if not cfg["model"]: raise ProviderError("请先在连接设置中填写模型名称")
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or not 1 <= max_tokens <= MAX_GENERATION_TOKENS:
        raise ValueError("模型正文预算须为 1 至 65536 之间的整数")
    validate_base(cfg["base"])
    started = time.monotonic()
    thinking = bool(reasoning and cfg["provider"] == "deepseek" and cfg["protocol"] != "anthropic")
    first_limit = min(MAX_GENERATION_TOKENS, max(32768, max_tokens + 16384)) if thinking else max_tokens
    # Unknown compatible providers may have smaller limits. Their HTTP rejection is
    # surfaced, never followed by an unbounded parameter guessing/retry loop.
    second_limit = min(MAX_GENERATION_TOKENS, first_limit + first_limit // 2) if thinking else min(32768, first_limit * 2)
    second_limit = max(first_limit, second_limit)
    budget=execution_budget.current()
    if budget:
        cap=budget['generation_tokens']
        first_limit=min(first_limit,cap)
        # The selected ceiling also bounds the recovery request. Ordinary short
        # decisions can use less; a long reasoning draft may recover up to it.
        second_limit=cap if reasoning else min(second_limit,cap)
    shared_deadline=execution_budget.deadline()
    if shared_deadline is not None:
        deadline=min(deadline,shared_deadline) if deadline is not None else shared_deadline
    answer_budget=min(max_tokens,first_limit)
    attempts = []
    actual_limit = first_limit

    def metadata(status):
        return {"model": cfg["model"], "provider": cfg["provider"], "connection_id": cfg["id"],
            "reasoning_enabled": thinking, "status": status,
            "requested_output_tokens": max_tokens, "actual_limit": actual_limit,
            "execution_generation_cap": budget['generation_tokens'] if budget else None,
            "generation_limit_scope": "reasoning_and_answer" if thinking or cfg["provider"] == "openai" else "provider_completion",
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "attempt_count": len(attempts), "attempts": list(attempts),
            "usage": attempts[-1].get("usage", {}) if attempts else {}, "usage_total": _usage_total(attempts)}

    def fail(message, status):
        diagnostics = metadata(status)
        competition.audit("model_failure", diagnostics=diagnostics)
        raise ProviderError(message, diagnostics) from None

    def check():
        # The worker also terminates in-flight requests; this prevents a stale
        # worker or callback cancellation from starting a recovery request.
        store.check_execution()
        if deadline is not None and time.monotonic() >= deadline:
            fail("模型生成达到本轮时间预算，已保留已有证据，未保存不完整草稿", "deadline_exceeded")
        if cancel_check: cancel_check()

    headers = {"Content-Type": "application/json"}
    if cfg["protocol"] == "anthropic":
        headers.update({"x-api-key": cfg["key"], "anthropic-version": "2023-06-01"})
        endpoint = "/messages"
    else:
        if cfg["key"]: headers["Authorization"] = "Bearer " + cfg["key"]
        endpoint = "/chat/completions"
    message = {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}
    prompt = system + "\nReturn a valid JSON object only, no markdown fences."
    if thinking:
        prompt += f"\nKeep the final JSON answer within approximately {answer_budget} tokens. Preserve required fields, source IDs and uncertainty; avoid repeating source text."

    for index, actual_limit in enumerate((first_limit, second_limit), 1):
        check()
        current_prompt = prompt
        if index > 1:
            current_prompt += ("\nThe previous response exceeded a generation or context limit and was discarded. "
                "Generate a fresh, complete JSON object from the original inputs. Be concise: remove repetition, "
                "use short exact evidence quotes, preserve required fields and material uncertainty. "
                "Do not continue or refer to the discarded response, invent facts, or claim unfinished work completed.")
        if cfg["protocol"] == "anthropic":
            body = {"model": cfg["model"], "system": current_prompt, "messages": [message], "max_tokens": actual_limit}
        else:
            body = {"model": cfg["model"], "messages": [{"role": "system", "content": current_prompt}, message]}
            body["max_completion_tokens" if cfg["provider"] == "openai" else "max_tokens"] = actual_limit
            if cfg.get("json_mode", True): body["response_format"] = {"type": "json_object"}
            if cfg["provider"] == "deepseek":
                body["thinking"] = {"type": "enabled" if thinking else "disabled"}
                if not thinking: body["temperature"] = 0.1
        competition.check_network(cfg["base"] + endpoint, "model")
        competition.audit("model_request", model=cfg["model"], protocol=cfg["protocol"], system=current_prompt,
            payload=payload, max_tokens=actual_limit, answer_budget=max_tokens, attempt=index)
        attempt = {"attempt": index, "actual_limit": actual_limit, "status": "requesting", "usage": {}}
        attempts.append(attempt)
        attempt_started = time.monotonic()
        timeout = 300 if reasoning else 80
        if deadline is not None: timeout = max(0.001, min(timeout, deadline - time.monotonic()))
        try:
            response = httpx.post(cfg["base"] + endpoint, headers=headers, json=body, timeout=timeout)
            response.raise_for_status()
            data = response.json()
            attempt["usage"] = _safe_usage(data.get("usage"))
            if cfg["protocol"] == "anthropic":
                content = "".join(block["text"] for block in data.get("content", []) if block.get("type") == "text")
                finish_reason = data.get("stop_reason")
                stopped = finish_reason == "max_tokens"
                completed = finish_reason in (None, "end_turn", "stop_sequence")
            else:
                choice = data["choices"][0]
                content = choice["message"].get("content") or ""
                finish_reason = choice.get("finish_reason")
                stopped = finish_reason == "length"
                completed = finish_reason in (None, "stop")
            if not isinstance(content, str): raise ValueError()
            known_stops = ("stop", "length", "max_tokens", "end_turn", "stop_sequence", "content_filter",
                           "tool_calls", "insufficient_system_resource", "aborted", "refusal", "pause_turn")
            attempt.update(finish_reason=finish_reason if finish_reason in known_stops or finish_reason is None else "unknown",
                           content_chars=len(content), elapsed_ms=int((time.monotonic() - attempt_started) * 1000),
                           status="truncated" if stopped else "received")
            check()
            if stopped:
                generated = attempt["usage"].get("completion_tokens", attempt["usage"].get("output_tokens"))
                attempt.update(status="truncated", truncation_cause=("generation_budget" if isinstance(generated, int) and generated >= actual_limit else "output_or_context_limit"))
                competition.audit("model_attempt", diagnostics=attempt)
                if index == 1:
                    continue
                fail("模型两次生成仍达到输出或上下文上限，未采用不完整草稿；可调整生成额度、减少单次研究问题或分段更新报告", "length_exhausted")
            if not completed:
                attempt["status"] = "interrupted"
                fail("模型服务中断了本次生成，未采用不完整草稿；已有证据仍保留", "provider_interrupted")
            content = content.strip()
            fence = chr(96) * 3
            if content.startswith(fence): content = content.split("\n", 1)[-1].rsplit(fence, 1)[0].strip()
            value = json.loads(content)
            if not isinstance(value, dict): raise ValueError()
            attempt["status"] = "completed"
            result_meta = metadata("completed")
            competition.audit("model_response", model=cfg["model"], result=value, diagnostics=result_meta)
            return value, result_meta
        except competition.PolicyError: raise
        except httpx.HTTPStatusError as error:
            attempt.update(status="http_error", http_status=error.response.status_code,
                           elapsed_ms=int((time.monotonic() - attempt_started) * 1000))
            fail(f"模型接口返回 HTTP {error.response.status_code}；请检查当前服务的密钥、模型名、接口协议和输出额度", "http_error")
        except httpx.TimeoutException:
            attempt.update(status="timeout", elapsed_ms=int((time.monotonic() - attempt_started) * 1000))
            check()
            fail("模型响应超时，已保留已有证据；可缩小本次问题范围后继续", "timeout")
        except httpx.HTTPError:
            attempt.update(status="network_error", elapsed_ms=int((time.monotonic() - attempt_started) * 1000))
            fail("模型网络请求失败，已保留已有证据；请检查当前模型连接", "network_error")
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            attempt.update(status="invalid_response", elapsed_ms=int((time.monotonic() - attempt_started) * 1000))
            fail("模型调用超时、请求失败或未返回有效 JSON；已保留财务证据，不生成无依据结论", "invalid_response")


def list_models(profile_id):
    from .connections import resolve_model
    cfg=resolve_model(profile_id)
    headers={"x-api-key":cfg["key"],"anthropic-version":"2023-06-01"} if cfg["protocol"]=="anthropic" else ({"Authorization":"Bearer "+cfg["key"]} if cfg["key"] else {})
    try:
        competition.check_network(cfg["base"]+"/models","model")
        response=httpx.get(cfg["base"]+"/models",headers=headers,timeout=15)
        response.raise_for_status()
        models=[v["id"] for v in response.json().get("data",[]) if isinstance(v.get("id"),str)]
        return {"models":sorted(models)[:500],"detail":"来自当前服务的模型列表"}
    except (httpx.HTTPError,ValueError,KeyError):
        raise ProviderError("该服务未返回模型列表；可按服务商提供的模型名称手动填写") from None
