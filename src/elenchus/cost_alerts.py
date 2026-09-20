"""
cost_alerts.py — notice when a day's LLM spend is out of the ordinary.

The grant's LLM line is large next to what a study costs, so a
"percentage of budget" alert would never fire in time to matter. What
can actually go wrong is a **runaway**: a client stuck in a loop, a
script hammering the message route, a model swapped for one ten times
the price. Those show up as one day costing far more than a day should,
so that is what is watched.

After every recorded LLM call, today's spend (priced from tokens, the
same way the dashboard does it) is compared with a daily threshold:

  * crossing the threshold fires a HIGH alert;
  * each further multiple of it (2×, 3×, …) fires again, as CRITICAL —
    a runaway keeps announcing itself instead of going quiet after the
    first message, and the number of alerts is bounded by the spend.

Each level has its own alert category, so the dispatcher's dedup window
can't swallow "2×" because "1×" went out four minutes earlier. What has
been fired today is kept in `platform_settings`, so a restart doesn't
repeat it.

A model with no rate can't be priced, so its runaway would be invisible
here: the first unpriced tokens of a day fire a MEDIUM alert saying so.

The threshold is the admin's setting (Costs tab, `PUT
/api/admin/costs/alert`), else `ELENCHUS_DAILY_SPEND_ALERT_USD`, else
$25. Zero turns the check off. Alerts go wherever `alerting.py` sends
them — the server log always, email when `ALERT_EMAIL_TO` is set.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date

from . import alerting, costs

logger = logging.getLogger(__name__)

SETTING_KEY = "cost_alert"
STATE_KEY = "cost_alert_state"
ENV_VAR = "ELENCHUS_DAILY_SPEND_ALERT_USD"
DEFAULT_DAILY_USD = 25.0
MAX_DAILY_USD = 1_000_000


def _env_threshold() -> tuple[float, str]:
    raw = os.environ.get(ENV_VAR, "").strip()
    if raw:
        try:
            value = float(raw)
            if 0 <= value <= MAX_DAILY_USD:
                return value, "env"
        except ValueError:
            pass
        logger.warning("Ignoring %s=%r: not a number of dollars", ENV_VAR, raw)
    return DEFAULT_DAILY_USD, "default"


def get_threshold(con) -> tuple[float, str]:
    """`(dollars per day, where it came from)` — 'setting', 'env' or
    'default'. Zero means the check is off."""
    from .db import platform as pdb

    raw = pdb.get_setting(con, SETTING_KEY)
    if raw:
        try:
            return float(json.loads(raw)["daily_usd"]), "setting"
        except (ValueError, KeyError, TypeError):
            logger.warning("platform_settings[%s] is malformed; ignoring", SETTING_KEY)
    return _env_threshold()


def set_threshold(con, daily_usd) -> tuple[float, str]:
    """Store the admin's threshold (0 = off), or with None go back to
    the environment / default. Caller holds the platform lock."""
    from .db import platform as pdb

    if daily_usd is None or daily_usd == "":
        pdb.set_setting(con, SETTING_KEY, "")
        return get_threshold(con)
    try:
        value = float(daily_usd)
    except (TypeError, ValueError):
        raise ValueError("The daily alert threshold must be a number of US dollars.") from None
    if value != value or not 0 <= value <= MAX_DAILY_USD:
        raise ValueError("The daily alert threshold must be zero (off) or more.")
    pdb.set_setting(con, SETTING_KEY, json.dumps({"daily_usd": round(value, 2)}))
    return get_threshold(con)


def _state(con) -> dict:
    from .db import platform as pdb

    try:
        return json.loads(pdb.get_setting(con, STATE_KEY) or "{}")
    except json.JSONDecodeError:
        return {}


def today_spend(con, today: date | None = None) -> dict:
    """Today's LLM spend, priced from tokens."""
    today = today or date.today()
    return costs.summarize(costs.priced_groups(con, since=today))


def status(con, today: date | None = None) -> dict:
    """The `alert` block of the cost report."""
    today = today or date.today()
    threshold, source = get_threshold(con)
    spend = today_spend(con, today)
    return {
        "daily_usd": threshold,
        "source": source,
        "enabled": threshold > 0,
        "today_usd": spend["cost_usd"],
        "today_unpriced_tokens": spend["unpriced_tokens"],
        "exceeded": threshold > 0 and spend["cost_usd"] >= threshold,
        "times_threshold": (spend["cost_usd"] / threshold) if threshold > 0 else None,
        "emailed": bool(os.environ.get("ALERT_EMAIL_TO", "").strip()),
    }


def check(con, *, today: date | None = None) -> list[alerting.Alert]:
    """Compare today's spend with the threshold and dispatch whatever
    hasn't been said yet today. Returns the alerts dispatched. Caller
    holds the platform lock (the fired-state is written here)."""
    from .db import platform as pdb

    today = today or date.today()
    threshold, source = get_threshold(con)
    spend = today_spend(con, today)
    state = _state(con)
    if state.get("day") != today.isoformat():
        state = {"day": today.isoformat(), "level": 0, "unpriced": False}
    fired: list[alerting.Alert] = []

    if threshold > 0:
        level = int(spend["cost_usd"] // threshold)
        if level > state.get("level", 0):
            severity = alerting.Severity.HIGH if level == 1 else alerting.Severity.CRITICAL
            fired.append(
                alerting.Alert(
                    severity=severity,
                    # One category per level: the dispatcher dedups on
                    # (severity, category), and 2× must not be swallowed
                    # because 1× went out a few minutes ago.
                    category=f"cost.daily_spend.x{level}",
                    subject=(
                        f"LLM spend today is ${spend['cost_usd']:.2f} — "
                        f"{level}× the ${threshold:.2f} daily alert threshold"
                    ),
                    body=(
                        "A day costing this much usually means a runaway: a client in a "
                        "loop, a script on the message route, or a far more expensive "
                        "model. The Costs tab shows today's spend by model, purpose and "
                        "account. Nothing has been cut off."
                    ),
                    metadata={
                        "day": today.isoformat(),
                        "spend_usd": round(spend["cost_usd"], 4),
                        "threshold_usd": threshold,
                        "threshold_source": source,
                        "level": level,
                        "calls": spend["calls"],
                        "prompt_tokens": spend["prompt_tokens"],
                        "completion_tokens": spend["completion_tokens"],
                    },
                )
            )
            state["level"] = level

    if spend["unpriced_tokens"] > 0 and not state.get("unpriced"):
        fired.append(
            alerting.Alert(
                severity=alerting.Severity.MEDIUM,
                category="cost.unpriced_model",
                subject=(
                    f"{spend['unpriced_tokens']:,} tokens today are on a model with no rate — "
                    "their cost can't be watched"
                ),
                body=(
                    "The daily spend alert prices tokens from the price table, so usage on a "
                    "model without a rate is invisible to it. Add the rate with "
                    "ELENCHUS_PRICING_JSON (the Costs tab lists the model)."
                ),
                metadata={"day": today.isoformat(), "unpriced_tokens": spend["unpriced_tokens"]},
            )
        )
        state["unpriced"] = True

    if fired:
        pdb.set_setting(con, STATE_KEY, json.dumps(state))
        for alert in fired:
            logger.warning("Cost alert: %s %s", alert.category, alert.metadata)
            alerting.dispatch(alert)
    return fired
