"""
cost_ledger.py — what the platform costs to run besides LLM tokens.

LLM spend is measured (`usage`, priced at read time — `costs.py`).
Infrastructure spend arrives as invoices from whoever hosts the box, so
it is **recorded by an admin, not fetched**: a provider-agnostic ledger
survives a move between hosts or accounts, needs no billing credential
on the server, and lets every row carry the invoice reference a grant
report asks for. Platform migration `0013` has the tables; this module
owns them.

Three kinds of thing live here, and only the first is ever summed:

  * **entries** (`cost_entries`) in an infrastructure category — real
    charges and credits. `amount` + `currency` are the invoice's;
    `amount_usd` is what is charged to the budget. Never deleted: a
    wrong row is voided and stays, excluded from every sum. `estimated`
    marks a row generated from a recurring item and not yet checked
    against an invoice.
  * **entries in the `llm_provider` category** — the LLM provider's own
    figure for a month of usage (`covers_month`). Reconciliation only:
    that money is already counted from tokens, so adding it would count
    it twice.
  * **recurring items** (`cost_recurring`) — a charge expected monthly
    or yearly. A forecast and a convenience: they give the run-rate,
    the projection to the end of the budget period, the reminder that a
    past month has nothing recorded, and one-click entry of a month's
    charges. They are never themselves counted as spend.

Every write is logged with who made it and, for an edit, the fields
that changed — the ledger's audit trail is the server log.
"""

from __future__ import annotations

import calendar
import logging
import re
from datetime import UTC, date, datetime

logger = logging.getLogger(__name__)


INFRA_CATEGORIES: dict[str, str] = {
    "hosting": "Hosting (servers, storage, backups)",
    "domain": "Domain and DNS",
    "email": "Email delivery",
    "other": "Other infrastructure",
}
RECONCILIATION_CATEGORY = "llm_provider"
CATEGORIES: dict[str, str] = {
    **INFRA_CATEGORIES,
    RECONCILIATION_CATEGORY: "LLM provider's own figure (reconciliation only)",
}
CADENCES = ("monthly", "yearly")

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
MAX_AMOUNT = 10_000_000

_ENTRY_COLUMNS = (
    "id, incurred_on, category, vendor, description, amount, currency, amount_usd, "
    "invoice_ref, covers_month, estimated, recurring_id, created_by, created_at, "
    "updated_by, updated_at, voided_by, voided_at, void_reason"
)
_RECURRING_COLUMNS = (
    "id, category, vendor, description, amount_usd, cadence, starts_on, ends_on, "
    "created_by, created_at"
)
# The fields an admin supplies (and may later correct).
_ENTRY_FIELDS = (
    "incurred_on",
    "category",
    "vendor",
    "description",
    "amount",
    "currency",
    "amount_usd",
    "invoice_ref",
    "covers_month",
    "estimated",
)


def now_utc() -> datetime:
    """Naive UTC, written explicitly: DuckDB's CURRENT_TIMESTAMP lands
    in a TIMESTAMP column as server-local time."""
    return datetime.now(UTC).replace(tzinfo=None)


def month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


# ─── Validation ──────────────────────────────────────────────────────


def _text(payload: dict, key: str, *, limit: int, required: bool = False, label: str) -> str:
    value = str(payload.get(key) or "").strip()
    if required and not value:
        raise ValueError(f"{label} is required.")
    if len(value) > limit:
        raise ValueError(f"{label} must be at most {limit} characters.")
    return value


def _date(value, label: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value or ""))
    except ValueError:
        raise ValueError(f"{label} must be a date (YYYY-MM-DD).") from None


def _amount(value, label: str, *, positive: bool = False) -> float:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a number.") from None
    if amount != amount or abs(amount) > MAX_AMOUNT:  # NaN or absurd
        raise ValueError(f"{label} is out of range.")
    if positive and amount <= 0:
        raise ValueError(f"{label} must be greater than zero.")
    if amount == 0:
        raise ValueError(f"{label} can't be zero.")
    return round(amount, 2)


def validate_entry(payload: dict) -> dict:
    """Normalize an entry submitted by an admin. Raises ValueError with
    a message fit to show them."""
    category = str(payload.get("category") or "").strip()
    if category not in CATEGORIES:
        raise ValueError("Choose a category: " + ", ".join(CATEGORIES) + ".")
    amount = _amount(payload.get("amount"), "The amount")
    currency = str(payload.get("currency") or "USD").strip().upper()
    if not _CURRENCY_RE.match(currency):
        raise ValueError("The currency must be a three-letter code such as USD or EUR.")
    if currency == "USD":
        amount_usd = amount
    else:
        if payload.get("amount_usd") in (None, ""):
            raise ValueError(
                f"Give the US-dollar equivalent of this {currency} amount — the figure "
                "charged to the budget."
            )
        amount_usd = _amount(payload.get("amount_usd"), "The US-dollar equivalent")
        if (amount_usd > 0) != (amount > 0):
            raise ValueError("The US-dollar equivalent must have the same sign as the amount.")
    covers_month = str(payload.get("covers_month") or "").strip()
    if covers_month and not _MONTH_RE.match(covers_month):
        raise ValueError("The month covered must look like 2026-10.")
    if category == RECONCILIATION_CATEGORY and not covers_month:
        raise ValueError("Say which month of usage the provider's figure covers (e.g. 2026-10).")
    return {
        "incurred_on": _date(payload.get("incurred_on"), "The date"),
        "category": category,
        "vendor": _text(payload, "vendor", limit=80, required=True, label="The vendor"),
        "description": _text(payload, "description", limit=200, label="The description"),
        "amount": amount,
        "currency": currency,
        "amount_usd": amount_usd,
        "invoice_ref": _text(payload, "invoice_ref", limit=80, label="The invoice reference"),
        "covers_month": covers_month,
        "estimated": bool(payload.get("estimated", False)),
    }


def validate_recurring(payload: dict) -> dict:
    category = str(payload.get("category") or "").strip()
    if category not in INFRA_CATEGORIES:
        raise ValueError("Choose a category: " + ", ".join(INFRA_CATEGORIES) + ".")
    cadence = str(payload.get("cadence") or "").strip()
    if cadence not in CADENCES:
        raise ValueError("A recurring charge is monthly or yearly.")
    starts_on = _date(payload.get("starts_on"), "The start date")
    ends_on = None
    if payload.get("ends_on") not in (None, ""):
        ends_on = _date(payload.get("ends_on"), "The end date")
        if ends_on < starts_on:
            raise ValueError("A recurring charge can't end before it starts.")
    return {
        "category": category,
        "vendor": _text(payload, "vendor", limit=80, required=True, label="The vendor"),
        "description": _text(payload, "description", limit=200, label="The description"),
        "amount_usd": _amount(payload.get("amount_usd"), "The amount", positive=True),
        "cadence": cadence,
        "starts_on": starts_on,
        "ends_on": ends_on,
    }


# ─── Rows ────────────────────────────────────────────────────────────


def _iso(value) -> str | None:
    return None if value is None else value.isoformat()


def _entry(row) -> dict:
    return {
        "id": row[0],
        "incurred_on": _iso(row[1]),
        "category": row[2],
        "vendor": row[3],
        "description": row[4],
        "amount": float(row[5]),
        "currency": row[6],
        "amount_usd": float(row[7]),
        "invoice_ref": row[8],
        "covers_month": row[9],
        "estimated": bool(row[10]),
        "recurring_id": row[11],
        "created_by": row[12],
        "created_at": _iso(row[13]),
        "updated_by": row[14],
        "updated_at": _iso(row[15]),
        "voided_by": row[16],
        "voided_at": _iso(row[17]),
        "void_reason": row[18],
        "voided": row[17] is not None,
    }


def _recurring(row) -> dict:
    return {
        "id": row[0],
        "category": row[1],
        "vendor": row[2],
        "description": row[3],
        "amount_usd": float(row[4]),
        "cadence": row[5],
        "starts_on": _iso(row[6]),
        "ends_on": _iso(row[7]),
        "created_by": row[8],
        "created_at": _iso(row[9]),
    }


def get_entry(con, entry_id: int) -> dict | None:
    row = con.execute(
        f"SELECT {_ENTRY_COLUMNS} FROM cost_entries WHERE id = ?", [entry_id]
    ).fetchone()
    return _entry(row) if row else None


def list_entries(con, *, include_voided: bool = True) -> list[dict]:
    """Newest charge first."""
    where = "" if include_voided else "WHERE voided_at IS NULL"
    rows = con.execute(
        f"SELECT {_ENTRY_COLUMNS} FROM cost_entries {where} ORDER BY incurred_on DESC, id DESC"
    ).fetchall()
    return [_entry(r) for r in rows]


def list_recurring(con) -> list[dict]:
    rows = con.execute(
        f"SELECT {_RECURRING_COLUMNS} FROM cost_recurring ORDER BY starts_on, id"
    ).fetchall()
    return [_recurring(r) for r in rows]


def get_recurring(con, recurring_id: int) -> dict | None:
    row = con.execute(
        f"SELECT {_RECURRING_COLUMNS} FROM cost_recurring WHERE id = ?", [recurring_id]
    ).fetchone()
    return _recurring(row) if row else None


# ─── Writes (caller holds the platform lock) ─────────────────────────


def create_entry(con, payload: dict, *, actor_id: int, recurring_id: int | None = None) -> dict:
    e = validate_entry(payload)
    row = con.execute(
        "INSERT INTO cost_entries (incurred_on, category, vendor, description, amount, "
        "currency, amount_usd, invoice_ref, covers_month, estimated, recurring_id, "
        "created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
        [*(e[f] for f in _ENTRY_FIELDS), recurring_id, actor_id, now_utc()],
    ).fetchone()
    entry = get_entry(con, int(row[0]))
    logger.info(
        "Cost ledger: entry #%d created by actor=%d: %s %s %s %.2f %s (USD %.2f) "
        "invoice=%r covers=%r estimated=%s recurring=%s",
        entry["id"],
        actor_id,
        entry["incurred_on"],
        entry["category"],
        entry["vendor"],
        entry["amount"],
        entry["currency"],
        entry["amount_usd"],
        entry["invoice_ref"],
        entry["covers_month"],
        entry["estimated"],
        recurring_id,
    )
    return entry


def update_entry(con, entry_id: int, payload: dict, *, actor_id: int) -> dict:
    """Correct an entry. A voided entry is closed; a change to nothing
    is a no-op. The log line records each field's old and new value."""
    before = get_entry(con, entry_id)
    if before is None:
        raise LookupError(f"Ledger entry #{entry_id} not found")
    if before["voided"]:
        raise ValueError("A voided entry can't be edited — record a new one.")
    e = validate_entry(payload)
    after = {**e, "incurred_on": e["incurred_on"].isoformat()}
    changed = {f: (before[f], after[f]) for f in _ENTRY_FIELDS if before[f] != after[f]}
    if not changed:
        return before
    con.execute(
        "UPDATE cost_entries SET incurred_on = ?, category = ?, vendor = ?, description = ?, "
        "amount = ?, currency = ?, amount_usd = ?, invoice_ref = ?, covers_month = ?, "
        "estimated = ?, updated_by = ?, updated_at = ? WHERE id = ?",
        [*(e[f] for f in _ENTRY_FIELDS), actor_id, now_utc(), entry_id],
    )
    logger.info(
        "Cost ledger: entry #%d edited by actor=%d: %s",
        entry_id,
        actor_id,
        "; ".join(f"{f}: {old!r} -> {new!r}" for f, (old, new) in changed.items()),
    )
    return get_entry(con, entry_id)


def void_entry(con, entry_id: int, *, actor_id: int, reason: str = "") -> dict:
    """Take an entry out of every sum without deleting it."""
    before = get_entry(con, entry_id)
    if before is None:
        raise LookupError(f"Ledger entry #{entry_id} not found")
    if before["voided"]:
        return before
    reason = (reason or "").strip()[:200]
    con.execute(
        "UPDATE cost_entries SET voided_by = ?, voided_at = ?, void_reason = ? WHERE id = ?",
        [actor_id, now_utc(), reason, entry_id],
    )
    logger.info(
        "Cost ledger: entry #%d voided by actor=%d (USD %.2f, %s %s) reason=%r",
        entry_id,
        actor_id,
        before["amount_usd"],
        before["incurred_on"],
        before["vendor"],
        reason,
    )
    return get_entry(con, entry_id)


def create_recurring(con, payload: dict, *, actor_id: int) -> dict:
    r = validate_recurring(payload)
    row = con.execute(
        "INSERT INTO cost_recurring (category, vendor, description, amount_usd, cadence, "
        "starts_on, ends_on, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "RETURNING id",
        [
            r["category"],
            r["vendor"],
            r["description"],
            r["amount_usd"],
            r["cadence"],
            r["starts_on"],
            r["ends_on"],
            actor_id,
            now_utc(),
        ],
    ).fetchone()
    item = get_recurring(con, int(row[0]))
    logger.info(
        "Cost ledger: recurring #%d created by actor=%d: %s %s USD %.2f %s from %s to %s",
        item["id"],
        actor_id,
        item["category"],
        item["vendor"],
        item["amount_usd"],
        item["cadence"],
        item["starts_on"],
        item["ends_on"],
    )
    return item


def end_recurring(con, recurring_id: int, *, ends_on, actor_id: int) -> dict:
    """Stop expecting a recurring charge after `ends_on` (the last day
    it applies). A price change is: end the old item, add a new one."""
    item = get_recurring(con, recurring_id)
    if item is None:
        raise LookupError(f"Recurring charge #{recurring_id} not found")
    ends = _date(ends_on, "The end date")
    if ends < date.fromisoformat(item["starts_on"]):
        raise ValueError("A recurring charge can't end before it starts.")
    con.execute("UPDATE cost_recurring SET ends_on = ? WHERE id = ?", [ends, recurring_id])
    logger.info(
        "Cost ledger: recurring #%d (%s) ended on %s by actor=%d",
        recurring_id,
        item["vendor"],
        ends,
        actor_id,
    )
    return get_recurring(con, recurring_id)


# ─── What recurring items expect ─────────────────────────────────────


def _clip_day(year: int, month: int, day: int) -> date:
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def due_charges(recurring: list[dict], start: date, end: date) -> list[dict]:
    """Every charge the recurring items expect from `start` to `end`
    inclusive, oldest first: `{due_on, recurring_id, amount_usd, …}`. A
    monthly item falls due on its start day each month (clipped to the
    month's length), a yearly one on its anniversary."""
    out: list[dict] = []
    for item in recurring:
        first = date.fromisoformat(item["starts_on"])
        last = date.fromisoformat(item["ends_on"]) if item["ends_on"] else end
        lo, hi = max(first, start), min(last, end)
        if lo > hi:
            continue
        year, month = lo.year, lo.month
        while (year, month) <= (hi.year, hi.month):
            if item["cadence"] == "monthly" or month == first.month:
                due = _clip_day(year, month, first.day)
                if lo <= due <= hi:
                    out.append(
                        {
                            "due_on": due,
                            "recurring_id": item["id"],
                            "category": item["category"],
                            "vendor": item["vendor"],
                            "description": item["description"],
                            "amount_usd": item["amount_usd"],
                            "cadence": item["cadence"],
                        }
                    )
            month += 1
            if month == 13:
                year, month = year + 1, 1
    return sorted(out, key=lambda c: (c["due_on"], c["recurring_id"]))


def monthly_run_rate(recurring: list[dict], today: date) -> float:
    """What the items active today add up to per month (a yearly charge
    counts as a twelfth)."""
    total = 0.0
    for item in recurring:
        if date.fromisoformat(item["starts_on"]) > today:
            continue
        if item["ends_on"] and date.fromisoformat(item["ends_on"]) < today:
            continue
        total += item["amount_usd"] / (12.0 if item["cadence"] == "yearly" else 1.0)
    return total


def _recorded_keys(entries: list[dict]) -> set[tuple[int, str]]:
    """(recurring_id, month) pairs that already have a live entry."""
    return {
        (e["recurring_id"], e["incurred_on"][:7])
        for e in entries
        if e["recurring_id"] is not None and not e["voided"]
    }


def unrecorded_charges(recurring: list[dict], entries: list[dict], today: date) -> list[dict]:
    """Charges the recurring items expected up to today that have no
    live entry in their month — probably real money nobody has entered."""
    if not recurring:
        return []
    start = min(date.fromisoformat(r["starts_on"]) for r in recurring)
    have = _recorded_keys(entries)
    return [
        c
        for c in due_charges(recurring, start, today)
        if (c["recurring_id"], month_key(c["due_on"])) not in have
    ]


def record_recurring_month(con, month: str, *, actor_id: int) -> dict:
    """Enter the charges the recurring items expect in `month`
    ('YYYY-MM') as **estimated** entries, skipping any already there —
    so it is safe to press twice. The admin then checks each against the
    invoice and clears its `estimated` flag."""
    if not _MONTH_RE.match(month or ""):
        raise ValueError("The month must look like 2026-10.")
    year, mon = int(month[:4]), int(month[5:])
    start = date(year, mon, 1)
    end = _clip_day(year, mon, 31)
    have = _recorded_keys(list_entries(con))
    created, skipped = [], 0
    for charge in due_charges(list_recurring(con), start, end):
        if (charge["recurring_id"], month) in have:
            skipped += 1
            continue
        created.append(
            create_entry(
                con,
                {
                    "incurred_on": charge["due_on"],
                    "category": charge["category"],
                    "vendor": charge["vendor"],
                    "description": charge["description"],
                    "amount": charge["amount_usd"],
                    "currency": "USD",
                    "covers_month": month,
                    "estimated": True,
                },
                actor_id=actor_id,
                recurring_id=charge["recurring_id"],
            )
        )
    logger.info(
        "Cost ledger: recurring charges for %s recorded by actor=%d: %d created, %d already there",
        month,
        actor_id,
        len(created),
        skipped,
    )
    return {"month": month, "created": created, "already_recorded": skipped}


# ─── The report block ────────────────────────────────────────────────


def _sum(entries: list[dict]) -> dict:
    return {
        "amount_usd": round(sum(e["amount_usd"] for e in entries), 2),
        "entries": len(entries),
    }


def _by_month(entries: list[dict], today: date, months: int = 12) -> list[dict]:
    """The last `months` calendar months, oldest first, gaps zero."""
    keys = []
    year, month = today.year, today.month
    for _ in range(months):
        keys.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    out = []
    for key in reversed(keys):
        mine = [e for e in entries if e["incurred_on"][:7] == key]
        out.append(
            {
                "month": key,
                **_sum(mine),
                "estimated_usd": round(sum(e["amount_usd"] for e in mine if e["estimated"]), 2),
            }
        )
    return out


def _reconciliation(entries: list[dict], llm_usd_by_month: dict[str, float]) -> list[dict]:
    """Per month the provider's figure was recorded for: that figure,
    what the platform computed from tokens, and the gap. Newest first."""
    provider: dict[str, float] = {}
    for e in entries:
        provider[e["covers_month"]] = provider.get(e["covers_month"], 0.0) + e["amount_usd"]
    out = []
    for month in sorted(provider, reverse=True):
        computed = llm_usd_by_month.get(month, 0.0)
        reported = provider[month]
        out.append(
            {
                "month": month,
                "provider_usd": round(reported, 2),
                "computed_usd": round(computed, 2),
                "difference_usd": round(reported - computed, 2),
                "difference_pct": (100.0 * (reported - computed) / reported) if reported else None,
            }
        )
    return out


def infrastructure_report(
    con,
    *,
    since: date | None,
    today: date,
    llm_usd_by_month: dict[str, float] | None = None,
) -> dict:
    """The `infrastructure` block of the cost report. `since` bounds the
    window views, as in `costs.build_report`; `llm_usd_by_month` is the
    platform's computed LLM spend per 'YYYY-MM', for reconciliation."""
    everything = list_entries(con)
    live = [e for e in everything if not e["voided"]]
    infra = [e for e in live if e["category"] in INFRA_CATEGORIES]
    window = [e for e in infra if since is None or e["incurred_on"] >= since.isoformat()]
    recurring = list_recurring(con)
    missing = unrecorded_charges(recurring, everything, today)

    missing_by_month: dict[str, dict] = {}
    for c in missing:
        m = missing_by_month.setdefault(
            month_key(c["due_on"]),
            {"month": month_key(c["due_on"]), "amount_usd": 0.0, "items": 0},
        )
        m["amount_usd"] = round(m["amount_usd"] + c["amount_usd"], 2)
        m["items"] += 1

    by_category = []
    for category, label in INFRA_CATEGORIES.items():
        mine = [e for e in window if e["category"] == category]
        if mine:
            by_category.append({"category": category, "label": label, **_sum(mine)})

    unconfirmed = [e for e in infra if e["estimated"]]
    return {
        "totals": {
            "window": _sum(window),
            "month_to_date": _sum([e for e in infra if e["incurred_on"][:7] == month_key(today)]),
            "all_time": _sum(infra),
        },
        "by_month": _by_month(infra, today),
        "by_category": sorted(by_category, key=lambda r: -r["amount_usd"]),
        "run_rate_monthly_usd": round(monthly_run_rate(recurring, today), 2),
        "unrecorded": sorted(missing_by_month.values(), key=lambda m: m["month"]),
        "unrecorded_usd": round(sum(c["amount_usd"] for c in missing), 2),
        "unconfirmed": _sum(unconfirmed),
        "reconciliation": _reconciliation(
            [e for e in live if e["category"] == RECONCILIATION_CATEGORY],
            llm_usd_by_month or {},
        ),
    }


def budget_status(con, *, amount_usd: float, start: date, end: date, today: date) -> dict:
    """Infrastructure spend against its budget line, with a projection:
    what is recorded in the period, plus what the recurring items
    expected in the period but nobody has entered, plus what they
    expect from tomorrow to the end of the period."""
    everything = list_entries(con)
    infra = [e for e in everything if not e["voided"] and e["category"] in INFRA_CATEGORIES]
    in_period = [e for e in infra if start.isoformat() <= e["incurred_on"] <= end.isoformat()]
    before = [e for e in infra if e["incurred_on"] < start.isoformat()]
    recurring = list_recurring(con)
    spent = sum(e["amount_usd"] for e in in_period)
    unrecorded = sum(
        c["amount_usd"]
        for c in unrecorded_charges(recurring, everything, min(today, end))
        if c["due_on"] >= start
    )
    upcoming_from = max(start, date.fromordinal(today.toordinal() + 1))
    upcoming = sum(c["amount_usd"] for c in due_charges(recurring, upcoming_from, end))
    return {
        "amount_usd": amount_usd,
        "spent_usd": round(spent, 2),
        "remaining_usd": round(amount_usd - spent, 2),
        "pct_spent": 100.0 * spent / amount_usd,
        "unrecorded_usd": round(unrecorded, 2),
        "upcoming_usd": round(upcoming, 2),
        "projected_usd": round(spent + unrecorded + upcoming, 2),
        "spent_before_period_usd": round(sum(e["amount_usd"] for e in before), 2),
    }
