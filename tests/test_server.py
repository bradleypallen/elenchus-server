"""Tests for server.py — FastAPI API endpoints."""

import contextlib
import logging
import os
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

# Conftest sets ELENCHUS_DATA, ELENCHUS_API_KEY, BCRYPT_ROUNDS before
# this module imports `elenchus.server`. Sharing the data dir across
# test files ensures the registry's data_dir matches what every test
# file's fixture writes to / cleans up.
from elenchus import auth
from elenchus.db import get_registry
from elenchus.db import platform as pdb
from elenchus.server import app

logger = logging.getLogger(__name__)

_test_data_dir = os.environ["ELENCHUS_DATA"]

client = TestClient(app)


def _close_per_base_handles(reg):
    """Close every per-base connection cached in the registry without
    touching the platform connection. Used between tests to release
    file locks on per-base .duckdb files."""
    for _name, handle in list(reg._handles.items()):
        with contextlib.suppress(Exception):
            handle.state.base.con.close()
    reg._handles.clear()


@pytest.fixture(autouse=True)
def _clean_states():
    """Clean up state, log in as a default test user, then tear down.

    All routes now require auth; the fixture creates a fresh user per
    test and sets the session cookie on the shared TestClient.
    """
    reg = get_registry()
    reg.migrate_platform()
    con = reg.platform_con()

    # Wipe platform tables. Keep the platform connection open — closing
    # it here would invalidate any subsequent `con.execute` in this
    # fixture.
    with reg.platform_lock:
        for table in ("auth_sessions", "magic_links", "invites", "sessions", "bases", "actors"):
            con.execute(f"DELETE FROM {table}")
    _close_per_base_handles(reg)
    client.cookies.clear()

    # Create a default test user and log them in.
    actor_id = pdb.create_actor(
        con,
        kind="user",
        email="testuser@example.com",
        display_name="Test User",
        password_hash=auth.hash_password("test-pw"),
    )
    token = auth.create_session(actor_id)
    client.cookies.set(auth.SESSION_COOKIE, token)

    yield {"actor_id": actor_id, "token": token}

    client.cookies.clear()
    _close_per_base_handles(reg)
    # Clean up any .duckdb files created during tests
    for f in os.listdir(_test_data_dir):
        if f.endswith(".duckdb"):
            with contextlib.suppress(OSError):
                os.remove(os.path.join(_test_data_dir, f))


# ── Dialectic CRUD ──


class TestDialecticCRUD:
    def test_create_dialectic(self):
        r = client.post("/api/dialectics", json={"name": "test1", "topic": "Test Topic"})
        assert r.status_code == 200
        data = r.json()
        assert data["name"] == "test1"
        assert data["state"]["name"] == "Test Topic"

    def test_create_duplicate_returns_409(self):
        client.post("/api/dialectics", json={"name": "dup"})
        r = client.post("/api/dialectics", json={"name": "dup"})
        assert r.status_code == 409

    def test_create_empty_name_returns_400(self):
        r = client.post("/api/dialectics", json={"name": "  ", "topic": "x"})
        assert r.status_code == 400

    def test_list_dialectics(self):
        client.post("/api/dialectics", json={"name": "list1"})
        client.post("/api/dialectics", json={"name": "list2"})
        r = client.get("/api/dialectics")
        assert r.status_code == 200
        names = [d["name"] for d in r.json()]
        assert "list1" in names
        assert "list2" in names

    def test_get_dialectic(self):
        client.post("/api/dialectics", json={"name": "get1", "topic": "Get Test"})
        r = client.get("/api/dialectics/get1")
        assert r.status_code == 200
        data = r.json()
        assert data["name"] == "Get Test"
        assert "conversation" in data
        assert "commitments" in data

    def test_get_nonexistent_returns_404(self):
        r = client.get("/api/dialectics/nonexistent")
        assert r.status_code == 404

    def test_delete_dialectic(self):
        client.post("/api/dialectics", json={"name": "del1"})
        r = client.delete("/api/dialectics/del1")
        assert r.status_code == 200
        assert r.json()["deleted"] == "del1"
        # Confirm it's gone
        r = client.get("/api/dialectics/del1")
        assert r.status_code == 404

    def test_delete_nonexistent_returns_404(self):
        r = client.delete("/api/dialectics/nope")
        assert r.status_code == 404


# ── Authorization ──


class TestAuthorization:
    """Routes require auth and enforce per-actor base ownership."""

    def test_unauthenticated_create_401(self):
        client.cookies.clear()
        r = client.post("/api/dialectics", json={"name": "anon"})
        assert r.status_code == 401

    def test_unauthenticated_list_401(self):
        client.cookies.clear()
        r = client.get("/api/dialectics")
        assert r.status_code == 401

    def test_unauthenticated_get_401(self):
        client.cookies.clear()
        r = client.get("/api/dialectics/anything")
        assert r.status_code == 401

    def test_unauthenticated_message_401(self):
        client.cookies.clear()
        r = client.post("/api/dialectics/anything/message", json={"message": "hi"})
        assert r.status_code == 401

    def test_other_actor_cannot_read(self):
        # Default test user creates a dialectic.
        client.post("/api/dialectics", json={"name": "private"})
        # Log out and log in as a different user.
        other_id = pdb.create_actor(
            get_registry().platform_con(),
            kind="user",
            email="other@example.com",
            display_name="Other",
            password_hash=auth.hash_password("pw"),
        )
        client.cookies.clear()
        token = auth.create_session(other_id)
        client.cookies.set(auth.SESSION_COOKIE, token)

        r = client.get("/api/dialectics/private")
        assert r.status_code == 404  # 404 (not 403) so we don't leak names

    def test_other_actor_cannot_delete(self):
        client.post("/api/dialectics", json={"name": "mine"})
        other_id = pdb.create_actor(
            get_registry().platform_con(),
            kind="user",
            email="other@example.com",
            display_name="Other",
            password_hash=auth.hash_password("pw"),
        )
        client.cookies.clear()
        client.cookies.set(auth.SESSION_COOKIE, auth.create_session(other_id))
        r = client.delete("/api/dialectics/mine")
        assert r.status_code == 404

    def test_other_actor_cannot_send_message(self):
        client.post("/api/dialectics", json={"name": "private"})
        other_id = pdb.create_actor(
            get_registry().platform_con(),
            kind="user",
            email="other@example.com",
            display_name="Other",
            password_hash=auth.hash_password("pw"),
        )
        client.cookies.clear()
        client.cookies.set(auth.SESSION_COOKIE, auth.create_session(other_id))
        r = client.post("/api/dialectics/private/message", json={"message": "hi"})
        assert r.status_code == 404

    def test_list_filtered_to_current_actor(self):
        # Default user creates one dialectic.
        client.post("/api/dialectics", json={"name": "mine1"})
        client.post("/api/dialectics", json={"name": "mine2"})

        # Log in as a different user; they should see no dialectics.
        other_id = pdb.create_actor(
            get_registry().platform_con(),
            kind="user",
            email="other@example.com",
            display_name="Other",
            password_hash=auth.hash_password("pw"),
        )
        client.cookies.clear()
        client.cookies.set(auth.SESSION_COOKIE, auth.create_session(other_id))
        r = client.get("/api/dialectics")
        assert r.status_code == 200
        names = [d["name"] for d in r.json()]
        assert "mine1" not in names
        assert "mine2" not in names

    def test_admin_sees_all_dialectics(self):
        # Default user creates a dialectic.
        client.post("/api/dialectics", json={"name": "userbase"})

        # Log in as admin.
        admin_id = pdb.create_actor(
            get_registry().platform_con(),
            kind="admin",
            email="admin@example.com",
            display_name="Admin",
            password_hash=auth.hash_password("pw"),
        )
        client.cookies.clear()
        client.cookies.set(auth.SESSION_COOKIE, auth.create_session(admin_id))
        r = client.get("/api/dialectics")
        assert r.status_code == 200
        names = [d["name"] for d in r.json()]
        assert "userbase" in names

    def test_admin_can_access_any_dialectic(self):
        client.post("/api/dialectics", json={"name": "userbase"})

        admin_id = pdb.create_actor(
            get_registry().platform_con(),
            kind="admin",
            email="admin@example.com",
            display_name="Admin",
            password_hash=auth.hash_password("pw"),
        )
        client.cookies.clear()
        client.cookies.set(auth.SESSION_COOKIE, auth.create_session(admin_id))
        r = client.get("/api/dialectics/userbase")
        assert r.status_code == 200


# ── Tensions ──


class TestTensionEndpoints:
    def _setup_dialectic_with_tension(self):
        client.post("/api/dialectics", json={"name": "tens"})
        state = get_registry().get("tens")
        state.commit("P")
        state.deny("Q")
        tid = state.add_tension(["P"], ["Q"], reason="conflict")
        return tid

    def test_accept_tension(self):
        tid = self._setup_dialectic_with_tension()
        r = client.post(f"/api/dialectics/tens/tensions/{tid}", json={"action": "accept"})
        assert r.status_code == 200
        assert "accepted" in r.json()
        assert len(r.json()["state"]["implications"]) == 1

    def test_contest_tension(self):
        tid = self._setup_dialectic_with_tension()
        r = client.post(f"/api/dialectics/tens/tensions/{tid}", json={"action": "contest"})
        assert r.status_code == 200
        assert r.json()["contested"] == tid

    def test_invalid_tension_action(self):
        tid = self._setup_dialectic_with_tension()
        r = client.post(f"/api/dialectics/tens/tensions/{tid}", json={"action": "invalid"})
        assert r.status_code == 400

    def test_accept_nonexistent_tension(self):
        client.post("/api/dialectics", json={"name": "tens2"})
        r = client.post("/api/dialectics/tens2/tensions/999", json={"action": "accept"})
        assert r.status_code == 404


# ── Retract ──


class TestRetractEndpoint:
    def test_retract_proposition(self):
        client.post("/api/dialectics", json={"name": "ret1"})
        get_registry().get("ret1").commit("Some claim")
        r = client.post("/api/dialectics/ret1/retract", json={"proposition": "Some claim"})
        assert r.status_code == 200
        assert r.json()["retracted"] == "Some claim"
        assert "Some claim" not in r.json()["state"]["commitments"]


# ── Derive ──


class TestDeriveEndpoint:
    def test_derive_containment(self):
        client.post("/api/dialectics", json={"name": "der1"})
        get_registry().get("der1").commit("P")
        r = client.post("/api/dialectics/der1/derive", json={"gamma": ["P"], "delta": ["P"]})
        assert r.status_code == 200
        assert r.json()["derives"] is True

    def test_derive_no_derivation(self):
        client.post("/api/dialectics", json={"name": "der2"})
        get_registry().get("der2").commit("P")
        get_registry().get("der2").commit("Q")
        r = client.post("/api/dialectics/der2/derive", json={"gamma": ["P"], "delta": ["Q"]})
        assert r.status_code == 200
        assert r.json()["derives"] is False


# ── Message (mocked LLM) ──


class TestMessageEndpoint:
    @patch("elenchus.server.opponent.async_respond", new_callable=AsyncMock)
    def test_send_message(self, mock_respond):
        mock_respond.return_value = {
            "response": "Interesting claim.",
            "speech_acts": [{"type": "COMMIT", "proposition": "Test prop"}],
            "new_tensions": [],
        }
        client.post("/api/dialectics", json={"name": "msg1"})
        r = client.post("/api/dialectics/msg1/message", json={"message": "I believe X."})
        assert r.status_code == 200
        data = r.json()
        assert data["response"] == "Interesting claim."
        assert len(data["speech_acts"]) == 1

    @patch("elenchus.server.opponent.async_respond", new_callable=AsyncMock)
    def test_message_to_nonexistent_dialectic(self, mock_respond):
        r = client.post("/api/dialectics/nope/message", json={"message": "Hello"})
        assert r.status_code == 404
        mock_respond.assert_not_called()


# ── Settings ──


class TestSettingsEndpoints:
    def test_get_settings(self):
        r = client.get("/api/settings")
        assert r.status_code == 200
        data = r.json()
        assert "model" in data
        assert "protocol" in data
        assert "has_api_key" in data

    @patch("elenchus.server.opponent.reconfigure")
    def test_update_settings(self, mock_reconfig):
        r = client.put("/api/settings", json={"model": "gpt-4o"})
        assert r.status_code == 200
        mock_reconfig.assert_called_once()


# ── Report ──


class TestReportEndpoint:
    def test_report_text(self):
        client.post("/api/dialectics", json={"name": "rpt1"})
        get_registry().get("rpt1").commit("P")
        r = client.get("/api/dialectics/rpt1/report")
        assert r.status_code == 200
        assert "report" in r.json()
        assert "L_B" in r.json()["report"]
