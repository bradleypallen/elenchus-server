"""Tests for the infrastructure ledger (`cost_ledger.py`,
`/api/admin/costs/ledger`, `/api/admin/costs/recurring`).

What the ledger promises: only live infrastructure entries are ever
summed; a voided row stays but counts for nothing; the LLM provider's own
figure is kept for reconciliation and never added to a total (that money
is already counted from tokens); and recurring items forecast and remind
but are never themselves spend.
"""

from __future__ import annotations

import contextlib
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from elenchus import auth, cost_ledger, costs, pricing
from elenchus.db import get_registry
from elenchus.db import platform as pdb
from elenchus.server import app

client = TestClient(app)
TODAY = date.today()


@pytest.fixture(autouse=True)
def _clean():
    reg = get_registry()
    reg.migrate_platform()
    con = reg.platform_con()
    with reg.platform_lock:
        for table in ("cost_entries", "cost_recurring", "usage", "auth_sessions", "actors"):
            con.execute(f"DELETE FROM {table}")
        con.execute("DELETE FROM platform_settings WHERE key = ?", [costs.BUDGET_SETTING_KEY])
    for _name, handle in list(reg._handles.items()):
        with contextlib.suppress(Exception):
            handle.state.base.con.close()
    reg._handles.clear()
    client.cookies.clear()
    pricing._reset_cache_for_tests()
    yield
    client.cookies.clear()


def _login(kind: str = "admin") -> int:
    con = get_registry().platform_con()
    actor_id = pdb.create_actor(
        con,
        kind=kind,
        email=f"{kind}@example.com",
        display_name=kind,
        password_hash=auth.hash_password("pw"),
    )
    client.cookies.set(auth.SESSION_COOKIE, auth.create_session(actor_id))
    return actor_id


def _entry(**overrides) -> dict:
    return {
        "incurred_on": TODAY.isoformat(),
        "category": "hosting",
        "vendor": "AWS Lightsail",
        "description": "micro_3_0 instance",
        "amount": 7.00,
        "currency": "USD",
        "invoice_ref": "INV-1",
        **overrides,
    }


def _add(**overrides) -> dict:
    r = client.post("/api/admin/costs/ledger", json=_entry(**overrides))
    assert r.status_code == 200, r.text
    return r.json()["entry"]


def _recurring(**overrides) -> dict:
    payload = {
        "category": "hosting",
        "vendor": "AWS Lightsail",
        "description": "micro_3_0 instance",
        "amount_usd": 7.00,
        "cadence": "monthly",
        "starts_on": "2026-01-15",
        **overrides,
    }
    r = client.post("/api/admin/costs/recurring", json=payload)
    assert r.status_code == 200, r.text
    return r.json()["recurring"]


def _infra(**kwargs) -> dict:
    return costs.build_report(get_registry().platform_con(), **kwargs)["infrastructure"]


# ─── Entries ─────────────────────────────────────────────────────────


class TestEntries:
    def test_admin_only(self):
        assert client.get("/api/admin/costs/ledger").status_code == 401
        _login("researcher")
        assert client.get("/api/admin/costs/ledger").status_code == 403
        assert client.post("/api/admin/costs/ledger", json=_entry()).status_code == 403
        assert client.post("/api/admin/costs/recurring", json={}).status_code == 403

    def test_add_and_list(self):
        admin = _login()
        e = _add()
        assert e["amount_usd"] == 7.00 and e["currency"] == "USD"
        assert e["created_by"] == admin and e["voided"] is False and e["estimated"] is False
        listing = client.get("/api/admin/costs/ledger").json()
        assert [x["id"] for x in listing["entries"]] == [e["id"]]
        assert "hosting" in listing["infra_categories"]
        assert listing["reconciliation_category"] == "llm_provider"

    def test_a_credit_is_a_negative_amount(self):
        _login()
        _add(amount=7.00)
        _add(amount=-2.50, description="promotional credit")
        assert _infra()["totals"]["all_time"] == {"amount_usd": 4.50, "entries": 2}

    def test_a_foreign_invoice_needs_the_dollar_figure(self):
        _login()
        r = client.post("/api/admin/costs/ledger", json=_entry(amount=9.00, currency="EUR"))
        assert r.status_code == 422
        assert "US-dollar equivalent" in r.json()["detail"]["user_message"]
        e = _add(amount=9.00, currency="eur", amount_usd=10.35, vendor="SURF")
        assert (e["amount"], e["currency"], e["amount_usd"]) == (9.00, "EUR", 10.35)
        # …and it is the dollar figure that is summed.
        assert _infra()["totals"]["all_time"]["amount_usd"] == 10.35

    def test_usd_entries_ignore_a_stray_dollar_equivalent(self):
        _login()
        assert _add(amount=7.00, amount_usd=700.0)["amount_usd"] == 7.00

    @pytest.mark.parametrize(
        ("bad", "fragment"),
        [
            ({"amount": 0}, "zero"),
            ({"amount": "lots"}, "number"),
            ({"amount": 1e9}, "out of range"),
            ({"vendor": "  "}, "vendor is required"),
            ({"category": "snacks"}, "Choose a category"),
            ({"incurred_on": "yesterday"}, "date"),
            ({"currency": "dollars"}, "three-letter"),
            ({"covers_month": "Oct 2026"}, "2026-10"),
            ({"amount": 5, "currency": "EUR", "amount_usd": -5}, "same sign"),
            ({"category": "llm_provider"}, "which month"),
        ],
    )
    def test_rejects_nonsense_with_a_message(self, bad, fragment):
        _login()
        r = client.post("/api/admin/costs/ledger", json=_entry(**bad))
        assert r.status_code == 422, r.text
        assert fragment in r.json()["detail"]["user_message"]

    def test_edit_is_logged_field_by_field(self, caplog):
        admin = _login()
        e = _add(amount=7.00, estimated=True, invoice_ref="")
        with caplog.at_level("INFO", logger="elenchus.cost_ledger"):
            r = client.put(
                f"/api/admin/costs/ledger/{e['id']}",
                json=_entry(amount=7.31, estimated=False, invoice_ref="INV-77"),
            )
        assert r.status_code == 200, r.text
        after = r.json()["entry"]
        assert (after["amount_usd"], after["estimated"], after["invoice_ref"]) == (
            7.31,
            False,
            "INV-77",
        )
        assert after["updated_by"] == admin and after["updated_at"]
        line = next(rec.message for rec in caplog.records if "edited" in rec.message)
        assert "amount: 7.0 -> 7.31" in line
        assert "invoice_ref: '' -> 'INV-77'" in line
        assert "vendor" not in line  # unchanged fields aren't noise in the log

    def test_an_edit_that_changes_nothing_is_a_no_op(self):
        _login()
        e = _add()
        after = client.put(f"/api/admin/costs/ledger/{e['id']}", json=_entry()).json()["entry"]
        assert after["updated_at"] is None

    def test_void_keeps_the_row_and_drops_it_from_every_sum(self):
        admin = _login()
        keep, wrong = _add(amount=7.00), _add(amount=70.00)
        r = client.post(
            f"/api/admin/costs/ledger/{wrong['id']}/void", json={"reason": "typed an extra zero"}
        )
        assert r.status_code == 200, r.text
        voided = r.json()["entry"]
        assert voided["voided"] and voided["voided_by"] == admin
        assert voided["void_reason"] == "typed an extra zero"
        ids = {e["id"] for e in client.get("/api/admin/costs/ledger").json()["entries"]}
        assert ids == {keep["id"], wrong["id"]}  # still in the ledger
        assert _infra()["totals"]["all_time"] == {"amount_usd": 7.00, "entries": 1}
        # Voiding twice is harmless; editing a voided entry is refused.
        assert client.post(f"/api/admin/costs/ledger/{wrong['id']}/void").status_code == 200
        r = client.put(f"/api/admin/costs/ledger/{wrong['id']}", json=_entry())
        assert r.status_code == 422 and "voided" in r.json()["detail"]["user_message"]

    def test_missing_entry(self):
        _login()
        assert client.put("/api/admin/costs/ledger/999", json=_entry()).status_code == 404
        assert client.post("/api/admin/costs/ledger/999/void").status_code == 404

    def test_writes_are_logged(self, caplog):
        _login()
        with caplog.at_level("INFO", logger="elenchus.cost_ledger"):
            e = _add()
            client.post(f"/api/admin/costs/ledger/{e['id']}/void", json={"reason": "test"})
        messages = [r.message for r in caplog.records]
        assert any("created" in m and "AWS Lightsail" in m for m in messages)
        assert any("voided" in m and "'test'" in m for m in messages)


# ─── What recurring items expect (pure) ──────────────────────────────


def _item(**kw) -> dict:
    return {
        "id": 1,
        "category": "hosting",
        "vendor": "v",
        "description": "",
        "amount_usd": 7.0,
        "cadence": "monthly",
        "starts_on": "2026-01-31",
        "ends_on": None,
        **kw,
    }


class TestDueCharges:
    def test_monthly_falls_on_the_start_day_clipped_to_the_month(self):
        due = cost_ledger.due_charges([_item()], date(2026, 1, 1), date(2026, 4, 30))
        assert [c["due_on"].isoformat() for c in due] == [
            "2026-01-31",
            "2026-02-28",
            "2026-03-31",
            "2026-04-30",
        ]

    def test_yearly_falls_on_the_anniversary(self):
        item = _item(cadence="yearly", starts_on="2026-06-17", amount_usd=39.0)
        due = cost_ledger.due_charges([item], date(2026, 1, 1), date(2028, 12, 31))
        assert [c["due_on"].isoformat() for c in due] == ["2026-06-17", "2027-06-17", "2028-06-17"]

    def test_a_leap_day_anniversary_is_clipped(self):
        item = _item(cadence="yearly", starts_on="2028-02-29")
        due = cost_ledger.due_charges([item], date(2028, 1, 1), date(2029, 12, 31))
        assert [c["due_on"].isoformat() for c in due] == ["2028-02-29", "2029-02-28"]

    def test_respects_start_end_and_the_window(self):
        item = _item(starts_on="2026-03-10", ends_on="2026-05-09")
        due = cost_ledger.due_charges([item], date(2026, 1, 1), date(2026, 12, 31))
        assert [c["due_on"].isoformat() for c in due] == ["2026-03-10", "2026-04-10"]
        assert cost_ledger.due_charges([item], date(2026, 4, 11), date(2026, 12, 31)) == []

    def test_run_rate_counts_a_yearly_charge_as_a_twelfth(self):
        items = [
            _item(amount_usd=7.0, starts_on="2026-01-01"),
            _item(id=2, amount_usd=0.5, starts_on="2026-01-01"),
            _item(id=3, amount_usd=39.0, cadence="yearly", starts_on="2026-06-17"),
            _item(id=4, amount_usd=100.0, starts_on="2026-01-01", ends_on="2026-02-01"),
            _item(id=5, amount_usd=100.0, starts_on="2030-01-01"),
        ]
        assert cost_ledger.monthly_run_rate(items, date(2026, 9, 1)) == pytest.approx(10.75)


# ─── Recurring items through the API ─────────────────────────────────


class TestRecurring:
    def test_add_and_end(self):
        _login()
        item = _recurring()
        assert item["cadence"] == "monthly" and item["ends_on"] is None
        r = client.put(
            f"/api/admin/costs/recurring/{item['id']}/end", json={"ends_on": "2026-12-31"}
        )
        assert r.status_code == 200 and r.json()["recurring"]["ends_on"] == "2026-12-31"
        r = client.put(
            f"/api/admin/costs/recurring/{item['id']}/end", json={"ends_on": "2025-01-01"}
        )
        assert r.status_code == 422
        assert client.put("/api/admin/costs/recurring/999/end", json={}).status_code == 404

    @pytest.mark.parametrize(
        "bad",
        [
            {"amount_usd": 0},
            {"amount_usd": -7},
            {"cadence": "weekly"},
            {"category": "llm_provider"},  # reconciliation figures don't recur
            {"vendor": ""},
            {"ends_on": "2025-01-01"},
        ],
    )
    def test_rejects_nonsense(self, bad):
        _login()
        payload = {
            "category": "hosting",
            "vendor": "v",
            "amount_usd": 7,
            "cadence": "monthly",
            "starts_on": "2026-01-15",
            **bad,
        }
        assert client.post("/api/admin/costs/recurring", json=payload).status_code == 422

    def test_recurring_items_are_never_spend(self):
        _login()
        _recurring(starts_on=(TODAY - timedelta(days=200)).isoformat())
        infra = _infra()
        assert infra["totals"]["all_time"] == {"amount_usd": 0, "entries": 0}
        assert infra["run_rate_monthly_usd"] == 7.00
        # …but the months nobody has entered are pointed out.
        assert infra["unrecorded_usd"] > 0
        assert all(m["items"] == 1 for m in infra["unrecorded"])

    def test_record_a_month_is_idempotent_and_marks_entries_estimated(self):
        _login()
        box = _recurring(starts_on="2026-01-15")
        _recurring(category="domain", vendor="Route 53", amount_usd=0.50, starts_on="2026-01-01")
        _recurring(
            category="domain",
            vendor="Route 53 Domains",
            amount_usd=39.0,
            cadence="yearly",
            starts_on="2026-06-17",
        )
        r = client.post("/api/admin/costs/ledger/record-recurring", json={"month": "2026-06"})
        assert r.status_code == 200, r.text
        created = r.json()["created"]
        assert sorted(e["amount_usd"] for e in created) == [0.50, 7.00, 39.00]
        assert all(e["estimated"] and e["covers_month"] == "2026-06" for e in created)
        assert {e["incurred_on"] for e in created} == {"2026-06-01", "2026-06-15", "2026-06-17"}
        again = client.post(
            "/api/admin/costs/ledger/record-recurring", json={"month": "2026-06"}
        ).json()
        assert again["created"] == [] and again["already_recorded"] == 3
        # A month without the yearly charge gets only the monthly ones.
        july = client.post(
            "/api/admin/costs/ledger/record-recurring", json={"month": "2026-07"}
        ).json()
        assert sorted(e["amount_usd"] for e in july["created"]) == [0.50, 7.00]
        # Voiding a generated entry lets the month be recorded again.
        mine = next(e for e in created if e["recurring_id"] == box["id"])
        client.post(f"/api/admin/costs/ledger/{mine['id']}/void")
        redo = client.post(
            "/api/admin/costs/ledger/record-recurring", json={"month": "2026-06"}
        ).json()
        assert [e["amount_usd"] for e in redo["created"]] == [7.00]

    def test_record_rejects_a_bad_month(self):
        _login()
        r = client.post("/api/admin/costs/ledger/record-recurring", json={"month": "June"})
        assert r.status_code == 422

    def test_recorded_months_stop_being_flagged(self):
        _login()
        start = (TODAY.replace(day=1) - timedelta(days=40)).replace(day=1)
        _recurring(starts_on=start.isoformat())
        flagged = [m["month"] for m in _infra()["unrecorded"]]
        assert cost_ledger.month_key(start) in flagged
        client.post(
            "/api/admin/costs/ledger/record-recurring",
            json={"month": cost_ledger.month_key(start)},
        )
        assert cost_ledger.month_key(start) not in [m["month"] for m in _infra()["unrecorded"]]


# ─── The report block and the budget line ────────────────────────────


def _seed_usage(day: date, prompt: int) -> None:
    con = get_registry().platform_con()
    rid = pdb.record_usage(
        con,
        actor_id=None,
        base_id=None,
        model="claude-opus-4-6",
        category="success",
        prompt_tokens=prompt,
        completion_tokens=0,
        cost_usd=0.0,
        attempts=1,
        latency_ms=1,
        purpose="dialectic_turn",
    )
    con.execute(
        "UPDATE usage SET occurred_at = ? WHERE id = ?",
        [datetime.combine(day, datetime.min.time()) + timedelta(hours=12), rid],
    )


class TestReport:
    def test_empty(self):
        infra = _infra()
        assert infra["totals"]["all_time"] == {"amount_usd": 0, "entries": 0}
        assert len(infra["by_month"]) == 12
        assert infra["by_month"][-1]["month"] == cost_ledger.month_key(TODAY)
        assert infra["by_category"] == [] and infra["reconciliation"] == []
        assert infra["unrecorded"] == [] and infra["run_rate_monthly_usd"] == 0

    def test_totals_window_and_categories(self):
        _login()
        _add(amount=7.00)
        _add(amount=0.50, category="domain", vendor="Route 53")
        _add(amount=39.00, category="domain", incurred_on=(TODAY - timedelta(days=90)).isoformat())
        infra = _infra(days=30)
        assert infra["totals"]["window"]["amount_usd"] == 7.50
        assert infra["totals"]["all_time"]["amount_usd"] == 46.50
        assert {c["category"]: c["amount_usd"] for c in infra["by_category"]} == {
            "hosting": 7.00,
            "domain": 0.50,
        }
        assert sum(m["amount_usd"] for m in infra["by_month"]) == 46.50

    def test_estimated_entries_are_counted_and_flagged(self):
        _login()
        _add(amount=7.00, estimated=True)
        _add(amount=0.50)
        infra = _infra()
        assert infra["totals"]["all_time"]["amount_usd"] == 7.50
        assert infra["unconfirmed"] == {"amount_usd": 7.00, "entries": 1}
        assert infra["by_month"][-1]["estimated_usd"] == 7.00

    def test_the_providers_figure_reconciles_and_is_never_added(self):
        """The provider billed $9.80 for a month the platform computed at
        $10.00. That is a reconciliation row — not $9.80 more spend."""
        _login()
        month = cost_ledger.month_key(TODAY)
        _seed_usage(TODAY, 2_000_000)  # $10.00 at opus-4-6's $5 per 1M in
        _add(
            category="llm_provider",
            vendor="Anthropic",
            amount=9.80,
            covers_month=month,
            description="Console → Cost, whole month",
        )
        report = costs.build_report(get_registry().platform_con())
        assert report["infrastructure"]["totals"]["all_time"] == {"amount_usd": 0, "entries": 0}
        assert report["totals"]["all_time"]["cost_usd"] == pytest.approx(10.00)
        (row,) = report["infrastructure"]["reconciliation"]
        assert row["month"] == month
        assert (row["provider_usd"], row["computed_usd"]) == (9.80, 10.00)
        assert row["difference_usd"] == pytest.approx(-0.20)

    def test_text_rendering(self):
        _login()
        _recurring(starts_on=(TODAY - timedelta(days=70)).isoformat())
        _add(amount=7.00, estimated=True)
        text = costs.format_report(costs.build_report(get_registry().platform_con()))
        assert "Infrastructure (recorded from invoices" in text
        assert "NOT YET RECORDED" in text
        assert "not yet checked against an invoice" in text


class TestInfraBudget:
    def _set(self, **overrides):
        payload = {
            "llm_usd": 3000,
            "infra_usd": 3000,
            "period_start": (TODAY - timedelta(days=59)).isoformat(),
            "period_end": (TODAY + timedelta(days=300)).isoformat(),
            "label": "Compute",
            **overrides,
        }
        r = client.put("/api/admin/costs/budget", json=payload)
        assert r.status_code == 200, r.text

    def _budget(self) -> dict:
        return client.get("/api/admin/costs").json()["budget"]

    def test_either_line_alone_is_enough(self):
        _login()
        self._set(llm_usd=None)
        b = self._budget()
        assert b["llm"] is None and b["infra"]["amount_usd"] == 3000

    def test_spent_excludes_voided_out_of_period_and_reconciliation_rows(self):
        _login()
        self._set()
        _add(amount=7.00)
        _add(amount=100.00, incurred_on=(TODAY - timedelta(days=200)).isoformat())
        wrong = _add(amount=500.00)
        client.post(f"/api/admin/costs/ledger/{wrong['id']}/void")
        _add(
            category="llm_provider",
            vendor="Anthropic",
            amount=42.0,
            covers_month=cost_ledger.month_key(TODAY),
        )
        infra = self._budget()["infra"]
        assert infra["spent_usd"] == 7.00
        assert infra["remaining_usd"] == 2993.00
        assert infra["spent_before_period_usd"] == 100.00

    def test_projection_adds_what_recurring_items_still_expect(self):
        _login()
        self._set()
        start = TODAY - timedelta(days=59)
        item = _recurring(starts_on=start.isoformat(), amount_usd=10.0)
        con = get_registry().platform_con()
        items = cost_ledger.list_recurring(con)
        period_end = TODAY + timedelta(days=300)
        past = cost_ledger.due_charges(items, start, TODAY)
        future = cost_ledger.due_charges(items, TODAY + timedelta(days=1), period_end)
        assert past and future
        # Record the first expected charge; the rest of the past is unrecorded.
        client.post(
            "/api/admin/costs/ledger/record-recurring",
            json={"month": cost_ledger.month_key(past[0]["due_on"])},
        )
        infra = self._budget()["infra"]
        assert infra["spent_usd"] == 10.0
        assert infra["unrecorded_usd"] == 10.0 * (len(past) - 1)
        assert infra["upcoming_usd"] == 10.0 * len(future)
        assert infra["projected_usd"] == 10.0 * (len(past) + len(future))
        assert item["id"] in {c["recurring_id"] for c in future}
