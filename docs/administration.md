# Administration

This guide is for the **platform administrator** — the person who runs an
Elenchus server, creates accounts, and keeps it healthy. For deploying the
server in the first place, see [Deployment](deployment.md) and the
[Operations Runbook](OPERATIONS.md); for running the human-subjects study,
see [Running a Study](study.md).

## Roles

Every account (an *actor*) has a `kind` that determines what it can do.
Routes are gated by role, and a higher role inherits the powers of the
lower study roles it supervises.

| Kind | Can do | Gating |
|---|---|---|
| `admin` | Everything: issue invites, manage users, back up, audit, plus all researcher powers | `require_admin` |
| `researcher` | Run studies: issue participant tokens, assemble judge packages, export study data | `require_researcher` (admin or researcher) |
| `user` | Create and work in their own dialectics | authenticated |
| `judge` | See assigned, blinded report pairs and rate them | `require_judge` (admin or judge) |
| `participant` | Passwordless study participant; the study token is the only credential | token only |
| `opponent_llm`, `system` | Internal actors used for attribution | — |

Non-owners get **404** (not 403) when addressing another actor's session or
base, so the API never leaks that a name exists.

## Bootstrap the first admin

One-time, before the first launch (the server holds `platform.duckdb`
open, so the admin must be created while the server is stopped):

```bash
elenchus admin create --email admin@local --name "Study Admin"
```

It prompts for a password, or reads `ELENCHUS_ADMIN_PASSWORD` for a
non-interactive create. The command is idempotent — re-running for an
existing email offers to reset the password.

Migrating a pre-0.2 single-user install? Run `elenchus migrate-legacy
--create-admin` once to relocate flat-layout dialectics into
`bases/{actor_id}/…` and register them under the admin.

## The admin dashboard

Admins see an **ADMIN** button in the home header. It opens a dashboard
with four tabs:

- **Invites** — issue an invite (pick a role, optionally pin it to an
  email), list outstanding/consumed/expired invites, and revoke unused
  ones.
- **Users** — list every actor with id, kind, email, display name, and
  active/deactivated status (admins marked ★).
- **Study** — issue participant tokens and watch a cohort: tokens by
  study, their status, session links, report generation, and per-study
  export. (See [Running a Study](study.md).)
- **Judging** — assemble blinded report pairs and assign them to judges.

The Study and Judging tabs drive researcher-gated routes; they live in the
admin dashboard so a sole admin can run a pilot end to end.

## Accounts and invites

Signup is **invite-only** by default (`platform_settings.signup_mode =
invite_only`).

1. An admin issues an invite (Invites tab, or `POST /api/admin/invites`)
   choosing the new account's role. If an email is given and an SMTP
   backend is configured, the invite link is emailed; otherwise the token
   is returned for you to share.
2. The recipient opens `/?token=<token>`, sets a display name and
   password, and the invite is consumed atomically (`POST
   /api/auth/signup`). Invites are single-use and expire after 30 days.

Passwords are bcrypt-hashed (`BCRYPT_ROUNDS`, default 12 — never lower it
in production). Sessions are cookie tokens with a 30-day TTL; changing a
password or deactivating an actor revokes all of that actor's sessions.
**Magic links** (passwordless email login, 20-minute TTL) are available
via `POST /api/auth/magic-link`.

> Changing `signup_mode` away from `invite_only` is a direct
> `platform_settings` edit — there is intentionally no UI or API to open
> public signup.

## Managing users

User deactivation is a **soft delete** (`actors.deactivated_at`): the
account can no longer log in and its sessions stop working immediately,
but its past contributions stay attributed. The server refuses to
deactivate your own account or the last active admin.

- `PUT /api/admin/users/{id}/deactivate`
- `PUT /api/admin/users/{id}/reactivate` (does **not** restore old
  session cookies)

## Cost and usage

Every LLM call is recorded (model, tokens, latency, status, cost). The
dashboard reads `GET /api/admin/usage?days=30` for a total, per-day
buckets, and a per-actor breakdown. Costs are computed from per-model
rates in `pricing.py`; override them with `ELENCHUS_PRICING_JSON` (a JSON
map of `model → {input_per_1m, output_per_1m}`). Unknown models record
zero cost with a warning rather than guessing.

## Integrity and audit

- **Per-base integrity** — `GET /api/admin/integrity` gives a cheap,
  usage-table summary per base (calls, cost); `GET
  /api/admin/integrity/{base_id}` opens one base for full content metrics
  (|C|, |D|, tensions by status, implications, atoms, turns).
- **Drift audit** — `elenchus audit` (CLI) or `GET /api/admin/audit`
  cross-checks the platform DB against the filesystem: registered bases
  with/without files, orphaned files, and cross-DB actor references that
  point at no actor. DuckDB does not enforce foreign keys across files, so
  run this periodically.

## Backups

`POST /api/admin/backup` snapshots the platform DB and every registered
base into one timestamped `tar.gz` under `$ELENCHUS_DATA/backups/`, using
DuckDB `EXPORT DATABASE` (MVCC-safe, runs inside the server process so it
respects the single-writer lock). It prunes to the newest *N* (default
14). Schedule it with `scripts/backup.py` on a cron — see [Operations
Runbook §7](OPERATIONS.md). Copy archives off-box for durability; restore
with `IMPORT DATABASE` (the rollback path, since migrations are
forward-only).

## Alerting

Operational failures (LLM outages, exhausted retries, budget caps) are
dispatched to alert channels. The **console** channel is always on; set
`ALERT_EMAIL_TO` to also email them. Tune with:

| Variable | Meaning | Default |
|---|---|---|
| `ALERT_EMAIL_TO` | recipient; unset = console only | (none) |
| `ALERT_EMAIL_MIN_SEVERITY` | `critical`/`high`/`medium`/`low` | `high` |
| `ALERT_DEDUP_MINUTES` | dedup window per severity+category | `5` |

`critical` alerts (e.g. revoked API key) are never deduped.

## Runtime LLM settings

Model, API key, base URL, and protocol can be changed at runtime via the
**gear icon → Settings** modal or `PUT /api/settings`, without a restart.
Non-secret settings (model, base URL) persist in the browser and re-sync
on restart. For the canonical, restart-safe configuration, set the
environment variables in [Deployment](deployment.md) instead.

## Health

`GET /healthz` is unauthenticated and cheap. Point an uptime monitor at
it and alert on two flags in the response: `llm_configured` (must be
`true`) and `phase_b_enabled` (must be `false` for the Sloan study).

## Admin API reference

All routes require `require_admin` unless marked *(researcher)*.

| Method & path | Purpose |
|---|---|
| `POST /api/admin/invites` | Issue an invite (role, optional email) |
| `GET /api/admin/invites` | List invites |
| `DELETE /api/admin/invites/{token}` | Revoke an unused invite |
| `GET /api/admin/users` | List all actors |
| `PUT /api/admin/users/{id}/deactivate` | Soft-delete an actor |
| `PUT /api/admin/users/{id}/reactivate` | Restore an actor |
| `GET /api/admin/usage?days=N` | Cost/usage rollup |
| `GET /api/admin/integrity` · `/{base_id}` | Per-base integrity summary / detail |
| `GET /api/admin/audit` | Platform ↔ filesystem drift |
| `POST /api/admin/backup` · `GET` | Run a backup / list archives |
| `POST /api/admin/study/tokens` *(researcher)* | Issue a participant token |
| `GET`/`DELETE /api/admin/study/tokens[/{token}]` *(researcher)* | List / void tokens |
| `POST /api/admin/study/{study_id}/export` *(researcher)* | Export a study |
| `POST`/`GET /api/admin/study/judge-packages` *(researcher)* | Create / list judge packages |
| `POST /api/admin/study/judge-assignments` *(researcher)* | Assign a package to a judge |
| `GET /api/admin/study/surveys` · `reports` *(researcher)* | Cohort questionnaire / report views |
