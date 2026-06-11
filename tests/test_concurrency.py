"""Concurrency tests for the multi-user platform foundation.

Phase A D5 scope: per-base async lock + transaction wrapping in
`Opponent._record_and_apply`. These tests use `asyncio.run` rather
than `pytest-asyncio`; the latter arrives with the broader Week 1 D5
concurrency suite if/when needed.

Scenarios covered:
- A lock argument to `async_respond` serializes concurrent applies on
  the same base while allowing the LLM call itself to overlap.
- An exception inside `_record_and_apply` rolls back the transaction,
  leaving the state in its pre-message form.
- `INSERT OR IGNORE` / `INSERT OR REPLACE` patterns no longer abort an
  outer transaction when a constraint conflict would have fired with
  the previous try/except code.
"""

import asyncio
from unittest.mock import AsyncMock, patch

from elenchus.dialectical_state import DialecticalState
from elenchus.opponent import Opponent


def _make_opp():
    return Opponent(api_key="fake-key")


class TestTransactionalApply:
    """`_record_and_apply` wraps state mutations in a transaction so a
    crash mid-apply leaves the base consistent."""

    def test_clean_apply_persists(self):
        opp = _make_opp()
        state = DialecticalState.in_memory("test")

        fake = (
            '{"speech_acts":[{"type":"COMMIT","proposition":"P"}],'
            '"new_tensions":[],"response":"ok"}'
        )
        with patch.object(opp, "_async_chat", new=AsyncMock(return_value=fake)):
            asyncio.run(opp.async_respond("msg", state))

        assert "P" in state.C
        assert len(state.get_conversation()) == 2

    def test_exception_in_apply_rolls_back(self):
        """If _apply raises mid-transaction, the conversation insert that
        ran earlier in the transaction is also rolled back."""
        opp = _make_opp()
        state = DialecticalState.in_memory("test")

        # Pre-condition: no conversation rows yet.
        assert len(state.get_conversation()) == 0

        fake = (
            '{"speech_acts":[{"type":"COMMIT","proposition":"P"}],'
            '"new_tensions":[],"response":"ok"}'
        )

        # Force _apply to raise after the conversation has been written.
        # If the transaction works correctly, the conversation rows
        # written earlier in _record_and_apply should be rolled back.
        original_apply = opp._apply

        def _boom(parsed, st):
            original_apply(parsed, st)
            raise RuntimeError("simulated post-apply failure")

        with (
            patch.object(opp, "_async_chat", new=AsyncMock(return_value=fake)),
            patch.object(opp, "_apply", side_effect=_boom),
        ):
            raised = False
            try:
                asyncio.run(opp.async_respond("msg", state))
            except RuntimeError:
                raised = True
            assert raised

        # Transaction rolled back: conversation is empty, P not in C.
        assert len(state.get_conversation()) == 0
        assert "P" not in state.C


class TestIdempotentInserts:
    """Verify the upsert patterns work inside an outer transaction —
    the previous try/except patterns would have aborted it."""

    def test_redundant_commit_inside_apply(self):
        """Applying a COMMIT for an already-committed atom must not abort
        the surrounding transaction."""
        opp = _make_opp()
        state = DialecticalState.in_memory("test")
        state.commit("P")  # P now in C

        # async_respond will run _apply with COMMIT P again — previously
        # this would raise ConstraintException inside the transaction.
        fake = (
            '{"speech_acts":[{"type":"COMMIT","proposition":"P"},'
            '{"type":"COMMIT","proposition":"Q"}],'
            '"new_tensions":[],"response":"ok"}'
        )
        with patch.object(opp, "_async_chat", new=AsyncMock(return_value=fake)):
            asyncio.run(opp.async_respond("msg", state))

        # Both commits applied; the redundant P didn't break Q's insertion.
        assert "P" in state.C
        assert "Q" in state.C

    def test_new_tension_with_existing_atoms(self):
        """A new_tension that references already-existing atoms used to
        abort the transaction via the constraint-suppress pattern."""
        opp = _make_opp()
        state = DialecticalState.in_memory("test")
        state.commit("P")  # P in atoms and in C

        fake = (
            '{"speech_acts":[],"new_tensions":['
            '{"gamma":["P"],"delta":["Q"],"reason":"r"}'
            '],"response":"ok"}'
        )
        with patch.object(opp, "_async_chat", new=AsyncMock(return_value=fake)):
            asyncio.run(opp.async_respond("msg", state))

        assert len(state.T) == 1


class TestPerBaseLock:
    """The per-base lock serializes the apply phase of concurrent
    callers on the same base while leaving the LLM call un-locked."""

    def test_lock_argument_serializes_applies(self):
        """Two concurrent async_respond calls with the same lock produce
        deterministic state — the second apply sees the first's writes."""
        opp = _make_opp()
        state = DialecticalState.in_memory("test")
        lock = asyncio.Lock()

        fake1 = (
            '{"speech_acts":[{"type":"COMMIT","proposition":"A"}],'
            '"new_tensions":[],"response":"ok1"}'
        )
        fake2 = (
            '{"speech_acts":[{"type":"COMMIT","proposition":"B"}],'
            '"new_tensions":[],"response":"ok2"}'
        )

        responses = iter([fake1, fake2])

        async def fake_chat(*args, **kwargs):
            # Slight yield so both tasks interleave at the LLM call.
            await asyncio.sleep(0)
            return next(responses)

        async def driver():
            with patch.object(opp, "_async_chat", side_effect=fake_chat):
                await asyncio.gather(
                    opp.async_respond("msg1", state, lock=lock),
                    opp.async_respond("msg2", state, lock=lock),
                )

        asyncio.run(driver())

        # Both apply blocks ran; the lock serialized them. Conversation
        # has exactly 4 entries (2 user + 2 assistant), and both commits
        # are present.
        assert "A" in state.C
        assert "B" in state.C
        assert len(state.get_conversation()) == 4

    def test_no_lock_argument_still_works(self):
        """When lock=None (default, used by CLI and tests), the function
        runs without serialization and produces correct single-thread
        results."""
        opp = _make_opp()
        state = DialecticalState.in_memory("test")

        fake = (
            '{"speech_acts":[{"type":"COMMIT","proposition":"P"}],'
            '"new_tensions":[],"response":"ok"}'
        )
        with patch.object(opp, "_async_chat", new=AsyncMock(return_value=fake)):
            asyncio.run(opp.async_respond("msg", state, lock=None))

        assert "P" in state.C


class TestBaseHandleLock:
    """The BaseHandle's lazily-initialized lock can be used from any
    async context and remains stable across accesses."""

    def test_lock_is_stable_across_accesses(self):
        from elenchus.db.registry import BaseHandle

        async def get_lock():
            handle = BaseHandle(state=DialecticalState.in_memory("t"))
            return handle.lock, handle.lock

        l1, l2 = asyncio.run(get_lock())
        assert l1 is l2

    def test_lock_can_be_acquired(self):
        from elenchus.db.registry import BaseHandle

        async def check():
            handle = BaseHandle(state=DialecticalState.in_memory("t"))
            async with handle.lock:
                # Inside the lock; acquiring again would deadlock so we
                # don't test that — just verify entry/exit works.
                pass
            return True

        assert asyncio.run(check()) is True


class TestPlatformConnectionConcurrency:
    """The single platform DuckDB connection is shared across FastAPI's
    threadpool. A bare connection isn't safe for concurrent execute/fetch
    — interleaved queries clobber each other's result state, so a session
    lookup intermittently returns None → a spurious 401. The serializing
    wrapper (`registry._SerializedConnection`) must make concurrent reads
    deterministic. Regression for the bug the browser E2E surfaced.

    This hammers the connection directly (not via TestClient, whose
    single anyio portal serializes calls and would hide the race).
    """

    def test_concurrent_session_lookups_never_drop(self):
        import os
        from concurrent.futures import ThreadPoolExecutor

        from elenchus import auth
        from elenchus.db import get_registry, init_registry
        from elenchus.db import platform as pdb

        # This file doesn't import elenchus.server, so the registry may not
        # be initialized yet. Init only if needed (idempotent re-init would
        # clobber the shared one other test files rely on).
        try:
            reg = get_registry()
        except RuntimeError:
            init_registry(os.environ["ELENCHUS_DATA"])
            reg = get_registry()
        reg.migrate_platform()
        con = reg.platform_con()

        email = "concurrency@example.com"
        with reg.platform_lock:
            existing = pdb.find_actor_by_email(con, email)
            actor_id = (
                existing["id"]
                if existing
                else pdb.create_actor(
                    con,
                    kind="user",
                    email=email,
                    display_name="Concurrency",
                    password_hash=auth.hash_password("pw"),
                )
            )
        token = auth.create_session(actor_id)

        # Each worker resolves the session token AND does an unrelated
        # read, the way several concurrent authenticated requests would.
        def worker(_):
            results = []
            for _ in range(40):
                a = auth.resolve_token(token)
                b = pdb.find_actor_by_email(con, email)
                results.append(a is not None and a["id"] == actor_id and b is not None)
            return all(results)

        with ThreadPoolExecutor(max_workers=16) as ex:
            outcomes = list(ex.map(worker, range(16)))

        # Without serialization, some lookups return None → False here.
        assert all(outcomes), "a concurrent session lookup spuriously returned None"
