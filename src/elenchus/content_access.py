"""
content_access.py — looking at a dialectic that isn't yours.

Only its owner opens a dialectic on the working routes; there is no
staff bypass (docs/data-access.md, design-notes/content-access-policy.md).
When an administrator does need to see someone else's dialectic it is a
separate, explicit act: **a stated reason, a read-only response, and a
row in an append-only log** — this module.

  * `validate_reason` — a category from `CATEGORIES` and a sentence.
  * `open_grant`      — records the reason; it then covers that actor and
                        that dialectic for `GRANT_MINUTES`, so reading it
                        and then taking the PDF isn't two interrogations.
  * `find_grant`      — a grant still valid for this actor and this base.
  * `record`          — one row per fetch (`view` / `pdf` / `records`).
  * `list_log`, `summary_by_base` — what the dashboard shows.
  * `build_records_archive` — the raw records of one dialectic as a
                        tar.gz, for the owner or for a logged admin
                        download: the same files a study session's
                        export directory holds.

Study records are never opened through here — the routes refuse them
before a grant is considered; what a study session produced reaches the
team through the study export.

Timestamps are explicit naive UTC (`now_utc`), not `CURRENT_TIMESTAMP`
(DuckDB fills that in the server's local zone).
"""

from __future__ import annotations

import io
import logging
import os
import tarfile
import tempfile
from datetime import UTC, datetime, timedelta

from .db.registry import sanitize_base_name

logger = logging.getLogger(__name__)

RECORDS_FORMAT = "elenchus-dialectic-records"
RECORDS_FORMAT_VERSION = "1"

GRANT_MINUTES = 30
REASON_MIN_CHARS = 10
REASON_MAX_CHARS = 500

FETCH_ACTIONS = ("view", "pdf", "records")

# The order is the order the prompt offers them in. The keys go in the
# log; the labels are what the administrator reads.
CATEGORIES: dict[str, str] = {
    "support_request": "Owner asked for help",
    "problem_report": "Investigating a reported problem",
    "owner_agreed_analysis": "Owner agreed to analysis",
    "policy_concern": "Abuse or policy concern",
    "other": "Other",
}


def now_utc() -> datetime:
    """Naive UTC, to the second — what the log stores."""
    return datetime.now(UTC).replace(tzinfo=None, microsecond=0)


def iso(ts: datetime | None) -> str | None:
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ") if ts is not None else None


def categories() -> list[dict]:
    return [{"value": k, "label": v} for k, v in CATEGORIES.items()]


def validate_reason(category, reason) -> tuple[str, str]:
    """A known category and a real sentence. Returns the cleaned pair;
    raises ValueError with a message fit to show the administrator."""
    if category not in CATEGORIES:
        raise ValueError("Choose what this is for.")
    text = " ".join(reason.split()) if isinstance(reason, str) else ""
    if len(text) < REASON_MIN_CHARS:
        raise ValueError(
            f"Say why in a sentence — at least {REASON_MIN_CHARS} characters. It is recorded."
        )
    if len(text) > REASON_MAX_CHARS:
        raise ValueError(f"Keep the reason under {REASON_MAX_CHARS} characters.")
    return category, text


def _row_to_entry(row) -> dict:
    return {
        "id": row[0],
        "at_utc": row[1],
        "actor_id": row[2],
        "action": row[3],
        "base_id": row[4],
        "owner_id": row[5],
        "base_kind": row[6],
        "category": row[7],
        "reason": row[8],
        "grant_id": row[9],
        "expires_at_utc": row[10],
    }


_COLUMNS = (
    "id, at_utc, actor_id, action, base_id, owner_id, base_kind, "
    "category, reason, grant_id, expires_at_utc"
)


def _insert(con, **fields) -> dict:
    entry_id = con.execute("SELECT nextval('content_access_log_seq')").fetchone()[0]
    con.execute(
        "INSERT INTO content_access_log (id, at_utc, actor_id, action, base_id, owner_id, "
        "base_kind, category, reason, grant_id, expires_at_utc) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            entry_id,
            fields["at_utc"],
            fields["actor_id"],
            fields["action"],
            fields["base_id"],
            fields.get("owner_id"),
            fields.get("base_kind", "ordinary"),
            fields["category"],
            fields["reason"],
            fields.get("grant_id"),
            fields.get("expires_at_utc"),
        ],
    )
    row = con.execute(
        f"SELECT {_COLUMNS} FROM content_access_log WHERE id = ?", [entry_id]
    ).fetchone()
    return _row_to_entry(row)


def open_grant(
    con,
    *,
    actor_id: int,
    base_id: str,
    owner_id: int | None,
    category: str,
    reason: str,
    base_kind: str = "ordinary",
) -> dict:
    """Record the reason. The caller has validated it and holds the
    platform lock."""
    at = now_utc()
    grant = _insert(
        con,
        at_utc=at,
        actor_id=actor_id,
        action="grant",
        base_id=base_id,
        owner_id=owner_id,
        base_kind=base_kind,
        category=category,
        reason=reason,
        expires_at_utc=at + timedelta(minutes=GRANT_MINUTES),
    )
    logger.warning(
        "Content access granted: actor=%s base=%r owner=%s category=%s reason=%r grant=%s",
        actor_id,
        base_id,
        owner_id,
        category,
        reason,
        grant["id"],
    )
    return grant


def find_grant(con, grant_id: int, *, actor_id: int, base_id: str) -> dict | None:
    """The grant, if it is this actor's, for this dialectic, and still
    in its window. Anything else is None — the caller asks again."""
    row = con.execute(
        f"SELECT {_COLUMNS} FROM content_access_log "
        "WHERE id = ? AND action = 'grant' AND actor_id = ? AND base_id = ?",
        [grant_id, actor_id, base_id],
    ).fetchone()
    if row is None:
        return None
    grant = _row_to_entry(row)
    if grant["expires_at_utc"] is None or grant["expires_at_utc"] <= now_utc():
        return None
    return grant


def record(con, *, grant: dict, action: str) -> dict:
    """One fetch under a grant. Carries the grant's category and reason
    so the row reads on its own."""
    if action not in FETCH_ACTIONS:
        raise ValueError(f"unknown content-access action: {action}")
    entry = _insert(
        con,
        at_utc=now_utc(),
        actor_id=grant["actor_id"],
        action=action,
        base_id=grant["base_id"],
        owner_id=grant["owner_id"],
        base_kind=grant["base_kind"],
        category=grant["category"],
        reason=grant["reason"],
        grant_id=grant["id"],
    )
    logger.warning(
        "Content access: actor=%s action=%s base=%r owner=%s grant=%s category=%s",
        grant["actor_id"],
        action,
        grant["base_id"],
        grant["owner_id"],
        grant["id"],
        grant["category"],
    )
    return entry


def list_log(con, *, limit: int = 200, fetches_only: bool = True) -> list[dict]:
    """Newest first, with the names an administrator reads it by."""
    where = "WHERE l.action <> 'grant'" if fetches_only else ""
    rows = con.execute(
        "SELECT l.id, l.at_utc, l.actor_id, l.action, l.base_id, l.owner_id, l.base_kind, "
        "l.category, l.reason, l.grant_id, l.expires_at_utc, "
        "a.display_name, o.display_name, o.email, b.name "
        "FROM content_access_log l "
        "LEFT JOIN actors a ON a.id = l.actor_id "
        "LEFT JOIN actors o ON o.id = l.owner_id "
        "LEFT JOIN bases b ON b.id = l.base_id "
        f"{where} ORDER BY l.id DESC LIMIT ?",
        [max(1, min(int(limit), 1000))],
    ).fetchall()
    out = []
    for row in rows:
        entry = _row_to_entry(row)
        out.append(
            {
                "id": entry["id"],
                "at_utc": iso(entry["at_utc"]),
                "action": entry["action"],
                "base_id": entry["base_id"],
                "base_name": row[14] or entry["base_id"],
                "base_exists": row[14] is not None,
                # 'ordinary', or 'study_dev' for a development study's record.
                "base_kind": entry["base_kind"] or "ordinary",
                "actor_id": entry["actor_id"],
                "actor_name": row[11] or f"actor {entry['actor_id']}",
                "owner_id": entry["owner_id"],
                "owner_name": row[12] or "",
                "owner_email": row[13] or "",
                "category": entry["category"],
                "category_label": CATEGORIES.get(entry["category"], entry["category"]),
                "reason": entry["reason"],
                "grant_id": entry["grant_id"],
            }
        )
    return out


def summary_by_base(con) -> dict[str, dict]:
    """Per dialectic: how many times it was fetched by a non-owner, and
    the last time — for the list's "viewed" column."""
    rows = con.execute(
        "SELECT l.base_id, COUNT(*), MAX(l.at_utc), "
        "arg_max(a.display_name, l.id) "
        "FROM content_access_log l LEFT JOIN actors a ON a.id = l.actor_id "
        "WHERE l.action <> 'grant' GROUP BY l.base_id"
    ).fetchall()
    return {
        base_id: {"count": int(count), "last_at_utc": iso(last), "last_by": last_by or ""}
        for base_id, count, last, last_by in rows
    }


def build_records_archive(reg, con, *, base: dict, exported_by: str) -> tuple[str, bytes]:
    """The raw records of one dialectic as a tar.gz: `(filename, bytes)`.

    The same files a study session's export directory holds — the state,
    the transcript, the turn log, the state events, the integrity report
    and a dump of the per-base database — under one top directory with a
    manifest. The owner appears as `OWNER`, not by name or id: who it
    belongs to is known to whoever asked for it and is in the access
    log, not in the archive.
    """
    from .integrity import compute_base_integrity
    from .study_export import _export_base, _pseudonymize, _versions, _write_json

    stamp = now_utc().strftime("%Y%m%dT%H%M%SZ")
    top = f"dialectic-{sanitize_base_name(base['id'])}-{stamp}"
    pseudonyms = {base["owner_id"]: "OWNER"} if base.get("owner_id") is not None else {}
    with tempfile.TemporaryDirectory() as tmp:
        dest = os.path.join(tmp, top)
        os.makedirs(dest)
        _write_json(
            os.path.join(dest, "manifest.json"),
            {
                "format": RECORDS_FORMAT,
                "format_version": RECORDS_FORMAT_VERSION,
                "exported_at_utc": iso(now_utc()),
                "exported_by": exported_by,
                "dialectic": {"id": base["id"], "name": base["name"]},
                "owner": "OWNER",
                "versions": _versions(con),
                "files": {
                    "state.json": "the position: commitments, denials, tensions, implications",
                    "transcript.json": "the conversation as shown",
                    "turn_log.json": "one row per exchange with the model: what was sent, "
                    "what came back verbatim, the model, latency, tokens",
                    "state_events.json": "every change to the position, with its source",
                    "integrity.json": "usage and content metrics",
                    "base/": "a dump of the per-dialectic database (schema.sql, load.sql, CSVs)",
                },
            },
        )
        _write_json(
            os.path.join(dest, "integrity.json"),
            _pseudonymize(compute_base_integrity(base["id"]), pseudonyms),
        )
        with reg.hold(base["id"], transient=True) as handle:
            _export_base(handle.state, dest, base["id"], pseudonyms)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            tar.add(dest, arcname=top)
    data = buf.getvalue()
    logger.info(
        "Built records archive for dialectic %r: %s (%d bytes), exported_by=%s",
        base["id"],
        top,
        len(data),
        exported_by,
    )
    return f"{top}.tar.gz", data
