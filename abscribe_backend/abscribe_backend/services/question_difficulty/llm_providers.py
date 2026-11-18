"""Provider abstraction for multi-model respondent LLM calls.

OpenAI is implemented now. Other providers are stubbed with clear extension points.

Async calibrate/score runs should enter ``shared_async_openai_client()`` so all
calls share one httpx pool with TCP keepalive (avoids silent NAT idle drops and
per-call pool churn). Timeout/retry remains the safety net.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import random
import socket
import sys
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any, NamedTuple, Optional


class CompletionResult(NamedTuple):
    """LLM completion text plus provider finish_reason (e.g. stop / length)."""

    text: str
    finish_reason: str | None = None

# Prefer gunicorn's error logger so lines land in --error-logfile (not tmux stdout).
_logger = logging.getLogger("gunicorn.error")
if not _logger.handlers:
    _logger = logging.getLogger("abscribe_backend.services.question_difficulty.llm_providers")
    if not _logger.handlers:
        _handler = logging.StreamHandler(sys.stderr)
        _handler.setFormatter(logging.Formatter("%(message)s"))
        _logger.addHandler(_handler)
        _logger.setLevel(logging.INFO)
        _logger.propagate = False

from .model_pool import (
    PROVIDER_COHERE,
    PROVIDER_GOOGLE,
    PROVIDER_OPENAI,
    PROVIDER_REPLICATE,
    SUPPORTED_PROVIDERS,
)

# Retries for 429 / transient rate limits before failing the call.
_RATE_LIMIT_MAX_RETRIES = int(os.getenv("QUESTION_DIFFICULTY_RATE_LIMIT_RETRIES", "6"))
_RATE_LIMIT_BASE_SLEEP_SEC = float(
    os.getenv("QUESTION_DIFFICULTY_RATE_LIMIT_BASE_SLEEP", "1.0")
)
_RATE_LIMIT_MAX_SLEEP_SEC = float(
    os.getenv("QUESTION_DIFFICULTY_RATE_LIMIT_MAX_SLEEP", "60.0")
)

# Per-attempt transport timeout. Fail fast on hung connections; retries re-issue
# the same call. Legitimate slow calls (~few seconds; rare outliers longer) are
# cheap to redo if killed.
_LLM_TIMEOUT_SEC = float(os.getenv("QUESTION_DIFFICULTY_LLM_TIMEOUT_SEC", "45"))
# Hard ceiling slightly above client timeout so httpx/OpenAI errors surface first.
_LLM_WAIT_FOR_SEC = float(
    os.getenv(
        "QUESTION_DIFFICULTY_LLM_WAIT_FOR_SEC",
        str(_LLM_TIMEOUT_SEC + 5.0),
    )
)
# Bound retries on timeout/connection (not infinite — a permanently broken cell
# must fail the run loudly rather than hang forever).
_LLM_MAX_ATTEMPTS = int(os.getenv("QUESTION_DIFFICULTY_LLM_MAX_ATTEMPTS", "4"))
_LLM_RETRY_BUDGET_SEC = float(
    os.getenv("QUESTION_DIFFICULTY_LLM_RETRY_BUDGET_SEC", "900")
)  # 15 minutes wall clock per cell
# Bound completion length so temp=1.75 cannot runaway-generate for minutes.
# Respondents are instructed "Answer only. No prose." — 50 is ample for keys.
_LLM_MAX_TOKENS = int(os.getenv("QUESTION_DIFFICULTY_MAX_TOKENS", "150"))

_async_openai_client_var: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "prompthive_async_openai_client", default=None
)
_sync_openai_client_var: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "prompthive_sync_openai_client", default=None
)


class LLMCallError(RuntimeError):
    """Exhausted retries for a single respondent×item (or batch) LLM call."""


def _async_concurrency_limit() -> int:
    return max(1, int(os.getenv("QUESTION_DIFFICULTY_ASYNC_CONCURRENCY", "40")))


def _tcp_keepalive_socket_options() -> list[tuple[int, int, int]]:
    """TCP keepalive so middleboxes cannot silently drop idle HTTPS conns."""
    opts: list[tuple[int, int, int]] = [
        (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),
    ]
    # Linux (Quadro); Darwin uses TCP_KEEPALIVE for idle in some versions.
    if hasattr(socket, "TCP_KEEPIDLE"):
        opts.append((socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60))
    elif hasattr(socket, "TCP_KEEPALIVE"):
        opts.append((socket.IPPROTO_TCP, socket.TCP_KEEPALIVE, 60))
    if hasattr(socket, "TCP_KEEPINTVL"):
        opts.append((socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 30))
    if hasattr(socket, "TCP_KEEPCNT"):
        opts.append((socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 5))
    return opts


def _require_api_key() -> str:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError(
            "OPENAI_API_KEY not set. Please configure .env with your OpenAI API key."
        )
    return api_key


def _build_async_openai_client(*, max_connections: int) -> Any:
    """One AsyncOpenAI + httpx pool with keepalive, sized to run concurrency."""
    try:
        import httpx
        from openai import AsyncOpenAI
    except ImportError as exc:
        raise ImportError("openai package not installed. Run: pip install openai") from exc

    max_connections = max(1, int(max_connections))
    timeout = httpx.Timeout(_LLM_TIMEOUT_SEC, connect=10.0)
    limits = httpx.Limits(
        max_connections=max_connections,
        max_keepalive_connections=max_connections,
        keepalive_expiry=60.0,
    )
    transport = httpx.AsyncHTTPTransport(
        limits=limits,
        socket_options=_tcp_keepalive_socket_options(),
    )
    http_client = httpx.AsyncClient(transport=transport, timeout=timeout)
    return AsyncOpenAI(
        api_key=_require_api_key(),
        http_client=http_client,
        timeout=_LLM_TIMEOUT_SEC,
        max_retries=0,
    )


def _build_sync_openai_client(*, max_connections: int) -> Any:
    try:
        import httpx
        from openai import OpenAI
    except ImportError as exc:
        raise ImportError("openai package not installed. Run: pip install openai") from exc

    max_connections = max(1, int(max_connections))
    timeout = httpx.Timeout(_LLM_TIMEOUT_SEC, connect=10.0)
    limits = httpx.Limits(
        max_connections=max_connections,
        max_keepalive_connections=max_connections,
        keepalive_expiry=60.0,
    )
    transport = httpx.HTTPTransport(
        limits=limits,
        socket_options=_tcp_keepalive_socket_options(),
    )
    http_client = httpx.Client(transport=transport, timeout=timeout)
    return OpenAI(
        api_key=_require_api_key(),
        http_client=http_client,
        timeout=_LLM_TIMEOUT_SEC,
        max_retries=0,
    )


@asynccontextmanager
async def shared_async_openai_client(
    *,
    max_connections: int | None = None,
) -> AsyncIterator[Any]:
    """Run-scoped AsyncOpenAI client (keepalive + bounded pool). Always aclose's."""
    if max_connections is None:
        max_connections = _async_concurrency_limit()
    client = _build_async_openai_client(max_connections=max_connections)
    token = _async_openai_client_var.set(client)
    try:
        yield client
    finally:
        _async_openai_client_var.reset(token)
        try:
            # Prefer aclose when present (httpx / newer SDKs); else close().
            closer = getattr(client, "aclose", None) or client.close
            result = closer()
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:
            _logger.warning(
                "[llm_providers] shared AsyncOpenAI close failed: %s: %s",
                type(exc).__name__,
                exc,
            )


@contextmanager
def shared_sync_openai_client(
    *,
    max_connections: int | None = None,
) -> Iterator[Any]:
    """Run-scoped sync OpenAI client; always closed on exit."""
    if max_connections is None:
        max_connections = _async_concurrency_limit()
    client = _build_sync_openai_client(max_connections=max_connections)
    token = _sync_openai_client_var.set(client)
    try:
        yield client
    finally:
        _sync_openai_client_var.reset(token)
        try:
            client.close()
        except Exception as exc:
            _logger.warning(
                "[llm_providers] shared OpenAI close failed: %s: %s",
                type(exc).__name__,
                exc,
            )


def _build_messages(
    prompt: str | list,
    system_prompt: Optional[str],
) -> list[dict]:
    messages: list[dict] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})
    return messages


def _is_rate_limit_error(exc: BaseException) -> bool:
    try:
        from openai import RateLimitError
    except ImportError:
        RateLimitError = ()  # type: ignore[misc, assignment]
    if RateLimitError and isinstance(exc, RateLimitError):
        return True
    status = getattr(exc, "status_code", None)
    if status == 429:
        return True
    name = type(exc).__name__
    if "RateLimit" in name:
        return True
    msg = str(exc).lower()
    return "rate limit" in msg or "429" in msg


def _is_timeout_error(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return True
    try:
        from openai import APITimeoutError
    except ImportError:
        APITimeoutError = ()  # type: ignore[misc, assignment]
    if APITimeoutError and isinstance(exc, APITimeoutError):
        return True
    name = type(exc).__name__
    if "Timeout" in name:
        return True
    msg = str(exc).lower()
    return "timed out" in msg or "timeout" in msg


def _is_connection_error(exc: BaseException) -> bool:
    try:
        from openai import APIConnectionError
    except ImportError:
        APIConnectionError = ()  # type: ignore[misc, assignment]
    if APIConnectionError and isinstance(exc, APIConnectionError):
        return True
    name = type(exc).__name__
    if any(
        token in name
        for token in ("APIConnection", "ConnectError", "ConnectionError", "ConnectTimeout")
    ):
        return True
    msg = str(exc).lower()
    return "connection" in msg and (
        "reset" in msg or "refused" in msg or "closed" in msg or "error" in msg
    )


def _is_retryable_error(exc: BaseException) -> bool:
    """Timeouts, connection failures, and 429s are safe to re-issue."""
    return (
        _is_rate_limit_error(exc)
        or _is_timeout_error(exc)
        or _is_connection_error(exc)
    )


def _retry_after_seconds(exc: BaseException) -> float | None:
    """Honor Retry-After when present (seconds or HTTP-date ignored → None)."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) if response is not None else None
    if headers is None:
        return None
    raw = None
    try:
        raw = headers.get("retry-after") or headers.get("Retry-After")
    except Exception:
        raw = None
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None


def _backoff_sleep_seconds(attempt: int, exc: BaseException) -> float:
    """Exponential backoff with jitter; prefer Retry-After when provided."""
    retry_after = _retry_after_seconds(exc)
    if retry_after is not None:
        # Small jitter so concurrent greenlets don't stampede.
        return min(_RATE_LIMIT_MAX_SLEEP_SEC, retry_after + random.uniform(0, 0.5))
    exp = _RATE_LIMIT_BASE_SLEEP_SEC * (2**attempt)
    jittered = exp * (0.5 + random.random())  # 0.5x–1.5x
    return min(_RATE_LIMIT_MAX_SLEEP_SEC, jittered)


def _attempts_exhausted(attempt: int, started_monotonic: float) -> bool:
    if attempt >= _LLM_MAX_ATTEMPTS:
        return True
    return (time.monotonic() - started_monotonic) >= _LLM_RETRY_BUDGET_SEC


def _format_call_context(
    respondent_id: str | None,
    item_id: str | None,
) -> str:
    parts = []
    if respondent_id:
        parts.append(f"respondent={respondent_id}")
    if item_id:
        parts.append(f"item={item_id}")
    return (" ".join(parts) + " ") if parts else ""


def _log_retry(
    *,
    kind: str,
    exc: BaseException,
    attempt: int,
    sleep_s: float,
    respondent_id: str | None = None,
    item_id: str | None = None,
) -> None:
    """Log retries to gunicorn error log (falls back to stderr outside gunicorn)."""
    ctx = _format_call_context(respondent_id, item_id)
    _logger.warning(
        "[llm_providers] OpenAI %s %s: %s | %ssleep %.1fs (attempt %s/%s)",
        kind,
        type(exc).__name__,
        exc,
        ctx,
        sleep_s,
        attempt,
        _LLM_MAX_ATTEMPTS,
    )


def _completion_kwargs(
    *,
    model_name: str,
    prompt: str | list,
    system_prompt: Optional[str],
    temperature: float,
    seed: int | None,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "model": model_name,
        "messages": _build_messages(prompt, system_prompt),
        "temperature": temperature,
        "max_tokens": _LLM_MAX_TOKENS if max_tokens is None else int(max_tokens),
    }
    if seed is not None:
        kwargs["seed"] = seed
    return kwargs


def _maybe_log_truncation(
    response: Any,
    *,
    respondent_id: str | None,
    item_id: str | None,
    max_tokens: int | None = None,
) -> None:
    """Warn when the model hit max_tokens (possible truncation of a real answer).

    ``max_tokens`` is the per-call cap when one was given (e.g. the answer key
    check's 400); otherwise the provider default applies.
    """
    try:
        choice = response.choices[0]
    except Exception:
        return
    cap = _LLM_MAX_TOKENS if max_tokens is None else int(max_tokens)
    finish = getattr(choice, "finish_reason", None)
    usage = getattr(response, "usage", None)
    completion_tokens = getattr(usage, "completion_tokens", None) if usage else None
    hit_cap = finish == "length" or (
        completion_tokens is not None and int(completion_tokens) >= cap
    )
    if not hit_cap:
        return
    ctx = _format_call_context(respondent_id, item_id)
    _logger.warning(
        "[llm_providers] possible truncation at max_tokens=%s "
        "(finish_reason=%s completion_tokens=%s) | %s",
        cap,
        finish,
        completion_tokens,
        ctx.strip(),
    )


def _ask_openai(
    *,
    prompt: str | list,
    seed: int,
    temperature: float,
    model_name: str,
    system_prompt: Optional[str],
    respondent_id: str | None = None,
    item_id: str | None = None,
    max_tokens: int | None = None,
) -> CompletionResult:
    shared = _sync_openai_client_var.get()
    owns_client = False
    if shared is not None:
        client = shared
    else:
        # Fallback for call sites outside shared_sync_openai_client().
        client = _build_sync_openai_client(max_connections=1)
        owns_client = True

    kwargs = _completion_kwargs(
        model_name=model_name,
        prompt=prompt,
        system_prompt=system_prompt,
        temperature=temperature,
        seed=seed,
        max_tokens=max_tokens,
    )

    started = time.monotonic()
    attempt = 0
    try:
        while True:
            attempt += 1
            try:
                response = client.chat.completions.create(**kwargs)
                _maybe_log_truncation(
                    response,
                    respondent_id=respondent_id,
                    item_id=item_id,
                    max_tokens=max_tokens,
                )
                choice = response.choices[0]
                text = (choice.message.content or "").strip()
                return CompletionResult(
                    text=text,
                    finish_reason=getattr(choice, "finish_reason", None),
                )
            except Exception as exc:
                if not _is_retryable_error(exc) or _attempts_exhausted(attempt, started):
                    raise LLMCallError(
                        f"OpenAI call failed after {attempt} attempt(s) "
                        f"over {time.monotonic() - started:.1f}s: {exc}"
                    ) from exc
                sleep_s = _backoff_sleep_seconds(attempt - 1, exc)
                remaining = _LLM_RETRY_BUDGET_SEC - (time.monotonic() - started)
                if remaining <= 0:
                    raise LLMCallError(
                        f"OpenAI call failed after {attempt} attempt(s) "
                        f"(retry budget {_LLM_RETRY_BUDGET_SEC:.0f}s exhausted): {exc}"
                    ) from exc
                sleep_s = min(sleep_s, max(0.0, remaining))
                kind = (
                    "rate limit"
                    if _is_rate_limit_error(exc)
                    else ("timeout" if _is_timeout_error(exc) else "connection")
                )
                _log_retry(
                    kind=kind,
                    exc=exc,
                    attempt=attempt,
                    sleep_s=sleep_s,
                    respondent_id=respondent_id,
                    item_id=item_id,
                )
                time.sleep(sleep_s)
    finally:
        if owns_client:
            try:
                client.close()
            except Exception:
                pass


async def _ask_openai_async(
    *,
    prompt: str | list,
    seed: int,
    temperature: float,
    model_name: str,
    system_prompt: Optional[str],
    respondent_id: str | None = None,
    item_id: str | None = None,
    max_tokens: int | None = None,
) -> CompletionResult:
    shared = _async_openai_client_var.get()
    owns_client = False
    if shared is not None:
        client = shared
    else:
        # Fallback for call sites outside shared_async_openai_client().
        client = _build_async_openai_client(max_connections=1)
        owns_client = True

    kwargs = _completion_kwargs(
        model_name=model_name,
        prompt=prompt,
        system_prompt=system_prompt,
        temperature=temperature,
        seed=seed,
        max_tokens=max_tokens,
    )

    started = time.monotonic()
    attempt = 0
    try:
        while True:
            attempt += 1
            try:
                response = await asyncio.wait_for(
                    client.chat.completions.create(**kwargs),
                    timeout=_LLM_WAIT_FOR_SEC,
                )
                _maybe_log_truncation(
                    response,
                    respondent_id=respondent_id,
                    item_id=item_id,
                    max_tokens=max_tokens,
                )
                choice = response.choices[0]
                text = (choice.message.content or "").strip()
                return CompletionResult(
                    text=text,
                    finish_reason=getattr(choice, "finish_reason", None),
                )
            except Exception as exc:
                if not _is_retryable_error(exc) or _attempts_exhausted(attempt, started):
                    raise LLMCallError(
                        f"OpenAI call failed after {attempt} attempt(s) "
                        f"over {time.monotonic() - started:.1f}s: {exc}"
                    ) from exc
                sleep_s = _backoff_sleep_seconds(attempt - 1, exc)
                remaining = _LLM_RETRY_BUDGET_SEC - (time.monotonic() - started)
                if remaining <= 0:
                    raise LLMCallError(
                        f"OpenAI call failed after {attempt} attempt(s) "
                        f"(retry budget {_LLM_RETRY_BUDGET_SEC:.0f}s exhausted): {exc}"
                    ) from exc
                sleep_s = min(sleep_s, max(0.0, remaining))
                kind = (
                    "rate limit"
                    if _is_rate_limit_error(exc)
                    else ("timeout" if _is_timeout_error(exc) else "connection")
                )
                _log_retry(
                    kind=kind,
                    exc=exc,
                    attempt=attempt,
                    sleep_s=sleep_s,
                    respondent_id=respondent_id,
                    item_id=item_id,
                )
                await asyncio.sleep(sleep_s)
    finally:
        if owns_client:
            try:
                closer = getattr(client, "aclose", None) or client.close
                result = closer()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                pass


def _stub_provider(provider: str) -> None:
    raise NotImplementedError(
        f"Provider {provider!r} is not implemented yet. "
        "Add credentials and a handler in llm_providers.py."
    )


def ask_provider(
    *,
    provider: str,
    prompt: str | list,
    seed: int,
    temperature: float,
    model_name: str,
    system_prompt: Optional[str] = None,
    respondent_id: str | None = None,
    item_id: str | None = None,
    max_tokens: int | None = None,
) -> CompletionResult:
    """Route a completion request to the configured provider."""
    provider = (provider or PROVIDER_OPENAI).lower()
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(f"Unknown provider: {provider!r}")

    if provider == PROVIDER_OPENAI:
        return _ask_openai(
            prompt=prompt,
            seed=seed,
            temperature=temperature,
            model_name=model_name,
            system_prompt=system_prompt,
            respondent_id=respondent_id,
            item_id=item_id,
            max_tokens=max_tokens,
        )
    if provider == PROVIDER_REPLICATE:
        _stub_provider(PROVIDER_REPLICATE)
    if provider == PROVIDER_GOOGLE:
        _stub_provider(PROVIDER_GOOGLE)
    if provider == PROVIDER_COHERE:
        _stub_provider(PROVIDER_COHERE)
    raise ValueError(f"Unhandled provider: {provider!r}")


async def ask_provider_async(
    *,
    provider: str,
    prompt: str | list,
    seed: int,
    temperature: float,
    model_name: str,
    system_prompt: Optional[str] = None,
    respondent_id: str | None = None,
    item_id: str | None = None,
    max_tokens: int | None = None,
) -> CompletionResult:
    """Async provider router."""
    provider = (provider or PROVIDER_OPENAI).lower()
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(f"Unknown provider: {provider!r}")

    if provider == PROVIDER_OPENAI:
        return await _ask_openai_async(
            prompt=prompt,
            seed=seed,
            temperature=temperature,
            model_name=model_name,
            system_prompt=system_prompt,
            respondent_id=respondent_id,
            item_id=item_id,
            max_tokens=max_tokens,
        )
    if provider == PROVIDER_REPLICATE:
        _stub_provider(PROVIDER_REPLICATE)
    if provider == PROVIDER_GOOGLE:
        _stub_provider(PROVIDER_GOOGLE)
    if provider == PROVIDER_COHERE:
        _stub_provider(PROVIDER_COHERE)
    raise ValueError(f"Unhandled provider: {provider!r}")
