"""
invites.py — high-level invite workflow.

Wraps `db.platform` invite helpers with the higher-level moves the
route handlers need: issue with optional email delivery, consume
during signup (which atomically creates the actor and starts a
session), list/revoke for admin use.

Routes that map to these functions land in D4 (server.py
`/api/admin/invites/*` and `/api/auth/signup`).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException

from . import auth, email_service
from .db import get_registry
from .db import platform as pdb

logger = logging.getLogger(__name__)

DEFAULT_INVITE_TTL = timedelta(days=30)


def issue_invite(
    *,
    role: str,
    issued_by: int,
    intended_email: str | None = None,
    ttl: timedelta | None = DEFAULT_INVITE_TTL,
    base_url: str = "",
    send_email: bool = True,
    metadata: dict | None = None,
) -> str:
    """Issue a new invite. Returns the token. If `intended_email` is
    set and `send_email` is True, delivers the invite by email via the
    configured EmailService (the console backend logs it to stdout for
    out-of-band delivery)."""
    if role not in {"admin", "researcher", "user", "judge"}:
        raise HTTPException(status_code=400, detail=f"Invalid invite role: {role!r}")

    reg = get_registry()
    token = auth.generate_token()
    expires_at: datetime | None = datetime.now(UTC) + ttl if ttl is not None else None

    with reg.platform_lock:
        pdb.create_invite(
            reg.platform_con(),
            token=token,
            role=role,
            issued_by=issued_by,
            intended_email=intended_email,
            expires_at=expires_at,
            metadata=metadata,
        )

    if send_email and intended_email:
        try:
            email_service.send_invite_email(
                token=token, recipient=intended_email, role=role, base_url=base_url
            )
        except Exception:
            logger.exception(
                "Failed to send invite email to %s (token still valid)", intended_email
            )

    logger.info(
        "Issued invite (role=%s, email=%s, issued_by=%d)",
        role,
        intended_email or "(none)",
        issued_by,
    )
    return token


def list_invites(*, include_consumed: bool = True) -> list[dict]:
    return pdb.list_invites(get_registry().platform_con(), include_consumed=include_consumed)


def revoke_invite(token: str) -> bool:
    """Revoke an unconsumed invite. Returns True if an invite was
    revoked, False otherwise (already consumed or never existed)."""
    reg = get_registry()
    with reg.platform_lock:
        return pdb.revoke_invite(reg.platform_con(), token)


def signup_with_invite(
    *,
    token: str,
    display_name: str,
    password: str,
    email_override: str | None = None,
) -> dict:
    """Atomically consume an invite and create the actor it authorizes.
    Returns `{actor_id, session_token, role}`.

    Raises HTTPException(400) if the invite is invalid (unknown,
    consumed, or expired). The signup also creates an auth session so
    the caller can immediately set a cookie.

    `email_override` allows the signing-up user to provide their own
    email when the invite didn't pre-fill `intended_email`. If the
    invite *did* specify an email, that email is used and any override
    is ignored (prevents email-swap on accepted invite).
    """
    reg = get_registry()

    # Pre-flight check — surfaces a clean 400 without holding the lock.
    invite = pdb.find_invite(reg.platform_con(), token)
    if invite is None:
        raise HTTPException(status_code=400, detail="Invalid invite token")
    if invite["consumed_at"] is not None:
        raise HTTPException(status_code=400, detail="This invite has already been used")

    email = invite["intended_email"] or email_override
    if not email:
        raise HTTPException(
            status_code=400,
            detail="This invite did not specify an email; please supply one",
        )

    # Check email isn't already registered (would violate UNIQUE).
    existing = pdb.find_actor_by_email(reg.platform_con(), email)
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"An account already exists for {email}")

    # All checks passed; perform the create + consume inside the
    # platform lock so concurrent signups on the same token can't both
    # succeed.
    with reg.platform_lock:
        con = reg.platform_con()

        # Re-check inside the lock — atomic SQL gate.
        consumed = pdb.consume_invite(con, token, consumed_by=0)  # placeholder consumed_by
        if consumed is None:
            raise HTTPException(status_code=400, detail="Invalid or already used invite")

        actor_id = pdb.create_actor(
            con,
            kind=invite["role"],
            email=email,
            display_name=display_name,
            password_hash=auth.hash_password(password),
        )

        # Update consumed_by to the real actor id (we used 0 as a
        # placeholder to satisfy NOT NULL).
        con.execute(
            "UPDATE invites SET consumed_by = ? WHERE token = ?",
            [actor_id, token],
        )

        session_token = auth.generate_token()
        pdb.create_auth_session(con, token=session_token, actor_id=actor_id)

    logger.info(
        "Signup via invite: actor_id=%d, role=%s, email=%s", actor_id, invite["role"], email
    )
    return {
        "actor_id": actor_id,
        "session_token": session_token,
        "role": invite["role"],
    }
