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
- `ELENCHUS_SECRET_KEY` — master key (any long random string) used to
  encrypt the admin-set LLM API key at rest in `platform_settings` (see
  `secretbox.py`). Unset = the UI-set key is held in memory only and lost
  on restart; non-secret settings (model/endpoint) persist either way.
- `ELENCHUS_ENABLE_PHASE_B` — opt in to the theory-articulation speech acts
  (`ASSERT_IMPLICATION` / `INTRODUCE_BEARER` / `RETRACT_IMPLICATION`).
  **Off by default** so the live message route matches the Sloan proposal's
  Elenchus-condition speech-act vocabulary exactly. Set to `1`/`true`/`yes`/
  `on` outside study contexts.
- `ELENCHUS_TASK_MINUTES` — the **default** intended length of a study's
  main task (default `60`); a study's own *Length of the main task*
  (`study_configs.task_minutes`, Study tab) overrides it. Drives the writing pane's clock and its two **soft**
  warnings (ten minutes before, and at, the limit); nothing is cut off.
  Set low (e.g. `10`) for training runs and demos.
- `ELENCHUS_PRICING_JSON` — JSON object mapping model → `{input_per_1m,
  output_per_1m}` USD rates, or a list of such rates each with an
  `effective_from` date. Replaces that model's entries in `pricing.py`.
  Costs are priced at read time, so a corrected rate corrects history.
- `ALERT_EMAIL_TO` — recipient for the email alert channel. Unset = console-only.
- `ALERT_EMAIL_MIN_SEVERITY` — `critical|high|medium|low` (default `high`).
- `ALERT_DEDUP_MINUTES` — dedup window for repeated alerts (default `5`).
- `ELENCHUS_DAILY_SPEND_ALERT_USD` — a day's LLM spend that triggers an
  alert (default `25`; `0` = off). The Costs tab's setting overrides it.
- `ANTHROPIC_ADMIN_KEY` — read **only** by `elenchus-provider-report`, on
  an admin's own machine. Never set it on the server.

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
├── turn_log.py                                 (append-only research capture)
├── pricing.py · costs.py                       (dated price table; read-time cost report)
├── cost_ledger.py                              (admin-entered infrastructure charges)
├── cost_alerts.py                              (daily LLM spend alert)
├── provider_report.py                          (provider's own usage/cost: off-box fetch, upload, reconciliation)
├── email_service.py                            (invites + magic links)
├── static/index.html                           (React 18 + Babel, single file)
├── cli.py                                      (CLI REPL, bypasses platform DB)
└── pdf_report.py
```

**Layered bottom-up:**

1. **material_base.py** — Definition 5: `B = ⟨L_B, |∼_B⟩`. DuckDB-backed atomic language and base consequence relation. Derivability is delegated to pyNMMS (`NMMSReasoner`), which implements correct nonmonotonic proof search (no Weakening, no Cut) per Hlobil & Brandom 2025. An in-memory pyNMMS `MaterialBase` mirrors the DuckDB state, synced incrementally on `accept()`/`add_atoms()` and rebuilt from `base_sequents` after `reject()`. pyNMMS ≥0.6.2 only accepts identifiers or quoted atoms `<...>`, so natural-language atoms are quoted at that boundary only (`quote_atom` / `to_nmms_sentence` / `unquote_atoms`; `<`, `>`, `%` percent-escaped) — DuckDB always stores the plain sentence, and `derive_with_trace` returns an Elenchus `DerivationResult` with unquoted trace lines. Utility functions `set_to_str`/`str_to_set`/`fmt_set` for serializing frozensets to DuckDB strings.

2. **dialectical_state.py** — Definition 4: `S = ⟨[C : D], T, I⟩`. Wraps `MaterialBase` and adds DuckDB tables for positions (commitments/denials), tensions, and conversation history. The mapping: `L_B = C ∪ D`, `|∼_B = I ∪ Cont`.

3. **opponent.py** — The LLM oracle. Sends full formal state + windowed conversation history to the LLM API (Anthropic or OpenAI-compatible via `_chat()` abstraction), expects structured JSON with `speech_acts`, `new_tensions`, and `response`. Applies state transitions via `_apply()`. Protocol auto-detected from `base_url` or set explicitly. Periodically generates conversation summaries (every 20 stored messages) to keep the context window manageable. Also generates analytical summaries for PDF reports via `generate_summary()`.

4. **db/registry.py** — Process-wide owner of all DuckDB connections. Holds the platform DB open for the lifetime of the server; per-base files are loaded lazily via a bounded LRU (`BaseHandle` per name, each with an `asyncio.Lock` for write serialization). `db_path(name)` resolves through `platform.bases` to `bases/{owner_id}/{name}.duckdb`. Single-writer-per-file is a hard constraint of DuckDB; the server runs as one process. Migration to Postgres swaps this module out.

5. **auth.py / invites.py / email_service.py** — Phase A platform layer. bcrypt password hashing, session tokens (`secrets.token_urlsafe`), magic-link login, invite issuance/consumption. `current_actor` and `require_admin` FastAPI dependencies gate every protected route. **The platform emails only account holders** (never study participants), and the two public forms that can trigger mail — *forgot password?* and *email me a login link* — send only to an active, registered account, rate-limited (`auth.magic_link_recipient`, `auth.reset_rate_limited`), while answering identically either way: not revealing who is registered is the response's job and never a reason to email a stranger. Any new route that sends mail must keep that property — `deploy/ses-production-access.md` promises it to AWS. An admin can move a person between `admin` / `researcher` / `user` / `judge` (`PUT /api/admin/users/{id}/role`, the Users tab's kind menu) — never their own role, the last active admin, a platform identity, or a judge with assigned work (that would unblind their ratings); sessions resolve the actor row per request, so a role change needs no re-login. `EmailService` has Console (logs) and SMTP backends.

6. **server.py** — FastAPI app. Routes under `/api/auth`, `/api/admin`, `/api/sessions` (primary, session-keyed) and `/api/dialectics` (retained name-keyed alias for the study-participant flow + back-compat). Each `/api/sessions/{id}/*` route resolves `session_id → base` via `_resolve_session_base(id, actor)` and delegates to the name-keyed handler; both return 404 (not 403) for non-owners to avoid leaking that a name/session exists. See `docs/session-api-migration.md`.

7. **audit.py / backup.py / legacy.py** — Operational tooling. `backup.py` uses DuckDB `EXPORT DATABASE` under the platform lock and per-base MVCC. `audit.py` reports drift between platform DB, the filesystem, and per-base actor refs. `legacy.py` powers `elenchus migrate-legacy`.

8. **migrations/runner.py + migrations/{platform,base}/*.sql** — Numbered, forward-only SQL migrations with `-- version: N` headers. Platform migrations run at FastAPI lifespan startup; per-base migrations run on `MaterialBase.open` / `MaterialBase.create`. See `migrations/README.md` for the workflow.

9. **pdf_report.py** — Generates PDF reports of dialectics using fpdf2. Includes summary, bilateral position, tensions/implications, material base report, and conversation transcript. Converts Markdown formatting to HTML for rendering via `_md_to_html()`.

**static/index.html** — Single-file HTML/CSS/JS frontend (no build step). React 18 + Babel (in-browser transpilation). `<AuthGate>` wraps the app and swaps in Login / Signup / MagicLink forms on 401. An `<AuthContext>` exposes `actor` and `logout` to children. Admins see an ADMIN button in the home header that opens a six-tab dashboard (Invites + Users + Study + Judging + Costs + System); researchers see a STUDY button that opens the same dashboard with only the Study and Judging tabs (the ones that drive the researcher-gated study routes). There is no Settings tab — runtime LLM settings live in the gear-icon modal (`PUT /api/settings`). Supports dark/light themes, font scaling, and custom colors (persisted in localStorage).

**cli.py** — Standalone CLI REPL. Bypasses the platform layer entirely: same `Opponent` + `DialecticalState` stack, no auth, no server needed. Supports slash commands (`/state`, `/tensions`, `/derive`, etc.).

## Key Domain Concepts

- **Bilateral position [C : D]** — C = commitments (accepted propositions), D = denials (rejected propositions)
- **Tension** — A proposed incoherence `{gamma} |~ {delta}` where gamma draws from C; stored with status open/accepted/contested
- **Material implication** — An accepted tension becomes an assessment in the base consequence relation
- **Speech acts** — COMMIT, DENY, RETRACT, REFINE, ACCEPT_TENSION, CONTEST_TENSION
- **Derivability** — Checked by pyNMMS's `NMMSReasoner`: backward proof search with Containment (Ax1), exact base consequence match (Ax2, no Weakening), and 8 Ketonen-style propositional rules. Returns an Elenchus `DerivationResult` (`derivable`, `trace`, `depth_reached`, `cache_hits`) with a human-readable trace; a malformed query raises `QuerySyntaxError`. Invoked on-demand via `/derive` (CLI and API), never automatically during the dialectic flow.

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

- `platform.duckdb` — `actors`, `auth_sessions`, `magic_links`, `invites`, `bases`, `sessions`, `usage`, `platform_settings`, `meta` (schema version), plus the study tables. Held open by the registry for the server's lifetime.
- `bases/{actor_id}/{name}.duckdb` — one per dialectic, owned by `actor_id`. Tables: `meta`, `atoms`, `assessments`, `positions`, `tensions`, `conversation`, `cases`, plus the append-only capture tables `turn_log` and `state_events` (see Research Capture). Sets are serialized as sorted comma-separated strings (with `\x1e` for new entries). The `base_sequents` view computes the active consequence relation from `current_assessments` (which filters on `status='active'`).
- `backups/elenchus-*.tar.gz` — `EXPORT DATABASE` snapshots, one tar per run.

Cross-DB integrity (per-base `contributor_id` / `actor_id` referencing `platform.actors`) is enforced at the application layer; DuckDB does not honor FKs across files. `elenchus audit` reports drift.

## Research Capture

The study's formal analysis (NMMS, RDF translation) is done **offline from captured data**, so anything not recorded during a session is unrecoverable. `turn_log.py` owns two append-only per-base tables (migration `base/0003`):

- **`turn_log`** — one row per LLM exchange in either condition, *including failed calls* (`outcome='llm_error'`): the respondent's message, exactly what the LLM was shown (`request_content`, `state_before`), its verbatim output (`raw_text` — `conversation` only keeps the cleaned prose), which recovery path parsed it (`parse_strategy`), the parsed payload, `state_after`, the system prompt's name + SHA-256, and model / latency / tokens / attempts.
- **`state_events`** — one row per state transition or attempted transition, with `source` (`opponent` / `ui` / `direct`), `turn_id`, `actor_id`, and `outcome` (`applied` / `noop` / `dropped`). `positions` is an upsert and retractions carry no timestamp, so this is the only history of the position.

Events are written **inside the `DialecticalState` mutators**, not by their callers, so no code path can bypass capture. A new mutator must call `self._log_event(...)`; a new caller should pass `event=EventContext(...)` to say who is behind the change (the UI action routes pass `source="ui"`; the opponent passes `source="opponent"` with the turn id). A speech act that `_apply` drops (Phase B firewall, malformed) is logged there as `dropped`. Turn rows and their events are written inside the turn's transaction — a rolled-back turn leaves no log. Both tables are in the study export (`turn_log.json`, `state_events.json`, pseudonymized) and summarized under `capture` in the integrity report, where `uncaptured_assistant_turns` should be 0 for any session run after the migration.

## Partner Strip

`<PartnerStrip>` (sign-in `AuthShell`, home, participant `StudyShell` — never the working interface) renders `static/partners.json`: per partner `name`, `title`, `caption`, `url` (https), `logo` (a file in `static/logos/`, or `null` → shown as a text link), plus a top-level `funding` sentence (the Sloan acknowledgement; `link_text` + `url` link the funder's name). Universities are written out in full — never "UvA" / "VU" — and a lab's logo carries its university as the `caption`. Logos come **from the organisations, with permission** — never redraw or fetch one; `static/logos/README.md` has the rules. `tests/test_partners.py` fails on a named-but-missing logo, an orphan logo file, or a non-https link. `sw.js` deliberately skips `/static/partners.json` and `/static/logos/` so edits aren't pinned by the cache-first rule.

## Study Text (the judged artifact)

Each study participant writes a short prose introduction to a topic, in their own words, while working with the LLM; an expert panel rates **that text** (absolute ratings on coverage, correctness, concision, and whether the reasoning holds together). The LLM-generated structured report (`study_reports.py`) is no longer what judges see. Formal analysis (NMMS, RDF) is offline, from the capture log.

- **Topic** — `participant_session_tokens.topic_title` / `topic_brief` (platform migration `0009`). The task base is named after `topic_title`; `baseline_system_prompt(topic)` tells the baseline assistant.
- **Writing pane** — `<WritingPane>` in `static/index.html`, shown to participants in `tutorial` (practice base) and `active` (task base), same in both conditions. Autosaves via `PUT /api/study/session/text`; drafts are append-only `text_snapshots`, editor events (`paste` = length only, `soft_warning_shown`) are `editor_events` (base migration `0004`, `study_text.py`). The text routes are `async` and take the per-base lock so a save can't land inside an opponent turn's transaction.
- **Finish** — `POST /api/study/session/finish` stores a `submit` snapshot, writes the platform `study_texts` row (once per session) and moves `active → post_session`. The generic `advance` route refuses `post_session` without a submitted text.
- **Clock** — `_study_session_payload` returns `state_elapsed_seconds` (database clock both ends), `task_minutes` and `soft_warning_minutes`; the pane ticks locally from that anchor. Guidance only — there is no hard cutoff.

## Text Judging

The panel gives each submitted text **absolute** ratings (texts on different topics aren't comparable head to head, so there is no pairing). `text_judging.py` holds the versioned rubric — four dimensions (`coverage`, `correctness`, `concision`, `reasoning`), 1–7 — and `validate_ratings`; **bump `RUBRIC_VERSION` on any wording change**, it is stamped on every rating. `text_assignments` (one per text × judge, with a random `position` that orders that judge's queue) and `text_ratings` (every submission kept; `latest_text_rating` is the one that counts; `seconds_spent`) are platform migration `0011`. Researcher routes: `GET /api/admin/study/judges`, `GET /api/admin/study/{id}/texts` (unblinded metadata + progress, no content), `POST /api/admin/study/{id}/text-assignments` (default: every submitted text the judge doesn't have yet — idempotent). Judge routes: `GET /api/judge/rubric`, `GET /api/judge/texts`, `GET /api/judge/texts/{assignment_id}`, `POST …/rate`. **The judge view must never carry** the condition, participant code, period, session id or text id (ids are handed out in submission order) — `tests/test_text_judging.py::test_view_is_blinded` and the sim's `blinding_no_leak` probe guard this; the rubric block's guess options are the only place the condition vocabulary appears. Export: `text_judging.json` (unblinded analysis set). The paired-report tables/routes of migration `0006` (`judge_packages` …) are legacy: still tested, labelled as such in the UI, not used by the pilot or the sim.

## Enrolment (crossover design)

Each participant does two sessions — one per condition, a different topic each time. `study_configs` holds a study's two topics (A, B) and `min_gap_hours`; `study_participants` holds one row per *person* (code `P01…`, `first_condition`, `first_topic`, `allocation`); tokens carry `participant_id` + `period` (platform migration `0010`). `POST /api/admin/study/{id}/participants` allocates the cell and issues **both** links. Allocation is permuted-block randomization over the 2×2 of (first condition × first topic) — pure functions in `study_enrolment.py`; manually-placed participants are excluded from the blocks. **A token still owns its own passwordless actor** (the session's identity, owner of that session's bases); the participant row is the person's identity and is what links the two sessions in the export. `pdb.second_session_gate` keeps a period-2 link shut (HTTP 409 with a `user_message`) until the period-1 session is terminal and the gap has passed — checked only when a `scheduled` token is first opened, never on resume; a voided first token doesn't hold the second. `POST /api/admin/study/sessions/{id}/interrupt` is the researcher's way to close an abandoned session. Hand-issued tokens (`POST /api/admin/study/tokens`) still work and are never gated. The dashboard (`<AdminEnrolmentPanel>`) is reachable by `researcher` **and** `admin` accounts; researchers see only the Study and Judging tabs.

## Self-Serve Operation

The person running a study is an admin with **no shell on the server**, so anything they need day to day must be in the dashboard; when adding a feature, ask whether it leaves them needing SSH. What exists for that: a study's own task length (`study_configs.task_minutes`, platform migration `0015`; NULL = `ELENCHUS_TASK_MINUTES`; read at session time via `_task_minutes(study_id)`, changes logged with the old value); export **downloads** (`GET /api/admin/study/{id}/exports[/{name}]`, researcher; `…/{name}/pseudonyms`, **admin only**, logged at WARNING — a file is served only if `_study_exports(study_id)` lists it, the requested name is never joined into a path); the **System tab** (`GET /api/admin/system`: release, LLM/email configuration without secrets, disk, server time zone, backups with *Back up now*, the consistency check, and recent alerts); **stored alerts** (`alerting.DatabaseAlertChannel`, always on beside the console channel, platform migration `0016`, newest `ALERT_HISTORY_ROWS` kept — it must never raise and must work with or without the platform lock held); **email that never fails silently** (every template goes through `email_service.deliver`, which records the last outcome for the System tab and dispatches `email.send_failed` — a category `EmailAlertChannel` refuses to email, or it would loop; `invites.issue_invite_with_outcome` returns `emailed` True / False / None and the Invites tab says so); and `/healthz` `email_enabled`, which the sign-in page uses to say that reset/login links can't be emailed instead of pretending. What still needs the server: upgrades, restores, copying backups off the box, configuring SMTP, the clock. The runbook (`docs/study-runbook.md`) has an admin track and a practice run that needs nobody's help — **`tests/test_practice_run.py` walks that run step by step from a bare admin account, so change the two together**; `docs/judge-guide.md` is what gets sent to the panel — keep its rubric wording in step with `text_judging.py` (bump `RUBRIC_VERSION` there, then update the guide).

## Costs

**Tokens are the source of truth; dollars are computed when read.** Every server-side LLM call writes one `usage` row (platform migration `0002`; `purpose` from `0012`) through `opponent._make_usage_recorder(actor_id=, base_id=, purpose=)` — a new LLM call site **must** pass a recorder as `on_result` (or call one), with a `purpose` from `costs.PURPOSES` (`dialectic_turn`, `baseline_turn`, `rolling_summary`, `report_summary`, `study_report`, `sim_persona`); a call without one is spend nobody can see (the two summary calls and the sim personas were exactly that until 0.5). `usage.cost_usd` is the estimate at write time and **nothing reads it**: `costs.priced_groups` fetches usage grouped by day × model × purpose × category × actor × base and prices each group with `pricing.lookup_rate(model, day)`, and every figure — `costs.build_report` (the Costs tab, `GET /api/admin/costs`, `elenchus costs`), and the older `pdb.total_cost` / `daily_cost` / `cost_by_actor` / `usage_for_base` behind `/api/admin/usage` and the integrity report — is a rollup of those groups. `pricing.py` is a *dated* table (`Rate.effective_from`), matched on a normalized name (routing prefix dropped, `.` → `-`) by longest registered prefix; keep keys specific (`claude-opus-4-1`, not `claude-opus-4`) so a future model can't inherit an old sibling's rate, and bump `PRICES_AS_OF` when you check the table. A model with no rate is **unpriced, never free**: `lookup_rate` → None, the report lists it under `unpriced` and leaves its tokens out of every dollar figure. Study sessions are attributed through the token's own actor; calls on `practice-{session_id}` are the tutorial's share; a session is *finished* (its cost final, counted in the per-condition mean used for the projection) from `post_session` on. The budget is JSON under `platform_settings['cost_budget']` (`llm_usd` and/or `infra_usd` over one period; `PUT /api/admin/costs/budget`), display only. Admin-only.

**Infrastructure is recorded, not measured** (`cost_ledger.py`, platform migration `0013`, bottom of the Costs tab): an admin enters hosting / domain / email charges from invoices, because a provider-agnostic ledger survives a change of host or account and needs no billing credential on the box. Three kinds of thing, and **only the first is ever summed**: live `cost_entries` in an infrastructure category (`amount` + `currency` as invoiced, `amount_usd` as charged to the budget; a credit is negative; `estimated` = generated from a recurring item, not yet checked against an invoice); entries in category `llm_provider` — the provider's own figure for a `covers_month`, shown next to the computed LLM spend for reconciliation and **never added to a total** (that money is already counted from tokens); and `cost_recurring` items — a forecast that yields the run-rate, the budget projection (recorded + expected-but-unrecorded + upcoming, via `due_charges`), the unrecorded-month reminder and `record_recurring_month` (idempotent per recurring item × month). Entries are never deleted — `void_entry` keeps the row and excludes it everywhere; `update_entry` logs each field's old → new value, and that server log is the audit trail. Validation and its user-facing messages live in `cost_ledger.validate_*`; the routes take plain dicts and `_ledger_call` maps `ValueError` → 422 `user_message`, `LookupError` → 404. Timestamps are written as explicit naive UTC (`cost_ledger.now_utc`), not `CURRENT_TIMESTAMP`.

**Daily spend alert** (`cost_alerts.py`): the usage recorder calls `cost_alerts.check(con)` after every call that spent tokens (inside the platform lock, wrapped so a failure can never cost a turn). Today's spend is priced from tokens; crossing the threshold dispatches a HIGH alert and each further multiple a CRITICAL one, **each level in its own category** (`cost.daily_spend.x2`) so the dispatcher's (severity, category) dedup window can't swallow 2× because 1× just went out. What has fired today lives in `platform_settings['cost_alert_state']`, so a restart doesn't repeat it; unpriced tokens fire `cost.unpriced_model` once a day, because that spend is invisible to the check. Threshold: `platform_settings['cost_alert']` (`PUT /api/admin/costs/alert`) > `ELENCHUS_DAILY_SPEND_ALERT_USD` > $25; 0 = off. Alerting only — there is deliberately no hard cap.

**Provider reconciliation** (`provider_report.py`, platform migration `0014`): two halves on different machines. *Fetching* (`elenchus-provider-report`, the module's `main`) runs **off the box** with an Admin API key from `ANTHROPIC_ADMIN_KEY` — that key can manage the whole organization and must never reach the server — and calls Anthropic's `GET /v1/organizations/usage_report/messages` (grouped by model + workspace, `bucket_width=1d`, `limit=31`, paginated via `has_more` / `next_page`) and `GET /v1/organizations/cost_report` (grouped by description + workspace; `amount` is **cents as a decimal string**; it has **no API-key filter**, so clean dollars need a workspace of the platform's own). These endpoints are raw HTTP (httpx) — they are not in the Anthropic SDK — and the module must stay importable without pulling in `server` / the registry (a test enforces it). It writes a figures-only JSON file (`FORMAT`). *Importing* (`POST /api/admin/costs/provider-report`) validates the file and **replaces** `provider_usage_daily` rows for the days it covers (derived data, not a ledger), logging each upload in `provider_report_imports`. `reconciliation()` compares provider and platform **over the covered days only**, per model (`canonical_model` strips a trailing release date, never a version), with verdicts `agrees` / `rates_differ` / `provider_saw_more` / `platform_recorded_more`; an imported month wins over a typed-in `llm_provider` figure. The fetcher is tested against a mock transport shaped like the documented responses — it has not been run against the live Admin API.

## Settings

LLM settings (model, API key, base URL/endpoint, protocol) are configured at runtime by an **admin** via the gear-icon settings modal or `PUT /api/settings` (both gated by `require_admin`). They are **persisted server-side** in `platform_settings`: model/base_url/protocol in plaintext, the API key Fernet-encrypted via `secretbox.py` using the `ELENCHUS_SECRET_KEY` master key. At startup (`_apply_persisted_llm_settings` in the lifespan handler) persisted values are loaded and override the env-derived config (precedence: persisted > env). If `ELENCHUS_SECRET_KEY` is unset, a UI-set key is applied live but not persisted (lost on restart); `GET /api/settings` reports `persistence_available` / `key_persisted` and never returns the key value.

## Adding a Migration

Numbered SQL files under `src/elenchus/migrations/{platform,base}/`. Each must begin with `-- version: N`. The runner applies any version strictly greater than the current `meta.schema_version`, each in its own transaction. Forward-only — restore from backup to roll back. Use `ALTER TABLE ... ADD COLUMN ... DEFAULT ...` for backwards-compatible additions; the default backfills existing rows. After adding a migration, update any positional `INSERT INTO table VALUES (...)` to be column-explicit (otherwise the now-mismatched value count will break the route). Tests in `tests/test_migrations.py` lock in the schema's shape per version.
