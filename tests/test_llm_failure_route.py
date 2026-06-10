"""Tests for Phase C/5 — graceful LLM failure on the message route.

The route now catches `LLMCallError` separately from generic
exceptions, mapping the underlying `ChatCategory` to an appropriate
HTTP status and a user-facing string the frontend renders verbatim.
"""

from __future__ import annotations

import contextlib
import os
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from elenchus import auth
from elenchus.db import get_registry
from elenchus.db import platform as pdb
from elenchus.llm_client import ChatCategory, ChatResult
from elenchus.opponent import LLMCallError
from elenchus.server import (
    _http_status_for_chat_category,
    _user_message_for_chat_category,
    app,
)

client = TestClient(app)
_test_data_dir = os.environ["ELENCHUS_DATA"]


@pytest.fixture(autouse=True)
def _clean():
    reg = get_registry()
    reg.migrate_platform()
    con = reg.platform_con()
    with reg.platform_lock:
        for table in (
            "usage",
            "auth_sessions",
            "magic_links",
            "invites",
            "sessions",
            "bases",
            "actors",
        ):
            con.execute(f"DELETE FROM {table}")
    for _name, handle in list(reg._handles.items()):
        with contextlib.suppress(Exception):
            handle.state.base.con.close()
    reg._handles.clear()
    client.cookies.clear()
    _wipe()
    yield
    client.cookies.clear()
    _wipe()


def _wipe():
    for root, _dirs, files in os.walk(_test_data_dir):
        for f in files:
            if f.endswith(".duckdb") and f != "platform.duckdb":
                with contextlib.suppress(OSError):
                    os.remove(os.path.join(root, f))


def _create_user_with_base(name: str = "test") -> int:
    con = get_registry().platform_con()
    user_id = pdb.create_actor(
        con,
        kind="user",
        email="u@example.com",
        display_name="U",
        password_hash=auth.hash_password("pw"),
    )
    client.cookies.set(auth.SESSION_COOKIE, auth.create_session(user_id))
    r = client.post("/api/dialectics", json={"name": name})
    assert r.status_code == 200, r.text
    return user_id


# ─── Mapping tables ──────────────────────────────────────────────────


class TestMappingTables:
    """The two pure mapping helpers are exhaustive over ChatCategory."""

    def test_every_category_has_a_status(self):
        for cat in ChatCategory:
            status = _http_status_for_chat_category(cat)
            assert 400 <= status < 600

    def test_every_category_has_a_user_message(self):
        for cat in ChatCategory:
            msg = _user_message_for_chat_category(cat)
            assert isinstance(msg, str)
            assert len(msg) > 0
            # No raw category names leak to the user.
            assert cat.value not in msg

    def test_auth_failure_is_503(self):
        assert _http_status_for_chat_category(ChatCategory.AUTH_FAILURE) == 503

    def test_token_overflow_is_413(self):
        assert _http_status_for_chat_category(ChatCategory.TOKEN_OVERFLOW) == 413

    def test_content_policy_is_422(self):
        assert _http_status_for_chat_category(ChatCategory.CONTENT_POLICY) == 422

    def test_timeout_is_504(self):
        assert _http_status_for_chat_category(ChatCategory.TIMEOUT) == 504

    def test_rate_limit_is_503(self):
        assert _http_status_for_chat_category(ChatCategory.RATE_LIMIT) == 503

    def test_user_message_for_rate_limit_suggests_retry(self):
        msg = _user_message_for_chat_category(ChatCategory.RATE_LIMIT).lower()
        assert "try" in msg or "again" in msg

    def test_user_message_for_token_overflow_suggests_fresh_dialectic(self):
        msg = _user_message_for_chat_category(ChatCategory.TOKEN_OVERFLOW).lower()
        assert "fresh" in msg or "new" in msg or "long" in msg


# ─── Message route — end-to-end ──────────────────────────────────────


class TestMessageRouteFailureSurface:
    """When `async_respond` raises LLMCallError, the route returns a
    structured `detail` body with `category`, `attempts`, and
    `user_message` — what the frontend renders verbatim."""

    def _make_failure(self, category: ChatCategory, *, attempts: int = 1) -> ChatResult:
        return ChatResult(
            category=category,
            attempts=attempts,
            latency_ms=100,
            model="claude-opus-4-6",
            error_message=f"simulated {category.value}",
            exception_type="SimulatedError",
        )

    def test_rate_limit_returns_503_with_user_message(self):
        _create_user_with_base("rl-test")
        # async_respond raises LLMCallError(result) for any non-success.
        # Patch it to skip the LLMClient entirely.
        fail_result = self._make_failure(ChatCategory.RATE_LIMIT, attempts=3)
        with patch(
            "elenchus.server.opponent.async_respond",
            new=AsyncMock(side_effect=LLMCallError(fail_result)),
        ):
            r = client.post("/api/dialectics/rl-test/message", json={"message": "hi"})

        assert r.status_code == 503
        detail = r.json()["detail"]
        assert detail["category"] == "rate_limit"
        assert detail["attempts"] == 3
        assert "user_message" in detail
        # Should not leak raw exception text.
        assert "simulated" not in detail["user_message"].lower()
        assert "Traceback" not in detail["user_message"]

    def test_auth_failure_returns_503(self):
        _create_user_with_base("auth-test")
        fail = self._make_failure(ChatCategory.AUTH_FAILURE)
        with patch(
            "elenchus.server.opponent.async_respond",
            new=AsyncMock(side_effect=LLMCallError(fail)),
        ):
            r = client.post("/api/dialectics/auth-test/message", json={"message": "x"})
        assert r.status_code == 503
        assert r.json()["detail"]["category"] == "auth_failure"

    def test_timeout_returns_504(self):
        _create_user_with_base("timeout-test")
        fail = self._make_failure(ChatCategory.TIMEOUT)
        with patch(
            "elenchus.server.opponent.async_respond",
            new=AsyncMock(side_effect=LLMCallError(fail)),
        ):
            r = client.post("/api/dialectics/timeout-test/message", json={"message": "x"})
        assert r.status_code == 504
        assert r.json()["detail"]["category"] == "timeout"

    def test_content_policy_returns_422(self):
        _create_user_with_base("policy-test")
        fail = self._make_failure(ChatCategory.CONTENT_POLICY)
        with patch(
            "elenchus.server.opponent.async_respond",
            new=AsyncMock(side_effect=LLMCallError(fail)),
        ):
            r = client.post("/api/dialectics/policy-test/message", json={"message": "x"})
        assert r.status_code == 422
        assert r.json()["detail"]["category"] == "content_policy"

    def test_token_overflow_returns_413(self):
        _create_user_with_base("overflow-test")
        fail = self._make_failure(ChatCategory.TOKEN_OVERFLOW)
        with patch(
            "elenchus.server.opponent.async_respond",
            new=AsyncMock(side_effect=LLMCallError(fail)),
        ):
            r = client.post("/api/dialectics/overflow-test/message", json={"message": "x"})
        assert r.status_code == 413
        assert r.json()["detail"]["category"] == "token_overflow"

    def test_generic_exception_still_returns_500(self):
        """A non-LLM error (e.g. a bug in _record_and_apply) must keep
        the existing 500 path so we don't accidentally swallow it."""
        _create_user_with_base("generic-test")
        with patch(
            "elenchus.server.opponent.async_respond",
            new=AsyncMock(side_effect=RuntimeError("unrelated bug")),
        ):
            r = client.post("/api/dialectics/generic-test/message", json={"message": "x"})
        assert r.status_code == 500
        # Generic path uses string detail, not structured.
        detail = r.json()["detail"]
        assert isinstance(detail, str)
        assert "Opponent error" in detail
