"""Tests for the session-keyed API (`/api/sessions/...`).

These cover what is unique to session-keying: a session id is returned on
create, addresses the underlying base on every verb, enforces the same
404 leak-prevention for non-owners, backfills a session for bases that
predate the feature, and coexists with the retained `/api/dialectics/...`
alias. The shared verb logic itself is already covered by
`test_server.py`; here we prove the resolution + delegation layer.
"""

import contextlib
import os
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from elenchus import auth
from elenchus.db import get_registry
from elenchus.db import platform as pdb
from elenchus.server import app

_test_data_dir = os.environ["ELENCHUS_DATA"]

client = TestClient(app)


def _close_per_base_handles(reg):
    for _name, handle in list(reg._handles.items()):
        with contextlib.suppress(Exception):
            handle.state.base.con.close()
    reg._handles.clear()


@pytest.fixture(autouse=True)
def _clean_states():
    reg = get_registry()
    reg.migrate_platform()
    con = reg.platform_con()
    with reg.platform_lock:
        for table in ("auth_sessions", "magic_links", "invites", "sessions", "bases", "actors"):
            con.execute(f"DELETE FROM {table}")
    _close_per_base_handles(reg)
    client.cookies.clear()

    actor_id = pdb.create_actor(
        con,
        kind="user",
        email="owner@example.com",
        display_name="Owner",
        password_hash=auth.hash_password("pw"),
    )
    client.cookies.set(auth.SESSION_COOKIE, auth.create_session(actor_id))
    yield {"actor_id": actor_id}

    client.cookies.clear()
    _close_per_base_handles(reg)
    for root, _dirs, files in os.walk(_test_data_dir):
        for f in files:
            if f.endswith(".duckdb") and f != "platform.duckdb":
                with contextlib.suppress(OSError):
                    os.remove(os.path.join(root, f))


def _create(name="s1", topic="Topic"):
    r = client.post("/api/sessions", json={"name": name, "topic": topic})
    assert r.status_code == 200, r.text
    return r.json()


class TestCreateAndAddress:
    def test_create_returns_session_id(self):
        body = _create()
        assert isinstance(body["session_id"], int)
        assert body["name"] == "s1"
        assert "state" in body

    def test_session_addresses_its_base(self):
        sid = _create(name="prov")["session_id"]
        r = client.get(f"/api/sessions/{sid}")
        assert r.status_code == 200
        # Same payload shape as GET /api/dialectics/{name}.
        assert "conversation" in r.json()

    def test_legacy_alias_still_works_after_session_create(self):
        _create(name="bridge")
        # The retained name-keyed route addresses the same base.
        assert client.get("/api/dialectics/bridge").status_code == 200

    def test_create_via_dialectics_also_opens_a_session(self):
        # The old create route now opens a session too, so the new list
        # surfaces it without a backfill.
        client.post("/api/dialectics", json={"name": "viaold"})
        names = {s["name"]: s for s in client.get("/api/sessions").json()}
        assert "viaold" in names
        assert isinstance(names["viaold"]["session_id"], int)


class TestVerbsViaSession:
    @patch("elenchus.server.opponent.async_respond", new_callable=AsyncMock)
    def test_message(self, mock_respond):
        mock_respond.return_value = {
            "response": "Noted.",
            "speech_acts": [{"type": "COMMIT", "proposition": "P"}],
            "new_tensions": [],
        }
        sid = _create()["session_id"]
        r = client.post(f"/api/sessions/{sid}/message", json={"message": "I claim P."})
        assert r.status_code == 200
        assert r.json()["response"] == "Noted."

    def test_derive_and_report_and_retract(self):
        sid = _create()["session_id"]
        assert (
            client.post(
                f"/api/sessions/{sid}/derive", json={"gamma": ["a"], "delta": ["b"]}
            ).status_code
            == 200
        )
        assert client.get(f"/api/sessions/{sid}/report").status_code == 200
        assert (
            client.post(f"/api/sessions/{sid}/retract", json={"proposition": "nope"}).status_code
            == 200
        )

    def test_unknown_tension_404_through_session(self):
        sid = _create()["session_id"]
        r = client.post(f"/api/sessions/{sid}/tensions/999", json={"action": "accept"})
        assert r.status_code == 404

    def test_delete_via_session(self):
        sid = _create(name="todelete")["session_id"]
        assert client.delete(f"/api/sessions/{sid}").status_code == 200
        # Base is gone; addressing the (now closed) session 404s.
        assert client.get(f"/api/sessions/{sid}").status_code == 404


class TestListAndBackfill:
    def test_list_includes_counts(self):
        _create(name="counted")
        rows = {s["name"]: s for s in client.get("/api/sessions").json()}
        assert "counted" in rows
        for k in ("commitments", "denials", "tensions", "implications"):
            assert k in rows["counted"]

    def test_backfills_session_for_legacy_base(self):
        # A base created out-of-band (no session row) must still surface
        # with a stable session id on first list.
        reg = get_registry()
        con = reg.platform_con()
        actor_id = pdb.find_actor_by_email(con, "owner@example.com")["id"]
        # Create the base file + row directly, no session.
        path = reg.db_path("legacy", actor_id=actor_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        from elenchus.dialectical_state import DialecticalState

        reg.put("legacy", DialecticalState.create(path, "Legacy"))
        with reg.platform_lock:
            pdb.create_base(con, base_id="legacy", name="Legacy", owner_id=actor_id)

        rows = {s["name"]: s for s in client.get("/api/sessions").json()}
        assert "legacy" in rows
        sid = rows["legacy"]["session_id"]
        assert isinstance(sid, int)
        # The backfilled id is stable + addressable.
        assert client.get(f"/api/sessions/{sid}").status_code == 200
        assert {s["name"]: s["session_id"] for s in client.get("/api/sessions").json()}[
            "legacy"
        ] == sid


class TestOwnership:
    def test_unknown_session_404(self):
        assert client.get("/api/sessions/999999").status_code == 404

    def test_cross_tenant_session_is_404(self):
        sid = _create(name="secret")["session_id"]
        # A different user must not address the owner's session.
        reg = get_registry()
        con = reg.platform_con()
        other = pdb.create_actor(
            con,
            kind="user",
            email="intruder@example.com",
            display_name="Intruder",
            password_hash=auth.hash_password("pw"),
        )
        other_client = TestClient(app)
        other_client.cookies.set(auth.SESSION_COOKIE, auth.create_session(other))
        assert other_client.get(f"/api/sessions/{sid}").status_code == 404
        assert (
            other_client.post(
                f"/api/sessions/{sid}/message", json={"message": "let me in"}
            ).status_code
            == 404
        )
