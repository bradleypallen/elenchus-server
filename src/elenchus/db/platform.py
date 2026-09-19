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
        "credentials, created_at, deactivated_at, must_change_password "
        "FROM actors WHERE id = ?",
        [actor_id],
    ).fetchone()
    return _row_to_actor(row)


def find_actor_by_email(con, email: str) -> dict | None:
    row = con.execute(
        "SELECT id, kind, email, display_name, password_hash, "
        "credentials, created_at, deactivated_at, must_change_password "
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


def reactivate_actor(con, actor_id: int) -> None:
    con.execute(
        "UPDATE actors SET deactivated_at = NULL WHERE id = ?",
        [actor_id],
    )


def count_active_admins(con) -> int:
    """Return how many admin actors are currently active. Used to
    refuse a deactivation that would lock the platform out of itself."""
    row = con.execute(
        "SELECT COUNT(*) FROM actors WHERE kind = 'admin' AND deactivated_at IS NULL"
    ).fetchone()
    return int(row[0]) if row else 0


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
        "must_change_password": bool(row[8]) if len(row) > 8 else False,
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
        "a.credentials, a.created_at, a.deactivated_at, a.must_change_password "
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


# ─── Password resets ──────────────────────────────────────────────────


def set_must_change_password(con, actor_id: int, value: bool) -> None:
    con.execute(
        "UPDATE actors SET must_change_password = ? WHERE id = ?",
        [value, actor_id],
    )


def create_password_reset(
    con,
    *,
    token_hash: str,
    actor_id: int,
    ttl: timedelta,
    created_by: int | None = None,
    request_ip: str | None = None,
) -> datetime:
    """Store a (hashed) reset token. First invalidates the actor's other
    outstanding reset tokens, so only the newest link is ever live."""
    con.execute(
        "UPDATE password_resets SET used_at = CURRENT_TIMESTAMP "
        "WHERE actor_id = ? AND used_at IS NULL",
        [actor_id],
    )
    expires_at = datetime.now(UTC) + ttl
    con.execute(
        "INSERT INTO password_resets "
        "(token_hash, actor_id, created_at, expires_at, created_by, request_ip) "
        "VALUES (?, ?, CURRENT_TIMESTAMP, ?, ?, ?)",
        [token_hash, actor_id, expires_at, created_by, request_ip],
    )
    return expires_at


def consume_password_reset(con, token_hash: str) -> int | None:
    """Atomically consume a reset token by its hash. Returns the actor_id
    if valid (unused, unexpired), else None."""
    row = con.execute(
        "SELECT actor_id FROM password_resets "
        "WHERE token_hash = ? AND used_at IS NULL AND expires_at > CURRENT_TIMESTAMP",
        [token_hash],
    ).fetchone()
    if row is None:
        return None
    con.execute(
        "UPDATE password_resets SET used_at = CURRENT_TIMESTAMP WHERE token_hash = ?",
        [token_hash],
    )
    return row[0]


def count_recent_password_resets(con, actor_id: int, since: datetime) -> int:
    """Reset tokens created for this actor since `since` — for rate limiting."""
    row = con.execute(
        "SELECT COUNT(*) FROM password_resets WHERE actor_id = ? AND created_at > ?",
        [actor_id, since],
    ).fetchone()
    return int(row[0]) if row else 0


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


def list_bases(con) -> list[dict]:
    """List every base registered in the platform DB. Admin-only API
    surface uses this; non-admins should call `list_bases_for_actor`."""
    rows = con.execute(
        "SELECT id, name, owner_id, created_at FROM bases ORDER BY created_at"
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


def create_study_session(
    con,
    *,
    actor_id: int,
    study_token: str,
    condition: str,
    initial_state: str = "briefing",
) -> int:
    """Create a participant session row in the requested initial state.

    Unlike `create_session`, this allows base_id=NULL (the dialectic
    base doesn't exist yet during briefing/tutorial) and stamps the
    state-machine columns added in migration 0004."""
    session_id = con.execute("SELECT nextval('sessions_id_seq')").fetchone()[0]
    con.execute(
        "INSERT INTO sessions "
        "(id, actor_id, base_id, opened_at, status, "
        "state, state_changed_at, study_token, condition) "
        "VALUES (?, ?, NULL, CURRENT_TIMESTAMP, 'open', "
        "?, CURRENT_TIMESTAMP, ?, ?)",
        [session_id, actor_id, initial_state, study_token, condition],
    )
    return session_id


def find_study_session(con, session_id: int) -> dict | None:
    """Like `find_session` but returns the state-machine columns too."""
    row = con.execute(
        "SELECT id, actor_id, base_id, opened_at, closed_at, status, "
        "state, state_changed_at, study_token, condition "
        "FROM sessions WHERE id = ?",
        [session_id],
    ).fetchone()
    if row is None:
        return None
    return _row_to_study_session(row)


def find_live_session_for_actor(con, actor_id: int) -> dict | None:
    """Return the actor's currently-live session (state IN briefing,
    tutorial, active, post_session, surveyed), newest first. Returns
    None if they don't have one. Used by `GET /api/study/session` so
    the frontend can route the participant to the right screen."""
    row = con.execute(
        "SELECT id, actor_id, base_id, opened_at, closed_at, status, "
        "state, state_changed_at, study_token, condition "
        "FROM sessions "
        "WHERE actor_id = ? "
        "AND state IN ('briefing','tutorial','active','post_session','surveyed') "
        "ORDER BY opened_at DESC LIMIT 1",
        [actor_id],
    ).fetchone()
    if row is None:
        return None
    return _row_to_study_session(row)


def advance_session_state(con, session_id: int, to_state: str) -> dict | None:
    """Atomically move a session to `to_state` if the documented
    transition is allowed. Returns the updated row, or None if the
    session doesn't exist or the transition is invalid.

    Transition validation runs in Python (via `study_flow`) BEFORE
    the UPDATE so the SQL stays simple — we just guard on
    `state = <current>` to detect a concurrent modification."""
    from .. import study_flow  # local import — avoids a startup cycle

    current = find_study_session(con, session_id)
    if current is None:
        return None
    try:
        from_state = study_flow.parse_state(current["state"])
        target = study_flow.parse_state(to_state)
        study_flow.assert_transition(from_state, target)
    except ValueError:
        return None

    rows = con.execute(
        "UPDATE sessions SET state = ?, state_changed_at = CURRENT_TIMESTAMP "
        "WHERE id = ? AND state = ? RETURNING id",
        [to_state, session_id, current["state"]],
    ).fetchall()
    if not rows:
        return None
    # If we just hit a terminal state, also close the session row.
    if study_flow.is_terminal(target):
        con.execute(
            "UPDATE sessions SET closed_at = CURRENT_TIMESTAMP, status = 'closed' WHERE id = ?",
            [session_id],
        )
    return find_study_session(con, session_id)


def list_sessions_for_study(con, study_id: str) -> list[dict]:
    """Every session belonging to a study, oldest first. The link runs
    through `participant_session_tokens` (sessions.study_token →
    tokens.token, tokens.study_id) since sessions don't carry study_id
    directly."""
    rows = con.execute(
        "SELECT s.id, s.actor_id, s.base_id, s.opened_at, s.closed_at, "
        "s.status, s.state, s.state_changed_at, s.study_token, s.condition "
        "FROM sessions s "
        "JOIN participant_session_tokens t ON t.token = s.study_token "
        "WHERE t.study_id = ? "
        "ORDER BY s.opened_at",
        [study_id],
    ).fetchall()
    return [_row_to_study_session(r) for r in rows]


def attach_base_to_session(con, session_id: int, base_id: str) -> bool:
    """Bind a dialectic base to a session. Called when the
    participant transitions from `tutorial` to `active` and a
    fresh per-base file is created for their study task."""
    rows = con.execute(
        "UPDATE sessions SET base_id = ? WHERE id = ? AND base_id IS NULL RETURNING id",
        [base_id, session_id],
    ).fetchall()
    return bool(rows)


def _row_to_study_session(row) -> dict:
    return {
        "id": row[0],
        "actor_id": row[1],
        "base_id": row[2],
        "opened_at": row[3],
        "closed_at": row[4],
        "status": row[5],
        "state": row[6],
        "state_changed_at": row[7],
        "study_token": row[8],
        "condition": row[9],
    }


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


# ─── Survey responses (Sloan post-session questionnaires) ─────────────


def record_survey_response(
    con,
    *,
    session_id: int,
    instrument: str,
    instrument_version: str,
    responses: dict,
) -> int:
    """Insert one questionnaire submission. A re-submission inserts a
    new row; analysis takes the newest per (session, instrument)."""
    row = con.execute(
        "INSERT INTO survey_responses "
        "(session_id, instrument, instrument_version, responses) "
        "VALUES (?, ?, ?, ?) RETURNING id",
        [session_id, instrument, instrument_version, json.dumps(responses)],
    ).fetchone()
    return int(row[0]) if row else -1


def list_survey_responses_for_session(con, session_id: int) -> list[dict]:
    """Every submission for a session, newest first. The frontend uses
    this to show which instruments are already done."""
    rows = con.execute(
        "SELECT id, session_id, instrument, instrument_version, "
        "responses, submitted_at "
        "FROM survey_responses WHERE session_id = ? "
        "ORDER BY submitted_at DESC",
        [session_id],
    ).fetchall()
    return [_row_to_survey_response(r) for r in rows]


def list_survey_responses(con, *, instrument: str | None = None) -> list[dict]:
    """Cohort view for researchers, newest first."""
    if instrument is None:
        rows = con.execute(
            "SELECT id, session_id, instrument, instrument_version, "
            "responses, submitted_at "
            "FROM survey_responses ORDER BY submitted_at DESC"
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT id, session_id, instrument, instrument_version, "
            "responses, submitted_at "
            "FROM survey_responses WHERE instrument = ? "
            "ORDER BY submitted_at DESC",
            [instrument],
        ).fetchall()
    return [_row_to_survey_response(r) for r in rows]


def _row_to_survey_response(row) -> dict:
    try:
        responses = json.loads(row[4]) if row[4] else {}
    except (json.JSONDecodeError, TypeError):
        responses = {}
    return {
        "id": row[0],
        "session_id": row[1],
        "instrument": row[2],
        "instrument_version": row[3],
        "responses": responses,
        "submitted_at": row[5],
    }


# ─── Blinded judging (Sloan study) ────────────────────────────────────


def create_judge_package(
    con,
    *,
    study_id: str,
    slot_a_report_id: int,
    slot_b_report_id: int,
    slot_a_condition: str,
    slot_b_condition: str,
    created_by: int,
    notes: str = "",
) -> int:
    """Insert one judge package. The caller decides slot assignment
    (typically random). Returns the new id."""
    row = con.execute(
        "INSERT INTO judge_packages "
        "(study_id, slot_a_report_id, slot_b_report_id, "
        "slot_a_condition, slot_b_condition, created_by, notes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id",
        [
            study_id,
            slot_a_report_id,
            slot_b_report_id,
            slot_a_condition,
            slot_b_condition,
            created_by,
            notes,
        ],
    ).fetchone()
    return int(row[0]) if row else -1


def find_judge_package(con, package_id: int) -> dict | None:
    row = con.execute(
        "SELECT id, study_id, slot_a_report_id, slot_b_report_id, "
        "slot_a_condition, slot_b_condition, created_by, created_at, notes "
        "FROM judge_packages WHERE id = ?",
        [package_id],
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "study_id": row[1],
        "slot_a_report_id": row[2],
        "slot_b_report_id": row[3],
        "slot_a_condition": row[4],
        "slot_b_condition": row[5],
        "created_by": row[6],
        "created_at": row[7],
        "notes": row[8],
    }


def list_judge_packages(con, *, study_id: str | None = None) -> list[dict]:
    if study_id is None:
        rows = con.execute(
            "SELECT id, study_id, slot_a_report_id, slot_b_report_id, "
            "slot_a_condition, slot_b_condition, created_by, created_at, notes "
            "FROM judge_packages ORDER BY created_at DESC"
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT id, study_id, slot_a_report_id, slot_b_report_id, "
            "slot_a_condition, slot_b_condition, created_by, created_at, notes "
            "FROM judge_packages WHERE study_id = ? "
            "ORDER BY created_at DESC",
            [study_id],
        ).fetchall()
    return [
        {
            "id": r[0],
            "study_id": r[1],
            "slot_a_report_id": r[2],
            "slot_b_report_id": r[3],
            "slot_a_condition": r[4],
            "slot_b_condition": r[5],
            "created_by": r[6],
            "created_at": r[7],
            "notes": r[8],
        }
        for r in rows
    ]


def create_judge_assignment(
    con,
    *,
    judge_actor_id: int,
    package_id: int,
    assigned_by: int,
) -> int:
    row = con.execute(
        "INSERT INTO judge_assignments "
        "(judge_actor_id, package_id, assigned_by) "
        "VALUES (?, ?, ?) RETURNING id",
        [judge_actor_id, package_id, assigned_by],
    ).fetchone()
    return int(row[0]) if row else -1


def find_judge_assignment(con, assignment_id: int) -> dict | None:
    row = con.execute(
        "SELECT id, judge_actor_id, package_id, assigned_at, "
        "assigned_by, status "
        "FROM judge_assignments WHERE id = ?",
        [assignment_id],
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "judge_actor_id": row[1],
        "package_id": row[2],
        "assigned_at": row[3],
        "assigned_by": row[4],
        "status": row[5],
    }


def list_assignments_for_judge(
    con,
    judge_actor_id: int,
    *,
    status: str | None = None,
) -> list[dict]:
    """The judge's queue. Default returns every assignment ordered by
    assigned_at; pass `status='pending'` to filter."""
    if status is None:
        rows = con.execute(
            "SELECT id, judge_actor_id, package_id, assigned_at, "
            "assigned_by, status "
            "FROM judge_assignments WHERE judge_actor_id = ? "
            "ORDER BY assigned_at",
            [judge_actor_id],
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT id, judge_actor_id, package_id, assigned_at, "
            "assigned_by, status "
            "FROM judge_assignments "
            "WHERE judge_actor_id = ? AND status = ? "
            "ORDER BY assigned_at",
            [judge_actor_id, status],
        ).fetchall()
    return [
        {
            "id": r[0],
            "judge_actor_id": r[1],
            "package_id": r[2],
            "assigned_at": r[3],
            "assigned_by": r[4],
            "status": r[5],
        }
        for r in rows
    ]


def list_assignments_for_package(con, package_id: int) -> list[dict]:
    """Every assignment of a package, oldest first. Export + researcher
    dashboard surface."""
    rows = con.execute(
        "SELECT id, judge_actor_id, package_id, assigned_at, "
        "assigned_by, status "
        "FROM judge_assignments WHERE package_id = ? "
        "ORDER BY assigned_at",
        [package_id],
    ).fetchall()
    return [
        {
            "id": r[0],
            "judge_actor_id": r[1],
            "package_id": r[2],
            "assigned_at": r[3],
            "assigned_by": r[4],
            "status": r[5],
        }
        for r in rows
    ]


def mark_assignment_completed(con, assignment_id: int) -> bool:
    rows = con.execute(
        "UPDATE judge_assignments SET status = 'completed' "
        "WHERE id = ? AND status = 'pending' RETURNING id",
        [assignment_id],
    ).fetchall()
    return bool(rows)


def record_judge_rating(
    con,
    *,
    assignment_id: int,
    ratings: dict,
    justification_a: str,
    justification_b: str,
    pairwise_winner: str,
    condition_guess_a: str | None,
    condition_guess_b: str | None,
    confidence: int | None,
) -> int:
    """Insert one rating row. `ratings` is JSON-encoded; the rest are
    validated against table CHECKs."""
    row = con.execute(
        "INSERT INTO judge_ratings "
        "(assignment_id, ratings, justification_a, justification_b, "
        "pairwise_winner, condition_guess_a, condition_guess_b, confidence) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
        [
            assignment_id,
            json.dumps(ratings or {}),
            justification_a,
            justification_b,
            pairwise_winner,
            condition_guess_a,
            condition_guess_b,
            confidence,
        ],
    ).fetchone()
    return int(row[0]) if row else -1


def find_rating_for_assignment(con, assignment_id: int) -> dict | None:
    row = con.execute(
        "SELECT id, assignment_id, submitted_at, ratings, "
        "justification_a, justification_b, pairwise_winner, "
        "condition_guess_a, condition_guess_b, confidence "
        "FROM judge_ratings WHERE assignment_id = ? "
        "ORDER BY submitted_at DESC LIMIT 1",
        [assignment_id],
    ).fetchone()
    if row is None:
        return None
    try:
        ratings = json.loads(row[3]) if row[3] else {}
    except (json.JSONDecodeError, TypeError):
        ratings = {}
    return {
        "id": row[0],
        "assignment_id": row[1],
        "submitted_at": row[2],
        "ratings": ratings,
        "justification_a": row[4],
        "justification_b": row[5],
        "pairwise_winner": row[6],
        "condition_guess_a": row[7],
        "condition_guess_b": row[8],
        "confidence": row[9],
    }


# ─── Study reports (Sloan blinded judging) ────────────────────────────


def record_study_report(
    con,
    *,
    session_id: int,
    condition: str,
    content: str,
    generator_model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cost_usd: float,
    metadata: dict | None = None,
) -> int:
    """Insert one structured report row. Returns the new row id."""
    row = con.execute(
        "INSERT INTO study_reports "
        "(session_id, condition, content, generator_model, "
        "prompt_tokens, completion_tokens, cost_usd, metadata) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
        [
            session_id,
            condition,
            content,
            generator_model,
            prompt_tokens,
            completion_tokens,
            cost_usd,
            json.dumps(metadata or {}),
        ],
    ).fetchone()
    return int(row[0]) if row else -1


def find_study_report_for_session(con, session_id: int) -> dict | None:
    """Return the most recent report for a session, or None if none
    has been generated yet."""
    row = con.execute(
        "SELECT id, session_id, condition, generated_at, content, "
        "generator_model, prompt_tokens, completion_tokens, cost_usd, "
        "metadata "
        "FROM study_reports WHERE session_id = ? "
        "ORDER BY generated_at DESC LIMIT 1",
        [session_id],
    ).fetchone()
    return _row_to_study_report(row)


def list_study_reports(con, *, condition: str | None = None) -> list[dict]:
    """All reports, newest first. Researcher dashboard surface."""
    if condition is None:
        rows = con.execute(
            "SELECT id, session_id, condition, generated_at, content, "
            "generator_model, prompt_tokens, completion_tokens, cost_usd, "
            "metadata "
            "FROM study_reports ORDER BY generated_at DESC"
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT id, session_id, condition, generated_at, content, "
            "generator_model, prompt_tokens, completion_tokens, cost_usd, "
            "metadata "
            "FROM study_reports WHERE condition = ? "
            "ORDER BY generated_at DESC",
            [condition],
        ).fetchall()
    return [_row_to_study_report(r) for r in rows if r is not None]


def _row_to_study_report(row) -> dict | None:
    if row is None:
        return None
    try:
        metadata = json.loads(row[9]) if row[9] else {}
    except (json.JSONDecodeError, TypeError):
        metadata = {}
    return {
        "id": row[0],
        "session_id": row[1],
        "condition": row[2],
        "generated_at": row[3],
        "content": row[4],
        "generator_model": row[5],
        "prompt_tokens": row[6],
        "completion_tokens": row[7],
        "cost_usd": row[8],
        "metadata": metadata,
    }


# ─── Participant session tokens (Sloan study) ─────────────────────────


def create_participant_token(
    con,
    *,
    token: str,
    actor_id: int,
    study_id: str,
    condition: str,
    issued_by: int,
    scheduled_start: str | None = None,
    scheduled_end: str | None = None,
    notes: str = "",
    topic_title: str = "",
    topic_brief: str = "",
    participant_id: int | None = None,
    period: int | None = None,
) -> None:
    """Insert one participant_session_tokens row. The caller has
    already validated condition; the DB has its own CHECK as a
    backstop. `topic_title` / `topic_brief` are the writing task the
    participant is given in this session (migration 0009);
    `participant_id` / `period` tie the token to an enrolled participant
    and say which of their two sessions it opens (migration 0010)."""
    con.execute(
        "INSERT INTO participant_session_tokens "
        "(token, actor_id, study_id, condition, issued_by, "
        "scheduled_start, scheduled_end, notes, topic_title, topic_brief, "
        "participant_id, period) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            token,
            actor_id,
            study_id,
            condition,
            issued_by,
            scheduled_start,
            scheduled_end,
            notes,
            topic_title,
            topic_brief,
            participant_id,
            period,
        ],
    )


def find_participant_token(con, token: str) -> dict | None:
    """Look up a participant token. Returns None if not found.

    The result includes the token's status and used_at so callers can
    decide whether it's still consumable without re-querying."""
    row = con.execute(
        "SELECT token, actor_id, study_id, condition, scheduled_start, "
        "scheduled_end, issued_by, issued_at, used_at, session_id, "
        "status, notes, topic_title, topic_brief, participant_id, period "
        "FROM participant_session_tokens WHERE token = ?",
        [token],
    ).fetchone()
    if row is None:
        return None
    return {
        "token": row[0],
        "actor_id": row[1],
        "study_id": row[2],
        "condition": row[3],
        "scheduled_start": row[4],
        "scheduled_end": row[5],
        "issued_by": row[6],
        "issued_at": row[7],
        "used_at": row[8],
        "session_id": row[9],
        "status": row[10],
        "notes": row[11],
        "topic_title": row[12] or "",
        "topic_brief": row[13] or "",
        "participant_id": row[14],
        "period": row[15],
    }


def consume_participant_token(con, token: str) -> dict | None:
    """Atomically mark a `scheduled` token as `active` with used_at =
    now, returning the row. Returns None if the token doesn't exist,
    is in a non-consumable status, or is outside its scheduled window.

    Single-use semantics: the WHERE clause makes the UPDATE a no-op
    on a second call, so a participant who reloads the link doesn't
    accidentally double-trigger.
    """
    rows = con.execute(
        "UPDATE participant_session_tokens "
        "SET status = 'active', used_at = CURRENT_TIMESTAMP "
        "WHERE token = ? AND status = 'scheduled' "
        "AND (scheduled_start IS NULL OR scheduled_start <= CURRENT_TIMESTAMP) "
        "AND (scheduled_end   IS NULL OR scheduled_end   >  CURRENT_TIMESTAMP) "
        "RETURNING token",
        [token],
    ).fetchall()
    if not rows:
        return None
    return find_participant_token(con, token)


def set_token_session(con, token: str, session_id: int) -> None:
    """Link a consumed token to the session it opened. Called right
    after `create_study_session` so the researcher dashboard can join
    tokens → sessions → reports without a separate session-list
    endpoint."""
    con.execute(
        "UPDATE participant_session_tokens SET session_id = ? WHERE token = ?",
        [session_id, token],
    )


def void_participant_token(con, token: str) -> bool:
    """Mark a still-`scheduled` token as `voided`. Idempotent on
    already-voided / used tokens (returns False)."""
    rows = con.execute(
        "UPDATE participant_session_tokens SET status = 'voided' "
        "WHERE token = ? AND status = 'scheduled' RETURNING token",
        [token],
    ).fetchall()
    return bool(rows)


def list_participant_tokens(
    con,
    *,
    study_id: str | None = None,
    condition: str | None = None,
) -> list[dict]:
    """List tokens, newest first, optionally filtered by study or
    condition. Researchers use this for cohort overview."""
    clauses: list[str] = []
    params: list = []
    if study_id is not None:
        clauses.append("study_id = ?")
        params.append(study_id)
    if condition is not None:
        clauses.append("condition = ?")
        params.append(condition)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = con.execute(
        f"SELECT token, actor_id, study_id, condition, scheduled_start, "
        f"scheduled_end, issued_by, issued_at, used_at, session_id, "
        f"status, notes, topic_title, participant_id, period "
        f"FROM participant_session_tokens {where} "
        f"ORDER BY issued_at DESC",
        params,
    ).fetchall()
    return [
        {
            "token": r[0],
            "actor_id": r[1],
            "study_id": r[2],
            "condition": r[3],
            "scheduled_start": r[4],
            "scheduled_end": r[5],
            "issued_by": r[6],
            "issued_at": r[7],
            "used_at": r[8],
            "session_id": r[9],
            "status": r[10],
            "notes": r[11],
            "topic_title": r[12] or "",
            "participant_id": r[13],
            "period": r[14],
        }
        for r in rows
    ]


# ─── Study setup + enrolment (migration 0010) ─────────────────────────


def upsert_study_config(
    con,
    *,
    study_id: str,
    topic_a_title: str,
    topic_a_brief: str,
    topic_b_title: str,
    topic_b_brief: str,
    min_gap_hours: int,
    actor_id: int,
) -> dict:
    """Create or update a study's setup. `created_by` / `created_at`
    are kept from the first write."""
    if find_study_config(con, study_id) is None:
        con.execute(
            "INSERT INTO study_configs (study_id, topic_a_title, topic_a_brief, "
            "topic_b_title, topic_b_brief, min_gap_hours, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                study_id,
                topic_a_title,
                topic_a_brief,
                topic_b_title,
                topic_b_brief,
                min_gap_hours,
                actor_id,
            ],
        )
    else:
        con.execute(
            "UPDATE study_configs SET topic_a_title = ?, topic_a_brief = ?, "
            "topic_b_title = ?, topic_b_brief = ?, min_gap_hours = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE study_id = ?",
            [topic_a_title, topic_a_brief, topic_b_title, topic_b_brief, min_gap_hours, study_id],
        )
    return find_study_config(con, study_id)


def find_study_config(con, study_id: str) -> dict | None:
    row = con.execute(
        "SELECT study_id, topic_a_title, topic_a_brief, topic_b_title, topic_b_brief, "
        "min_gap_hours, created_by, created_at, updated_at "
        "FROM study_configs WHERE study_id = ?",
        [study_id],
    ).fetchone()
    if row is None:
        return None
    return {
        "study_id": row[0],
        "topics": {
            "A": {"title": row[1], "brief": row[2] or ""},
            "B": {"title": row[3], "brief": row[4] or ""},
        },
        "min_gap_hours": row[5],
        "created_by": row[6],
        "created_at": row[7],
        "updated_at": row[8],
    }


def list_study_configs(con) -> list[dict]:
    rows = con.execute("SELECT study_id FROM study_configs ORDER BY created_at").fetchall()
    return [find_study_config(con, r[0]) for r in rows]


_PARTICIPANT_COLUMNS = (
    "id, study_id, participant_code, display_name, first_condition, first_topic, "
    "allocation, enrolled_by, enrolled_at, notes"
)


def _row_to_participant(row) -> dict:
    return {
        "id": row[0],
        "study_id": row[1],
        "participant_code": row[2],
        "display_name": row[3],
        "first_condition": row[4],
        "first_topic": row[5],
        "allocation": row[6],
        "enrolled_by": row[7],
        "enrolled_at": row[8],
        "notes": row[9] or "",
    }


def create_study_participant(
    con,
    *,
    study_id: str,
    participant_code: str,
    display_name: str,
    first_condition: str,
    first_topic: str,
    allocation: str,
    enrolled_by: int,
    notes: str = "",
) -> int:
    row = con.execute(
        "INSERT INTO study_participants (study_id, participant_code, display_name, "
        "first_condition, first_topic, allocation, enrolled_by, notes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
        [
            study_id,
            participant_code,
            display_name,
            first_condition,
            first_topic,
            allocation,
            enrolled_by,
            notes,
        ],
    ).fetchone()
    return int(row[0])


def find_study_participant(con, participant_id: int) -> dict | None:
    row = con.execute(
        f"SELECT {_PARTICIPANT_COLUMNS} FROM study_participants WHERE id = ?", [participant_id]
    ).fetchone()
    return _row_to_participant(row) if row else None


def list_study_participants(con, study_id: str) -> list[dict]:
    """A study's participants in enrolment order — the order the
    permuted blocks were drawn in."""
    rows = con.execute(
        f"SELECT {_PARTICIPANT_COLUMNS} FROM study_participants WHERE study_id = ? ORDER BY id",
        [study_id],
    ).fetchall()
    return [_row_to_participant(r) for r in rows]


def find_participant_period_token(con, participant_id: int, period: int) -> dict | None:
    """The (non-voided, newest) token for one of a participant's two
    sessions."""
    row = con.execute(
        "SELECT token FROM participant_session_tokens "
        "WHERE participant_id = ? AND period = ? "
        "ORDER BY (status = 'voided'), issued_at DESC LIMIT 1",
        [participant_id, period],
    ).fetchone()
    return find_participant_token(con, row[0]) if row else None


def second_session_gate(con, token_row: dict) -> dict | None:
    """Why a participant's second link can't be opened yet — or None if
    it can. The design wants the two sessions in order and apart: the
    second opens only once the first has ended, and `min_gap_hours`
    after that.

    Returns `{"reason": ..., "opens_at": timestamp | None}`. Tokens
    that aren't an enrolled participant's period 2 are never gated, and
    a first session that was cancelled (token voided / expired unused)
    doesn't hold the second one up.
    """
    if token_row.get("period") != 2 or token_row.get("participant_id") is None:
        return None
    first = find_participant_period_token(con, token_row["participant_id"], 1)
    if first is None or first["status"] in ("voided", "expired"):
        return None
    if first["session_id"] is None:
        return {"reason": "first_not_started", "opens_at": None}

    config = find_study_config(con, token_row["study_id"]) or {}
    gap = int(config.get("min_gap_hours", 0) or 0)
    # `closed_at` is a naive TIMESTAMP, and DuckDB fills such columns
    # from CURRENT_TIMESTAMP in the *server's local* time zone — not UTC.
    # So the opening time is computed as a true instant (TIMESTAMPTZ,
    # which also keeps the gap exact across a DST change) and handed back
    # as an aware UTC datetime: what a participant is told must be right
    # whatever zone the server happens to run in.
    from datetime import UTC, datetime

    row = con.execute(
        "SELECT state, closed_at, "
        "epoch(timezone(current_setting('TimeZone'), closed_at) + to_hours(?)), "
        "timezone(current_setting('TimeZone'), closed_at) + to_hours(?) <= CURRENT_TIMESTAMP "
        "FROM sessions WHERE id = ?",
        [gap, gap, first["session_id"]],
    ).fetchone()
    if row is None:
        return None
    state, closed_at, opens_epoch, open_now = row
    if closed_at is None:
        return {"reason": "first_still_open", "opens_at": None, "first_state": state}
    if not open_now:
        return {"reason": "too_soon", "opens_at": datetime.fromtimestamp(opens_epoch, UTC)}
    return None


# ─── Study texts (the judged artifact) ────────────────────────────────


def session_state_elapsed_seconds(con, session_id: int) -> int | None:
    """Whole seconds the session has been in its current state, by the
    database clock on both ends (so no time-zone or client-clock skew).
    In the `active` state this is time on task."""
    row = con.execute(
        "SELECT date_diff('second', state_changed_at, CAST(CURRENT_TIMESTAMP AS TIMESTAMP)) "
        "FROM sessions WHERE id = ?",
        [session_id],
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return max(0, int(row[0]))


def create_study_text(
    con,
    *,
    session_id: int,
    actor_id: int,
    condition: str,
    topic_title: str,
    content: str,
    word_count: int,
    active_elapsed_seconds: int | None,
) -> int | None:
    """Store a session's submitted text. Written once: returns the new
    row id, or None if the session already has one (a double-clicked
    Finish must not replace what was first submitted)."""
    if find_study_text_for_session(con, session_id) is not None:
        return None
    row = con.execute(
        "INSERT INTO study_texts (session_id, actor_id, condition, topic_title, "
        "content, word_count, char_count, active_elapsed_seconds) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
        [
            session_id,
            actor_id,
            condition,
            topic_title,
            content,
            word_count,
            len(content),
            active_elapsed_seconds,
        ],
    ).fetchone()
    return int(row[0])


_STUDY_TEXT_COLUMNS = (
    "id, session_id, actor_id, condition, topic_title, content, word_count, "
    "char_count, active_elapsed_seconds, submitted_at"
)


def _row_to_study_text(row) -> dict:
    return {
        "id": row[0],
        "session_id": row[1],
        "actor_id": row[2],
        "condition": row[3],
        "topic_title": row[4] or "",
        "content": row[5],
        "word_count": row[6],
        "char_count": row[7],
        "active_elapsed_seconds": row[8],
        "submitted_at": row[9],
    }


def find_study_text_for_session(con, session_id: int) -> dict | None:
    row = con.execute(
        f"SELECT {_STUDY_TEXT_COLUMNS} FROM study_texts WHERE session_id = ?", [session_id]
    ).fetchone()
    return _row_to_study_text(row) if row else None


def list_study_texts(con, *, study_id: str | None = None) -> list[dict]:
    """Submitted texts, oldest first; optionally one study's only."""
    if study_id is None:
        rows = con.execute(f"SELECT {_STUDY_TEXT_COLUMNS} FROM study_texts ORDER BY id").fetchall()
    else:
        cols = ", ".join(f"x.{c.strip()}" for c in _STUDY_TEXT_COLUMNS.split(","))
        rows = con.execute(
            f"SELECT {cols} FROM study_texts x "
            "JOIN sessions s ON s.id = x.session_id "
            "JOIN participant_session_tokens t ON t.token = s.study_token "
            "WHERE t.study_id = ? ORDER BY x.id",
            [study_id],
        ).fetchall()
    return [_row_to_study_text(r) for r in rows]


# ─── Text judging (migration 0011) ────────────────────────────────────


def list_judges(con) -> list[dict]:
    """Active judge accounts — what a researcher needs to assign work.
    (The full user list is admin-only; this is the narrow slice.)"""
    rows = con.execute(
        "SELECT id, display_name, email FROM actors "
        "WHERE kind = 'judge' AND deactivated_at IS NULL ORDER BY id"
    ).fetchall()
    return [{"id": r[0], "display_name": r[1], "email": r[2]} for r in rows]


def create_text_assignment(
    con, *, study_id: str, text_id: int, judge_actor_id: int, assigned_by: int, position: float
) -> int | None:
    """Assign one text to one judge. Returns the new id, or None if
    that judge already has that text (assigning is idempotent, so
    "assign everything" can be pressed again after more texts come in)."""
    existing = con.execute(
        "SELECT id FROM text_assignments WHERE text_id = ? AND judge_actor_id = ?",
        [text_id, judge_actor_id],
    ).fetchone()
    if existing is not None:
        return None
    row = con.execute(
        "INSERT INTO text_assignments (study_id, text_id, judge_actor_id, assigned_by, position) "
        "VALUES (?, ?, ?, ?, ?) RETURNING id",
        [study_id, text_id, judge_actor_id, assigned_by, position],
    ).fetchone()
    return int(row[0])


_TEXT_ASSIGNMENT_COLUMNS = (
    "id, study_id, text_id, judge_actor_id, assigned_by, assigned_at, position, "
    "status, completed_at"
)


def _row_to_text_assignment(row) -> dict:
    return {
        "id": row[0],
        "study_id": row[1],
        "text_id": row[2],
        "judge_actor_id": row[3],
        "assigned_by": row[4],
        "assigned_at": row[5],
        "position": row[6],
        "status": row[7],
        "completed_at": row[8],
    }


def find_text_assignment(con, assignment_id: int) -> dict | None:
    row = con.execute(
        f"SELECT {_TEXT_ASSIGNMENT_COLUMNS} FROM text_assignments WHERE id = ?", [assignment_id]
    ).fetchone()
    return _row_to_text_assignment(row) if row else None


def list_text_assignments_for_judge(con, judge_actor_id: int) -> list[dict]:
    """A judge's queue, in that judge's own random order."""
    rows = con.execute(
        f"SELECT {_TEXT_ASSIGNMENT_COLUMNS} FROM text_assignments "
        "WHERE judge_actor_id = ? ORDER BY position, id",
        [judge_actor_id],
    ).fetchall()
    return [_row_to_text_assignment(r) for r in rows]


def list_text_assignments_for_study(con, study_id: str) -> list[dict]:
    rows = con.execute(
        f"SELECT {_TEXT_ASSIGNMENT_COLUMNS} FROM text_assignments WHERE study_id = ? ORDER BY id",
        [study_id],
    ).fetchall()
    return [_row_to_text_assignment(r) for r in rows]


def find_study_text(con, text_id: int) -> dict | None:
    row = con.execute(
        f"SELECT {_STUDY_TEXT_COLUMNS} FROM study_texts WHERE id = ?", [text_id]
    ).fetchone()
    return _row_to_study_text(row) if row else None


def record_text_rating(
    con,
    *,
    assignment_id: int,
    rubric_version: str,
    ratings: dict,
    justification: str,
    condition_guess: str | None,
    confidence: int | None,
    seconds_spent: int | None,
) -> int:
    """Store a submission and mark the assignment completed. Every
    submission is kept; `latest_text_rating` is the one that counts."""
    import json

    row = con.execute(
        "INSERT INTO text_ratings (assignment_id, rubric_version, ratings, justification, "
        "condition_guess, confidence, seconds_spent) VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id",
        [
            assignment_id,
            rubric_version,
            json.dumps(ratings, sort_keys=True),
            justification,
            condition_guess,
            confidence,
            seconds_spent,
        ],
    ).fetchone()
    con.execute(
        "UPDATE text_assignments SET status = 'completed', completed_at = CURRENT_TIMESTAMP "
        "WHERE id = ?",
        [assignment_id],
    )
    return int(row[0])


def list_text_ratings(con, assignment_id: int) -> list[dict]:
    """Every submission for an assignment, oldest first."""
    import json

    rows = con.execute(
        "SELECT id, assignment_id, rubric_version, ratings, justification, condition_guess, "
        "confidence, seconds_spent, submitted_at FROM text_ratings "
        "WHERE assignment_id = ? ORDER BY id",
        [assignment_id],
    ).fetchall()
    return [
        {
            "id": r[0],
            "assignment_id": r[1],
            "rubric_version": r[2],
            "ratings": json.loads(r[3]),
            "justification": r[4] or "",
            "condition_guess": r[5],
            "confidence": r[6],
            "seconds_spent": r[7],
            "submitted_at": r[8],
        }
        for r in rows
    ]


def latest_text_rating(con, assignment_id: int) -> dict | None:
    ratings = list_text_ratings(con, assignment_id)
    return ratings[-1] if ratings else None


# ─── Usage / cost tracking ────────────────────────────────────────────


def record_usage(
    con,
    *,
    actor_id: int | None,
    base_id: str | None,
    model: str,
    category: str,
    prompt_tokens: int,
    completion_tokens: int,
    cost_usd: float,
    attempts: int,
    latency_ms: int,
    purpose: str = "",
) -> int:
    """Insert one row into `usage`. Returns the new row's id.

    `actor_id` and `base_id` are nullable — system calls (summaries,
    batch jobs) may have neither. `category` is the `ChatCategory`
    string value; failure rows are recorded too so the dashboard can
    surface error rates alongside cost. `purpose` says what the call
    was for (`costs.PURPOSES`). `cost_usd` is the estimate at the time
    of the call and is kept for the record only — every reader prices
    the tokens itself (see `costs.py`)."""
    row = con.execute(
        "INSERT INTO usage "
        "(actor_id, base_id, model, category, prompt_tokens, "
        "completion_tokens, cost_usd, attempts, latency_ms, purpose) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
        [
            actor_id,
            base_id,
            model,
            category,
            prompt_tokens,
            completion_tokens,
            cost_usd,
            attempts,
            latency_ms,
            purpose,
        ],
    ).fetchone()
    return int(row[0]) if row else -1


def _usage_bucket(groups: list[dict]) -> dict:
    """The rollup shape the usage helpers return, from priced groups."""
    from .. import costs

    return costs.summarize(groups)


def total_cost(con, *, since: str | None = None, until: str | None = None) -> dict:
    """Cost + token counts over a time window (ISO timestamps or None
    to mean unbounded on that end), priced at read time. Returns
    {cost_usd, prompt_tokens, completion_tokens, calls,
    successful_calls, unpriced_tokens}."""
    from .. import costs

    clauses = []
    params: list = []
    if since is not None:
        clauses.append("occurred_at >= ?")
        params.append(since)
    if until is not None:
        clauses.append("occurred_at < ?")
        params.append(until)
    groups = costs.priced_groups(con, where=" AND ".join(clauses) or None, params=params)
    return _usage_bucket(groups)


def daily_cost(con, *, days: int = 30) -> list[dict]:
    """Per-day cost rollup for the last `days` days, newest first,
    priced at read time. Days without calls are omitted."""
    from .. import costs

    groups = costs.priced_groups(
        con, where="occurred_at >= CURRENT_TIMESTAMP - INTERVAL (?) DAY", params=[days]
    )
    return [
        {
            "day": day.isoformat(),
            "cost_usd": b["cost_usd"],
            "tokens": b["prompt_tokens"] + b["completion_tokens"],
            "calls": b["calls"],
        }
        for day, b in sorted(costs.rollup(groups, "day").items(), reverse=True)
    ]


def usage_for_base(con, base_id: str) -> dict:
    """Per-base integrity rollup over the entire history of the base.

    Returns:
      * `total`: aggregate cost + tokens + call counts (success vs all).
      * `by_category`: per-ChatCategory breakdown of call counts +
        total cost. Captures the failure profile (rate_limit storms,
        token-overflow incidents, etc.) alongside successful calls.
      * `latency_ms`: median + p95 over all calls (DuckDB
        `quantile_cont` — exact, fine at our scale).
      * `attempts`: mean attempts per call (>1 = retries fired).
      * `first_call_at` / `last_call_at`: span of activity.
    """
    from .. import costs

    groups = costs.priced_groups(con, where="base_id = ?", params=[base_id])
    total = _usage_bucket(groups)
    by_category = [
        {"category": c, "calls": b["calls"], "cost_usd": b["cost_usd"]}
        for c, b in sorted(costs.rollup(groups, "category").items())
    ]

    latency_row = con.execute(
        "SELECT "
        "quantile_cont(latency_ms, 0.5) AS p50, "
        "quantile_cont(latency_ms, 0.95) AS p95, "
        "AVG(attempts) AS mean_attempts, "
        "MIN(occurred_at) AS first_at, "
        "MAX(occurred_at) AS last_at "
        "FROM usage WHERE base_id = ?",
        [base_id],
    ).fetchone()
    if latency_row is None or latency_row[3] is None:
        latency = {"median_ms": 0, "p95_ms": 0}
        mean_attempts = 0.0
        first_at = None
        last_at = None
    else:
        latency = {
            "median_ms": int(latency_row[0] or 0),
            "p95_ms": int(latency_row[1] or 0),
        }
        mean_attempts = float(latency_row[2] or 0.0)
        first_at = str(latency_row[3]) if latency_row[3] is not None else None
        last_at = str(latency_row[4]) if latency_row[4] is not None else None

    return {
        "total": total,
        "by_category": by_category,
        "latency_ms": latency,
        "mean_attempts": mean_attempts,
        "first_call_at": first_at,
        "last_call_at": last_at,
    }


def total_cost_for_base(con, base_id: str) -> dict:
    """Like `total_cost` but filtered to one base. Lives next to the
    other rollups so the integrity report can fetch both with the
    same locking discipline."""
    from .. import costs

    return _usage_bucket(costs.priced_groups(con, where="base_id = ?", params=[base_id]))


def cost_by_actor(con, *, since: str | None = None) -> list[dict]:
    """Per-actor cost rollup, priced at read time, joined to
    actors.email for readability. Includes a NULL bucket for system
    calls. Most expensive first."""
    from .. import costs

    groups = costs.priced_groups(
        con,
        where="occurred_at >= ?" if since is not None else None,
        params=[since] if since is not None else None,
    )
    actors = {
        r[0]: (r[1], r[2])
        for r in con.execute("SELECT id, email, display_name FROM actors").fetchall()
    }
    rows = [
        {
            "actor_id": actor_id,
            "email": actors.get(actor_id, (None, None))[0],
            "display_name": actors.get(actor_id, (None, None))[1],
            "cost_usd": b["cost_usd"],
            "tokens": b["prompt_tokens"] + b["completion_tokens"],
            "calls": b["calls"],
        }
        for actor_id, b in costs.rollup(groups, "actor_id").items()
    ]
    return sorted(rows, key=lambda r: -r["cost_usd"])
