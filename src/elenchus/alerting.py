"""
alerting.py — operational alerts with severity tiers, channels, and
rate-limited dispatch.

Subsystems that detect operational problems (LLM auth failure,
sustained rate-limit storm, budget overrun, server crash) emit
`Alert` instances through this module's `dispatch()` function. A
process-wide `Dispatcher` fans them out to configured channels
(console, email) while applying a per-(severity, category) dedup
window so a sustained failure doesn't flood an operator's inbox.

Severity policy:
  * CRITICAL — operator action required to keep the platform running
    (API key revoked, budget hard cap hit). Never deduped.
  * HIGH     — degraded service, retries exhausted, sustained
    upstream errors. Deduped within the window.
  * MEDIUM   — single deterministic failure (content policy refusal,
    timeout). Deduped.
  * LOW      — forensic noise (parse failure, token overflow on a
    runaway prompt). Deduped.

Channels:
  * `ConsoleAlertChannel` — always installed, writes to the
    `elenchus.alerts` logger at the level matching severity.
  * `EmailAlertChannel` — installed when `ALERT_EMAIL_TO` is set.
    Per-channel `min_severity` (default HIGH) filters out the low-
    importance noise.

Configuration via env vars:
  * `ALERT_EMAIL_TO`             — recipient for the email channel.
  * `ALERT_EMAIL_MIN_SEVERITY`   — `critical|high|medium|low` (default: high)
  * `ALERT_DEDUP_MINUTES`        — dedup window (default: 5)
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from .llm_client import ChatCategory, ChatResult

logger = logging.getLogger(__name__)


# ── Severity ─────────────────────────────────────────────────────────


class Severity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


# Strict ordering for `>=` comparisons in channel filters.
_SEVERITY_RANK = {
    Severity.LOW: 0,
    Severity.MEDIUM: 1,
    Severity.HIGH: 2,
    Severity.CRITICAL: 3,
}


def _at_least(severity: Severity, minimum: Severity) -> bool:
    return _SEVERITY_RANK[severity] >= _SEVERITY_RANK[minimum]


def parse_severity(s: str | None, default: Severity = Severity.HIGH) -> Severity:
    """Forgiving env-var parser. Unknown strings fall back to default
    with a logged warning rather than crashing the dispatcher."""
    if not s:
        return default
    try:
        return Severity(s.strip().lower())
    except ValueError:
        logger.warning("Invalid severity %r; falling back to %s", s, default.value)
        return default


# ── Alert ────────────────────────────────────────────────────────────


@dataclass
class Alert:
    """One alert. `category` is the dedup grouping key — alerts in the
    same (severity, category) bucket are coalesced within the
    dispatcher's window. Use stable strings ("llm.rate_limit",
    "budget.exceeded") rather than free-form variations."""

    severity: Severity
    category: str
    subject: str
    body: str = ""
    metadata: dict = field(default_factory=dict)

    def envelope_subject(self) -> str:
        """Subject formatted for inbox filtering: `[ELENCHUS:HIGH] ...`."""
        return f"[ELENCHUS:{self.severity.value.upper()}] {self.subject}"


# ── Channels ─────────────────────────────────────────────────────────


class AlertChannel(Protocol):
    """A sink for alerts. Implementations must be safe to call from
    the dispatcher's thread (i.e. don't block indefinitely)."""

    def send(self, alert: Alert) -> None: ...


class ConsoleAlertChannel:
    """Logs alerts at the severity-matched log level. Always installed
    so operators see alerts even before email is configured."""

    _LEVEL = {
        Severity.CRITICAL: logging.CRITICAL,
        Severity.HIGH: logging.ERROR,
        Severity.MEDIUM: logging.WARNING,
        Severity.LOW: logging.INFO,
    }

    def __init__(self, logger_name: str = "elenchus.alerts") -> None:
        self.logger = logging.getLogger(logger_name)

    def send(self, alert: Alert) -> None:
        self.logger.log(
            self._LEVEL[alert.severity],
            "ALERT [%s] %s: %s%s",
            alert.severity.value,
            alert.category,
            alert.subject,
            f" — {alert.body}" if alert.body else "",
        )


class EmailAlertChannel:
    """Sends alerts via the configured `EmailService`. Filters by
    `min_severity` so low-importance alerts don't go to email even
    when the channel is installed."""

    def __init__(
        self,
        *,
        recipient: str,
        email_service,
        min_severity: Severity = Severity.HIGH,
    ) -> None:
        self.recipient = recipient
        self.email_service = email_service
        self.min_severity = min_severity

    def send(self, alert: Alert) -> None:
        if not _at_least(alert.severity, self.min_severity):
            return
        if not self.recipient or self.email_service is None:
            return
        try:
            self.email_service.send(
                to=self.recipient,
                subject=alert.envelope_subject(),
                body=_format_body(alert),
            )
        except Exception:
            # Email delivery failure must not crash the dispatcher.
            logger.exception("EmailAlertChannel.send failed")


def _format_body(alert: Alert) -> str:
    lines = [alert.body or alert.subject, ""]
    if alert.metadata:
        lines.append("Details:")
        for k, v in sorted(alert.metadata.items()):
            lines.append(f"  {k}: {v}")
    lines.append("")
    lines.append(f"Severity: {alert.severity.value}")
    lines.append(f"Category: {alert.category}")
    return "\n".join(lines)


# ── Dispatcher ───────────────────────────────────────────────────────


class Dispatcher:
    """Fans alerts to channels with a per-(severity, category) dedup
    window. CRITICAL alerts bypass dedup — every one is sent.

    Thread-safe: a single internal lock guards the dedup state and
    channel registration.
    """

    def __init__(
        self,
        channels: list[AlertChannel] | None = None,
        dedup_window_minutes: int = 5,
    ) -> None:
        self.channels: list[AlertChannel] = list(channels or [])
        self.dedup_window = timedelta(minutes=dedup_window_minutes)
        self._recent: dict[tuple[Severity, str], datetime] = {}
        self._lock = threading.Lock()

    def add_channel(self, channel: AlertChannel) -> None:
        with self._lock:
            self.channels.append(channel)

    def dispatch(self, alert: Alert, *, now: datetime | None = None) -> bool:
        """Send `alert` through every channel. Returns True if it was
        actually dispatched (not deduped), False if it fell inside an
        existing dedup window. `now` is an injection point for tests;
        production callers leave it unset."""
        now = now or datetime.now()
        key = (alert.severity, alert.category)

        with self._lock:
            if alert.severity != Severity.CRITICAL:
                last = self._recent.get(key)
                if last is not None and (now - last) < self.dedup_window:
                    logger.debug(
                        "Alert deduped: %s/%s (last sent %.0fs ago)",
                        alert.severity.value,
                        alert.category,
                        (now - last).total_seconds(),
                    )
                    return False
            self._recent[key] = now
            # Garbage-collect entries older than 2× the dedup window.
            cutoff = now - (self.dedup_window * 2)
            self._recent = {k: v for k, v in self._recent.items() if v > cutoff}
            channels = list(self.channels)

        for ch in channels:
            try:
                ch.send(alert)
            except Exception:
                logger.exception("Alert channel %s.send raised; continuing", type(ch).__name__)
        return True

    def reset(self) -> None:
        """Clear dedup state. Used by tests; production restart is the
        normal way to start fresh."""
        with self._lock:
            self._recent.clear()


# ── Module-level singleton + env-driven config ───────────────────────


_dispatcher: Dispatcher | None = None
_dispatcher_lock = threading.Lock()


def _build_dispatcher_from_env() -> Dispatcher:
    """Build a Dispatcher from env vars. Always includes the console
    channel; adds the email channel if `ALERT_EMAIL_TO` is set."""
    channels: list[AlertChannel] = [ConsoleAlertChannel()]

    recipient = os.environ.get("ALERT_EMAIL_TO", "").strip()
    if recipient:
        min_sev = parse_severity(os.environ.get("ALERT_EMAIL_MIN_SEVERITY"), default=Severity.HIGH)
        # Lazy import: avoids a hard EmailService dep if email isn't configured.
        from . import email_service as _email_mod

        channels.append(
            EmailAlertChannel(
                recipient=recipient,
                email_service=_email_mod.get_email_service(),
                min_severity=min_sev,
            )
        )

    dedup_min = 5
    raw = os.environ.get("ALERT_DEDUP_MINUTES")
    if raw:
        try:
            dedup_min = max(0, int(raw))
        except ValueError:
            logger.warning("Invalid ALERT_DEDUP_MINUTES=%r; using default 5", raw)

    return Dispatcher(channels=channels, dedup_window_minutes=dedup_min)


def get_dispatcher() -> Dispatcher:
    """Return the process-wide Dispatcher, building it lazily from env
    on first access. Safe to call concurrently."""
    global _dispatcher
    with _dispatcher_lock:
        if _dispatcher is None:
            _dispatcher = _build_dispatcher_from_env()
        return _dispatcher


def set_dispatcher_for_tests(d: Dispatcher | None) -> None:
    """Test hook — install or clear the singleton."""
    global _dispatcher
    with _dispatcher_lock:
        _dispatcher = d


def dispatch(alert: Alert) -> bool:
    """Convenience: dispatch via the process-wide Dispatcher."""
    return get_dispatcher().dispatch(alert)


# ── ChatResult → Alert mapping ───────────────────────────────────────


_CATEGORY_TO_SEVERITY: dict[ChatCategory, Severity] = {
    # Operator-attention failures — must surface immediately.
    ChatCategory.AUTH_FAILURE: Severity.CRITICAL,
    # Sustained upstream problem; user-visible.
    ChatCategory.RATE_LIMIT: Severity.HIGH,
    ChatCategory.PROVIDER_ERROR: Severity.HIGH,
    ChatCategory.UNKNOWN: Severity.HIGH,
    # Single-call deterministic failures; worth knowing but not paging.
    ChatCategory.TIMEOUT: Severity.MEDIUM,
    ChatCategory.NETWORK_ERROR: Severity.MEDIUM,
    ChatCategory.CONTENT_POLICY: Severity.MEDIUM,
    # Forensic — usually a malformed prompt.
    ChatCategory.TOKEN_OVERFLOW: Severity.LOW,
    ChatCategory.BAD_REQUEST: Severity.LOW,
}


def dispatch_for_chat_failure(
    result: ChatResult,
    *,
    actor_id: int | None = None,
    base_id: str | None = None,
) -> bool:
    """Translate a non-success `ChatResult` into an `Alert` and
    dispatch it. No-op on success. Returns True if the alert was
    dispatched (not deduped)."""
    if result.ok:
        return False

    severity = _CATEGORY_TO_SEVERITY.get(result.category, Severity.HIGH)
    alert = Alert(
        severity=severity,
        category=f"llm.{result.category.value}",
        subject=f"LLM {result.category.value} after {result.attempts} attempt(s) on {result.model}",
        body=(result.error_message or ""),
        metadata={
            "model": result.model,
            "category": result.category.value,
            "attempts": result.attempts,
            "latency_ms": result.latency_ms,
            "exception_type": result.exception_type or "",
            "actor_id": actor_id if actor_id is not None else "",
            "base_id": base_id if base_id is not None else "",
        },
    )
    return dispatch(alert)
