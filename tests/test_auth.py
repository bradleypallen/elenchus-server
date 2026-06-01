"""Tests for auth.py — password hashing, session tokens, magic links.

These tests use a temporary DBRegistry pointed at an in-memory
platform.duckdb so password operations don't leak to disk and tests
run fast. The registry is monkey-patched into `elenchus.auth` and
`elenchus.db` for the test's duration.
"""

from datetime import timedelta

import pytest

from elenchus import auth
from elenchus.db import platform as pdb


@pytest.fixture
def fresh_registry(tmp_path):
    """Replace the process-wide registry with one backed by a temp
    platform.duckdb for the duration of one test. `init_registry`
    closes the old registry first, so this is safe even when server.py
    has already initialized one at module load."""
    # Preserve the previous registry so server tests that run after
    # this one don't lose their state. We restore on teardown.
    import elenchus.db.registry as registry_module
    from elenchus.db import init_registry

    previous = registry_module.registry

    data_dir = str(tmp_path)
    platform_path = str(tmp_path / "platform.duckdb")
    reg = init_registry(data_dir=data_dir, platform_path=platform_path)
    reg.migrate_platform()

    yield reg

    reg.close_all()
    registry_module.registry = previous


@pytest.fixture
def actor(fresh_registry):
    """Create a baseline test actor and return their id + raw password."""
    password = "correct horse battery staple"
    con = fresh_registry.platform_con()
    actor_id = pdb.create_actor(
        con,
        kind="user",
        email="alice@example.com",
        display_name="Alice",
        password_hash=auth.hash_password(password),
    )
    return {"id": actor_id, "email": "alice@example.com", "password": password}


# ─── Password hashing ─────────────────────────────────────────────────


class TestPasswordHashing:
    def test_hash_verify_roundtrip(self):
        h = auth.hash_password("hunter2")
        assert auth.verify_password("hunter2", h) is True

    def test_wrong_password_rejected(self):
        h = auth.hash_password("hunter2")
        assert auth.verify_password("wrong", h) is False

    def test_two_hashes_of_same_password_differ(self):
        # bcrypt has a per-hash salt — same password gives different hashes.
        h1 = auth.hash_password("p")
        h2 = auth.hash_password("p")
        assert h1 != h2
        assert auth.verify_password("p", h1)
        assert auth.verify_password("p", h2)

    def test_verify_empty_hash_returns_false(self):
        assert auth.verify_password("anything", "") is False

    def test_verify_malformed_hash_returns_false(self):
        assert auth.verify_password("anything", "not-a-bcrypt-hash") is False


# ─── Token generation ────────────────────────────────────────────────


class TestTokenGeneration:
    def test_tokens_are_unique(self):
        tokens = {auth.generate_token() for _ in range(100)}
        assert len(tokens) == 100

    def test_tokens_are_url_safe(self):
        import string

        allowed = set(string.ascii_letters + string.digits + "-_")
        for _ in range(20):
            tok = auth.generate_token()
            assert set(tok) <= allowed

    def test_tokens_are_sufficient_length(self):
        # secrets.token_urlsafe(32) yields ~43 chars
        tok = auth.generate_token()
        assert len(tok) >= 40


# ─── Authentication flow ──────────────────────────────────────────────


class TestAuthenticate:
    def test_correct_password_returns_actor(self, fresh_registry, actor):
        result = auth.authenticate(actor["email"], actor["password"])
        assert result is not None
        assert result["id"] == actor["id"]

    def test_wrong_password_returns_none(self, fresh_registry, actor):
        assert auth.authenticate(actor["email"], "wrong") is None

    def test_unknown_email_returns_none(self, fresh_registry):
        assert auth.authenticate("ghost@example.com", "x") is None

    def test_deactivated_actor_cannot_authenticate(self, fresh_registry, actor):
        pdb.deactivate_actor(fresh_registry.platform_con(), actor["id"])
        assert auth.authenticate(actor["email"], actor["password"]) is None


# ─── Session lifecycle ───────────────────────────────────────────────


class TestSessions:
    def test_create_and_resolve(self, fresh_registry, actor):
        token = auth.create_session(actor["id"])
        resolved = auth.resolve_token(token)
        assert resolved is not None
        assert resolved["id"] == actor["id"]

    def test_revoke_makes_token_unresolvable(self, fresh_registry, actor):
        token = auth.create_session(actor["id"])
        auth.revoke_session(token)
        assert auth.resolve_token(token) is None

    def test_resolve_empty_token_returns_none(self, fresh_registry):
        assert auth.resolve_token("") is None
        assert auth.resolve_token(None) is None  # type: ignore[arg-type]

    def test_revoke_unknown_token_is_noop(self, fresh_registry):
        # Should not raise.
        auth.revoke_session("nonexistent-token")

    def test_expired_session_does_not_resolve(self, fresh_registry, actor):
        token = auth.create_session(actor["id"], ttl=timedelta(seconds=-1))
        assert auth.resolve_token(token) is None


# ─── Magic links ──────────────────────────────────────────────────────


class TestMagicLinks:
    def test_issue_then_consume(self, fresh_registry):
        token = auth.issue_magic_link("user@example.com")
        email = auth.consume_magic_link(token)
        assert email == "user@example.com"

    def test_consume_twice_returns_none(self, fresh_registry):
        token = auth.issue_magic_link("user@example.com")
        auth.consume_magic_link(token)
        assert auth.consume_magic_link(token) is None

    def test_expired_magic_link(self, fresh_registry):
        token = auth.issue_magic_link("u@example.com", ttl=timedelta(seconds=-1))
        assert auth.consume_magic_link(token) is None

    def test_unknown_token(self, fresh_registry):
        assert auth.consume_magic_link("nope") is None

    def test_empty_token(self, fresh_registry):
        assert auth.consume_magic_link("") is None


# ─── Password change ─────────────────────────────────────────────────


class TestChangePassword:
    def test_success_with_correct_old(self, fresh_registry, actor):
        ok = auth.change_password(actor["id"], actor["password"], "new-password")
        assert ok is True
        # Old password no longer works
        assert auth.authenticate(actor["email"], actor["password"]) is None
        # New password works
        result = auth.authenticate(actor["email"], "new-password")
        assert result is not None

    def test_wrong_old_password_fails(self, fresh_registry, actor):
        ok = auth.change_password(actor["id"], "wrong-old", "new")
        assert ok is False
        # Original password still works
        assert auth.authenticate(actor["email"], actor["password"]) is not None

    def test_unknown_actor_fails(self, fresh_registry):
        assert auth.change_password(9999, "x", "y") is False

    def test_change_revokes_all_sessions(self, fresh_registry, actor):
        t1 = auth.create_session(actor["id"])
        t2 = auth.create_session(actor["id"])
        auth.change_password(actor["id"], actor["password"], "new")
        # All existing sessions revoked.
        assert auth.resolve_token(t1) is None
        assert auth.resolve_token(t2) is None


# ─── FastAPI dependencies ────────────────────────────────────────────


class _FakeRequest:
    """Minimal Request stand-in for unit-testing the deps without
    spinning up FastAPI's app machinery."""

    def __init__(self, cookies: dict | None = None):
        self.cookies = cookies or {}


class TestFastAPIDeps:
    def test_current_actor_with_valid_cookie(self, fresh_registry, actor):
        token = auth.create_session(actor["id"])
        request = _FakeRequest(cookies={auth.SESSION_COOKIE: token})
        result = auth.current_actor(request)
        assert result["id"] == actor["id"]

    def test_current_actor_without_cookie_raises_401(self, fresh_registry):
        from fastapi import HTTPException

        request = _FakeRequest()
        with pytest.raises(HTTPException) as excinfo:
            auth.current_actor(request)
        assert excinfo.value.status_code == 401

    def test_current_actor_with_invalid_cookie_raises_401(self, fresh_registry):
        from fastapi import HTTPException

        request = _FakeRequest(cookies={auth.SESSION_COOKIE: "bogus"})
        with pytest.raises(HTTPException) as excinfo:
            auth.current_actor(request)
        assert excinfo.value.status_code == 401

    def test_current_actor_optional_returns_none_when_absent(self, fresh_registry):
        request = _FakeRequest()
        assert auth.current_actor_optional(request) is None

    def test_require_admin_rejects_non_admin(self, fresh_registry, actor):
        from fastapi import HTTPException

        token = auth.create_session(actor["id"])
        request = _FakeRequest(cookies={auth.SESSION_COOKIE: token})
        with pytest.raises(HTTPException) as excinfo:
            auth.require_admin(request)
        assert excinfo.value.status_code == 403

    def test_require_admin_accepts_admin(self, fresh_registry):
        admin_id = pdb.create_actor(
            fresh_registry.platform_con(),
            kind="admin",
            email="admin@example.com",
            display_name="Admin",
            password_hash=auth.hash_password("admin-pw"),
        )
        token = auth.create_session(admin_id)
        request = _FakeRequest(cookies={auth.SESSION_COOKIE: token})
        result = auth.require_admin(request)
        assert result["kind"] == "admin"


# ─── Base ownership ──────────────────────────────────────────────────


class TestRequireBaseOwner:
    def test_owner_can_access(self, fresh_registry, actor):
        from elenchus.db import platform as pdb

        pdb.create_base(
            fresh_registry.platform_con(),
            base_id="b1",
            name="B",
            owner_id=actor["id"],
        )
        base = auth.require_base_owner("b1", actor)
        assert base["id"] == "b1"

    def test_non_owner_rejected(self, fresh_registry, actor):
        from fastapi import HTTPException

        from elenchus.db import platform as pdb

        # Different actor owns the base
        other_id = pdb.create_actor(
            fresh_registry.platform_con(),
            kind="user",
            email="other@example.com",
            display_name="Other",
            password_hash="h",
        )
        pdb.create_base(fresh_registry.platform_con(), base_id="b1", name="B", owner_id=other_id)
        with pytest.raises(HTTPException) as excinfo:
            auth.require_base_owner("b1", actor)
        assert excinfo.value.status_code == 403

    def test_admin_can_access_any_base(self, fresh_registry):
        from elenchus.db import platform as pdb

        owner_id = pdb.create_actor(
            fresh_registry.platform_con(),
            kind="user",
            email="o@example.com",
            display_name="O",
            password_hash="h",
        )
        admin_id = pdb.create_actor(
            fresh_registry.platform_con(),
            kind="admin",
            email="a@example.com",
            display_name="A",
            password_hash="h",
        )
        pdb.create_base(fresh_registry.platform_con(), base_id="b1", name="B", owner_id=owner_id)
        admin_actor = pdb.find_actor_by_id(fresh_registry.platform_con(), admin_id)
        base = auth.require_base_owner("b1", admin_actor)
        assert base["id"] == "b1"

    def test_unknown_base_404(self, fresh_registry, actor):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as excinfo:
            auth.require_base_owner("nonexistent", actor)
        assert excinfo.value.status_code == 404
