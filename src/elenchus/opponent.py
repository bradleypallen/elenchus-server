"""
opponent.py — The LLM opponent / derivability oracle

Sends the dialectical state to an LLM API (Anthropic or OpenAI-compatible),
parses structured responses, and applies state transitions per Figure 4.

Supports Anthropic directly and any OpenAI-compatible endpoint (e.g. OpenRouter).
"""

import asyncio
import logging
import os
from collections.abc import Callable

from anthropic import Anthropic, AsyncAnthropic
from openai import AsyncOpenAI, OpenAI

from .dialectical_state import DialecticalState
from .llm_client import ChatResult, LLMClient

logger = logging.getLogger(__name__)


def _make_usage_recorder(
    *,
    actor_id: int | None,
    base_id: str | None,
) -> Callable[[ChatResult], None] | None:
    """Return an `on_result` callback that writes one `usage` row per
    LLM call. Returns None if the platform DB isn't reachable (CLI,
    test in-memory bases) — in that case the call still happens, just
    without cost tracking.

    The recorder is built per-call so each call carries its own
    actor/base context. The platform DB lock is acquired briefly to
    serialize writes; the lookup happens lazily so importing
    opponent.py doesn't require an initialized registry."""

    def _record(result: ChatResult) -> None:
        try:
            # Local imports so the opponent module stays importable
            # without a live registry (CLI, tests with no platform DB).
            from . import pricing
            from .db import get_registry
            from .db import platform as pdb

            reg = get_registry()
            con = reg.platform_con()
            cost = pricing.compute_cost(
                result.model, result.prompt_tokens, result.completion_tokens
            )
            with reg.platform_lock:
                pdb.record_usage(
                    con,
                    actor_id=actor_id,
                    base_id=base_id,
                    model=result.model,
                    category=str(result.category),
                    prompt_tokens=result.prompt_tokens,
                    completion_tokens=result.completion_tokens,
                    cost_usd=cost,
                    attempts=result.attempts,
                    latency_ms=result.latency_ms,
                )
        except RuntimeError as e:
            # `get_registry()` raises RuntimeError if no registry is
            # initialized (CLI path, in-memory test). Silently skip.
            logger.debug("usage recording skipped (no registry): %s", e)
        except Exception:
            # Any other failure: log but don't propagate. Cost
            # tracking must never break the user-facing path.
            logger.exception("usage recording failed; continuing")

    return _record


class LLMCallError(RuntimeError):
    """Raised by `Opponent._chat` / `_async_chat` when the underlying
    `LLMClient` returns a non-success `ChatResult`. Carries the result
    so the route handler can surface the category to the user without
    rebuilding it from a bare exception message.

    Catching it: `except LLMCallError as e: e.result.category` gives
    you a `ChatCategory` you can map to an HTTP status / user message
    / alert severity."""

    def __init__(self, result: ChatResult):
        self.result = result
        super().__init__(
            f"LLM call failed: category={result.category.value} "
            f"attempts={result.attempts} latency_ms={result.latency_ms} "
            f"error={result.error_message!r}"
        )


# Known OpenAI-compatible base URLs (auto-detect API protocol)
_OPENAI_COMPAT_HOSTS = {"openrouter.ai", "api.openai.com", "api.together.xyz", "api.groq.com"}


# ── System prompts ───────────────────────────────────────────────────
#
# Two prompts are maintained side by side:
#
#   SLOAN_SYSTEM_PROMPT — the canonical Sloan-condition prompt. Speech
#   acts available to the LLM are exactly {COMMIT, DENY, ACCEPT_TENSION,
#   CONTEST_TENSION, RETRACT, REFINE} plus tension proposals — matching
#   the proposal's description of the Elenchus condition. This is the
#   default; do not weaken it without explicit reason.
#
#   PHASE_B_SYSTEM_PROMPT — adds ASSERT_IMPLICATION / INTRODUCE_BEARER /
#   RETRACT_IMPLICATION for theory articulation. Only sent to the LLM
#   when Opponent.enable_phase_b is True (gated by the
#   ELENCHUS_ENABLE_PHASE_B env var). NOT for the Sloan study.
#
# The two share most content; if you tune one, audit the other.
# `test_opponent.TestSystemPrompt` asserts the Sloan prompt excludes
# Phase B keywords so accidental cross-contamination is caught.


SLOAN_SYSTEM_PROMPT = """You are the opponent in an Elenchus dialectic (Allen 2026). You are conducting a prover-skeptic dialogue where the human respondent develops a bilateral position on a topic.

YOUR ROLE:
- Parse the respondent's natural language into formal speech acts
- Maintain the bilateral state [C : D] (commitments and denials)
- Detect and propose tensions (incoherences in the position)
- Be charitable: interpret claims in their strongest plausible form
- Be relentless but patient

SPEECH ACT RECOGNITION:
When the respondent speaks, classify their utterances:
- COMMIT: asserting/endorsing a proposition (becomes part of C in [C:D])
- DENY: rejecting a proposition (becomes part of D in [C:D])
- ACCEPT_TENSION: agreeing a tension is genuine (by number)
- CONTEST_TENSION: rejecting a tension (by number)
- RETRACT: withdrawing a previous commitment or denial
- REFINE: replacing a commitment with a more precise version

RESPONSE FORMAT — respond ONLY with this JSON. No markdown fences, no prose preamble, no trailing commentary. The FIRST character of your reply MUST be `{` and the LAST character MUST be `}`. Anything you want the respondent to read goes inside the "response" field — NEVER outside the JSON object.
{
  "speech_acts": [
    {"type": "COMMIT"|"DENY"|"ACCEPT_TENSION"|"CONTEST_TENSION"|"RETRACT"|"REFINE",
     "proposition": "the natural language proposition",
     "target_tension_id": null,
     "old_proposition": null}
  ],
  "new_tensions": [
    {"gamma": ["premise from C", "another premise from C"], "delta": ["conclusion", "optional further conclusion"], "reason": "why incoherent"}
  ],
  "response": "Your natural language response. Be conversational, Socratic, probing."
}

PROPOSITION QUALITY:
- Every proposition must be a clean, atomic, declarative sentence
- NEVER include metadata annotations like "(DENIED)", "(COMMITTED)", "(from C)" etc.
- NEVER include justifications, conjunctions, or multiple claims in one proposition
- BAD: "Since anyone can die, no one should start collecting" (contains justification)
- GOOD: "No one of any age should start a bonsai collection"
- For RETRACT/REFINE: old_proposition must EXACTLY match the wording in C or D

TENSION CONSTRUCTION — {gamma} |~ {delta}:
A tension means: "If you accept ALL of gamma, you are materially committed to ALL of delta — which conflicts with your position."

Both gamma and delta are SETS of propositions. A sequent may have multiple premises and multiple conclusions.

- gamma: Each element must be COPIED VERBATIM from the current commitments (C). Do not paraphrase, abridge, or annotate. Use the exact strings shown in the state.
- delta: One or more clean propositions that LOGICALLY FOLLOW from the gamma premises taken together. Each element of delta is a genuine material consequence — something the gamma premises commit the respondent to, which creates a problem for their overall position. Use multiple conclusions when the premises jointly entail several distinct problematic consequences.
- PREFER tensions where delta contains or entails a proposition the respondent has DENIED (in D). These are the sharpest tensions: they show that the respondent's commitments materially entail something they explicitly reject. If D is non-empty, actively look for such C-vs-D incoherences before proposing tensions with novel delta propositions.
- reason: A brief explanation of WHY gamma entails delta and why that is problematic.
- Do NOT put justifications or causal connectives in delta. The "reason" field is where you explain the inference.
- Do NOT propose tensions where delta does not actually follow from gamma. The inference must be defensible.

RULES:
- For ACCEPT_TENSION, include target_tension_id
- For CONTEST_TENSION, include target_tension_id
- For REFINE, include old_proposition (what's replaced) and proposition (the new version)
- Your "response" is what the respondent reads — make it a real philosophical conversation

UI-DRIVEN ACTIONS (CRITICAL — read carefully):
The respondent can accept tensions, contest tensions, and retract propositions via buttons in the UI. The state is updated BEFORE you receive the message. This means:
- An accepted tension will already appear in Material Implications, not Open Tensions.
- A contested tension will already appear in the Contested list.
- A retracted proposition will already appear in the Retracted list.

You MUST treat these as decisions the respondent JUST made right now. NEVER say "that has already been done", "that's already been retracted", "I don't see that tension", or any variation. The state reflects the action they are telling you about — that is expected and correct.

Do NOT emit ACCEPT_TENSION, CONTEST_TENSION, or RETRACT speech_acts for these — the state change is already applied.

Instead, respond as a philosophical interlocutor:
- For accepted tensions: discuss what this new material implication means for their position, what further consequences or pressures it creates
- For contested tensions: probe WHY they reject the inference, ask what they think is wrong with it, explore the philosophical stakes
- For retractions: discuss what retracting this proposition changes in their overall position, what commitments remain that depended on it, what new space opens up
- You may propose new_tensions if the updated position warrants them

TENSION QUEUE (CRITICAL — read carefully):
The respondent addresses tensions ONE AT A TIME to avoid cognitive overload. Only the focal tension is shown to the respondent in the UI — any additional tensions you propose are placed on a hidden queue and surface automatically as each is resolved.

- You will see ONLY the focal tension in "Open tensions (T)" — the count of queued tensions follows as a hint.
- In your "response" text, discuss ONLY the focal tension. Do NOT reference queued tensions by ID (the respondent cannot see them) and do NOT pile on additional incoherences in prose.
- You MAY still propose multiple new_tensions in a single turn — they will queue up — but prefer proposing one sharp, well-motivated tension per turn.
- Do NOT re-propose a tension that is already focal or queued. The "Next tension ID" reflects all tensions ever proposed, including queued.

IDENTIFIERS:
Use the identifiers shown in the state when referring to items in your response:
- Atoms: P1, P2, P3, ... (e.g., "P3 commits you to...")
- Tensions: T1, T2, ... (e.g., "tension T7 shows...")
- Implications: I1, I2, ... (e.g., "implication I3 establishes...")
Do NOT use "Tension #7" or "Proposition 3" — always use the short form: T7, P3, I3."""


# ── Phase B prompt (opt-in only) ─────────────────────────────────────
# Only sent to the LLM when Opponent.enable_phase_b is True. Adds three
# theory-articulation speech acts that bypass the tension loop. Not for
# the Sloan study — see the firewall rationale in the Opponent docstring.

PHASE_B_SYSTEM_PROMPT = """You are the opponent in an Elenchus dialectic (Allen 2026). You are conducting a prover-skeptic dialogue where the human respondent develops a bilateral position on a topic.

YOUR ROLE:
- Parse the respondent's natural language into formal speech acts
- Maintain the bilateral state [C : D] (commitments and denials)
- Detect and propose tensions (incoherences in the position)
- Be charitable: interpret claims in their strongest plausible form
- Be relentless but patient

SPEECH ACT RECOGNITION:
When the respondent speaks, classify their utterances:
- COMMIT: asserting/endorsing a proposition (becomes part of C in [C:D])
- DENY: rejecting a proposition (becomes part of D in [C:D])
- ACCEPT_TENSION: agreeing a tension is genuine (by number)
- CONTEST_TENSION: rejecting a tension (by number)
- RETRACT: withdrawing a previous commitment or denial
- REFINE: replacing a commitment with a more precise version
- ASSERT_IMPLICATION: respondent directly asserts a material rule {γ}|~{δ}
  (theory articulation — bypasses the tension loop)
- INTRODUCE_BEARER: respondent introduces a new atom into the vocabulary
  (L_B) without committing to or denying it
- RETRACT_IMPLICATION: respondent withdraws a previously-recorded
  implication by id

RESPONSE FORMAT — respond ONLY with this JSON. No markdown fences, no prose preamble, no trailing commentary. The FIRST character of your reply MUST be `{` and the LAST character MUST be `}`. Anything you want the respondent to read goes inside the "response" field — NEVER outside the JSON object.
{
  "speech_acts": [
    {"type": "COMMIT"|"DENY"|"ACCEPT_TENSION"|"CONTEST_TENSION"|"RETRACT"|"REFINE"|"ASSERT_IMPLICATION"|"INTRODUCE_BEARER"|"RETRACT_IMPLICATION",
     "proposition": "the natural language proposition (COMMIT/DENY/RETRACT/REFINE/INTRODUCE_BEARER)",
     "target_tension_id": null,
     "old_proposition": null,
     "gamma": null,
     "delta": null,
     "reason": null,
     "implication_id": null,
     "description": null}
  ],
  "new_tensions": [
    {"gamma": ["premise from C", "another premise from C"], "delta": ["conclusion", "optional further conclusion"], "reason": "why incoherent"}
  ],
  "response": "Your natural language response. Be conversational, Socratic, probing."
}

PROPOSITION QUALITY:
- Every proposition must be a clean, atomic, declarative sentence
- NEVER include metadata annotations like "(DENIED)", "(COMMITTED)", "(from C)" etc.
- NEVER include justifications, conjunctions, or multiple claims in one proposition
- BAD: "Since anyone can die, no one should start collecting" (contains justification)
- GOOD: "No one of any age should start a bonsai collection"
- For RETRACT/REFINE: old_proposition must EXACTLY match the wording in C or D

TENSION CONSTRUCTION — {gamma} |~ {delta}:
A tension means: "If you accept ALL of gamma, you are materially committed to ALL of delta — which conflicts with your position."

Both gamma and delta are SETS of propositions. A sequent may have multiple premises and multiple conclusions.

- gamma: Each element must be COPIED VERBATIM from the current commitments (C). Do not paraphrase, abridge, or annotate. Use the exact strings shown in the state.
- delta: One or more clean propositions that LOGICALLY FOLLOW from the gamma premises taken together. Each element of delta is a genuine material consequence — something the gamma premises commit the respondent to, which creates a problem for their overall position. Use multiple conclusions when the premises jointly entail several distinct problematic consequences.
- PREFER tensions where delta contains or entails a proposition the respondent has DENIED (in D). These are the sharpest tensions: they show that the respondent's commitments materially entail something they explicitly reject. If D is non-empty, actively look for such C-vs-D incoherences before proposing tensions with novel delta propositions.
- reason: A brief explanation of WHY gamma entails delta and why that is problematic.
- Do NOT put justifications or causal connectives in delta. The "reason" field is where you explain the inference.
- Do NOT propose tensions where delta does not actually follow from gamma. The inference must be defensible.

RULES:
- For ACCEPT_TENSION, include target_tension_id
- For CONTEST_TENSION, include target_tension_id
- For REFINE, include old_proposition (what's replaced) and proposition (the new version)
- For ASSERT_IMPLICATION, include gamma (list of premise atoms), delta (list of
  conclusion atoms), and reason. Use this when the respondent directly
  articulates a rule, e.g. "anything alive is an animal" → gamma=["X is alive"],
  delta=["X is an animal"]. Atoms may be NEW — they'll be added to L_B.
- For INTRODUCE_BEARER, include proposition (the new atom). Use when the
  respondent names a concept without endorsing or rejecting it: "let's call
  things that change over time 'mutable entities'" → INTRODUCE_BEARER
  "X is a mutable entity".
- For RETRACT_IMPLICATION, include implication_id (the integer id shown next to
  each rule in Material Implications). Use when the respondent says "drop rule
  3" or "I take back that anything alive is an animal".
- Your "response" is what the respondent reads — make it a real philosophical conversation

THEORY-ARTICULATION VS TENSION DETECTION:
Respondents bringing an *ontology* (definitions, taxonomy, rules) into the
dialectic typically want to *assert* the rules directly rather than have them
earned via tension. Recognize positum framings:
- Descriptive case ("a 58-year-old patient presents with...") → tension loop:
  COMMIT propositions, propose tensions to expose what their case-specific
  commitments materially entail.
- Ontology articulation ("an animal is anything that is alive...") → theory
  loop: use ASSERT_IMPLICATION for rules, INTRODUCE_BEARER for vocabulary,
  RETRACT_IMPLICATION when they walk something back. Only propose tensions
  when the asserted theory is genuinely incoherent.
When the framing is ambiguous, ask before assuming.

UI-DRIVEN ACTIONS (CRITICAL — read carefully):
The respondent can accept tensions, contest tensions, and retract propositions via buttons in the UI. The state is updated BEFORE you receive the message. This means:
- An accepted tension will already appear in Material Implications, not Open Tensions.
- A contested tension will already appear in the Contested list.
- A retracted proposition will already appear in the Retracted list.

You MUST treat these as decisions the respondent JUST made right now. NEVER say "that has already been done", "that's already been retracted", "I don't see that tension", or any variation. The state reflects the action they are telling you about — that is expected and correct.

Do NOT emit ACCEPT_TENSION, CONTEST_TENSION, or RETRACT speech_acts for these — the state change is already applied.

Instead, respond as a philosophical interlocutor:
- For accepted tensions: discuss what this new material implication means for their position, what further consequences or pressures it creates
- For contested tensions: probe WHY they reject the inference, ask what they think is wrong with it, explore the philosophical stakes
- For retractions: discuss what retracting this proposition changes in their overall position, what commitments remain that depended on it, what new space opens up
- You may propose new_tensions if the updated position warrants them

TENSION QUEUE (CRITICAL — read carefully):
The respondent addresses tensions ONE AT A TIME to avoid cognitive overload. Only the focal tension is shown to the respondent in the UI — any additional tensions you propose are placed on a hidden queue and surface automatically as each is resolved.

- You will see ONLY the focal tension in "Open tensions (T)" — the count of queued tensions follows as a hint.
- In your "response" text, discuss ONLY the focal tension. Do NOT reference queued tensions by ID (the respondent cannot see them) and do NOT pile on additional incoherences in prose.
- You MAY still propose multiple new_tensions in a single turn — they will queue up — but prefer proposing one sharp, well-motivated tension per turn.
- Do NOT re-propose a tension that is already focal or queued. The "Next tension ID" reflects all tensions ever proposed, including queued.

IDENTIFIERS:
Use the identifiers shown in the state when referring to items in your response:
- Atoms: P1, P2, P3, ... (e.g., "P3 commits you to...")
- Tensions: T1, T2, ... (e.g., "tension T7 shows...")
- Implications: I1, I2, ... (e.g., "implication I3 establishes...")
Do NOT use "Tension #7" or "Proposition 3" — always use the short form: T7, P3, I3."""


def _parse_tension_id(tid) -> int:
    """Parse a tension ID that may have a 'T' prefix (e.g. 'T1' -> 1, 1 -> 1)."""
    s = str(tid).strip().upper()
    if s.startswith("T"):
        s = s[1:]
    return int(s)


class Opponent:
    def __init__(
        self,
        model: str = "claude-opus-4-6",
        api_key: str | None = None,
        base_url: str | None = None,
        protocol: str | None = None,
        enable_phase_b: bool = False,
    ):
        """Configure the LLM opponent.

        `enable_phase_b` gates the Phase B speech acts
        (ASSERT_IMPLICATION, INTRODUCE_BEARER, RETRACT_IMPLICATION).
        When False (default), the system prompt makes no mention of
        them and `_apply` silently drops any the LLM tries to emit.
        This keeps the live message route compliant with the Sloan
        proposal's Elenchus condition, whose speech-act vocabulary is
        explicitly `{COMMIT, DENY, ACCEPT_TENSION, CONTEST_TENSION,
        RETRACT, REFINE}` plus opponent-side tension proposals — and
        only those. Operators running outside that study can opt in
        via the `ELENCHUS_ENABLE_PHASE_B` env var.

        The underlying `DialecticalState.assert_implication`,
        `introduce_bearer`, and `retract_implication` methods stay
        available for admin tooling, batch imports, and tests
        regardless of the flag.
        """
        self.model = model
        self.base_url = base_url
        self._api_key = api_key
        self.protocol = protocol or self._detect_protocol(base_url)
        self.client = self._build_client()
        self.async_client = self._build_async_client()
        self._has_api_key = bool(api_key or self._env_api_key())
        self.enable_phase_b = enable_phase_b
        self._llm_client = self._build_llm_client()
        logger.info(
            "Opponent initialized: protocol=%s, model=%s, base_url=%s, api_key_set=%s, phase_b=%s",
            self.protocol,
            model,
            base_url or "(default)",
            self._has_api_key,
            "ON" if enable_phase_b else "off (Sloan-default)",
        )

    def reconfigure(
        self,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        protocol: str | None = None,
        enable_phase_b: bool | None = None,
    ):
        """Recreate the client with new settings."""
        if model:
            self.model = model
        if api_key:
            self._api_key = api_key
            self._has_api_key = True
        if base_url is not None:
            self.base_url = base_url if base_url else None
        if protocol:
            self.protocol = protocol
        elif base_url is not None:
            self.protocol = self._detect_protocol(self.base_url)
        if enable_phase_b is not None:
            self.enable_phase_b = enable_phase_b
        self.client = self._build_client()
        self.async_client = self._build_async_client()
        self._llm_client = self._build_llm_client()
        logger.info(
            "Opponent reconfigured: protocol=%s, model=%s, base_url=%s, "
            "api_key_updated=%s, phase_b=%s",
            self.protocol,
            self.model,
            self.base_url or "(default)",
            bool(api_key),
            "ON" if self.enable_phase_b else "off (Sloan-default)",
        )

    def _system_prompt(self) -> str:
        """Return the system prompt for the current Phase B setting.

        Sloan-default returns SLOAN_SYSTEM_PROMPT (no mention of the
        theory-articulation acts). With the flag on it returns
        PHASE_B_SYSTEM_PROMPT, which is the same prompt with three
        extra speech acts described."""
        return PHASE_B_SYSTEM_PROMPT if self.enable_phase_b else SLOAN_SYSTEM_PROMPT

    @staticmethod
    def _detect_protocol(base_url: str | None) -> str:
        """Auto-detect protocol from base URL. Defaults to 'anthropic'."""
        if base_url:
            from urllib.parse import urlparse

            host = urlparse(base_url).hostname or ""
            if any(h in host for h in _OPENAI_COMPAT_HOSTS):
                return "openai"
        return "anthropic"

    def _env_api_key(self) -> str | None:
        """Return the relevant env-var API key for the current protocol."""
        if self.protocol == "openai":
            return os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENROUTER_API_KEY")
        return os.environ.get("ANTHROPIC_API_KEY")

    def _build_client(self):
        """Build the appropriate sync SDK client. Used by the CLI and by
        any sync code path that wants a blocking LLM call."""
        if self.protocol == "openai":
            kwargs = {}
            if self._api_key:
                kwargs["api_key"] = self._api_key
            if self.base_url:
                kwargs["base_url"] = self.base_url
            return OpenAI(**kwargs)
        else:
            kwargs = {}
            if self._api_key:
                kwargs["api_key"] = self._api_key
            if self.base_url:
                kwargs["base_url"] = self.base_url
            return Anthropic(**kwargs)

    def _build_async_client(self):
        """Build the appropriate async SDK client. Used by the FastAPI
        routes so the event loop can handle other requests during the
        5–30 s LLM call. Mirrors `_build_client` exactly except for the
        SDK class."""
        if self.protocol == "openai":
            kwargs = {}
            if self._api_key:
                kwargs["api_key"] = self._api_key
            if self.base_url:
                kwargs["base_url"] = self.base_url
            return AsyncOpenAI(**kwargs)
        else:
            kwargs = {}
            if self._api_key:
                kwargs["api_key"] = self._api_key
            if self.base_url:
                kwargs["base_url"] = self.base_url
            return AsyncAnthropic(**kwargs)

    def _build_llm_client(self):
        """Build the LLMClient wrapper that owns classification +
        retry. Re-created on every reconfigure since the model name
        and underlying SDK clients can change."""
        return LLMClient(
            protocol=self.protocol,
            model=self.model,
            sync_client=self.client,
            async_client=self.async_client,
        )

    def _chat(
        self,
        messages: list[dict],
        system: str | None = None,
        max_tokens: int = 2000,
        on_result: "Callable[[ChatResult], None] | None" = None,
    ) -> str:
        """Sync chat call. Delegates to the LLMClient for
        classification + retry; on failure surfaces an exception so
        the caller's existing error path runs (the message route
        translates it to a 500 with the category in the body).

        `on_result` is an optional callback invoked with the structured
        `ChatResult` regardless of success or failure. Used by the
        cost-tracking layer so a failed call is still recorded
        (latency, attempts, category) even though no tokens were
        spent. Errors raised inside the callback are caught and
        logged — usage recording should never break the user-facing
        path."""
        result = self._llm_client.chat(messages, system=system, max_tokens=max_tokens)
        if on_result is not None:
            try:
                on_result(result)
            except Exception:
                logger.exception("on_result callback raised; ignoring")
        if not result.ok:
            raise LLMCallError(result)
        return result.text

    async def _async_chat(
        self,
        messages: list[dict],
        system: str | None = None,
        max_tokens: int = 2000,
        on_result: "Callable[[ChatResult], None] | None" = None,
    ) -> str:
        """Async chat call. See `_chat` for the result-vs-exception
        contract and the `on_result` semantics. Async callers in
        particular benefit because the LLMClient uses `asyncio.sleep`
        for retry backoff, so the event loop stays responsive while
        we wait out a rate-limit."""
        result = await self._llm_client.achat(messages, system=system, max_tokens=max_tokens)
        if on_result is not None:
            try:
                on_result(result)
            except Exception:
                logger.exception("on_result callback raised; ignoring")
        if not result.ok:
            raise LLMCallError(result)
        return result.text

    def _build_request_messages(
        self,
        user_message: str,
        state: DialecticalState,
        context_turns: int,
        action_context: dict | None,
    ) -> list[dict]:
        """Build the `messages` list sent to the LLM.

        Pure function over the state (read-only on DuckDB) plus the
        user's prose and optional action context. Shared by the sync
        `respond` and async `async_respond` paths so the actual LLM call
        is the only thing that differs between them.
        """
        s = state.to_dict()
        row = state.base.con.execute("SELECT COALESCE(MAX(id), 0) FROM tensions").fetchone()
        tid = row[0]

        atom_ids = s.get("atom_ids", {})
        queued = s.get("queued_tensions", [])
        queued_hint = (
            f" [+ {len(queued)} queued, hidden from respondent until current resolves]"
            if queued
            else ""
        )
        formal_state = f"""CURRENT DIALECTICAL STATE:
Topic: {s["name"]}
Next tension ID: {tid + 1}

Commitments (C):{self._fmt_list(s["commitments"], atom_ids)}
Denials (D):{self._fmt_list(s["denials"], atom_ids)}
Open tensions (T) — focal only:{self._fmt_tensions(s["tensions"])}{queued_hint}
Contested tensions:{self._fmt_tensions(s["contested"])}
Material implications (I):{self._fmt_implications(s["implications"])}
Retracted:{self._fmt_list(s["retracted"], atom_ids)}"""

        # Detect UI-driven actions and inject a reminder so the model
        # doesn't say "that's already been done" (the state was updated
        # before this message was sent — that's by design).
        ui_action_note = ""
        msg_lower = user_message.lower()
        if (
            msg_lower.startswith("i accept tension")
            or msg_lower.startswith("i contest tension")
            or msg_lower.startswith("i retract")
        ):
            detail = ""
            if action_context:
                ctx_id = action_context.get("tension_id")
                gamma = action_context.get("gamma", [])
                delta = action_context.get("delta", [])
                action = action_context.get("action", "")
                g = ", ".join(f'"{x}"' for x in gamma)
                d = ", ".join(f'"{x}"' for x in delta)
                detail = f" The tension was T{ctx_id}: {{{g}}} |~ {{{d}}}."
                if action == "accept":
                    detail += " It is now a material implication."
            id_reminder = (
                f" Use the correct tension ID (T{action_context['tension_id']}) when referring to it."
                if action_context and "tension_id" in action_context
                else ""
            )
            ui_action_note = f"""
[NOTE: This action was applied via the UI — the state above already reflects it.{detail} This is the respondent's JUST-MADE decision. Do NOT say it was "already done" or "already processed." Respond as if they just told you their decision in conversation. Discuss the philosophical implications.{id_reminder}]
"""

        user_content = f"""{formal_state}

RESPONDENT SAYS: "{user_message}" {ui_action_note}"""

        # Windowed conversation history. The formal state above makes the
        # full history unnecessary — we only need recent turns for
        # conversational continuity.
        history = state.get_conversation()

        messages: list[dict] = []
        if len(history) > context_turns * 2:
            summary = state.get_summary()
            if summary:
                messages.append(
                    {"role": "user", "content": f"[SUMMARY OF EARLIER DISCUSSION]\n{summary}"}
                )
                messages.append(
                    {"role": "assistant", "content": "Understood. I have the dialectical context."}
                )
            # Take only the last N exchanges
            history = history[-(context_turns * 2) :]

        messages.extend(history)
        messages.append({"role": "user", "content": user_content})
        return messages

    def respond(
        self,
        user_message: str,
        state: DialecticalState,
        context_turns: int = 6,
        action_context: dict | None = None,
        actor_id: int | None = None,
        base_id: str | None = None,
    ) -> dict:
        """Sync entry point. Used by the CLI and any blocking caller.

        Sends the respondent's message + dialectical state to the LLM,
        parses the structured response, applies state transitions,
        returns the result. The async sibling `async_respond` has the
        same contract; only the LLM call differs.

        `actor_id` + `base_id` are passed through to the cost-tracking
        recorder so this call is attributed correctly. Both default to
        None for CLI use (no platform DB).
        """
        messages = self._build_request_messages(user_message, state, context_turns, action_context)
        recorder = _make_usage_recorder(actor_id=actor_id, base_id=base_id)
        raw_text = self._chat(
            messages,
            system=self._system_prompt(),
            max_tokens=2000,
            on_result=recorder,
        )
        return self._record_and_apply(user_message, raw_text, state)

    async def async_respond(
        self,
        user_message: str,
        state: DialecticalState,
        context_turns: int = 6,
        action_context: dict | None = None,
        lock: asyncio.Lock | None = None,
        actor_id: int | None = None,
        base_id: str | None = None,
    ) -> dict:
        """Async entry point. Used by FastAPI route handlers so the event
        loop can service other requests during the 5–30 s LLM call.

        Concurrency model:
        - Reading state to build the request runs without a lock —
          DuckDB MVCC gives a consistent snapshot for reads.
        - The LLM call is awaited with no lock held — concurrent tabs
          on the same base can have overlapping LLM calls.
        - State mutations (conversation insert + `_apply`) run under
          the per-base lock when one is passed, wrapped in an explicit
          transaction. The lock serializes apply blocks across
          concurrent callers on the same base; the transaction ensures
          atomicity within an apply.

        Passing `lock=None` (the default) skips lock acquisition —
        used by tests and any caller that has already arranged
        serialization. Route handlers pass `handle.lock` from the
        DBRegistry.
        """
        messages = self._build_request_messages(user_message, state, context_turns, action_context)
        recorder = _make_usage_recorder(actor_id=actor_id, base_id=base_id)
        raw_text = await self._async_chat(
            messages,
            system=self._system_prompt(),
            max_tokens=2000,
            on_result=recorder,
        )
        if lock is None:
            return self._record_and_apply(user_message, raw_text, state)
        async with lock:
            return self._record_and_apply(user_message, raw_text, state)

    def _record_and_apply(self, user_message: str, raw_text: str, state: DialecticalState) -> dict:
        """Common post-LLM bookkeeping: store conversation, parse, apply
        state transitions, periodically update the rolling summary.

        The apply phase runs inside an explicit DuckDB transaction so a
        crash or exception leaves the base either fully pre-message or
        fully post-message — never half-applied. The summary update
        runs outside the transaction (best-effort; shouldn't roll back
        the user's turn).
        """
        con = state.base.con
        con.execute("BEGIN")
        try:
            state.add_conversation("user", user_message)
            state.add_conversation("assistant", raw_text)
            parsed = self._parse_response(raw_text)
            self._apply(parsed, state)
            con.execute("COMMIT")
        except Exception:
            try:
                con.execute("ROLLBACK")
            except Exception:
                logger.exception("Rollback failed in _record_and_apply")
            raise

        total_turns = len(state.get_conversation())
        if total_turns > 0 and total_turns % 20 == 0:
            self._update_summary(state)

        return parsed

    def generate_summary(self, state: DialecticalState) -> str:
        """Generate a substantive analytical summary of the dialectic.

        Returns the summary text without storing it. Used for PDF reports.
        """
        s = state.to_dict()

        # Build a rich prompt with full formal state
        atom_ids = s.get("atom_ids", {})
        commitments_block = (
            "\n".join(
                f'  P{atom_ids[c]} - "{c}"' if c in atom_ids else f'  - "{c}"'
                for c in s["commitments"]
            )
            or "  (none)"
        )
        denials_block = (
            "\n".join(
                f'  P{atom_ids[d]} - "{d}"' if d in atom_ids else f'  - "{d}"'
                for d in s["denials"]
            )
            or "  (none)"
        )
        retracted_block = (
            "\n".join(
                f'  P{atom_ids[r]} - "{r}"' if r in atom_ids else f'  - "{r}"'
                for r in s["retracted"]
            )
            or "  (none)"
        )

        tensions_block = ""
        # Summary covers all open tensions, focal and queued
        for t in s["tensions"] + s.get("queued_tensions", []):
            g = ", ".join(f'"{x}"' for x in t["gamma"])
            d = ", ".join(f'"{x}"' for x in t["delta"])
            tensions_block += f"\n  T{t['id']}: {{{g}}} |~ {{{d}}}: {t['reason']}"
        if not tensions_block:
            tensions_block = "  (none)"

        implications_block = ""
        for imp in s["implications"]:
            g = ", ".join(f'"{x}"' for x in imp["gamma"])
            d = ", ".join(f'"{x}"' for x in imp["delta"])
            imp_id = imp.get("id", "")
            implications_block += f"\n  I{imp_id}: {{{g}}} |~ {{{d}}}"
        if not implications_block:
            implications_block = "  (none)"

        contested_block = ""
        for t in s.get("contested", []):
            g = ", ".join(f'"{x}"' for x in t["gamma"])
            d = ", ".join(f'"{x}"' for x in t["delta"])
            contested_block += f"\n  T{t['id']}: {{{g}}} |~ {{{d}}}: {t['reason']}"
        if not contested_block:
            contested_block = "  (none)"

        prompt = f"""Write a brief summary of the current state of this Elenchus dialectic. Describe:

- The topic and the respondent's final bilateral position (what is committed, what is denied)
- The key material implications that have been established
- Any open tensions that remain unresolved

DIALECTICAL STATE:
Topic: {s["name"]}

Commitments (C):
{commitments_block}

Denials (D):
{denials_block}

Open tensions (T):
{tensions_block}

Material implications (I):
{implications_block}

Retracted propositions:
{retracted_block}

Write 1-3 short paragraphs. Be concise and precise. Describe the position as it stands now — do not narrate the history of how it got here. Do NOT include a title or heading — start directly with the substantive content. Use the identifiers shown (P1, T3, I2, etc.) when referring to specific atoms, tensions, or implications."""

        try:
            summary = self._chat([{"role": "user", "content": prompt}], max_tokens=800)
            logger.info(
                "Generated analytical summary for dialectic '%s' (%d chars)",
                s["name"],
                len(summary),
            )
            return summary
        except Exception as e:
            logger.error("Failed to generate summary for '%s': %s", s["name"], e)
            return f"Summary generation failed: {e}"

    def _update_summary(self, state: DialecticalState):
        """Ask the LLM to summarize the dialectic so far."""
        s = state.to_dict()
        history = state.get_conversation()
        # Take a sample of the history for summarization
        sample = history[:20] if len(history) > 20 else history

        prompt = f"""Summarize this Elenchus dialectic concisely (3-5 sentences).
Focus on: the main commitments, key tensions that were resolved,
any retractions or refinements, and the current trajectory.

Topic: {s["name"]}
Current commitments: {len(s["commitments"])}
Material implications: {len(s["implications"])}

Recent exchanges:
""" + "\n".join(f"{m['role']}: {m['content'][:200]}" for m in sample[-10:])

        try:
            summary = self._chat([{"role": "user", "content": prompt}], max_tokens=500)
            state.set_summary(summary)
        except Exception:
            logger.debug("Summary update failed (non-critical)")

    def _parse_response(self, text: str) -> dict:
        """Parse the LLM's response into the opponent's expected payload
        shape. Tolerates code fences, prose preamble, and trailing
        chatter via `response_parsing.parse_llm_response`. Falls back
        to wrapping the raw text as a plain conversational response so
        the dialogue never breaks on a malformed turn."""
        from .response_parsing import parse_llm_response

        parsed = parse_llm_response(text)
        if parsed is not None:
            return parsed

        # Final fallback: treat entire text as conversational response.
        # Log so we can spot prompt-adherence regressions during runs.
        logger.warning(
            "LLM response did not contain parseable JSON; treating as plain "
            "text (len=%d, first 80 chars=%r)",
            len(text),
            text[:80],
        )
        return {"speech_acts": [], "new_tensions": [], "response": text}

    def _apply(self, parsed: dict, state: DialecticalState):
        """Apply speech acts and tensions to state."""
        for act in parsed.get("speech_acts", []):
            atype = act.get("type", "")
            prop = act.get("proposition", "")

            if atype == "COMMIT" and prop:
                state.commit(prop)
            elif atype == "DENY" and prop:
                state.deny(prop)
            elif atype == "RETRACT" and prop:
                state.retract_prop(prop)
            elif atype == "REFINE":
                old = act.get("old_proposition", "")
                if old:
                    state.retract_prop(old)
                if prop:
                    state.commit(prop)
            elif atype == "ACCEPT_TENSION":
                tid = act.get("target_tension_id")
                if tid is not None:
                    result = state.accept_tension(_parse_tension_id(tid))
                    if not result:
                        logger.info(
                            "Skipped ACCEPT_TENSION #%s (already resolved or not found)", tid
                        )
            elif atype == "CONTEST_TENSION":
                tid = act.get("target_tension_id")
                if tid is not None:
                    result = state.contest_tension(_parse_tension_id(tid))
                    if not result:
                        logger.info(
                            "Skipped CONTEST_TENSION #%s (already resolved or not found)", tid
                        )

            # ── Phase B speech acts ────────────────────────────────
            # Firewalled by Opponent.enable_phase_b. When disabled
            # (default), the LLM hasn't been told these acts exist —
            # but if it emits one anyway (stale conversation context,
            # prompt drift, adversarial respondent), we silently drop
            # it and log so an audit can spot the attempt. The
            # underlying DialecticalState methods stay reachable for
            # admin tooling regardless.
            elif atype in ("ASSERT_IMPLICATION", "INTRODUCE_BEARER", "RETRACT_IMPLICATION"):
                if not self.enable_phase_b:
                    logger.info(
                        "Firewall: dropped Phase B speech act %r (ELENCHUS_ENABLE_PHASE_B is off)",
                        atype,
                    )
                    continue
                if atype == "ASSERT_IMPLICATION":
                    gamma = act.get("gamma", [])
                    delta = act.get("delta", [])
                    reason = act.get("reason", "")
                    if gamma or delta:
                        iid = state.assert_implication(gamma, delta, reason=reason)
                        logger.info(
                            "Applied ASSERT_IMPLICATION → assessments.id=%d (γ=%d, δ=%d)",
                            iid,
                            len(gamma),
                            len(delta),
                        )
                    else:
                        logger.warning("Skipped ASSERT_IMPLICATION with empty γ and δ")
                elif atype == "INTRODUCE_BEARER":
                    if prop:
                        description = act.get("description", "")
                        state.introduce_bearer(prop, description=description)
                        logger.info("Applied INTRODUCE_BEARER %r", prop)
                    else:
                        logger.warning("Skipped INTRODUCE_BEARER with no proposition")
                else:  # RETRACT_IMPLICATION
                    iid_raw = act.get("implication_id")
                    try:
                        iid = int(iid_raw) if iid_raw is not None else None
                    except (TypeError, ValueError):
                        iid = None
                    if iid is not None:
                        ok = state.retract_implication(iid)
                        if not ok:
                            logger.info(
                                "Skipped RETRACT_IMPLICATION #%s (already retracted or not found)",
                                iid,
                            )
                    else:
                        logger.warning(
                            "Skipped RETRACT_IMPLICATION: missing or non-integer "
                            "implication_id (%r)",
                            iid_raw,
                        )

        for t in parsed.get("new_tensions", []):
            gamma = t.get("gamma", [])
            delta = t.get("delta", [])
            reason = t.get("reason", "")
            if gamma or delta:
                # Ensure atoms exist
                for a in gamma + delta:
                    state.base.add_atoms({a}, contributor="oracle")
                state.add_tension(gamma, delta, reason)

    def _fmt_list(self, items, atom_ids=None):
        if not items:
            return " (none)"
        if atom_ids:
            return "".join(
                f'\n  P{atom_ids[item]} - "{item}"' if item in atom_ids else f'\n  - "{item}"'
                for item in items
            )
        return "".join(f'\n  - "{item}"' for item in items)

    def _fmt_tensions(self, tensions):
        if not tensions:
            return " (none)"
        lines = []
        for t in tensions:
            g = ", ".join(f'"{x}"' for x in t["gamma"])
            d = ", ".join(f'"{x}"' for x in t["delta"])
            lines.append(f"\n  T{t['id']}: {{{g}}} |~ {{{d}}}: {t['reason']}")
        return "".join(lines)

    def _fmt_implications(self, imps):
        if not imps:
            return " (none)"
        lines = []
        for imp in imps:
            g = ", ".join(f'"{x}"' for x in imp["gamma"])
            d = ", ".join(f'"{x}"' for x in imp["delta"])
            imp_id = imp.get("id", "")
            lines.append(f"\n  I{imp_id}: {{{g}}} |~ {{{d}}}")
        return "".join(lines)
