"""
opponent.py — The LLM opponent / derivability oracle

Sends the dialectical state to the Anthropic API, parses structured
responses, and applies state transitions per Figure 4.
"""

import json
import logging
import os

from anthropic import Anthropic

from dialectical_state import DialecticalState

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are the opponent in an Elenchus dialectic (Allen 2026). You are conducting a prover-skeptic dialogue where the human respondent develops a bilateral position on a topic.

YOUR ROLE:
- Parse the respondent's natural language into formal speech acts
- Maintain the bilateral state [C : D] (commitments and denials)
- Detect and propose tensions (incoherences in the position)
- Be charitable: interpret claims in their strongest plausible form
- Be relentless but patient

SPEECH ACT RECOGNITION:
When the respondent speaks, classify their utterances:
- COMMIT: asserting/endorsing a proposition
- DENY: rejecting a proposition
- ACCEPT_TENSION: agreeing a tension is genuine (by number)
- CONTEST_TENSION: rejecting a tension (by number)
- RETRACT: withdrawing a previous commitment or denial
- REFINE: replacing a commitment with a more precise version

RESPONSE FORMAT — respond ONLY with this JSON, no markdown:
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
- You may propose new_tensions if the updated position warrants them"""


class Opponent:
    def __init__(
        self,
        model: str = "claude-sonnet-4-20250514",
        api_key: str | None = None,
        base_url: str | None = None,
    ):
        client_kwargs = {}
        if api_key:
            client_kwargs["api_key"] = api_key
        if base_url:
            client_kwargs["base_url"] = base_url
        self.client = Anthropic(**client_kwargs)
        self.model = model
        self.base_url = base_url
        self._api_key = api_key
        self._has_api_key = bool(api_key or os.environ.get("ANTHROPIC_API_KEY"))
        logger.info(
            "Opponent initialized: model=%s, base_url=%s, api_key_set=%s",
            model,
            base_url or "(default)",
            self._has_api_key,
        )

    def reconfigure(
        self, model: str | None = None, api_key: str | None = None, base_url: str | None = None
    ):
        """Recreate the Anthropic client with new settings."""
        if model:
            self.model = model
        if api_key:
            self._api_key = api_key
            self._has_api_key = True
        if base_url is not None:
            self.base_url = base_url if base_url else None
        # Rebuild client, preserving existing credentials
        client_kwargs = {}
        if getattr(self, "_api_key", None):
            client_kwargs["api_key"] = self._api_key
        if self.base_url:
            client_kwargs["base_url"] = self.base_url
        self.client = Anthropic(**client_kwargs)
        logger.info(
            "Opponent reconfigured: model=%s, base_url=%s, api_key_updated=%s",
            self.model,
            self.base_url or "(default)",
            bool(api_key),
        )

    def respond(self, user_message: str, state: DialecticalState, context_turns: int = 6) -> dict:
        """
        Send the respondent's message + dialectical state to the LLM.

        The formal state (C, D, T, I) is always sent in full — it's
        compact. Conversation history is windowed to the last N turns
        for continuity, plus a summary of earlier discussion.

        Parse the structured response. Apply state transitions.
        Return the result.
        """
        # Build the formal state block (always complete, always compact)
        s = state.to_dict()
        row = state.base.con.execute("SELECT COALESCE(MAX(id), 0) FROM tensions").fetchone()
        tid = row[0]

        formal_state = f"""CURRENT DIALECTICAL STATE:
Topic: {s["name"]}
Next tension ID: {tid + 1}

Commitments (C):{self._fmt_list(s["commitments"])}
Denials (D):{self._fmt_list(s["denials"])}
Open tensions (T):{self._fmt_tensions(s["tensions"])}
Material implications (I):{self._fmt_implications(s["implications"])}
Retracted:{self._fmt_list(s["retracted"])}"""

        # Build message with respondent's input
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
            ui_action_note = """
[NOTE: This action was applied via the UI — the state above already reflects it. This is the respondent's JUST-MADE decision. Do NOT say it was "already done" or "already processed." Respond as if they just told you their decision in conversation. Discuss the philosophical implications.]
"""

        user_content = f"""{formal_state}

RESPONDENT SAYS: "{user_message}" {ui_action_note}"""

        # Get windowed conversation history
        # The formal state above makes the full history unnecessary —
        # we only need recent turns for conversational continuity
        history = state.get_conversation()

        # If history is longer than the window, prepend a summary
        messages = []
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

        # Call API
        response = self.client.messages.create(
            model=self.model,
            max_tokens=2000,
            system=SYSTEM_PROMPT,
            messages=messages,
        )

        raw_text = response.content[0].text

        # Store in conversation history (full, for the record)
        state.add_conversation("user", user_message)
        state.add_conversation("assistant", raw_text)

        # Periodically update the summary (every 10 turns)
        total_turns = len(state.get_conversation())
        if total_turns > 0 and total_turns % 20 == 0:
            self._update_summary(state)

        # Parse
        parsed = self._parse_response(raw_text)

        # Apply state transitions
        self._apply(parsed, state)

        return parsed

    def generate_summary(self, state: DialecticalState) -> str:
        """Generate a substantive analytical summary of the dialectic.

        Returns the summary text without storing it. Used for PDF reports.
        """
        s = state.to_dict()

        # Build a rich prompt with full formal state
        commitments_block = "\n".join(f'  - "{c}"' for c in s["commitments"]) or "  (none)"
        denials_block = "\n".join(f'  - "{d}"' for d in s["denials"]) or "  (none)"
        retracted_block = "\n".join(f'  - "{r}"' for r in s["retracted"]) or "  (none)"

        tensions_block = ""
        for t in s["tensions"]:
            g = ", ".join(f'"{x}"' for x in t["gamma"])
            d = ", ".join(f'"{x}"' for x in t["delta"])
            tensions_block += f"\n  #{t['id']}: {{{g}}} |~ {{{d}}}: {t['reason']}"
        if not tensions_block:
            tensions_block = "  (none)"

        implications_block = ""
        for imp in s["implications"]:
            g = ", ".join(f'"{x}"' for x in imp["gamma"])
            d = ", ".join(f'"{x}"' for x in imp["delta"])
            implications_block += f"\n  {{{g}}} |~ {{{d}}}"
        if not implications_block:
            implications_block = "  (none)"

        contested_block = ""
        for t in s.get("contested", []):
            g = ", ".join(f'"{x}"' for x in t["gamma"])
            d = ", ".join(f'"{x}"' for x in t["delta"])
            contested_block += f"\n  #{t['id']}: {{{g}}} |~ {{{d}}}: {t['reason']}"
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

Write 1-3 short paragraphs. Be concise and precise. Describe the position as it stands now — do not narrate the history of how it got here. Do NOT include a title or heading — start directly with the substantive content."""

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=800,
                messages=[{"role": "user", "content": prompt}],
            )
            summary = response.content[0].text
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
            response = self.client.messages.create(
                model=self.model,
                max_tokens=500,
                messages=[{"role": "user", "content": prompt}],
            )
            summary = response.content[0].text
            state.set_summary(summary)
        except Exception:
            logger.debug("Summary update failed (non-critical)")

    def _parse_response(self, text: str) -> dict:
        """Parse JSON from the LLM response."""
        try:
            clean = text.strip()
            if clean.startswith("```"):
                clean = clean.split("\n", 1)[1]
                if clean.endswith("```"):
                    clean = clean[:-3]
                clean = clean.strip()
            return json.loads(clean)
        except json.JSONDecodeError:
            # If the model responded conversationally, wrap it
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
                    result = state.accept_tension(int(tid))
                    if not result:
                        logger.info(
                            "Skipped ACCEPT_TENSION #%s (already resolved or not found)", tid
                        )
            elif atype == "CONTEST_TENSION":
                tid = act.get("target_tension_id")
                if tid is not None:
                    result = state.contest_tension(int(tid))
                    if not result:
                        logger.info(
                            "Skipped CONTEST_TENSION #%s (already resolved or not found)", tid
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

    def _fmt_list(self, items):
        if not items:
            return " (none)"
        return "".join(f'\n  - "{item}"' for item in items)

    def _fmt_tensions(self, tensions):
        if not tensions:
            return " (none)"
        lines = []
        for t in tensions:
            g = ", ".join(f'"{x}"' for x in t["gamma"])
            d = ", ".join(f'"{x}"' for x in t["delta"])
            lines.append(f"\n  #{t['id']}: {{{g}}} |~ {{{d}}}: {t['reason']}")
        return "".join(lines)

    def _fmt_implications(self, imps):
        if not imps:
            return " (none)"
        lines = []
        for imp in imps:
            g = ", ".join(f'"{x}"' for x in imp["gamma"])
            d = ", ".join(f'"{x}"' for x in imp["delta"])
            lines.append(f"\n  {{{g}}} |~ {{{d}}}")
        return "".join(lines)
