# Changelog

All notable changes to Elenchus are documented here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- **Participant enrolment for the crossover design.** A researcher sets a
  study up once — its two topics and the minimum gap between a
  participant's sessions (`PUT /api/admin/study/{id}/config`) — then
  enrols a *person* (`POST /api/admin/study/{id}/participants`), which
  issues **both** of their session links in one step, each carrying its
  condition and topic. Which condition and which topic come first is drawn
  by **permuted-block randomization** over the four order × topic cells,
  so the cells stay balanced throughout recruitment and the next
  allocation isn't predictable; a replacement can be placed by hand, and
  sits outside the blocks. Each participant gets a code (`P01`, …) that
  links their two sessions in the export (`participant_code`, `period`,
  allocation in `session.json`; a name-free `participants.json` and
  `study_config.json`; names only in the pseudonym file kept beside the
  archive). A participant's **second link stays shut** until their first
  session has ended and the study's gap has passed, with a message saying
  when it opens; resuming a session is never blocked. Researchers can
  close an abandoned session as `interrupted`
  (`POST /api/admin/study/sessions/{id}/interrupt`) so it doesn't hold the
  second one up. The Study tab gains study setup, one-click enrolment and
  a roster (both sessions per person, status, text submitted, copy-link,
  balance across cells). Platform migration `0010`; export format `2`.
- **Writing pane: the participant's own text is now the study's judged
  artifact.** Each participant token can carry a topic (`topic_title`,
  `topic_brief`); the task base is named after it, so the Elenchus
  opponent sees it as the dialectic's topic, and the baseline assistant is
  told it in its system prompt. During the tutorial and the main task a
  pane beside the dialogue — identical in both conditions — shows the
  topic, the brief and the standing task instruction, and holds an editor
  that autosaves. Every distinct draft is kept as a timestamped snapshot
  (`text_snapshots`, migration `base/0004`); pastes are logged by **length
  and time only**, never content, along with soft-timer warnings shown
  (`editor_events`). An elapsed clock anchored to the server gives **soft**
  time guidance — a warning ten minutes before `ELENCHUS_TASK_MINUTES`
  (default 60) and again when it is up; nothing is cut off. FINISH SESSION
  now submits the text (`POST /api/study/session/finish`, stored in
  `study_texts`, migration `platform/0009`, written once) and the task
  cannot be left without one. The study export gains `text.json`,
  `text_snapshots.json` and `editor_events.json` per session, and the
  session's topic. The pilot simulation writes, pastes, probes the
  no-text guard and finishes.

- **Research capture log.** Two append-only per-base tables (migration
  `base/0003`) record what the live tables don't keep, so formal analysis
  can be done offline from captured data alone. `turn_log` has one row per
  LLM exchange in either study condition — the respondent's message,
  exactly what the LLM was shown, its **verbatim output** (the transcript
  only keeps the cleaned prose), which parse-recovery path was used, the
  parsed payload, the dialectical state before and after, the system
  prompt's name and SHA-256, and model / latency / tokens — and also
  records exchanges whose LLM call **failed**, which previously left no
  trace. `state_events` has one row per state transition (commit, deny,
  retract, refine, tension proposed / accepted / contested, Phase B acts)
  with its source (`opponent`, `ui` button, or `direct`), the turn that
  caused it, the prior position it overwrote, and whether it was applied,
  a no-op, or dropped (e.g. by the Phase B firewall). Events are written
  inside the `DialecticalState` mutators, so the opponent, the UI action
  routes, the CLI and the scripts are all captured. Both tables are in the
  study export (`turn_log.json`, `state_events.json`, actor ids
  pseudonymized) and summarized under `capture` in the integrity report.

### Changed

- Minimum pyNMMS is now 0.6.2 (the first release with quoted atoms).
- `MaterialBase.derive_with_trace` returns an Elenchus `DerivationResult`
  (`derivable`, `trace`, `depth_reached`, `cache_hits`) rather than
  pyNMMS's `ProofResult`, whose `trace` changed from a field to a
  read-only property across the supported pyNMMS range.
- The baseline condition's system prompt describes the actual task (the
  expert is writing a short introduction in an editor the assistant can't
  see) instead of calling the conversation transcript the deliverable.
  **Study-design wording — review before the pilot.** The Elenchus
  opponent's prompt is unchanged.
- Participant-facing copy (briefing, post-task screen) rewritten for the
  writing task; baseline participants no longer see Elenchus wording
  ("Opponent is considering…", "commit, deny, respond to tensions…") in
  the chat box.
- `GET /api/study/session` and the routes that return a session no longer
  include the participant's token; the export's `session.json` omits it
  too.

### Fixed

- **Researcher accounts could not reach the Study or Judging tabs.** The
  dashboard button and view were gated on `admin`, although the study
  routes themselves have always allowed researchers. Researchers now get
  a STUDY button and a dashboard showing the Study and Judging tabs only.
- Derivability checks work again with pyNMMS ≥ 0.6.2. That release made
  the atom grammar strict (identifiers, `C(a)`, or quoted `<...>` only),
  so building the reasoner raised `ValueError` for every real dialectic —
  whose atoms are natural-language sentences — and `POST …/derive` and the
  CLI `/derive` failed (HTTP 500). Only on-demand derivability was
  affected; the dialectic flow never builds the reasoner. `material_base.py`
  now quotes every atom as `<...>` at the pyNMMS boundary (percent-escaping
  `<`, `>` and `%`, so propositions like "PaO2/FiO2 < 300 mmHg" are safe)
  and unquotes proof traces on the way back. DuckDB still stores the plain
  sentence: no schema or data change.
- `/derive` query sentences may mix pyNMMS connectives (`~ & | ->`,
  parentheses) with natural-language propositions: a sentence that is
  verbatim a known atom is that atom, `<...>` quotes a proposition
  verbatim, and any other run of text between connectives is a
  proposition — so identifier-style queries (`A -> B`) behave as before. A
  stored proposition that itself contains syntax characters must be quoted
  inside a larger sentence; leaving it bare is reported as an error rather
  than silently parsed as syntax.
- A malformed `/derive` query now returns HTTP 422 with an explanatory
  message (and the CLI prints it) instead of a 500 / REPL crash — including
  a query nested deeply enough to exhaust the parser's recursion (sentences
  are capped at 2000 characters and 100 negations/parentheses). Malformed
  queries raise a dedicated `QuerySyntaxError`, and only that is reported
  as the caller's mistake: a `ValueError` from inside pyNMMS is a server
  fault and surfaces as a 5xx, rather than masquerading as a bad query.
  Every rejection is logged.
- Query sentences are handed to pyNMMS in its own canonical form. pyNMMS
  0.6.2 compares sentences as strings, so a redundant pair of parentheses
  (`(A) |~ A`) answered False there while answering True on later releases.
- A padded quote (`< A >`) resolves to the known atom `A` instead of
  silently becoming a different, unknown atom.
- The service worker served the HTML shell cache-first, so a browser that
  had visited before kept running the **old frontend after a server
  upgrade**. The shell is now network-first (cache only as the offline
  fallback); `CACHE_NAME` bumped to `elenchus-v3`.
- A reply arriving no longer pulls keyboard focus out of the writing pane.
- An opponent reply that was valid JSON but not an object (a bare string
  or list) no longer crashes the turn; it is treated as prose like any
  other unparseable reply.


## [0.3.3] — 2026-06-21

### Fixed

- The opponent's reply bubble no longer goes missing until reload. When a
  malformed envelope put its defect inside `new_tensions`, the json-repair
  recovery (0.3.2) salvaged the tension but dropped the trailing `response`
  prose — so the message route returned an empty `response`, the live UI
  appended nothing, and the reply only showed up on refresh (where the
  frontend re-derives it from the stored payload). The parser now salvages
  the `response` field directly when the structured parse loses it, so the
  route, storage, PDF, and live UI all agree. As a safety net, the frontend
  also shows a short placeholder when a turn returns only tensions/speech
  acts with no prose, instead of rendering nothing.

## [0.3.2] — 2026-06-21

### Fixed

- Opponent turns no longer silently drop their proposed tensions
  (sequents). The model frequently emits JSON with an unescaped `"` inside
  a long natural-language string value, which neither strict nor lenient
  `json.loads` can parse — so the turn's `new_tensions` were lost and no
  sequents appeared in the dialectic. Added a `json-repair` recovery layer
  after the strict-parse and brace-walk attempts; verified against real
  failing payloads that it recovers the structured envelope with tensions
  intact. New dependency: `json-repair`.

## [0.3.1] — 2026-06-19

### Fixed

- README links are now absolute (GitHub URLs) so they resolve on the PyPI
  project page and the docs site, not only on GitHub. In particular the
  MIT license badge no longer 404s on PyPI.

## [0.3.0] — 2026-06-19

Copyright holder reassigned to the **University of Amsterdam** (author
unchanged). Adds admin-managed persisted LLM settings and a full password-
reset flow; fixes the JSON-into-transcript and broken-invite-link bugs.

### Documentation

- New task-oriented guides on the docs site: **Administration** (roles,
  the four-tab admin dashboard, invites/accounts, users, cost/usage,
  audit, backups, alerting), **Running a Study** (conditions, participant
  flow, questionnaires, structured reports, blinded judging, export), and
  **Deployment** (local / production VM / cloud). MkDocs nav restructured;
  previously orphaned ops/study docs surfaced.
- Deploy docs and the AWS PoC scaffold install `elenchus>=0.2.0` from PyPI.

### Added

- **Password reset** (admin- and user-initiated), built to standard
  best practice. Self-service "Forgot password?" sends a one-time link
  (60-min TTL, rate-limited, no account enumeration); admin "Reset
  password" logs the user out and returns/emails a 24-h link. Reset
  tokens are stored only as SHA-256 hashes; completing a reset revokes all
  of the actor's sessions. New `must_change_password` flag forces a new
  password at next login, surfaced as "Reactivate + require new password"
  in the Users tab. Migration `0008_password_resets`. New password minimum
  of 10 chars on the reset/set paths. (Self-service delivery needs SMTP;
  admin resets work without it via the returned link.)
- `release.yml` GitHub Actions workflow: tag push (`v*.*.*`) builds,
  `twine check`s, publishes to PyPI via OIDC trusted publishing, and
  creates a GitHub Release with the artifacts.
- Participant **resume link**. The study token link now doubles as a
  resume link: re-clicking it while the participant's session is still
  live (non-terminal) re-issues a session cookie and routes them back to
  their current step — so a participant can pause and resume from another
  device or after losing their cookie, which the passwordless model
  otherwise made impossible. `POST /api/study/{token}` returns the live
  session with `resumed: true` instead of `410`; it still returns `410`
  once the session is terminal, or the token is voided/expired/out-of-window.
- Admin-managed, persisted LLM settings. An admin can set the model, API
  endpoint (base URL), protocol, and API key from the gear-icon Settings
  modal (`PUT /api/settings`), and they survive restarts: non-secret
  values are stored in `platform_settings`, the API key **encrypted at
  rest** (Fernet) via a new `secretbox` module keyed by
  `ELENCHUS_SECRET_KEY`. Persisted values override the environment at
  boot. New dependency: `cryptography`.

### Changed

- **Copyright holder reassigned to the University of Amsterdam** (LICENSE);
  the docs-site footer and a new README License section state it too.
  Bradley P. Allen remains the author (`pyproject` `authors`).
- Clearer auth screens. Each (sign-in / set-up-account / magic-link) now
  shows a heading saying which it is; arriving via an invite link hides
  the raw token field and frames it as "set up your account" (pick a
  display name + password). The admin Invites tab spells out what the
  recipient does and that they sign in afterwards with email + password.

### Security

- `GET`/`PUT /api/settings` are now gated by `require_admin` (previously
  unauthenticated — any caller could change the model/key/endpoint). The
  endpoint never returns the key value; the modal is admin-only in the UI.

### Fixed

- Raw JSON no longer leaks into the dialogue. The opponent's response
  parser now uses `json.loads(strict=False)`, tolerating the literal
  newlines models emit inside a multi-paragraph `response` (strict JSON
  rejected them, dumping the whole `{speech_acts, new_tensions, response}`
  envelope into the transcript). The server now also stores the clean
  `response` prose — not the raw JSON — so reloads, the PDF, and summaries
  read cleanly without re-parsing; and the frontend salvages the
  `response` field from any envelope already stored.
- Emailed invite link now targets the SPA root (`/?token=`) instead of a
  non-existent `/signup` route, so the link in the invitation resolves.

## [0.2.0] — Multi-user platform, operational tooling, and the study harness

The single-user install becomes a multi-user **platform** with
authentication, invite-only signup, and per-actor data scoping; gains the
**operational tooling** to run it in production (cost tracking, alerting,
integrity/audit, backups, health); and ships the complete **Sloan study
harness** (participant flow, two conditions, questionnaires, structured
reports, blinded judging, pseudonymized export). Backwards-compatible
upgrade path via `elenchus migrate-legacy`. 122 → **734 tests**.

> The Sloan study's Elenchus condition uses the speech-act vocabulary
> `{COMMIT, DENY, ACCEPT_TENSION, CONTEST_TENSION, RETRACT, REFINE}` only.
> The Phase B theory-articulation acts (`ASSERT_IMPLICATION`,
> `INTRODUCE_BEARER`, `RETRACT_IMPLICATION`) are **firewalled off by
> default** and require `ELENCHUS_ENABLE_PHASE_B=1` to enable.

### Platform & auth

- **Auth**: bcrypt password hashing, HTTP-only SameSite=Lax session
  cookies (30-day TTL), magic-link login, `/api/auth/{login,logout,
  signup,change-password,magic-link,magic/{token},me}` routes.
- **Platform DB**: `platform.duckdb` carrying `actors`, `auth_sessions`,
  `magic_links`, `invites`, `bases`, `sessions`, `platform_settings`,
  held open by `DBRegistry` for the server's lifetime.
- **Invite-only signup**: admins issue invites with role + optional
  recipient email; signup consumes the token and creates the actor in one
  atomic step. Magic-link tokens are single-use, atomically consumed.
- **Per-actor data scoping**: dialectic files live at
  `bases/{actor_id}/{name}.duckdb`; non-owners get 404 (not 403) on
  cross-actor URL manipulation.
- **Admin dashboard**: a four-tab in-browser view (Invites, Users, Study,
  Judging); `<AuthGate>` shell swaps in Login / Signup / MagicLink on 401.
- **Actor lifecycle**: `PUT /api/admin/users/{id}/{deactivate,reactivate}`
  (revokes sessions in the same transaction; cannot deactivate yourself or
  the last active admin).
- **Migrations**: numbered, forward-only SQL migrations under
  `src/elenchus/migrations/{platform,base}/` with a runner; the base v2
  migration future-proofs the schema (contributor/actor/case scoping,
  provenance, a `cases` table) for multi-respondent features.
- **Session-keyed API**: `/api/sessions/{id}/*` as the primary surface,
  with `/api/dialectics/{name}` retained as a thin alias.

### Operations (Phase C)

- **Cost tracking**: every LLM call recorded (model, tokens, latency,
  status, cost); `GET /api/admin/usage` rollup; per-model rates in
  `pricing.py`, overridable via `ELENCHUS_PRICING_JSON`.
- **Alerting**: console + optional email channels with severity filtering
  and dedup (`ALERT_EMAIL_TO`, `ALERT_EMAIL_MIN_SEVERITY`,
  `ALERT_DEDUP_MINUTES`).
- **Integrity & audit**: `GET /api/admin/integrity[/{base_id}]` and
  `elenchus audit` / `GET /api/admin/audit` report per-base content and
  platform↔filesystem drift.
- **Backup**: `POST /api/admin/backup` (`EXPORT DATABASE`, MVCC-safe,
  timestamped tar.gz, retention) + `scripts/backup.py` cron entry point.
- **Health**: unauthenticated `GET /healthz` surfacing `llm_configured`
  and `phase_b_enabled` for uptime monitors.
- **Resilient LLM client**: error classification + retry, with graceful
  failure surfaced in the UI.

### Study harness (Phase D)

- **Participant tokens**: single-use, passwordless study links
  (`POST /api/admin/study/tokens` → `POST /api/study/{token}`), scoped to
  a study + condition with an optional scheduling window.
- **Session state machine**: briefing → tutorial → active → post_session
  → surveyed → complete (with expired/interrupted), routed server-side so
  the flow is safe on a shared machine.
- **Two conditions**: `elenchus` (Socratic opponent with tensions/speech
  acts) vs `baseline` (plain assistant chat), enforced at message time.
- **Questionnaires**: NASA-TLX, SUS, TIAS, and the custom EEQ, strictly
  validated and version-stamped (`INSTRUMENT_VERSION`).
- **Structured reports**: a condition-agnostic LLM report per session
  (Domain / Atomic statements / Implications / Notes).
- **Blinded judging**: matched report pairs in randomized A/B slots,
  multi-judge assignment, five rating dimensions plus a condition-guess
  blinding check.
- **Per-study export**: analysis-ready pseudonymized archive with the
  identity (pseudonym) map written **separately**, never inside it.
- **Simulation harness**: `elenchus sim` drives the full study flow
  (scripted or LLM personas) including the access/auth probes.

### Changed

- All `/api/dialectics/*` routes now require authentication.
- The LLM message route is `async def` (AsyncAnthropic / AsyncOpenAI) with
  a per-base `asyncio.Lock` serializing the apply phase; platform
  migrations run at FastAPI lifespan startup.
- The default `SLOAN_SYSTEM_PROMPT` omits the Phase B acts; the Phase B
  prompt is used only when `ELENCHUS_ENABLE_PHASE_B` is set.

### Fixed

- Wheel build: removed a redundant `force-include` that double-added
  `migrations/` files and aborted every hatchling wheel build — the
  reason PyPI had been stranded at 0.1.1.

### Tests

- 122 → **734** passing. New suites cover auth, invites, platform DB,
  cross-actor authorization, per-base schema, legacy migration, backup +
  retention, audit, deactivation, the Phase B firewall, cost / alerting,
  the study state machine, questionnaires, judging, and export.

## [0.1.1]

Initial single-user PyPI release.
