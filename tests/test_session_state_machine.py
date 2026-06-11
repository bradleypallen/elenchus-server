"""Tests for Phase D/2 — participant session state machine.

Three slices:
  1. `study_flow` — pure state machine: enum, transitions, terminal /
     live partitions.
  2. Platform DB helpers — create/find/advance, transition guard,
     auto-close on terminal, attach_base.
  3. HTTP routes — token consumption opens a briefing session;
     `GET /api/study/session` and `POST /api/study/session/advance`
     drive the participant through the flow.
"""

from __future__ import annotations

import contextlib
import os

import pytest
from fastapi.testclient import TestClient

from elenchus import auth, study_flow
from elenchus.db import get_registry
from elenchus.db import platform as pdb
from elenchus.server import app
from elenchus.study_flow import (
    ALLOWED_TRANSITIONS,
    LIVE_STATES,
    TERMINAL_STATES,
    SessionState,
    assert_transition,
    can_transition,
    is_live,
    is_terminal,
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


def _create_researcher() -> int:
    con = get_registry().platform_con()
    rid = pdb.create_actor(
        con,
        kind="researcher",
        email="r@example.com",
        display_name="R",
        password_hash=auth.hash_password("pw"),
    )
    client.cookies.set(auth.SESSION_COOKIE, auth.create_session(rid))
    return rid


def _issue_and_consume_token(condition: str = "elenchus") -> tuple[TestClient, dict]:
    """Returns (participant_client, consumed_body)."""
    _create_researcher()
    r = client.post(
        "/api/admin/study/tokens",
        json={"study_id": "S", "condition": condition, "display_name": "P"},
    )
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    client.cookies.clear()

    pclient = TestClient(app)
    r = pclient.post(f"/api/study/{token}")
    assert r.status_code == 200, r.text
    return pclient, r.json()


# ─── study_flow pure module ──────────────────────────────────────────


class TestStateMachineShape:
    def test_eight_states(self):
        # 6 progress + 2 terminal alternatives = 8.
        assert len(list(SessionState)) == 8

    def test_terminal_partition(self):
        assert {
            SessionState.COMPLETE,
            SessionState.EXPIRED,
            SessionState.INTERRUPTED,
        } == TERMINAL_STATES

    def test_live_partition(self):
        # Live = everything that isn't terminal.
        assert {
            SessionState.BRIEFING,
            SessionState.TUTORIAL,
            SessionState.ACTIVE,
            SessionState.POST_SESSION,
            SessionState.SURVEYED,
        } == LIVE_STATES

    def test_partitions_dont_overlap(self):
        assert set() == LIVE_STATES & TERMINAL_STATES

    def test_partitions_cover_all_states(self):
        assert set(SessionState) == LIVE_STATES | TERMINAL_STATES


class TestAllowedTransitions:
    def test_canonical_happy_path(self):
        chain = [
            SessionState.BRIEFING,
            SessionState.TUTORIAL,
            SessionState.ACTIVE,
            SessionState.POST_SESSION,
            SessionState.SURVEYED,
            SessionState.COMPLETE,
        ]
        for src, dst in zip(chain, chain[1:], strict=False):
            assert can_transition(src, dst), f"{src} → {dst} should be allowed"

    def test_interrupted_reachable_from_every_live_state(self):
        for src in LIVE_STATES:
            assert can_transition(src, SessionState.INTERRUPTED)

    def test_expired_reachable_after_briefing(self):
        # Can expire from tutorial/active/post_session (timed states)
        # but NOT from briefing (consent — pre-timer) or surveyed
        # (post-task — already on the post-session homestretch).
        assert can_transition(SessionState.TUTORIAL, SessionState.EXPIRED)
        assert can_transition(SessionState.ACTIVE, SessionState.EXPIRED)
        assert can_transition(SessionState.POST_SESSION, SessionState.EXPIRED)
        assert not can_transition(SessionState.BRIEFING, SessionState.EXPIRED)
        assert not can_transition(SessionState.SURVEYED, SessionState.EXPIRED)

    def test_no_backward_transitions(self):
        forward_order = [
            SessionState.BRIEFING,
            SessionState.TUTORIAL,
            SessionState.ACTIVE,
            SessionState.POST_SESSION,
            SessionState.SURVEYED,
            SessionState.COMPLETE,
        ]
        # For every pair where i > j, the i→j direction must be blocked.
        for i, src in enumerate(forward_order):
            for j, dst in enumerate(forward_order):
                if i > j and src != dst:
                    assert not can_transition(src, dst), (
                        f"backward transition {src} → {dst} must be blocked"
                    )

    def test_terminal_states_have_no_outgoing_edges(self):
        for state in TERMINAL_STATES:
            assert ALLOWED_TRANSITIONS.get(state, frozenset()) == frozenset()

    def test_self_loop_blocked(self):
        for state in SessionState:
            assert not can_transition(state, state)

    def test_assert_transition_passes_on_valid(self):
        assert_transition(SessionState.TUTORIAL, SessionState.ACTIVE)  # no raise

    def test_assert_transition_raises_on_invalid(self):
        with pytest.raises(ValueError, match="Invalid"):
            assert_transition(SessionState.BRIEFING, SessionState.COMPLETE)

    def test_is_terminal_predicates(self):
        assert is_terminal(SessionState.COMPLETE)
        assert is_terminal(SessionState.EXPIRED)
        assert is_terminal(SessionState.INTERRUPTED)
        assert not is_terminal(SessionState.ACTIVE)

    def test_is_live_predicates(self):
        assert is_live(SessionState.BRIEFING)
        assert is_live(SessionState.ACTIVE)
        assert not is_live(SessionState.COMPLETE)


# ─── Platform DB helpers ─────────────────────────────────────────────


class TestStudySessionCRUD:
    def test_create_in_briefing_with_null_base(self):
        con = get_registry().platform_con()
        actor_id = pdb.create_actor(
            con, kind="participant", email=None, display_name="P", password_hash=None
        )
        sid = pdb.create_study_session(
            con,
            actor_id=actor_id,
            study_token="tok-1",
            condition="elenchus",
            initial_state="briefing",
        )
        row = pdb.find_study_session(con, sid)
        assert row["state"] == "briefing"
        assert row["base_id"] is None
        assert row["study_token"] == "tok-1"
        assert row["condition"] == "elenchus"

    def test_find_live_returns_briefing_or_later(self):
        con = get_registry().platform_con()
        actor_id = pdb.create_actor(
            con, kind="participant", email=None, display_name="P", password_hash=None
        )
        pdb.create_study_session(
            con,
            actor_id=actor_id,
            study_token="t",
            condition="elenchus",
            initial_state="briefing",
        )
        live = pdb.find_live_session_for_actor(con, actor_id)
        assert live is not None
        assert live["state"] == "briefing"

    def test_find_live_skips_terminal_sessions(self):
        con = get_registry().platform_con()
        actor_id = pdb.create_actor(
            con, kind="participant", email=None, display_name="P", password_hash=None
        )
        sid = pdb.create_study_session(
            con,
            actor_id=actor_id,
            study_token="t",
            condition="elenchus",
            initial_state="briefing",
        )
        # Walk the session to complete.
        for to_state in ("tutorial", "active", "post_session", "surveyed", "complete"):
            pdb.advance_session_state(con, sid, to_state)
        assert pdb.find_live_session_for_actor(con, actor_id) is None

    def test_advance_with_valid_transition(self):
        con = get_registry().platform_con()
        actor_id = pdb.create_actor(
            con, kind="participant", email=None, display_name="P", password_hash=None
        )
        sid = pdb.create_study_session(
            con,
            actor_id=actor_id,
            study_token="t",
            condition="elenchus",
            initial_state="briefing",
        )
        updated = pdb.advance_session_state(con, sid, "tutorial")
        assert updated is not None
        assert updated["state"] == "tutorial"

    def test_advance_blocks_invalid_transition(self):
        con = get_registry().platform_con()
        actor_id = pdb.create_actor(
            con, kind="participant", email=None, display_name="P", password_hash=None
        )
        sid = pdb.create_study_session(
            con,
            actor_id=actor_id,
            study_token="t",
            condition="elenchus",
            initial_state="briefing",
        )
        # briefing → complete is two steps forward — not allowed.
        assert pdb.advance_session_state(con, sid, "complete") is None
        # Session is still in briefing.
        assert pdb.find_study_session(con, sid)["state"] == "briefing"

    def test_advance_to_terminal_also_closes_session(self):
        con = get_registry().platform_con()
        actor_id = pdb.create_actor(
            con, kind="participant", email=None, display_name="P", password_hash=None
        )
        sid = pdb.create_study_session(
            con,
            actor_id=actor_id,
            study_token="t",
            condition="elenchus",
            initial_state="briefing",
        )
        # Jump straight to interrupted (allowed from briefing).
        updated = pdb.advance_session_state(con, sid, "interrupted")
        assert updated["state"] == "interrupted"
        assert updated["status"] == "closed"
        assert updated["closed_at"] is not None

    def test_advance_unknown_state_returns_none(self):
        con = get_registry().platform_con()
        actor_id = pdb.create_actor(
            con, kind="participant", email=None, display_name="P", password_hash=None
        )
        sid = pdb.create_study_session(
            con,
            actor_id=actor_id,
            study_token="t",
            condition="elenchus",
            initial_state="briefing",
        )
        assert pdb.advance_session_state(con, sid, "totally-fake-state") is None

    def test_attach_base_only_once(self):
        con = get_registry().platform_con()
        actor_id = pdb.create_actor(
            con, kind="participant", email=None, display_name="P", password_hash=None
        )
        # Need a real `bases` row for the FK.
        pdb.create_base(con, base_id="b-1", name="study task", owner_id=actor_id)
        sid = pdb.create_study_session(
            con,
            actor_id=actor_id,
            study_token="t",
            condition="elenchus",
            initial_state="briefing",
        )
        assert pdb.attach_base_to_session(con, sid, "b-1") is True
        # Second attach is a no-op (idempotent return False since the
        # WHERE clause guards on base_id IS NULL).
        assert pdb.attach_base_to_session(con, sid, "b-2") is False
        assert pdb.find_study_session(con, sid)["base_id"] == "b-1"


# ─── End-to-end: token consumption opens a briefing session ─────────


class TestTokenConsumptionOpensBriefingSession:
    def test_session_id_in_response(self):
        _, body = _issue_and_consume_token()
        assert "session_id" in body
        assert body["state"] == "briefing"
        assert body["session_id"] > 0

    def test_session_visible_via_current_endpoint(self):
        pclient, body = _issue_and_consume_token(condition="baseline")
        r = pclient.get("/api/study/session")
        assert r.status_code == 200
        s = r.json()
        assert s["id"] == body["session_id"]
        assert s["state"] == "briefing"
        assert s["condition"] == "baseline"
        assert s["study_token"] is not None

    def test_token_row_links_back_to_session(self):
        """The token's session_id column is set on consumption so the
        researcher dashboard can join tokens → sessions → reports
        without a separate session-list endpoint."""
        _, body = _issue_and_consume_token()
        con = get_registry().platform_con()
        token_row = pdb.find_participant_token(con, body.get("study_token") or "")
        # study_token isn't in the consume response; find it via the
        # session instead.
        session = pdb.find_study_session(con, body["session_id"])
        linked = pdb.find_participant_token(con, session["study_token"])
        assert linked["session_id"] == body["session_id"]
        _ = token_row  # the by-token lookup above is a no-op guard


# ─── HTTP routes: GET + advance ──────────────────────────────────────


class TestStudySessionRoutes:
    def test_current_404_when_no_session(self):
        # Log in as a non-participant.
        con = get_registry().platform_con()
        actor_id = pdb.create_actor(
            con,
            kind="user",
            email="u@example.com",
            display_name="U",
            password_hash=auth.hash_password("pw"),
        )
        c = TestClient(app)
        c.cookies.set(auth.SESSION_COOKIE, auth.create_session(actor_id))
        r = c.get("/api/study/session")
        assert r.status_code == 404

    def test_current_unauthenticated_401(self):
        r = client.get("/api/study/session")
        assert r.status_code == 401

    def test_advance_briefing_to_tutorial(self):
        pclient, _ = _issue_and_consume_token()
        r = pclient.post("/api/study/session/advance", json={"to_state": "tutorial"})
        assert r.status_code == 200, r.text
        assert r.json()["state"] == "tutorial"
        # GET reflects the change.
        assert pclient.get("/api/study/session").json()["state"] == "tutorial"

    def test_advance_rejects_invalid_transition(self):
        pclient, _ = _issue_and_consume_token()
        r = pclient.post("/api/study/session/advance", json={"to_state": "complete"})
        assert r.status_code == 400
        detail = r.json()["detail"]
        assert detail["current_state"] == "briefing"
        assert detail["requested_state"] == "complete"
        assert "user_message" in detail

    def test_full_happy_path(self):
        pclient, _ = _issue_and_consume_token()
        for to_state in ("tutorial", "active", "post_session", "surveyed", "complete"):
            r = pclient.post("/api/study/session/advance", json={"to_state": to_state})
            assert r.status_code == 200, f"failed at {to_state}: {r.text}"
            assert r.json()["state"] == to_state
        # After complete, the session is no longer live — GET returns 404.
        assert pclient.get("/api/study/session").status_code == 404

    def test_advance_interrupted_from_briefing(self):
        pclient, _ = _issue_and_consume_token()
        r = pclient.post("/api/study/session/advance", json={"to_state": "interrupted"})
        assert r.status_code == 200
        assert r.json()["state"] == "interrupted"
        assert pclient.get("/api/study/session").status_code == 404
        _ = study_flow  # silence unused import in some test runs


# ─── Phase D/4: begin-tutorial / begin-task transitions ──────────────


class TestBeginTutorial:
    def test_creates_practice_base_and_advances(self):
        pclient, body = _issue_and_consume_token()
        r = pclient.post("/api/study/session/begin-tutorial")
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["state"] == "tutorial"
        assert data["practice_base_id"] == f"practice-{body['session_id']}"
        # The base is registered and owned by the participant.
        con = get_registry().platform_con()
        base = pdb.find_base(con, data["practice_base_id"])
        assert base is not None
        assert base["owner_id"] == body["actor_id"]
        # The practice base is NOT attached to the session.
        session = pdb.find_study_session(con, body["session_id"])
        assert session["base_id"] is None

    def test_participant_can_message_practice_base(self):
        """The practice base is a regular owned base — the message
        route's authorization accepts it."""
        pclient, body = _issue_and_consume_token()
        pclient.post("/api/study/session/begin-tutorial")
        practice = f"practice-{body['session_id']}"
        r = pclient.get(f"/api/dialectics/{practice}")
        assert r.status_code == 200
        assert r.json()["name"] == "Practice: kinds of pets"

    def test_rejected_outside_briefing(self):
        pclient, _ = _issue_and_consume_token()
        pclient.post("/api/study/session/begin-tutorial")
        # Second call: session is now in tutorial → 400.
        r = pclient.post("/api/study/session/begin-tutorial")
        assert r.status_code == 400

    def test_404_without_session(self):
        con = get_registry().platform_con()
        uid = pdb.create_actor(
            con,
            kind="user",
            email="plain@example.com",
            display_name="U",
            password_hash=auth.hash_password("pw"),
        )
        c = TestClient(app)
        c.cookies.set(auth.SESSION_COOKIE, auth.create_session(uid))
        r = c.post("/api/study/session/begin-tutorial")
        assert r.status_code == 404


class TestBeginTask:
    def test_creates_task_base_attaches_and_advances(self):
        pclient, body = _issue_and_consume_token()
        pclient.post("/api/study/session/begin-tutorial")
        r = pclient.post("/api/study/session/begin-task")
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["state"] == "active"
        task_base = f"task-{body['session_id']}"
        assert data["task_base_id"] == task_base
        # Task base IS attached to the session (export + routing key).
        session = pdb.find_study_session(get_registry().platform_con(), body["session_id"])
        assert session["base_id"] == task_base

    def test_rejected_outside_tutorial(self):
        pclient, _ = _issue_and_consume_token()
        # Still in briefing.
        r = pclient.post("/api/study/session/begin-task")
        assert r.status_code == 400


class TestPracticeBaseBaselineRouting:
    def test_baseline_participant_practice_base_routes_to_baseline(self):
        """Baseline-condition participants must practice in their
        actual condition — the routing predicate recognizes the
        practice-base naming convention even though only the task
        base is attached to the session."""
        from elenchus.server import _is_baseline_for_actor_and_base

        pclient, body = _issue_and_consume_token(condition="baseline")
        pclient.post("/api/study/session/begin-tutorial")
        practice = f"practice-{body['session_id']}"
        assert _is_baseline_for_actor_and_base(body["actor_id"], practice) is True
        # A random other base still routes to the dialectic.
        assert _is_baseline_for_actor_and_base(body["actor_id"], "unrelated") is False

    def test_elenchus_participant_practice_base_routes_to_dialectic(self):
        from elenchus.server import _is_baseline_for_actor_and_base

        pclient, body = _issue_and_consume_token(condition="elenchus")
        pclient.post("/api/study/session/begin-tutorial")
        practice = f"practice-{body['session_id']}"
        assert _is_baseline_for_actor_and_base(body["actor_id"], practice) is False
