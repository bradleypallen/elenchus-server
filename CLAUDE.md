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

When starting the server, source `~/.zshrc` first to pick up `ANTHROPIC_API_KEY` and other env vars: `source ~/.zshrc 2>/dev/null; python server.py`

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

**Five modules, layered bottom-up:**

1. **material_base.py** — Definition 5: `B = ⟨L_B, |∼_B⟩`. DuckDB-backed atomic language and base consequence relation. Implements derivability via the Projection theorem (`_proof_search`). Utility functions `set_to_str`/`str_to_set`/`fmt_set` for serializing frozensets to comma-separated DuckDB strings.

2. **dialectical_state.py** — Definition 4: `S = ⟨[C : D], T, I⟩`. Wraps `MaterialBase` and adds DuckDB tables for positions (commitments/denials), tensions, and conversation history. The mapping: `L_B = C ∪ D`, `|∼_B = I ∪ Cont`.

3. **opponent.py** — The LLM oracle. Sends full formal state + windowed conversation history to Anthropic, expects structured JSON with `speech_acts`, `new_tensions`, and `response`. Applies state transitions via `_apply()`. Periodically generates conversation summaries (every 20 stored messages) to keep the context window manageable. Also generates analytical summaries for PDF reports via `generate_summary()`.

4. **server.py** — FastAPI app. Manages a cache of open `DialecticalState` instances (`_states` dict). REST API under `/api/dialectics/`. Serves `static/index.html` at root.

5. **pdf_report.py** — Generates PDF reports of dialectics using fpdf2. Includes summary, bilateral position, tensions/implications, material base report, and conversation transcript. Converts Markdown formatting to HTML for rendering via `_md_to_html()`.

**static/index.html** — Single-file HTML/CSS/JS frontend (no build step). React 18 + Babel (in-browser transpilation). Communicates with the server via fetch calls to the API. Supports dark/light themes, font scaling, and custom colors (persisted in localStorage).

**elenchus_cli.py** — Standalone CLI REPL. Same `Opponent` + `DialecticalState` stack, no server needed. Supports slash commands (`/state`, `/tensions`, `/derive`, etc.).

## Key Domain Concepts

- **Bilateral position [C : D]** — C = commitments (accepted propositions), D = denials (rejected propositions)
- **Tension** — A proposed incoherence `{gamma} |~ {delta}` where gamma draws from C; stored with status open/accepted/contested
- **Material implication** — An accepted tension becomes an assessment in the base consequence relation
- **Speech acts** — COMMIT, DENY, RETRACT, REFINE, ACCEPT_TENSION, CONTEST_TENSION
- **Derivability** — Checked via Containment (premises ∩ conclusions non-empty) then Projection (subset search over base sequents)

## UI Action Flow (Two-Phase Pattern)

Accept, contest, and retract actions from the UI use a two-phase flow:

1. **Phase 1** — Direct API call (`POST /tensions/{tid}` or `/retract`) mutates state immediately. Columns update instantly.
2. **Phase 2** — Follow-up `POST /message` sends a natural-language description of the action to the opponent. The opponent responds conversationally (acknowledging the decision, discussing implications, potentially proposing new tensions).

The follow-up message includes the substance of the tension/proposition (not just the ID) so the opponent can engage meaningfully. An inline `[NOTE: ...]` is injected into the user content to prevent the opponent from saying "that's already been done" (since the state was updated before the message).

All interactive buttons (accept, contest, retract ×) are disabled while `loading` is true.

## LLM System Prompt Notes

The opponent system prompt in `opponent.py` includes:
- **UI-DRIVEN ACTIONS** section — instructs the LLM not to re-issue speech acts for actions already applied via UI, and to respond substantively rather than noting the state was already updated
- **PROPOSITION QUALITY** — clean, atomic, declarative sentences only; no metadata annotations
- **TENSION CONSTRUCTION** — gamma must be verbatim from C; delta should preferentially target propositions in D

## Persistence

Each dialectic is a single `.duckdb` file in `dialectics/`. DuckDB tables: `meta`, `atoms`, `assessments`, `positions`, `tensions`, `conversation`. Sets are serialized as sorted comma-separated strings. The `base_sequents` view computes the active consequence relation from assessments.

## Settings

LLM settings (model, API key, base URL) can be configured at runtime via `PUT /api/settings` or the settings modal in the UI. Non-secret settings (model, base_url) are persisted in localStorage and re-synced on server restart.
