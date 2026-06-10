"""
study_reports.py — extractive structured-report generation for the
Sloan blinded-judge interface.

Both experimental conditions (Elenchus / AI-as-collaborator and
baseline / AI-as-tool) produce per-session artifacts: a material base
(atoms + positions + accepted implications + transcript) for Elenchus,
a conversation transcript for baseline. Blinded judges need to assess
both side by side, but the artifacts themselves have very different
structure — a judge could trivially tell which condition produced
which output just from format.

This module strips that structural tell. The same prompt template
runs over both: "given the source material below, extract a conceptual
specification in this format". The LLM produces a uniformly-shaped
report (atomic statements, implications, optional notes). The
materials going in differ; the report coming out doesn't.

Key invariant: `REPORT_PROMPT_TEMPLATE` is condition-agnostic. The
only condition-specific code is the input formatter — `_format_*`
functions that turn the session artifact into the `{source_material}`
slot. A regression here would compromise the blinding, so a test
asserts the prompt never mentions either condition name.
"""

from __future__ import annotations

import logging

from .dialectical_state import DialecticalState

logger = logging.getLogger(__name__)


# ── The single template both conditions share ───────────────────────


REPORT_PROMPT_TEMPLATE = """You are extracting a conceptual specification from source material a domain expert produced while working with an AI. Output a structured report in the exact format below — no preamble, no commentary, no markdown fences.

# Domain
A short, neutral paragraph naming the domain.

# Atomic statements
A numbered list (1., 2., 3., ...) of atomic, declarative statements the expert is committed to about the domain. Each statement is a single clean sentence. No conjunctions, no conditionals (those go in Implications), no metadata annotations.

# Implications
A numbered list (1., 2., 3., ...) of rules: "If X, then Y" — the inferential commitments endorsed in the source material. Each implication is one antecedent → one consequent in plain English. Use the same vocabulary as the atomic statements.

# Notes
Any qualifying remarks, open questions, or scope limitations from the source material. One short paragraph; "None." if there are no qualifications.

The source material below is what you extract from. Reflect what the expert produced — do not introduce new concepts or volunteer your own opinions about the domain.

────── SOURCE MATERIAL ──────

{source_material}

────── END SOURCE ──────

Output the report now, starting with the `# Domain` heading."""


# ── Input formatters ────────────────────────────────────────────────


def format_source_material(state: DialecticalState, condition: str) -> str:
    """Turn the per-base file into the `{source_material}` slot of the
    template. The dispatch on `condition` is the only place this
    module knows about the experimental design — everywhere else the
    pipeline is condition-agnostic.
    """
    if condition == "elenchus":
        return _format_elenchus_input(state)
    if condition == "baseline":
        return _format_baseline_input(state)
    raise ValueError(f"Unknown condition: {condition!r}")


def _format_elenchus_input(state: DialecticalState) -> str:
    """Render the Elenchus material base as source material for the
    extractor. Includes the bilateral position, the accepted material
    implications, and the conversation transcript — all three are
    grist for the extractor's mill."""
    s = state.to_dict()
    parts: list[str] = ["DIALECTICAL POSITION:"]

    parts.append("")
    parts.append("Commitments (C):")
    if s["commitments"]:
        for i, prop in enumerate(s["commitments"], 1):
            parts.append(f"  {i}. {prop}")
    else:
        parts.append("  (none)")

    parts.append("")
    parts.append("Denials (D):")
    if s["denials"]:
        for i, prop in enumerate(s["denials"], 1):
            parts.append(f"  {i}. {prop}")
    else:
        parts.append("  (none)")

    parts.append("")
    parts.append("Accepted material implications (I):")
    if s["implications"]:
        for i, impl in enumerate(s["implications"], 1):
            gamma = " ∧ ".join(impl["gamma"]) or "(empty)"
            delta = " ∨ ".join(impl["delta"]) or "(empty)"
            parts.append(f"  {i}. {gamma} ⊢ {delta}")
    else:
        parts.append("  (none)")

    # Transcript provides motivating context the LLM uses to fill in
    # the # Notes section. Use a windowed version so a 60-min session
    # doesn't blow the report-generator's context budget.
    parts.append("")
    parts.append("CONVERSATION TRANSCRIPT (most recent first):")
    history = state.get_conversation()
    if not history:
        parts.append("  (empty)")
    else:
        # Take the last ~40 turns. 60-minute sessions average ~20-30
        # user turns plus the matching LLM responses; 40 messages
        # covers the bulk of a typical session.
        recent = history[-40:]
        for turn in recent:
            role = "EXPERT" if turn["role"] == "user" else "AI"
            parts.append(f"  [{role}] {turn['content']}")

    return "\n".join(parts)


def _format_baseline_input(state: DialecticalState) -> str:
    """Render the baseline transcript as source material. No
    bilateral state to surface here — the conversation IS the
    artifact. Same windowing as Elenchus."""
    parts: list[str] = ["CONVERSATION TRANSCRIPT:"]
    history = state.get_conversation()
    if not history:
        parts.append("  (empty)")
    else:
        recent = history[-40:]
        for turn in recent:
            role = "EXPERT" if turn["role"] == "user" else "AI"
            parts.append(f"  [{role}] {turn['content']}")
    return "\n".join(parts)


# ── Generation pipeline ─────────────────────────────────────────────


async def generate_report(
    state: DialecticalState,
    *,
    condition: str,
    opponent,
    session_id: int | None = None,
    actor_id: int | None = None,
    base_id: str | None = None,
    max_tokens: int = 2000,
) -> dict:
    """Build the prompt, run it through the LLMClient, return the
    structured result.

    `opponent` is the configured Opponent instance — we reuse its
    LLMClient so cost tracking, retry, alerting, and the
    LLMCallError surface all come for free. The `system` slot is left
    empty; the entire template lives in the user message so the LLM
    treats it as a one-shot extractive prompt rather than continuing
    a conversation.

    Returns a dict with `content`, `prompt_tokens`, `completion_tokens`,
    `model`, `attempts`, `latency_ms`. Raises `LLMCallError` on any
    non-success category — the caller (the route handler) translates
    it to the structured HTTP body.
    """
    source = format_source_material(state, condition)
    user_message = REPORT_PROMPT_TEMPLATE.format(source_material=source)

    # _async_chat returns the raw text and dispatches the on_result
    # callback for cost + alerting bookkeeping.
    from .opponent import _make_usage_recorder  # local — avoid import cycle

    recorder = _make_usage_recorder(actor_id=actor_id, base_id=base_id)

    # Bypass `_async_chat` so we can pull token counts out of the
    # ChatResult. The LLMClient handles classification + retry.
    result = await opponent._llm_client.achat(
        [{"role": "user", "content": user_message}],
        system=None,
        max_tokens=max_tokens,
    )
    if recorder is not None:
        try:
            recorder(result)
        except Exception:
            logger.exception("on_result callback raised in report generation")

    if not result.ok:
        from .opponent import LLMCallError  # local — avoid import cycle

        raise LLMCallError(result)

    return {
        "content": result.text,
        "model": result.model,
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
        "attempts": result.attempts,
        "latency_ms": result.latency_ms,
        "session_id": session_id,
        "condition": condition,
    }
