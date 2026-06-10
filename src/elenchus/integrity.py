"""
integrity.py — per-base integrity reports.

Combines two data sources that live in different DuckDB files:

  * `usage` (in `platform.duckdb`) — every LLM call recorded by
    `pricing.compute_cost` + `pdb.record_usage`. Gives us call counts
    by category, p50/p95 latency, total cost, mean attempts.

  * The per-base DB (`bases/{owner_id}/{name}.duckdb`) — atoms,
    positions, tensions, assessments, conversation. Gives us
    content metrics: |C|, |D|, |I|, atoms introduced, tensions
    accepted vs contested, retracted propositions, conversation
    turns.

The output is a structured dict the admin dashboard can render and
the Sloan study's per-session integrity check can consume verbatim.

`compute_base_integrity(base_id)` is the single entry point. Failures
during content-metric collection (e.g. a corrupt per-base file) are
caught and surfaced under `content.error` rather than propagating —
the usage-side numbers should still be visible even if the per-base
file is unreadable.
"""

from __future__ import annotations

import logging

from .db import get_registry
from .db import platform as pdb

logger = logging.getLogger(__name__)


def compute_base_integrity(base_id: str) -> dict:
    """Build the integrity report for one base.

    Always returns a dict — `usage` is empty if no LLM calls have
    been recorded against the base, and `content` has an `error`
    field if the per-base DB couldn't be opened.
    """
    reg = get_registry()
    con = reg.platform_con()

    base = pdb.find_base(con, base_id)
    usage = pdb.usage_for_base(con, base_id)
    content = _content_metrics(reg, base_id)

    return {
        "base_id": base_id,
        "owner_id": base["owner_id"] if base else None,
        "registered": base is not None,
        "usage": usage,
        "content": content,
    }


def list_base_integrity_summaries() -> list[dict]:
    """Compact summary of every registered base — one row per base
    suitable for an admin dashboard listing. Avoids the per-base file
    open cost (which would multiply by N for a many-base deployment);
    counts come from the usage table only."""
    reg = get_registry()
    con = reg.platform_con()
    bases = pdb.list_bases(con)
    out: list[dict] = []
    for b in bases:
        u = pdb.total_cost_for_base(con, b["id"])
        out.append(
            {
                "base_id": b["id"],
                "name": b["name"],
                "owner_id": b["owner_id"],
                "calls": u["calls"],
                "successful_calls": u["successful_calls"],
                "cost_usd": u["cost_usd"],
                "tokens": u["prompt_tokens"] + u["completion_tokens"],
            }
        )
    out.sort(key=lambda r: r["cost_usd"], reverse=True)
    return out


# ── Content metrics ──────────────────────────────────────────────────


def _content_metrics(reg, base_id: str) -> dict:
    """Open the per-base DB and pull the dialectical structural metrics.

    Catches any error from the registry / migration / corrupt-file path
    and reports it under `error` so the rest of the integrity report
    is still useful even if the per-base file is broken."""
    try:
        state = reg.get(base_id)
    except FileNotFoundError:
        return {"error": "base file not found"}
    except ValueError as e:
        return {"error": f"base file unreadable: {e}"}
    except Exception as e:
        logger.exception("integrity: unexpected error opening base %r", base_id)
        return {"error": f"open failed: {e}"}

    con = state.base.con
    try:
        # |C|, |D|, retracted positions.
        c_open, d_open, retracted = con.execute(
            "SELECT "
            "COUNT(*) FILTER (WHERE side='C' AND status='open'), "
            "COUNT(*) FILTER (WHERE side='D' AND status='open'), "
            "COUNT(DISTINCT atom) FILTER (WHERE status='retracted') "
            "FROM positions"
        ).fetchone() or (0, 0, 0)

        # Tensions by status.
        open_t, accepted_t, contested_t = con.execute(
            "SELECT "
            "COUNT(*) FILTER (WHERE status='open'), "
            "COUNT(*) FILTER (WHERE status='accepted'), "
            "COUNT(*) FILTER (WHERE status='contested') "
            "FROM tensions"
        ).fetchone() or (0, 0, 0)

        # Implications: active vs retracted. `current_assessments`
        # already filters on status='active'; raw `assessments` lets
        # us count the dropped ones too.
        active_impls = con.execute(
            "SELECT COUNT(*) FROM current_assessments WHERE judgment='holds'"
        ).fetchone()
        active_impls_count = int(active_impls[0] if active_impls else 0)

        retracted_impls = con.execute(
            "SELECT COUNT(*) FROM assessments WHERE judgment='holds' AND status='retracted'"
        ).fetchone()
        retracted_impls_count = int(retracted_impls[0] if retracted_impls else 0)

        # Atoms in L_B.
        atoms_count = con.execute("SELECT COUNT(*) FROM atoms").fetchone()[0] or 0

        # Conversation turns (user + assistant). Useful for the
        # "did the participant engage" sanity check.
        conv_row = con.execute(
            "SELECT "
            "COUNT(*) FILTER (WHERE role='user'), "
            "COUNT(*) FILTER (WHERE role='assistant') "
            "FROM conversation"
        ).fetchone() or (0, 0)
        user_turns, assistant_turns = int(conv_row[0]), int(conv_row[1])
    except Exception as e:
        logger.exception("integrity: failed reading content metrics for %r", base_id)
        return {"error": f"content read failed: {e}"}

    return {
        "atoms": int(atoms_count),
        "position": {
            "commitments": int(c_open),
            "denials": int(d_open),
            "retracted_propositions": int(retracted),
        },
        "tensions": {
            "open": int(open_t),
            "accepted": int(accepted_t),
            "contested": int(contested_t),
        },
        "implications": {
            "active": active_impls_count,
            "retracted": retracted_impls_count,
        },
        "conversation": {
            "user_turns": user_turns,
            "assistant_turns": assistant_turns,
        },
    }
