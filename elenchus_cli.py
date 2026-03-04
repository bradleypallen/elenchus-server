"""
elenchus_cli.py — Terminal interface for Elenchus

A REPL that lets you have a natural language dialectic from
the command line. Same protocol, same oracle, no web server needed.

Usage:
    python elenchus_cli.py --name "My Topic"
    python elenchus_cli.py --db saved.duckdb
"""

import argparse
import os

from dialectical_state import DialecticalState
from opponent import Opponent


def main():
    parser = argparse.ArgumentParser(description="Elenchus CLI")
    parser.add_argument(
        "--db", default=None, help="DuckDB file (creates if missing, omit for in-memory)"
    )
    parser.add_argument("--name", default="inquiry", help="Topic name (for new dialectics)")
    parser.add_argument("--model", default="claude-sonnet-4-20250514", help="Anthropic model")
    args = parser.parse_args()

    # Load or create state
    if args.db:
        if os.path.exists(args.db):
            state = DialecticalState.open(args.db)
            print(f"Resumed: {state.base.name}")
        else:
            state = DialecticalState.create(args.db, args.name)
            print(f"Created: {args.name} → {args.db}")
    else:
        state = DialecticalState.in_memory(args.name)
        print(f"In-memory session: {args.name}")

    opp = Opponent(model=args.model)

    # Show current state
    _show_state(state)
    print()
    print("Type naturally. The opponent will parse your speech acts.")
    print("Commands: /state  /tensions  /implications  /derive  /report  /quit")
    print()

    while True:
        try:
            msg = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if not msg:
            continue

        # Meta-commands
        if msg.startswith("/"):
            cmd = msg[1:].lower().split()[0]
            if cmd in ("quit", "exit", "q"):
                break
            elif cmd == "state":
                _show_state(state)
            elif cmd in ("tensions", "t"):
                _show_tensions(state)
            elif cmd in ("implications", "i"):
                _show_implications(state)
            elif cmd == "derive":
                _derive(msg, state)
            elif cmd == "report":
                print(state.base.report())
            elif cmd == "help":
                print("Commands: /state /tensions /implications /derive /report /quit")
            else:
                print(f"Unknown command: {msg}")
            print()
            continue

        # Send to opponent
        print()
        try:
            result = opp.respond(msg, state)
            response = result.get("response", "")
            if response:
                print(f"Opponent: {response}")

            # Show state changes
            acts = result.get("speech_acts", [])
            new_t = result.get("new_tensions", [])

            if acts:
                for a in acts:
                    _show_act(a)

            if new_t:
                for t in state.T[-len(new_t) :]:
                    print(f"  ⚡ #{t['id']}: {t['reason']}")

        except Exception as e:
            print(f"Error: {e}")

        print()

    print("\nSession ended.")
    _show_state(state)

    if args.db:
        print(f"State saved to {args.db}")


def _show_state(state):
    d = state.to_dict()
    print(f"  [{d['name']}]")
    print(
        f"  C: {len(d['commitments'])} commitments, "
        f"D: {len(d['denials'])} denials, "
        f"T: {len(d['tensions'])} tensions, "
        f"I: {len(d['implications'])} implications"
    )


def _show_tensions(state):
    T = state.T
    if not T:
        print("  No open tensions.")
        return
    for t in T:
        g = ", ".join(t["gamma"])
        d = ", ".join(t["delta"])
        print(f"  #{t['id']}: {{{g}}} |~ {{{d}}}")
        print(f"          {t['reason']}")


def _show_implications(state):
    I = state.I
    if not I:
        print("  No material implications.")
        return
    for imp in I:
        g = ", ".join(imp["gamma"])
        d = ", ".join(imp["delta"])
        print(f"  {{{g}}} |~ {{{d}}}")


def _show_act(act):
    t = act.get("type", "")
    p = act.get("proposition", "")
    if t == "COMMIT":
        print(f"  + {p}")
    elif t == "DENY":
        print(f"  − {p}")
    elif t == "RETRACT":
        print(f"  ↩ {p}")
    elif t == "REFINE":
        old = act.get("old_proposition", "?")
        print(f"  ↩ {old} → {p}")
    elif t == "ACCEPT_TENSION":
        print(f"  ✓ Accepted tension #{act.get('target_tension_id')}")
    elif t == "CONTEST_TENSION":
        print(f"  ✗ Contested tension #{act.get('target_tension_id')}")


def _derive(msg, state):
    parts = msg.split()
    if len(parts) < 2:
        print("  Usage: /derive premise1,premise2 ~ conclusion1")
        return
    rest = " ".join(parts[1:])
    for sep in ("|~", "~", "|∼"):
        if sep in rest:
            left, right = rest.split(sep, 1)
            gamma = [x.strip() for x in left.split(",") if x.strip()]
            delta = [x.strip() for x in right.split(",") if x.strip()]
            result = state.derives(gamma, delta)
            sym = "✓" if result else "✗"
            print(f"  {sym} {{{', '.join(gamma)}}} |~ {{{', '.join(delta)}}}")
            return
    print("  Usage: /derive premise ~ conclusion")


if __name__ == "__main__":
    main()
