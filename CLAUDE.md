# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Elenchus is a dialectical knowledge base construction system implementing the Elenchus protocol (Allen 2026). A human respondent develops a bilateral position [C : D] (commitments and denials) through Socratic dialogue with an LLM opponent. Accepted tensions become material implications in a NMMS material base satisfying Containment.

## Commands

```bash
# Install (editable, for development)
pip install -e ".[dev]"

# Bootstrap the first admin (one-time per install)
elenchus admin create --email admin@local --name "Admin"

# Run web server (serves API + static frontend)
elenchus                            # or: uvicorn elenchus.server:app --reload
elenchus serve --port 9000 --model claude-opus-4-6

# Migrate legacy single-user dialectics into the multi-user layout
elenchus migrate-legacy [--admin-email admin@local] [--create-admin]

# Cross-check platform DB ↔ filesystem
elenchus audit

# Run CLI REPL (in-memory) — bypasses platform DB
elenchus-cli --name "Topic"

# Run CLI REPL (persistent)
elenchus-cli --db my_inquiry.duckdb --name "Topic"
```

When starting the server, source `~/.zshrc` first to pick up `ANTHROPIC_API_KEY` and other env vars: `source ~/.zshrc 2>/dev/null; elenchus`

```bash
# Lint
ruff check .
ruff format --check .

# Run tests
pytest -v
```

## Environment Variables

- `ELENCHUS_API_KEY` — LLM API key (also accepts `ANTHROPIC_API_KEY`)
- `ELENCHUS_MODEL` — LLM model (default: `claude-opus-4-6`)
- `ELENCHUS_BASE_URL` — API base URL for OpenAI-compatible providers (e.g. `https://openrouter.ai/api/v1`)
- `ELENCHUS_PROTOCOL` — API protocol: `anthropic` or `openai` (auto-detected from base URL)
- `ELENCHUS_DATA` — directory for `.duckdb` files (default: `./dialectics`)
- `PORT` — server port (default: `8741`)
- `SESSION_COOKIE_SECURE` — set to `true` behind HTTPS in production
- `BCRYPT_ROUNDS` — bcrypt cost factor (default 12; tests use 4 for speed)
- `ELENCHUS_ADMIN_PASSWORD` — non-interactive password for `admin create`
- `ELENCHUS_ENABLE_PHASE_B` — opt in to the theory-articulation speech acts
  (`ASSERT_IMPLICATION` / `INTRODUCE_BEARER` / `RETRACT_IMPLICATION`).
  **Off by default** so the live message route matches the Sloan proposal's
  Elenchus-condition speech-act vocabulary exactly. Set to `1`/`true`/`yes`/
  `on` outside study contexts.

## Architecture

```text
src/elenchus/
├── server.py ──→ auth.py / invites.py        (Phase A platform layer)
│       ↓
│   db/registry.py  ──→  platform.duckdb       (actors, sessions, bases, invites)
│       ↓                bases/{actor_id}/*.duckdb
│   dialectical_state.py
│       ↓
│   material_base.py ──→ opponent.py ──→ LLM API
│       ↓
│   migrations/{platform,base}/*.sql
├── audit.py · backup.py · legacy.py           (operational tools)
├── email_service.py                            (invites + magic links)
├── static/index.html                           (React 18 + Babel, single file)
├── cli.py                                      (CLI REPL, bypasses platform DB)
└── pdf_report.py
```

**Layered bottom-up:**

1. **material_base.py** — Definition 5: `B = ⟨L_B, |∼_B⟩`. DuckDB-backed atomic language and base consequence relation. Derivability is delegated to pyNMMS (`NMMSReasoner`), which implements correct nonmonotonic proof search (no Weakening, no Cut) per Hlobil & Brandom 2025. An in-memory pyNMMS `MaterialBase` mirrors the DuckDB state, synced incrementally on `accept()`/`add_atoms()` and rebuilt from `base_sequents` after `reject()`. Utility functions `set_to_str`/`str_to_set`/`fmt_set` for serializing frozensets to DuckDB strings.

2. **dialectical_state.py** — Definition 4: `S = ⟨[C : D], T, I⟩`. Wraps `MaterialBase` and adds DuckDB tables for positions (commitments/denials), tensions, and conversation history. The mapping: `L_B = C ∪ D`, `|∼_B = I ∪ Cont`.

3. **opponent.py** — The LLM oracle. Sends full formal state + windowed conversation history to the LLM API (Anthropic or OpenAI-compatible via `_chat()` abstraction), expects structured JSON with `speech_acts`, `new_tensions`, and `response`. Applies state transitions via `_apply()`. Protocol auto-detected from `base_url` or set explicitly. Periodically generates conversation summaries (every 20 stored messages) to keep the context window manageable. Also generates analytical summaries for PDF reports via `generate_summary()`.

4. **db/registry.py** — Process-wide owner of all DuckDB connections. Holds the platform DB open for the lifetime of the server; per-base files are loaded lazily via a bounded LRU (`BaseHandle` per name, each with an `asyncio.Lock` for write serialization). `db_path(name)` resolves through `platform.bases` to `bases/{owner_id}/{name}.duckdb`. Single-writer-per-file is a hard constraint of DuckDB; the server runs as one process. Migration to Postgres swaps this module out.

5. **auth.py / invites.py / email_service.py** — Phase A platform layer. bcrypt password hashing, session tokens (`secrets.token_urlsafe`), magic-link login, invite issuance/consumption. `current_actor` and `require_admin` FastAPI dependencies gate every protected route. `EmailService` has Console (logs) and SMTP backends.

6. **server.py** — FastAPI app. Routes under `/api/auth`, `/api/admin`, `/api/dialectics`. Every `/api/dialectics/{name}/*` resolves through `_authorize_and_get_state(name, actor)`, which returns 404 (not 403) for non-owners to avoid leaking that a name exists.

7. **audit.py / backup.py / legacy.py** — Operational tooling. `backup.py` uses DuckDB `EXPORT DATABASE` under the platform lock and per-base MVCC. `audit.py` reports drift between platform DB, the filesystem, and per-base actor refs. `legacy.py` powers `elenchus migrate-legacy`.

8. **migrations/runner.py + migrations/{platform,base}/*.sql** — Numbered, forward-only SQL migrations with `-- version: N` headers. Platform migrations run at FastAPI lifespan startup; per-base migrations run on `MaterialBase.open` / `MaterialBase.create`. See `migrations/README.md` for the workflow.

9. **pdf_report.py** — Generates PDF reports of dialectics using fpdf2. Includes summary, bilateral position, tensions/implications, material base report, and conversation transcript. Converts Markdown formatting to HTML for rendering via `_md_to_html()`.

**static/index.html** — Single-file HTML/CSS/JS frontend (no build step). React 18 + Babel (in-browser transpilation). `<AuthGate>` wraps the app and swaps in Login / Signup / MagicLink forms on 401. An `<AuthContext>` exposes `actor` and `logout` to children. Admins see an ADMIN button in the home header that opens a two-tab dashboard (Invites + Users). Supports dark/light themes, font scaling, and custom colors (persisted in localStorage).

**cli.py** — Standalone CLI REPL. Bypasses the platform layer entirely: same `Opponent` + `DialecticalState` stack, no auth, no server needed. Supports slash commands (`/state`, `/tensions`, `/derive`, etc.).

## Key Domain Concepts

- **Bilateral position [C : D]** — C = commitments (accepted propositions), D = denials (rejected propositions)
- **Tension** — A proposed incoherence `{gamma} |~ {delta}` where gamma draws from C; stored with status open/accepted/contested
- **Material implication** — An accepted tension becomes an assessment in the base consequence relation
- **Speech acts** — COMMIT, DENY, RETRACT, REFINE, ACCEPT_TENSION, CONTEST_TENSION
- **Derivability** — Checked by pyNMMS's `NMMSReasoner`: backward proof search with Containment (Ax1), exact base consequence match (Ax2, no Weakening), and 8 Ketonen-style propositional rules. Returns a `ProofResult` with human-readable trace. Invoked on-demand via `/derive` (CLI and API), never automatically during the dialectic flow.

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

The data directory (`$ELENCHUS_DATA`, default `./dialectics/`) holds:

- `platform.duckdb` — `actors`, `auth_sessions`, `magic_links`, `invites`, `bases`, `sessions`, `platform_settings`, `meta` (schema version). Held open by the registry for the server's lifetime.
- `bases/{actor_id}/{name}.duckdb` — one per dialectic, owned by `actor_id`. Tables: `meta`, `atoms`, `assessments`, `positions`, `tensions`, `conversation`, `cases`. Sets are serialized as sorted comma-separated strings (with `\x1e` for new entries). The `base_sequents` view computes the active consequence relation from `current_assessments` (which filters on `status='active'`).
- `backups/elenchus-*.tar.gz` — `EXPORT DATABASE` snapshots, one tar per run.

Cross-DB integrity (per-base `contributor_id` / `actor_id` referencing `platform.actors`) is enforced at the application layer; DuckDB does not honor FKs across files. `elenchus audit` reports drift.

## Settings

LLM settings (model, API key, base URL) can be configured at runtime via `PUT /api/settings` or the settings modal in the UI. Non-secret settings (model, base_url) are persisted in localStorage and re-synced on server restart.

## Adding a Migration

Numbered SQL files under `src/elenchus/migrations/{platform,base}/`. Each must begin with `-- version: N`. The runner applies any version strictly greater than the current `meta.schema_version`, each in its own transaction. Forward-only — restore from backup to roll back. Use `ALTER TABLE ... ADD COLUMN ... DEFAULT ...` for backwards-compatible additions; the default backfills existing rows. After adding a migration, update any positional `INSERT INTO table VALUES (...)` to be column-explicit (otherwise the now-mismatched value count will break the route). Tests in `tests/test_migrations.py` lock in the schema's shape per version.
