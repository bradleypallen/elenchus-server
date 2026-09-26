"""Tests for the registry's bounded LRU of open base files (`db/registry.py`).

Each open per-base DuckDB file costs ~10 MB resident, and a study
touches far more bases than are ever in use at once, so the registry
closes what nobody is using: least recently used first, beyond a bound
or after an idle period. What is tested is what could go wrong: a base
closed while a request still holds it (locked, pinned, or fetched
moments ago), a closed base that doesn't come back cleanly, a one-off
read that leaves a base warm, shutdown leaving something open, and a
bad setting.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from unittest.mock import patch

import duckdb
import pytest

from elenchus.db import registry as registry_mod
from elenchus.db.registry import DBRegistry
from elenchus.dialectical_state import DialecticalState

LOGGER = "elenchus.db.registry"


def _make(tmp_path, **kw) -> DBRegistry:
    # grace=0: a just-touched handle may be closed at once, so the tests
    # can drive eviction without waiting a minute.
    kw.setdefault("grace", 0)
    kw.setdefault("idle_ttl", 10_000)
    return DBRegistry(str(tmp_path), **kw)


def _create(reg: DBRegistry, name: str, *commits: str) -> str:
    """A base file on disk (flat layout — there is no platform row),
    closed so `reg.get` opens it fresh. Returns its path."""
    path = reg.db_path(name)
    state = DialecticalState.create(path, name)
    for prop in commits:
        state.commit(prop)
    state.base.con.close()
    return path


def _closed(state: DialecticalState) -> bool:
    try:
        state.base.con.execute("SELECT 1")
    except duckdb.ConnectionException:
        return True
    return False


def _open_names(reg: DBRegistry) -> list[str]:
    return list(reg._handles)


class TestBound:
    def test_the_bound_holds_and_the_oldest_go_first(self, tmp_path):
        reg = _make(tmp_path, capacity=3)
        states = {}
        for i in range(5):
            name = f"b{i}"
            _create(reg, name)
            states[name] = reg.get(name)
        assert len(reg) == 3
        assert _open_names(reg) == ["b2", "b3", "b4"]
        assert _closed(states["b0"]) and _closed(states["b1"])
        assert not _closed(states["b4"])

    def test_a_lookup_is_a_use(self, tmp_path):
        reg = _make(tmp_path, capacity=3)
        for name in ("b0", "b1", "b2", "b3"):
            _create(reg, name)
        reg.get("b0"), reg.get("b1"), reg.get("b2")
        reg.get("b0")  # b0 is now the most recently used
        reg.get("b3")
        assert _open_names(reg) == ["b2", "b0", "b3"]

    def test_a_fresh_base_is_never_closed_to_make_room(self, tmp_path, caplog):
        """With every other handle held, the one being opened must
        survive even under a bound of one — the bound is exceeded and
        the log says so."""
        reg = _make(tmp_path, capacity=1)
        _create(reg, "held"), _create(reg, "new")
        with reg.hold("held"), caplog.at_level(logging.WARNING, logger=LOGGER):
            state = reg.get("new")
        assert not _closed(state)
        assert "2 bases open, above the bound of 1" in caplog.text
        assert registry_mod.CAPACITY_ENV in caplog.text

    def test_the_grace_period_protects_a_recent_lookup(self, tmp_path):
        """A sync route that fetched a state seconds ago may still be
        using it: over the bound, only handles idle past the grace
        period are closed."""
        reg = _make(tmp_path, capacity=1, grace=60)
        _create(reg, "b0"), _create(reg, "b1")
        first = reg.get("b0")
        reg.get("b1")
        assert len(reg) == 2 and not _closed(first)
        reg._handles["b0"].last_used -= 61
        assert reg.evict_idle() == ["b0"]
        assert _closed(first) and _open_names(reg) == ["b1"]


class TestHeld:
    def test_a_locked_handle_is_skipped(self, tmp_path):
        """The per-base lock means a turn or a text save is inside a
        transaction on that connection."""
        reg = _make(tmp_path, capacity=1)
        for name in ("locked", "b1", "b2"):
            _create(reg, name)

        async def scenario():
            handle = reg.get_handle("locked")
            async with handle.lock:
                reg.get("b1")
                reg.get("b2")
                assert "locked" in reg and "b1" not in reg and "b2" in reg
                assert not _closed(handle.state)
            # Lock released: the next sweep may close it.
            assert reg.evict_idle() == ["locked"]

        asyncio.run(scenario())

    def test_a_pinned_handle_is_skipped(self, tmp_path):
        """`hold` is for code that keeps the state across a long wait
        without the lock — the message route across its LLM call."""
        reg = _make(tmp_path, capacity=1, idle_ttl=0)
        _create(reg, "pinned"), _create(reg, "other")
        with reg.hold("pinned") as handle:
            reg.get("other")
            assert reg.evict_idle() == ["other"]
            assert "pinned" in reg and not _closed(handle.state)
        # Released: the sweep on release closed it (idle_ttl=0).
        assert "pinned" not in reg and _closed(handle.state)

    def test_holds_nest(self, tmp_path):
        reg = _make(tmp_path, idle_ttl=0)
        _create(reg, "b0")
        with reg.hold("b0") as outer:
            with reg.hold("b0") as inner:
                assert outer is inner and inner.pins == 2
                assert reg.evict_idle() == []
            # The inner release leaves the outer pin in place.
            assert outer.pins == 1 and reg.evict_idle() == []
        assert outer.pins == 0 and "b0" not in reg


class TestReopen:
    def test_reopen_after_eviction_is_transparent(self, tmp_path):
        """A closed base comes back on the next lookup with everything
        written before, and no stale WAL beside the file."""
        reg = _make(tmp_path, capacity=1)
        path = _create(reg, "b0", "P")
        _create(reg, "b1")
        first = reg.get("b0")
        first.commit("Q")
        reg.get("b1")  # evicts b0
        assert _closed(first)
        assert not os.path.exists(path + ".wal")
        again = reg.get("b0")
        assert again is not first
        assert {"P", "Q"} <= set(again.C)
        again.commit("R")
        assert "R" in again.C

    def test_the_same_object_while_it_stays_open(self, tmp_path):
        reg = _make(tmp_path)
        _create(reg, "b0")
        assert reg.get("b0") is reg.get("b0") is reg.get_handle("b0").state

    def test_remove_closes_at_once(self, tmp_path):
        reg = _make(tmp_path)
        _create(reg, "b0")
        state = reg.get("b0")
        assert reg.remove("b0") is True
        assert _closed(state) and "b0" not in reg
        assert reg.remove("b0") is False


class TestTransientHold:
    def test_closes_what_it_opened(self, tmp_path, caplog):
        reg = _make(tmp_path, grace=60)  # even inside the grace period
        _create(reg, "b0")
        with caplog.at_level(logging.INFO, logger=LOGGER), reg.hold("b0", transient=True) as h:
            assert "b0" in reg
            state = h.state
        assert "b0" not in reg and _closed(state)
        assert "Closed base 'b0' (idle, after a one-off read; 0 open)" in caplog.text

    def test_keeps_a_base_that_was_already_open(self, tmp_path):
        reg = _make(tmp_path, grace=60)
        _create(reg, "b0")
        state = reg.get("b0")
        with reg.hold("b0", transient=True):
            pass
        assert "b0" in reg and not _closed(state)

    def test_keeps_a_base_someone_looked_up_meanwhile(self, tmp_path):
        """Another request fetched the state during the block; it may
        still be using it, so the release must not mark it cold."""
        reg = _make(tmp_path, grace=60)
        _create(reg, "b0")
        with reg.hold("b0", transient=True):
            other = reg.get("b0")
        assert "b0" in reg and not _closed(other)

    def test_a_batch_keeps_one_base_open_at_a_time(self, tmp_path):
        reg = _make(tmp_path, grace=60)
        for i in range(10):
            _create(reg, f"b{i}")
        peak = 0
        for i in range(10):
            with reg.hold(f"b{i}", transient=True):
                peak = max(peak, len(reg))
        assert peak == 1 and len(reg) == 0


class TestIdle:
    def test_idle_handles_are_closed_whatever_the_count(self, tmp_path):
        reg = _make(tmp_path, capacity=100, idle_ttl=0)
        _create(reg, "b0"), _create(reg, "b1")
        reg.get("b0"), reg.get("b1")
        # The sweep after the second open already closed the first.
        assert _open_names(reg) == ["b1"]
        assert reg.evict_idle() == ["b1"]
        assert len(reg) == 0

    def test_nothing_idle_nothing_closed(self, tmp_path):
        reg = _make(tmp_path, idle_ttl=1000)
        _create(reg, "b0")
        reg.get("b0")
        assert reg.evict_idle() == [] and "b0" in reg

    def test_idle_never_undercuts_the_grace_period(self, tmp_path):
        reg = _make(tmp_path, idle_ttl=0, grace=60)
        _create(reg, "b0")
        reg.get("b0")
        assert reg.evict_idle() == []


class TestShutdown:
    def test_close_all_closes_everything(self, tmp_path):
        reg = _make(tmp_path)
        paths = [_create(reg, f"b{i}") for i in range(3)]
        states = [reg.get(f"b{i}") for i in range(3)]
        states[0].commit("P")
        reg.platform_con()
        with reg.hold("b1"):  # even a held one: this is shutdown
            reg.close_all()
        assert len(reg) == 0
        assert all(_closed(s) for s in states)
        assert not any(os.path.exists(p + ".wal") for p in paths)
        assert reg._platform_con is None


class TestConfig:
    def test_defaults(self, tmp_path, monkeypatch):
        monkeypatch.delenv(registry_mod.CAPACITY_ENV, raising=False)
        monkeypatch.delenv(registry_mod.IDLE_TTL_ENV, raising=False)
        reg = DBRegistry(str(tmp_path))
        assert reg.capacity == registry_mod.DEFAULT_CAPACITY == 32
        assert reg.idle_ttl == registry_mod.DEFAULT_IDLE_TTL == 900

    def test_from_the_environment(self, tmp_path, monkeypatch):
        monkeypatch.setenv(registry_mod.CAPACITY_ENV, "5")
        monkeypatch.setenv(registry_mod.IDLE_TTL_ENV, "120")
        reg = DBRegistry(str(tmp_path))
        assert reg.capacity == 5 and reg.idle_ttl == 120.0

    @pytest.mark.parametrize("bad", ["lots", "0", "-3", "2.5"])
    def test_a_bad_bound_is_ignored(self, tmp_path, monkeypatch, caplog, bad):
        monkeypatch.setenv(registry_mod.CAPACITY_ENV, bad)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            reg = DBRegistry(str(tmp_path))
        assert reg.capacity == registry_mod.DEFAULT_CAPACITY
        assert registry_mod.CAPACITY_ENV in caplog.text

    @pytest.mark.parametrize("bad", ["soon", "-1", "nan"])
    def test_a_bad_idle_time_is_ignored(self, tmp_path, monkeypatch, caplog, bad):
        monkeypatch.setenv(registry_mod.IDLE_TTL_ENV, bad)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            reg = DBRegistry(str(tmp_path))
        assert reg.idle_ttl == registry_mod.DEFAULT_IDLE_TTL

    def test_the_policy_is_logged_at_startup(self, tmp_path, caplog):
        reg = _make(tmp_path, capacity=7, idle_ttl=300, grace=60)
        with caplog.at_level(logging.INFO, logger=LOGGER):
            reg.log_policy()
        assert "up to 7 open, closed after 300s idle" in caplog.text

    def test_the_server_logs_the_policy_on_startup(self, caplog):
        from fastapi.testclient import TestClient

        import elenchus.server as srv

        with caplog.at_level(logging.INFO, logger=LOGGER), TestClient(srv.app):
            pass
        assert "Base cache: up to" in caplog.text


class TestLogging:
    def test_every_eviction_is_logged_with_its_reason(self, tmp_path, caplog):
        reg = _make(tmp_path, capacity=1)
        _create(reg, "b0"), _create(reg, "b1")
        reg.get("b0")
        with caplog.at_level(logging.INFO, logger=LOGGER):
            reg.get("b1")
        assert "Closed base 'b0' (capacity, idle 0s; 1 open)" in caplog.text


class TestServerSweep:
    def test_the_server_sweeps_periodically(self, monkeypatch):
        """The lifespan task calls `evict_idle` on the process-wide
        registry every interval, off the event loop."""
        import elenchus.server as srv
        from elenchus.db import get_registry

        monkeypatch.setattr(srv, "SWEEP_INTERVAL_SECONDS", 0.01)

        async def run():
            task = asyncio.create_task(srv._sweep_open_bases())
            await asyncio.sleep(0.1)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        with patch.object(get_registry(), "evict_idle", return_value=[]) as sweep:
            asyncio.run(run())
        assert sweep.call_count >= 1

    def test_a_failing_sweep_does_not_end_the_task(self, monkeypatch, caplog):
        import elenchus.server as srv
        from elenchus.db import get_registry

        monkeypatch.setattr(srv, "SWEEP_INTERVAL_SECONDS", 0.01)

        async def run():
            task = asyncio.create_task(srv._sweep_open_bases())
            await asyncio.sleep(0.1)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            return task

        with (
            patch.object(get_registry(), "evict_idle", side_effect=RuntimeError("boom")) as sweep,
            caplog.at_level(logging.ERROR, logger="elenchus.server"),
        ):
            asyncio.run(run())
        assert sweep.call_count >= 2
        assert "Sweep of open bases failed" in caplog.text
