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
) -> None:
    """Insert one participant_session_tokens row. The caller has
    already validated condition; the DB has its own CHECK as a
    backstop."""
    con.execute(
        "INSERT INTO participant_session_tokens "
        "(token, actor_id, study_id, condition, issued_by, "
        "scheduled_start, scheduled_end, notes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            token,
            actor_id,
            study_id,
            condition,
            issued_by,
            scheduled_start,
            scheduled_end,
            notes,
        ],
    )


def find_participant_token(con, token: str) -> dict | None:
    """Look up a participant token. Returns None if not found.

    The result includes the token's status and used_at so callers can
    decide whether it's still consumable without re-querying."""
    row = con.execute(
        "SELECT token, actor_id, study_id, condition, scheduled_start, "
        "scheduled_end, issued_by, issued_at, used_at, session_id, "
        "status, notes "
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
        f"status, notes "
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
        }
        for r in rows
    ]


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
) -> int:
    """Insert one row into `usage`. Returns the new row's id.

    `actor_id` and `base_id` are nullable — system calls (summaries,
    batch jobs) may have neither. `category` is the `ChatCategory`
    string value; failure rows are recorded too so the dashboard can
    surface error rates alongside cost."""
    row = con.execute(
        "INSERT INTO usage "
        "(actor_id, base_id, model, category, prompt_tokens, "
        "completion_tokens, cost_usd, attempts, latency_ms) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
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
        ],
    ).fetchone()
    return int(row[0]) if row else -1


def total_cost(con, *, since: str | None = None, until: str | None = None) -> dict:
    """Sum cost + token counts over a time window (ISO timestamps or
    None to mean unbounded on that end). Returns
    {cost_usd, prompt_tokens, completion_tokens, calls,
    successful_calls}."""
    clauses = []
    params: list = []
    if since is not None:
        clauses.append("occurred_at >= ?")
        params.append(since)
    if until is not None:
        clauses.append("occurred_at < ?")
        params.append(until)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    row = con.execute(
        f"SELECT COALESCE(SUM(cost_usd), 0), "
        f"COALESCE(SUM(prompt_tokens), 0), "
        f"COALESCE(SUM(completion_tokens), 0), "
        f"COUNT(*), "
        f"COUNT(*) FILTER (WHERE category = 'success') "
        f"FROM usage {where}",
        params,
    ).fetchone()
    return {
        "cost_usd": float(row[0] or 0.0),
        "prompt_tokens": int(row[1] or 0),
        "completion_tokens": int(row[2] or 0),
        "calls": int(row[3] or 0),
        "successful_calls": int(row[4] or 0),
    }


def daily_cost(con, *, days: int = 30) -> list[dict]:
    """Per-day cost rollup for the last `days` days, newest first."""
    rows = con.execute(
        "SELECT CAST(occurred_at AS DATE) AS day, "
        "SUM(cost_usd), SUM(prompt_tokens) + SUM(completion_tokens), "
        "COUNT(*) "
        "FROM usage "
        "WHERE occurred_at >= CURRENT_TIMESTAMP - INTERVAL (?) DAY "
        "GROUP BY day ORDER BY day DESC",
        [days],
    ).fetchall()
    return [
        {
            "day": str(r[0]),
            "cost_usd": float(r[1] or 0.0),
            "tokens": int(r[2] or 0),
            "calls": int(r[3] or 0),
        }
        for r in rows
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
    total = total_cost_for_base(con, base_id)

    rows = con.execute(
        "SELECT category, COUNT(*), SUM(cost_usd) "
        "FROM usage WHERE base_id = ? "
        "GROUP BY category ORDER BY category",
        [base_id],
    ).fetchall()
    by_category = [
        {"category": r[0], "calls": int(r[1]), "cost_usd": float(r[2] or 0.0)} for r in rows
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
    row = con.execute(
        "SELECT COALESCE(SUM(cost_usd), 0), "
        "COALESCE(SUM(prompt_tokens), 0), "
        "COALESCE(SUM(completion_tokens), 0), "
        "COUNT(*), "
        "COUNT(*) FILTER (WHERE category = 'success') "
        "FROM usage WHERE base_id = ?",
        [base_id],
    ).fetchone()
    return {
        "cost_usd": float(row[0] or 0.0),
        "prompt_tokens": int(row[1] or 0),
        "completion_tokens": int(row[2] or 0),
        "calls": int(row[3] or 0),
        "successful_calls": int(row[4] or 0),
    }


def cost_by_actor(con, *, since: str | None = None) -> list[dict]:
    """Per-actor cost rollup, joined to actors.email for readability.
    Includes a NULL bucket for system calls."""
    clauses = []
    params: list = []
    if since is not None:
        clauses.append("u.occurred_at >= ?")
        params.append(since)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = con.execute(
        f"SELECT u.actor_id, a.email, a.display_name, "
        f"SUM(u.cost_usd), "
        f"SUM(u.prompt_tokens) + SUM(u.completion_tokens), "
        f"COUNT(*) "
        f"FROM usage u "
        f"LEFT JOIN actors a ON a.id = u.actor_id "
        f"{where} "
        f"GROUP BY u.actor_id, a.email, a.display_name "
        f"ORDER BY SUM(u.cost_usd) DESC",
        params,
    ).fetchall()
    return [
        {
            "actor_id": r[0],
            "email": r[1],
            "display_name": r[2],
            "cost_usd": float(r[3] or 0.0),
            "tokens": int(r[4] or 0),
            "calls": int(r[5] or 0),
        }
        for r in rows
    ]
