"""Tests for the cost dashboard (`costs.py`, `GET /api/admin/costs`).

The design under test: **tokens are the source of truth and dollars are
computed when read.** So these tests seed token counts with a stored
`cost_usd` that is deliberately wrong, and check that every view prices
the tokens itself — against the rate in effect on the day of the call —
and reports a model without a rate as unpriced rather than as free.
"""

from __future__ import annotations

import contextlib
from datetime import date, datetime, timedelta
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from elenchus import auth, costs, pricing
from elenchus.db import get_registry
from elenchus.db import platform as pdb
from elenchus.llm_client import ChatCategory, ChatResult
from elenchus.server import app

client = TestClient(app)

CONFIG = {
    "topic_a_title": "Occurrence and its relatives in Darwin Core",
    "topic_a_brief": "Occurrence, Organism, Event, MaterialSample.",
    "topic_b_title": "Taxon concepts and names",
    "topic_b_brief": "Name, taxon concept, usage, circumscription.",
    "min_gap_hours": 0,
}

TODAY = date.today()


@pytest.fixture(autouse=True)
def _clean():
    reg = get_registry()
    reg.migrate_platform()
    con = reg.platform_con()
    with reg.platform_lock:
        for table in (
            "study_participants",
            "study_configs",
            "study_texts",
            "participant_session_tokens",
            "usage",
            "auth_sessions",
            "sessions",
            "bases",
            "actors",
        ):
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
    pricing._reset_cache_for_tests()


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


def _seed(
    *,
    model: str = "claude-opus-4-6",
    prompt: int = 0,
    completion: int = 0,
    purpose: str = "dialectic_turn",
    category: str = "success",
    actor_id: int | None = None,
    base_id: str | None = None,
    attempts: int = 1,
    day: date | None = None,
    stored_cost: float = 99.0,
) -> None:
    """One usage row. `stored_cost` defaults to an obviously wrong
    number: nothing may read it."""
    con = get_registry().platform_con()
    rid = pdb.record_usage(
        con,
        actor_id=actor_id,
        base_id=base_id,
        model=model,
        category=category,
        prompt_tokens=prompt,
        completion_tokens=completion,
        cost_usd=stored_cost,
        attempts=attempts,
        latency_ms=100,
        purpose=purpose,
    )
    if day is not None:
        con.execute(
            "UPDATE usage SET occurred_at = ? WHERE id = ?",
            [datetime.combine(day, datetime.min.time()) + timedelta(hours=12), rid],
        )


def _report(**kwargs) -> dict:
    return costs.build_report(get_registry().platform_con(), **kwargs)


# ─── The price table ─────────────────────────────────────────────────


class TestPriceTable:
    @pytest.mark.parametrize(
        "model",
        [
            "claude-fable-5-1",
            "claude-opus-5",
            "claude-opus-4-8",
            "claude-opus-4-6",
            "claude-sonnet-5",
            "claude-sonnet-4-6",
            "claude-haiku-4-5-20251001",
        ],
    )
    def test_current_models_are_priced(self, model):
        """The bug that started this: the table lacked every current
        model, so their cost was recorded as $0."""
        assert pricing.lookup_rates(model) is not None

    def test_the_deploy_recipes_model_is_priced(self):
        # deploy/manual-poc.md sets ELENCHUS_MODEL=claude-sonnet-4-6.
        assert pricing.compute_cost("claude-sonnet-4-6", 1_000_000, 1_000_000) == pytest.approx(
            18.00
        )

    def test_routing_prefix_and_dots_are_normalized(self):
        assert pricing.lookup_rates("anthropic/claude-sonnet-4.6") == pricing.lookup_rates(
            "claude-sonnet-4-6"
        )

    def test_a_future_sibling_does_not_inherit_an_old_rate(self):
        """`claude-opus-4-9` must not quietly match a bare `claude-opus-4`
        key at Opus 4's old price."""
        assert pricing.lookup_rates("claude-opus-4-9") is None

    def test_dated_rates_do_not_reprice_history(self, monkeypatch):
        monkeypatch.setenv(
            "ELENCHUS_PRICING_JSON",
            '{"m": [{"input_per_1m": 10, "output_per_1m": 0},'
            ' {"input_per_1m": 4, "output_per_1m": 0, "effective_from": "2027-01-01"}]}',
        )
        pricing._reset_cache_for_tests()
        assert pricing.compute_cost("m", 1_000_000, 0, on="2026-12-31") == pytest.approx(10.0)
        assert pricing.compute_cost("m", 1_000_000, 0, on="2027-01-01") == pytest.approx(4.0)

    def test_a_rate_that_starts_later_leaves_earlier_calls_unpriced(self, monkeypatch):
        monkeypatch.setenv(
            "ELENCHUS_PRICING_JSON",
            '{"m": {"input_per_1m": 4, "output_per_1m": 0, "effective_from": "2027-01-01"}}',
        )
        pricing._reset_cache_for_tests()
        assert pricing.lookup_rate("m", on="2026-06-01") is None

    def test_unknown_model_warns_once(self, caplog):
        with caplog.at_level("WARNING", logger="elenchus.pricing"):
            pricing.compute_cost("mystery-model", 10, 10)
            pricing.compute_cost("mystery-model", 10, 10)
        assert sum("mystery-model" in r.message for r in caplog.records) == 1


# ─── Read-time pricing in the report ─────────────────────────────────


class TestReport:
    def test_empty(self):
        report = _report()
        assert report["totals"]["all_time"]["cost_usd"] == 0
        assert report["by_model"] == []
        assert report["studies"] == []
        assert report["unpriced"] == []
        assert report["budget"] is None
        assert report["prices_as_of"] == pricing.PRICES_AS_OF

    def test_prices_tokens_not_the_stored_figure(self):
        _seed(model="claude-sonnet-4-6", prompt=1_000_000, completion=100_000, stored_cost=0.0)
        total = _report()["totals"]["all_time"]
        assert total["cost_usd"] == pytest.approx(3.00 + 1.50)

    def test_views_agree_with_each_other(self):
        _seed(model="claude-opus-4-6", prompt=400_000, purpose="dialectic_turn")
        _seed(model="claude-sonnet-4-6", prompt=200_000, purpose="baseline_turn")
        _seed(model="claude-sonnet-4-6", completion=50_000, purpose="rolling_summary")
        report = _report()
        total = report["totals"]["window"]["cost_usd"]
        assert total == pytest.approx(2.00 + 0.60 + 0.75)
        for view in ("by_model", "by_purpose", "by_actor", "by_day"):
            assert sum(r["cost_usd"] for r in report[view]) == pytest.approx(total), view

    def test_by_purpose_is_labelled(self):
        _seed(prompt=1000, purpose="rolling_summary")
        _seed(prompt=1000, purpose="")
        rows = {r["purpose"]: r["label"] for r in _report()["by_purpose"]}
        assert rows["rolling_summary"] == "Rolling context summaries"
        assert rows[""].startswith("Unlabelled")

    def test_unpriced_model_is_listed_not_free(self, caplog):
        _seed(model="mystery-model", prompt=7000, completion=3000)
        _seed(model="claude-opus-4-6", prompt=1_000_000)
        with caplog.at_level("WARNING", logger="elenchus.pricing"):
            report = _report()
        assert report["unpriced"] == [
            {
                "model": "mystery-model",
                "calls": 1,
                "prompt_tokens": 7000,
                "completion_tokens": 3000,
            }
        ]
        assert report["totals"]["all_time"]["unpriced_tokens"] == 10_000
        assert report["totals"]["all_time"]["cost_usd"] == pytest.approx(5.00)
        mystery = next(m for m in report["by_model"] if m["model"] == "mystery-model")
        assert mystery["rate"] is None
        assert any("mystery-model" in r.message for r in caplog.records)

    def test_by_model_names_the_rate_it_used(self):
        _seed(model="claude-haiku-4-5-20251001", prompt=1_000_000)
        row = _report()["by_model"][0]
        assert row["rate"] == {
            "input_per_1m": 1.0,
            "output_per_1m": 5.0,
            "matched": "claude-haiku-4-5",
        }

    def test_window_bounds_the_breakdowns_but_not_the_totals(self):
        _seed(prompt=1_000_000, day=TODAY - timedelta(days=40))
        _seed(prompt=200_000, day=TODAY - timedelta(days=2))
        report = _report(days=7)
        assert report["totals"]["window"]["cost_usd"] == pytest.approx(1.00)
        assert report["totals"]["all_time"]["cost_usd"] == pytest.approx(6.00)
        assert len(report["by_day"]) == 7  # continuous, gaps zero-filled
        assert report["by_day"][-1]["day"] == TODAY.isoformat()
        assert sum(d["cost_usd"] for d in report["by_day"]) == pytest.approx(1.00)

    def test_days_zero_means_all_time(self):
        _seed(prompt=1_000_000, day=TODAY - timedelta(days=400))
        report = _report(days=0)
        assert report["window"] == {"days": 0, "since": None}
        assert report["totals"]["window"]["cost_usd"] == pytest.approx(5.00)

    def test_month_to_date(self):
        _seed(prompt=1_000_000, day=TODAY.replace(day=1))
        _seed(prompt=1_000_000, day=TODAY.replace(day=1) - timedelta(days=1))
        assert _report()["totals"]["month_to_date"]["cost_usd"] == pytest.approx(5.00)

    def test_a_price_change_applies_from_its_date(self, monkeypatch):
        cut = TODAY - timedelta(days=3)
        monkeypatch.setenv(
            "ELENCHUS_PRICING_JSON",
            '{"m": [{"input_per_1m": 10, "output_per_1m": 0},'
            f' {{"input_per_1m": 4, "output_per_1m": 0, "effective_from": "{cut.isoformat()}"}}]}}',
        )
        pricing._reset_cache_for_tests()
        _seed(model="m", prompt=1_000_000, day=cut - timedelta(days=1))
        _seed(model="m", prompt=1_000_000, day=cut)
        assert _report()["totals"]["all_time"]["cost_usd"] == pytest.approx(14.0)

    def test_waste_counts_failures_and_retries(self):
        _seed(prompt=1000)
        _seed(prompt=1000, attempts=3)
        _seed(category="rate_limit", attempts=4)
        _seed(category="timeout", attempts=2)
        waste = _report()["waste"]
        assert waste["calls"] == 4
        assert waste["failed_calls"] == 2
        assert waste["retry_attempts"] == 2 + 3 + 1
        assert {c["category"] for c in waste["by_category"]} == {"rate_limit", "timeout"}


# ─── Study rollup ────────────────────────────────────────────────────


def _setup_study(study: str = "PILOT") -> None:
    r = client.put(f"/api/admin/study/{study}/config", json=CONFIG)
    assert r.status_code == 200, r.text


def _enrol(study: str = "PILOT", name: str = "Ada", **fields) -> dict:
    r = client.post(
        f"/api/admin/study/{study}/participants", json={"display_name": name, **fields}
    )
    assert r.status_code == 200, r.text
    return r.json()


def _open(token: str) -> TestClient:
    pclient = TestClient(app)
    assert pclient.post(f"/api/study/{token}").status_code == 200
    return pclient


def _finish(pclient: TestClient, *, complete: bool = False) -> None:
    """Submit the text (the point at which a session's LLM spend is
    final); with `complete`, also see the session through its surveys
    so the participant's second link opens."""
    assert pclient.post("/api/study/session/begin-tutorial").status_code == 200
    assert pclient.post("/api/study/session/begin-task").status_code == 200
    r = pclient.post("/api/study/session/finish", json={"content": "My text."})
    assert r.status_code == 200, r.text
    if complete:
        for to_state in ("surveyed", "complete"):
            r = pclient.post("/api/study/session/advance", json={"to_state": to_state})
            assert r.status_code == 200, r.text


def _token_row(token: str) -> dict:
    return pdb.find_participant_token(get_registry().platform_con(), token)


class TestStudyRollup:
    def test_sessions_are_attributed_through_their_actor(self):
        _login()
        _setup_study()
        p = _enrol(first_condition="elenchus", first_topic="A")
        first, second = p["sessions"]
        pclient = _open(first["token"])
        _finish(pclient)
        tok = _token_row(first["token"])
        sid = tok["session_id"]
        # Tutorial turns land on the practice base, task turns on the
        # task base — both belong to the session.
        _seed(actor_id=tok["actor_id"], base_id=f"practice-{sid}", prompt=100_000)
        _seed(actor_id=tok["actor_id"], base_id="task", prompt=500_000)
        _seed(actor_id=tok["actor_id"], base_id="task", prompt=100_000, purpose="rolling_summary")
        _seed(prompt=900_000)  # somebody else's spend

        (study,) = _report()["studies"]
        assert study["study_id"] == "PILOT"
        rows = {(r["participant_code"], r["period"]): r for r in study["sessions"]}
        done = rows[("P01", 1)]
        assert done["condition"] == "elenchus"
        assert done["finished"] is True
        assert done["cost_usd"] == pytest.approx(3.50)
        assert done["practice_cost_usd"] == pytest.approx(0.50)
        assert done["task_cost_usd"] == pytest.approx(3.00)
        waiting = rows[("P01", 2)]
        assert waiting["finished"] is False
        assert waiting["cost_usd"] == 0
        assert study["cost_usd"] == pytest.approx(3.50)

    def test_mean_and_projection_per_condition(self):
        _login()
        _setup_study()
        # Two people start with Elenchus; one has finished, one hasn't opened.
        a = _enrol(name="A", first_condition="elenchus", first_topic="A")
        _enrol(name="B", first_condition="elenchus", first_topic="B")
        _finish(_open(a["sessions"][0]["token"]))
        _seed(actor_id=_token_row(a["sessions"][0]["token"])["actor_id"], prompt=400_000)

        (study,) = _report()["studies"]
        elenchus = study["conditions"]["elenchus"]
        assert elenchus["sessions"] == 2
        assert elenchus["finished_sessions"] == 1
        assert elenchus["outstanding_sessions"] == 1
        assert elenchus["mean_cost_per_finished_session"] == pytest.approx(2.00)
        # No baseline session has finished, so there is no mean to
        # project the two outstanding baseline sessions from — the
        # report says so rather than projecting from nothing.
        assert study["conditions"]["baseline"]["mean_cost_per_finished_session"] is None
        assert study["projected_total_usd"] is None

    def test_projection_once_both_conditions_have_a_mean(self):
        _login()
        _setup_study()
        a = _enrol(name="A", first_condition="elenchus", first_topic="A")
        _enrol(name="B", first_condition="baseline", first_topic="A")
        for session, tokens in zip(a["sessions"], (400_000, 100_000), strict=True):
            _finish(_open(session["token"]), complete=True)
            _seed(actor_id=_token_row(session["token"])["actor_id"], prompt=tokens)

        (study,) = _report()["studies"]
        assert study["conditions"]["elenchus"]["mean_cost_per_finished_session"] == pytest.approx(
            2.00
        )
        assert study["conditions"]["baseline"]["mean_cost_per_finished_session"] == pytest.approx(
            0.50
        )
        # B still owes one session of each condition.
        assert study["projected_remaining_usd"] == pytest.approx(2.50)
        assert study["projected_total_usd"] == pytest.approx(5.00)

    def test_a_voided_link_is_not_outstanding(self):
        _login()
        _setup_study()
        p = _enrol(first_condition="elenchus", first_topic="A")
        con = get_registry().platform_con()
        for session in p["sessions"]:
            pdb.void_participant_token(con, session["token"])
        (study,) = _report()["studies"]
        assert all(c["outstanding_sessions"] == 0 for c in study["conditions"].values())
        assert study["projected_remaining_usd"] == 0


# ─── Budget line ─────────────────────────────────────────────────────


class TestBudget:
    PERIOD = {
        "llm_usd": 3000,
        "period_start": (TODAY - timedelta(days=9)).isoformat(),
        "period_end": (TODAY + timedelta(days=90)).isoformat(),
        "label": "Sloan G-2026-79650 — LLM API costs",
    }

    def test_set_and_report(self):
        _login()
        r = client.put("/api/admin/costs/budget", json=self.PERIOD)
        assert r.status_code == 200, r.text
        _seed(prompt=60_000_000)  # $300 inside the period
        _seed(prompt=2_000_000, day=TODAY - timedelta(days=30))  # $10 before it opened
        budget = client.get("/api/admin/costs").json()["budget"]
        assert budget["llm_usd"] == 3000
        assert budget["label"] == self.PERIOD["label"]
        assert budget["pct_period_elapsed"] == pytest.approx(10.0)
        llm = budget["llm"]
        assert llm["amount_usd"] == 3000
        assert llm["spent_usd"] == pytest.approx(300.0)
        assert llm["remaining_usd"] == pytest.approx(2700.0)
        assert llm["pct_spent"] == pytest.approx(10.0)
        assert llm["spent_before_period_usd"] == pytest.approx(10.0)
        # No infrastructure line was set, so none is reported.
        assert budget["infra_usd"] is None and budget["infra"] is None

    def test_a_budget_stored_by_0_5_0_still_reads(self):
        """0.5.0 stored only `llm_usd`; the infrastructure line came later."""
        import json

        _login()
        con = get_registry().platform_con()
        pdb.set_setting(
            con,
            costs.BUDGET_SETTING_KEY,
            json.dumps({k: v for k, v in self.PERIOD.items()}),
        )
        budget = client.get("/api/admin/costs").json()["budget"]
        assert budget["llm"]["amount_usd"] == 3000
        assert budget["infra"] is None

    def test_needs_at_least_one_line(self):
        _login()
        r = client.put(
            "/api/admin/costs/budget", json={**self.PERIOD, "llm_usd": None, "infra_usd": None}
        )
        assert r.status_code == 422
        assert "both" in r.json()["detail"]["user_message"]

    def test_clear(self):
        _login()
        client.put("/api/admin/costs/budget", json=self.PERIOD)
        assert client.put("/api/admin/costs/budget", json={}).status_code == 200
        assert client.get("/api/admin/costs").json()["budget"] is None

    @pytest.mark.parametrize(
        "bad",
        [
            {"llm_usd": 0},
            {"llm_usd": -5},
            {"llm_usd": "lots"},
            {"period_end": "2020-01-01"},
            {"period_start": "soon"},
        ],
    )
    def test_rejects_nonsense_with_a_message(self, bad):
        _login()
        r = client.put("/api/admin/costs/budget", json={**self.PERIOD, **bad})
        assert r.status_code == 422
        if isinstance(r.json().get("detail"), dict):
            assert r.json()["detail"]["user_message"]


# ─── Route + CLI ─────────────────────────────────────────────────────


class TestRoute:
    def test_admin_only(self):
        assert client.get("/api/admin/costs").status_code == 401
        _login("researcher")
        assert client.get("/api/admin/costs").status_code == 403
        assert client.put("/api/admin/costs/budget", json={}).status_code == 403

    def test_rejects_a_silly_window(self):
        _login()
        assert client.get("/api/admin/costs?days=-1").status_code == 422

    def test_payload_shape(self):
        _login()
        _seed(prompt=1000)
        data = client.get("/api/admin/costs?days=7").json()
        assert set(data) >= {
            "generated_on",
            "prices_as_of",
            "window",
            "totals",
            "by_day",
            "by_model",
            "by_purpose",
            "by_actor",
            "waste",
            "unpriced",
            "studies",
            "infrastructure",
            "budget",
        }

    def test_report_is_logged(self, caplog):
        _seed(prompt=1000)
        with caplog.at_level("INFO", logger="elenchus.costs"):
            _report()
        assert any("Cost report" in r.message for r in caplog.records)

    def test_text_rendering_does_not_repeat_the_all_time_line(self):
        _seed(prompt=1_000_000)

        def llm_part(days: int) -> str:
            # The infrastructure section has an "All time" line of its own.
            return costs.format_report(_report(days=days)).split("Infrastructure")[0]

        assert llm_part(0).count("All time  ") == 1
        assert "Last 30 days" in llm_part(30) and llm_part(30).count("All time  ") == 1

    def test_text_rendering_flags_unpriced_models(self):
        _seed(model="mystery-model", prompt=5000)
        _seed(prompt=1_000_000)
        text = costs.format_report(_report())
        assert "UNPRICED" in text
        assert "mystery-model" in text
        assert "$5.00" in text


# ─── Every server-side LLM call leaves a labelled usage row ──────────


def _ok(text: str, *, prompt: int = 100, completion: int = 50) -> ChatResult:
    return ChatResult(
        category=ChatCategory.SUCCESS,
        text=text,
        attempts=1,
        latency_ms=5,
        prompt_tokens=prompt,
        completion_tokens=completion,
        model="claude-opus-4-6",
    )


def _purposes() -> list[str]:
    con = get_registry().platform_con()
    return [r[0] for r in con.execute("SELECT purpose FROM usage ORDER BY id").fetchall()]


class TestPurposeIsRecorded:
    def test_dialectic_turn(self):
        from elenchus.dialectical_state import DialecticalState
        from elenchus.opponent import Opponent

        state = DialecticalState.in_memory("t")
        opp = Opponent(api_key=None, model="claude-opus-4-6")
        reply = _ok('{"speech_acts":[],"new_tensions":[],"response":"ok"}')
        with patch.object(opp._llm_client, "chat", return_value=reply):
            opp.respond("hi", state, base_id="b")
        assert _purposes() == ["dialectic_turn"]
        state.base.con.close()

    def test_rolling_summary_is_recorded_and_attributed(self):
        """Before 0.5 the every-20-messages summary wrote no usage row
        at all — spend nobody could see."""
        from elenchus.dialectical_state import DialecticalState
        from elenchus.opponent import Opponent

        admin_id = _login()
        state = DialecticalState.in_memory("t")
        for i in range(9):  # 18 stored messages; the next turn makes 20
            state.add_conversation("user", f"u{i}")
            state.add_conversation("assistant", f"a{i}")
        opp = Opponent(api_key=None, model="claude-opus-4-6")
        reply = _ok('{"speech_acts":[],"new_tensions":[],"response":"ok"}')
        with patch.object(opp._llm_client, "chat", return_value=reply):
            opp.respond("hi", state, actor_id=admin_id, base_id="b")
        con = get_registry().platform_con()
        rows = con.execute("SELECT purpose, actor_id, base_id FROM usage ORDER BY id").fetchall()
        assert rows == [("dialectic_turn", admin_id, "b"), ("rolling_summary", admin_id, "b")]
        state.base.con.close()

    def test_report_summary(self):
        from elenchus.dialectical_state import DialecticalState
        from elenchus.opponent import Opponent

        state = DialecticalState.in_memory("t")
        opp = Opponent(api_key=None, model="claude-opus-4-6")
        with patch.object(opp._llm_client, "chat", return_value=_ok("A summary.")):
            opp.generate_summary(state, base_id="b")
        assert _purposes() == ["report_summary"]
        state.base.con.close()

    def test_sim_personas(self):
        from elenchus.sim.driver import LLMDriver
        from elenchus.sim.personas import ParticipantPersona

        class _LLM:
            def chat(self, messages, system=None, max_tokens=0):
                return _ok("A turn.")

        persona = ParticipantPersona.__new__(ParticipantPersona)
        persona.elenchus_domain = persona.baseline_domain = "taxonomy"
        persona.disposition = "careful"
        LLMDriver(_LLM()).participant_task_message(persona, "elenchus", 0, {})
        assert _purposes() == ["sim_persona"]
