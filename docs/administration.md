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
| `researcher` | Run studies: set a study up, enrol participants, assign texts to judges, export study data | `require_researcher` (admin or researcher) |
| `user` | Create and work in their own dialectics | authenticated |
| `judge` | See the blinded texts assigned to them and rate each one | `require_judge` (admin or judge) |
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
with five tabs:

- **Invites** — issue an invite (pick a role, optionally pin it to an
  email), list outstanding/consumed/expired invites, and revoke unused
  ones.
- **Users** — list every actor with id, kind, email, display name, and
  active/deactivated status (admins marked ★).
- **Study** — set a study up (its two topics), enrol participants (one
  step issues both of a person's session links, with a balanced random
  allocation), watch the roster, close an abandoned session, and export.
  (See [Running a Study](study.md) and the [Study Runbook](study-runbook.md).)
- **Judging** — assign submitted texts to judges and watch the panel's
  progress.
- **Costs** — what the LLM calls have cost, against a budget line, by
  model, by purpose and per study session. (See [Cost and
  usage](#cost-and-usage).)

The Study and Judging tabs drive researcher-gated routes. A `researcher`
account sees a **STUDY** button instead of ADMIN, opening the same
dashboard with just those two tabs; an admin sees all five, so a sole
admin can run a pilot end to end. **Judge accounts are created by an
admin** (invite with role `judge`) — researchers can assign work to judges
but not create them.

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

The **Users** tab lists every actor with a per-row action.

**Deactivate / reactivate.** Deactivation is a **soft delete**
(`actors.deactivated_at`): the account can no longer log in and its
sessions stop working immediately, but its past contributions stay
attributed. The server refuses to deactivate your own account or the last
active admin. Reactivate restores access (it does **not** restore old
session cookies — they log in fresh); the **"+ new password"** variant
also forces them to set a new password at that next login (use it when the
deactivation was security-related). There is no hard delete.

- `PUT /api/admin/users/{id}/deactivate`
- `PUT /api/admin/users/{id}/reactivate[?require_password_change=true]`

## Passwords & resets

Passwords are bcrypt-hashed; new passwords (set via reset or a forced
change) must be **≥10 characters**.

- **Admin reset** — the Users tab **"reset password"** button (or
  `POST /api/admin/users/{id}/reset-password`) logs the user out
  immediately and produces a **one-time link** (valid 24 h) that lets them
  choose a new password. The link is **emailed** when SMTP is configured,
  and **always returned in the UI** so you can share it directly when it
  isn't. Reset tokens are stored only as SHA-256 **hashes**, so a leaked DB
  or backup exposes no usable links.
- **Self-service** — the login screen's **"Forgot password?"** sends a
  reset link (60-minute TTL). The request is rate-limited and always
  responds the same way whether or not the email is registered (no
  account enumeration). This path only delivers once SMTP is configured.
- **Forced change** — `actors.must_change_password` makes a user set a new
  password on their next login before they can do anything else. It's set
  by "reactivate + new password" above; a successful change (or any reset)
  revokes the actor's other sessions.

> Self-service "Forgot password?" needs a working mail transport
> ([Deployment](deployment.md) → SMTP); admin resets work without one via
> the returned link.

## Cost and usage

Every LLM call the server makes is recorded in the `usage` table: who
made it, on which dialectic, the model, the token counts, latency,
retries, whether it succeeded, and what it was **for** (an Elenchus turn,
a baseline chat turn, a rolling context summary, a PDF-report summary, a
simulation persona).

**Tokens are the record; dollars are computed when you look.** The
**Costs** tab (`GET /api/admin/costs?days=30`) prices the recorded tokens
against the price table in `pricing.py` each time it is opened, at the
rate in effect on the day of each call. So correcting a rate corrects the
history as well, a provider's price change (a new entry with an
`effective_from` date) leaves earlier calls at the old price, and a model
with **no** rate is listed in a red *Unpriced models* box — its tokens
counted, its cost left out of every figure — instead of being shown as
$0. The figure stored with each row at the time of the call is kept but
never used.

The tab shows:

- **Month to date / the chosen window / all time**, with a 7-, 30-,
  90-day or all-time window for the breakdowns.
- **Budget** — spend against a budget line you set (an amount and the
  period it covers, e.g. a grant's LLM line and the grant period), with a
  marker for how much of the period has passed. Display only: nothing is
  cut off when it is exceeded.
- **Spend per day**, **by model** (with the rate used) and **by
  purpose** — which separates participants' turns from platform overhead
  — plus failed calls and retry attempts. Providers don't report the
  tokens of a failed attempt, so retries are counted, not priced.
- **Studies** — per study and condition: sessions finished and still to
  come, the practice and task shares, the mean cost of a finished
  session, and a projection for the outstanding sessions at those means.
  *Sessions* lists every participant session with its tokens and cost. A
  session's spend is everything its own (passwordless) account did;
  calls on its `practice-…` dialectic are the tutorial's share.
- **By account** — the fifteen largest spenders in the window.

The price table carries a "checked on" date, shown at the top of the tab.
**The provider's invoice is the final word** — check the table against the
provider's pricing page when you budget, and correct or extend it with
`ELENCHUS_PRICING_JSON`, a JSON map of model name to a rate or a list of
dated rates:

```json
{"my-model": {"input_per_1m": 1.0, "output_per_1m": 2.0},
 "claude-opus-5": [
   {"input_per_1m": 5, "output_per_1m": 25},
   {"input_per_1m": 4, "output_per_1m": 20, "effective_from": "2027-01-01"}]}
```

Model names are matched after dropping a routing prefix (`anthropic/…`)
and turning `.` into `-`, then by the longest registered name the model
starts with, so dated revisions resolve to their family.

For a grant report or a post-run record, `elenchus costs [--days N]
[--json]` prints the same report from the command line (stop the server
first — DuckDB allows one process per file). Infrastructure costs
(hosting, domain) are not tracked by the platform.

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

Model, API key, API endpoint (base URL), and protocol are set at runtime by
an admin via the **gear icon → Settings** modal (or `PUT /api/settings`,
admin-gated), without a restart. This is the intended way to provide the
key on a fresh server: log in as the bootstrap admin, open Settings, and
paste it.

Settings **persist server-side** in `platform_settings`: model, endpoint,
and protocol in plaintext; the API key **encrypted at rest** (Fernet) using
the `ELENCHUS_SECRET_KEY` master key. On restart the server loads and
applies them (persisted values override the environment). So set
`ELENCHUS_SECRET_KEY` once at deploy ([Deployment](deployment.md)) and the
UI-set key survives restarts — the plaintext key never touches the DB file
or backups, only its ciphertext does.

If `ELENCHUS_SECRET_KEY` is unset, a key entered in the modal is applied to
the running process but **not** persisted (the modal warns you, and
`GET /api/settings` reports `persistence_available: false`). `GET` never
returns the key value — only whether one is set and persisted. Setting
`ELENCHUS_API_KEY` in the environment remains a valid alternative; a
persisted key takes precedence over it at boot.

> Security boundary: the master key lives in the env file on the same host,
> so this is encryption *at rest* (protects DB files and backups), not
> protection against full host compromise. A cloud KMS/HSM holding the
> master key is the production upgrade.

## Health

`GET /healthz` is unauthenticated and cheap. Point an uptime monitor at
it and alert on two flags in the response: `llm_configured` (must be
`true`) and `phase_b_enabled` (must be `false` for the Sloan Foundation-funded study).

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
| `GET /api/admin/costs?days=N` | Cost dashboard report (N = 0 for all time) |
| `PUT /api/admin/costs/budget` | Set (`llm_usd`, `period_start`, `period_end`, `label`) or clear (`{}`) the budget line |
| `GET /api/admin/usage?days=N` | Older cost/usage rollup (same read-time pricing) |
| `GET /api/admin/integrity` · `/{base_id}` | Per-base integrity summary / detail |
| `GET /api/admin/audit` | Platform ↔ filesystem drift |
| `POST /api/admin/backup` · `GET` | Run a backup / list archives |
| `PUT`/`GET /api/admin/study/{study_id}/config` *(researcher)* | Set up / read a study (topics, session gap) |
| `POST`/`GET /api/admin/study/{study_id}/participants` *(researcher)* | Enrol a participant (both links) / roster |
| `POST /api/admin/study/sessions/{id}/interrupt` *(researcher)* | Close an abandoned session |
| `GET /api/admin/study/judges` *(researcher)* | Judge accounts |
| `GET /api/admin/study/{study_id}/texts` · `POST …/text-assignments` *(researcher)* | Submitted texts + panel progress / assign texts to a judge |
| `POST /api/admin/study/tokens` *(researcher)* | Issue a single participant link (test links, one-offs) |
| `GET`/`DELETE /api/admin/study/tokens[/{token}]` *(researcher)* | List / void tokens |
| `POST /api/admin/study/{study_id}/export` *(researcher)* | Export a study |
| `POST`/`GET /api/admin/study/judge-packages` *(researcher)* | Legacy: create / list paired-report packages |
| `POST /api/admin/study/judge-assignments` *(researcher)* | Legacy: assign a package to a judge |
| `GET /api/admin/study/surveys` · `reports` *(researcher)* | Cohort questionnaire / report views |
