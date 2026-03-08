# Architecture

This page describes the internal architecture of Elenchus for developers and contributors. If you just want to use Elenchus, see the [User Guide](guide.md).

## System Overview

```text
Browser / CLI
     │
     ▼
server.py ──→ opponent.py ──→ LLM API (Anthropic / OpenAI-compatible)
     │              │
     │              ▼
     │        _parse_response()
     │        _apply() speech acts
     │              │
     ▼              ▼
dialectical_state.py
     │
     ▼
material_base.py ──→ pyNMMS (NMMSReasoner)
     │
     ▼
DuckDB (.duckdb file)
```

A request flows top-down: user input arrives at the server (or CLI), is sent to the opponent along with the formal dialectical state, the opponent calls the LLM API, parses the structured JSON response, applies state transitions to the DuckDB-backed dialectical state, and returns the result.

## Modules

### material_base.py

**Definition 5**: `B = ⟨L_B, |∼_B⟩`

The foundation layer. A material base consists of an atomic language and a base consequence relation, both stored in DuckDB.

- **DuckDB tables**: `meta`, `atoms`, `assessments` (plus views `current_assessments` and `base_sequents`)
- **pyNMMS mirror**: An in-memory `MaterialBase` + `NMMSReasoner` mirrors the DuckDB state for derivability queries. Synced incrementally on `accept()` and `add_atoms()`, fully rebuilt from `base_sequents` after `reject()`.
- **Set serialization**: Sets of propositions are stored as `\x1e`-delimited sorted strings (ASCII Record Separator). Legacy data using comma delimiters is handled transparently on read.

Key methods: `add_atoms()`, `accept()`, `reject()`, `derives()`, `derive_with_trace()`

### dialectical_state.py

**Definition 4**: `S = ⟨[C : D], T, I⟩`

Wraps `MaterialBase` and adds DuckDB tables for the bilateral position, tensions, and conversation history.

The mapping to the material base (Definition 7):

- `L_B = C ∪ D` — the atomic language is the union of commitments and denials
- `|∼_B = I ∪ Cont` — the consequence relation is the accepted implications plus Containment

Key methods: `commit()`, `deny()`, `retract_prop()`, `add_tension()`, `accept_tension()`, `contest_tension()`, `to_dict()`

### opponent.py

The LLM oracle. Sends the formal state + windowed conversation history to the LLM and parses the structured response.

- **Protocol abstraction**: `_chat()` handles both Anthropic Messages API and OpenAI Chat Completions API. Protocol auto-detected from base URL hostname via `_detect_protocol()`.
- **Request construction**: Full formal state (always complete, always compact) + last N conversation turns + running summary of earlier discussion.
- **Response format**: Expects JSON with `speech_acts`, `new_tensions`, and `response`. Falls back to wrapping conversational text if JSON parsing fails.
- **State application**: `_apply()` processes each speech act (COMMIT, DENY, RETRACT, REFINE, ACCEPT_TENSION, CONTEST_TENSION) and registers new tensions.
- **Summarization**: Every 20 stored messages, generates a running summary to keep the context window manageable. Separate `generate_summary()` for PDF reports.

### server.py

FastAPI application. Manages a cache of open `DialecticalState` instances (`_states` dict keyed by dialectic name).

- REST API under `/api/dialectics/` (CRUD, message, tensions, retract, derive, report)
- Settings API (`GET/PUT /api/settings`) for runtime LLM configuration
- Serves `static/index.html` at root
- Each dialectic maps to a `.duckdb` file via `_db_path()` (name sanitized to alphanumeric + `-_`)

### pdf_report.py

Generates PDF reports using fpdf2. Includes LLM-generated analytical summary, bilateral position with atom IDs, sequent cards for tensions and implications, and the full conversation transcript.

- `_md_to_html()` converts Markdown to simple HTML for fpdf2's `write_html()`
- `_parse_assistant_content()` extracts the natural language response from raw LLM JSON stored in conversation history

### static/index.html

Single-file React 18 + Babel frontend (in-browser transpilation, no build step). Three-column layout: position [C : D] on the left, chat in the center, tensions and implications on the right.

- Communicates with the server via `fetch()` calls to the REST API
- Display preferences (theme, font scale, colors) persisted in `localStorage`
- LLM settings (model, base_url) persisted in `localStorage` and re-synced to the server on page load
- Active dialectic name persisted in `localStorage` for refresh recovery

### cli.py

Standalone REPL using the same `Opponent` + `DialecticalState` stack. Supports in-memory or persistent (DuckDB file) sessions. Slash commands for inspecting state (`/state`, `/tensions`, `/implications`, `/derive`, `/report`).

## DuckDB Schema

Each dialectic is a single `.duckdb` file containing all tables below. The schema is created by `MaterialBase.create()` and extended by `DialecticalState._ensure_tables()`.

### Tables

#### `meta`

Key-value store for dialectic metadata.

| Column | Type | Description |
|---|---|---|
| `key` | VARCHAR (PK) | Metadata key |
| `value` | VARCHAR | Metadata value |

Standard keys: `name` (topic name), `version` (schema version, currently `"5"`), `summary` (running conversation summary).

#### `atoms`

The atomic language `L_B` — all propositions that have appeared in the dialectic.

| Column | Type | Description |
|---|---|---|
| `sentence` | VARCHAR (PK) | The proposition text |
| `added_by` | VARCHAR | Who introduced it (`"respondent"`, `"oracle"`, `"system"`) |
| `added_at` | TIMESTAMP | When it was added |
| `description` | VARCHAR | Optional description |

#### `assessments`

The base consequence relation `|∼_B`. Each row is a judgment about whether a sequent holds.

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER | Auto-incrementing ID |
| `premises` | VARCHAR | Premise set (serialized) |
| `conclusions` | VARCHAR | Conclusion set (serialized) |
| `judgment` | VARCHAR | `"holds"` or `"rejected"` |
| `contributor` | VARCHAR | Who assessed it |
| `assessed_at` | TIMESTAMP | When assessed |
| `reason` | VARCHAR | Why (e.g., `"Tension #3: ..."`) |
| `domain` | VARCHAR | Category (e.g., `"tension"`) |

Sets in `premises` and `conclusions` are serialized as `\x1e`-delimited sorted strings. Example: `"alpha\x1ebeta\x1e"` represents `{"alpha", "beta"}`.

#### `positions`

The bilateral position `[C : D]`.

| Column | Type | Description |
|---|---|---|
| `atom` | VARCHAR | The proposition |
| `side` | VARCHAR | `"C"` (commitment) or `"D"` (denial) |
| `status` | VARCHAR | `"open"` or `"retracted"` |
| `introduced_at` | TIMESTAMP | When added to this side |

Primary key: `(atom, side)`.

#### `tensions`

Proposed incoherences in the position.

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER (PK) | Tension ID (sequential) |
| `gamma` | VARCHAR | Premise set (serialized) |
| `delta` | VARCHAR | Conclusion set (serialized) |
| `reason` | VARCHAR | Why this is a tension |
| `status` | VARCHAR | `"open"`, `"accepted"`, or `"contested"` |
| `proposed_at` | TIMESTAMP | When proposed |
| `resolved_at` | TIMESTAMP | When accepted or contested (null if open) |

#### `conversation`

Multi-turn dialogue history for oracle context.

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER (PK) | Sequential message ID |
| `role` | VARCHAR | `"user"` or `"assistant"` |
| `content` | TEXT | Message content (raw LLM JSON for assistant messages) |
| `created_at` | TIMESTAMP | When stored |

### Views

#### `current_assessments`

Most-recent assessment per (premises, conclusions, contributor). Uses `ROW_NUMBER()` partitioned by these three columns, ordered by `assessed_at DESC`. This implements most-recent-wins semantics — a later `"rejected"` overrides an earlier `"holds"`.

#### `base_sequents`

The active consequence relation. Selects from `current_assessments` where `judgment = 'holds'` and all contributors agree (unanimous consent). This is what gets loaded into the pyNMMS reasoner.

## API Protocol

### Two-Phase UI Action Flow

Accept, contest, and retract actions from the web UI use a two-phase pattern:

1. **Phase 1**: Direct API call (`POST /tensions/{tid}` or `POST /retract`) mutates state immediately. UI columns update instantly.
2. **Phase 2**: Follow-up `POST /message` sends a natural-language description of the action to the opponent. An inline `[NOTE: ...]` is injected into the user content to prevent the opponent from saying "that's already been done."

### LLM Request Construction

Each call to `opponent.respond()` builds a message sequence:

1. **Summary prefix** (if history > N turns): Earlier conversation condensed into a summary
2. **Windowed history**: Last N conversation turns (default 6 exchanges)
3. **Current message**: Formal state block + respondent's input + optional UI action note

The formal state block includes the complete `[C : D]`, all open/contested tensions, all material implications, and retracted propositions. It is always sent in full because it is compact.

### LLM Response Format

The opponent expects this JSON structure:

```json
{
  "speech_acts": [
    {"type": "COMMIT|DENY|RETRACT|REFINE|ACCEPT_TENSION|CONTEST_TENSION",
     "proposition": "...",
     "target_tension_id": null,
     "old_proposition": null}
  ],
  "new_tensions": [
    {"gamma": ["..."], "delta": ["..."], "reason": "..."}
  ],
  "response": "Natural language response."
}
```

If the LLM returns non-JSON, the response is wrapped as `{"speech_acts": [], "new_tensions": [], "response": "<raw text>"}`.

## Persistence and Recovery

### File-Based Persistence

Each dialectic is fully self-contained in a single `.duckdb` file. No external state, no filesystem dependencies beyond the file itself.

### Sequence Re-seeding

DuckDB sequences (`tension_seq`, `conv_seq`) don't survive disconnection. On every `DialecticalState._ensure_tables()` call (including reopen), sequences are dropped and recreated starting from `MAX(id) + 1` in the respective table.

### pyNMMS Reasoner Lifecycle

The in-memory pyNMMS reasoner is lazily built on the first derivability query:

- **Incremental sync**: `accept()` and `add_atoms()` update the in-memory base directly, then invalidate the reasoner (forcing rebuild on next query)
- **Full rebuild**: `reject()` invalidates both the base and reasoner, triggering a full rebuild from `base_sequents` on next query
- **Cold start**: After reopening a `.duckdb` file, the reasoner is `None` and rebuilds from scratch on first use
