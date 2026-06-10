"""Tests for Phase D/3 — baseline (AI-as-tool) condition mode.

Three slices:
  1. `BASELINE_SYSTEM_PROMPT` content — distinct from the dialectic
     prompt, no opponent / Socratic framing.
  2. `Opponent.async_baseline_respond` — stores transcript, skips
     speech-act parsing, leaves the bilateral position untouched.
  3. Message route — picks the baseline path when the actor has a
     live BASELINE-condition session bound to the request's base,
     and the dialectic path otherwise.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from elenchus import auth
from elenchus.db import get_registry
from elenchus.db import platform as pdb
from elenchus.dialectical_state import DialecticalState
from elenchus.llm_client import ChatCategory, ChatResult
from elenchus.opponent import BASELINE_SYSTEM_PROMPT, Opponent
from elenchus.server import _is_baseline_for_actor_and_base, app

client = TestClient(app)
_test_data_dir = os.environ["ELENCHUS_DATA"]


@pytest.fixture(autouse=True)
def _clean():
    reg = get_registry()
    reg.migrate_platform()
    con = reg.platform_con()
    with reg.platform_lock:
        for table in (
            "participant_session_tokens",
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
    yield
    client.cookies.clear()
    for root, _dirs, files in os.walk(_test_data_dir):
        for f in files:
            if f.endswith(".duckdb") and f != "platform.duckdb":
                with contextlib.suppress(OSError):
                    os.remove(os.path.join(root, f))


# ─── BASELINE_SYSTEM_PROMPT content ──────────────────────────────────


class TestBaselinePrompt:
    """The baseline prompt is the canonical AI-as-tool framing. It
    must NOT mention the Elenchus dialectical vocabulary; that would
    confound the within-subjects comparison."""

    def test_omits_dialectical_vocabulary(self):
        forbidden = [
            "tension",
            "speech act",
            "bilateral",
            "commitment",
            "denial",
            "elenchus",
            "socratic",
            "prover-skeptic",
            "opponent",
        ]
        prompt_lower = BASELINE_SYSTEM_PROMPT.lower()
        for word in forbidden:
            assert word not in prompt_lower, (
                f"baseline prompt mentions {word!r} — would confound the "
                f"AI-as-tool vs AI-as-collaborator contrast"
            )

    def test_mentions_tool_assistant_framing(self):
        # Concept words it MUST mention so the LLM understands the
        # job. Loose check — any one of these suffices.
        prompt_lower = BASELINE_SYSTEM_PROMPT.lower()
        assert "assistant" in prompt_lower
        assert "domain" in prompt_lower or "specification" in prompt_lower


# ─── Opponent.async_baseline_respond ─────────────────────────────────


class TestBaselineRespond:
    """The baseline path stores both turns and returns the response
    text. It must NOT touch atoms, positions, tensions, or
    implications — those are dialectic-only concepts."""

    def _opp(self) -> Opponent:
        return Opponent(api_key=None, model="claude-opus-4-6")

    def _success(self, text: str) -> ChatResult:
        return ChatResult(
            category=ChatCategory.SUCCESS,
            text=text,
            attempts=1,
            latency_ms=42,
            prompt_tokens=20,
            completion_tokens=8,
            model="claude-opus-4-6",
        )

    def test_returns_response_text_and_empty_state_arrays(self):
        opp = self._opp()
        state = DialecticalState.in_memory("baseline")
        result_text = "Sure, biomes are usually classified by climate and biota."

        with patch.object(
            opp._llm_client, "achat", new=AsyncMock(return_value=self._success(result_text))
        ):
            result = asyncio.run(
                opp.async_baseline_respond("Tell me about biome classifications.", state)
            )
        assert result["response"] == result_text
        assert result["speech_acts"] == []
        assert result["new_tensions"] == []
        state.base.con.close()

    def test_stores_both_turns_in_conversation(self):
        opp = self._opp()
        state = DialecticalState.in_memory("baseline")
        text = "Marine, freshwater, and terrestrial are the top-level distinctions."
        with patch.object(
            opp._llm_client, "achat", new=AsyncMock(return_value=self._success(text))
        ):
            asyncio.run(opp.async_baseline_respond("How would you split biomes?", state))

        conversation = state.get_conversation()
        assert len(conversation) == 2
        assert conversation[0]["role"] == "user"
        assert conversation[0]["content"] == "How would you split biomes?"
        assert conversation[1]["role"] == "assistant"
        assert conversation[1]["content"] == text
        state.base.con.close()

    def test_does_not_touch_dialectical_state(self):
        """The baseline path must NOT add atoms / positions / tensions
        even if the LLM's response looks like structured JSON. That
        keeps the within-subjects comparison clean."""
        opp = self._opp()
        state = DialecticalState.in_memory("baseline")

        # Even if the LLM returns dialectic-shaped JSON (e.g. because
        # of conversation history bleed), we don't parse it.
        json_response = (
            '{"speech_acts": [{"type": "COMMIT", "proposition": "X"}], '
            '"new_tensions": [{"gamma":["X"],"delta":["Y"]}], "response": "hi"}'
        )
        with patch.object(
            opp._llm_client, "achat", new=AsyncMock(return_value=self._success(json_response))
        ):
            asyncio.run(opp.async_baseline_respond("hi", state))

        assert state.C == []
        assert state.D == []
        assert state.T == []
        assert state.I == []
        # The transcript is still stored verbatim.
        assert state.get_conversation()[1]["content"] == json_response
        state.base.con.close()

    def test_uses_baseline_system_prompt(self):
        """Verify the LLM is sent the baseline prompt, not the
        Elenchus / Phase B prompts."""
        opp = self._opp()
        state = DialecticalState.in_memory("baseline")

        mock = AsyncMock(return_value=self._success("ok"))
        with patch.object(opp._llm_client, "achat", new=mock):
            asyncio.run(opp.async_baseline_respond("hi", state))

        _args, kwargs = mock.call_args
        assert kwargs["system"] == BASELINE_SYSTEM_PROMPT
        state.base.con.close()

    def test_context_window_trims_old_turns(self):
        """The conversation history sent to the LLM is windowed to the
        last `context_turns * 2` messages so the prompt doesn't grow
        without bound across a 60-minute session."""
        opp = self._opp()
        state = DialecticalState.in_memory("baseline")
        # Seed 20 user + 20 assistant turns.
        for i in range(20):
            state.add_conversation("user", f"user-{i}")
            state.add_conversation("assistant", f"assistant-{i}")

        mock = AsyncMock(return_value=self._success("ok"))
        with patch.object(opp._llm_client, "achat", new=mock):
            asyncio.run(opp.async_baseline_respond("new message", state, context_turns=3))

        # 3 turns * 2 = 6 history messages + 1 new user message = 7 total.
        sent_messages = mock.call_args[0][0]
        assert len(sent_messages) == 7
        # The last message is the new user turn.
        assert sent_messages[-1] == {"role": "user", "content": "new message"}
        state.base.con.close()


# ─── _is_baseline_for_actor_and_base ─────────────────────────────────


def _make_participant_with_session(*, condition: str, base_id: str | None) -> tuple[int, int]:
    """Create a participant actor + study session bound to the given
    base (or none). Returns (actor_id, session_id)."""
    con = get_registry().platform_con()
    researcher_id = pdb.create_actor(
        con,
        kind="researcher",
        email="r@example.com",
        display_name="R",
        password_hash=auth.hash_password("pw"),
    )
    participant_id = pdb.create_actor(
        con,
        kind="participant",
        email=None,
        display_name="P",
        password_hash=None,
    )
    sid = pdb.create_study_session(
        con,
        actor_id=participant_id,
        study_token="t-1",
        condition=condition,
        initial_state="active",
    )
    if base_id is not None:
        pdb.create_base(con, base_id=base_id, name=base_id, owner_id=participant_id)
        pdb.attach_base_to_session(con, sid, base_id)
    _ = researcher_id  # silence unused
    return participant_id, sid


class TestRoutingPredicate:
    def test_false_when_no_session(self):
        # Just a regular user, no participant session.
        con = get_registry().platform_con()
        uid = pdb.create_actor(
            con,
            kind="user",
            email="u@example.com",
            display_name="U",
            password_hash=auth.hash_password("pw"),
        )
        assert _is_baseline_for_actor_and_base(uid, "any-base") is False

    def test_false_when_session_is_elenchus_condition(self):
        actor_id, _ = _make_participant_with_session(condition="elenchus", base_id="alpha")
        assert _is_baseline_for_actor_and_base(actor_id, "alpha") is False

    def test_true_when_session_is_baseline_and_base_matches(self):
        actor_id, _ = _make_participant_with_session(condition="baseline", base_id="alpha")
        assert _is_baseline_for_actor_and_base(actor_id, "alpha") is True

    def test_false_when_session_bound_to_different_base(self):
        actor_id, _ = _make_participant_with_session(condition="baseline", base_id="alpha")
        assert _is_baseline_for_actor_and_base(actor_id, "beta") is False

    def test_false_when_session_has_no_base_yet(self):
        # Session created but base_id never attached (briefing/tutorial
        # phase). The participant can't be in baseline-message mode yet.
        actor_id, _ = _make_participant_with_session(condition="baseline", base_id=None)
        assert _is_baseline_for_actor_and_base(actor_id, "any-base") is False


# ─── End-to-end: message route dispatches based on condition ─────────


class TestMessageRouteDispatch:
    """The route runs `_is_baseline_for_actor_and_base` and picks the
    right opponent method. We patch both opponent methods to record
    which one was called."""

    def _setup_participant_base(self, condition: str) -> tuple[TestClient, str]:
        """Create a participant + their base + log them in via the
        platform's session-cookie machinery. Returns (test_client,
        base_name)."""
        actor_id, _ = _make_participant_with_session(
            condition=condition, base_id="participant-base"
        )
        c = TestClient(app)
        c.cookies.set(auth.SESSION_COOKIE, auth.create_session(actor_id))
        # Make sure the base file actually exists on disk (the registry
        # opens it lazily on first access).
        reg = get_registry()
        path = reg.db_path("participant-base", actor_id=actor_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        DialecticalState.create(path, "participant-base").base.con.close()
        return c, "participant-base"

    def test_baseline_session_routes_to_baseline_respond(self):
        c, base = self._setup_participant_base("baseline")

        mock_baseline = AsyncMock(
            return_value={"response": "tool-mode reply", "speech_acts": [], "new_tensions": []}
        )
        mock_dialectic = AsyncMock(
            return_value={"response": "dialectic reply", "speech_acts": [], "new_tensions": []}
        )
        with (
            patch("elenchus.server.opponent.async_baseline_respond", new=mock_baseline),
            patch("elenchus.server.opponent.async_respond", new=mock_dialectic),
        ):
            r = c.post(f"/api/dialectics/{base}/message", json={"message": "hi"})
        assert r.status_code == 200, r.text
        assert r.json()["response"] == "tool-mode reply"
        assert r.json()["condition"] == "baseline"
        assert mock_baseline.called
        assert not mock_dialectic.called

    def test_elenchus_session_routes_to_dialectic(self):
        c, base = self._setup_participant_base("elenchus")
        mock_baseline = AsyncMock(
            return_value={"response": "BAD", "speech_acts": [], "new_tensions": []}
        )
        mock_dialectic = AsyncMock(
            return_value={"response": "dialectic reply", "speech_acts": [], "new_tensions": []}
        )
        with (
            patch("elenchus.server.opponent.async_baseline_respond", new=mock_baseline),
            patch("elenchus.server.opponent.async_respond", new=mock_dialectic),
        ):
            r = c.post(f"/api/dialectics/{base}/message", json={"message": "hi"})
        assert r.status_code == 200
        assert r.json()["response"] == "dialectic reply"
        assert r.json()["condition"] == "elenchus"
        assert mock_dialectic.called
        assert not mock_baseline.called

    def test_regular_user_always_dialectic(self):
        """A non-participant user (no study session at all) gets the
        dialectic path even if they share a base name with somebody."""
        # Regular user with their own base.
        con = get_registry().platform_con()
        uid = pdb.create_actor(
            con,
            kind="user",
            email="u@example.com",
            display_name="U",
            password_hash=auth.hash_password("pw"),
        )
        c = TestClient(app)
        c.cookies.set(auth.SESSION_COOKIE, auth.create_session(uid))
        c.post("/api/dialectics", json={"name": "mine"})

        mock_baseline = AsyncMock(
            return_value={"response": "BAD", "speech_acts": [], "new_tensions": []}
        )
        mock_dialectic = AsyncMock(
            return_value={"response": "ok", "speech_acts": [], "new_tensions": []}
        )
        with (
            patch("elenchus.server.opponent.async_baseline_respond", new=mock_baseline),
            patch("elenchus.server.opponent.async_respond", new=mock_dialectic),
        ):
            r = c.post("/api/dialectics/mine/message", json={"message": "hi"})
        assert r.status_code == 200
        assert r.json()["condition"] == "elenchus"
        assert mock_dialectic.called
        assert not mock_baseline.called
