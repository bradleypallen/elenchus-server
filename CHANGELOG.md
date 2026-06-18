# Changelog

All notable changes to Elenchus are documented here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Documentation

- New task-oriented guides on the docs site: **Administration** (roles,
  the four-tab admin dashboard, invites/accounts, users, cost/usage,
  audit, backups, alerting), **Running a Study** (conditions, participant
  flow, questionnaires, structured reports, blinded judging, export), and
  **Deployment** (local / production VM / cloud). MkDocs nav restructured;
  previously orphaned ops/study docs surfaced.
- Deploy docs and the AWS PoC scaffold install `elenchus>=0.2.0` from PyPI.

### Added

- `release.yml` GitHub Actions workflow: tag push (`v*.*.*`) builds,
  `twine check`s, publishes to PyPI via OIDC trusted publishing, and
  creates a GitHub Release with the artifacts.
- Admin-managed, persisted LLM settings. An admin can set the model, API
  endpoint (base URL), protocol, and API key from the gear-icon Settings
  modal (`PUT /api/settings`), and they survive restarts: non-secret
  values are stored in `platform_settings`, the API key **encrypted at
  rest** (Fernet) via a new `secretbox` module keyed by
  `ELENCHUS_SECRET_KEY`. Persisted values override the environment at
  boot. New dependency: `cryptography`.

### Security

- `GET`/`PUT /api/settings` are now gated by `require_admin` (previously
  unauthenticated — any caller could change the model/key/endpoint). The
  endpoint never returns the key value; the modal is admin-only in the UI.

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
