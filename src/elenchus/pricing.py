"""
pricing.py — per-model cost rates and a `compute_cost` helper.

Rates are expressed in USD per million tokens (input and output
separately) since that's how the providers publish them. The lookup
is by exact model name, with a substring-prefix fallback so dated
revisions of a model (`claude-opus-4-6-20260301`) still match.

Defaults are reasonable as of mid-2026. The provider pricing pages
are the source of truth; this dict gets stale. Operators can override
via the `ELENCHUS_PRICING_JSON` env var, which holds a JSON object
mapping model name → `{"input_per_1m": <usd>, "output_per_1m": <usd>}`.

Unknown models return zero cost and emit a logged warning rather than
guessing — better to under-report than to silently invent numbers
that diverge from the provider invoice.
"""

from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)


# USD per 1 million tokens. (input, output)
_DEFAULT_PRICING: dict[str, tuple[float, float]] = {
    # Anthropic Claude (current as of mid-2026)
    "claude-opus-4-6": (15.00, 75.00),
    "claude-opus-4": (15.00, 75.00),
    "claude-sonnet-5-0": (3.00, 15.00),
    "claude-sonnet-4-0": (3.00, 15.00),
    "claude-haiku-4-0": (0.80, 4.00),
    "claude-3-5-sonnet": (3.00, 15.00),
    "claude-3-5-haiku": (0.80, 4.00),
    # OpenAI
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4-turbo": (10.00, 30.00),
    "o1": (15.00, 60.00),
    "o1-mini": (3.00, 12.00),
}


_PRICING_CACHE: dict[str, tuple[float, float]] | None = None


def _load_pricing() -> dict[str, tuple[float, float]]:
    """Combine default rates with the `ELENCHUS_PRICING_JSON` override.
    Cached after first call; the env var is consulted exactly once
    per process. Operators wanting a refresh should restart."""
    global _PRICING_CACHE
    if _PRICING_CACHE is not None:
        return _PRICING_CACHE

    table = dict(_DEFAULT_PRICING)
    raw = os.environ.get("ELENCHUS_PRICING_JSON", "").strip()
    if raw:
        try:
            override = json.loads(raw)
            for name, rates in override.items():
                if isinstance(rates, dict):
                    inp = float(rates.get("input_per_1m", 0))
                    outp = float(rates.get("output_per_1m", 0))
                    table[name] = (inp, outp)
                elif isinstance(rates, list | tuple) and len(rates) == 2:
                    table[name] = (float(rates[0]), float(rates[1]))
            logger.info(
                "Loaded %d pricing overrides from ELENCHUS_PRICING_JSON",
                len(override),
            )
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            logger.warning("Failed to parse ELENCHUS_PRICING_JSON; using defaults only: %s", e)

    _PRICING_CACHE = table
    return table


def _reset_cache_for_tests() -> None:
    """Test hook — reset the cache so a test that monkey-patches the
    env var sees its change. Not for production use."""
    global _PRICING_CACHE
    _PRICING_CACHE = None


def lookup_rates(model: str) -> tuple[float, float] | None:
    """Return `(input_per_1m, output_per_1m)` for `model`, or None if
    unknown. Exact match first; then prefix match (so dated revisions
    like `claude-opus-4-6-20260301` resolve to the family rate)."""
    table = _load_pricing()
    if model in table:
        return table[model]
    # Prefix fallback — match the longest registered prefix so
    # `claude-opus-4-6` wins over `claude-opus-4` for an opus-4-6 rev.
    best: tuple[str, tuple[float, float]] | None = None
    for known, rates in table.items():
        if model.startswith(known) and (best is None or len(known) > len(best[0])):
            best = (known, rates)
    return best[1] if best else None


def compute_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Return the USD cost of one call. Returns 0.0 for unknown models
    (with a one-shot warning, deduped by model name)."""
    rates = lookup_rates(model)
    if rates is None:
        _warn_unknown_model(model)
        return 0.0
    input_per_1m, output_per_1m = rates
    return (prompt_tokens * input_per_1m + completion_tokens * output_per_1m) / 1_000_000.0


_WARNED: set[str] = set()


def _warn_unknown_model(model: str) -> None:
    if model in _WARNED:
        return
    _WARNED.add(model)
    logger.warning(
        "No pricing rate registered for model %r; cost will be recorded as 0. "
        "Set ELENCHUS_PRICING_JSON to override.",
        model,
    )
