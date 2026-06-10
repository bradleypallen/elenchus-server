"""Tests for the Phase C/4 per-base integrity report.

Three slices:
  1. Platform-DB rollup helpers (`usage_for_base`, `total_cost_for_base`).
  2. `compute_base_integrity` end-to-end — usage stats + content metrics.
  3. Admin endpoints.
"""

from __future__ import annotations

import contextlib
import os
import shutil

import pytest
from fastapi.testclient import TestClient

from elenchus import auth, integrity
from elenchus.db import get_registry
from elenchus.db import platform as pdb
from elenchus.dialectical_state import DialecticalState
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
    bases_dir = os.path.join(_test_data_dir, "bases")
    if os.path.isdir(bases_dir):
        shutil.rmtree(bases_dir, ignore_errors=True)


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


def _create_base_with_content(actor_id: int, name: str) -> str:
    """Spin up a registered base with realistic dialectical content."""
    reg = get_registry()
    path = reg.db_path(name, actor_id=actor_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    state = DialecticalState.create(path, name)

    # Position
    state.commit("Sky is blue.")
    state.commit("Grass is green.")
    state.deny("Sky is green.")
    state.retract_prop("Grass is green.")

    # Tensions
    state.commit("Birds can fly.")
    tid1 = state.add_tension(["Birds can fly."], ["Penguins can fly."], "reasoning")
    state.accept_tension(tid1)

    state.commit("All ravens are black.")
    tid2 = state.add_tension(["All ravens are black."], ["Albino ravens exist."], "edge case")
    state.contest_tension(tid2)

    # Open tension
    state.add_tension(["Sky is blue."], ["Sky is red."], "open")

    # Conversation
    state.add_conversation("user", "Hello")
    state.add_conversation("assistant", "Hi")
    state.add_conversation("user", "Tell me about birds")
    state.base.con.close()

    with reg.platform_lock:
        pdb.create_base(reg.platform_con(), base_id=name, name=name, owner_id=actor_id)
    return path


def _seed_usage(
    *,
    base_id: str | None,
    actor_id: int | None = None,
    category: str = "success",
    model: str = "claude-opus-4-6",
    prompt: int = 100,
    completion: int = 50,
    cost: float = 0.005,
    attempts: int = 1,
    latency: int = 100,
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
        attempts=attempts,
        latency_ms=latency,
    )


# ─── Platform helpers ─────────────────────────────────────────────────


class TestUsageForBase:
    def test_returns_zeros_when_empty(self):
        result = pdb.usage_for_base(get_registry().platform_con(), "nonexistent")
        assert result["total"]["calls"] == 0
        assert result["total"]["cost_usd"] == 0
        assert result["by_category"] == []
        assert result["latency_ms"] == {"median_ms": 0, "p95_ms": 0}
        assert result["first_call_at"] is None
        assert result["last_call_at"] is None

    def test_aggregates_one_base(self):
        for _ in range(3):
            _seed_usage(base_id="b1", cost=0.10, latency=100)
        # A call against a different base should not contribute.
        _seed_usage(base_id="other", cost=99.0)

        result = pdb.usage_for_base(get_registry().platform_con(), "b1")
        assert result["total"]["calls"] == 3
        assert result["total"]["cost_usd"] == pytest.approx(0.30)
        assert result["total"]["successful_calls"] == 3
        assert len(result["by_category"]) == 1
        assert result["by_category"][0]["category"] == "success"
        assert result["by_category"][0]["calls"] == 3

    def test_groups_by_category(self):
        _seed_usage(base_id="b", category="success", cost=0.10)
        _seed_usage(base_id="b", category="success", cost=0.10)
        _seed_usage(base_id="b", category="rate_limit", cost=0.0)
        _seed_usage(base_id="b", category="auth_failure", cost=0.0)

        result = pdb.usage_for_base(get_registry().platform_con(), "b")
        cats = {r["category"]: r for r in result["by_category"]}
        assert cats["success"]["calls"] == 2
        assert cats["rate_limit"]["calls"] == 1
        assert cats["auth_failure"]["calls"] == 1
        assert result["total"]["calls"] == 4
        assert result["total"]["successful_calls"] == 2

    def test_latency_quantiles(self):
        # Latencies 100..1000 in 100ms increments → median 550, p95 ≈ 955
        for ms in range(100, 1001, 100):
            _seed_usage(base_id="b", latency=ms, cost=0.01)
        result = pdb.usage_for_base(get_registry().platform_con(), "b")
        assert result["latency_ms"]["median_ms"] >= 500
        assert result["latency_ms"]["median_ms"] <= 600
        assert result["latency_ms"]["p95_ms"] >= 900

    def test_mean_attempts_above_one_when_retries(self):
        _seed_usage(base_id="b", attempts=1)
        _seed_usage(base_id="b", attempts=3)  # one retried call
        _seed_usage(base_id="b", attempts=1)
        result = pdb.usage_for_base(get_registry().platform_con(), "b")
        assert result["mean_attempts"] == pytest.approx(5 / 3)


class TestTotalCostForBase:
    def test_isolates_to_one_base(self):
        _seed_usage(base_id="alpha", cost=1.00, prompt=100, completion=50)
        _seed_usage(base_id="beta", cost=9.99, prompt=999, completion=999)
        result = pdb.total_cost_for_base(get_registry().platform_con(), "alpha")
        assert result["cost_usd"] == pytest.approx(1.00)
        assert result["prompt_tokens"] == 100
        assert result["completion_tokens"] == 50


# ─── compute_base_integrity ──────────────────────────────────────────


class TestComputeBaseIntegrity:
    def test_unknown_base_returns_unregistered_with_error(self):
        result = integrity.compute_base_integrity("does-not-exist")
        assert result["registered"] is False
        assert result["owner_id"] is None
        assert result["usage"]["total"]["calls"] == 0
        # Content section flags the missing file rather than crashing.
        assert "error" in result["content"]

    def test_registered_base_full_report(self):
        admin_id = _create_admin()
        _create_base_with_content(admin_id, "demo")
        _seed_usage(base_id="demo", actor_id=admin_id, cost=0.10)
        _seed_usage(base_id="demo", actor_id=admin_id, cost=0.0, category="rate_limit", attempts=3)

        result = integrity.compute_base_integrity("demo")
        assert result["registered"] is True
        assert result["owner_id"] == admin_id
        # Usage section
        assert result["usage"]["total"]["calls"] == 2
        assert result["usage"]["total"]["successful_calls"] == 1
        assert result["usage"]["total"]["cost_usd"] == pytest.approx(0.10)
        assert result["usage"]["mean_attempts"] == pytest.approx(2.0)
        cats = {c["category"] for c in result["usage"]["by_category"]}
        assert cats == {"success", "rate_limit"}
        # Content section
        c = result["content"]
        assert "error" not in c
        assert c["position"]["commitments"] == 3  # Sky, Birds, Ravens (Grass retracted)
        assert c["position"]["denials"] == 1  # Sky-green
        assert c["position"]["retracted_propositions"] == 1
        assert c["tensions"]["accepted"] == 1
        assert c["tensions"]["contested"] == 1
        assert c["tensions"]["open"] == 1
        assert c["implications"]["active"] == 1
        assert c["implications"]["retracted"] == 0
        assert c["conversation"]["user_turns"] == 2
        assert c["conversation"]["assistant_turns"] == 1
        # Atoms include all propositions plus delta atoms from tensions
        assert c["atoms"] > 0

    def test_registered_but_file_missing_reports_content_error(self):
        admin_id = _create_admin()
        path = _create_base_with_content(admin_id, "ghost")
        os.remove(path)
        # Force the registry to forget the handle so it tries to reopen.
        reg = get_registry()
        for _name, handle in list(reg._handles.items()):
            with contextlib.suppress(Exception):
                handle.state.base.con.close()
        reg._handles.clear()

        result = integrity.compute_base_integrity("ghost")
        assert result["registered"] is True
        assert "error" in result["content"]


class TestListBaseIntegritySummaries:
    def test_empty_when_no_bases(self):
        _create_admin()
        assert integrity.list_base_integrity_summaries() == []

    def test_sorted_by_cost_desc(self):
        admin_id = _create_admin()
        _create_base_with_content(admin_id, "cheap")
        _create_base_with_content(admin_id, "expensive")
        _create_base_with_content(admin_id, "middle")

        _seed_usage(base_id="cheap", cost=0.10)
        _seed_usage(base_id="expensive", cost=5.00)
        _seed_usage(base_id="middle", cost=1.00)

        rows = integrity.list_base_integrity_summaries()
        assert [r["base_id"] for r in rows] == ["expensive", "middle", "cheap"]


# ─── Admin endpoints ─────────────────────────────────────────────────


class TestAdminEndpoints:
    def test_summary_admin_only(self):
        r = client.get("/api/admin/integrity")
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
        r = client.get("/api/admin/integrity")
        assert r.status_code == 403

    def test_summary_returns_rows(self):
        admin_id = _create_admin()
        _create_base_with_content(admin_id, "alpha")
        _create_base_with_content(admin_id, "beta")
        _seed_usage(base_id="alpha", cost=0.20)
        _seed_usage(base_id="beta", cost=0.10)

        r = client.get("/api/admin/integrity")
        assert r.status_code == 200
        bases = r.json()["bases"]
        assert len(bases) == 2
        assert bases[0]["base_id"] == "alpha"
        assert bases[0]["cost_usd"] == pytest.approx(0.20)

    def test_detail_admin_only(self):
        r = client.get("/api/admin/integrity/anything")
        assert r.status_code == 401

    def test_detail_returns_full_report(self):
        admin_id = _create_admin()
        _create_base_with_content(admin_id, "demo")
        _seed_usage(base_id="demo", cost=0.50, latency=200)

        r = client.get("/api/admin/integrity/demo")
        assert r.status_code == 200
        data = r.json()
        assert data["base_id"] == "demo"
        assert data["registered"] is True
        assert data["usage"]["total"]["calls"] == 1
        assert data["content"]["position"]["commitments"] == 3

    def test_detail_for_unknown_base_returns_unregistered(self):
        _create_admin()
        r = client.get("/api/admin/integrity/never-existed")
        assert r.status_code == 200
        data = r.json()
        assert data["registered"] is False
