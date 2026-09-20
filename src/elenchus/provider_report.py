"""
provider_report.py — the LLM provider's own books, for reconciliation.

The platform counts tokens as calls are made and prices them itself
(`costs.py`). The provider keeps its own record. Comparing the two —
per month and per model, tokens as well as dollars — is how these get
noticed:

  * a stale or wrong rate in `pricing.py` (tokens agree, dollars don't);
  * usage on the same key or workspace from outside the platform (the
    provider saw more tokens than the platform recorded);
  * calls that escaped recording, or prompt caching turned on without
    the `usage` table learning about cache tokens (same symptom);
  * a report that doesn't cover everything (the platform recorded more).

Two halves, deliberately on different machines:

**Fetching runs off the box.** Anthropic's usage and cost reports need
an *Admin* API key (`sk-ant-admin…`), which can manage the whole
organization — members, API keys. It has no business on a web server.
So `elenchus-provider-report` (this module's `main`) runs on an admin's
own machine, reads the key from `ANTHROPIC_ADMIN_KEY`, calls the two
documented endpoints —

    GET /v1/organizations/usage_report/messages   (tokens, per day × model)
    GET /v1/organizations/cost_report             (USD cents, per day × description)

— and writes a JSON file (`FORMAT`). The file holds figures only, never
the key. This half imports nothing that opens a database.

**Importing runs on the server.** An admin uploads the file (Costs tab,
`POST /api/admin/costs/provider-report`). `import_report` replaces the
stored rows for the days the file covers (platform migration `0014`) and
`reconciliation` sets them beside the platform's own figures for the
same days.

The cost report can be segmented by workspace but **not** by API key, so
clean numbers need a workspace used by nothing but this platform; the
file records the scope it was fetched under and the report shows it.
Provider days are UTC; run the server with `TZ=UTC` (docs/OPERATIONS.md)
so the platform's days line up.
"""

from __future__ import annotations

import argparse
import calendar
import json
import logging
import os
import re
import sys
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from . import pricing

logger = logging.getLogger(__name__)

FORMAT = "elenchus-provider-report/1"
API_BASE = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"
ADMIN_KEY_ENV = "ANTHROPIC_ADMIN_KEY"
USAGE_PATH = "/v1/organizations/usage_report/messages"
COST_PATH = "/v1/organizations/cost_report"
MAX_DAILY_BUCKETS = 31  # the documented maximum `limit` for 1d buckets
DEFAULT_WORKSPACE = "default"  # how a caller names the workspace whose id is null
MAX_ROWS = 50_000

# How far apart the two sets of books may be before it is worth saying.
TOKEN_TOLERANCE = 0.02
COST_TOLERANCE = 0.05
COST_FLOOR_USD = 0.05

_DATE_SUFFIX = re.compile(r"-\d{8}$")


class ProviderReportError(Exception):
    """A failure fit to show the person running the fetch or the upload."""


def canonical_model(model: str | None) -> str:
    """The name two sets of books can be matched on: normalized, with a
    trailing release date dropped (`claude-haiku-4-5-20251001` →
    `claude-haiku-4-5`). Only a date is stripped — never a version."""
    return _DATE_SUFFIX.sub("", pricing.normalize_model(model or ""))


# ─── Fetching (off the box; no database) ─────────────────────────────


def _rfc3339(d: date) -> str:
    return f"{d.isoformat()}T00:00:00Z"


def _raise_for_status(response) -> None:
    if response.status_code < 400:
        return
    try:
        detail = response.json().get("error", {}).get("message") or response.text
    except ValueError:
        detail = response.text
    if response.status_code in (401, 403):
        raise ProviderReportError(
            f"The Admin API refused the credential ({response.status_code}). These reports need "
            f"an Admin API key (sk-ant-admin…) in {ADMIN_KEY_ENV}; an ordinary API key doesn't "
            "work, and individual accounts can't create one (Console → Settings → Organization). "
            f"Provider said: {detail[:300]}"
        )
    raise ProviderReportError(f"Admin API error {response.status_code}: {detail[:300]}")


def _buckets(client, path: str, params: list[tuple[str, str]]) -> Iterator[dict]:
    """Every time bucket, following `has_more` / `next_page`."""
    page: str | None = None
    pages = 0
    while True:
        query = [*params, ("page", page)] if page else params
        response = client.get(path, params=query)
        _raise_for_status(response)
        body = response.json()
        pages += 1
        yield from body.get("data", [])
        page = body.get("next_page")
        if not body.get("has_more") or not page:
            logger.info("Fetched %s: %d page(s)", path, pages)
            return


def _cents_to_usd(amount) -> float:
    """`amount` is cents as a decimal string: "123.45" is $1.2345."""
    try:
        return float(Decimal(str(amount)) / Decimal(100))
    except (InvalidOperation, ValueError):
        raise ProviderReportError(
            f"Unreadable cost amount from the provider: {amount!r}"
        ) from None


def _in_scope(workspace_id, wanted: set[str]) -> bool:
    if not wanted:
        return True
    return (workspace_id or DEFAULT_WORKSPACE) in wanted


def fetch_anthropic(
    *,
    admin_key: str,
    start: date,
    end: date,
    workspace_ids: list[str] | None = None,
    api_key_ids: list[str] | None = None,
    transport=None,
    user_agent: str = "elenchus-provider-report",
) -> dict:
    """Fetch usage and cost for `start`..`end` inclusive (UTC days) and
    return a report in `FORMAT`. `workspace_ids` narrows both (name the
    default workspace `"default"`); `api_key_ids` narrows usage only —
    the cost report has no such filter, and the file says so.
    `transport` is an injection point for tests."""
    import httpx  # local: the server side of this module doesn't need it

    if not admin_key:
        raise ProviderReportError(f"Set {ADMIN_KEY_ENV} to an Admin API key (sk-ant-admin…).")
    if end < start:
        raise ProviderReportError("The period must end on or after the day it starts.")
    wanted = set(workspace_ids or [])
    window = [
        ("starting_at", _rfc3339(start)),
        ("ending_at", _rfc3339(end + timedelta(days=1))),
        ("limit", str(MAX_DAILY_BUCKETS)),
    ]
    usage_params = [
        *window,
        ("bucket_width", "1d"),
        ("group_by[]", "model"),
        ("group_by[]", "workspace_id"),
        *[("api_key_ids[]", k) for k in (api_key_ids or [])],
    ]
    cost_params = [*window, ("group_by[]", "description"), ("group_by[]", "workspace_id")]

    usage: dict[tuple[str, str], dict] = {}
    costs: dict[tuple, dict] = {}
    with httpx.Client(
        base_url=API_BASE,
        headers={
            "x-api-key": admin_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "user-agent": user_agent,
        },
        timeout=60.0,
        transport=transport,
    ) as client:
        for bucket in _buckets(client, USAGE_PATH, usage_params):
            day = str(bucket["starting_at"])[:10]
            if not start.isoformat() <= day <= end.isoformat():
                continue
            for r in bucket.get("results", []):
                if not _in_scope(r.get("workspace_id"), wanted):
                    continue
                row = usage.setdefault(
                    (day, r.get("model") or ""),
                    {
                        "day": day,
                        "model": r.get("model") or "",
                        "uncached_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                        "output_tokens": 0,
                        "web_search_requests": 0,
                    },
                )
                creation = r.get("cache_creation") or {}
                row["uncached_input_tokens"] += int(r.get("uncached_input_tokens") or 0)
                row["cache_read_input_tokens"] += int(r.get("cache_read_input_tokens") or 0)
                row["cache_creation_input_tokens"] += int(
                    creation.get("ephemeral_5m_input_tokens") or 0
                ) + int(creation.get("ephemeral_1h_input_tokens") or 0)
                row["output_tokens"] += int(r.get("output_tokens") or 0)
                row["web_search_requests"] += int(
                    (r.get("server_tool_use") or {}).get("web_search_requests") or 0
                )
        for bucket in _buckets(client, COST_PATH, cost_params):
            day = str(bucket["starting_at"])[:10]
            if not start.isoformat() <= day <= end.isoformat():
                continue
            for r in bucket.get("results", []):
                if not _in_scope(r.get("workspace_id"), wanted):
                    continue
                key = (
                    day,
                    r.get("model") or "",
                    r.get("cost_type") or "unspecified",
                    r.get("token_type") or "",
                    r.get("service_tier") or "",
                    r.get("description") or "",
                )
                row = costs.setdefault(
                    key,
                    {
                        "day": key[0],
                        "model": key[1],
                        "cost_type": key[2],
                        "token_type": key[3],
                        "service_tier": key[4],
                        "description": key[5],
                        "amount_usd": 0.0,
                    },
                )
                row["amount_usd"] += _cents_to_usd(r.get("amount") or "0")

    for row in costs.values():
        row["amount_usd"] = round(row["amount_usd"], 6)
    report = {
        "format": FORMAT,
        "provider": "anthropic",
        "fetched_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "period": {"from": start.isoformat(), "to": end.isoformat()},
        "scope": {
            "workspace_ids": sorted(wanted),
            "api_key_ids": sorted(api_key_ids or []),
            "usage": "api_keys" if api_key_ids else ("workspaces" if wanted else "organization"),
            # The cost report has no API-key filter.
            "cost": "workspaces" if wanted else "organization",
        },
        "usage": [usage[k] for k in sorted(usage)],
        "costs": [costs[k] for k in sorted(costs)],
    }
    logger.info(
        "Provider report %s..%s: %d usage rows, %d cost rows, $%.2f",
        start,
        end,
        len(report["usage"]),
        len(report["costs"]),
        sum(r["amount_usd"] for r in report["costs"]),
    )
    return report


# ─── Validating an uploaded file ─────────────────────────────────────


def _day(value, label: str) -> date:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        raise ProviderReportError(f"{label} isn't a date (YYYY-MM-DD).") from None


def _count(value, label: str) -> int:
    try:
        n = int(value or 0)
    except (TypeError, ValueError):
        raise ProviderReportError(f"{label} isn't a whole number.") from None
    if n < 0:
        raise ProviderReportError(f"{label} is negative.")
    return n


def validate_report(payload) -> dict:
    """Check an uploaded report and return it normalized. Raises
    ProviderReportError with a message fit to show the admin."""
    if not isinstance(payload, dict) or payload.get("format") != FORMAT:
        raise ProviderReportError(
            f"This isn't a provider report ({FORMAT}) — make one with elenchus-provider-report."
        )
    provider = str(payload.get("provider") or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9_-]{1,40}", provider):
        raise ProviderReportError("The report doesn't name its provider.")
    period = payload.get("period") or {}
    start, end = _day(period.get("from"), "The period start"), _day(period.get("to"), "The end")
    if end < start or (end - start).days > 3 * 366:
        raise ProviderReportError("The report's period is not believable.")
    usage_rows, cost_rows = payload.get("usage") or [], payload.get("costs") or []
    if not isinstance(usage_rows, list) or not isinstance(cost_rows, list):
        raise ProviderReportError("The report's usage and costs must be lists.")
    if len(usage_rows) + len(cost_rows) > MAX_ROWS:
        raise ProviderReportError("The report is too large; fetch a shorter period.")

    def in_period(d: date) -> date:
        if not start <= d <= end:
            raise ProviderReportError(f"A row dated {d} is outside the report's period.")
        return d

    usage = [
        {
            "day": in_period(_day(r.get("day"), "A usage row's day")),
            "model": str(r.get("model") or "")[:120],
            "uncached_input_tokens": _count(r.get("uncached_input_tokens"), "A token count"),
            "cache_read_input_tokens": _count(r.get("cache_read_input_tokens"), "A token count"),
            "cache_creation_input_tokens": _count(
                r.get("cache_creation_input_tokens"), "A token count"
            ),
            "output_tokens": _count(r.get("output_tokens"), "A token count"),
        }
        for r in usage_rows
    ]
    costs = []
    for r in cost_rows:
        try:
            amount = float(r.get("amount_usd") or 0)
        except (TypeError, ValueError):
            raise ProviderReportError("A cost row's amount isn't a number.") from None
        if amount != amount or abs(amount) > 10_000_000:
            raise ProviderReportError("A cost row's amount is out of range.")
        costs.append(
            {
                "day": in_period(_day(r.get("day"), "A cost row's day")),
                "model": str(r.get("model") or "")[:120],
                "cost_type": str(r.get("cost_type") or "unspecified")[:40],
                "amount_usd": amount,
            }
        )
    scope = payload.get("scope") if isinstance(payload.get("scope"), dict) else {}
    return {
        "provider": provider,
        "fetched_at": str(payload.get("fetched_at") or "")[:40],
        "period_from": start,
        "period_to": end,
        "scope": scope,
        "usage": usage,
        "costs": costs,
    }


# ─── Importing (on the server; caller holds the platform lock) ───────


def import_report(con, payload, *, actor_id: int) -> dict:
    """Replace the stored provider rows for the days the report covers.
    Derived data, so replacement is the point: upload a fresher report
    for the same month and it supersedes the old one. Each upload leaves
    a row in `provider_report_imports`."""
    report = validate_report(payload)
    rows: dict[tuple[date, str, str], dict] = {}

    def row(day: date, model: str, cost_type: str) -> dict:
        return rows.setdefault(
            (day, model, cost_type),
            {"uncached": 0, "cache_read": 0, "cache_creation": 0, "output": 0, "cost_usd": 0.0},
        )

    for u in report["usage"]:
        r = row(u["day"], u["model"], "tokens")
        r["uncached"] += u["uncached_input_tokens"]
        r["cache_read"] += u["cache_read_input_tokens"]
        r["cache_creation"] += u["cache_creation_input_tokens"]
        r["output"] += u["output_tokens"]
    for c in report["costs"]:
        row(c["day"], c["model"], c["cost_type"])["cost_usd"] += c["amount_usd"]
    total = sum(r["cost_usd"] for r in rows.values())

    now = datetime.now(UTC).replace(tzinfo=None)
    con.execute("BEGIN")
    try:
        import_id = con.execute(
            "INSERT INTO provider_report_imports (provider, period_from, period_to, fetched_at, "
            "scope, rows_imported, total_cost_usd, imported_by, imported_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
            [
                report["provider"],
                report["period_from"],
                report["period_to"],
                report["fetched_at"],
                json.dumps(report["scope"], sort_keys=True),
                len(rows),
                total,
                actor_id,
                now,
            ],
        ).fetchone()[0]
        replaced = con.execute(
            "SELECT COUNT(*) FROM provider_usage_daily WHERE provider = ? AND day BETWEEN ? AND ?",
            [report["provider"], report["period_from"], report["period_to"]],
        ).fetchone()[0]
        con.execute(
            "DELETE FROM provider_usage_daily WHERE provider = ? AND day BETWEEN ? AND ?",
            [report["provider"], report["period_from"], report["period_to"]],
        )
        for (day, model, cost_type), r in sorted(rows.items()):
            con.execute(
                "INSERT INTO provider_usage_daily (provider, day, model, cost_type, "
                "uncached_input_tokens, cache_read_input_tokens, cache_creation_input_tokens, "
                "output_tokens, cost_usd, import_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    report["provider"],
                    day,
                    model,
                    cost_type,
                    r["uncached"],
                    r["cache_read"],
                    r["cache_creation"],
                    r["output"],
                    r["cost_usd"],
                    import_id,
                ],
            )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    logger.info(
        "Provider report imported by actor=%d: provider=%s %s..%s fetched_at=%s scope=%s — "
        "%d rows ($%.2f), replacing %d",
        actor_id,
        report["provider"],
        report["period_from"],
        report["period_to"],
        report["fetched_at"],
        json.dumps(report["scope"], sort_keys=True),
        len(rows),
        total,
        replaced,
    )
    return {
        "import_id": int(import_id),
        "provider": report["provider"],
        "period_from": report["period_from"].isoformat(),
        "period_to": report["period_to"].isoformat(),
        "rows": len(rows),
        "rows_replaced": int(replaced),
        "total_cost_usd": round(total, 2),
    }


def list_imports(con, limit: int = 20) -> list[dict]:
    rows = con.execute(
        "SELECT id, provider, period_from, period_to, fetched_at, scope, rows_imported, "
        "total_cost_usd, imported_by, imported_at FROM provider_report_imports "
        "ORDER BY id DESC LIMIT ?",
        [limit],
    ).fetchall()
    return [
        {
            "id": r[0],
            "provider": r[1],
            "period_from": r[2].isoformat(),
            "period_to": r[3].isoformat(),
            "fetched_at": r[4],
            "scope": json.loads(r[5] or "{}"),
            "rows": r[6],
            "total_cost_usd": round(float(r[7]), 2),
            "imported_by": r[8],
            "imported_at": r[9].isoformat(),
        }
        for r in rows
    ]


# ─── Reconciliation ──────────────────────────────────────────────────


def _covered_days(con) -> set[date]:
    """Every day some import's period covers — including days with no
    usage, which have no row but are nonetheless accounted for."""
    days: set[date] = set()
    for start, end in con.execute(
        "SELECT period_from, period_to FROM provider_report_imports"
    ).fetchall():
        d = start
        while d <= end:
            days.add(d)
            d += timedelta(days=1)
    return days


def _verdict(p_in: int, p_out: int, m_in: int, m_out: int, p_usd: float, m_usd: float) -> str:
    def apart(a: float, b: float, tolerance: float) -> bool:
        return abs(a - b) > tolerance * max(a, b, 1)

    provider_tokens, platform_tokens = p_in + p_out, m_in + m_out
    if apart(provider_tokens, platform_tokens, TOKEN_TOLERANCE):
        if provider_tokens > platform_tokens:
            return "provider_saw_more"
        return "platform_recorded_more"
    if abs(p_usd - m_usd) > COST_FLOOR_USD and apart(p_usd, m_usd, COST_TOLERANCE):
        return "rates_differ"
    return "agrees"


VERDICTS = {
    "agrees": "The two sets of books agree.",
    "rates_differ": (
        "Tokens agree but dollars don't: check this model's rate in the price table."
    ),
    "provider_saw_more": (
        "The provider saw more tokens than the platform recorded: another client on the same "
        "key or workspace, calls that escaped recording, or prompt caching the usage table "
        "doesn't count."
    ),
    "platform_recorded_more": (
        "The platform recorded more than the provider reports: the report may not cover every "
        "key or workspace, or the provider's data hadn't settled when it was fetched."
    ),
}


def reconciliation(con, groups: list[dict], entered: list[dict]) -> list[dict]:
    """Per month, newest first: the provider's figure beside the
    platform's. `groups` are `costs.priced_groups`; `entered` are the
    live ledger entries in the `llm_provider` category (a figure typed
    in by hand). An imported report wins over a typed figure for the
    same month, and is compared **over the days it covers only**."""
    covered = _covered_days(con)
    provider_rows = con.execute(
        "SELECT day, model, cost_type, uncached_input_tokens + cache_read_input_tokens + "
        "cache_creation_input_tokens, output_tokens, cost_usd, cache_read_input_tokens + "
        "cache_creation_input_tokens FROM provider_usage_daily"
    ).fetchall()

    typed: dict[str, float] = {}
    for e in entered:
        typed[e["covers_month"]] = typed.get(e["covers_month"], 0.0) + e["amount_usd"]
    months = {d.isoformat()[:7] for d in covered} | set(typed)

    out = []
    for month in sorted(months, reverse=True):
        days = {d for d in covered if d.isoformat()[:7] == month}
        if not days:
            computed = sum(g["cost_usd"] for g in groups if g["day"].isoformat()[:7] == month)
            reported = typed[month]
            out.append(
                {
                    "month": month,
                    "source": "entered",
                    "provider_usd": round(reported, 2),
                    "computed_usd": round(computed, 2),
                    "difference_usd": round(reported - computed, 2),
                    "difference_pct": (
                        100.0 * (reported - computed) / reported if reported else None
                    ),
                    "days_covered": None,
                    "days_in_month": calendar.monthrange(int(month[:4]), int(month[5:]))[1],
                    "entered_usd": round(reported, 2),
                    "non_token_usd": 0.0,
                    "cache_tokens": 0,
                    "models": [],
                }
            )
            continue

        models: dict[str, dict] = {}

        def model_row(models: dict, name: str) -> dict:
            return models.setdefault(
                name,
                {
                    "model": name,
                    "provider_input_tokens": 0,
                    "provider_output_tokens": 0,
                    "platform_input_tokens": 0,
                    "platform_output_tokens": 0,
                    "provider_usd": 0.0,
                    "computed_usd": 0.0,
                },
            )

        non_token = 0.0
        cache_tokens = 0
        for day, model, cost_type, tokens_in, tokens_out, cost, cached in provider_rows:
            if day not in days:
                continue
            if cost_type != "tokens" or not model:
                non_token += float(cost)
                continue
            m = model_row(models, canonical_model(model))
            m["provider_input_tokens"] += int(tokens_in)
            m["provider_output_tokens"] += int(tokens_out)
            m["provider_usd"] += float(cost)
            cache_tokens += int(cached)
        for g in groups:
            if g["day"] not in days:
                continue
            m = model_row(models, canonical_model(g["model"]))
            m["platform_input_tokens"] += g["prompt_tokens"]
            m["platform_output_tokens"] += g["completion_tokens"]
            m["computed_usd"] += g["cost_usd"]

        rows = []
        for m in models.values():
            code = _verdict(
                m["provider_input_tokens"],
                m["provider_output_tokens"],
                m["platform_input_tokens"],
                m["platform_output_tokens"],
                m["provider_usd"],
                m["computed_usd"],
            )
            rows.append(
                {
                    **m,
                    "provider_usd": round(m["provider_usd"], 2),
                    "computed_usd": round(m["computed_usd"], 2),
                    "verdict": code,
                    "verdict_text": VERDICTS[code],
                }
            )
        reported = sum(m["provider_usd"] for m in models.values()) + non_token
        computed = sum(m["computed_usd"] for m in models.values())
        out.append(
            {
                "month": month,
                "source": "imported",
                "provider_usd": round(reported, 2),
                "computed_usd": round(computed, 2),
                "difference_usd": round(reported - computed, 2),
                "difference_pct": 100.0 * (reported - computed) / reported if reported else None,
                "days_covered": len(days),
                "days_in_month": calendar.monthrange(int(month[:4]), int(month[5:]))[1],
                "entered_usd": round(typed[month], 2) if month in typed else None,
                "non_token_usd": round(non_token, 2),
                "cache_tokens": cache_tokens,
                "models": sorted(rows, key=lambda r: -max(r["provider_usd"], r["computed_usd"])),
            }
        )
    return out


# ─── Command line: `elenchus-provider-report` ────────────────────────


def _utc_today() -> date:
    return datetime.now(UTC).date()


def _month_bounds(month: str) -> tuple[date, date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month or ""):
        raise ProviderReportError("--month must look like 2026-10.")
    year, mon = int(month[:4]), int(month[5:])
    return date(year, mon, 1), date(year, mon, calendar.monthrange(year, mon)[1])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="elenchus-provider-report",
        description=(
            "Fetch Anthropic's own usage and cost figures into a file an Elenchus admin can "
            "upload for reconciliation (Costs tab). Run it on your own machine, not the "
            f"server: it needs an Admin API key, read from {ADMIN_KEY_ENV}."
        ),
    )
    parser.add_argument("--month", help="A whole month, e.g. 2026-10 (instead of --from/--to)")
    parser.add_argument("--from", dest="start", help="First UTC day, YYYY-MM-DD")
    parser.add_argument("--to", dest="end", help="Last UTC day (inclusive), YYYY-MM-DD")
    parser.add_argument(
        "--workspace-id",
        action="append",
        default=[],
        help=f"Only this workspace (repeatable; '{DEFAULT_WORKSPACE}' for the default workspace)",
    )
    parser.add_argument(
        "--api-key-id",
        action="append",
        default=[],
        help="Only this API key's usage (repeatable). Costs can't be filtered by key.",
    )
    parser.add_argument("--out", required=True, help="Where to write the report (JSON)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)

    try:
        if args.month:
            start, end = _month_bounds(args.month)
        elif args.start and args.end:
            start, end = _day(args.start, "--from"), _day(args.end, "--to")
        else:
            raise ProviderReportError("Give --month, or both --from and --to.")
        # A month in progress has no figures for days that haven't happened.
        if start > _utc_today():
            raise ProviderReportError("That period hasn't started yet (provider days are UTC).")
        end = min(end, _utc_today())
        report = fetch_anthropic(
            admin_key=os.environ.get(ADMIN_KEY_ENV, "").strip(),
            start=start,
            end=end,
            workspace_ids=args.workspace_id,
            api_key_ids=args.api_key_id,
        )
    except ProviderReportError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    total = sum(r["amount_usd"] for r in report["costs"])
    tokens_in = sum(
        r["uncached_input_tokens"]
        + r["cache_read_input_tokens"]
        + r["cache_creation_input_tokens"]
        for r in report["usage"]
    )
    tokens_out = sum(r["output_tokens"] for r in report["usage"])
    print(
        f"{report['period']['from']} → {report['period']['to']}: ${total:,.2f}, "
        f"{tokens_in:,} tokens in, {tokens_out:,} out "
        f"(usage scope: {report['scope']['usage']}; cost scope: {report['scope']['cost']})"
    )
    if args.api_key_id:
        print(
            "note: the cost report can't be filtered by API key — the dollar figures cover "
            f"the whole {report['scope']['cost']}. For clean numbers give the platform a "
            "workspace of its own and use --workspace-id.",
            file=sys.stderr,
        )
    print(f"Wrote {args.out} — upload it in the Costs tab (Import a provider report).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
