"""
llm_client.py — single chokepoint for every LLM API call.

The previous architecture had Opponent._chat / _async_chat dispatch
directly to the Anthropic or OpenAI SDK. That worked for the
single-user path but provided no single place to:

  * classify failures into actionable categories
  * retry the retryable ones with exponential backoff
  * record token usage and latency for cost tracking
  * emit structured events the alerting subsystem can subscribe to

This module is the chokepoint that does all of that. Opponent still
owns the prompt + state-to-messages translation + speech-act dispatch;
the network-IO portion is delegated here.

Two entry points: `LLMClient.chat()` (sync, for CLI) and
`LLMClient.achat()` (async, for the FastAPI route). Both return a
`ChatResult` carrying the response text plus all the metadata
downstream subsystems need (category, attempts, latency, token
counts, captured exception). Callers decide what to do with a
non-success result; the client itself only handles the retry policy.

The classification is exhaustive over the documented exception
hierarchies in `anthropic` (>= 0.40) and `openai` (>= 1.0). Unknown
exceptions fall into `UNKNOWN`. That category is non-retryable so a
genuinely novel failure surfaces immediately instead of silently
burning retries.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


# ── Result shape ─────────────────────────────────────────────────────


class ChatCategory(StrEnum):
    """Classified outcome of one LLM call (final attempt after any retries).

    `SUCCESS` is the only category that carries a usable `text`. All
    others mean the call failed; the caller chooses whether to surface
    that as an error to the user or to fall back to some other path.
    """

    SUCCESS = "success"
    RATE_LIMIT = "rate_limit"  # 429 — provider says back off
    PROVIDER_ERROR = "provider_error"  # 5xx — provider is having a bad day
    AUTH_FAILURE = "auth_failure"  # 401/403 — key invalid / revoked / missing perms
    TIMEOUT = "timeout"  # request hit our or the SDK's timeout
    CONTENT_POLICY = "content_policy"  # provider refused on safety grounds
    TOKEN_OVERFLOW = "token_overflow"  # context window exceeded or max_tokens too high
    NETWORK_ERROR = "network_error"  # DNS / connection / TLS failure before HTTP
    BAD_REQUEST = "bad_request"  # 4xx that isn't auth/policy/tokens — programming error
    UNKNOWN = "unknown"  # caught Exception we couldn't classify


# Categories where retrying again with the same payload may succeed.
# Auth / content-policy / token-overflow are deterministic failures of
# the request itself — retrying changes nothing.
RETRYABLE = frozenset(
    {
        ChatCategory.RATE_LIMIT,
        ChatCategory.PROVIDER_ERROR,
        ChatCategory.TIMEOUT,
        ChatCategory.NETWORK_ERROR,
    }
)


@dataclass
class ChatResult:
    """Outcome of a single (possibly-retried) LLM chat call."""

    category: ChatCategory
    text: str = ""
    attempts: int = 0
    latency_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""
    error_message: str | None = None
    exception_type: str | None = None
    # Free-form metadata that subscribers may attach (e.g. trace id).
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.category == ChatCategory.SUCCESS

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


# ── Exception classification ─────────────────────────────────────────


def classify_exception(exc: BaseException) -> ChatCategory:
    """Map a provider-SDK exception to a `ChatCategory`.

    Handles both `anthropic` and `openai` exception hierarchies. The
    classification is structural (isinstance on the exception class)
    plus message-substring matching for the cases where the SDK
    overloads a single exception class to mean several things —
    notably `BadRequestError`, which covers token-overflow,
    content-policy, and pure-malformed-payload failures.
    """
    # Anthropic
    try:
        import anthropic

        if isinstance(exc, anthropic.AuthenticationError):
            return ChatCategory.AUTH_FAILURE
        if isinstance(exc, anthropic.PermissionDeniedError):
            return ChatCategory.AUTH_FAILURE
        if isinstance(exc, anthropic.RateLimitError):
            return ChatCategory.RATE_LIMIT
        if isinstance(exc, anthropic.APITimeoutError):
            return ChatCategory.TIMEOUT
        if isinstance(exc, anthropic.APIConnectionError):
            return ChatCategory.NETWORK_ERROR
        if isinstance(exc, anthropic.InternalServerError):
            return ChatCategory.PROVIDER_ERROR
        if isinstance(exc, anthropic.BadRequestError):
            return _classify_bad_request_message(str(exc))
        if isinstance(exc, anthropic.NotFoundError):
            return ChatCategory.BAD_REQUEST  # model name typo, missing resource
        if isinstance(exc, anthropic.APIStatusError):
            return _classify_status_code(getattr(exc, "status_code", 0), str(exc))
        if isinstance(exc, anthropic.APIError):
            # APIError is the root; fall through to UNKNOWN if no subclass matched.
            pass
    except ImportError:
        pass

    # OpenAI (analogous hierarchy)
    try:
        import openai

        if isinstance(exc, openai.AuthenticationError):
            return ChatCategory.AUTH_FAILURE
        if isinstance(exc, openai.PermissionDeniedError):
            return ChatCategory.AUTH_FAILURE
        if isinstance(exc, openai.RateLimitError):
            return ChatCategory.RATE_LIMIT
        if isinstance(exc, openai.APITimeoutError):
            return ChatCategory.TIMEOUT
        if isinstance(exc, openai.APIConnectionError):
            return ChatCategory.NETWORK_ERROR
        if isinstance(exc, openai.InternalServerError):
            return ChatCategory.PROVIDER_ERROR
        if isinstance(exc, openai.BadRequestError):
            return _classify_bad_request_message(str(exc))
        if isinstance(exc, openai.NotFoundError):
            return ChatCategory.BAD_REQUEST
        if isinstance(exc, openai.APIStatusError):
            return _classify_status_code(getattr(exc, "status_code", 0), str(exc))
    except ImportError:
        pass

    # Fall back to asyncio's own timeout if neither SDK matched.
    if isinstance(exc, asyncio.TimeoutError | TimeoutError):
        return ChatCategory.TIMEOUT

    return ChatCategory.UNKNOWN


def _classify_bad_request_message(msg: str) -> ChatCategory:
    """`BadRequestError` is overloaded by both SDKs — pick a finer
    category by looking at the message text. Falls back to BAD_REQUEST
    for genuine 4xx-the-request-is-malformed cases."""
    m = msg.lower()
    if "content_policy" in m or "content policy" in m or "safety" in m or "refused" in m:
        return ChatCategory.CONTENT_POLICY
    if (
        "context length" in m
        or "context_length" in m
        or "maximum context" in m
        or "max_tokens" in m
        or "too many tokens" in m
        or "token limit" in m
    ):
        return ChatCategory.TOKEN_OVERFLOW
    return ChatCategory.BAD_REQUEST


def _classify_status_code(status: int, msg: str) -> ChatCategory:
    """Map a raw HTTP status to a category when we only have a generic
    `APIStatusError`. Used as the fallback when the SDK hasn't raised
    one of the more specific subclasses."""
    if status == 429:
        return ChatCategory.RATE_LIMIT
    if status in (401, 403):
        return ChatCategory.AUTH_FAILURE
    if status == 408:
        return ChatCategory.TIMEOUT
    if 500 <= status < 600:
        return ChatCategory.PROVIDER_ERROR
    if status == 400:
        return _classify_bad_request_message(msg)
    if 400 <= status < 500:
        return ChatCategory.BAD_REQUEST
    return ChatCategory.UNKNOWN


# ── The client ───────────────────────────────────────────────────────


@dataclass
class _ProviderAdapter:
    """Per-protocol wiring: how to format the request and where to
    pull the response text + usage out of."""

    protocol: str

    def build_kwargs(
        self,
        *,
        model: str,
        messages: list[dict],
        system: str | None,
        max_tokens: int,
    ) -> dict:
        if self.protocol == "openai":
            oai_messages = []
            if system:
                oai_messages.append({"role": "system", "content": system})
            oai_messages.extend(messages)
            return {
                "model": model,
                "max_tokens": max_tokens,
                "messages": oai_messages,
            }
        # Anthropic
        kwargs = {"model": model, "max_tokens": max_tokens, "messages": messages}
        if system:
            kwargs["system"] = system
        return kwargs

    def extract_text(self, response) -> str:
        if self.protocol == "openai":
            return response.choices[0].message.content or ""
        return response.content[0].text

    def extract_usage(self, response) -> tuple[int, int]:
        """Return (prompt_tokens, completion_tokens). Best-effort: both
        SDKs expose this as `response.usage` but the field names differ
        and either may be absent on streaming / unusual responses."""
        usage = getattr(response, "usage", None)
        if usage is None:
            return (0, 0)
        if self.protocol == "openai":
            return (
                int(getattr(usage, "prompt_tokens", 0) or 0),
                int(getattr(usage, "completion_tokens", 0) or 0),
            )
        # Anthropic — input_tokens / output_tokens
        return (
            int(getattr(usage, "input_tokens", 0) or 0),
            int(getattr(usage, "output_tokens", 0) or 0),
        )


class LLMClient:
    """Wraps a provider SDK with classification, retry, and metrics.

    `sync_client` / `async_client` are the already-built SDK clients
    (anthropic.Anthropic / anthropic.AsyncAnthropic, or the OpenAI
    equivalents). The LLMClient itself is protocol-aware via the
    `protocol` argument, which switches the request-shape adapter.

    Backoff: `base_backoff_s * 2**attempt`, so 1s, 2s, 4s for the
    default `base_backoff_s=1.0` and `max_attempts=3`. Total wall-clock
    wait ≤ 7s before giving up; chosen to be short enough that a
    user-facing route can still return within a normal HTTP timeout.
    """

    def __init__(
        self,
        *,
        protocol: str,
        model: str,
        sync_client=None,
        async_client=None,
        max_attempts: int = 3,
        base_backoff_s: float = 1.0,
        max_backoff_s: float = 8.0,
    ):
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if base_backoff_s < 0:
            raise ValueError("base_backoff_s must be >= 0")
        self.protocol = protocol
        self.model = model
        self.sync_client = sync_client
        self.async_client = async_client
        self.max_attempts = max_attempts
        self.base_backoff_s = base_backoff_s
        self.max_backoff_s = max_backoff_s
        self._adapter = _ProviderAdapter(protocol=protocol)

    # ── Sync entry point ─────────────────────────────────────────────

    def chat(
        self,
        messages: list[dict],
        *,
        system: str | None = None,
        max_tokens: int = 2000,
        model: str | None = None,
    ) -> ChatResult:
        """Synchronous chat call. Retries internally on retryable
        categories. Always returns a `ChatResult` — never raises."""
        if self.sync_client is None:
            return ChatResult(
                category=ChatCategory.BAD_REQUEST,
                model=model or self.model,
                error_message="LLMClient has no sync_client configured",
                exception_type="ConfigurationError",
            )

        use_model = model or self.model
        kwargs = self._adapter.build_kwargs(
            model=use_model, messages=messages, system=system, max_tokens=max_tokens
        )

        started = time.monotonic()
        last_error: BaseException | None = None
        category = ChatCategory.UNKNOWN

        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self._sync_send(kwargs)
                text = self._adapter.extract_text(response)
                p_tok, c_tok = self._adapter.extract_usage(response)
                elapsed_ms = int((time.monotonic() - started) * 1000)
                logger.info(
                    "LLM call ok: model=%s attempts=%d latency_ms=%d "
                    "prompt_tokens=%d completion_tokens=%d",
                    use_model,
                    attempt,
                    elapsed_ms,
                    p_tok,
                    c_tok,
                )
                return ChatResult(
                    category=ChatCategory.SUCCESS,
                    text=text,
                    attempts=attempt,
                    latency_ms=elapsed_ms,
                    prompt_tokens=p_tok,
                    completion_tokens=c_tok,
                    model=use_model,
                )
            except Exception as exc:  # noqa: BLE001 — we classify it
                last_error = exc
                category = classify_exception(exc)
                if category in RETRYABLE and attempt < self.max_attempts:
                    sleep = self._backoff_for(attempt)
                    logger.warning(
                        "LLM call failed (%s); attempt=%d/%d, retrying in %.1fs: %s",
                        category.value,
                        attempt,
                        self.max_attempts,
                        sleep,
                        exc,
                    )
                    time.sleep(sleep)
                    continue
                # Non-retryable, or out of retries.
                break

        elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.error(
            "LLM call failed: model=%s category=%s attempts=%d latency_ms=%d error=%s",
            use_model,
            category.value,
            self.max_attempts if category in RETRYABLE else 1,
            elapsed_ms,
            last_error,
        )
        return ChatResult(
            category=category,
            text="",
            attempts=self.max_attempts if category in RETRYABLE else 1,
            latency_ms=elapsed_ms,
            model=use_model,
            error_message=str(last_error) if last_error else None,
            exception_type=type(last_error).__name__ if last_error else None,
        )

    # ── Async entry point ────────────────────────────────────────────

    async def achat(
        self,
        messages: list[dict],
        *,
        system: str | None = None,
        max_tokens: int = 2000,
        model: str | None = None,
    ) -> ChatResult:
        """Async chat call — same contract as `chat()`, but awaitable
        and uses `asyncio.sleep` for backoff so the event loop stays
        responsive during retry waits."""
        if self.async_client is None:
            return ChatResult(
                category=ChatCategory.BAD_REQUEST,
                model=model or self.model,
                error_message="LLMClient has no async_client configured",
                exception_type="ConfigurationError",
            )

        use_model = model or self.model
        kwargs = self._adapter.build_kwargs(
            model=use_model, messages=messages, system=system, max_tokens=max_tokens
        )

        started = time.monotonic()
        last_error: BaseException | None = None
        category = ChatCategory.UNKNOWN

        for attempt in range(1, self.max_attempts + 1):
            try:
                response = await self._async_send(kwargs)
                text = self._adapter.extract_text(response)
                p_tok, c_tok = self._adapter.extract_usage(response)
                elapsed_ms = int((time.monotonic() - started) * 1000)
                logger.info(
                    "LLM call ok: model=%s attempts=%d latency_ms=%d "
                    "prompt_tokens=%d completion_tokens=%d",
                    use_model,
                    attempt,
                    elapsed_ms,
                    p_tok,
                    c_tok,
                )
                return ChatResult(
                    category=ChatCategory.SUCCESS,
                    text=text,
                    attempts=attempt,
                    latency_ms=elapsed_ms,
                    prompt_tokens=p_tok,
                    completion_tokens=c_tok,
                    model=use_model,
                )
            except Exception as exc:  # noqa: BLE001 — we classify it
                last_error = exc
                category = classify_exception(exc)
                if category in RETRYABLE and attempt < self.max_attempts:
                    sleep = self._backoff_for(attempt)
                    logger.warning(
                        "LLM call failed (%s); attempt=%d/%d, retrying in %.1fs: %s",
                        category.value,
                        attempt,
                        self.max_attempts,
                        sleep,
                        exc,
                    )
                    await asyncio.sleep(sleep)
                    continue
                break

        elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.error(
            "LLM call failed: model=%s category=%s attempts=%d latency_ms=%d error=%s",
            use_model,
            category.value,
            self.max_attempts if category in RETRYABLE else 1,
            elapsed_ms,
            last_error,
        )
        return ChatResult(
            category=category,
            text="",
            attempts=self.max_attempts if category in RETRYABLE else 1,
            latency_ms=elapsed_ms,
            model=use_model,
            error_message=str(last_error) if last_error else None,
            exception_type=type(last_error).__name__ if last_error else None,
        )

    # ── Internals ────────────────────────────────────────────────────

    def _sync_send(self, kwargs: dict):
        if self.protocol == "openai":
            return self.sync_client.chat.completions.create(**kwargs)
        return self.sync_client.messages.create(**kwargs)

    async def _async_send(self, kwargs: dict):
        if self.protocol == "openai":
            return await self.async_client.chat.completions.create(**kwargs)
        return await self.async_client.messages.create(**kwargs)

    def _backoff_for(self, attempt: int) -> float:
        """Exponential backoff capped at `max_backoff_s`. attempt is
        1-indexed so the first retry waits `base_backoff_s`, the
        second `2 * base_backoff_s`, etc."""
        return min(self.base_backoff_s * (2 ** (attempt - 1)), self.max_backoff_s)
