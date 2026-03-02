# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Elenchus is a dialectical knowledge base construction system implementing the Elenchus protocol (Allen 2026). A human respondent develops a bilateral position [C : D] (commitments and denials) through Socratic dialogue with an LLM opponent. Accepted tensions become material implications in a NMMS material base satisfying Containment.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run web server (serves API + static frontend)
python server.py                    # or: uvicorn server:app --reload

# Run CLI (in-memory)
python elenchus_cli.py --name "Topic"

# Run CLI (persistent)
python elenchus_cli.py --db my_inquiry.duckdb --name "Topic"
```

There are no tests or linting configured.

## Environment Variables

- `ANTHROPIC_API_KEY` (required)
- `ELENCHUS_MODEL` — LLM model (default: `claude-sonnet-4-20250514`)
- `ELENCHUS_DATA` — directory for `.duckdb` files (default: `./dialectics`)
- `PORT` — server port (default: `8000`)

## Architecture

```
respondent ──→ server.py ──→ opponent.py ──→ Anthropic API
    ↑              ↓
    └── static/    ↓
        index.html dialectical_state.py
                       ↓
                   material_base.py
                       ↓
                   dialectics/*.duckdb
```

**Four modules, layered bottom-up:**

1. **material_base.py** — Definition 5: `B = ⟨L_B, |∼_B⟩`. DuckDB-backed atomic language and base consequence relation. Implements derivability via the Projection theorem (`_proof_search`). Utility functions `set_to_str`/`str_to_set`/`fmt_set` for serializing frozensets to comma-separated DuckDB strings.

2. **dialectical_state.py** — Definition 4: `S = ⟨[C : D], T, I⟩`. Wraps `MaterialBase` and adds DuckDB tables for positions (commitments/denials), tensions, and conversation history. The mapping: `L_B = C ∪ D`, `|∼_B = I ∪ Cont`.

3. **opponent.py** — The LLM oracle. Sends full formal state + windowed conversation history to Anthropic, expects structured JSON with `speech_acts`, `new_tensions`, and `response`. Applies state transitions via `_apply()`. Periodically generates conversation summaries (every 20 stored messages) to keep the context window manageable.

4. **server.py** — FastAPI app. Manages a cache of open `DialecticalState` instances (`_states` dict). REST API under `/api/dialectics/`. Serves `static/index.html` at root.

**static/index.html** — Single-file HTML/CSS/JS frontend (no build step). Communicates with the server via fetch calls to the API.

**elenchus_cli.py** — Standalone CLI REPL. Same `Opponent` + `DialecticalState` stack, no server needed. Supports slash commands (`/state`, `/tensions`, `/derive`, etc.).

## Key Domain Concepts

- **Bilateral position [C : D]** — C = commitments (accepted propositions), D = denials (rejected propositions)
- **Tension** — A proposed incoherence `{gamma} |~ {delta}` where gamma draws from C; stored with status open/accepted/contested
- **Material implication** — An accepted tension becomes an assessment in the base consequence relation
- **Speech acts** — COMMIT, DENY, RETRACT, REFINE, ACCEPT_TENSION, CONTEST_TENSION
- **Derivability** — Checked via Containment (premises ∩ conclusions non-empty) then Projection (subset search over base sequents)

## Persistence

Each dialectic is a single `.duckdb` file in `dialectics/`. DuckDB tables: `meta`, `atoms`, `assessments`, `positions`, `tensions`, `conversation`. Sets are serialized as sorted comma-separated strings. The `base_sequents` view computes the active consequence relation from assessments.
