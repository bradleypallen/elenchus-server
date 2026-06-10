"""Tests for the Phase C cost-tracking subsystem.

Three slices:
  1. `pricing.py` — model rate lookup and `compute_cost`.
  2. `db/platform.py::record_usage` + aggregations — schema and SQL.
  3. End-to-end: an LLM call (mocked) writes a usage row with the
     right actor_id / base_id / cost. `/api/admin/usage` returns the
     rollup.
"""

from __future__ import annotations

import contextlib
import os

import pytest
from fastapi.testclient import TestClient

from elenchus import auth, pricing
from elenchus.db import get_registry
from elenchus.db import platform as pdb
from elenchus.llm_client import ChatCategory, ChatResult
from elenchus.server import app

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
    pricing._reset_cache_for_tests()
    yield
    client.cookies.clear()
    pricing._reset_cache_for_tests()


def _create_admin(email="admin@example.com") -> int:
    con = get_registry().platform_con()
    actor_id = pdb.create_actor(
        con,
        kind="admin",
        email=email,
        display_name="Admin",
        password_hash=auth.hash_password("pw"),
    )
    client.cookies.set(auth.SESSION_COOKIE, auth.create_session(actor_id))
    return actor_id


# ─── Pricing ─────────────────────────────────────────────────────────


class TestPricing:
    def test_known_model_exact_match(self):
        rates = pricing.lookup_rates("claude-opus-4-6")
        assert rates is not None
        assert rates[0] > 0 and rates[1] > 0

    def test_unknown_model_returns_none(self):
        assert pricing.lookup_rates("totally-fake-model-7") is None

    def test_prefix_fallback(self):
        """Dated revisions of a model fall back to the family rate."""
        rates_family = pricing.lookup_rates("claude-opus-4-6")
        rates_dated = pricing.lookup_rates("claude-opus-4-6-20260301")
        assert rates_family == rates_dated

    def test_longest_prefix_wins(self):
        """`claude-opus-4-6-X` matches opus-4-6, not opus-4."""
        rates_4 = pricing.lookup_rates("claude-opus-4")
        rates_46 = pricing.lookup_rates("claude-opus-4-6")
        # Both real entries — they may differ in pricing or be the same.
        # The longest-prefix rule should pick 4-6 for an opus-4-6 revision.
        rates_46_rev = pricing.lookup_rates("claude-opus-4-6-zzzz")
        assert rates_46_rev == rates_46
        _ = rates_4  # silence unused

    def test_compute_cost_known(self):
        # Claude opus 4-6 is $15/M input, $75/M output by default.
        # 1000 prompt + 100 completion → 1000*15 + 100*75 = 22500 micro-dollars
        # = $0.0225
        cost = pricing.compute_cost("claude-opus-4-6", 1000, 100)
        assert cost == pytest.approx(0.0225, rel=1e-9)

    def test_compute_cost_unknown_returns_zero(self):
        cost = pricing.compute_cost("totally-fake-model-7", 1_000_000, 1_000_000)
        assert cost == 0.0

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv(
            "ELENCHUS_PRICING_JSON",
            '{"my-custom-model": {"input_per_1m": 1.0, "output_per_1m": 2.0}}',
        )
        pricing._reset_cache_for_tests()
        rates = pricing.lookup_rates("my-custom-model")
        assert rates == (1.0, 2.0)
        # 1000 in + 1000 out → (1000 + 2000) / 1_000_000 = 0.003
        assert pricing.compute_cost("my-custom-model", 1000, 1000) == pytest.approx(0.003)

    def test_malformed_env_override_falls_back_to_defaults(self, monkeypatch, caplog):
        monkeypatch.setenv("ELENCHUS_PRICING_JSON", "not valid json")
        pricing._reset_cache_for_tests()
        # Default rates still loaded.
        assert pricing.lookup_rates("claude-opus-4-6") is not None
        # Warning emitted.
        with caplog.at_level("WARNING", logger="elenchus.pricing"):
            pricing._reset_cache_for_tests()
            pricing._load_pricing()
        assert any("Failed to parse" in r.message for r in caplog.records)


# ─── Platform DB helpers ─────────────────────────────────────────────


class TestRecordUsage:
    def test_inserts_one_row(self):
        con = get_registry().platform_con()
        rid = pdb.record_usage(
            con,
            actor_id=None,
            base_id=None,
            model="claude-opus-4-6",
            category="success",
            prompt_tokens=100,
            completion_tokens=50,
            cost_usd=0.005,
            attempts=1,
            latency_ms=420,
        )
        assert rid > 0
        row = con.execute(
            "SELECT model, category, prompt_tokens, completion_tokens, cost_usd, "
            "attempts, latency_ms FROM usage WHERE id = ?",
            [rid],
        ).fetchone()
        assert row == ("claude-opus-4-6", "success", 100, 50, 0.005, 1, 420)

    def test_nullable_actor_and_base(self):
        con = get_registry().platform_con()
        rid = pdb.record_usage(
            con,
            actor_id=None,
            base_id=None,
            model="m",
            category="success",
            prompt_tokens=0,
            completion_tokens=0,
            cost_usd=0.0,
            attempts=1,
            latency_ms=0,
        )
        row = con.execute("SELECT actor_id, base_id FROM usage WHERE id = ?", [rid]).fetchone()
        assert row == (None, None)


class TestAggregations:
    def _seed_one(
        self,
        *,
        actor_id=None,
        base_id=None,
        model="claude-opus-4-6",
        category="success",
        prompt=10,
        completion=5,
        cost=0.001,
    ):
        con = get_registry().platform_con()
        pdb.record_usage(
            con,
            actor_id=actor_id,
            base_id=base_id,
            model=model,
            category=category,
            prompt_tokens=prompt,
            completion_tokens=completion,
            cost_usd=cost,
            attempts=1,
            latency_ms=100,
        )

    def test_total_cost_aggregates(self):
        for _ in range(3):
            self._seed_one(cost=0.10)
        result = pdb.total_cost(get_registry().platform_con())
        assert result["cost_usd"] == pytest.approx(0.30)
        assert result["calls"] == 3
        assert result["successful_calls"] == 3
        assert result["prompt_tokens"] == 30
        assert result["completion_tokens"] == 15

    def test_total_cost_counts_failures_in_calls_but_not_in_successful(self):
        self._seed_one(category="success", cost=0.10)
        self._seed_one(category="rate_limit", cost=0.0)
        result = pdb.total_cost(get_registry().platform_con())
        assert result["calls"] == 2
        assert result["successful_calls"] == 1
        assert result["cost_usd"] == pytest.approx(0.10)

    def test_cost_by_actor(self):
        con = get_registry().platform_con()
        a1 = pdb.create_actor(
            con,
            kind="user",
            email="a1@example.com",
            display_name="A1",
            password_hash=None,
        )
        a2 = pdb.create_actor(
            con,
            kind="user",
            email="a2@example.com",
            display_name="A2",
            password_hash=None,
        )
        self._seed_one(actor_id=a1, cost=0.50)
        self._seed_one(actor_id=a1, cost=0.30)
        self._seed_one(actor_id=a2, cost=0.10)
        self._seed_one(actor_id=None, cost=0.05)  # system call

        rows = pdb.cost_by_actor(con)
        # Sorted by cost desc.
        assert rows[0]["actor_id"] == a1
        assert rows[0]["cost_usd"] == pytest.approx(0.80)
        assert rows[0]["calls"] == 2
        assert rows[1]["actor_id"] == a2
        assert rows[1]["cost_usd"] == pytest.approx(0.10)
        # System call (actor_id IS NULL) appears as well.
        assert any(r["actor_id"] is None for r in rows)


# ─── End-to-end: Opponent records usage ──────────────────────────────


class TestOpponentRecordsUsage:
    """When `respond` / `async_respond` is called with actor_id + base_id,
    a usage row appears in the platform DB after the call completes."""

    def test_respond_records_usage_on_success(self):
        from unittest.mock import patch

        from elenchus.dialectical_state import DialecticalState
        from elenchus.opponent import Opponent

        admin_id = _create_admin()
        state = DialecticalState.in_memory("test")
        opp = Opponent(api_key=None, model="claude-opus-4-6")

        success = ChatResult(
            category=ChatCategory.SUCCESS,
            text='{"speech_acts":[],"new_tensions":[],"response":"ok"}',
            attempts=1,
            latency_ms=42,
            prompt_tokens=100,
            completion_tokens=50,
            model="claude-opus-4-6",
        )
        with patch.object(opp._llm_client, "chat", return_value=success):
            opp.respond("hi", state, actor_id=admin_id, base_id="test-base")

        con = get_registry().platform_con()
        rows = con.execute(
            "SELECT actor_id, base_id, model, category, prompt_tokens, "
            "completion_tokens, attempts, latency_ms FROM usage"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0] == (admin_id, "test-base", "claude-opus-4-6", "success", 100, 50, 1, 42)
        # And cost matches the pricing computation.
        cost = con.execute("SELECT cost_usd FROM usage").fetchone()[0]
        expected = pricing.compute_cost("claude-opus-4-6", 100, 50)
        assert cost == pytest.approx(expected)

        state.base.con.close()

    def test_respond_records_usage_on_failure(self):
        """Failed calls (rate_limit, etc.) still write a row so the
        dashboard can surface error rates alongside cost."""
        from unittest.mock import patch

        from elenchus.dialectical_state import DialecticalState
        from elenchus.opponent import LLMCallError, Opponent

        admin_id = _create_admin()
        state = DialecticalState.in_memory("test")
        opp = Opponent(api_key=None, model="claude-opus-4-6")

        fail = ChatResult(
            category=ChatCategory.RATE_LIMIT,
            attempts=3,
            latency_ms=4500,
            model="claude-opus-4-6",
            error_message="429",
            exception_type="RateLimitError",
        )
        with (
            patch.object(opp._llm_client, "chat", return_value=fail),
            pytest.raises(LLMCallError),
        ):
            opp.respond("hi", state, actor_id=admin_id, base_id="b")

        con = get_registry().platform_con()
        row = con.execute("SELECT category, attempts, cost_usd FROM usage").fetchone()
        assert row[0] == "rate_limit"
        assert row[1] == 3
        assert row[2] == 0.0  # no tokens were spent

        state.base.con.close()

    def test_no_actor_no_base_still_records(self):
        """CLI / system calls (actor_id=None) record a row with NULL
        actor_id rather than skipping."""
        from unittest.mock import patch

        from elenchus.dialectical_state import DialecticalState
        from elenchus.opponent import Opponent

        # Need at least the platform DB initialized — admin not required.
        get_registry().migrate_platform()
        state = DialecticalState.in_memory("test")
        opp = Opponent(api_key=None, model="claude-opus-4-6")

        success = ChatResult(
            category=ChatCategory.SUCCESS,
            text='{"speech_acts":[],"new_tensions":[],"response":"ok"}',
            attempts=1,
            latency_ms=1,
            prompt_tokens=5,
            completion_tokens=3,
            model="claude-opus-4-6",
        )
        with patch.object(opp._llm_client, "chat", return_value=success):
            opp.respond("hi", state)  # no actor_id/base_id

        con = get_registry().platform_con()
        rows = con.execute("SELECT actor_id, base_id FROM usage").fetchall()
        assert len(rows) == 1
        assert rows[0] == (None, None)

        state.base.con.close()


# ─── Admin endpoint ──────────────────────────────────────────────────


class TestAdminUsageEndpoint:
    def test_admin_only(self):
        # Unauthenticated.
        r = client.get("/api/admin/usage")
        assert r.status_code == 401

        # Non-admin user.
        con = get_registry().platform_con()
        user_id = pdb.create_actor(
            con,
            kind="user",
            email="u@example.com",
            display_name="U",
            password_hash=auth.hash_password("pw"),
        )
        client.cookies.set(auth.SESSION_COOKIE, auth.create_session(user_id))
        r = client.get("/api/admin/usage")
        assert r.status_code == 403

    def test_returns_zero_totals_when_empty(self):
        _create_admin()
        r = client.get("/api/admin/usage")
        assert r.status_code == 200
        data = r.json()
        assert data["total"]["cost_usd"] == 0
        assert data["total"]["calls"] == 0
        assert data["by_day"] == []

    def test_returns_seeded_rollup(self):
        admin_id = _create_admin()
        con = get_registry().platform_con()
        pdb.record_usage(
            con,
            actor_id=admin_id,
            base_id="b",
            model="claude-opus-4-6",
            category="success",
            prompt_tokens=200,
            completion_tokens=100,
            cost_usd=0.10,
            attempts=1,
            latency_ms=300,
        )
        r = client.get("/api/admin/usage?days=7")
        assert r.status_code == 200
        data = r.json()
        assert data["window_days"] == 7
        assert data["total"]["calls"] == 1
        assert data["total"]["cost_usd"] == pytest.approx(0.10)
        assert len(data["by_actor"]) == 1
        assert data["by_actor"][0]["email"] == "admin@example.com"
        assert data["by_actor"][0]["cost_usd"] == pytest.approx(0.10)
