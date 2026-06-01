"""Integration tests for the /api/auth/* and /api/admin/* routes.

Drives the FastAPI app via TestClient, exercising end-to-end flows
(invite → signup → login → access → logout). Uses a per-test temp
data directory so the platform DB is isolated.
"""

import contextlib
import logging
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

logger = logging.getLogger(__name__)

# Set ENV before any elenchus import so server.py picks up the temp dir.
_test_data_dir = tempfile.mkdtemp(prefix="elenchus_auth_routes_test_")
os.environ["ELENCHUS_DATA"] = _test_data_dir
os.environ.setdefault("ELENCHUS_API_KEY", "test-key-for-ci")

from elenchus import auth  # noqa: E402
from elenchus.db import get_registry  # noqa: E402
from elenchus.db import platform as pdb  # noqa: E402
from elenchus.server import app  # noqa: E402

client = TestClient(app)


@pytest.fixture(autouse=True)
def _clean_state():
    """Reset platform state between tests. The lifespan migrations only
    run on first request when using TestClient — we run them
    explicitly here so the tables exist before the cleanup runs."""
    reg = get_registry()
    reg.migrate_platform()
    con = reg.platform_con()
    with reg.platform_lock:
        for table in ("auth_sessions", "magic_links", "invites", "sessions", "bases", "actors"):
            con.execute(f"DELETE FROM {table}")
    # Reset per-base cache too in case a prior test left stale handles.
    reg._handles.clear()  # type: ignore[attr-defined]
    # Reset the TestClient cookies between tests.
    client.cookies.clear()
    yield
    client.cookies.clear()
    for f in os.listdir(_test_data_dir):
        with contextlib.suppress(OSError):
            os.remove(os.path.join(_test_data_dir, f))


def _make_admin() -> dict:
    """Create an admin actor and log in. Returns
    {actor_id, email, password, client (with auth cookie set)}."""
    con = get_registry().platform_con()
    actor_id = pdb.create_actor(
        con,
        kind="admin",
        email="admin@example.com",
        display_name="Admin",
        password_hash=auth.hash_password("admin-pw"),
    )
    r = client.post("/api/auth/login", json={"email": "admin@example.com", "password": "admin-pw"})
    assert r.status_code == 200, r.text
    return {"actor_id": actor_id, "email": "admin@example.com", "password": "admin-pw"}


# ─── Login / logout ───────────────────────────────────────────────────


class TestLogin:
    def test_correct_credentials_set_cookie(self):
        _make_admin()
        # After login, client has the cookie set.
        assert auth.SESSION_COOKIE in client.cookies

    def test_wrong_password_401(self):
        _make_admin()
        client.cookies.clear()
        r = client.post(
            "/api/auth/login",
            json={"email": "admin@example.com", "password": "wrong"},
        )
        assert r.status_code == 401

    def test_unknown_email_401(self):
        r = client.post("/api/auth/login", json={"email": "ghost@example.com", "password": "x"})
        assert r.status_code == 401


class TestLogout:
    def test_logout_revokes_session(self):
        _make_admin()
        # Hit a protected route — works.
        r = client.get("/api/auth/me")
        assert r.status_code == 200
        # Logout.
        r = client.post("/api/auth/logout")
        assert r.status_code == 200
        # Hit a protected route — 401.
        client.cookies.clear()
        r = client.get("/api/auth/me")
        assert r.status_code == 401

    def test_logout_without_session_is_idempotent(self):
        client.cookies.clear()
        r = client.post("/api/auth/logout")
        assert r.status_code == 200


# ─── /api/auth/me ─────────────────────────────────────────────────────


class TestMe:
    def test_returns_actor_fields(self):
        _make_admin()
        r = client.get("/api/auth/me")
        assert r.status_code == 200
        data = r.json()
        assert data["email"] == "admin@example.com"
        assert data["kind"] == "admin"

    def test_401_without_cookie(self):
        client.cookies.clear()
        r = client.get("/api/auth/me")
        assert r.status_code == 401


# ─── Signup via invite ────────────────────────────────────────────────


class TestSignup:
    def test_end_to_end_invite_then_signup(self):
        _make_admin()
        # Create an invite for a new user.
        r = client.post(
            "/api/admin/invites",
            json={"role": "user", "intended_email": "new@example.com"},
        )
        assert r.status_code == 200, r.text
        token = r.json()["token"]

        # Logout admin.
        client.post("/api/auth/logout")
        client.cookies.clear()

        # Sign up with the invite.
        r = client.post(
            "/api/auth/signup",
            json={"token": token, "display_name": "New User", "password": "user-pw"},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["role"] == "user"
        # The signup set a session cookie.
        assert auth.SESSION_COOKIE in client.cookies

        # The new user can hit /me.
        r = client.get("/api/auth/me")
        assert r.status_code == 200
        assert r.json()["email"] == "new@example.com"

    def test_signup_with_invalid_token_400(self):
        client.cookies.clear()
        r = client.post(
            "/api/auth/signup",
            json={"token": "bogus", "display_name": "X", "password": "pw"},
        )
        assert r.status_code == 400


# ─── Change password ─────────────────────────────────────────────────


class TestChangePassword:
    def test_change_succeeds_and_keeps_session(self):
        _make_admin()
        r = client.post(
            "/api/auth/change-password",
            json={"old_password": "admin-pw", "new_password": "new-pw"},
        )
        assert r.status_code == 200
        # Cookie should still work (server issued a fresh session).
        r2 = client.get("/api/auth/me")
        assert r2.status_code == 200

    def test_wrong_old_password_400(self):
        _make_admin()
        r = client.post(
            "/api/auth/change-password",
            json={"old_password": "wrong", "new_password": "x"},
        )
        assert r.status_code == 400

    def test_unauthenticated_401(self):
        client.cookies.clear()
        r = client.post(
            "/api/auth/change-password",
            json={"old_password": "x", "new_password": "y"},
        )
        assert r.status_code == 401


# ─── Magic links ──────────────────────────────────────────────────────


class TestMagicLinkRoutes:
    def test_request_returns_200_regardless_of_email(self):
        # Don't leak whether the email is registered.
        client.cookies.clear()
        r = client.post("/api/auth/magic-link", json={"email": "ghost@example.com"})
        assert r.status_code == 200

    def test_consume_unknown_token_400(self):
        client.cookies.clear()
        r = client.get("/api/auth/magic/nonexistent")
        assert r.status_code == 400

    def test_full_magic_link_flow(self):
        _make_admin()
        client.post("/api/auth/logout")
        client.cookies.clear()

        # Request a magic link for an existing actor.
        token = auth.issue_magic_link("admin@example.com")

        # Consume it.
        r = client.get(f"/api/auth/magic/{token}")
        assert r.status_code == 200
        # Cookie set.
        assert auth.SESSION_COOKIE in client.cookies
        # And /me works.
        r = client.get("/api/auth/me")
        assert r.status_code == 200


# ─── Admin: invites ───────────────────────────────────────────────────


class TestAdminInvites:
    def test_non_admin_cannot_create(self):
        # Create a non-admin user via invite.
        admin = _make_admin()
        r = client.post(
            "/api/admin/invites",
            json={"role": "user", "intended_email": "u@example.com"},
        )
        token = r.json()["token"]
        client.post("/api/auth/logout")
        client.cookies.clear()

        client.post(
            "/api/auth/signup",
            json={"token": token, "display_name": "U", "password": "pw"},
        )
        # Now logged in as user — should not be able to create invites.
        r = client.post("/api/admin/invites", json={"role": "user"})
        assert r.status_code == 403
        _ = admin  # silence unused-var

    def test_unauthenticated_admin_routes_return_401(self):
        client.cookies.clear()
        r = client.post("/api/admin/invites", json={"role": "user"})
        assert r.status_code == 401

    def test_list_invites(self):
        _make_admin()
        client.post("/api/admin/invites", json={"role": "user"})
        client.post("/api/admin/invites", json={"role": "judge"})
        r = client.get("/api/admin/invites")
        assert r.status_code == 200
        invites = r.json()["invites"]
        assert len(invites) >= 2

    def test_revoke_invite(self):
        _make_admin()
        r = client.post("/api/admin/invites", json={"role": "user"})
        token = r.json()["token"]
        r = client.delete(f"/api/admin/invites/{token}")
        assert r.status_code == 200
        # Replay revoke returns 404 (already revoked, can't re-revoke).
        r = client.delete(f"/api/admin/invites/{token}")
        assert r.status_code == 404

    def test_invalid_role_400(self):
        _make_admin()
        r = client.post("/api/admin/invites", json={"role": "superuser"})
        assert r.status_code == 400


class TestAdminUsers:
    def test_list_users(self):
        _make_admin()
        r = client.get("/api/admin/users")
        assert r.status_code == 200
        users = r.json()["users"]
        assert len(users) == 1
        assert users[0]["email"] == "admin@example.com"
        # password_hash never leaks
        assert "password_hash" not in users[0]

    def test_non_admin_forbidden(self):
        _make_admin()
        r = client.post(
            "/api/admin/invites", json={"role": "user", "intended_email": "x@example.com"}
        )
        token = r.json()["token"]
        client.post("/api/auth/logout")
        client.cookies.clear()
        client.post(
            "/api/auth/signup",
            json={"token": token, "display_name": "X", "password": "pw"},
        )
        r = client.get("/api/admin/users")
        assert r.status_code == 403
