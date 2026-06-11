"""
driver.py — pluggable persona engines.

A driver supplies the *content* the harness needs: the participant's
next message, the judge's rating, and (scripted only) the canned LLM
the server's opponent runs on. The harness handles all the HTTP
orchestration; the driver only decides what to say.

ScriptedDriver is deterministic and free — the server's LLM is
replaced by `CannedLLMClient` so the entire request stack still runs,
just over fixed responses. LLMDriver reuses the platform's own
LLMClient, so the simulation's spend lands in the same usage table the
platform tracks.
"""

from __future__ import annotations

import json
import logging

from ..llm_client import ChatCategory, ChatResult
from ..questionnaires import INSTRUMENTS
from .personas import JudgePersona, ParticipantPersona

logger = logging.getLogger(__name__)


# ── Canned LLM (scripted mode) ───────────────────────────────────────


def _dialectic_response(n: int) -> str:
    """A valid dialectic response that commits a fresh proposition and
    proposes a fresh tension every call. Unique text per call (keyed on
    `n`) keeps atoms/tensions from colliding, so every task turn yields
    a focal tension the participant can accept — exercising the
    accept-tension → material-implication path on every session.

    `gamma` need not be verbatim in C (that's a prompt rule, not an
    enforced one), so a fixed-shape payload mutates state cleanly.
    """
    claim = f"Core concept {n} is well defined."
    consequence = f"Instances of core concept {n} classify unambiguously."
    return json.dumps(
        {
            "speech_acts": [{"type": "COMMIT", "proposition": claim}],
            "new_tensions": [
                {
                    "gamma": [claim],
                    "delta": [consequence],
                    "reason": "A well-defined concept should classify its instances, but edge cases resist.",
                }
            ],
            "response": "I've recorded a commitment and I see a tension worth examining.",
        }
    )


_BASELINE_REPLY = (
    "Good question. The domain is usually organized around a small set of core "
    "concepts, with classification rules layered on top. Edge cases are typically "
    "handled by explicit exception lists."
)
_CANNED_REPORT = """# Domain
The conceptual domain under specification.

# Atomic statements
1. The domain has well-defined core concepts.
2. Edge cases require explicit handling.

# Implications
1. If a concept is a well-defined core concept, then instances of it can be classified.

# Notes
Edge cases are a recognized limitation of any purely rule-based classification.
"""


class CannedLLMClient:
    """Drop-in for `Opponent._llm_client` in scripted mode. Branches on
    the system prompt / user message to return the right shape:
      * dialectic opponent (system mentions 'prover-skeptic') → JSON
      * report generation (user contains 'SOURCE MATERIAL')   → markdown
      * baseline chat (everything else)                        → plain text
    Token counts are tiny and the model name is unknown to pricing.py,
    so scripted runs cost $0 — which the report asserts.
    """

    def __init__(self, model: str = "sim-canned"):
        self.model = model
        self._dialectic_calls = 0

    def _respond(self, messages, system) -> ChatResult:
        sys = system or ""
        user = messages[-1]["content"] if messages else ""
        if "prover-skeptic" in sys:
            text = _dialectic_response(self._dialectic_calls)
            self._dialectic_calls += 1
        elif "SOURCE MATERIAL" in user:
            text = _CANNED_REPORT
        else:
            text = _BASELINE_REPLY
        return ChatResult(
            category=ChatCategory.SUCCESS,
            text=text,
            attempts=1,
            latency_ms=1,
            prompt_tokens=20,
            completion_tokens=10,
            model=self.model,
        )

    def chat(self, messages, *, system=None, max_tokens=2000, model=None) -> ChatResult:
        return self._respond(messages, system)

    async def achat(self, messages, *, system=None, max_tokens=2000, model=None) -> ChatResult:
        return self._respond(messages, system)


# ── Drivers ──────────────────────────────────────────────────────────


def _full_survey_response(instrument: str) -> dict:
    """A valid mid-scale submission for any instrument."""
    spec = INSTRUMENTS[instrument]
    return {item["id"]: (item["scale_min"] + item["scale_max"]) // 2 for item in spec["items"]}


def _scripted_rating(persona: JudgePersona) -> dict:
    """A complete, schema-valid judge rating from a scripted disposition."""
    dims = ["completeness", "correctness", "conciseness", "fidelity", "coherence"]
    fav_a = persona.favor == "a"
    return {
        "ratings": {d: {"a": 5 if fav_a else 4, "b": 4 if fav_a else 5} for d in dims},
        "justification_a": "Adequate coverage of the domain.",
        "justification_b": "Comparable, with different emphasis.",
        "pairwise_winner": persona.favor,
        "condition_guess_a": persona.guess,
        "condition_guess_b": persona.guess,
        "confidence": persona.confidence,
    }


class ScriptedDriver:
    """Deterministic, free, CI-able. The server's opponent runs on
    `CannedLLMClient`, so the full stack still executes."""

    uses_llm = False

    def canned_llm_client(self) -> CannedLLMClient:
        return CannedLLMClient()

    def participant_tutorial_message(self, persona: ParticipantPersona) -> str:
        return persona.tutorial_message

    def participant_task_message(
        self, persona: ParticipantPersona, condition: str, turn_idx: int, state: dict
    ) -> str:
        msgs = persona.scripted_task_messages
        return msgs[min(turn_idx, len(msgs) - 1)] if msgs else "Continue."

    def survey_response(self, instrument: str) -> dict:
        return _full_survey_response(instrument)

    def judge_rating(self, persona: JudgePersona, slot_a: str, slot_b: str) -> dict:
        return _scripted_rating(persona)


class LLMDriver:
    """Real-LLM personas for pre-pilot rehearsals. Reuses an `LLMClient`
    (typically the platform opponent's) so spend is tracked. Survey
    responses stay deterministic (mid-scale) — questionnaires aren't
    where the dialogue-fidelity signal lives."""

    uses_llm = True

    def __init__(self, llm_client):
        self._llm = llm_client

    def canned_llm_client(self):  # not used — server uses its real client
        return None

    def participant_tutorial_message(self, persona: ParticipantPersona) -> str:
        return persona.tutorial_message

    def participant_task_message(
        self, persona: ParticipantPersona, condition: str, turn_idx: int, state: dict
    ) -> str:
        domain = persona.elenchus_domain if condition == "elenchus" else persona.baseline_domain
        system = (
            f"You are a domain expert in {domain}. You are working with an AI to "
            f"develop a conceptual specification of this domain. Your disposition is "
            f"'{persona.disposition}'. Reply with ONE short, substantive turn — a "
            f"commitment, a response to the AI, or a refinement. Plain prose, 1–3 "
            f"sentences. Do not use JSON or meta-commentary."
        )
        commitments = ", ".join(state.get("commitments", [])[:5]) or "(none yet)"
        user = (
            f"Current commitments: {commitments}. This is turn {turn_idx + 1}. "
            f"What is your next contribution to the specification?"
        )
        result = self._llm.chat([{"role": "user", "content": user}], system=system, max_tokens=200)
        return result.text.strip() if result.ok else "Let me continue with the domain."

    def survey_response(self, instrument: str) -> dict:
        return _full_survey_response(instrument)

    def judge_rating(self, persona: JudgePersona, slot_a: str, slot_b: str) -> dict:
        system = (
            "You are an expert judge evaluating two anonymized conceptual "
            "specifications of a domain. Rate each on five 1-7 dimensions "
            "(completeness, correctness, conciseness, fidelity, coherence), pick an "
            "overall winner, and guess which was produced through structured dialogue "
            "vs free-form chat. Respond ONLY with JSON: "
            '{"ratings":{"completeness":{"a":N,"b":N},...},'
            '"justification_a":"...","justification_b":"...",'
            '"pairwise_winner":"a|b|tie",'
            '"condition_guess_a":"elenchus|baseline|unsure",'
            '"condition_guess_b":"elenchus|baseline|unsure","confidence":N}'
        )
        user = f"OUTPUT A:\n{slot_a}\n\nOUTPUT B:\n{slot_b}"
        result = self._llm.chat([{"role": "user", "content": user}], system=system, max_tokens=800)
        if result.ok:
            parsed = _extract_json(result.text)
            if parsed is not None:
                return _coerce_rating(parsed)
        # Fall back to a neutral valid rating so the run continues.
        return _scripted_rating(JudgePersona(label=persona.label, favor="tie", guess="unsure"))


def _extract_json(text: str):
    import re

    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def _coerce_rating(d: dict) -> dict:
    """Make a parsed LLM rating schema-valid (clamp ranges, default
    missing fields) so the judge endpoint accepts it."""
    dims = ["completeness", "correctness", "conciseness", "fidelity", "coherence"]
    ratings = {}
    src = d.get("ratings", {}) if isinstance(d.get("ratings"), dict) else {}
    for dim in dims:
        slot = src.get(dim, {}) if isinstance(src.get(dim), dict) else {}
        ratings[dim] = {
            "a": _clamp(slot.get("a"), 1, 7, 4),
            "b": _clamp(slot.get("b"), 1, 7, 4),
        }
    winner = d.get("pairwise_winner")
    if winner not in ("a", "b", "tie"):
        winner = "tie"
    ga = d.get("condition_guess_a")
    gb = d.get("condition_guess_b")
    return {
        "ratings": ratings,
        "justification_a": str(d.get("justification_a", ""))[:2000],
        "justification_b": str(d.get("justification_b", ""))[:2000],
        "pairwise_winner": winner,
        "condition_guess_a": ga if ga in ("elenchus", "baseline", "unsure") else "unsure",
        "condition_guess_b": gb if gb in ("elenchus", "baseline", "unsure") else "unsure",
        "confidence": _clamp(d.get("confidence"), 1, 7, 3),
    }


def _clamp(v, lo, hi, default):
    try:
        iv = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, iv))
