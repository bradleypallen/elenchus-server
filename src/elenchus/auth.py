"""
auth.py — authentication primitives for Elenchus.

Provides password hashing, session-token creation/resolution/revocation,
magic-link issuance/consumption, and the FastAPI dependencies that
route handlers use to identify the current actor and gate access to
admin and base-owner endpoints.

This module is the boundary between the platform database
(`db/platform.py` query helpers) and the HTTP layer. Functions here
return rich objects or raise FastAPI HTTPException; route handlers
should not call `db.platform` directly for auth purposes.
"""

from __future__ import annotations

import logging
import os
import secrets
from datetime import timedelta

import bcrypt
from fastapi import HTTPException, Request

from .db import get_registry
from .db import platform as pdb

logger = logging.getLogger(__name__)

# bcrypt cost factor. 12 is the production default (≈ 250 ms per hash
# on modern hardware). The `BCRYPT_ROUNDS` env var lets the test
# suite drop this to 4 (≈ 1 ms per hash) without changing test logic.
# Production deployments should leave this unset.
_BCRYPT_ROUNDS = int(os.environ.get("BCRYPT_ROUNDS", "12"))

# bcrypt has a hard 72-byte limit on password input. We don't enforce
# a UI-level cap here; longer passwords are silently truncated by
# bcrypt itself, which is the standard practice.
_BCRYPT_MAX_BYTES = 72

# Cookie name. Kept consistent across routes; if it ever changes, all
# clients invalidate at once.
SESSION_COOKIE = "elenchus_session"

# Default session and magic-link TTLs.
SESSION_TTL = timedelta(days=30)
MAGIC_LINK_TTL = timedelta(minutes=20)


# ─── Password hashing ─────────────────────────────────────────────────


def _truncate_for_bcrypt(password: str) -> bytes:
    """Encode `password` as UTF-8 and truncate to bcrypt's 72-byte
    limit. This matches what bcrypt does internally on newer versions
    that raise rather than silently truncating."""
    return password.encode("utf-8")[:_BCRYPT_MAX_BYTES]


def hash_password(password: str) -> str:
    """Hash a password with bcrypt. Returns the hash as a UTF-8 string
    so it can be stored in a VARCHAR column."""
    salt = bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)
    hashed = bcrypt.hashpw(_truncate_for_bcrypt(password), salt)
    return hashed.decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a candidate password against a stored hash. Returns False
    for empty / malformed hashes rather than raising."""
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(
            _truncate_for_bcrypt(password),
            password_hash.encode("utf-8"),
        )
    except (ValueError, TypeError):
        logger.warning("malformed password hash during verify")
        return False


# ─── Token generation ─────────────────────────────────────────────────


def generate_token() -> str:
    """Generate a cryptographically random URL-safe token (~256 bits)."""
    return secrets.token_urlsafe(32)


# ─── Authentication ───────────────────────────────────────────────────


def authenticate(email: str, password: str) -> dict | None:
    """Look up an actor by email and verify the password. Returns the
    actor dict on success, None on any failure (no email, wrong
    password, deactivated)."""
    con = get_registry().platform_con()
    actor = pdb.find_actor_by_email(con, email)
    if actor is None:
        return None
    if actor.get("deactivated_at") is not None:
        return None
    if not verify_password(password, actor.get("password_hash", "")):
        return None
    return actor


def create_session(actor_id: int, ttl: timedelta = SESSION_TTL) -> str:
    """Issue a new session token for the given actor. Returns the token."""
    reg = get_registry()
    token = generate_token()
    with reg.platform_lock:
        pdb.create_auth_session(reg.platform_con(), token=token, actor_id=actor_id, ttl=ttl)
    return token


def resolve_token(token: str) -> dict | None:
    """Return the actor for a valid session token, or None if absent /
    expired / revoked / deactivated."""
    if not token:
        return None
    con = get_registry().platform_con()
    return pdb.resolve_auth_token(con, token)


def revoke_session(token: str) -> None:
    """Revoke a session token. Idempotent — no-op if already revoked."""
    if not token:
        return
    reg = get_registry()
    with reg.platform_lock:
        pdb.revoke_auth_session(reg.platform_con(), token)


def change_password(actor_id: int, old_password: str, new_password: str) -> bool:
    """Change an actor's password. Returns True on success, False if
    the old password doesn't verify or the actor is unknown. On
    success, all other outstanding sessions for this actor are
    revoked (the current session, if any, must be re-issued by the
    caller)."""
    reg = get_registry()
    con = reg.platform_con()
    actor = pdb.find_actor_by_id(con, actor_id)
    if actor is None:
        return False
    if not verify_password(old_password, actor.get("password_hash", "")):
        return False
    with reg.platform_lock:
        pdb.update_actor_password(con, actor_id, hash_password(new_password))
        pdb.revoke_actor_sessions(con, actor_id)
    return True


# ─── Magic links ──────────────────────────────────────────────────────


def issue_magic_link(email: str, ttl: timedelta = MAGIC_LINK_TTL) -> str:
    """Issue a magic-link token for `email`. The token is stored in
    `magic_links`; delivery is the caller's responsibility (typically
    via EmailService). The token itself is returned; it should be
    embedded in a URL like `https://.../auth/magic/<token>`.

    Note: we issue magic links for any email, not just registered ones.
    This avoids leaking which emails are registered. If the email isn't
    in `actors`, `consume_magic_link` will fail gracefully.
    """
    reg = get_registry()
    token = generate_token()
    with reg.platform_lock:
        pdb.create_magic_link(reg.platform_con(), token=token, email=email, ttl=ttl)
    return token


def consume_magic_link(token: str) -> str | None:
    """Atomically consume a magic-link token. Returns the email it was
    issued for, or None if the token is invalid (unknown / expired /
    already consumed). Caller is responsible for then creating an
    auth session for the actor identified by that email.
    """
    if not token:
        return None
    reg = get_registry()
    with reg.platform_lock:
        return pdb.consume_magic_link(reg.platform_con(), token)


# ─── FastAPI dependencies ─────────────────────────────────────────────


def current_actor(request: Request) -> dict:
    """FastAPI dependency. Extracts the session token from the
    `elenchus_session` HTTP-only cookie, resolves to an actor, raises
    401 if absent or invalid.
    """
    token = request.cookies.get(SESSION_COOKIE)
    actor = resolve_token(token) if token else None
    if actor is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return actor


def current_actor_optional(request: Request) -> dict | None:
    """Like `current_actor` but returns None instead of raising 401.
    Used by endpoints that change behavior based on whether the caller
    is authenticated, but don't strictly require it."""
    token = request.cookies.get(SESSION_COOKIE)
    return resolve_token(token) if token else None


def require_admin(request: Request) -> dict:
    """FastAPI dependency. Like `current_actor` but additionally
    requires `kind == 'admin'`."""
    actor = current_actor(request)
    if actor.get("kind") != "admin":
        raise HTTPException(status_code=403, detail="Admin privilege required")
    return actor


def require_researcher(request: Request) -> dict:
    """FastAPI dependency: allows kind in {'researcher', 'admin'}.

    The Sloan study has researchers who can issue participant tokens
    and view per-study data, but who don't need full platform admin
    privileges (e.g. they shouldn't deactivate users). Admins are a
    superset — they can do everything a researcher can."""
    actor = current_actor(request)
    if actor.get("kind") not in ("researcher", "admin"):
        raise HTTPException(status_code=403, detail="Researcher privilege required")
    return actor


def require_judge(request: Request) -> dict:
    """FastAPI dependency for the blinded-judge interface.

    Allows kind in {'judge', 'admin'}. Admins can act as judges for
    smoke-testing the workflow; production judges have kind='judge'
    (invited like any other role). Researchers are NOT allowed —
    they assemble packages and view ratings but should not also rate,
    to keep the analysis chain clean."""
    actor = current_actor(request)
    if actor.get("kind") not in ("judge", "admin"):
        raise HTTPException(status_code=403, detail="Judge privilege required")
    return actor


def require_base_owner(base_id: str, actor: dict) -> dict:
    """Assert that `actor` owns the given base. Raises 403 otherwise.
    Returns the base dict on success, 404 if the base doesn't exist."""
    con = get_registry().platform_con()
    base = pdb.find_base(con, base_id)
    if base is None:
        raise HTTPException(status_code=404, detail=f"Base '{base_id}' not found")
    if base.get("owner_id") != actor.get("id") and actor.get("kind") != "admin":
        raise HTTPException(status_code=403, detail="You do not own this base")
    return base
