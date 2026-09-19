"""
turn_log.py — append-only research capture for a dialectic.

The live tables hold the *current* dialectical state. Offline analysis
(NMMS derivability, translation to RDF, process measures) needs the
*history*: what the LLM was shown and what it literally said, which
recovery path parsed it, which state transitions each turn caused, and
which transitions came from a UI button instead. This module writes
that history into the per-base `turn_log` and `state_events` tables
(migration base/0003) and reads it back for the study export.

Anything not captured while a session runs is unrecoverable afterwards,
so the writers here are deliberately dumb: plain inserts, no updates,
ids from sequences (never MAX(id)+1, which can collide when a UI action
and an opponent turn write at once — and a constraint error inside the
opponent's transaction would abort the participant's whole turn).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

TURN_SEQ = "turn_log_seq"
EVENT_SEQ = "state_event_seq"


@dataclass(frozen=True)
class EventContext:
    """Who/what is behind a state transition.

    Passed explicitly down to the `DialecticalState` mutators rather
    than parked on the (shared, per-base) state object, so a UI action
    running concurrently with an opponent turn can't be mis-attributed
    to that turn.
    """

    source: str = "direct"  # 'opponent' | 'ui' | 'direct'
    turn_id: int | None = None
    actor_id: int | None = None


DIRECT = EventContext()


def now_utc() -> str:
    """Application-clock timestamp: ISO-8601, UTC, microseconds."""
    return datetime.now(UTC).isoformat(timespec="microseconds")


def prompt_fingerprint(prompt: str | None) -> str | None:
    """SHA-256 of a system prompt, so the export shows exactly which
    wording a session ran under (and a mid-study prompt edit is
    detectable) without storing the full prompt on every row."""
    if prompt is None:
        return None
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def _json(value) -> str | None:
    """Serialize for a JSON column. `default=str` so an unexpected type
    (a datetime, a set) degrades to its string form instead of raising
    — a capture failure must not take the participant's turn with it."""
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)


def next_turn_id(con) -> int:
    """Reserve a turn id up front, so the state events a turn causes can
    reference it before the turn row itself is written. A rolled-back
    turn just leaves a gap."""
    return con.execute(f"SELECT nextval('{TURN_SEQ}')").fetchone()[0]


def record_turn(
    con,
    *,
    turn_id: int | None = None,
    mode: str,
    outcome: str = "ok",
    user_message: str,
    actor_id: int | None = None,
    action_context: dict | None = None,
    request_content: str | None = None,
    history_window: int | None = None,
    summary_included: bool | None = None,
    system_prompt_name: str | None = None,
    system_prompt_sha256: str | None = None,
    state_before: dict | None = None,
    state_after: dict | None = None,
    raw_text: str | None = None,
    parse_strategy: str | None = None,
    parsed: dict | None = None,
    user_conversation_id: int | None = None,
    assistant_conversation_id: int | None = None,
    chat_result=None,
) -> int:
    """Insert one `turn_log` row and return its id.

    `chat_result` is the `llm_client.ChatResult` of the call, when the
    caller has it; model / latency / tokens / error fields come from it.
    """
    if turn_id is None:
        turn_id = next_turn_id(con)
    failed = chat_result is not None and not chat_result.ok
    con.execute(
        "INSERT INTO turn_log (id, at_utc, mode, outcome, actor_id, user_message, "
        "action_context, request_content, history_window, summary_included, "
        "system_prompt_name, system_prompt_sha256, state_before, state_after, "
        "raw_text, parse_strategy, parsed, user_conversation_id, "
        "assistant_conversation_id, model, attempts, latency_ms, prompt_tokens, "
        "completion_tokens, error_category, error_message) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            turn_id,
            now_utc(),
            mode,
            outcome,
            actor_id,
            user_message,
            _json(action_context),
            request_content,
            history_window,
            summary_included,
            system_prompt_name,
            system_prompt_sha256,
            _json(state_before),
            _json(state_after),
            raw_text,
            parse_strategy,
            _json(parsed),
            user_conversation_id,
            assistant_conversation_id,
            getattr(chat_result, "model", None) or None,
            getattr(chat_result, "attempts", None),
            getattr(chat_result, "latency_ms", None),
            getattr(chat_result, "prompt_tokens", None),
            getattr(chat_result, "completion_tokens", None),
            chat_result.category.value if failed else None,
            chat_result.error_message if failed else None,
        ],
    )
    logger.info(
        "turn_log #%d: mode=%s outcome=%s parse=%s model=%s latency_ms=%s tokens=%s/%s",
        turn_id,
        mode,
        outcome,
        parse_strategy,
        getattr(chat_result, "model", None),
        getattr(chat_result, "latency_ms", None),
        getattr(chat_result, "prompt_tokens", None),
        getattr(chat_result, "completion_tokens", None),
    )
    return turn_id


def record_state_event(
    con,
    event_type: str,
    payload: dict,
    *,
    event: EventContext | None = None,
    outcome: str = "applied",
    note: str = "",
) -> int:
    """Insert one `state_events` row and return its id."""
    ctx = event or DIRECT
    event_id = con.execute(f"SELECT nextval('{EVENT_SEQ}')").fetchone()[0]
    con.execute(
        "INSERT INTO state_events (id, at_utc, turn_id, source, actor_id, "
        "event_type, outcome, payload, note) VALUES (?,?,?,?,?,?,?,?,?)",
        [
            event_id,
            now_utc(),
            ctx.turn_id,
            ctx.source,
            ctx.actor_id,
            event_type,
            outcome,
            _json(payload) or "{}",
            note,
        ],
    )
    logger.debug(
        "state_event #%d: %s %s (source=%s turn=%s)",
        event_id,
        event_type,
        outcome,
        ctx.source,
        ctx.turn_id,
    )
    return event_id


# ── Readers (study export, integrity checks, offline tooling) ────────

_JSON_TURN_COLUMNS = ("action_context", "state_before", "state_after", "parsed")


def _rows(con, sql: str) -> list[dict]:
    cur = con.execute(sql)
    columns = [d[0] for d in cur.description]
    return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]


def _decode(row: dict, json_columns) -> dict:
    for col in json_columns:
        if isinstance(row.get(col), str):
            try:
                row[col] = json.loads(row[col])
            except json.JSONDecodeError:
                logger.warning("turn_log: undecodable JSON in column %r (row %s)", col, row["id"])
    return row


def list_turns(con) -> list[dict]:
    """Every `turn_log` row, oldest first, JSON columns decoded."""
    return [
        _decode(r, _JSON_TURN_COLUMNS) for r in _rows(con, "SELECT * FROM turn_log ORDER BY id")
    ]


def list_state_events(con) -> list[dict]:
    """Every `state_events` row, oldest first, payload decoded."""
    return [_decode(r, ("payload",)) for r in _rows(con, "SELECT * FROM state_events ORDER BY id")]
