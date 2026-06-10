"""Tests for the Phase C alerting subsystem.

Four slices:
  1. `Severity` ordering and `parse_severity`.
  2. `Alert` envelope formatting + metadata serialization.
  3. `Dispatcher` — fan-out, dedup window, CRITICAL bypass,
     channel failure isolation.
  4. `dispatch_for_chat_failure` — ChatResult → Alert mapping.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from elenchus.alerting import (
    Alert,
    ConsoleAlertChannel,
    Dispatcher,
    EmailAlertChannel,
    Severity,
    _at_least,
    dispatch_for_chat_failure,
    get_dispatcher,
    parse_severity,
    set_dispatcher_for_tests,
)
from elenchus.llm_client import ChatCategory, ChatResult

# ─── Severity ────────────────────────────────────────────────────────


class TestSeverity:
    def test_ordering(self):
        # critical > high > medium > low (strict)
        assert _at_least(Severity.CRITICAL, Severity.LOW)
        assert _at_least(Severity.CRITICAL, Severity.CRITICAL)
        assert _at_least(Severity.HIGH, Severity.HIGH)
        assert not _at_least(Severity.LOW, Severity.MEDIUM)
        assert not _at_least(Severity.MEDIUM, Severity.HIGH)

    def test_parse_known(self):
        assert parse_severity("critical") == Severity.CRITICAL
        assert parse_severity("HIGH") == Severity.HIGH
        assert parse_severity("  medium  ") == Severity.MEDIUM

    def test_parse_unknown_uses_default(self):
        assert parse_severity("not-a-severity") == Severity.HIGH
        assert parse_severity("nope", default=Severity.LOW) == Severity.LOW

    def test_parse_empty_uses_default(self):
        assert parse_severity("") == Severity.HIGH
        assert parse_severity(None) == Severity.HIGH


# ─── Alert ───────────────────────────────────────────────────────────


class TestAlert:
    def test_envelope_subject_format(self):
        a = Alert(severity=Severity.CRITICAL, category="x", subject="key revoked")
        assert a.envelope_subject() == "[ELENCHUS:CRITICAL] key revoked"

    def test_envelope_uses_uppercase_severity(self):
        a = Alert(severity=Severity.LOW, category="x", subject="noise")
        assert "[ELENCHUS:LOW]" in a.envelope_subject()

    def test_metadata_default_empty(self):
        a = Alert(severity=Severity.HIGH, category="x", subject="y")
        assert a.metadata == {}


# ─── Channels ────────────────────────────────────────────────────────


class _RecordingChannel:
    """Test-only channel that captures alerts in a list."""

    def __init__(self):
        self.received: list[Alert] = []

    def send(self, alert: Alert) -> None:
        self.received.append(alert)


class TestConsoleChannel:
    def test_writes_to_severity_matched_level(self, caplog):
        ch = ConsoleAlertChannel("test.alerts")
        with caplog.at_level("DEBUG", logger="test.alerts"):
            ch.send(Alert(Severity.CRITICAL, "auth", "key revoked", "body text"))
            ch.send(Alert(Severity.LOW, "noise", "parse fail"))
        # CRITICAL → CRITICAL level
        assert any(r.levelname == "CRITICAL" for r in caplog.records)
        # LOW → INFO level
        assert any(r.levelname == "INFO" for r in caplog.records)


class _StubEmail:
    def __init__(self):
        self.sent: list[tuple[str, str, str]] = []
        self.fail = False

    def send(self, to, subject, body):
        if self.fail:
            raise RuntimeError("smtp down")
        self.sent.append((to, subject, body))


class TestEmailChannel:
    def test_sends_when_above_min_severity(self):
        email = _StubEmail()
        ch = EmailAlertChannel(
            recipient="ops@example.com",
            email_service=email,
            min_severity=Severity.HIGH,
        )
        ch.send(Alert(Severity.CRITICAL, "x", "bad"))
        assert len(email.sent) == 1
        to, subj, body = email.sent[0]
        assert to == "ops@example.com"
        assert subj.startswith("[ELENCHUS:CRITICAL]")
        assert "bad" in body

    def test_filters_below_min_severity(self):
        email = _StubEmail()
        ch = EmailAlertChannel(
            recipient="ops@example.com",
            email_service=email,
            min_severity=Severity.HIGH,
        )
        ch.send(Alert(Severity.LOW, "x", "noise"))
        ch.send(Alert(Severity.MEDIUM, "x", "noise"))
        assert email.sent == []

    def test_missing_recipient_is_noop(self):
        email = _StubEmail()
        ch = EmailAlertChannel(
            recipient="",
            email_service=email,
            min_severity=Severity.LOW,
        )
        ch.send(Alert(Severity.CRITICAL, "x", "y"))
        assert email.sent == []

    def test_email_failure_does_not_raise(self):
        email = _StubEmail()
        email.fail = True
        ch = EmailAlertChannel(
            recipient="ops@example.com",
            email_service=email,
            min_severity=Severity.LOW,
        )
        # Should swallow the exception.
        ch.send(Alert(Severity.CRITICAL, "x", "y"))

    def test_body_includes_metadata(self):
        email = _StubEmail()
        ch = EmailAlertChannel(
            recipient="ops@example.com",
            email_service=email,
            min_severity=Severity.LOW,
        )
        ch.send(
            Alert(
                Severity.HIGH,
                category="llm.rate_limit",
                subject="rate-limited",
                body="429",
                metadata={"model": "claude-opus-4-6", "attempts": 3},
            )
        )
        body = email.sent[0][2]
        assert "model: claude-opus-4-6" in body
        assert "attempts: 3" in body
        assert "Severity: high" in body
        assert "Category: llm.rate_limit" in body


# ─── Dispatcher ──────────────────────────────────────────────────────


class TestDispatcher:
    def test_fan_out_to_all_channels(self):
        ch_a = _RecordingChannel()
        ch_b = _RecordingChannel()
        d = Dispatcher(channels=[ch_a, ch_b])
        alert = Alert(Severity.HIGH, "x", "y")
        assert d.dispatch(alert) is True
        assert ch_a.received == [alert]
        assert ch_b.received == [alert]

    def test_dedup_within_window(self):
        ch = _RecordingChannel()
        d = Dispatcher(channels=[ch], dedup_window_minutes=5)
        a = Alert(Severity.HIGH, "x", "y")
        now = datetime(2026, 6, 1, 12, 0, 0)
        assert d.dispatch(a, now=now) is True
        # Second identical alert within the window → deduped.
        assert d.dispatch(a, now=now + timedelta(minutes=2)) is False
        assert len(ch.received) == 1

    def test_redispatch_after_window(self):
        ch = _RecordingChannel()
        d = Dispatcher(channels=[ch], dedup_window_minutes=5)
        a = Alert(Severity.HIGH, "x", "y")
        now = datetime(2026, 6, 1, 12, 0, 0)
        assert d.dispatch(a, now=now) is True
        assert d.dispatch(a, now=now + timedelta(minutes=6)) is True
        assert len(ch.received) == 2

    def test_different_categories_not_deduped_together(self):
        ch = _RecordingChannel()
        d = Dispatcher(channels=[ch], dedup_window_minutes=5)
        now = datetime(2026, 6, 1, 12, 0, 0)
        d.dispatch(Alert(Severity.HIGH, "a", "x"), now=now)
        d.dispatch(Alert(Severity.HIGH, "b", "y"), now=now)
        assert len(ch.received) == 2

    def test_different_severities_not_deduped_together(self):
        ch = _RecordingChannel()
        d = Dispatcher(channels=[ch], dedup_window_minutes=5)
        now = datetime(2026, 6, 1, 12, 0, 0)
        d.dispatch(Alert(Severity.HIGH, "x", "subject"), now=now)
        d.dispatch(Alert(Severity.LOW, "x", "subject"), now=now)
        assert len(ch.received) == 2

    def test_critical_bypasses_dedup(self):
        ch = _RecordingChannel()
        d = Dispatcher(channels=[ch], dedup_window_minutes=60)
        now = datetime(2026, 6, 1, 12, 0, 0)
        a = Alert(Severity.CRITICAL, "x", "key revoked")
        # Three back-to-back criticals all go through.
        assert d.dispatch(a, now=now) is True
        assert d.dispatch(a, now=now + timedelta(seconds=1)) is True
        assert d.dispatch(a, now=now + timedelta(seconds=2)) is True
        assert len(ch.received) == 3

    def test_channel_failure_does_not_block_others(self):
        class _Broken:
            def send(self, alert):
                raise RuntimeError("broken sink")

        ok = _RecordingChannel()
        d = Dispatcher(channels=[_Broken(), ok])
        assert d.dispatch(Alert(Severity.HIGH, "x", "y")) is True
        assert len(ok.received) == 1

    def test_add_channel_at_runtime(self):
        d = Dispatcher()
        ch = _RecordingChannel()
        d.add_channel(ch)
        d.dispatch(Alert(Severity.LOW, "x", "y"))
        assert len(ch.received) == 1

    def test_reset_clears_dedup(self):
        ch = _RecordingChannel()
        d = Dispatcher(channels=[ch], dedup_window_minutes=60)
        now = datetime(2026, 6, 1, 12, 0, 0)
        d.dispatch(Alert(Severity.HIGH, "x", "y"), now=now)
        assert d.dispatch(Alert(Severity.HIGH, "x", "y"), now=now) is False
        d.reset()
        assert d.dispatch(Alert(Severity.HIGH, "x", "y"), now=now) is True
        assert len(ch.received) == 2


# ─── Env-driven configuration ────────────────────────────────────────


class TestEnvConfiguration:
    def setup_method(self):
        set_dispatcher_for_tests(None)  # force re-init

    def teardown_method(self):
        set_dispatcher_for_tests(None)

    def test_console_only_by_default(self, monkeypatch):
        monkeypatch.delenv("ALERT_EMAIL_TO", raising=False)
        d = get_dispatcher()
        # Exactly one channel (console).
        assert len(d.channels) == 1
        assert isinstance(d.channels[0], ConsoleAlertChannel)

    def test_email_added_when_alert_email_to_set(self, monkeypatch):
        monkeypatch.setenv("ALERT_EMAIL_TO", "ops@example.com")
        d = get_dispatcher()
        assert len(d.channels) == 2
        kinds = {type(c).__name__ for c in d.channels}
        assert "ConsoleAlertChannel" in kinds
        assert "EmailAlertChannel" in kinds

    def test_dedup_minutes_env(self, monkeypatch):
        monkeypatch.delenv("ALERT_EMAIL_TO", raising=False)
        monkeypatch.setenv("ALERT_DEDUP_MINUTES", "13")
        d = get_dispatcher()
        assert d.dedup_window == timedelta(minutes=13)

    def test_invalid_dedup_minutes_falls_back(self, monkeypatch, caplog):
        monkeypatch.delenv("ALERT_EMAIL_TO", raising=False)
        monkeypatch.setenv("ALERT_DEDUP_MINUTES", "not-a-number")
        with caplog.at_level("WARNING", logger="elenchus.alerting"):
            d = get_dispatcher()
        assert d.dedup_window == timedelta(minutes=5)
        assert any("Invalid ALERT_DEDUP_MINUTES" in r.message for r in caplog.records)


# ─── ChatResult → Alert mapping ──────────────────────────────────────


class TestChatFailureMapping:
    def setup_method(self):
        # Install a recording channel for assertions.
        self.ch = _RecordingChannel()
        set_dispatcher_for_tests(Dispatcher(channels=[self.ch], dedup_window_minutes=5))

    def teardown_method(self):
        set_dispatcher_for_tests(None)

    def _result(self, category: ChatCategory, **kwargs) -> ChatResult:
        defaults = {
            "category": category,
            "attempts": 1,
            "latency_ms": 100,
            "model": "claude-opus-4-6",
        }
        defaults.update(kwargs)
        return ChatResult(**defaults)

    def test_success_emits_nothing(self):
        ok = self._result(ChatCategory.SUCCESS, text="hi")
        assert dispatch_for_chat_failure(ok) is False
        assert self.ch.received == []

    def test_auth_failure_is_critical(self):
        result = self._result(
            ChatCategory.AUTH_FAILURE,
            error_message="invalid api key",
            exception_type="AuthenticationError",
        )
        assert dispatch_for_chat_failure(result) is True
        alert = self.ch.received[0]
        assert alert.severity == Severity.CRITICAL
        assert alert.category == "llm.auth_failure"
        assert "claude-opus-4-6" in alert.subject
        assert alert.metadata["model"] == "claude-opus-4-6"
        assert alert.metadata["exception_type"] == "AuthenticationError"

    def test_rate_limit_is_high(self):
        r = self._result(ChatCategory.RATE_LIMIT, attempts=3)
        assert dispatch_for_chat_failure(r) is True
        assert self.ch.received[0].severity == Severity.HIGH

    def test_provider_error_is_high(self):
        r = self._result(ChatCategory.PROVIDER_ERROR)
        assert dispatch_for_chat_failure(r) is True
        assert self.ch.received[0].severity == Severity.HIGH

    def test_timeout_is_medium(self):
        r = self._result(ChatCategory.TIMEOUT)
        assert dispatch_for_chat_failure(r) is True
        assert self.ch.received[0].severity == Severity.MEDIUM

    def test_content_policy_is_medium(self):
        r = self._result(ChatCategory.CONTENT_POLICY)
        assert dispatch_for_chat_failure(r) is True
        assert self.ch.received[0].severity == Severity.MEDIUM

    def test_token_overflow_is_low(self):
        r = self._result(ChatCategory.TOKEN_OVERFLOW)
        assert dispatch_for_chat_failure(r) is True
        assert self.ch.received[0].severity == Severity.LOW

    def test_unknown_is_high(self):
        r = self._result(ChatCategory.UNKNOWN)
        assert dispatch_for_chat_failure(r) is True
        assert self.ch.received[0].severity == Severity.HIGH

    def test_actor_and_base_carried_in_metadata(self):
        r = self._result(ChatCategory.RATE_LIMIT)
        dispatch_for_chat_failure(r, actor_id=42, base_id="my-base")
        alert = self.ch.received[0]
        assert alert.metadata["actor_id"] == 42
        assert alert.metadata["base_id"] == "my-base"


# ─── Integration: Opponent failure dispatches an alert ───────────────


class TestOpponentEmitsAlerts:
    """End-to-end: Opponent failure → record_usage row AND alert
    dispatched through the configured channels."""

    def setup_method(self):
        self.ch = _RecordingChannel()
        set_dispatcher_for_tests(Dispatcher(channels=[self.ch], dedup_window_minutes=5))

    def teardown_method(self):
        set_dispatcher_for_tests(None)

    def test_opponent_failure_dispatches_alert(self):
        from unittest.mock import patch

        from elenchus.dialectical_state import DialecticalState
        from elenchus.opponent import LLMCallError, Opponent

        opp = Opponent(api_key=None, model="claude-opus-4-6")
        state = DialecticalState.in_memory("test")

        fail = ChatResult(
            category=ChatCategory.AUTH_FAILURE,
            attempts=1,
            latency_ms=42,
            model="claude-opus-4-6",
            error_message="invalid api key",
            exception_type="AuthenticationError",
        )
        with (
            patch.object(opp._llm_client, "chat", return_value=fail),
            pytest.raises(LLMCallError),
        ):
            opp.respond("hi", state)

        assert len(self.ch.received) == 1
        assert self.ch.received[0].severity == Severity.CRITICAL
        state.base.con.close()

    def test_opponent_success_dispatches_nothing(self):
        from unittest.mock import patch

        from elenchus.dialectical_state import DialecticalState
        from elenchus.opponent import Opponent

        opp = Opponent(api_key=None, model="claude-opus-4-6")
        state = DialecticalState.in_memory("test")

        ok = ChatResult(
            category=ChatCategory.SUCCESS,
            text='{"speech_acts":[],"new_tensions":[],"response":"ok"}',
            attempts=1,
            latency_ms=10,
            model="claude-opus-4-6",
            prompt_tokens=5,
            completion_tokens=2,
        )
        with patch.object(opp._llm_client, "chat", return_value=ok):
            opp.respond("hi", state)

        assert self.ch.received == []
        state.base.con.close()
