"""
platform.py — query helpers for platform.duckdb.

This module is the data-access layer for the platform DB. Each function
takes a connection (typically from `registry.platform_con()`) plus its
specific arguments, and returns plain Python dicts. No FastAPI types,
no HTTP semantics — that all lives in `auth.py`, `invites.py`, and the
route handlers.

Writes that mutate platform tables should hold `registry.platform_lock`
to serialize writers. DuckDB's MVCC permits concurrent reads.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)


# ─── Actors ───────────────────────────────────────────────────────────


def find_actor_by_id(con, actor_id: int) -> dict | None:
    row = con.execute(
        "SELECT id, kind, email, display_name, password_hash, "
        "credentials, created_at, deactivated_at "
        "FROM actors WHERE id = ?",
        [actor_id],
    ).fetchone()
    return _row_to_actor(row)


def find_actor_by_email(con, email: str) -> dict | None:
    row = con.execute(
        "SELECT id, kind, email, display_name, password_hash, "
        "credentials, created_at, deactivated_at "
        "FROM actors WHERE email = ?",
        [email],
    ).fetchone()
    return _row_to_actor(row)


def create_actor(
    con,
    *,
    kind: str,
    email: str | None,
    display_name: str,
    password_hash: str | None,
    credentials: dict | None = None,
) -> int:
    """Insert an actor. Returns the new actor id."""
    actor_id = con.execute("SELECT nextval('actors_id_seq')").fetchone()[0]
    con.execute(
        "INSERT INTO actors (id, kind, email, display_name, password_hash, "
        "credentials, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
        [
            actor_id,
            kind,
            email,
            display_name,
            password_hash,
            json.dumps(credentials or {}),
        ],
    )
    return actor_id


def update_actor_password(con, actor_id: int, password_hash: str) -> None:
    con.execute("UPDATE actors SET password_hash = ? WHERE id = ?", [password_hash, actor_id])


def deactivate_actor(con, actor_id: int) -> None:
    con.execute(
        "UPDATE actors SET deactivated_at = CURRENT_TIMESTAMP WHERE id = ?",
        [actor_id],
    )


def list_actors(con, *, include_deactivated: bool = False) -> list[dict]:
    sql = (
        "SELECT id, kind, email, display_name, password_hash, "
        "credentials, created_at, deactivated_at FROM actors"
    )
    if not include_deactivated:
        sql += " WHERE deactivated_at IS NULL"
    sql += " ORDER BY id"
    rows = con.execute(sql).fetchall()
    return [_row_to_actor(r) for r in rows if r is not None]


def actor_exists(con, actor_id: int) -> bool:
    """Check whether an actor id corresponds to an active actor.

    Used by cross-DB integrity validation when writing actor_id /
    contributor_id values into per-base files.
    """
    row = con.execute(
        "SELECT 1 FROM actors WHERE id = ? AND deactivated_at IS NULL LIMIT 1",
        [actor_id],
    ).fetchone()
    return row is not None


def _row_to_actor(row) -> dict | None:
    if row is None:
        return None
    try:
        credentials = json.loads(row[5]) if row[5] else {}
    except (json.JSONDecodeError, TypeError):
        credentials = {}
    return {
        "id": row[0],
        "kind": row[1],
        "email": row[2],
        "display_name": row[3],
        "password_hash": row[4],
        "credentials": credentials,
        "created_at": row[6],
        "deactivated_at": row[7],
    }


# ─── Auth sessions ────────────────────────────────────────────────────


def create_auth_session(
    con,
    *,
    token: str,
    actor_id: int,
    ttl: timedelta = timedelta(days=30),
) -> datetime:
    """Insert a new auth session row. Returns the expires_at timestamp."""
    expires_at = datetime.now(UTC) + ttl
    con.execute(
        "INSERT INTO auth_sessions (token, actor_id, issued_at, expires_at) "
        "VALUES (?, ?, CURRENT_TIMESTAMP, ?)",
        [token, actor_id, expires_at],
    )
    return expires_at


def resolve_auth_token(con, token: str) -> dict | None:
    """Return the actor associated with `token`, or None if absent /
    expired / revoked / deactivated."""
    row = con.execute(
        "SELECT a.id, a.kind, a.email, a.display_name, a.password_hash, "
        "a.credentials, a.created_at, a.deactivated_at "
        "FROM auth_sessions s "
        "JOIN actors a ON a.id = s.actor_id "
        "WHERE s.token = ? "
        "AND s.revoked_at IS NULL "
        "AND s.expires_at > CURRENT_TIMESTAMP "
        "AND a.deactivated_at IS NULL",
        [token],
    ).fetchone()
    return _row_to_actor(row)


def revoke_auth_session(con, token: str) -> None:
    con.execute(
        "UPDATE auth_sessions SET revoked_at = CURRENT_TIMESTAMP WHERE token = ?",
        [token],
    )


def revoke_actor_sessions(con, actor_id: int) -> None:
    """Revoke all of an actor's outstanding sessions (e.g., after
    password change or deactivation)."""
    con.execute(
        "UPDATE auth_sessions SET revoked_at = CURRENT_TIMESTAMP "
        "WHERE actor_id = ? AND revoked_at IS NULL",
        [actor_id],
    )


# ─── Magic links ──────────────────────────────────────────────────────


def create_magic_link(
    con, *, token: str, email: str, ttl: timedelta = timedelta(minutes=20)
) -> datetime:
    expires_at = datetime.now(UTC) + ttl
    con.execute(
        "INSERT INTO magic_links (token, email, issued_at, expires_at) "
        "VALUES (?, ?, CURRENT_TIMESTAMP, ?)",
        [token, email, expires_at],
    )
    return expires_at


def consume_magic_link(con, token: str) -> str | None:
    """Atomically mark a magic link as consumed. Returns the email if
    the link was valid (not yet consumed, not expired), else None."""
    row = con.execute(
        "SELECT email FROM magic_links "
        "WHERE token = ? "
        "AND consumed_at IS NULL "
        "AND expires_at > CURRENT_TIMESTAMP",
        [token],
    ).fetchone()
    if row is None:
        return None
    con.execute(
        "UPDATE magic_links SET consumed_at = CURRENT_TIMESTAMP WHERE token = ?",
        [token],
    )
    return row[0]


# ─── Invites ──────────────────────────────────────────────────────────


def create_invite(
    con,
    *,
    token: str,
    role: str,
    issued_by: int,
    intended_email: str | None = None,
    expires_at: datetime | None = None,
    metadata: dict | None = None,
) -> None:
    con.execute(
        "INSERT INTO invites (token, role, intended_email, issued_by, "
        "issued_at, expires_at, metadata) "
        "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?)",
        [
            token,
            role,
            intended_email,
            issued_by,
            expires_at,
            json.dumps(metadata or {}),
        ],
    )


def find_invite(con, token: str) -> dict | None:
    row = con.execute(
        "SELECT token, role, intended_email, issued_by, issued_at, "
        "expires_at, consumed_at, consumed_by, metadata "
        "FROM invites WHERE token = ?",
        [token],
    ).fetchone()
    return _row_to_invite(row)


def consume_invite(con, token: str, consumed_by: int) -> dict | None:
    """Atomically mark an invite as consumed by the given actor.
    Returns the invite row if it was valid, else None.

    The expiration check is done in SQL (against CURRENT_TIMESTAMP)
    rather than Python-side, because DuckDB returns naive datetimes
    that don't compare with timezone-aware `datetime.now(...)`.
    """
    # SQL handles the validity gate atomically.
    row = con.execute(
        "SELECT 1 FROM invites "
        "WHERE token = ? "
        "AND consumed_at IS NULL "
        "AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)",
        [token],
    ).fetchone()
    if row is None:
        return None
    invite = find_invite(con, token)
    con.execute(
        "UPDATE invites SET consumed_at = CURRENT_TIMESTAMP, consumed_by = ? WHERE token = ?",
        [consumed_by, token],
    )
    return invite


def revoke_invite(con, token: str) -> bool:
    """Mark an unconsumed, unrevoked invite as expired. Returns True if
    the invite was newly revoked, False if it was unknown, already
    consumed, or already revoked. Atomic — the same caller calling
    twice gets True then False."""
    rows = con.execute(
        "UPDATE invites SET expires_at = CURRENT_TIMESTAMP "
        "WHERE token = ? "
        "AND consumed_at IS NULL "
        "AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP) "
        "RETURNING token",
        [token],
    ).fetchall()
    return len(rows) > 0


def list_invites(con, *, include_consumed: bool = True) -> list[dict]:
    sql = (
        "SELECT token, role, intended_email, issued_by, issued_at, "
        "expires_at, consumed_at, consumed_by, metadata FROM invites"
    )
    if not include_consumed:
        sql += " WHERE consumed_at IS NULL"
    sql += " ORDER BY issued_at DESC"
    rows = con.execute(sql).fetchall()
    return [_row_to_invite(r) for r in rows if r is not None]


def _row_to_invite(row) -> dict | None:
    if row is None:
        return None
    try:
        metadata = json.loads(row[8]) if row[8] else {}
    except (json.JSONDecodeError, TypeError):
        metadata = {}
    return {
        "token": row[0],
        "role": row[1],
        "intended_email": row[2],
        "issued_by": row[3],
        "issued_at": row[4],
        "expires_at": row[5],
        "consumed_at": row[6],
        "consumed_by": row[7],
        "metadata": metadata,
    }


# ─── Bases ────────────────────────────────────────────────────────────


def create_base(con, *, base_id: str, name: str, owner_id: int) -> None:
    con.execute(
        "INSERT INTO bases (id, name, owner_id, created_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
        [base_id, name, owner_id],
    )


def find_base(con, base_id: str) -> dict | None:
    row = con.execute(
        "SELECT id, name, owner_id, created_at FROM bases WHERE id = ?",
        [base_id],
    ).fetchone()
    if row is None:
        return None
    return {"id": row[0], "name": row[1], "owner_id": row[2], "created_at": row[3]}


def find_base_by_owner_and_name(con, owner_id: int, name: str) -> dict | None:
    row = con.execute(
        "SELECT id, name, owner_id, created_at FROM bases WHERE owner_id = ? AND name = ?",
        [owner_id, name],
    ).fetchone()
    if row is None:
        return None
    return {"id": row[0], "name": row[1], "owner_id": row[2], "created_at": row[3]}


def list_bases_for_actor(con, owner_id: int) -> list[dict]:
    rows = con.execute(
        "SELECT id, name, owner_id, created_at FROM bases WHERE owner_id = ? ORDER BY created_at",
        [owner_id],
    ).fetchall()
    return [{"id": r[0], "name": r[1], "owner_id": r[2], "created_at": r[3]} for r in rows]


def delete_base(con, base_id: str) -> None:
    con.execute("DELETE FROM bases WHERE id = ?", [base_id])


# ─── Per-actor sessions against a base ────────────────────────────────


def create_session(con, *, actor_id: int, base_id: str) -> int:
    session_id = con.execute("SELECT nextval('sessions_id_seq')").fetchone()[0]
    con.execute(
        "INSERT INTO sessions (id, actor_id, base_id, opened_at, status) "
        "VALUES (?, ?, ?, CURRENT_TIMESTAMP, 'open')",
        [session_id, actor_id, base_id],
    )
    return session_id


def find_session(con, session_id: int) -> dict | None:
    row = con.execute(
        "SELECT id, actor_id, base_id, opened_at, closed_at, status FROM sessions WHERE id = ?",
        [session_id],
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "actor_id": row[1],
        "base_id": row[2],
        "opened_at": row[3],
        "closed_at": row[4],
        "status": row[5],
    }


def close_session(con, session_id: int) -> None:
    con.execute(
        "UPDATE sessions SET closed_at = CURRENT_TIMESTAMP, status = 'closed' WHERE id = ?",
        [session_id],
    )


def list_sessions_for_actor(con, actor_id: int, *, status: str | None = "open") -> list[dict]:
    sql = (
        "SELECT id, actor_id, base_id, opened_at, closed_at, status "
        "FROM sessions WHERE actor_id = ?"
    )
    params: list[Any] = [actor_id]
    if status is not None:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY opened_at DESC"
    rows = con.execute(sql, params).fetchall()
    return [
        {
            "id": r[0],
            "actor_id": r[1],
            "base_id": r[2],
            "opened_at": r[3],
            "closed_at": r[4],
            "status": r[5],
        }
        for r in rows
    ]


# ─── Settings ─────────────────────────────────────────────────────────


def get_setting(con, key: str) -> str | None:
    row = con.execute("SELECT value FROM platform_settings WHERE key = ?", [key]).fetchone()
    return row[0] if row else None


def set_setting(con, key: str, value: str) -> None:
    con.execute(
        "INSERT OR REPLACE INTO platform_settings (key, value) VALUES (?, ?)",
        [key, value],
    )
