#!/usr/bin/env python3
"""
run_dialectic.py — watch a dialectic unfold between two LLMs.

A respondent LLM plays a domain expert who *posits* a paragraph (by
default a scientific upper-ontology specification) and then defends,
refines, or concedes it under examination by the real Elenchus opponent
LLM (the prover-skeptic). Both sides are LLMs; the opponent is the exact
same engine the platform uses, so the commitments it extracts, the
tensions it raises, and the material implications that form when a
tension is accepted are all genuine.

It drives `Opponent` + `DialecticalState` directly (in-process, like the
CLI) — no server, no study scaffolding — and prints a readable transcript
plus the resulting bilateral position [C : D], open tensions, and
accepted implications.

Examples:
    # default scientific-ontology positum, 5 exchanges, auto-accept tensions
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

    def reply(self, state: dict, opponent_text: str) -> str:
        commitments = state.get("commitments", [])
        focal = (state.get("tensions") or [None])[0]
        implications = state.get("implications", [])
        system = (
            f"You are a domain expert in {self.domain}. You are developing a "
            f"conceptual specification through Socratic dialogue with an AI "
            f"examiner who probes your position for hidden tensions. You posited "
            f"the opening specification and you OWN it. Your disposition is "
            f"'{self.disposition}'. Respond to the examiner's latest challenge in "
            f"2-4 sentences of plain prose: defend the commitment, refine it, "
            f"concede a point, or introduce a further commitment — whatever an "
            f"honest expert would do. Do not use JSON, lists, or meta-commentary; "
            f"speak as the expert."
        )
        lines = ["Your current commitments:\n- " + "\n- ".join(commitments[:12] or ["(none yet)"])]
        if implications:
            lines.append(f"\nAccepted implications so far: {len(implications)}.")
        if focal:
            g = ", ".join(focal.get("gamma", []))
            d = ", ".join(focal.get("delta", []))
            lines.append(f"\nA tension is on the table: from [{g}] the examiner draws [{d}].")
        lines.append(f"\nThe examiner just said:\n{opponent_text}\n\nYour reply:")
        result = self._llm.chat(
            [{"role": "user", "content": "\n".join(lines)}],
            system=system,
            max_tokens=300,
            model=self._model or None,
        )
        return result.text.strip() if result.ok else "Let me restate my position and continue."


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
    ap.add_argument("--disposition", default="rigorous and intellectually honest")
    ap.add_argument(
        "--no-accept",
        action="store_true",
        help="don't auto-accept the focal tension each round (leave tensions open)",
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
        # Optionally accept the focal tension → it becomes a material implication.
        if focal and not args.no_accept:
            state.accept_tension(focal["id"])
            g = ", ".join(focal.get("gamma", []))
            d = ", ".join(focal.get("delta", []))
            note = f"[respondent ACCEPTS the tension → implication: {g} |∼ {d}]"
            print(_wrap(f"— turn {turn + 1} —", note, _SYS))
            transcript.append({"role": "system", "content": note})

        reply = respondent.reply(state.to_dict(), result.get("response", ""))
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
