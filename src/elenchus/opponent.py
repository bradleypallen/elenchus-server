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

from . import prompts, turn_log
from .dialectical_state import DialecticalState
from .llm_client import DEFAULT_TEMPERATURE, ChatResult, LLMClient
from .turn_log import EventContext

logger = logging.getLogger(__name__)


def _make_usage_recorder(
    *,
    actor_id: int | None,
    base_id: str | None,
    purpose: str = "",
) -> Callable[[ChatResult], None] | None:
    """Return an `on_result` callback that writes one `usage` row per
    LLM call. Returns None if the platform DB isn't reachable (CLI,
    test in-memory bases) — in that case the call still happens, just
    without cost tracking.

    `purpose` says what the call is for (`costs.PURPOSES`) so the cost
    dashboard can tell a participant's turns from platform overhead.
    Every LLM call the server makes should go through a recorder — a
    call without one is spend nobody can see.

    The recorder is built per-call so each call carries its own
    actor/base context. The platform DB lock is acquired briefly to
    serialize writes; the lookup happens lazily so importing
    opponent.py doesn't require an initialized registry."""

    def _record(result: ChatResult) -> None:
        # 1. Cost tracking. Skipped silently if the registry isn't
        # initialized (CLI path, in-memory test).
        try:
            # Local imports so the opponent module stays importable
            # without a live registry.
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
                    purpose=purpose,
                    response_model=result.response_model,
                    request_id=result.request_id,
                )
                # A call that cost something may have pushed the day
                # past the spend-alert threshold (cost_alerts.py). Its
                # failure must never cost the respondent their turn.
                if result.prompt_tokens or result.completion_tokens:
                    try:
                        from . import cost_alerts

                        cost_alerts.check(con)
                    except Exception:
                        logger.exception("daily spend check failed; continuing")
        except RuntimeError as e:
            logger.debug("usage recording skipped (no registry): %s", e)
        except Exception:
            logger.exception("usage recording failed; continuing")

        # 2. Alerting. Independent of cost tracking — even if usage
        # recording fails, operators still see the alert. Dispatch
        # only on non-success; the dispatcher itself handles dedup.
        if not result.ok:
            try:
                from . import alerting

                alerting.dispatch_for_chat_failure(result, actor_id=actor_id, base_id=base_id)
            except Exception:
                logger.exception("alert dispatch failed; continuing")

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


SLOAN_SYSTEM_PROMPT = prompts.packaged(
    "elenchus"
).text  # the shipped text; see src/elenchus/prompts/


# ── Phase B prompt (opt-in only) ─────────────────────────────────────
# Only sent to the LLM when Opponent.enable_phase_b is True. Adds three
# theory-articulation speech acts that bypass the tension loop. Not for
# the Sloan study — see the firewall rationale in the Opponent docstring.

PHASE_B_SYSTEM_PROMPT = prompts.packaged(
    "phase_b"
).text  # the shipped text; see src/elenchus/prompts/


# ── Baseline condition prompt (Sloan AI-as-tool) ─────────────────────
# Used by participants whose session.condition == 'baseline'. No
# Socratic / opponent framing — this is meant to represent a
# naturalistic free-form chat with a helpful LLM, the AI-as-tool half
# of the Sloan study's within-subjects comparison. Length and tone
# are tuned to be comparable to the SLOAN_SYSTEM_PROMPT so the
# contrast between conditions is interaction structure, not prompt
# quality.

BASELINE_SYSTEM_PROMPT = prompts.packaged(
    "baseline"
).text  # the shipped text; see src/elenchus/prompts/


def baseline_system_prompt(topic: str = "") -> str:
    """The baseline condition's system prompt for a session on `topic`.

    The Elenchus opponent learns the topic from the state it is shown
    ("Topic: ..."); the baseline assistant is shown no state, so the
    topic rides on the system prompt instead. Generic base names (no
    topic issued) add nothing."""
    topic = (topic or "").strip()
    base = prompts.load("baseline").text
    if not topic or topic == "Study task":
        return base
    return f"{base}\n\nTHE EXPERT'S TOPIC: {topic}"


def _parse_tension_id(tid) -> int:
    """Parse a tension ID that may have a 'T' prefix (e.g. 'T1' -> 1, 1 -> 1)."""
    s = str(tid).strip().upper()
    if s.startswith("T"):
        s = s[1:]
    return int(s)


# The model used when none is configured. An *empty* model name — an
# `ELENCHUS_MODEL=` line in an env file, a blank field — means this too,
# never a request with `model=""` (which the API rejects).
DEFAULT_MODEL = "claude-opus-4-6"

TEMPERATURE_ENV = "ELENCHUS_TEMPERATURE"


def _env_temperature() -> float:
    """`ELENCHUS_TEMPERATURE`, or `llm_client.DEFAULT_TEMPERATURE` when
    unset, empty or not a number in [0, 2]. Always a number: the value
    is sent on every call and recorded with it."""
    raw = (os.environ.get(TEMPERATURE_ENV) or "").strip()
    if not raw:
        return DEFAULT_TEMPERATURE
    try:
        value = float(raw)
        if not 0.0 <= value <= 2.0:
            raise ValueError(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not a number between 0 and 2; using %s",
            TEMPERATURE_ENV,
            raw,
            DEFAULT_TEMPERATURE,
        )
        return DEFAULT_TEMPERATURE
    return value


class Opponent:
    def __init__(
        self,
        model: str | None = DEFAULT_MODEL,
        api_key: str | None = None,
        base_url: str | None = None,
        protocol: str | None = None,
        enable_phase_b: bool = False,
        temperature: float | None = None,
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
        self.model = model or DEFAULT_MODEL
        self.base_url = base_url
        self._api_key = api_key
        self.protocol = protocol or self._detect_protocol(base_url)
        self.client = self._build_client()
        self.async_client = self._build_async_client()
        self._has_api_key = bool(api_key or self._env_api_key())
        self.enable_phase_b = enable_phase_b
        # Sent on every call and recorded with it (the study freezes it).
        self.temperature = temperature if temperature is not None else _env_temperature()
        self._llm_client = self._build_llm_client()
        logger.info(
            "Opponent initialized: protocol=%s, model=%s, base_url=%s, api_key_set=%s, "
            "phase_b=%s, temperature=%s",
            self.protocol,
            model,
            base_url or "(default)",
            self._has_api_key,
            "ON" if enable_phase_b else "off (Sloan-default)",
            self.temperature,
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
        return prompts.load("phase_b" if self.enable_phase_b else "elenchus").text

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
            temperature=self.temperature,
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

    @staticmethod
    def _draft_section(draft: str | None, previous_draft: str | None, positum: bool) -> str:
        """The respondent's text as the model is shown it: the current
        draft, and the draft as of the model's last turn — or that it is
        unchanged — so the differences can be read as speech acts. Empty
        when there is no draft (the ordinary interface) and on the positum
        turn, whose message is the draft."""
        if draft is None or positum:
            return ""
        current = (
            f'\n\nCURRENT DRAFT (the respondent\'s introduction as it stands):\n"""\n{draft}\n"""'
        )
        if previous_draft is None:
            return current
        if previous_draft == draft:
            return current + "\n(unchanged since your last turn)"
        return current + f'\n\nPREVIOUS DRAFT (as of your last turn):\n"""\n{previous_draft}\n"""'

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
        return self._build_request(user_message, state, context_turns, action_context)[0]

    def _build_request(
        self,
        user_message: str,
        state: DialecticalState,
        context_turns: int,
        action_context: dict | None,
        *,
        draft: str | None = None,
        previous_draft: str | None = None,
        positum: bool = False,
    ) -> tuple[list[dict], dict]:
        """`_build_request_messages`, plus what the turn log records
        about the request: the state as the LLM saw it, the exact final
        user message, and how much history rode along.

        `draft` is the respondent's written text as it stands (the study
        flow; design-notes/text-as-positum.md) and `previous_draft` the
        text as shown at the last turn, so the opponent can parse the
        changes as speech acts; `positum` marks the opening turn, whose
        message *is* the first draft. Without a draft the request is
        exactly what it was before 0.14.0."""
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

        if positum:
            says = (
                "RESPONDENT'S FIRST DRAFT (the positum — their introduction as first "
                f'written; extract the initial position from it):\n"""\n{user_message}\n"""'
            )
        else:
            says = f'RESPONDENT SAYS: "{user_message}" {ui_action_note}'
        user_content = f"""{formal_state}{self._draft_section(draft, previous_draft, positum)}

{says}"""

        # Windowed conversation history. The formal state above makes the
        # full history unnecessary — we only need recent turns for
        # conversational continuity.
        history = state.get_conversation()

        messages: list[dict] = []
        summary_included = False
        if len(history) > context_turns * 2:
            summary = state.get_summary()
            if summary:
                summary_included = True
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
        capture = {
            "action_context": action_context,
            "request_content": user_content,
            "history_window": len(history),
            "summary_included": summary_included,
            "state_before": s,
        }
        return messages, capture

    def respond(
        self,
        user_message: str,
        state: DialecticalState,
        context_turns: int = 6,
        action_context: dict | None = None,
        actor_id: int | None = None,
        base_id: str | None = None,
        draft: str | None = None,
        previous_draft: str | None = None,
        positum: bool = False,
        draft_snapshot_id: int | None = None,
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
        messages, turn = self._build_request(
            user_message,
            state,
            context_turns,
            action_context,
            draft=draft,
            previous_draft=previous_draft,
            positum=positum,
        )
        turn["actor_id"] = actor_id
        turn["draft_snapshot_id"] = draft_snapshot_id
        try:
            raw_text = self._chat(
                messages,
                system=self._system_prompt(),
                max_tokens=2000,
                on_result=self._capturing(
                    turn, actor_id=actor_id, base_id=base_id, purpose="dialectic_turn"
                ),
            )
        except LLMCallError:
            self._record_failed_turn("elenchus", user_message, state, turn)
            raise
        return self._record_and_apply(user_message, raw_text, state, turn=turn)

    async def async_respond(
        self,
        user_message: str,
        state: DialecticalState,
        context_turns: int = 6,
        action_context: dict | None = None,
        lock: asyncio.Lock | None = None,
        actor_id: int | None = None,
        base_id: str | None = None,
        draft: str | None = None,
        previous_draft: str | None = None,
        positum: bool = False,
        draft_snapshot_id: int | None = None,
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
        messages, turn = self._build_request(
            user_message,
            state,
            context_turns,
            action_context,
            draft=draft,
            previous_draft=previous_draft,
            positum=positum,
        )
        turn["actor_id"] = actor_id
        turn["draft_snapshot_id"] = draft_snapshot_id
        try:
            raw_text = await self._async_chat(
                messages,
                system=self._system_prompt(),
                max_tokens=2000,
                on_result=self._capturing(
                    turn, actor_id=actor_id, base_id=base_id, purpose="dialectic_turn"
                ),
            )
        except LLMCallError:
            if lock is None:
                self._record_failed_turn("elenchus", user_message, state, turn)
            else:
                async with lock:
                    self._record_failed_turn("elenchus", user_message, state, turn)
            raise
        if lock is None:
            return self._record_and_apply(user_message, raw_text, state, turn=turn)
        async with lock:
            return self._record_and_apply(user_message, raw_text, state, turn=turn)

    async def async_baseline_respond(
        self,
        user_message: str,
        state: DialecticalState,
        context_turns: int = 8,
        lock: asyncio.Lock | None = None,
        actor_id: int | None = None,
        base_id: str | None = None,
        draft: str | None = None,
        previous_draft: str | None = None,
        positum: bool = False,
        draft_snapshot_id: int | None = None,
    ) -> dict:
        """Baseline (AI-as-tool) chat path for Sloan-condition participants.

        Differences from `async_respond`:
          * Uses BASELINE_SYSTEM_PROMPT (no opponent / Socratic framing).
          * Plain-text response — no JSON schema, no speech-act parsing,
            no `[C : D] / T / I` state mutations.
          * Stores both turns in `conversation` (same table as the
            dialectic path), so the per-base file IS the transcript and
            Phase D/5's report generator can read either condition the
            same way.

        Cost tracking, alerting, retry, and LLMCallError surfacing all
        come for free from the shared `_async_chat` chokepoint.
        """
        history = state.get_conversation()
        if context_turns > 0:
            history = history[-(context_turns * 2) :]
        messages = list(history)
        # The study flow shows the assistant the expert's draft (design-
        # notes/text-as-positum.md): the first message *is* the first
        # draft; later ones carry the text as it stands.
        if positum:
            request_content = (
                f'Here is my first draft of the introduction:\n"""\n{user_message}\n"""'
            )
        elif draft is not None:
            changed = (
                ""
                if previous_draft is None
                else " (unchanged since your last reply)"
                if previous_draft == draft
                else " (changed since your last reply)"
            )
            request_content = (
                f'MY DRAFT AS IT STANDS{changed}:\n"""\n{draft}\n"""\n\n{user_message}'
            )
        else:
            request_content = user_message
        messages.append({"role": "user", "content": request_content})

        system = baseline_system_prompt(state.base.name)
        turn = {
            "actor_id": actor_id,
            "request_content": request_content,
            "history_window": len(history),
            "summary_included": False,
            "system_prompt": system,
            "draft_snapshot_id": draft_snapshot_id,
        }
        try:
            raw_text = await self._async_chat(
                messages,
                system=system,
                max_tokens=2000,
                on_result=self._capturing(
                    turn, actor_id=actor_id, base_id=base_id, purpose="baseline_turn"
                ),
            )
        except LLMCallError:
            if lock is None:
                self._record_failed_turn("baseline", user_message, state, turn)
            else:
                async with lock:
                    self._record_failed_turn("baseline", user_message, state, turn)
            raise

        # Persist the transcript turn (no speech-act dispatch). The
        # per-base lock serializes concurrent baseline turns on the
        # same session so the transcript stays ordered.
        if lock is None:
            self._record_baseline_turn(user_message, raw_text, state, turn=turn)
        else:
            async with lock:
                self._record_baseline_turn(user_message, raw_text, state, turn=turn)

        # Match the dialectic path's response shape so frontend code
        # doesn't have to branch.
        return {
            "response": raw_text,
            "speech_acts": [],
            "new_tensions": [],
        }

    # ── Research capture (turn_log.py) ──

    def _prompt_identity(
        self, mode: str, system_prompt: str | None = None
    ) -> tuple[str, str, str]:
        """(name, sha256, version) of the system prompt a turn in `mode`
        ran under. `system_prompt` is the text actually sent, when the
        caller has it (the baseline prompt varies with the topic, so its
        hash is of the text sent while its version is the template's).
        The name is the family as recorded since 0.4 (`sloan` for the
        Elenchus prompt); the version is the file's label."""
        if mode == "baseline":
            prompt = prompts.load("baseline")
            return (
                "baseline",
                turn_log.prompt_fingerprint(system_prompt or prompt.text),
                prompt.version,
            )
        prompt = prompts.load("phase_b" if self.enable_phase_b else "elenchus")
        name = "phase_b" if self.enable_phase_b else "sloan"
        return name, turn_log.prompt_fingerprint(self._system_prompt()), prompt.version

    def _capturing(
        self, turn: dict, *, actor_id: int | None, base_id: str | None, purpose: str = ""
    ):
        """An `on_result` callback that keeps the call's `ChatResult` in
        `turn` for the turn log, then hands it to the usage recorder.
        It runs for failed calls too, which is how a failed turn's
        category / attempts / latency reach the log."""
        recorder = _make_usage_recorder(actor_id=actor_id, base_id=base_id, purpose=purpose)
        # Kept so follow-on calls made on this turn's behalf (the
        # rolling summary) are attributed to the same base.
        turn["base_id"] = base_id

        def _on_result(result: ChatResult) -> None:
            turn["chat_result"] = result
            if recorder is not None:
                recorder(result)

        return _on_result

    def _turn_fields(self, mode: str, turn: dict | None) -> dict:
        """The `record_turn` keyword arguments carried by `turn`."""
        turn = turn or {}
        name, sha, version = self._prompt_identity(mode, turn.get("system_prompt"))
        return {
            "mode": mode,
            "actor_id": turn.get("actor_id"),
            "action_context": turn.get("action_context"),
            "request_content": turn.get("request_content"),
            "history_window": turn.get("history_window"),
            "summary_included": turn.get("summary_included"),
            "system_prompt_name": name,
            "system_prompt_sha256": sha,
            "system_prompt_version": version,
            "draft_snapshot_id": turn.get("draft_snapshot_id"),
            "state_before": turn.get("state_before"),
            "chat_result": turn.get("chat_result"),
        }

    def _record_failed_turn(
        self, mode: str, user_message: str, state: DialecticalState, turn: dict | None
    ) -> None:
        """Log an exchange whose LLM call failed. Nothing else is stored
        for such a turn (no conversation rows, no state change), so this
        row is the only trace that the respondent said something and got
        no answer. Never raises: the caller is already propagating the
        LLM failure and that is the error the route must report."""
        try:
            turn_log.record_turn(
                state.base.con,
                outcome="llm_error",
                user_message=user_message,
                **self._turn_fields(mode, turn),
            )
        except Exception:
            logger.exception("Could not record failed %s turn in turn_log", mode)

    def _record_baseline_turn(
        self,
        user_message: str,
        response: str,
        state: DialecticalState,
        *,
        turn: dict | None = None,
    ) -> None:
        """Append both messages to the conversation log, and the
        exchange to the turn log.

        Runs inside an explicit transaction so a crash between the
        writes leaves the transcript in a consistent state — never one
        turn ahead of the other."""
        state.base.con.execute("BEGIN")
        try:
            user_cid = state.add_conversation("user", user_message)
            assistant_cid = state.add_conversation("assistant", response)
            turn_log.record_turn(
                state.base.con,
                user_message=user_message,
                raw_text=response,
                parse_strategy="plain",
                user_conversation_id=user_cid,
                assistant_conversation_id=assistant_cid,
                **self._turn_fields("baseline", turn),
            )
            state.base.con.execute("COMMIT")
        except Exception:
            try:
                state.base.con.execute("ROLLBACK")
            except Exception:
                logger.exception("Rollback failed in _record_baseline_turn")
            raise

    def _record_and_apply(
        self,
        user_message: str,
        raw_text: str,
        state: DialecticalState,
        *,
        turn: dict | None = None,
    ) -> dict:
        """Common post-LLM bookkeeping: store conversation, parse, apply
        state transitions, write the turn log, periodically update the
        rolling summary.

        `turn` carries what the request side knows for the turn log (see
        `_build_request` / `_capturing`); without it the row is still
        written, with those columns NULL.

        The apply phase runs inside an explicit DuckDB transaction so a
        crash or exception leaves the base either fully pre-message or
        fully post-message — never half-applied. The summary update
        runs outside the transaction (best-effort; shouldn't roll back
        the user's turn).
        """
        con = state.base.con
        con.execute("BEGIN")
        try:
            parsed, parse_strategy = self._parse_response_with_strategy(raw_text)
            user_cid = state.add_conversation("user", user_message)
            # Store the natural-language `response`, not the raw JSON
            # envelope — so the transcript, reload, PDF, and summary all
            # read clean prose without re-parsing (and a parse glitch can't
            # dump JSON into the dialogue). `_apply` still works from the
            # parsed payload. Fall back to raw_text only if there's no
            # usable response string.
            assistant_text = parsed.get("response") or raw_text
            assistant_cid = state.add_conversation("assistant", assistant_text)
            # The turn id is reserved before `_apply` so every state
            # event it causes can point back at this turn; the row goes
            # in afterwards, once `state_after` exists. All inside the
            # transaction: a rolled-back turn leaves no phantom log.
            turn_id = turn_log.next_turn_id(con)
            self._apply(
                parsed,
                state,
                event=EventContext(
                    source="opponent",
                    turn_id=turn_id,
                    actor_id=(turn or {}).get("actor_id"),
                ),
            )
            turn_log.record_turn(
                con,
                turn_id=turn_id,
                user_message=user_message,
                raw_text=raw_text,
                parse_strategy=parse_strategy,
                parsed=parsed,
                state_after=state.to_dict(),
                user_conversation_id=user_cid,
                assistant_conversation_id=assistant_cid,
                **self._turn_fields("elenchus", turn),
            )
            con.execute("COMMIT")
        except Exception:
            try:
                con.execute("ROLLBACK")
            except Exception:
                logger.exception("Rollback failed in _record_and_apply")
            raise

        total_turns = len(state.get_conversation())
        if total_turns > 0 and total_turns % 20 == 0:
            self._update_summary(
                state,
                actor_id=(turn or {}).get("actor_id"),
                base_id=(turn or {}).get("base_id"),
            )

        return parsed

    def generate_summary(
        self,
        state: DialecticalState,
        *,
        actor_id: int | None = None,
        base_id: str | None = None,
    ) -> str:
        """Generate a substantive analytical summary of the dialectic.

        Returns the summary text without storing it. Used for PDF reports.
        `actor_id` / `base_id` attribute the call in the usage table.
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
            summary = self._chat(
                [{"role": "user", "content": prompt}],
                max_tokens=800,
                on_result=_make_usage_recorder(
                    actor_id=actor_id, base_id=base_id, purpose="report_summary"
                ),
            )
            logger.info(
                "Generated analytical summary for dialectic '%s' (%d chars)",
                s["name"],
                len(summary),
            )
            return summary
        except Exception as e:
            logger.error("Failed to generate summary for '%s': %s", s["name"], e)
            return f"Summary generation failed: {e}"

    def _update_summary(
        self,
        state: DialecticalState,
        *,
        actor_id: int | None = None,
        base_id: str | None = None,
    ):
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
            summary = self._chat(
                [{"role": "user", "content": prompt}],
                max_tokens=500,
                on_result=_make_usage_recorder(
                    actor_id=actor_id, base_id=base_id, purpose="rolling_summary"
                ),
            )
            state.set_summary(summary)
        except Exception:
            logger.debug("Summary update failed (non-critical)")

    def _parse_response(self, text: str) -> dict:
        """Parse the LLM's response into the opponent's expected payload
        shape. Tolerates code fences, prose preamble, and trailing
        chatter via `response_parsing.parse_llm_response`. Falls back
        to wrapping the raw text as a plain conversational response so
        the dialogue never breaks on a malformed turn."""
        return self._parse_response_with_strategy(text)[0]

    def _parse_response_with_strategy(self, text: str) -> tuple[dict, str]:
        """`_parse_response`, plus which recovery path produced the
        payload (recorded in the turn log)."""
        from .response_parsing import parse_llm_response_with_strategy

        parsed, strategy = parse_llm_response_with_strategy(text)
        # A JSON value that isn't an object (a bare string or list) is
        # not the opponent's envelope — treat it as prose, like any
        # other unparseable turn, rather than crash on `.get`.
        if isinstance(parsed, dict):
            return parsed, strategy

        # Final fallback: treat entire text as conversational response.
        # Log so we can spot prompt-adherence regressions during runs.
        logger.warning(
            "LLM response did not contain parseable JSON; treating as plain "
            "text (len=%d, first 80 chars=%r)",
            len(text),
            text[:80],
        )
        return {"speech_acts": [], "new_tensions": [], "response": text}, "plain_text_fallback"

    def _apply(self, parsed: dict, state: DialecticalState, event: EventContext | None = None):
        """Apply speech acts and tensions to state.

        `event` attributes the resulting `state_events` rows to this
        turn. The state methods log what they change; this method logs
        what *doesn't* reach them — a speech act it drops — so the
        export accounts for every act the LLM emitted."""

        def dropped(act: dict, note: str) -> None:
            turn_log.record_state_event(
                state.base.con,
                str(act.get("type") or "UNKNOWN"),
                {"speech_act": act},
                event=event,
                outcome="dropped",
                note=note,
            )

        turn_event = event
        for act in parsed.get("speech_acts", []):
            atype = act.get("type", "")
            prop = act.get("proposition", "")
            # An act the opponent read off a change in the respondent's
            # written draft (design-notes/text-as-positum.md §3) is logged
            # with source 'text': the move log then says which commitments
            # came from writing and which from conversation. The state
            # effect is the same either way.
            event = (
                EventContext(
                    source="text",
                    turn_id=turn_event.turn_id if turn_event else None,
                    actor_id=turn_event.actor_id if turn_event else None,
                )
                if act.get("source") == "text" and turn_event is not None
                else turn_event
            )

            if atype == "COMMIT" and prop:
                state.commit(prop, event=event)
            elif atype == "DENY" and prop:
                state.deny(prop, event=event)
            elif atype == "RETRACT" and prop:
                state.retract_prop(prop, event=event)
            elif atype == "REFINE":
                old = act.get("old_proposition", "")
                # REFINE is applied as retract-old + commit-new; this
                # event is what ties the two halves together.
                turn_log.record_state_event(
                    state.base.con,
                    "REFINE",
                    {"old_proposition": old, "proposition": prop},
                    event=event,
                    outcome="applied" if (old or prop) else "dropped",
                    note="" if (old or prop) else "no old or new proposition",
                )
                if old:
                    state.retract_prop(old, event=event)
                if prop:
                    state.commit(prop, event=event)
            elif atype == "ACCEPT_TENSION":
                tid = act.get("target_tension_id")
                if tid is None:
                    dropped(act, "no target_tension_id")
                else:
                    result = state.accept_tension(_parse_tension_id(tid), event=event)
                    if not result:
                        logger.info(
                            "Skipped ACCEPT_TENSION #%s (already resolved or not found)", tid
                        )
            elif atype == "CONTEST_TENSION":
                tid = act.get("target_tension_id")
                if tid is None:
                    dropped(act, "no target_tension_id")
                else:
                    result = state.contest_tension(_parse_tension_id(tid), event=event)
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
                    dropped(act, "Phase B firewall (ELENCHUS_ENABLE_PHASE_B is off)")
                    continue
                if atype == "ASSERT_IMPLICATION":
                    gamma = act.get("gamma", [])
                    delta = act.get("delta", [])
                    reason = act.get("reason", "")
                    if gamma or delta:
                        iid = state.assert_implication(gamma, delta, reason=reason, event=event)
                        logger.info(
                            "Applied ASSERT_IMPLICATION → assessments.id=%d (γ=%d, δ=%d)",
                            iid,
                            len(gamma),
                            len(delta),
                        )
                    else:
                        logger.warning("Skipped ASSERT_IMPLICATION with empty γ and δ")
                        dropped(act, "empty gamma and delta")
                elif atype == "INTRODUCE_BEARER":
                    if prop:
                        description = act.get("description", "")
                        state.introduce_bearer(prop, description=description, event=event)
                        logger.info("Applied INTRODUCE_BEARER %r", prop)
                    else:
                        logger.warning("Skipped INTRODUCE_BEARER with no proposition")
                        dropped(act, "no proposition")
                else:  # RETRACT_IMPLICATION
                    iid_raw = act.get("implication_id")
                    try:
                        iid = int(iid_raw) if iid_raw is not None else None
                    except (TypeError, ValueError):
                        iid = None
                    if iid is not None:
                        ok = state.retract_implication(iid, event=event)
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
                        dropped(act, "missing or non-integer implication_id")
            else:
                # Unknown type, or a COMMIT / DENY / RETRACT with no
                # proposition: nothing to apply.
                logger.info("Dropped speech act with nothing to apply: %r", act)
                dropped(act, "unknown type or missing proposition")

        for t in parsed.get("new_tensions", []):
            gamma = t.get("gamma", [])
            delta = t.get("delta", [])
            reason = t.get("reason", "")
            if gamma or delta:
                # Ensure atoms exist
                for a in gamma + delta:
                    state.base.add_atoms({a}, contributor="oracle")
                state.add_tension(gamma, delta, reason, event=event)
            else:
                turn_log.record_state_event(
                    state.base.con,
                    "PROPOSE_TENSION",
                    {"tension": t},
                    event=event,
                    outcome="dropped",
                    note="empty gamma and delta",
                )

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
