"""
pricing.py — a dated per-model price table and the `compute_cost` helper.

**Tokens are the source of truth; dollars are derived.** The `usage`
table records the token counts of every LLM call. Every figure the
platform shows is computed *when it is read*, by pricing those tokens
against this table — see `costs.py`. That way a stale or missing rate
is fixed by correcting the table, not by rewriting history, and a
provider's price change (a new entry with an `effective_from` date)
doesn't reprice the calls made before it.

Rates are USD per million tokens (input and output separately), which
is how the providers publish them. The provider's pricing page is the
source of truth and this table goes stale: `PRICES_AS_OF` says when it
was last checked, and the cost dashboard shows that date. Operators
correct or extend it with the `ELENCHUS_PRICING_JSON` env var — a JSON
object mapping model name to either one rate or a list of dated rates:

    {"my-model": {"input_per_1m": 1.0, "output_per_1m": 2.0},
     "claude-opus-5": [
        {"input_per_1m": 5, "output_per_1m": 25},
        {"input_per_1m": 4, "output_per_1m": 20, "effective_from": "2027-01-01"}]}

An override replaces every built-in entry for that model name.

Lookup normalizes the model name (a routing prefix such as
`anthropic/` is dropped, `.` becomes `-`, so OpenRouter's
`anthropic/claude-sonnet-4.6` finds `claude-sonnet-4-6`), then takes
the longest registered name the model starts with — so a dated revision
(`claude-haiku-4-5-20251001`) resolves to its family — and, within that
name, the latest entry in effect on the day of the call.

A model with no rate is **unpriced**, never free: `lookup_rates`
returns None, `compute_cost` returns 0.0 and logs a warning once per
model, and the dashboard lists the model and its unpriced tokens
rather than showing $0.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime

logger = logging.getLogger(__name__)


# When the built-in table was last checked against the providers'
# published prices. Shown on the cost dashboard.
PRICES_AS_OF = "2026-09-19"


@dataclass(frozen=True)
class Rate:
    """One price for one model name, from `effective_from` onwards
    (None = since the beginning)."""

    model: str
    input_per_1m: float
    output_per_1m: float
    effective_from: date | None = None


def _r(model: str, inp: float, outp: float, effective_from: date | None = None) -> Rate:
    return Rate(model, inp, outp, effective_from)


# USD per 1 million tokens. Keys are normalized model-name prefixes;
# keep them as specific as the provider's naming allows, so a future
# model doesn't silently inherit an older sibling's rate.
_DEFAULT_RATES: tuple[Rate, ...] = (
    # Anthropic Claude
    _r("claude-fable-5", 10.00, 50.00),
    _r("claude-mythos-5", 10.00, 50.00),
    _r("claude-opus-5", 5.00, 25.00),
    _r("claude-opus-4-8", 5.00, 25.00),
    _r("claude-opus-4-7", 5.00, 25.00),
    _r("claude-opus-4-6", 5.00, 25.00),
    _r("claude-opus-4-5", 5.00, 25.00),
    _r("claude-opus-4-1", 15.00, 75.00),
    _r("claude-opus-4-0", 15.00, 75.00),
    _r("claude-opus-4-2025", 15.00, 75.00),  # claude-opus-4-20250514
    _r("claude-sonnet-5", 2.00, 10.00),
    _r("claude-sonnet-4-6", 3.00, 15.00),
    _r("claude-sonnet-4-5", 3.00, 15.00),
    _r("claude-sonnet-4-0", 3.00, 15.00),
    _r("claude-sonnet-4-2025", 3.00, 15.00),  # claude-sonnet-4-20250514
    _r("claude-haiku-4-5", 1.00, 5.00),
    _r("claude-3-7-sonnet", 3.00, 15.00),
    _r("claude-3-5-sonnet", 3.00, 15.00),
    _r("claude-3-5-haiku", 0.80, 4.00),
    # OpenAI
    _r("gpt-4o-mini", 0.15, 0.60),
    _r("gpt-4o", 2.50, 10.00),
    _r("gpt-4-turbo", 10.00, 30.00),
    _r("o1-mini", 3.00, 12.00),
    _r("o1", 15.00, 60.00),
)


def normalize_model(model: str) -> str:
    """The form model names are matched in: lower case, any routing
    prefix (`anthropic/…`, `openrouter/anthropic/…`) dropped, `.` → `-`."""
    name = (model or "").strip().lower()
    if "/" in name:
        name = name.rsplit("/", 1)[1]
    return name.replace(".", "-")


_PRICING_CACHE: dict[str, tuple[Rate, ...]] | None = None


def _parse_date(value) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _parse_override_entry(name: str, raw) -> Rate:
    if isinstance(raw, dict):
        return Rate(
            name,
            float(raw.get("input_per_1m", 0)),
            float(raw.get("output_per_1m", 0)),
            _parse_date(raw.get("effective_from")),
        )
    if isinstance(raw, list | tuple) and len(raw) == 2:
        return Rate(name, float(raw[0]), float(raw[1]))
    raise ValueError(f"unrecognized rate for {name!r}: {raw!r}")


def _is_rate_pair(raw) -> bool:
    return (
        isinstance(raw, list | tuple)
        and len(raw) == 2
        and all(isinstance(x, int | float) for x in raw)
    )


def _load_pricing() -> dict[str, tuple[Rate, ...]]:
    """Built-in rates combined with the `ELENCHUS_PRICING_JSON`
    override, keyed by normalized model name, each model's entries in
    `effective_from` order. Cached after the first call; the env var
    is read once per process — restart to pick up a change."""
    global _PRICING_CACHE
    if _PRICING_CACHE is not None:
        return _PRICING_CACHE

    table: dict[str, list[Rate]] = {}
    for rate in _DEFAULT_RATES:
        table.setdefault(normalize_model(rate.model), []).append(rate)

    raw = os.environ.get("ELENCHUS_PRICING_JSON", "").strip()
    if raw:
        try:
            override = json.loads(raw)
            parsed: dict[str, list[Rate]] = {}
            for name, rates in override.items():
                key = normalize_model(name)
                entries = (
                    rates if isinstance(rates, list) and not _is_rate_pair(rates) else [rates]
                )
                parsed[key] = [_parse_override_entry(key, e) for e in entries]
            table.update(parsed)
            logger.info("Loaded %d pricing overrides from ELENCHUS_PRICING_JSON", len(parsed))
        except (json.JSONDecodeError, ValueError, TypeError, AttributeError) as e:
            logger.warning("Failed to parse ELENCHUS_PRICING_JSON; using defaults only: %s", e)

    _PRICING_CACHE = {
        key: tuple(sorted(entries, key=lambda r: r.effective_from or date.min))
        for key, entries in table.items()
    }
    return _PRICING_CACHE


def _reset_cache_for_tests() -> None:
    """Test hook — reset the cache so a test that monkey-patches the
    env var sees its change. Not for production use."""
    global _PRICING_CACHE
    _PRICING_CACHE = None
    _WARNED.clear()


def lookup_rate(model: str, on: date | datetime | str | None = None) -> Rate | None:
    """The `Rate` that applies to `model` on the day `on` (default:
    today), or None if the model is unpriced on that day. Longest
    registered name the model starts with; within it, the latest entry
    whose `effective_from` is not after `on`."""
    table = _load_pricing()
    name = normalize_model(model)
    day = _parse_date(on) or date.today()
    for key in sorted((k for k in table if name.startswith(k)), key=len, reverse=True):
        in_effect = [r for r in table[key] if r.effective_from is None or r.effective_from <= day]
        if in_effect:
            return in_effect[-1]
    return None


def lookup_rates(
    model: str, on: date | datetime | str | None = None
) -> tuple[float, float] | None:
    """`(input_per_1m, output_per_1m)` for `model` on `on`, or None if
    unpriced."""
    rate = lookup_rate(model, on)
    return (rate.input_per_1m, rate.output_per_1m) if rate else None


def compute_cost(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    on: date | datetime | str | None = None,
) -> float:
    """USD cost of `prompt_tokens` + `completion_tokens` on `model` at
    the rate in effect on `on` (default: today). Returns 0.0 for an
    unpriced model, with a warning logged once per model — callers
    that must tell "free" from "unpriced" use `lookup_rate`."""
    rate = lookup_rate(model, on)
    if rate is None:
        _warn_unknown_model(model)
        return 0.0
    return (
        prompt_tokens * rate.input_per_1m + completion_tokens * rate.output_per_1m
    ) / 1_000_000.0


_WARNED: set[str] = set()


def _warn_unknown_model(model: str) -> None:
    if model in _WARNED:
        return
    _WARNED.add(model)
    logger.warning(
        "No pricing rate registered for model %r; its tokens are reported as "
        "unpriced (cost 0). Add a rate with ELENCHUS_PRICING_JSON.",
        model,
    )
