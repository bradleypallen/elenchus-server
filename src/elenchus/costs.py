"""
costs.py — what the platform's LLM calls cost, computed when asked.

The `usage` table (platform migration 0002, `purpose` from 0012) holds
one row per LLM call with its token counts. This module turns those
tokens into dollars **at read time**, against the dated price table in
`pricing.py` — the stored `usage.cost_usd` is never summed, because it
is only as good as the price table was on the day of the call (for
months it recorded $0 for every current model). Fix a rate and every
figure, historical ones included, corrects itself; a model with no
rate is reported as *unpriced*, never as free.

One query fetches the usage rows grouped at the finest grain any view
needs (day × model × purpose × outcome × actor × base); each group is
priced once, at the rate in effect on its day; every view is a rollup
of those priced groups, so the views always agree with each other.

`build_report` is what `GET /api/admin/costs` and `elenchus costs`
return:

  * `totals`     — the window, the month to date, all time
  * `by_day` / `by_model` / `by_purpose` / `by_actor` — over the window
  * `waste`      — failed calls and retry attempts over the window
  * `unpriced`   — models with tokens but no rate (all time)
  * `studies`    — per study → condition → session, all time, with the
                   mean cost of a finished session and a projection for
                   the sessions still outstanding
  * `budget`     — spend against the configured LLM budget line

A study session is attributed through its **actor**: every participant
token owns its own passwordless actor, so all of that actor's calls —
practice base and task base alike — belong to that session. Calls on
the `practice-{session_id}` base are the tutorial's share.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta

from . import pricing

logger = logging.getLogger(__name__)


# usage.purpose vocabulary → the label the dashboard shows.
PURPOSES: dict[str, str] = {
    "dialectic_turn": "Elenchus turns",
    "baseline_turn": "Baseline chat turns",
    "rolling_summary": "Rolling context summaries",
    "report_summary": "PDF report summaries",
    "study_report": "Study reports (legacy)",
    "sim_persona": "Simulation personas",
    "": "Unlabelled (recorded before 0.5)",
}

# Sessions in these states have finished the LLM-assisted task, so
# their cost is final and they count towards the per-session mean.
FINISHED_STATES = ("post_session", "surveyed", "complete")
# Token statuses that can still produce spend.
OUTSTANDING_TOKEN_STATUSES = ("scheduled", "active")

BUDGET_SETTING_KEY = "cost_budget"


# ─── Priced groups ───────────────────────────────────────────────────


def _as_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def priced_groups(
    con,
    *,
    since: date | None = None,
    until: date | None = None,
    where: str | None = None,
    params: list | None = None,
) -> list[dict]:
    """The usage rows from `since` (inclusive) to `until` (exclusive),
    grouped by day × model × purpose × category × actor × base, each
    group priced at the rate in effect on its day. `where` / `params`
    add a further SQL condition on `usage` (e.g. `"base_id = ?"`)."""
    clauses: list[str] = [where] if where else []
    params = list(params or [])
    if since is not None:
        clauses.append("occurred_at >= ?")
        params.append(datetime.combine(since, datetime.min.time()))
    if until is not None:
        clauses.append("occurred_at < ?")
        params.append(datetime.combine(until, datetime.min.time()))
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = con.execute(
        "SELECT CAST(occurred_at AS DATE) AS day, model, COALESCE(purpose, '') AS purpose, "
        "category, actor_id, base_id, "
        "COUNT(*), COALESCE(SUM(prompt_tokens), 0), COALESCE(SUM(completion_tokens), 0), "
        "COALESCE(SUM(attempts), 0), COALESCE(SUM(latency_ms), 0) "
        f"FROM usage {where} "
        "GROUP BY day, model, COALESCE(purpose, ''), category, actor_id, base_id",
        params,
    ).fetchall()
    groups = []
    for r in rows:
        day = _as_date(r[0])
        prompt, completion = int(r[7]), int(r[8])
        rate = pricing.lookup_rate(r[1], day)
        if rate is None and (prompt or completion):
            pricing._warn_unknown_model(r[1])
        cost = (
            (prompt * rate.input_per_1m + completion * rate.output_per_1m) / 1_000_000.0
            if rate
            else 0.0
        )
        groups.append(
            {
                "day": day,
                "model": r[1],
                "purpose": r[2],
                "category": r[3],
                "actor_id": r[4],
                "base_id": r[5],
                "calls": int(r[6]),
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "attempts": int(r[9]),
                "latency_ms": int(r[10]),
                "priced": rate is not None,
                "cost_usd": cost,
            }
        )
    return groups


def _empty_bucket() -> dict:
    return {
        "cost_usd": 0.0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "calls": 0,
        "successful_calls": 0,
        "unpriced_tokens": 0,
    }


def _add(bucket: dict, g: dict) -> None:
    bucket["cost_usd"] += g["cost_usd"]
    bucket["prompt_tokens"] += g["prompt_tokens"]
    bucket["completion_tokens"] += g["completion_tokens"]
    bucket["calls"] += g["calls"]
    if g["category"] == "success":
        bucket["successful_calls"] += g["calls"]
    if not g["priced"]:
        bucket["unpriced_tokens"] += g["prompt_tokens"] + g["completion_tokens"]


def summarize(groups: list[dict]) -> dict:
    """One bucket over all of `groups`."""
    bucket = _empty_bucket()
    for g in groups:
        _add(bucket, g)
    return bucket


def rollup(groups: list[dict], key) -> dict:
    """`{key(group): bucket}` — `key` is a field name or a callable."""
    key_fn = key if callable(key) else (lambda g, _k=key: g[_k])
    out: dict = {}
    for g in groups:
        _add(out.setdefault(key_fn(g), _empty_bucket()), g)
    return out


# ─── Views ───────────────────────────────────────────────────────────


def _by_day(groups: list[dict], since: date | None, until: date) -> list[dict]:
    """One entry per calendar day, oldest first, gaps filled with zeros
    so a chart's x-axis is continuous."""
    buckets = rollup(groups, "day")
    if not buckets and since is None:
        return []
    start = since if since is not None else min(buckets)
    days = []
    d = start
    while d < until:
        days.append({"day": d.isoformat(), **buckets.get(d, _empty_bucket())})
        d += timedelta(days=1)
    return days


def _by_model(groups: list[dict], today: date) -> list[dict]:
    out = []
    for model, bucket in rollup(groups, "model").items():
        rate = pricing.lookup_rate(model, today)
        out.append(
            {
                "model": model,
                **bucket,
                "rate": (
                    {
                        "input_per_1m": rate.input_per_1m,
                        "output_per_1m": rate.output_per_1m,
                        "matched": rate.model,
                    }
                    if rate
                    else None
                ),
            }
        )
    return sorted(out, key=lambda r: (-r["cost_usd"], r["model"]))


def _by_purpose(groups: list[dict]) -> list[dict]:
    out = [
        {"purpose": p, "label": PURPOSES.get(p, p), **bucket}
        for p, bucket in rollup(groups, "purpose").items()
    ]
    return sorted(out, key=lambda r: (-r["cost_usd"], r["purpose"]))


def _by_actor(con, groups: list[dict]) -> list[dict]:
    buckets = rollup(groups, "actor_id")
    actors = {
        r[0]: {"email": r[1], "display_name": r[2], "kind": r[3]}
        for r in con.execute("SELECT id, email, display_name, kind FROM actors").fetchall()
    }
    out = []
    for actor_id, bucket in buckets.items():
        info = actors.get(actor_id, {"email": None, "display_name": None, "kind": None})
        out.append({"actor_id": actor_id, **info, **bucket})
    return sorted(out, key=lambda r: -r["cost_usd"])


def _waste(groups: list[dict]) -> dict:
    """Calls that produced nothing usable. Providers don't report the
    tokens of a failed attempt, so retries are counted, not priced."""
    failed = [g for g in groups if g["category"] != "success"]
    by_category = [
        {"category": c, "calls": b["calls"], "cost_usd": b["cost_usd"]}
        for c, b in sorted(rollup(failed, "category").items())
    ]
    calls = sum(g["calls"] for g in groups)
    attempts = sum(g["attempts"] for g in groups)
    return {
        "calls": calls,
        "failed_calls": sum(g["calls"] for g in failed),
        "failed_cost_usd": sum(g["cost_usd"] for g in failed),
        "by_category": by_category,
        # attempts counts the first try too; anything beyond one per
        # call is a retry.
        "retry_attempts": max(0, attempts - calls),
    }


def _unpriced(groups: list[dict]) -> list[dict]:
    out = [
        {
            "model": model,
            "calls": b["calls"],
            "prompt_tokens": b["prompt_tokens"],
            "completion_tokens": b["completion_tokens"],
        }
        for model, b in rollup([g for g in groups if not g["priced"]], "model").items()
        if b["prompt_tokens"] or b["completion_tokens"]
    ]
    return sorted(out, key=lambda r: -(r["prompt_tokens"] + r["completion_tokens"]))


# ─── Study rollup ────────────────────────────────────────────────────


def _study_sessions(con) -> list[dict]:
    """Every participant token with its session state and, where the
    person was enrolled, their participant code and period."""
    rows = con.execute(
        "SELECT t.study_id, t.actor_id, t.condition, t.status, t.session_id, t.period, "
        "p.participant_code, s.state "
        "FROM participant_session_tokens t "
        "LEFT JOIN sessions s ON s.id = t.session_id "
        "LEFT JOIN study_participants p ON p.id = t.participant_id "
        "ORDER BY t.study_id, p.participant_code NULLS LAST, t.period NULLS LAST, t.issued_at"
    ).fetchall()
    return [
        {
            "study_id": r[0],
            "actor_id": r[1],
            "condition": r[2],
            "token_status": r[3],
            "session_id": r[4],
            "period": r[5],
            "participant_code": r[6],
            "state": r[7],
        }
        for r in rows
    ]


def _studies(con, groups: list[dict]) -> list[dict]:
    by_actor: dict = {}
    for g in groups:
        by_actor.setdefault(g["actor_id"], []).append(g)

    studies: dict[str, dict] = {}
    for sess in _study_sessions(con):
        mine = by_actor.get(sess["actor_id"], [])
        practice_base = f"practice-{sess['session_id']}"
        practice = summarize([g for g in mine if g["base_id"] == practice_base])
        total = summarize(mine)
        finished = sess["state"] in FINISHED_STATES
        row = {
            "participant_code": sess["participant_code"],
            "period": sess["period"],
            "condition": sess["condition"],
            "session_id": sess["session_id"],
            "state": sess["state"],
            "token_status": sess["token_status"],
            "finished": finished,
            "cost_usd": total["cost_usd"],
            "practice_cost_usd": practice["cost_usd"],
            "task_cost_usd": total["cost_usd"] - practice["cost_usd"],
            "calls": total["calls"],
            "prompt_tokens": total["prompt_tokens"],
            "completion_tokens": total["completion_tokens"],
            "unpriced_tokens": total["unpriced_tokens"],
        }
        study = studies.setdefault(
            sess["study_id"], {"study_id": sess["study_id"], "sessions": [], "conditions": {}}
        )
        study["sessions"].append(row)

    for study in studies.values():
        projected_remaining = 0.0
        projectable = True
        for condition in ("elenchus", "baseline"):
            rows = [r for r in study["sessions"] if r["condition"] == condition]
            if not rows:
                continue
            done = [r for r in rows if r["finished"]]
            outstanding = [
                r
                for r in rows
                if not r["finished"] and r["token_status"] in OUTSTANDING_TOKEN_STATUSES
            ]
            mean = (sum(r["cost_usd"] for r in done) / len(done)) if done else None
            if outstanding and mean is None:
                projectable = False
            elif mean is not None:
                # An outstanding session that has already spent more
                # than the mean isn't projected to spend less.
                projected_remaining += sum(max(0.0, mean - r["cost_usd"]) for r in outstanding)
            study["conditions"][condition] = {
                "sessions": len(rows),
                "finished_sessions": len(done),
                "outstanding_sessions": len(outstanding),
                "cost_usd": sum(r["cost_usd"] for r in rows),
                "practice_cost_usd": sum(r["practice_cost_usd"] for r in rows),
                "task_cost_usd": sum(r["task_cost_usd"] for r in rows),
                "mean_cost_per_finished_session": mean,
                "calls": sum(r["calls"] for r in rows),
            }
        study["cost_usd"] = sum(r["cost_usd"] for r in study["sessions"])
        study["projected_remaining_usd"] = projected_remaining if projectable else None
        study["projected_total_usd"] = (
            study["cost_usd"] + projected_remaining if projectable else None
        )
    return sorted(studies.values(), key=lambda s: s["study_id"])


# ─── Budget ──────────────────────────────────────────────────────────


def get_budget(con) -> dict | None:
    """The configured LLM budget line, or None. Stored as JSON under
    `platform_settings['cost_budget']`."""
    from .db import platform as pdb

    raw = pdb.get_setting(con, BUDGET_SETTING_KEY)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("platform_settings[%s] is not valid JSON; ignoring", BUDGET_SETTING_KEY)
        return None


def validate_budget(payload: dict) -> dict:
    """Normalize a budget submitted by an admin. Raises ValueError with
    a message fit to show them."""
    try:
        amount = float(payload.get("llm_usd"))
    except (TypeError, ValueError):
        raise ValueError("The budget amount must be a number of US dollars.") from None
    if amount <= 0 or amount > 10_000_000:
        raise ValueError("The budget amount must be greater than zero.")
    try:
        start = date.fromisoformat(str(payload.get("period_start") or ""))
        end = date.fromisoformat(str(payload.get("period_end") or ""))
    except ValueError:
        raise ValueError("The budget period needs a start and an end date (YYYY-MM-DD).") from None
    if end <= start:
        raise ValueError("The budget period must end after it starts.")
    label = str(payload.get("label") or "").strip()[:80]
    return {
        "llm_usd": round(amount, 2),
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "label": label,
    }


def set_budget(con, payload: dict | None) -> dict | None:
    """Store (or, with None, clear) the budget line. Caller holds the
    platform lock."""
    from .db import platform as pdb

    if payload is None:
        pdb.set_setting(con, BUDGET_SETTING_KEY, "")
        return None
    budget = validate_budget(payload)
    pdb.set_setting(con, BUDGET_SETTING_KEY, json.dumps(budget))
    return budget


def _budget_status(budget: dict | None, all_groups: list[dict], today: date) -> dict | None:
    if not budget:
        return None
    start = date.fromisoformat(budget["period_start"])
    end = date.fromisoformat(budget["period_end"])
    in_period = [g for g in all_groups if start <= g["day"] <= end]
    spent = summarize(in_period)
    before = summarize([g for g in all_groups if g["day"] < start])
    length = max(1, (end - start).days + 1)
    elapsed = min(length, max(0, (today - start).days + 1))
    return {
        **budget,
        "spent_usd": spent["cost_usd"],
        "remaining_usd": budget["llm_usd"] - spent["cost_usd"],
        "pct_spent": 100.0 * spent["cost_usd"] / budget["llm_usd"],
        "pct_period_elapsed": 100.0 * elapsed / length,
        "unpriced_tokens": spent["unpriced_tokens"],
        # Spend before the period opened isn't charged to the line,
        # but it shouldn't vanish either.
        "spent_before_period_usd": before["cost_usd"],
    }


# ─── The report ──────────────────────────────────────────────────────


def build_report(con, *, days: int = 30, today: date | None = None) -> dict:
    """The cost dashboard's payload. `days` is the window for the
    by-day / by-model / by-purpose / by-actor / waste views (0 = all
    time); totals, studies, unpriced and budget don't depend on it."""
    today = today or date.today()
    until = today + timedelta(days=1)
    since = (today - timedelta(days=days - 1)) if days and days > 0 else None

    everything = priced_groups(con)
    window = [g for g in everything if since is None or g["day"] >= since]
    month_start = today.replace(day=1)

    report = {
        "generated_on": today.isoformat(),
        "prices_as_of": pricing.PRICES_AS_OF,
        "window": {"days": days if since else 0, "since": since.isoformat() if since else None},
        "totals": {
            "window": summarize(window),
            "month_to_date": summarize([g for g in everything if g["day"] >= month_start]),
            "all_time": summarize(everything),
        },
        "by_day": _by_day(window, since, until),
        "by_model": _by_model(window, today),
        "by_purpose": _by_purpose(window),
        "by_actor": _by_actor(con, window),
        "waste": _waste(window),
        "unpriced": _unpriced(everything),
        "studies": _studies(con, everything),
        "budget": _budget_status(get_budget(con), everything, today),
    }
    total = report["totals"]["all_time"]
    logger.info(
        "Cost report: all_time=$%.4f calls=%d window_days=%s window=$%.4f "
        "unpriced_models=%d unpriced_tokens=%d studies=%d",
        total["cost_usd"],
        total["calls"],
        report["window"]["days"] or "all",
        report["totals"]["window"]["cost_usd"],
        len(report["unpriced"]),
        total["unpriced_tokens"],
        len(report["studies"]),
    )
    return report


# ─── Plain-text rendering (`elenchus costs`) ─────────────────────────


def _usd(value: float | None) -> str:
    return "—" if value is None else f"${value:,.2f}"


def format_report(report: dict) -> str:
    """The report as plain text, for a terminal or a grant file."""
    t = report["totals"]
    window = report["window"]
    span = f"last {window['days']} days" if window["days"] else "all time"
    lines = [
        f"Elenchus LLM costs — {report['generated_on']} (prices as of {report['prices_as_of']})",
        "",
        f"  Month to date   {_usd(t['month_to_date']['cost_usd']):>12}   "
        f"{t['month_to_date']['calls']} calls",
        f"  {span.capitalize():<15} {_usd(t['window']['cost_usd']):>12}   {t['window']['calls']} calls",
        f"  All time        {_usd(t['all_time']['cost_usd']):>12}   {t['all_time']['calls']} calls",
    ]
    budget = report.get("budget")
    if budget:
        lines += [
            "",
            f"Budget {budget.get('label') or 'LLM API'}: {_usd(budget['spent_usd'])} of "
            f"{_usd(budget['llm_usd'])} spent ({budget['pct_spent']:.1f}%), "
            f"{budget['pct_period_elapsed']:.0f}% of {budget['period_start']} → "
            f"{budget['period_end']} elapsed",
        ]
    if report["unpriced"]:
        lines += ["", "UNPRICED MODELS — these tokens are NOT in any figure above:"]
        for u in report["unpriced"]:
            lines.append(
                f"  {u['model']}: {u['prompt_tokens']:,} in / {u['completion_tokens']:,} out "
                f"({u['calls']} calls) — add a rate with ELENCHUS_PRICING_JSON"
            )
    lines += ["", f"By model ({span}):"]
    for m in report["by_model"]:
        rate = m["rate"]
        rate_txt = (
            f"${rate['input_per_1m']:g}/${rate['output_per_1m']:g} per 1M" if rate else "UNPRICED"
        )
        lines.append(
            f"  {m['model']:<32} {_usd(m['cost_usd']):>10}  {m['prompt_tokens']:>11,} in "
            f"{m['completion_tokens']:>10,} out  {rate_txt}"
        )
    lines += ["", f"By purpose ({span}):"]
    for row in report["by_purpose"]:
        lines.append(f"  {row['label']:<36} {_usd(row['cost_usd']):>10}  {row['calls']} calls")
    w = report["waste"]
    lines += [
        "",
        f"Waste ({span}): {w['failed_calls']} failed of {w['calls']} calls, "
        f"{w['retry_attempts']} retry attempts",
    ]
    for study in report["studies"]:
        lines += ["", f"Study {study['study_id']}: {_usd(study['cost_usd'])}"]
        for condition, c in study["conditions"].items():
            lines.append(
                f"  {condition:<9} {c['finished_sessions']}/{c['sessions']} sessions finished, "
                f"{_usd(c['cost_usd'])} total, "
                f"{_usd(c['mean_cost_per_finished_session'])} per finished session"
            )
        if study["projected_total_usd"] is not None:
            lines.append(
                f"  projected total {_usd(study['projected_total_usd'])} "
                f"({_usd(study['projected_remaining_usd'])} still to come)"
            )
    return "\n".join(lines)
