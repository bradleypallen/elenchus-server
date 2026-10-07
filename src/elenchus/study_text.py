"""
study_text.py — the participant's written text and how it came to be.

In the pilot each participant writes a short prose introduction to a
topic while working with the LLM; that text is what the expert panel
rates. This module owns the per-base side of it: the append-only draft
history (`text_snapshots`) and the editor events a snapshot can't show
(`editor_events`) — migration base/0004. The submitted text's
platform-level row (`study_texts`) is written by `db/platform.py`.

Pastes are logged by **length and time only**. The point is to be able
to say how much of a finished text arrived by paste, not to keep a copy
of what was pasted (the snapshots before and after already bracket it).
"""

from __future__ import annotations

import logging
import re

from .turn_log import _decode, _json, _rows, now_utc

logger = logging.getLogger(__name__)

SNAPSHOT_SEQ = "text_snapshot_seq"
EDITOR_EVENT_SEQ = "editor_event_seq"

# Generous for a two-to-three paragraph text; the cap exists so a stuck
# key or a pasted document can't bloat the base file.
MAX_TEXT_CHARS = 20_000

SNAPSHOT_TRIGGERS = frozenset({"autosave", "blur", "paste", "positum", "submit", "timeout"})
# The two that end the task: pressed by the participant, or reached by
# the clock. Both are always stored, even when identical to the last
# draft, so the submitted text is a row of its own.
FINAL_TRIGGERS = frozenset({"submit", "timeout"})
# Stored even when identical to the last autosave, like the final ones:
# the positum row marks *that the dialogue began from this text*.
ALWAYS_STORED_TRIGGERS = FINAL_TRIGGERS | {"positum"}

# What the editor may report, and the payload keys kept for each. The
# allow-list is what enforces "length and time only" for pastes: a
# client that sent the pasted text would have it dropped here.
EDITOR_EVENT_PAYLOAD_KEYS = {
    "paste": ("length",),
    "soft_warning_shown": ("threshold_minutes",),
}
MAX_EVENTS_PER_REQUEST = 50

_WORD_RE = re.compile(r"\S+")
_SENTENCE_END_RE = re.compile(r"[.!?]+(?:\s|$)")

# The first draft is the positum (design-notes/text-as-positum.md): the
# dialogue cannot begin until the participant has written at least this
# much — enough to hold a position, not enough to eat the clock. Either
# bound suffices. The tutorial's bar is lower: it exists to show the
# mechanism, not to produce a text.
POSITUM_MIN_WORDS, POSITUM_MIN_SENTENCES = 50, 3
TUTORIAL_POSITUM_MIN_WORDS, TUTORIAL_POSITUM_MIN_SENTENCES = 20, 2


def word_count(text: str) -> int:
    return len(_WORD_RE.findall(text))


def sentence_count(text: str) -> int:
    """Sentences as a participant would count them: runs of text ended
    by . ! or ?, plus a trailing run with no terminator."""
    text = text.strip()
    if not text:
        return 0
    ends = len(_SENTENCE_END_RE.findall(text))
    return ends + (0 if _SENTENCE_END_RE.search(text[-2:] + " ") else 1)


def positum_minimum(*, tutorial: bool) -> dict:
    words, sentences = (
        (TUTORIAL_POSITUM_MIN_WORDS, TUTORIAL_POSITUM_MIN_SENTENCES)
        if tutorial
        else (POSITUM_MIN_WORDS, POSITUM_MIN_SENTENCES)
    )
    return {"words": words, "sentences": sentences}


def positum_ready(text: str, *, tutorial: bool) -> bool:
    """Whether a draft is enough to start the dialogue from."""
    m = positum_minimum(tutorial=tutorial)
    return word_count(text) >= m["words"] or sentence_count(text) >= m["sentences"]


def latest_snapshot(con) -> dict | None:
    row = con.execute(
        "SELECT id, at_utc, trigger, content, char_count, word_count "
        "FROM text_snapshots ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "at_utc": row[1],
        "trigger": row[2],
        "content": row[3],
        "char_count": row[4],
        "word_count": row[5],
    }


def find_snapshot(con, snapshot_id: int) -> dict | None:
    row = con.execute(
        "SELECT id, at_utc, trigger, content, char_count, word_count "
        "FROM text_snapshots WHERE id = ?",
        [snapshot_id],
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "at_utc": row[1],
        "trigger": row[2],
        "content": row[3],
        "char_count": row[4],
        "word_count": row[5],
    }


def positum_snapshot(con) -> dict | None:
    """The snapshot that opened the dialogue, or None if it hasn't."""
    row = con.execute(
        "SELECT id, at_utc, trigger, content, char_count, word_count "
        "FROM text_snapshots WHERE trigger = 'positum' ORDER BY id LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "at_utc": row[1],
        "trigger": row[2],
        "content": row[3],
        "char_count": row[4],
        "word_count": row[5],
    }


def save_snapshot(con, content: str, *, trigger: str, actor_id: int | None = None) -> dict:
    """Append a snapshot of the text and return it (without content).

    An autosave whose content equals the latest snapshot is not stored
    again — `stored` is False and the existing row is returned — so an
    idle editor doesn't pad the history. A 'submit' is always stored:
    it marks *that the participant submitted*, even if the text hadn't
    changed since the last autosave.
    """
    if trigger not in SNAPSHOT_TRIGGERS:
        raise ValueError(f"Unknown snapshot trigger: {trigger!r}")
    if len(content) > MAX_TEXT_CHARS:
        raise ValueError(f"Text is longer than {MAX_TEXT_CHARS} characters")

    previous = latest_snapshot(con)
    if (
        trigger not in ALWAYS_STORED_TRIGGERS
        and previous is not None
        and previous["content"] == content
    ):
        previous.pop("content")
        return {**previous, "stored": False}

    snapshot_id = con.execute(f"SELECT nextval('{SNAPSHOT_SEQ}')").fetchone()[0]
    at_utc = now_utc()
    words = word_count(content)
    con.execute(
        "INSERT INTO text_snapshots (id, at_utc, actor_id, trigger, content, "
        "char_count, word_count) VALUES (?,?,?,?,?,?,?)",
        [snapshot_id, at_utc, actor_id, trigger, content, len(content), words],
    )
    logger.info(
        "text_snapshot #%d: trigger=%s chars=%d words=%d (delta_chars=%+d)",
        snapshot_id,
        trigger,
        len(content),
        words,
        len(content) - (previous["char_count"] if previous else 0),
    )
    return {
        "id": snapshot_id,
        "at_utc": at_utc,
        "trigger": trigger,
        "char_count": len(content),
        "word_count": words,
        "stored": True,
    }


def record_editor_events(con, events: list[dict], *, actor_id: int | None = None) -> int:
    """Append editor events; returns how many were stored. Unknown
    event types are skipped (and logged), and only the allow-listed
    payload keys of a known type are kept."""
    stored = 0
    for event in events[:MAX_EVENTS_PER_REQUEST]:
        event_type = event.get("type")
        keys = EDITOR_EVENT_PAYLOAD_KEYS.get(event_type)
        if keys is None:
            logger.warning("editor_events: ignoring unknown event type %r", event_type)
            continue
        payload = {k: event[k] for k in keys if isinstance(event.get(k), int | float)}
        event_id = con.execute(f"SELECT nextval('{EDITOR_EVENT_SEQ}')").fetchone()[0]
        con.execute(
            "INSERT INTO editor_events (id, at_utc, actor_id, event_type, payload) "
            "VALUES (?,?,?,?,?)",
            [event_id, now_utc(), actor_id, event_type, _json(payload) or "{}"],
        )
        logger.info("editor_event #%d: %s %s", event_id, event_type, payload)
        stored += 1
    if len(events) > MAX_EVENTS_PER_REQUEST:
        logger.warning(
            "editor_events: request carried %d events; kept the first %d",
            len(events),
            MAX_EVENTS_PER_REQUEST,
        )
    return stored


# ── Readers (study export) ───────────────────────────────────────────


def list_snapshots(con) -> list[dict]:
    """Every snapshot, oldest first, with content."""
    return _rows(con, "SELECT * FROM text_snapshots ORDER BY id")


def list_editor_events(con) -> list[dict]:
    return [
        _decode(r, ("payload",)) for r in _rows(con, "SELECT * FROM editor_events ORDER BY id")
    ]
