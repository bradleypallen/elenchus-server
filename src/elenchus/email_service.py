"""
email_service.py — outbound email abstraction.

Provides an `EmailService` interface with two implementations:
- `ConsoleEmailService` (default) — logs emails to stdout. Used in
  development and in production deployments that don't have SMTP
  configured yet. Tokens are still functional; they're just delivered
  out-of-band (the operator copies them from the log).
- `SMTPEmailService` — sends via SMTP. Used in production with a
  configured mailserver or transactional-email provider.

The backend is selected at module-load time from the EMAIL_BACKEND
env var (`console` default; `smtp` to enable SMTP). SMTP configuration
comes from `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`,
`SMTP_FROM` env vars.

Phase A scope: the abstraction and the console backend. The SMTP
backend ships as a minimal implementation; the alerting subsystem
in Phase C will share this infrastructure and may add a
transactional-provider backend (Postmark / SendGrid).
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage
from typing import Protocol

logger = logging.getLogger(__name__)


class EmailService(Protocol):
    """Minimal email-sending interface. Implementations send a single
    plaintext message; HTML support can be added later if needed."""

    def send(self, to: str, subject: str, body: str) -> None: ...


class ConsoleEmailService:
    """Prints emails to the application log. Used for development and
    for the Phase A pilot deployment where ADSA doesn't yet have an
    institutional SMTP relay configured."""

    def send(self, to: str, subject: str, body: str) -> None:
        logger.info(
            "[CONSOLE EMAIL]\n  To: %s\n  Subject: %s\n  Body:\n%s",
            to,
            subject,
            _indent(body, "    "),
        )


class SMTPEmailService:
    """Sends via the configured SMTP relay. Reads SMTP_HOST, SMTP_PORT,
    SMTP_USER, SMTP_PASSWORD, SMTP_FROM from env at instance time so
    operators can update them without code changes."""

    def __init__(self) -> None:
        self.host = os.environ.get("SMTP_HOST", "localhost")
        self.port = int(os.environ.get("SMTP_PORT", "587"))
        self.user = os.environ.get("SMTP_USER")
        self.password = os.environ.get("SMTP_PASSWORD")
        self.sender = os.environ.get("SMTP_FROM", "elenchus@localhost")
        self.use_tls = os.environ.get("SMTP_USE_TLS", "true").lower() != "false"

    def send(self, to: str, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["From"] = self.sender
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)

        with smtplib.SMTP(self.host, self.port) as smtp:
            if self.use_tls:
                smtp.starttls()
            if self.user and self.password:
                smtp.login(self.user, self.password)
            smtp.send_message(msg)
        logger.info("Sent email via SMTP to %s (subject=%r)", to, subject)


# ─── Backend selection ────────────────────────────────────────────────


def _build_service() -> EmailService:
    backend = os.environ.get("EMAIL_BACKEND", "console").lower()
    if backend == "smtp":
        logger.info("EmailService: SMTP backend")
        return SMTPEmailService()
    if backend != "console":
        logger.warning("Unknown EMAIL_BACKEND=%r; falling back to console", backend)
    logger.info("EmailService: console backend")
    return ConsoleEmailService()


_service: EmailService | None = None


def get_email_service() -> EmailService:
    """Return the process-wide EmailService. Lazily initialized so
    importing this module doesn't fail when SMTP env vars are absent."""
    global _service
    if _service is None:
        _service = _build_service()
    return _service


def set_email_service(service: EmailService) -> None:
    """Override the email service. Used by tests to substitute a
    capturing fake."""
    global _service
    _service = service


# ─── Template functions ───────────────────────────────────────────────


def send_invite_email(token: str, recipient: str, role: str, base_url: str = "") -> None:
    """Send an invite email. `base_url` should be the deployment's
    public URL (e.g. `https://elenchus.example.com`); the recipient
    follows the link to complete signup."""
    # The app is a single-page app served at "/"; it reads ?token= and shows
    # the signup form. (There is no separate /signup route.)
    link = f"{base_url.rstrip('/')}/?token={token}" if base_url else f"/?token={token}"
    body = (
        f"You have been invited to Elenchus as a {role}.\n\n"
        f"Click here to create your account:\n  {link}\n\n"
        f"If you weren't expecting this invitation, ignore this email.\n"
    )
    get_email_service().send(recipient, "Your Elenchus invitation", body)


def send_magic_link_email(token: str, recipient: str, base_url: str = "") -> None:
    """Send a magic-link login email."""
    link = f"{base_url.rstrip('/')}/auth/magic/{token}" if base_url else f"/auth/magic/{token}"
    body = (
        "Click here to log in to Elenchus:\n"
        f"  {link}\n\n"
        "This link is valid for 20 minutes and can be used once.\n"
        "If you didn't request this, ignore this email.\n"
    )
    get_email_service().send(recipient, "Your Elenchus login link", body)


def send_password_reset_email(token: str, recipient: str, base_url: str = "") -> None:
    """Send a password-reset email with a one-time link."""
    link = f"{base_url.rstrip('/')}/?reset={token}" if base_url else f"/?reset={token}"
    body = (
        "We received a request to reset your Elenchus password.\n\n"
        "Click here to choose a new password:\n"
        f"  {link}\n\n"
        "This link can be used once and expires soon.\n"
        "If you didn't request this, ignore this email — your password "
        "won't change.\n"
    )
    get_email_service().send(recipient, "Reset your Elenchus password", body)


def active_backend() -> str:
    """Name of the configured email backend ('console' or 'smtp'). Lets the
    admin UI tell whether a reset link was actually emailed or just logged."""
    return os.environ.get("EMAIL_BACKEND", "console").lower()


def send_password_changed_notification(recipient: str) -> None:
    """Notify an actor that their password has changed."""
    body = (
        "The password on your Elenchus account was just changed.\n\n"
        "If you did not make this change, contact the administrator immediately.\n"
    )
    get_email_service().send(recipient, "Your Elenchus password was changed", body)


# ─── Internal helpers ─────────────────────────────────────────────────


def _indent(text: str, prefix: str) -> str:
    return "\n".join(prefix + line for line in text.splitlines())
