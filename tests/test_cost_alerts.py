"""Tests for the daily LLM spend alert (`cost_alerts.py`).

It exists to catch a runaway — a day costing far more than a day
should — so what is tested is: it fires on crossing the threshold, keeps
firing at each further multiple (as CRITICAL, and without the
dispatcher's dedup window swallowing it), says nothing twice in a day or
after a restart, and admits when it is blind (tokens on a model with no
rate).
"""

from __future__ import annotations

import contextlib
from datetime import date, timedelta
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from elenchus import alerting, auth, cost_alerts, pricing
from elenchus.db import get_registry
from elenchus.db import platform as pdb
from elenchus.llm_client import ChatCategory, ChatResult
from elenchus.server import app

client = TestClient(app)
TODAY = date.today()


class _Capture:
    def __init__(self):
        self.alerts: list[alerting.Alert] = []

    def send(self, alert: alerting.Alert) -> None:
        self.alerts.append(alert)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv(cost_alerts.ENV_VAR, raising=False)
    monkeypatch.delenv("ALERT_EMAIL_TO", raising=False)
    reg = get_registry()
    reg.migrate_platform()
    con = reg.platform_con()
    with reg.platform_lock:
        for table in ("usage", "auth_sessions", "actors"):
            con.execute(f"DELETE FROM {table}")
        con.execute(
            "DELETE FROM platform_settings WHERE key IN (?, ?)",
            [cost_alerts.SETTING_KEY, cost_alerts.STATE_KEY],
        )
    for _name, handle in list(reg._handles.items()):
        with contextlib.suppress(Exception):
            handle.state.base.con.close()
    reg._handles.clear()
    client.cookies.clear()
    pricing._reset_cache_for_tests()
    yield
    alerting.set_dispatcher_for_tests(None)
    client.cookies.clear()


@pytest.fixture
def sent() -> list[alerting.Alert]:
    capture = _Capture()
    # The real dedup window, so the test proves level 2 isn't swallowed.
    alerting.set_dispatcher_for_tests(alerting.Dispatcher([capture], dedup_window_minutes=5))
    return capture.alerts


def _con():
    return get_registry().platform_con()


def _spend(usd: float, *, model: str = "claude-opus-4-6") -> None:
    """Record a call costing `usd` today (opus-4-6 input is $5 per 1M)."""
    pdb.record_usage(
        _con(),
        actor_id=None,
        base_id=None,
        model=model,
        category="success",
        prompt_tokens=round(usd * 200_000),
        completion_tokens=0,
        cost_usd=0.0,
        attempts=1,
        latency_ms=1,
        purpose="dialectic_turn",
    )


def _login(kind: str = "admin") -> int:
    actor_id = pdb.create_actor(
        _con(),
        kind=kind,
        email=f"{kind}@example.com",
        display_name=kind,
        password_hash=auth.hash_password("pw"),
    )
    client.cookies.set(auth.SESSION_COOKIE, auth.create_session(actor_id))
    return actor_id


class TestThreshold:
    def test_default_env_then_setting(self, monkeypatch):
        assert cost_alerts.get_threshold(_con()) == (25.0, "default")
        monkeypatch.setenv(cost_alerts.ENV_VAR, "40")
        assert cost_alerts.get_threshold(_con()) == (40.0, "env")
        cost_alerts.set_threshold(_con(), 10)
        assert cost_alerts.get_threshold(_con()) == (10.0, "setting")
        # None hands control back to the environment.
        assert cost_alerts.set_threshold(_con(), None) == (40.0, "env")

    def test_a_bad_env_value_is_ignored(self, monkeypatch, caplog):
        monkeypatch.setenv(cost_alerts.ENV_VAR, "plenty")
        with caplog.at_level("WARNING", logger="elenchus.cost_alerts"):
            assert cost_alerts.get_threshold(_con()) == (25.0, "default")
        assert any(cost_alerts.ENV_VAR in r.message for r in caplog.records)

    @pytest.mark.parametrize("bad", [-1, "lots", float("nan")])
    def test_rejects_nonsense(self, bad):
        with pytest.raises(ValueError):
            cost_alerts.set_threshold(_con(), bad)


class TestCheck:
    def test_quiet_below_the_threshold(self, sent):
        _spend(24.99)
        assert cost_alerts.check(_con()) == []
        assert sent == []

    def test_fires_once_on_crossing(self, sent):
        _spend(26)
        (alert,) = cost_alerts.check(_con())
        assert alert.severity == alerting.Severity.HIGH
        assert alert.category == "cost.daily_spend.x1"
        assert "$26.00" in alert.subject and "$25.00" in alert.subject
        assert alert.metadata["level"] == 1 and alert.metadata["calls"] == 1
        # More spend inside the same multiple says nothing new.
        _spend(10)
        assert cost_alerts.check(_con()) == []
        assert len(sent) == 1

    def test_a_runaway_keeps_announcing_itself(self, sent):
        """2× and 3× fire minutes after 1× — as CRITICAL, and in their own
        categories so the dispatcher's 5-minute dedup can't swallow them."""
        for _ in range(3):
            _spend(26)
            cost_alerts.check(_con())
        assert [a.category for a in sent] == [
            "cost.daily_spend.x1",
            "cost.daily_spend.x2",
            "cost.daily_spend.x3",
        ]
        assert [a.severity for a in sent] == [
            alerting.Severity.HIGH,
            alerting.Severity.CRITICAL,
            alerting.Severity.CRITICAL,
        ]

    def test_a_jump_past_several_multiples_fires_once(self, sent):
        _spend(110)
        (alert,) = cost_alerts.check(_con())
        assert alert.metadata["level"] == 4 and alert.severity == alerting.Severity.CRITICAL

    def test_a_restart_does_not_repeat_todays_alert(self, sent):
        _spend(26)
        cost_alerts.check(_con())
        alerting.get_dispatcher().reset()  # what a restart does to the in-memory dedup
        assert cost_alerts.check(_con()) == []
        assert len(sent) == 1

    def test_a_new_day_starts_fresh(self, sent):
        _spend(26)
        cost_alerts.check(_con())
        tomorrow = TODAY + timedelta(days=1)
        assert cost_alerts.check(_con(), today=tomorrow) == []  # nothing spent tomorrow yet
        assert cost_alerts.status(_con(), tomorrow)["today_usd"] == 0

    def test_zero_turns_it_off(self, sent):
        cost_alerts.set_threshold(_con(), 0)
        _spend(500)
        assert cost_alerts.check(_con()) == []
        assert cost_alerts.status(_con())["enabled"] is False

    def test_admits_when_it_is_blind(self, sent):
        _spend(1, model="mystery-model")
        (alert,) = cost_alerts.check(_con())
        assert alert.category == "cost.unpriced_model"
        assert alert.severity == alerting.Severity.MEDIUM
        _spend(1, model="mystery-model")
        assert cost_alerts.check(_con()) == []  # once a day

    def test_is_logged(self, sent, caplog):
        _spend(26)
        with caplog.at_level("WARNING", logger="elenchus.cost_alerts"):
            cost_alerts.check(_con())
        assert any("cost.daily_spend.x1" in r.message for r in caplog.records)


class TestWiring:
    def test_an_llm_call_triggers_the_check(self, sent):
        """The check rides on the usage recorder, so every server-side
        LLM call is watched without its caller doing anything."""
        from elenchus.dialectical_state import DialecticalState
        from elenchus.opponent import Opponent

        cost_alerts.set_threshold(_con(), 1)
        state = DialecticalState.in_memory("t")
        opp = Opponent(api_key=None, model="claude-opus-4-6")
        reply = ChatResult(
            category=ChatCategory.SUCCESS,
            text='{"speech_acts":[],"new_tensions":[],"response":"ok"}',
            attempts=1,
            latency_ms=5,
            prompt_tokens=400_000,  # $2.00
            completion_tokens=0,
            model="claude-opus-4-6",
        )
        with patch.object(opp._llm_client, "chat", return_value=reply):
            result = opp.respond("hi", state, base_id="b")
        assert result["response"] == "ok"
        assert [a.category for a in sent] == ["cost.daily_spend.x2"]
        state.base.con.close()

    def test_a_failing_check_never_costs_the_turn(self, sent):
        from elenchus.dialectical_state import DialecticalState
        from elenchus.opponent import Opponent

        state = DialecticalState.in_memory("t")
        opp = Opponent(api_key=None, model="claude-opus-4-6")
        reply = ChatResult(
            category=ChatCategory.SUCCESS,
            text='{"speech_acts":[],"new_tensions":[],"response":"ok"}',
            attempts=1,
            latency_ms=5,
            prompt_tokens=100,
            completion_tokens=10,
            model="claude-opus-4-6",
        )
        with (
            patch.object(opp._llm_client, "chat", return_value=reply),
            patch("elenchus.cost_alerts.check", side_effect=RuntimeError("boom")),
        ):
            assert opp.respond("hi", state, base_id="b")["response"] == "ok"
        assert _con().execute("SELECT COUNT(*) FROM usage").fetchone()[0] == 1
        state.base.con.close()


class TestRouteAndReport:
    def test_admin_only(self):
        assert client.put("/api/admin/costs/alert", json={"daily_usd": 5}).status_code == 401
        _login("researcher")
        assert client.put("/api/admin/costs/alert", json={"daily_usd": 5}).status_code == 403

    def test_set_and_report(self, monkeypatch):
        _login()
        r = client.put("/api/admin/costs/alert", json={"daily_usd": 10})
        assert r.status_code == 200 and r.json() == {"daily_usd": 10.0, "source": "setting"}
        _spend(12)
        alert = client.get("/api/admin/costs").json()["alert"]
        assert alert["daily_usd"] == 10.0 and alert["source"] == "setting"
        assert alert["today_usd"] == pytest.approx(12.0)
        assert alert["exceeded"] is True
        assert alert["times_threshold"] == pytest.approx(1.2)
        assert alert["emailed"] is False
        monkeypatch.setenv("ALERT_EMAIL_TO", "ops@example.org")
        assert client.get("/api/admin/costs").json()["alert"]["emailed"] is True

    def test_null_goes_back_to_the_default(self):
        _login()
        client.put("/api/admin/costs/alert", json={"daily_usd": 10})
        r = client.put("/api/admin/costs/alert", json={"daily_usd": None})
        assert r.json() == {"daily_usd": 25.0, "source": "default"}

    def test_rejects_nonsense_with_a_message(self):
        _login()
        r = client.put("/api/admin/costs/alert", json={"daily_usd": -3})
        assert r.status_code == 422 and r.json()["detail"]["user_message"]
