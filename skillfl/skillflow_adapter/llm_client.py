"""LiteLLM-based wrapper that matches a `(prompt) -> response` callable.

Used by `PatcherBridge` (per-worker reflection on each trial). Kept separate
from that consumer so it stays testable without a network. CloudSkillMerge
does not call this directly — its in-container claude-code merger agent
talks to the gateway through its own claude-code launcher.

The dashscope Anthropic-compatible gateway expects the bare model name (no
"anthropic/" prefix) when called via the /v1/messages API, but LiteLLM needs
the "anthropic/<name>" form to route. We normalize here.

Rate-limit handling: dashscope (and any provider under heavy concurrent load)
returns 429 / RateLimitError. We retry such failures indefinitely with a
capped exponential backoff so the 15-way federated run never drops a patch or
merge call to a transient throttle. Auth/quota/payload errors still raise
immediately so genuine config bugs surface fast.
"""
from __future__ import annotations

import os
import random
import sys
import time
from typing import Callable

LLMCall = Callable[[str], str]


# Retry knobs — tunable via env so we can crank up for high-fanout runs without
# editing code. Defaults bias toward "wait longer, retry more" because losing
# a single patcher/merge call drops a worker's contribution from a federated
# round and cannot be recovered cheaply.
_RATELIMIT_BASE_SLEEP = float(os.environ.get("SKILLFL_LLM_RATELIMIT_BASE_SLEEP", "5"))
_RATELIMIT_MAX_SLEEP = float(os.environ.get("SKILLFL_LLM_RATELIMIT_MAX_SLEEP", "300"))
_TRANSIENT_MAX_RETRIES = int(os.environ.get("SKILLFL_LLM_TRANSIENT_MAX_RETRIES", "20"))


def _is_rate_limit(e: BaseException) -> bool:
    """Detect rate-limit / overload errors across litellm / openai / anthropic.

    We are intentionally permissive: when in doubt, treat as rate-limit so the
    call keeps retrying instead of dropping. Genuine config errors (auth,
    invalid model name, bad payload) won't match these patterns and will
    surface immediately.
    """
    name = type(e).__name__
    if name in {"RateLimitError", "Throttled", "OverloadedError"}:
        return True
    msg = str(e).lower()
    rl_phrases = (
        "rate limit", "ratelimit", "too many requests",
        # dashscope/qwen style overload responses come through as 5xx with
        # these phrases — we want infinite retry on those, not the bounded
        # transient path.
        "overloaded", "overload", "throttle", "rate exceeded",
        "concurrent", "qps limit", "tokens per minute", "tpm",
        "requests per minute", "rpm exceeded", "quota exceeded for the moment",
    )
    if any(p in msg for p in rl_phrases):
        return True
    status = getattr(e, "status_code", None) or getattr(e, "code", None)
    return status in (429, "429")


class _EmptyResponseError(Exception):
    """Provider returned 200 OK but the message content is empty/missing."""


def _validate_nonempty_response(resp) -> None:
    """Raise _EmptyResponseError if `resp` doesn't contain real text content.

    Why: dashscope (and other providers under load) sometimes return 200 with
    empty content or a content list of zero text blocks. The downstream patcher
    interprets "" as "no patch" and silently drops that worker's contribution.
    By raising here, the retry loop re-invokes the provider until we get real
    text. Min length 8 chars (after strip) catches "{}" and similar near-empty
    JSON without false-positiving on legitimate single-word answers.
    """
    if resp is None:
        raise _EmptyResponseError("response is None")
    choices = getattr(resp, "choices", None)
    if not choices:
        raise _EmptyResponseError("response.choices is empty/missing")
    msg = getattr(choices[0], "message", None)
    if msg is None:
        raise _EmptyResponseError("response.choices[0].message is None")
    content = getattr(msg, "content", None)
    text = ""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                t = block.get("text")
            else:
                t = getattr(block, "text", None)
            if isinstance(t, str):
                parts.append(t)
        text = "".join(parts)
    elif content is None:
        raise _EmptyResponseError("message.content is None")
    else:
        raise _EmptyResponseError(
            f"unexpected content type: {type(content).__name__}"
        )
    stripped = text.strip()
    if not stripped:
        raise _EmptyResponseError("content is empty/whitespace-only")
    if len(stripped) < 8:
        # Very short responses are usually error stubs ("{}", "null", "error").
        raise _EmptyResponseError(
            f"content too short ({len(stripped)} chars): {stripped!r}"
        )


def _is_transient(e: BaseException) -> bool:
    """Network blips, timeouts, internal 5xx — retry a bounded number of times."""
    name = type(e).__name__
    if name in {
        "APIConnectionError", "APITimeoutError", "Timeout",
        "ServiceUnavailableError", "InternalServerError",
        "ConnectionError", "ReadTimeout", "ConnectTimeout",
        "RemoteProtocolError", "ProtocolError",
    }:
        return True
    status = getattr(e, "status_code", None) or getattr(e, "code", None)
    if status in (500, 502, 503, 504, "500", "502", "503", "504"):
        return True
    msg = str(e).lower()
    transient_phrases = (
        "timeout", "connection reset", "temporarily unavailable",
        "connection refused", "broken pipe", "eof occurred",
        "server disconnected", "incomplete read",
    )
    return any(p in msg for p in transient_phrases)


def make_llm_call(
    *,
    model_name: str,
    api_base: str | None = None,
    api_key: str | None = None,
    temperature: float | None = None,
    max_tokens: int = 16384,
    extra_headers: dict[str, str] | None = None,
    provider_hint: str | None = None,
) -> LLMCall:
    """Build a callable (prompt -> response-text) using LiteLLM.

    The returned callable also exposes a `cost_state` dict attribute:
        {"last_cost": float, "total_cost": float}
    `last_cost` is the cost of the most recent call (in USD); `total_cost`
    is the cumulative across all calls on this callable. Mergers read
    `last_cost` to attribute spend to a single MergedPatch. Cost comes from
    `litellm.completion_cost(...)`; if the provider/model isn't recognized
    by litellm's pricing table the cost stays 0.0 and we move on (no signal
    is fine for proprietary endpoints).

    The LiteLLM import is deferred so callers that never use this (e.g. unit
    tests that inject their own callable) don't pay the import cost / need
    LiteLLM installed.
    """
    # Normalize model name to LiteLLM's provider-prefixed form.
    normalized = _normalize_model_for_litellm(model_name, api_base, provider_hint)

    # Build extra headers once; dashscope-style gateways sometimes expect the
    # key in an Authorization header as well.
    headers: dict[str, str] = dict(extra_headers or {})
    if provider_hint == "anthropic" and api_key and "Authorization" not in headers:
        headers["Authorization"] = api_key

    cost_state: dict[str, float] = {"last_cost": 0.0, "total_cost": 0.0}

    def _call(prompt: str) -> str:
        import litellm  # lazy

        kwargs: dict = {
            "model": normalized,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
        }
        # Only include temperature when explicitly set — otherwise let the
        # provider's default kick in (claude API: 1.0; openai: 1.0).
        if temperature is not None:
            kwargs["temperature"] = temperature
        if api_base:
            kwargs["api_base"] = api_base
        if api_key:
            kwargs["api_key"] = api_key
        if headers:
            kwargs["extra_headers"] = headers

        rate_attempt = 0
        transient_attempt = 0
        while True:
            try:
                resp = litellm.completion(**kwargs)
                # Validate the response actually has content. Some providers
                # return 200 OK with an empty/null content field on internal
                # errors — without this check we'd silently emit "" to the
                # caller (patcher/merger), which looks like "this worker had
                # nothing to add" and quietly drops their contribution.
                _validate_nonempty_response(resp)
                break
            except _EmptyResponseError as e:
                # Treat empty/garbage as transient so the caller retries.
                transient_attempt += 1
                if transient_attempt >= _TRANSIENT_MAX_RETRIES:
                    raise
                sleep = min(
                    _RATELIMIT_BASE_SLEEP * (2 ** (transient_attempt - 1)),
                    _RATELIMIT_MAX_SLEEP,
                ) * (0.5 + random.random())
                print(
                    f"[llm_client] empty/invalid response "
                    f"(attempt {transient_attempt}/{_TRANSIENT_MAX_RETRIES}, "
                    f"sleep {sleep:.1f}s): {e}",
                    file=sys.stderr, flush=True,
                )
                time.sleep(sleep)
                continue
            except Exception as e:
                if _is_rate_limit(e):
                    # Capped exponential backoff with jitter; retry forever.
                    rate_attempt += 1
                    sleep = min(
                        _RATELIMIT_BASE_SLEEP * (2 ** min(rate_attempt - 1, 5)),
                        _RATELIMIT_MAX_SLEEP,
                    ) * (0.5 + random.random())
                    print(
                        f"[llm_client] rate-limit (attempt {rate_attempt}, "
                        f"sleep {sleep:.1f}s): {type(e).__name__}: {e}",
                        file=sys.stderr, flush=True,
                    )
                    time.sleep(sleep)
                    continue
                if _is_transient(e) and transient_attempt < _TRANSIENT_MAX_RETRIES:
                    transient_attempt += 1
                    sleep = min(
                        _RATELIMIT_BASE_SLEEP * (2 ** (transient_attempt - 1)),
                        _RATELIMIT_MAX_SLEEP,
                    ) * (0.5 + random.random())
                    print(
                        f"[llm_client] transient error "
                        f"(attempt {transient_attempt}/{_TRANSIENT_MAX_RETRIES}, "
                        f"sleep {sleep:.1f}s): {type(e).__name__}: {e}",
                        file=sys.stderr, flush=True,
                    )
                    time.sleep(sleep)
                    continue
                raise
        # Cost: best-effort. completion_cost can return None or raise for
        # unknown providers (dashscope, custom gateways). Treat any failure
        # as 0.0 — we'd rather under-report than crash the merger.
        try:
            cost = float(litellm.completion_cost(completion_response=resp) or 0.0)
        except Exception:
            cost = 0.0
        cost_state["last_cost"] = cost
        cost_state["total_cost"] += cost
        # Anthropic-style response via LiteLLM: resp.choices[0].message.content
        # (list-of-blocks) or a plain string depending on adapter version.
        msg = resp.choices[0].message
        content = getattr(msg, "content", None)
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            # Concat any "text" blocks.
            parts: list[str] = []
            for block in content:
                if isinstance(block, dict):
                    t = block.get("text")
                    if isinstance(t, str):
                        parts.append(t)
                else:
                    t = getattr(block, "text", None)
                    if isinstance(t, str):
                        parts.append(t)
            return "".join(parts)
        raise RuntimeError(f"Unexpected LiteLLM response shape: {type(content).__name__}")

    _call.cost_state = cost_state  # type: ignore[attr-defined]
    return _call


def _normalize_model_for_litellm(
    model_name: str,
    api_base: str | None,
    provider_hint: str | None,
) -> str:
    name = model_name.strip()
    if "/" in name:
        return name
    api_base_lower = (api_base or "").rstrip("/").lower()
    hint = (provider_hint or "").lower()
    lname = name.lower()
    if (
        hint == "anthropic"
        or "/anthropic" in api_base_lower
        or lname.startswith(("claude", "vertex.claude"))
    ):
        return f"anthropic/{name}"
    if hint == "gemini" or "/google/" in api_base_lower or lname.startswith("gemini"):
        return f"gemini/{name}"
    if hint == "openai" or "/openai" in api_base_lower:
        return f"openai/{name}"
    # Default: assume OpenAI-compatible. Tune later if another provider lands
    # here without a detectable hint.
    return f"openai/{name}"
