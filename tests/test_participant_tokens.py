"""Tests for Phase D/1 — participant session tokens.

Three slices:
  1. Platform DB helpers — schema, single-use semantics, scheduled
     window, void path.
  2. Researcher admin endpoints — issue, list, void; role gating.
  3. Public consumption endpoint — token → session cookie, idempotent
     replays, structured error body for non-consumable tokens.
"""

from __future__ import annotations

import contextlib
import os
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from elenchus import auth
from elenchus.db import get_registry
from elenchus.db import platform as pdb
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


def _create_actor(kind: str, email: str | None = None) -> int:
    """Create an actor of the given kind. Use kind='admin' or
    'researcher' to drive role-gated routes."""
    con = get_registry().platform_con()
    return pdb.create_actor(
        con,
        kind=kind,
        email=email or f"{kind}@example.com",
        display_name=kind.title(),
        password_hash=auth.hash_password("pw"),
    )


def _login_as(kind: str) -> int:
    """Find-or-create an actor of `kind` and set the test client's
    session cookie. Idempotent across calls in the same test so
    tests that re-authenticate after a logout don't trip the
    unique-email constraint."""
    con = get_registry().platform_con()
    email = f"{kind}@example.com"
    existing = pdb.find_actor_by_email(con, email)
    actor_id = existing["id"] if existing else _create_actor(kind, email=email)
    client.cookies.set(auth.SESSION_COOKIE, auth.create_session(actor_id))
    return actor_id


# ─── Platform DB helpers ─────────────────────────────────────────────


class TestParticipantTokenCRUD:
    def test_create_and_find(self):
        con = get_registry().platform_con()
        researcher_id = _create_actor("researcher")
        participant_id = pdb.create_actor(
            con, kind="participant", email=None, display_name="P1", password_hash=None
        )
        pdb.create_participant_token(
            con,
            token="tok-1",
            actor_id=participant_id,
            study_id="STUDY-A",
            condition="elenchus",
            issued_by=researcher_id,
            notes="pilot run",
        )

        row = pdb.find_participant_token(con, "tok-1")
        assert row is not None
        assert row["actor_id"] == participant_id
        assert row["study_id"] == "STUDY-A"
        assert row["condition"] == "elenchus"
        assert row["status"] == "scheduled"
        assert row["used_at"] is None
        assert row["notes"] == "pilot run"

    def test_find_unknown_returns_none(self):
        assert pdb.find_participant_token(get_registry().platform_con(), "nope") is None

    def test_consume_marks_active_and_returns_row(self):
        con = get_registry().platform_con()
        r = _create_actor("researcher")
        p = pdb.create_actor(
            con, kind="participant", email=None, display_name="P", password_hash=None
        )
        pdb.create_participant_token(
            con,
            token="t",
            actor_id=p,
            study_id="S",
            condition="elenchus",
            issued_by=r,
        )
        consumed = pdb.consume_participant_token(con, "t")
        assert consumed is not None
        assert consumed["status"] == "active"
        assert consumed["used_at"] is not None

    def test_consume_is_single_use(self):
        con = get_registry().platform_con()
        r = _create_actor("researcher")
        p = pdb.create_actor(
            con, kind="participant", email=None, display_name="P", password_hash=None
        )
        pdb.create_participant_token(
            con, token="t", actor_id=p, study_id="S", condition="elenchus", issued_by=r
        )
        first = pdb.consume_participant_token(con, "t")
        second = pdb.consume_participant_token(con, "t")
        assert first is not None
        assert second is None

    def test_consume_respects_scheduled_window(self):
        con = get_registry().platform_con()
        r = _create_actor("researcher")
        p = pdb.create_actor(
            con, kind="participant", email=None, display_name="P", password_hash=None
        )
        # Window in the future.
        future = (datetime.now() + timedelta(days=1)).isoformat()
        far_future = (datetime.now() + timedelta(days=2)).isoformat()
        pdb.create_participant_token(
            con,
            token="future-t",
            actor_id=p,
            study_id="S",
            condition="elenchus",
            issued_by=r,
            scheduled_start=future,
            scheduled_end=far_future,
        )
        # Can't consume yet.
        assert pdb.consume_participant_token(con, "future-t") is None
        # And it's still in 'scheduled' status.
        assert pdb.find_participant_token(con, "future-t")["status"] == "scheduled"

    def test_consume_respects_expiry(self):
        con = get_registry().platform_con()
        r = _create_actor("researcher")
        p = pdb.create_actor(
            con, kind="participant", email=None, display_name="P", password_hash=None
        )
        past_start = (datetime.now() - timedelta(days=2)).isoformat()
        past_end = (datetime.now() - timedelta(days=1)).isoformat()
        pdb.create_participant_token(
            con,
            token="expired-t",
            actor_id=p,
            study_id="S",
            condition="elenchus",
            issued_by=r,
            scheduled_start=past_start,
            scheduled_end=past_end,
        )
        assert pdb.consume_participant_token(con, "expired-t") is None

    def test_void_only_scheduled_tokens(self):
        con = get_registry().platform_con()
        r = _create_actor("researcher")
        p = pdb.create_actor(
            con, kind="participant", email=None, display_name="P", password_hash=None
        )
        pdb.create_participant_token(
            con, token="t", actor_id=p, study_id="S", condition="elenchus", issued_by=r
        )
        assert pdb.void_participant_token(con, "t") is True
        assert pdb.find_participant_token(con, "t")["status"] == "voided"
        # Already voided → False.
        assert pdb.void_participant_token(con, "t") is False
        # Consuming a voided token is impossible.
        assert pdb.consume_participant_token(con, "t") is None

    def test_void_after_use_is_idempotent_false(self):
        con = get_registry().platform_con()
        r = _create_actor("researcher")
        p = pdb.create_actor(
            con, kind="participant", email=None, display_name="P", password_hash=None
        )
        pdb.create_participant_token(
            con, token="t", actor_id=p, study_id="S", condition="elenchus", issued_by=r
        )
        pdb.consume_participant_token(con, "t")
        # Token is now 'active' — can't be voided.
        assert pdb.void_participant_token(con, "t") is False

    def test_list_filtered_by_study(self):
        con = get_registry().platform_con()
        r = _create_actor("researcher")
        p = pdb.create_actor(
            con, kind="participant", email=None, display_name="P", password_hash=None
        )
        pdb.create_participant_token(
            con, token="a", actor_id=p, study_id="A", condition="elenchus", issued_by=r
        )
        pdb.create_participant_token(
            con, token="b", actor_id=p, study_id="B", condition="baseline", issued_by=r
        )
        a_only = pdb.list_participant_tokens(con, study_id="A")
        assert [t["token"] for t in a_only] == ["a"]

    def test_list_filtered_by_condition(self):
        con = get_registry().platform_con()
        r = _create_actor("researcher")
        p = pdb.create_actor(
            con, kind="participant", email=None, display_name="P", password_hash=None
        )
        pdb.create_participant_token(
            con, token="e", actor_id=p, study_id="S", condition="elenchus", issued_by=r
        )
        pdb.create_participant_token(
            con, token="b", actor_id=p, study_id="S", condition="baseline", issued_by=r
        )
        baseline_only = pdb.list_participant_tokens(con, condition="baseline")
        assert [t["token"] for t in baseline_only] == ["b"]


# ─── auth.require_researcher ─────────────────────────────────────────


class TestRequireResearcher:
    def test_admin_passes(self):
        _login_as("admin")
        r = client.get("/api/admin/study/tokens")
        assert r.status_code == 200

    def test_researcher_passes(self):
        _login_as("researcher")
        r = client.get("/api/admin/study/tokens")
        assert r.status_code == 200

    def test_regular_user_forbidden(self):
        _login_as("user")
        r = client.get("/api/admin/study/tokens")
        assert r.status_code == 403

    def test_unauthenticated_unauthorized(self):
        r = client.get("/api/admin/study/tokens")
        assert r.status_code == 401


# ─── Admin endpoints — issue / list / void ───────────────────────────


class TestAdminIssue:
    def test_issues_token_and_creates_participant_actor(self):
        researcher_id = _login_as("researcher")
        r = client.post(
            "/api/admin/study/tokens",
            json={
                "study_id": "PILOT-1",
                "condition": "elenchus",
                "display_name": "P-001",
            },
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["study_id"] == "PILOT-1"
        assert data["condition"] == "elenchus"
        assert isinstance(data["token"], str) and len(data["token"]) > 16
        assert data["participant_actor_id"] > 0

        # The participant actor exists with kind='participant'.
        con = get_registry().platform_con()
        actor = pdb.find_actor_by_id(con, data["participant_actor_id"])
        assert actor["kind"] == "participant"
        assert actor["display_name"] == "P-001"
        assert actor["email"] is None
        # And the token is stored.
        row = pdb.find_participant_token(con, data["token"])
        assert row["issued_by"] == researcher_id

    def test_invalid_condition_rejected(self):
        _login_as("researcher")
        r = client.post(
            "/api/admin/study/tokens",
            json={"study_id": "S", "condition": "control", "display_name": "P"},
        )
        assert r.status_code == 400

    def test_missing_study_id_rejected(self):
        _login_as("researcher")
        r = client.post(
            "/api/admin/study/tokens",
            json={"study_id": "  ", "condition": "elenchus", "display_name": "P"},
        )
        assert r.status_code == 400

    def test_within_subjects_pair(self):
        """Same participant gets two tokens, one per condition. Each
        creates its own participant actor — which is the right design
        because anonymous cross-condition linkage stays the researcher's
        responsibility, not platform metadata."""
        _login_as("researcher")
        r1 = client.post(
            "/api/admin/study/tokens",
            json={"study_id": "S", "condition": "elenchus", "display_name": "P-1"},
        ).json()
        r2 = client.post(
            "/api/admin/study/tokens",
            json={"study_id": "S", "condition": "baseline", "display_name": "P-1"},
        ).json()
        assert r1["token"] != r2["token"]
        # Two separate actors (the display_name match is the researcher's
        # bookkeeping, not the platform's identity model).
        assert r1["participant_actor_id"] != r2["participant_actor_id"]


class TestAdminList:
    def test_returns_issued_tokens_newest_first(self):
        _login_as("researcher")
        for cond in ("elenchus", "baseline"):
            client.post(
                "/api/admin/study/tokens",
                json={"study_id": "S", "condition": cond, "display_name": "P"},
            )
        r = client.get("/api/admin/study/tokens")
        assert r.status_code == 200
        tokens = r.json()["tokens"]
        assert len(tokens) == 2

    def test_filters_by_study(self):
        _login_as("researcher")
        client.post(
            "/api/admin/study/tokens",
            json={"study_id": "A", "condition": "elenchus", "display_name": "P"},
        )
        client.post(
            "/api/admin/study/tokens",
            json={"study_id": "B", "condition": "elenchus", "display_name": "P"},
        )
        r = client.get("/api/admin/study/tokens?study_id=A")
        tokens = r.json()["tokens"]
        assert len(tokens) == 1
        assert tokens[0]["study_id"] == "A"


class TestAdminVoid:
    def test_voids_scheduled_token(self):
        _login_as("researcher")
        token = client.post(
            "/api/admin/study/tokens",
            json={"study_id": "S", "condition": "elenchus", "display_name": "P"},
        ).json()["token"]
        r = client.delete(f"/api/admin/study/tokens/{token}")
        assert r.status_code == 200
        assert r.json()["status"] == "voided"

    def test_void_used_token_404(self):
        _login_as("researcher")
        token = client.post(
            "/api/admin/study/tokens",
            json={"study_id": "S", "condition": "elenchus", "display_name": "P"},
        ).json()["token"]
        # Consume from a fresh client (the participant flow).
        c = TestClient(app)
        c.post(f"/api/study/{token}")
        r = client.delete(f"/api/admin/study/tokens/{token}")
        assert r.status_code == 404


# ─── Public consumption endpoint ─────────────────────────────────────


class TestPublicConsumption:
    def _issue(self) -> str:
        _login_as("researcher")
        token = client.post(
            "/api/admin/study/tokens",
            json={"study_id": "S", "condition": "elenchus", "display_name": "P"},
        ).json()["token"]
        client.cookies.clear()  # leave researcher's cookie behind
        return token

    def test_consumption_returns_session_cookie(self):
        token = self._issue()
        c = TestClient(app)
        r = c.post(f"/api/study/{token}")
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["study_id"] == "S"
        assert data["condition"] == "elenchus"
        assert data["actor_id"] > 0
        # Cookie set.
        assert auth.SESSION_COOKIE in c.cookies
        # The cookie is for a 'participant' actor.
        me = c.get("/api/auth/me").json()
        assert me["kind"] == "participant"
        assert me["id"] == data["actor_id"]

    def test_second_use_resumes_live_session(self):
        """The link doubles as a resume link: a fresh client (new device /
        lost cookie) re-clicking the token while the session is live gets a
        working cookie back and is routed to the same live session — not a
        410, and not a new session."""
        token = self._issue()
        c1 = TestClient(app)
        r1 = c1.post(f"/api/study/{token}")
        assert r1.status_code == 200
        assert r1.json().get("resumed") is not True  # first use is a consume

        c2 = TestClient(app)
        r2 = c2.post(f"/api/study/{token}")
        assert r2.status_code == 200, r2.text
        data = r2.json()
        assert data["resumed"] is True
        assert data["session_id"] == r1.json()["session_id"]  # same session
        assert data["actor_id"] == r1.json()["actor_id"]
        assert data["state"] == "briefing"
        # The resumed client holds a working participant cookie.
        assert auth.SESSION_COOKIE in c2.cookies
        me = c2.get("/api/auth/me").json()
        assert me["kind"] == "participant" and me["id"] == data["actor_id"]

    def test_resume_blocked_after_session_terminal(self):
        """Once the session reaches a terminal state, the link stops
        resuming and returns 410 — the participant is done."""
        token = self._issue()
        c1 = TestClient(app)
        sid = c1.post(f"/api/study/{token}").json()["session_id"]
        # Simulate completion (bypass the state machine for the fixture).
        reg = get_registry()
        with reg.platform_lock:
            reg.platform_con().execute(
                "UPDATE sessions SET state = 'complete' WHERE id = ?", [sid]
            )
        r = TestClient(app).post(f"/api/study/{token}")
        assert r.status_code == 410
        detail = r.json()["detail"]
        assert detail["status"] == "active"

    def test_unknown_token_404(self):
        r = TestClient(app).post("/api/study/totally-not-a-real-token")
        assert r.status_code == 404

    def test_voided_token_returns_structured_410(self):
        token = self._issue()
        # Researcher voids it.
        _login_as("researcher")
        client.delete(f"/api/admin/study/tokens/{token}")
        client.cookies.clear()

        r = TestClient(app).post(f"/api/study/{token}")
        assert r.status_code == 410
        detail = r.json()["detail"]
        assert detail["status"] == "voided"
        assert "cancel" in detail["user_message"].lower()

    def test_future_window_returns_410(self):
        """Token whose scheduled_start is in the future can't be used."""
        # Bypass the researcher endpoint to set a future window directly.
        con = get_registry().platform_con()
        researcher = _create_actor("researcher")
        participant = pdb.create_actor(
            con, kind="participant", email=None, display_name="P", password_hash=None
        )
        future = (datetime.now() + timedelta(days=1)).isoformat()
        pdb.create_participant_token(
            con,
            token="future-t",
            actor_id=participant,
            study_id="S",
            condition="elenchus",
            issued_by=researcher,
            scheduled_start=future,
        )

        r = TestClient(app).post("/api/study/future-t")
        assert r.status_code == 410
        detail = r.json()["detail"]
        # Still in 'scheduled' status — the window check failed but
        # the token itself isn't used / voided / expired.
        assert detail["status"] == "scheduled"
