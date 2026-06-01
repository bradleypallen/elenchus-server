# Changelog

All notable changes to Elenchus are documented here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.2.0] — Phase A: Multi-User Platform Foundation

The single-user install becomes a multi-user platform with
authentication, invite-only signup, per-actor data scoping, an admin
dashboard, and operational tooling for backup and audit. Backwards-
compatible upgrade path via `elenchus migrate-legacy`.

### Added

- **Auth**: bcrypt password hashing, HTTP-only SameSite=Lax session
  cookies (30-day TTL), magic-link login, `/api/auth/{login,logout,
  signup,change-password,magic-link,magic/{token},me}` routes.
- **Platform DB**: `platform.duckdb` carrying `actors`,
  `auth_sessions`, `magic_links`, `invites`, `bases`, `sessions`,
  `platform_settings`. Held open by `DBRegistry` for the server's
  lifetime.
- **Invite-only signup**: admins issue invites with role + optional
  recipient email, signup consumes the token and creates the actor
  in one atomic step. Magic-link tokens are single-use, atomically
  consumed in SQL.
- **Per-actor data scoping**: dialectic files now live at
  `bases/{actor_id}/{name}.duckdb`. Non-owners get 404 (not 403) on
  cross-actor URL manipulation.
- **Admin dashboard**: in-browser two-tab view for issuing/revoking
  invites and listing users; `<AuthGate>` shell with Login / Signup /
  MagicLink forms swaps in on 401.
- **Migrations**: numbered SQL migrations under
  `src/elenchus/migrations/{platform,base}/` with a forward-only
  runner. v2 base migration adds `contributor_id`, `paraphrases`,
  `references` (atoms), `provenance`, `status` (assessments),
  `actor_id`, `case_id` (positions), `case_id` (tensions),
  `session_id` / `case_id` / `actor_id` (conversation), plus a new
  `cases` table. Future-proofs the schema for multi-respondent
  features without a refactor.
- **Backup**: `POST /api/admin/backup` runs DuckDB `EXPORT DATABASE`
  on the platform DB and every registered base into a timestamped
  tar.gz; `scripts/backup.py` is the cron-side entry point. Retention
  prunes archives down to N newest (default 14).
- **Audit**: `elenchus audit` and `GET /api/admin/audit` report drift
  between `platform.bases` and the filesystem and surface per-base
  contributor/actor refs that point at non-existent actors.
- **Actor lifecycle**: `PUT /api/admin/users/{id}/{deactivate,
  reactivate}`. Deactivation revokes outstanding session cookies in
  the same transaction. Guards: cannot deactivate yourself; cannot
  deactivate the last active admin.
- **CLI**: `elenchus admin create` (bootstrap),
  `elenchus migrate-legacy` (single-user → multi-user file move),
  `elenchus audit` (drift report).
- **Documentation**: README sections for multi-user setup and
  operations; CLAUDE.md updated for the new module layout;
  `migrations/README.md` covers the runner contract and gotchas.

### Changed

- All `/api/dialectics/*` routes now require authentication.
- The `list_dialectics` admin path queries `platform.bases` (canonical)
  with a flat-layout glob fallback for unmigrated legacy files.
- Positional `INSERT INTO {atoms,positions,tensions} VALUES (...)`
  statements switched to column-explicit form so future schema
  additions don't break them.
- FastAPI lifespan startup runs platform migrations before the first
  request is accepted; the LLM message route is `async def` and uses
  `AsyncAnthropic` / `AsyncOpenAI` with a per-base `asyncio.Lock`
  serializing the apply phase.

### Tests

- 122 → 324 tests. New suites cover auth, invites, platform DB,
  authorization (cross-actor isolation), per-base schema extensions,
  legacy migration, backup + retention, audit, and
  deactivation/reactivation.

## [0.1.1]

Initial single-user PyPI release.
