"""Tests for password reset: hashed tokens, forgot/reset/set-password,
admin reset + force-logout, and the must_change_password flow."""

import contextlib
import hashlib
import os
from datetime import timedelta

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
        for t in (
            "password_resets",
            "auth_sessions",
            "magic_links",
            "invites",
            "sessions",
            "bases",
            "actors",
        ):
            con.execute(f"DELETE FROM {t}")
    for _n, h in list(reg._handles.items()):
        with contextlib.suppress(Exception):
            h.state.base.con.close()
    reg._handles.clear()
    client.cookies.clear()
    yield
    client.cookies.clear()


def _mk(kind: str, email: str, pw: str = "password-123") -> int:
    con = get_registry().platform_con()
    return pdb.create_actor(
        con,
        kind=kind,
        email=email,
        display_name=email.split("@")[0],
        password_hash=auth.hash_password(pw),
    )


def _login_admin() -> int:
    aid = _mk("admin", "admin@example.com")
    client.cookies.set(auth.SESSION_COOKIE, auth.create_session(aid))
    return aid


def _deactivate(actor_id: int) -> None:
    reg = get_registry()
    with reg.platform_lock:
        reg.platform_con().execute(
            "UPDATE actors SET deactivated_at = CURRENT_TIMESTAMP WHERE id = ?", [actor_id]
        )


# ── token storage / lifecycle (the security spine) ──


def test_reset_token_stored_hashed_not_raw():
    uid = _mk("user", "u@example.com")
    token = auth.issue_password_reset(uid)
    con = get_registry().platform_con()
    # The raw token is NOT in the table; only its SHA-256 hash is.
    assert (
        con.execute(
            "SELECT COUNT(*) FROM password_resets WHERE token_hash = ?", [token]
        ).fetchone()[0]
        == 0
    )
    h = hashlib.sha256(token.encode()).hexdigest()
    assert (
        con.execute("SELECT COUNT(*) FROM password_resets WHERE token_hash = ?", [h]).fetchone()[0]
        == 1
    )


def test_consume_resets_password_and_is_single_use():
    uid = _mk("user", "u@example.com", pw="old-password-1")
    token = auth.issue_password_reset(uid)
    actor = auth.consume_password_reset(token, "new-password-1")
    assert actor and actor["id"] == uid
    assert auth.authenticate("u@example.com", "new-password-1") is not None
    assert auth.authenticate("u@example.com", "old-password-1") is None
    # Single-use: the same token can't be replayed.
    assert auth.consume_password_reset(token, "another-1234") is None


def test_issuing_invalidates_prior_tokens():
    uid = _mk("user", "u@example.com")
    t1 = auth.issue_password_reset(uid)
    t2 = auth.issue_password_reset(uid)
    assert auth.consume_password_reset(t1, "newpass-12345") is None
    assert auth.consume_password_reset(t2, "newpass-12345") is not None


def test_expired_token_rejected():
    uid = _mk("user", "u@example.com")
    token = auth.issue_password_reset(uid, ttl=timedelta(seconds=-1))
    assert auth.consume_password_reset(token, "newpass-12345") is None


# ── forgot-password (anti-enumeration, rate limit) ──


def test_forgot_password_unknown_email_is_200_and_silent():
    r = client.post("/api/auth/forgot-password", json={"email": "nobody@example.com"})
    assert r.status_code == 200
    con = get_registry().platform_con()
    assert con.execute("SELECT COUNT(*) FROM password_resets").fetchone()[0] == 0


def test_forgot_password_known_email_issues_one_reset():
    _mk("user", "u@example.com")
    assert (
        client.post("/api/auth/forgot-password", json={"email": "u@example.com"}).status_code
        == 200
    )
    con = get_registry().platform_con()
    assert con.execute("SELECT COUNT(*) FROM password_resets").fetchone()[0] == 1


def test_forgot_password_deactivated_issues_nothing():
    uid = _mk("user", "u@example.com")
    _deactivate(uid)
    assert (
        client.post("/api/auth/forgot-password", json={"email": "u@example.com"}).status_code
        == 200
    )
    con = get_registry().platform_con()
    assert con.execute("SELECT COUNT(*) FROM password_resets").fetchone()[0] == 0


def test_forgot_password_rate_limited():
    _mk("user", "u@example.com")
    for _ in range(auth.RESET_RATE_LIMIT + 3):
        client.post("/api/auth/forgot-password", json={"email": "u@example.com"})
    con = get_registry().platform_con()
    assert (
        con.execute("SELECT COUNT(*) FROM password_resets").fetchone()[0] == auth.RESET_RATE_LIMIT
    )


# ── reset-password route ──


def test_reset_password_route_revokes_sessions_and_sets_pw():
    uid = _mk("user", "u@example.com", pw="old-password-1")
    sess = auth.create_session(uid)
    token = auth.issue_password_reset(uid)
    r = client.post(
        "/api/auth/reset-password", json={"token": token, "new_password": "brand-new-pass"}
    )
    assert r.status_code == 200
    assert auth.resolve_token(sess) is None  # old session killed
    assert auth.authenticate("u@example.com", "brand-new-pass") is not None


def test_reset_password_enforces_min_length():
    uid = _mk("user", "u@example.com")
    token = auth.issue_password_reset(uid)
    assert (
        client.post(
            "/api/auth/reset-password", json={"token": token, "new_password": "short"}
        ).status_code
        == 400
    )


def test_reset_password_bad_token_400():
    assert (
        client.post(
            "/api/auth/reset-password", json={"token": "nope", "new_password": "longenough-123"}
        ).status_code
        == 400
    )


# ── set-password (forced change) ──


def test_set_password_requires_the_flag():
    uid = _mk("user", "u@example.com")
    client.cookies.set(auth.SESSION_COOKIE, auth.create_session(uid))
    assert (
        client.post("/api/auth/set-password", json={"new_password": "longenough-123"}).status_code
        == 403
    )


def test_set_password_when_flagged_clears_flag_and_keeps_session():
    uid = _mk("user", "u@example.com", pw="old-password-1")
    reg = get_registry()
    with reg.platform_lock:
        pdb.set_must_change_password(reg.platform_con(), uid, True)
    client.cookies.set(auth.SESSION_COOKIE, auth.create_session(uid))
    assert client.get("/api/auth/me").json()["must_change_password"] is True
    assert (
        client.post("/api/auth/set-password", json={"new_password": "my-new-password"}).status_code
        == 200
    )
    assert client.get("/api/auth/me").json()["must_change_password"] is False
    assert auth.authenticate("u@example.com", "my-new-password") is not None


# ── admin reset + reactivate-with-flag ──


def test_admin_reset_returns_link_and_force_logs_out():
    _login_admin()
    uid = _mk("user", "u@example.com")
    user_sess = auth.create_session(uid)
    r = client.post(f"/api/admin/users/{uid}/reset-password")
    assert r.status_code == 200
    assert "reset=" in r.json()["reset_url"]
    assert auth.resolve_token(user_sess) is None  # force-logged-out


def test_admin_reset_requires_admin():
    uid = _mk("user", "u@example.com")
    client.cookies.set(auth.SESSION_COOKIE, auth.create_session(uid))
    assert client.post(f"/api/admin/users/{uid}/reset-password").status_code == 403


def test_admin_reset_unknown_user_404():
    _login_admin()
    assert client.post("/api/admin/users/999999/reset-password").status_code == 404


def test_reactivate_with_require_password_change_sets_flag():
    _login_admin()
    uid = _mk("user", "u@example.com")
    _deactivate(uid)
    r = client.put(f"/api/admin/users/{uid}/reactivate?require_password_change=true")
    assert r.status_code == 200 and r.json()["require_password_change"] is True
    actor = pdb.find_actor_by_id(get_registry().platform_con(), uid)
    assert actor["must_change_password"] is True
