"""Tests for invites.py — the high-level invite workflow.

Uses the same `fresh_registry` fixture pattern as test_auth.py, with
a captured EmailService so we can assert delivery behavior without
hitting SMTP.
"""

from datetime import timedelta

import pytest

from elenchus import auth, email_service, invites
from elenchus.db import platform as pdb


@pytest.fixture
def fresh_registry(tmp_path):
    """Same pattern as tests/test_auth.py — temp platform DB,
    restore previous registry on teardown."""
    import elenchus.db.registry as registry_module
    from elenchus.db import init_registry

    previous = registry_module.registry

    reg = init_registry(data_dir=str(tmp_path), platform_path=str(tmp_path / "platform.duckdb"))
    reg.migrate_platform()

    yield reg

    reg.close_all()
    registry_module.registry = previous


class _CapturingEmailService:
    """In-memory email backend for tests. Records every send call."""

    def __init__(self):
        self.sent: list[tuple[str, str, str]] = []

    def send(self, to: str, subject: str, body: str) -> None:
        self.sent.append((to, subject, body))


@pytest.fixture
def captured_email(fresh_registry):
    fake = _CapturingEmailService()
    email_service.set_email_service(fake)
    yield fake
    email_service.set_email_service(None)  # type: ignore[arg-type]
    # Reset to default backend for subsequent tests
    email_service._service = None


@pytest.fixture
def admin(fresh_registry):
    """Create an admin actor and return their id."""
    return pdb.create_actor(
        fresh_registry.platform_con(),
        kind="admin",
        email="admin@example.com",
        display_name="Admin",
        password_hash=auth.hash_password("admin-pw"),
    )


# ─── Issuing ──────────────────────────────────────────────────────────


class TestIssueInvite:
    def test_returns_token(self, fresh_registry, admin):
        token = invites.issue_invite(role="user", issued_by=admin)
        assert isinstance(token, str)
        assert len(token) >= 30  # token_urlsafe(32)

    def test_invite_persists(self, fresh_registry, admin):
        token = invites.issue_invite(role="user", issued_by=admin)
        invite = pdb.find_invite(fresh_registry.platform_con(), token)
        assert invite is not None
        assert invite["role"] == "user"
        assert invite["issued_by"] == admin

    def test_invalid_role_rejected(self, fresh_registry, admin):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as excinfo:
            invites.issue_invite(role="superuser", issued_by=admin)
        assert excinfo.value.status_code == 400

    def test_email_delivered_when_intended_set(self, fresh_registry, admin, captured_email):
        invites.issue_invite(
            role="user",
            issued_by=admin,
            intended_email="newuser@example.com",
            base_url="https://example.com",
        )
        assert len(captured_email.sent) == 1
        to, subject, body = captured_email.sent[0]
        assert to == "newuser@example.com"
        assert "Elenchus" in subject
        assert "https://example.com/?token=" in body

    def test_no_email_when_intended_blank(self, fresh_registry, admin, captured_email):
        invites.issue_invite(role="user", issued_by=admin)
        assert captured_email.sent == []

    def test_no_email_when_send_email_false(self, fresh_registry, admin, captured_email):
        invites.issue_invite(
            role="user", issued_by=admin, intended_email="x@example.com", send_email=False
        )
        assert captured_email.sent == []


# ─── Listing and revoking ────────────────────────────────────────────


class TestListAndRevoke:
    def test_list_includes_issued(self, fresh_registry, admin):
        invites.issue_invite(role="user", issued_by=admin)
        invites.issue_invite(role="judge", issued_by=admin)
        listed = invites.list_invites()
        assert len(listed) >= 2

    def test_revoke_unconsumed_returns_true(self, fresh_registry, admin):
        token = invites.issue_invite(role="user", issued_by=admin)
        assert invites.revoke_invite(token) is True

    def test_revoke_then_signup_fails(self, fresh_registry, admin):
        from fastapi import HTTPException

        token = invites.issue_invite(role="user", issued_by=admin)
        invites.revoke_invite(token)
        with pytest.raises(HTTPException):
            invites.signup_with_invite(
                token=token,
                display_name="X",
                password="pw",
                email_override="x@example.com",
            )

    def test_revoke_unknown_returns_false(self, fresh_registry):
        assert invites.revoke_invite("nope") is False


# ─── Signup workflow ─────────────────────────────────────────────────


class TestSignupWithInvite:
    def test_creates_actor_with_invite_role(self, fresh_registry, admin):
        token = invites.issue_invite(
            role="researcher", issued_by=admin, intended_email="r@example.com"
        )
        result = invites.signup_with_invite(
            token=token, display_name="Researcher", password="strong-password"
        )
        assert "actor_id" in result
        assert "session_token" in result
        assert result["role"] == "researcher"

        # The new actor exists with the right role.
        actor = pdb.find_actor_by_id(fresh_registry.platform_con(), result["actor_id"])
        assert actor is not None
        assert actor["kind"] == "researcher"
        assert actor["email"] == "r@example.com"

    def test_session_token_resolves(self, fresh_registry, admin):
        token = invites.issue_invite(role="user", issued_by=admin, intended_email="u@example.com")
        result = invites.signup_with_invite(token=token, display_name="U", password="pw")
        resolved = auth.resolve_token(result["session_token"])
        assert resolved is not None
        assert resolved["id"] == result["actor_id"]

    def test_invite_consumed_after_signup(self, fresh_registry, admin):
        from fastapi import HTTPException

        token = invites.issue_invite(role="user", issued_by=admin, intended_email="o@example.com")
        invites.signup_with_invite(token=token, display_name="O", password="pw")

        # Replay must fail.
        with pytest.raises(HTTPException) as excinfo:
            invites.signup_with_invite(token=token, display_name="O2", password="pw2")
        assert excinfo.value.status_code == 400

    def test_unknown_token_400(self, fresh_registry):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as excinfo:
            invites.signup_with_invite(
                token="bogus",
                display_name="X",
                password="pw",
                email_override="x@example.com",
            )
        assert excinfo.value.status_code == 400

    def test_email_override_used_when_invite_blank(self, fresh_registry, admin):
        token = invites.issue_invite(role="user", issued_by=admin)
        result = invites.signup_with_invite(
            token=token,
            display_name="X",
            password="pw",
            email_override="x@example.com",
        )
        actor = pdb.find_actor_by_id(fresh_registry.platform_con(), result["actor_id"])
        assert actor["email"] == "x@example.com"

    def test_invite_email_preferred_over_override(self, fresh_registry, admin):
        """When the invite specifies an email, override is ignored (an
        attacker shouldn't be able to use someone else's invite with a
        different email)."""
        token = invites.issue_invite(
            role="user", issued_by=admin, intended_email="official@example.com"
        )
        result = invites.signup_with_invite(
            token=token,
            display_name="X",
            password="pw",
            email_override="hijack@example.com",
        )
        actor = pdb.find_actor_by_id(fresh_registry.platform_con(), result["actor_id"])
        assert actor["email"] == "official@example.com"

    def test_no_email_anywhere_rejected(self, fresh_registry, admin):
        from fastapi import HTTPException

        token = invites.issue_invite(role="user", issued_by=admin)
        with pytest.raises(HTTPException) as excinfo:
            invites.signup_with_invite(token=token, display_name="X", password="pw")
        assert excinfo.value.status_code == 400

    def test_duplicate_email_rejected(self, fresh_registry, admin):
        from fastapi import HTTPException

        # First, create someone with the email.
        pdb.create_actor(
            fresh_registry.platform_con(),
            kind="user",
            email="dup@example.com",
            display_name="Existing",
            password_hash="h",
        )
        token = invites.issue_invite(
            role="user", issued_by=admin, intended_email="dup@example.com"
        )
        with pytest.raises(HTTPException) as excinfo:
            invites.signup_with_invite(token=token, display_name="New", password="pw")
        assert excinfo.value.status_code == 409

    def test_expired_invite_rejected(self, fresh_registry, admin):
        from fastapi import HTTPException

        token = invites.issue_invite(
            role="user",
            issued_by=admin,
            intended_email="e@example.com",
            ttl=timedelta(seconds=-1),
        )
        with pytest.raises(HTTPException) as excinfo:
            invites.signup_with_invite(token=token, display_name="E", password="pw")
        assert excinfo.value.status_code == 400


# ─── EmailService ────────────────────────────────────────────────────


class TestEmailService:
    def test_console_backend_logs(self, caplog):
        import logging

        svc = email_service.ConsoleEmailService()
        with caplog.at_level(logging.INFO, logger="elenchus.email_service"):
            svc.send("to@example.com", "Test", "Body content")
        assert any("to@example.com" in r.message for r in caplog.records)
        assert any("Test" in r.message for r in caplog.records)

    def test_set_email_service_overrides(self, captured_email):
        email_service.get_email_service().send("x@example.com", "Hi", "Body")
        assert len(captured_email.sent) == 1
        assert captured_email.sent[0][0] == "x@example.com"

    def test_invite_email_template_includes_role_and_link(self, captured_email):
        email_service.send_invite_email(
            token="abc123",
            recipient="u@example.com",
            role="researcher",
            base_url="https://example.com",
        )
        assert len(captured_email.sent) == 1
        _to, subject, body = captured_email.sent[0]
        assert "researcher" in body
        assert "abc123" in body
        assert "https://example.com/?token=abc123" in body
        assert "Elenchus" in subject

    def test_magic_link_template(self, captured_email):
        email_service.send_magic_link_email(
            token="xyz", recipient="u@example.com", base_url="https://example.com"
        )
        _to, subject, body = captured_email.sent[0]
        assert "https://example.com/auth/magic/xyz" in body
        assert "20 minutes" in body or "once" in body

    def test_password_changed_notification(self, captured_email):
        email_service.send_password_changed_notification("u@example.com")
        _to, subject, _body = captured_email.sent[0]
        assert "password" in subject.lower()
