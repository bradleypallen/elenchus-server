"""Tests for `llm_client.py` — error classification and retry policy.

The classifier is exhaustive over the documented exception hierarchies
in `anthropic` and `openai`. Each test constructs a stub SDK client
whose `.messages.create` (Anthropic) or `.chat.completions.create`
(OpenAI) raises a specific exception type or returns a fake response,
then checks that the LLMClient produces the expected ChatResult and
respects the retry policy.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from elenchus.llm_client import (
    RETRYABLE,
    ChatCategory,
    ChatResult,
    LLMClient,
    _classify_bad_request_message,
    _classify_status_code,
    classify_exception,
)

# ─── Helpers ──────────────────────────────────────────────────────────


class _FakeUsage:
    def __init__(self, prompt: int, completion: int, anthropic: bool = True):
        if anthropic:
            self.input_tokens = prompt
            self.output_tokens = completion
        else:
            self.prompt_tokens = prompt
            self.completion_tokens = completion


def _anthropic_response(text: str, prompt: int = 50, completion: int = 25):
    """Build a fake Anthropic response shaped like a real `Message`."""
    block = MagicMock()
    block.text = text
    resp = MagicMock()
    resp.content = [block]
    resp.usage = _FakeUsage(prompt, completion, anthropic=True)
    return resp


def _openai_response(text: str, prompt: int = 50, completion: int = 25):
    """Build a fake OpenAI response shaped like a real `ChatCompletion`."""
    choice = MagicMock()
    choice.message.content = text
    resp = MagicMock()
    resp.choices = [choice]
    resp.usage = _FakeUsage(prompt, completion, anthropic=False)
    return resp


def _anthropic_client(side_effects: list):
    """Build a stub anthropic.Anthropic whose `.messages.create` walks
    through `side_effects` in order (responses or exceptions)."""
    client = MagicMock()
    client.messages.create.side_effect = side_effects
    return client


def _openai_client(side_effects: list):
    client = MagicMock()
    client.chat.completions.create.side_effect = side_effects
    return client


# ─── Classification: exception → category ─────────────────────────────


class TestClassifyAnthropicExceptions:
    """Each anthropic.* error class maps to the expected category."""

    def test_rate_limit(self):
        import anthropic

        # RateLimitError requires response/body in its constructor in 0.40+.
        # Construct without args by skipping __init__ via __new__.
        exc = anthropic.RateLimitError.__new__(anthropic.RateLimitError)
        Exception.__init__(exc, "429 too many requests")
        assert classify_exception(exc) == ChatCategory.RATE_LIMIT

    def test_authentication(self):
        import anthropic

        exc = anthropic.AuthenticationError.__new__(anthropic.AuthenticationError)
        Exception.__init__(exc, "invalid api key")
        assert classify_exception(exc) == ChatCategory.AUTH_FAILURE

    def test_permission_denied(self):
        import anthropic

        exc = anthropic.PermissionDeniedError.__new__(anthropic.PermissionDeniedError)
        Exception.__init__(exc, "forbidden")
        assert classify_exception(exc) == ChatCategory.AUTH_FAILURE

    def test_timeout(self):
        import anthropic

        exc = anthropic.APITimeoutError.__new__(anthropic.APITimeoutError)
        Exception.__init__(exc, "request timed out")
        assert classify_exception(exc) == ChatCategory.TIMEOUT

    def test_connection(self):
        import anthropic

        exc = anthropic.APIConnectionError.__new__(anthropic.APIConnectionError)
        Exception.__init__(exc, "could not connect")
        assert classify_exception(exc) == ChatCategory.NETWORK_ERROR

    def test_internal_server_error(self):
        import anthropic

        exc = anthropic.InternalServerError.__new__(anthropic.InternalServerError)
        Exception.__init__(exc, "500 internal server error")
        assert classify_exception(exc) == ChatCategory.PROVIDER_ERROR

    def test_bad_request_content_policy(self):
        import anthropic

        exc = anthropic.BadRequestError.__new__(anthropic.BadRequestError)
        Exception.__init__(exc, "content_policy violation: blocked")
        assert classify_exception(exc) == ChatCategory.CONTENT_POLICY

    def test_bad_request_token_overflow(self):
        import anthropic

        exc = anthropic.BadRequestError.__new__(anthropic.BadRequestError)
        Exception.__init__(exc, "maximum context length is 200000 tokens")
        assert classify_exception(exc) == ChatCategory.TOKEN_OVERFLOW

    def test_bad_request_generic(self):
        import anthropic

        exc = anthropic.BadRequestError.__new__(anthropic.BadRequestError)
        Exception.__init__(exc, "messages: at least one message is required")
        assert classify_exception(exc) == ChatCategory.BAD_REQUEST

    def test_not_found(self):
        import anthropic

        exc = anthropic.NotFoundError.__new__(anthropic.NotFoundError)
        Exception.__init__(exc, "model not found")
        assert classify_exception(exc) == ChatCategory.BAD_REQUEST


class TestClassifyOpenAIExceptions:
    """Same coverage on the OpenAI hierarchy."""

    def test_rate_limit(self):
        import openai

        exc = openai.RateLimitError.__new__(openai.RateLimitError)
        Exception.__init__(exc, "429")
        assert classify_exception(exc) == ChatCategory.RATE_LIMIT

    def test_authentication(self):
        import openai

        exc = openai.AuthenticationError.__new__(openai.AuthenticationError)
        Exception.__init__(exc, "401")
        assert classify_exception(exc) == ChatCategory.AUTH_FAILURE

    def test_timeout(self):
        import openai

        exc = openai.APITimeoutError.__new__(openai.APITimeoutError)
        Exception.__init__(exc, "timeout")
        assert classify_exception(exc) == ChatCategory.TIMEOUT

    def test_connection(self):
        import openai

        exc = openai.APIConnectionError.__new__(openai.APIConnectionError)
        Exception.__init__(exc, "connection refused")
        assert classify_exception(exc) == ChatCategory.NETWORK_ERROR

    def test_internal_server_error(self):
        import openai

        exc = openai.InternalServerError.__new__(openai.InternalServerError)
        Exception.__init__(exc, "500")
        assert classify_exception(exc) == ChatCategory.PROVIDER_ERROR


class TestClassifyStandalone:
    """Standalone helpers."""

    def test_unknown_exception(self):
        assert classify_exception(RuntimeError("???")) == ChatCategory.UNKNOWN

    def test_builtin_timeout_error(self):
        assert classify_exception(TimeoutError("os timeout")) == ChatCategory.TIMEOUT

    def test_status_429_to_rate_limit(self):
        assert _classify_status_code(429, "") == ChatCategory.RATE_LIMIT

    def test_status_503_to_provider_error(self):
        assert _classify_status_code(503, "") == ChatCategory.PROVIDER_ERROR

    def test_status_401_to_auth(self):
        assert _classify_status_code(401, "") == ChatCategory.AUTH_FAILURE

    def test_status_400_with_token_message(self):
        assert _classify_status_code(400, "context_length exceeded") == ChatCategory.TOKEN_OVERFLOW

    def test_status_404_to_bad_request(self):
        assert _classify_status_code(404, "") == ChatCategory.BAD_REQUEST

    def test_bad_request_message_safety_keyword(self):
        assert (
            _classify_bad_request_message("refused due to safety policy")
            == ChatCategory.CONTENT_POLICY
        )

    def test_bad_request_message_token_keyword(self):
        assert (
            _classify_bad_request_message("too many tokens in prompt")
            == ChatCategory.TOKEN_OVERFLOW
        )


class TestRetryablePartition:
    """The retryable set is exactly: rate_limit, provider_error,
    timeout, network_error. Everything else is non-retryable so a
    deterministic failure surfaces immediately."""

    def test_retryable_set(self):
        assert {
            ChatCategory.RATE_LIMIT,
            ChatCategory.PROVIDER_ERROR,
            ChatCategory.TIMEOUT,
            ChatCategory.NETWORK_ERROR,
        } == RETRYABLE

    @pytest.mark.parametrize(
        "category",
        [
            ChatCategory.SUCCESS,
            ChatCategory.AUTH_FAILURE,
            ChatCategory.CONTENT_POLICY,
            ChatCategory.TOKEN_OVERFLOW,
            ChatCategory.BAD_REQUEST,
            ChatCategory.UNKNOWN,
        ],
    )
    def test_non_retryable_categories(self, category):
        assert category not in RETRYABLE


# ─── Sync chat: end-to-end ────────────────────────────────────────────


class TestSyncChat:
    def test_success_returns_text_and_tokens(self):
        client = _anthropic_client([_anthropic_response("hello", prompt=42, completion=7)])
        llm = LLMClient(protocol="anthropic", model="claude-opus-4-6", sync_client=client)
        result = llm.chat([{"role": "user", "content": "hi"}])
        assert result.ok
        assert result.text == "hello"
        assert result.prompt_tokens == 42
        assert result.completion_tokens == 7
        assert result.attempts == 1
        assert result.model == "claude-opus-4-6"
        assert result.category == ChatCategory.SUCCESS

    def test_openai_protocol_extracts_message_content(self):
        client = _openai_client([_openai_response("hi there", prompt=10, completion=3)])
        llm = LLMClient(protocol="openai", model="gpt-4", sync_client=client)
        result = llm.chat([{"role": "user", "content": "hi"}])
        assert result.text == "hi there"
        assert result.prompt_tokens == 10
        assert result.completion_tokens == 3

    def test_auth_failure_does_not_retry(self):
        import anthropic

        auth_err = anthropic.AuthenticationError.__new__(anthropic.AuthenticationError)
        Exception.__init__(auth_err, "invalid api key")
        client = _anthropic_client([auth_err, auth_err, auth_err])
        llm = LLMClient(
            protocol="anthropic",
            model="claude-opus-4-6",
            sync_client=client,
            max_attempts=3,
            base_backoff_s=0,
        )
        result = llm.chat([{"role": "user", "content": "hi"}])
        assert result.category == ChatCategory.AUTH_FAILURE
        assert result.attempts == 1, "auth failure should not retry"
        # SDK was called exactly once.
        assert client.messages.create.call_count == 1

    def test_rate_limit_retries_then_succeeds(self):
        import anthropic

        rl = anthropic.RateLimitError.__new__(anthropic.RateLimitError)
        Exception.__init__(rl, "429")
        client = _anthropic_client([rl, rl, _anthropic_response("recovered")])
        llm = LLMClient(
            protocol="anthropic",
            model="claude-opus-4-6",
            sync_client=client,
            max_attempts=3,
            base_backoff_s=0,
        )
        result = llm.chat([{"role": "user", "content": "hi"}])
        assert result.ok
        assert result.text == "recovered"
        assert result.attempts == 3

    def test_rate_limit_exhausts_attempts(self):
        import anthropic

        rl = anthropic.RateLimitError.__new__(anthropic.RateLimitError)
        Exception.__init__(rl, "429")
        client = _anthropic_client([rl, rl, rl])
        llm = LLMClient(
            protocol="anthropic",
            model="claude-opus-4-6",
            sync_client=client,
            max_attempts=3,
            base_backoff_s=0,
        )
        result = llm.chat([{"role": "user", "content": "hi"}])
        assert result.category == ChatCategory.RATE_LIMIT
        assert result.attempts == 3
        assert result.error_message == "429"
        assert result.exception_type == "RateLimitError"

    def test_missing_sync_client_returns_error_result(self):
        llm = LLMClient(protocol="anthropic", model="x", sync_client=None)
        result = llm.chat([])
        assert not result.ok
        assert result.category == ChatCategory.BAD_REQUEST
        assert "no sync_client" in result.error_message


# ─── Async chat ───────────────────────────────────────────────────────


def _async_anthropic_response(text: str, prompt: int = 5, completion: int = 3):
    resp = _anthropic_response(text, prompt=prompt, completion=completion)

    async def call(**kwargs):
        return resp

    return call


def _async_raises(exc):
    async def call(**kwargs):
        raise exc

    return call


class _AsyncAnthropic:
    """Stub that supports `.messages.create(...)` returning an awaitable."""

    def __init__(self, side_effects: list):
        self._side_effects = list(side_effects)
        self.messages = self  # so `.messages.create` works
        self.call_count = 0

    async def create(self, **kwargs):
        self.call_count += 1
        eff = self._side_effects.pop(0)
        if isinstance(eff, BaseException):
            raise eff
        return eff


class TestAsyncChat:
    """Async path mirrors sync. We use `asyncio.run` here rather than
    a pytest-asyncio plugin so the suite has zero extra test deps."""

    def test_success(self):
        import asyncio

        client = _AsyncAnthropic([_anthropic_response("async hello", prompt=8, completion=4)])
        llm = LLMClient(
            protocol="anthropic",
            model="claude-opus-4-6",
            async_client=client,
            base_backoff_s=0,
        )
        result = asyncio.run(llm.achat([{"role": "user", "content": "hi"}]))
        assert result.ok
        assert result.text == "async hello"
        assert result.prompt_tokens == 8

    def test_rate_limit_retries(self):
        import asyncio

        import anthropic

        rl = anthropic.RateLimitError.__new__(anthropic.RateLimitError)
        Exception.__init__(rl, "429")
        client = _AsyncAnthropic([rl, _anthropic_response("recovered")])
        llm = LLMClient(
            protocol="anthropic",
            model="claude-opus-4-6",
            async_client=client,
            max_attempts=3,
            base_backoff_s=0,
        )
        result = asyncio.run(llm.achat([{"role": "user", "content": "hi"}]))
        assert result.ok
        assert result.text == "recovered"
        assert result.attempts == 2

    def test_auth_failure_not_retried(self):
        import asyncio

        import anthropic

        auth_err = anthropic.AuthenticationError.__new__(anthropic.AuthenticationError)
        Exception.__init__(auth_err, "401")
        client = _AsyncAnthropic([auth_err])
        llm = LLMClient(
            protocol="anthropic",
            model="claude-opus-4-6",
            async_client=client,
            max_attempts=3,
            base_backoff_s=0,
        )
        result = asyncio.run(llm.achat([{"role": "user", "content": "hi"}]))
        assert result.category == ChatCategory.AUTH_FAILURE
        assert result.attempts == 1

    def test_missing_async_client(self):
        import asyncio

        llm = LLMClient(protocol="anthropic", model="x", async_client=None)
        result = asyncio.run(llm.achat([]))
        assert result.category == ChatCategory.BAD_REQUEST


# ─── Backoff timing ───────────────────────────────────────────────────


class TestBackoff:
    def test_backoff_progression(self):
        llm = LLMClient(
            protocol="anthropic",
            model="x",
            sync_client=MagicMock(),
            base_backoff_s=1.0,
            max_backoff_s=8.0,
        )
        assert llm._backoff_for(1) == 1.0
        assert llm._backoff_for(2) == 2.0
        assert llm._backoff_for(3) == 4.0
        assert llm._backoff_for(4) == 8.0
        # Capped.
        assert llm._backoff_for(10) == 8.0

    def test_zero_base_backoff_is_zero(self):
        llm = LLMClient(
            protocol="anthropic", model="x", sync_client=MagicMock(), base_backoff_s=0.0
        )
        assert llm._backoff_for(1) == 0.0
        assert llm._backoff_for(5) == 0.0


# ─── ChatResult shape ─────────────────────────────────────────────────


class TestChatResult:
    def test_ok_property(self):
        assert ChatResult(category=ChatCategory.SUCCESS).ok is True
        assert ChatResult(category=ChatCategory.RATE_LIMIT).ok is False

    def test_total_tokens(self):
        r = ChatResult(category=ChatCategory.SUCCESS, prompt_tokens=42, completion_tokens=8)
        assert r.total_tokens == 50

    def test_total_tokens_defaults_to_zero(self):
        r = ChatResult(category=ChatCategory.AUTH_FAILURE)
        assert r.total_tokens == 0


class TestConfiguration:
    def test_negative_max_attempts_raises(self):
        with pytest.raises(ValueError):
            LLMClient(protocol="anthropic", model="x", max_attempts=0)

    def test_negative_base_backoff_raises(self):
        with pytest.raises(ValueError):
            LLMClient(protocol="anthropic", model="x", base_backoff_s=-1.0)
