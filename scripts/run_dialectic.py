#!/usr/bin/env python3
"""
run_dialectic.py — watch a dialectic unfold between two LLMs.

A respondent LLM plays a domain expert who *posits* a paragraph (by
default a scientific upper-ontology specification) and then DEFENDS it
under examination by the real Elenchus opponent LLM (the prover-skeptic).
The respondent decides, for each tension the opponent raises, whether to
CONTEST it (reject the inference or a premise — most objections have a
principled answer) or ACCEPT it (genuinely grant the consequence, which
becomes a material implication). It defends by default and concedes only
under decisive pressure, so it does not cave at every objection.

Both sides are LLMs; the opponent is the exact same engine the platform
uses, so the extracted commitments, the tensions, the contests, and the
accepted material implications are all genuine. It drives `Opponent` +
`DialecticalState` directly (in-process, like the CLI) — no server, no
study scaffolding — and prints the transcript plus the resulting
bilateral position [C : D], accepted implications, contested tensions,
and any still-open tension.

Examples:
    # default scientific-ontology positum, 5 exchanges
    python scripts/run_dialectic.py

    # your own positum + domain, opponent and respondent on different models
    python scripts/run_dialectic.py \
        --positum @my_ontology_paragraph.txt \
        --domain "the Sequence Ontology" \
        --turns 6 --respondent-model claude-sonnet-4-6 \
        --out dialectic.json

Needs ELENCHUS_API_KEY / ANTHROPIC_API_KEY (real LLM calls).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import textwrap

# A genuinely dialectically-fertile scientific-ontology positum: a
# Basic-Formal-Ontology-style upper ontology. The "exactly one bearer"
# and "every occurrent has a continuant participant" clauses invite real
# tensions (relational qualities, boundaries, processes without manifest
# participants, ...).
DEFAULT_DOMAIN = "a scientific upper ontology (continuants and occurrents)"
DEFAULT_POSITUM = (
    "In this ontology every entity is either a continuant or an occurrent. "
    "Continuants persist through time while preserving their identity — a cell, "
    "an organism, a molecule — whereas occurrents unfold through successive "
    "temporal phases, such as cell division or a chemical reaction. Continuants "
    "divide into independent continuants, which can exist on their own (a "
    "mitochondrion), and dependent continuants, which require a bearer (a mass, "
    "an electric charge, a biological function). Every dependent continuant "
    "inheres in exactly one independent continuant, and every occurrent has at "
    "least one continuant participant."
)

_W = 96


def _wrap(label: str, text: str, color: str = "") -> str:
    body = textwrap.fill(
        (text or "").strip(),
        _W,
        initial_indent="    ",
        subsequent_indent="    ",
    )
    return f"\n{color}{label}{_RESET if color else ''}\n{body}"


_RESET = "\033[0m"
_DIM = "\033[2m"
_RESP = "\033[36m"  # cyan
_OPP = "\033[35m"  # magenta
_SYS = "\033[33m"  # yellow


class LLMRespondent:
    """A domain expert defending a posited specification under Socratic
    examination. Reuses the opponent's LLM client (so spend is tracked
    and the model/key are already configured); an optional separate model
    keeps the two voices distinct."""

    def __init__(self, client, model, domain, positum, disposition):
        self._llm = client
        self._model = model
        self.domain = domain
        self.positum = positum
        self.disposition = disposition

    def reply(self, state: dict, opponent_text: str) -> tuple[str, str]:
        """Return (verdict, prose). `verdict` is 'contest' | 'accept' |
        'none'. The respondent DEFENDS: it contests tensions it judges
        merely apparent and accepts only those it genuinely grants."""
        commitments = state.get("commitments", [])
        focal = (state.get("tensions") or [None])[0]
        has_tension = focal is not None
        system = (
            f"You are a leading expert in {self.domain}, defending a considered "
            f"specification under adversarial Socratic examination. You posited it "
            f"and you stand behind it. The examiner's job is to find tensions; "
            f"yours is to DEFEND.\n"
            f"- Do NOT concede merely because you are pressed. Most objections have "
            f"a principled answer that preserves your commitment: draw a "
            f"distinction, clarify a term, reject a false dichotomy, deny a hidden "
            f"premise, or show the putative tension dissolves under the correct "
            f"reading. Push back on the examiner's framing when it smuggles in "
            f"assumptions you do not hold.\n"
            f"- Hold your core commitments firm. Refine wording only to sharpen the "
            f"position, never as a retreat.\n"
            f"- Concede ONLY when an objection is genuinely decisive and no "
            f"distinction or refinement can save the commitment. That should be "
            f"rare.\n"
            f"Stance: {self.disposition}."
        )
        lines = ["Your current commitments:\n- " + "\n- ".join(commitments[:14] or ["(none yet)"])]
        if has_tension:
            g = ", ".join(focal.get("gamma", []))
            d = ", ".join(focal.get("delta", []))
            lines.append(
                f"\nThe examiner has put a TENSION on the table: from [{g}] it infers [{d}]."
            )
            lines.append(f"\nThe examiner just said:\n{opponent_text}")
            lines.append(
                "\nDecide whether the tension is real, then answer. Begin with EXACTLY one line:\n"
                "VERDICT: CONTEST  — you reject the inference or one of its premises; the tension is only apparent\n"
                "VERDICT: ACCEPT   — you genuinely grant that your commitments force this consequence\n"
                "Then 2-4 sentences of plain prose defending your decision, as the expert. "
                "No JSON, no lists."
            )
        else:
            lines.append(f"\nThe examiner just said:\n{opponent_text}")
            lines.append(
                "\nBegin with the line 'VERDICT: NONE', then reply in 2-4 sentences of "
                "plain prose, as the expert."
            )
        result = self._llm.chat(
            [{"role": "user", "content": "\n".join(lines)}],
            system=system,
            max_tokens=400,
            model=self._model or None,
        )
        text = (
            result.text.strip()
            if result.ok
            else "VERDICT: CONTEST\nLet me restate and defend my position."
        )
        return self._split_verdict(text, has_tension)

    @staticmethod
    def _split_verdict(text: str, has_tension: bool) -> tuple[str, str]:
        verdict = "none"
        prose_lines = []
        for i, ln in enumerate(text.splitlines()):
            m = re.match(r"\s*VERDICT:\s*(CONTEST|ACCEPT|NONE)", ln, re.I)
            if m and i < 3:
                verdict = m.group(1).lower()
                continue
            prose_lines.append(ln)
        prose = "\n".join(prose_lines).strip() or text
        if not has_tension:
            return "none", prose
        # Defend by default: an unclear verdict on an open tension is a contest,
        # not a concession.
        if verdict not in ("contest", "accept"):
            verdict = "contest"
        return verdict, prose


def _fmt_state(state: dict) -> str:
    out = ["\n" + "═" * _W, "  FINAL BILATERAL POSITION  [C : D]", "═" * _W]
    C, D = state.get("commitments", []), state.get("denials", [])
    out.append(f"\n  Commitments (C) — {len(C)}:")
    out += [f"    • {c}" for c in C] or ["    (none)"]
    out.append(f"\n  Denials (D) — {len(D)}:")
    out += [f"    • {d}" for d in D] or ["    (none)"]
    imps = state.get("implications", [])
    out.append(f"\n  Material implications (accepted tensions) — {len(imps)}:")
    for im in imps:
        g = ", ".join(im.get("gamma", [])) if isinstance(im, dict) else str(im)
        d = ", ".join(im.get("delta", [])) if isinstance(im, dict) else ""
        out.append(f"    • {g}  |∼  {d}")
    if not imps:
        out.append("    (none)")
    contested = state.get("contested", [])
    out.append(f"\n  Contested tensions (respondent defended) — {len(contested)}:")
    for t in contested:
        g = ", ".join(t.get("gamma", [])) if isinstance(t, dict) else str(t)
        d = ", ".join(t.get("delta", [])) if isinstance(t, dict) else ""
        out.append(f"    • denied: [{g}] ⊬ [{d}]")
    if not contested:
        out.append("    (none)")
    open_t = (state.get("tensions") or []) + state.get("queued_tensions", [])
    out.append(f"\n  Open tensions — {len(open_t)}:")
    for t in open_t:
        g = ", ".join(t.get("gamma", []))
        d = ", ".join(t.get("delta", []))
        out.append(f"    • from [{g}] → [{d}]")
    if not open_t:
        out.append("    (none)")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="Run a two-LLM dialectic from a positum.")
    ap.add_argument(
        "--positum", default=DEFAULT_POSITUM, help="opening paragraph, or @file to read it"
    )
    ap.add_argument(
        "--domain", default=DEFAULT_DOMAIN, help="the respondent's domain of expertise"
    )
    ap.add_argument(
        "--turns", type=int, default=5, help="respondent↔opponent exchanges after the positum"
    )
    ap.add_argument("--model", default=None, help="opponent model (default: ELENCHUS_MODEL)")
    ap.add_argument(
        "--respondent-model", default=None, help="respondent model (default: same as opponent)"
    )
    ap.add_argument(
        "--disposition",
        default="a leading expert who defends a considered position and concedes "
        "only under decisive, unanswerable pressure",
        help="the respondent's defensive stance",
    )
    ap.add_argument(
        "--out", default=None, help="write the full transcript + final state to this JSON file"
    )
    args = ap.parse_args()

    positum = args.positum
    if positum.startswith("@"):
        with open(positum[1:], encoding="utf-8") as fh:
            positum = fh.read().strip()

    from elenchus.dialectical_state import DialecticalState
    from elenchus.opponent import Opponent

    opp = Opponent(model=args.model) if args.model else Opponent()
    if not opp._has_api_key:
        print("error: no API key (set ELENCHUS_API_KEY / ANTHROPIC_API_KEY).", file=sys.stderr)
        return 2
    opp.enable_phase_b = False  # Elenchus condition: prover-skeptic vocabulary only

    state = DialecticalState.in_memory("ontology")
    respondent = LLMRespondent(
        opp._llm_client, args.respondent_model, args.domain, positum, args.disposition
    )

    print("═" * _W)
    print(f"  DIALECTIC — respondent: LLM domain expert · opponent: Elenchus ({opp.model})")
    print(f"  Domain: {args.domain}")
    if args.respondent_model:
        print(f"  Respondent model: {args.respondent_model}")
    print("═" * _W)
    print(_wrap("RESPONDENT (positum):", positum, _RESP))

    transcript = [{"role": "respondent", "content": positum}]
    result = opp.respond(positum, state)
    print(_wrap("OPPONENT:", result.get("response", ""), _OPP))
    transcript.append({"role": "opponent", "content": result.get("response", "")})

    for turn in range(args.turns):
        sd = state.to_dict()
        focal = (sd.get("tensions") or [None])[0]
        verdict, reply = respondent.reply(sd, result.get("response", ""))

        # The respondent owns the decision: contest a tension it judges
        # merely apparent, accept one it genuinely grants (→ implication).
        if focal:
            g = ", ".join(focal.get("gamma", []))
            d = ", ".join(focal.get("delta", []))
            if verdict == "accept":
                state.accept_tension(focal["id"])
                note = f"[respondent ACCEPTS T{focal['id']} → implication:  {g}  |∼  {d}]"
            else:
                state.contest_tension(focal["id"])
                note = f"[respondent CONTESTS T{focal['id']} — denies that {g} forces {d}]"
            print(_wrap(f"— turn {turn + 1} —", note, _SYS))
            transcript.append({"role": "system", "content": note, "verdict": verdict})

        print(_wrap("RESPONDENT:", reply, _RESP))
        transcript.append({"role": "respondent", "content": reply})

        result = opp.respond(reply, state)
        print(_wrap("OPPONENT:", result.get("response", ""), _OPP))
        transcript.append({"role": "opponent", "content": result.get("response", "")})

    final = state.to_dict()
    print(_fmt_state(final))

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "domain": args.domain,
                    "positum": positum,
                    "opponent_model": opp.model,
                    "respondent_model": args.respondent_model or opp.model,
                    "transcript": transcript,
                    "final_state": final,
                },
                fh,
                indent=2,
                default=str,
            )
        print(f"\n{_DIM}Transcript + final state written to {args.out}{_RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
