# Elenchus Roadmap

A working plan for the next phase of work on Elenchus. The conceptual rationale
behind these phases is documented in transcripts of the design sessions; this
file is the operational version — concrete deliverables, schemas, decisions,
and acceptance criteria.

## Sequencing Principle

**Multi-user platform foundation first; study-specific features last.** The
work proceeds from generally-useful infrastructure (Phase A) through protocol
extensions (Phase B) and operational hardening (Phase C) to study-specific
scaffolding (Phase D). Each phase produces a shippable artifact. Phases A–C
are independent of any particular study and have value on their own; Phase D
realizes the Sloan controlled-comparison study against the foundation Phases
A–C provide.

```
Phase A: Multi-user platform foundation         (~3–4 weeks)
Phase B: Protocol extensions                    (~2 weeks)
Phase C: Operational infrastructure             (~2 weeks)
Phase D: Study harness (Sloan-specific)         (~4–5 weeks)
```

Total ~11–13 weeks. Phases A–C can be done before any grant timing pressure;
Phase D fits the Sloan months 1–3 hardening window cleanly given A–C are
already in place.

---

## Phase A — Multi-User Platform Foundation

**Goal.** Multiple users can each have their own private Elenchus dialectics
on a single server, with persistent identity, invite-only signup, and a
data model that future-proofs the architecture for multi-respondent dialectics.

### Schema

Two-database layout: a platform-level DuckDB (`platform.duckdb`) for
identity, invites, and session metadata; per-base DuckDB files for dialectical
state, organized under `bases/{actor_id}/{base_id}.duckdb`.

#### `platform.duckdb`

```sql
CREATE TABLE actors (
    id INTEGER PRIMARY KEY,
    kind VARCHAR NOT NULL CHECK(kind IN
        ('admin','researcher','user','judge','participant','opponent_llm','system')),
    email VARCHAR UNIQUE,
    display_name VARCHAR NOT NULL,
    password_hash VARCHAR,        -- null for participants/opponents
    credentials JSON DEFAULT '{}',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    deactivated_at TIMESTAMP
);

CREATE TABLE auth_sessions (
    token VARCHAR PRIMARY KEY,
    actor_id INTEGER REFERENCES actors(id),
    issued_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP,
    revoked_at TIMESTAMP
);

CREATE TABLE invites (
    token VARCHAR PRIMARY KEY,
    role VARCHAR NOT NULL,
    intended_email VARCHAR,
    issued_by INTEGER REFERENCES actors(id),
    issued_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP,
    consumed_at TIMESTAMP,
    consumed_by INTEGER REFERENCES actors(id),
    metadata JSON DEFAULT '{}'
);

CREATE TABLE bases (
    id VARCHAR PRIMARY KEY,
    name VARCHAR NOT NULL,
    owner_id INTEGER REFERENCES actors(id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(owner_id, name)
);

CREATE TABLE sessions (
    id INTEGER PRIMARY KEY,
    actor_id INTEGER REFERENCES actors(id) NOT NULL,
    base_id VARCHAR REFERENCES bases(id) NOT NULL,
    opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    closed_at TIMESTAMP,
    status VARCHAR DEFAULT 'open'
);

CREATE TABLE platform_settings (
    key VARCHAR PRIMARY KEY,
    value VARCHAR
);
-- Initial setting: signup_mode = 'invite_only'
```

#### Per-base DuckDB (extends current `material_base.py` schema)

Existing tables (`atoms`, `assessments`, `positions`, `tensions`,
`conversation`, `meta`) are extended with:

- `atoms.contributor_id INTEGER NOT NULL` — FK to `actors.id` (cross-DB; just an int)
- `atoms.paraphrases JSON DEFAULT '[]'`
- `atoms.references JSON DEFAULT '[]'`
- `assessments.contributor_id INTEGER NOT NULL`
- `assessments.domain VARCHAR DEFAULT 'asserted'` — `asserted|tension|imported|refined`
- `assessments.provenance JSON DEFAULT '{}'`
- `assessments.status VARCHAR DEFAULT 'active'` — `active|disputed|retracted`
- `positions.actor_id INTEGER NOT NULL`
- `positions.case_id INTEGER NOT NULL` — references new `cases` table
- `tensions.case_id INTEGER NOT NULL`
- `conversation.session_id INTEGER NOT NULL`
- `conversation.case_id INTEGER`
- `conversation.actor_id INTEGER` (null for system messages)

New table:

```sql
CREATE TABLE cases (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL,
    name VARCHAR DEFAULT 'main',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

In Phase A, each session auto-creates one `cases` row at session start; all
positions and tensions bind to that case. Multiple cases per session is a
future feature; the column is in place from the start.

### Auth and identity

- **Email/password** as primary; magic-link as secondary (also useful for
  password reset).
- **Password storage**: bcrypt via `passlib`.
- **Session tokens**: server-side, stored in `auth_sessions`, returned as
  HTTP-only cookies. Simpler than JWT for a single-server deployment.
- **Signup mode**: `invite_only` (default) | `email_verified` | `open`.
  Configurable via admin UI; defaults to invite-only.
- **Admin bootstrap**: `elenchus admin create --email <e> --name <n>` CLI
  command for the first admin; subsequent admins via invite.
- **Opponent identity**: a single `actors` row with `kind='opponent_llm'`,
  identifier encoding the model name and prompt version. Created at server
  init; new row when model/prompt changes substantially.

### Invites

- Admin issues an invite via `POST /api/admin/invites` with role and optional
  email; system returns a token.
- Recipient hits `/signup?token=<t>` and creates an account; token is
  consumed on use, single-use, optional expiration (default 30 days).
- Admin can revoke unused invites.

### API surface

Session-keyed routes, future-proof for multi-respondent. The "current
dialectic" UI concept maps to "the user's active session against a base."

```
POST   /api/auth/signup            # accept invite, create account
POST   /api/auth/login             # email + password
POST   /api/auth/logout
GET    /api/auth/me                # current user

GET    /api/bases                  # bases owned by current user
POST   /api/bases                  # create base
GET    /api/bases/{id}             # base metadata
DELETE /api/bases/{id}

POST   /api/sessions               # open session against a base
GET    /api/sessions/{id}          # session state
POST   /api/sessions/{id}/message  # message into this session
POST   /api/sessions/{id}/tensions/{tid}
POST   /api/sessions/{id}/retract
POST   /api/sessions/{id}/derive
GET    /api/sessions/{id}/report.pdf
DELETE /api/sessions/{id}

POST   /api/admin/invites          # admin only
GET    /api/admin/invites
DELETE /api/admin/invites/{token}
GET    /api/admin/users
PUT    /api/admin/settings
```

Legacy `/api/dialectics/{name}/...` routes deprecated; redirects to the
session-keyed equivalents during a transition window if needed.

### Server-side state

The current in-memory `_states` dict in `server.py` is replaced with:

- An LRU-bounded connection cache keyed by `base_id`, lazy-loaded from disk
  on first access, evicted on idle or pressure.
- Per-base file lock to serialize writes (DuckDB is single-writer per file).
- Auth middleware on all routes; resolves the actor from the session cookie
  and rejects unauthenticated requests with 401.
- Authorization middleware on base routes; rejects access if the actor isn't
  the base's owner with 403.

### Frontend

- Login screen with email/password and "sign up with invite" link.
- Signup screen that accepts a token (from URL parameter) and creates an account.
- Account menu: display name, logout, change password.
- Base list scoped to the logged-in user (replaces current global list).
- 401 handling: redirect to login with return-URL preservation.
- API client: session-keyed, sends cookie automatically.

### Email service

A small `EmailService` abstraction with two implementations: console-print
(for development) and SMTP via a transactional provider (Postmark or SendGrid).
Used by:
- Invite delivery
- Magic-link login (Phase A)
- Password reset (Phase A)
- Operational alerts (Phase C)

Configuration via environment variables (`SMTP_HOST`, `SMTP_USER`, etc.;
provider-specific API keys for Postmark/SendGrid).

### Tests

- Auth flow: signup-from-invite, login, logout, password change, magic link.
- Multi-user scoping: actor A cannot read or write to actor B's bases.
- Invite lifecycle: issue, consume, revoke, expire.
- Session lifecycle: open, message, close.
- Cross-DB integrity: actor_id references in per-base DBs validate against
  platform.duckdb on relevant operations.
- Migration: existing single-user dialectic files can be assigned to an
  admin actor and continue working.

### Acceptance criteria

Phase A ships when:
1. Two users can independently sign up via invite, log in, and create their
   own dialectics that the other cannot access.
2. All existing functionality (commit, deny, tension proposal, accept/contest,
   derive, PDF export) works against the new session-keyed API.
3. Admin can issue and revoke invites via the admin dashboard.
4. Server survives restart without losing session state.
5. Tests pass (existing 118 + new auth/multi-user tests, targeting 150+).
6. `ruff check` and `ruff format --check` clean.
7. README and CLAUDE.md updated to reflect the new architecture.

### Decisions to lock in at start of Phase A

- **Auth library**: roll minimal with `passlib[bcrypt]` rather than
  `fastapi-users`. ~200 lines of focused code; total control.
- **Token storage**: server-side `auth_sessions` table; cookie-based, HTTP-only.
- **One DuckDB per base**: keep the current per-file model. Platform metadata
  lives separately in `platform.duckdb`.
- **Directory layout**: `bases/{actor_id}/{base_id}.duckdb`.
- **Opponent identity**: special actor with `kind='opponent_llm'`.

---

## Phase B — Protocol Extensions

**Goal.** The dialectical protocol supports conceptual specification as a
first-class workflow. Respondents articulate theory (bearers + defeasible
rules) as fluently as they propose tensions and accept/contest them.

### Speech acts to add

- `ASSERT_IMPLICATION` — respondent directly asserts `{γ} |~ {δ}` with
  per-atom side mapping. Recorded with `domain='asserted'`.
- `INTRODUCE_BEARER` — respondent adds an atom to L_B without committing
  to or denying it (vocabulary-only contribution).
- `RETRACT_IMPLICATION` — respondent withdraws an asserted or
  tension-derived implication. Marks `status='retracted'`.

### Opponent prompt changes

- Recognize positum framing: descriptive case ("a 58-year-old patient
  presents with...") vs. ontology articulation ("an animal is anything
  that is alive..."). Adjust extraction posture accordingly.
- Aggressive confirmation loops during early bearer-introduction and rule
  assertion: "I understood these atoms / rules — confirm?" Especially
  important for unattended use.
- Maieutic opening on empty base: ask context-setting questions, elicit
  vocabulary, then elicit rules.

### Provenance

Every assertion carries provenance JSON:
```json
{
  "source": "asserted" | "tension" | "imported",
  "session_id": <int>,
  "case_id": <int>,
  "turn": <int>,
  "reason": "...",
  "earned_via_tension": <tension_id>   // if source=tension
}
```

### Tests

- Parser tests for each new speech act.
- Application tests: state mutations correct, provenance recorded.
- End-to-end: an ontology positum produces atoms in L_B and assertions in
  |~_B without case commitments in [C:D].

### Acceptance criteria

1. Respondent can articulate a small ontology (5–10 atoms, 5–10 rules) in
   ~10 turns of natural-language interaction.
2. Atoms introduced via `INTRODUCE_BEARER` appear in L_B but not in C/D.
3. Rules asserted via `ASSERT_IMPLICATION` appear in I with
   `domain='asserted'`.
4. Retraction via `RETRACT_IMPLICATION` correctly removes a rule and
   triggers any necessary re-derivation.
5. Bootstrap from empty base feels natural (confirmed by PI walkthrough).

---

## Phase C — Operational Infrastructure

**Goal.** Platform is operable for unattended use. Failure modes are handled
gracefully, the operations team is alerted when something needs attention,
and post-session integrity is verifiable.

### API client wrapper

Replaces scattered `try/except` patterns. Single function per LLM call,
classifies errors into:
- Rate limit (429)
- Provider error (5xx)
- Auth failure (401/403)
- Timeout
- Content policy refusal
- Token overflow
- Parse failure (after fallback)

Retry with exponential backoff for retryable categories. Emits structured
events for all categories regardless of recovery.

### Alerting subsystem

- Severity tiers: critical | high | medium | low.
- Channels: email (always), SMS (critical only).
- Rate-limited dispatch: group repeated alerts of the same type within a
  window to avoid email storms.
- Configuration: `ALERT_EMAIL_TO`, `ALERT_SMS_TO`, severity thresholds.
- Templates: structured subject lines for inbox filtering.

### Cost tracking

- Every API call records token counts and computed cost into a `usage`
  table.
- Daily/monthly aggregation; pre-emptive alerts at 80% / 90% / 100% of
  configured budget.
- Hard cap: auto-pause new sessions when monthly limit hit; existing
  sessions complete.

### Logging

Structured logs of every state-changing move, API call, session transition,
error. Anonymized retention policy for post-study analysis even after
participant data deletion.

### Per-session integrity reports

At session end, compile:
- API call counts (success/retry/fail per category)
- Median + p95 latency
- Cost incurred
- Incidents during session
- Content metrics (atoms introduced, implications asserted, tensions
  resolved)

Visible in admin dashboard; flags sessions for review.

### Hosting

- Production server: small VM with systemd-managed FastAPI process,
  Nginx in front for SSL termination, daily DuckDB backups to object
  storage.
- Heartbeat endpoint for external monitoring.
- Log rotation.

### Acceptance criteria

1. A simulated rate-limit event triggers an alert email within seconds.
2. The participant UI shows a graceful "pausing — handling a technical
   issue" rather than a stack trace.
3. Cost tracking matches actual Anthropic billing within 5%.
4. Integrity report renders for every completed session.
5. Server can be restarted without losing active sessions.

---

## Phase D — Study Harness (Sloan-Specific)

**Goal.** Platform supports the Sloan controlled-comparison study end-to-end:
participant recruitment, session execution, output collection, blinded
judging, data export.

### Participant session tokens

Separate from invite/account flow. Pre-created participant actors
(`kind='participant'`) with `participant_session_tokens` table:

```sql
CREATE TABLE participant_session_tokens (
    token VARCHAR PRIMARY KEY,
    actor_id INTEGER REFERENCES actors(id),
    study_id VARCHAR,
    scheduled_start TIMESTAMP,
    scheduled_end TIMESTAMP,
    condition VARCHAR CHECK(condition IN ('elenchus','baseline')),
    session_id INTEGER REFERENCES sessions(id),
    status VARCHAR DEFAULT 'scheduled',
    used_at TIMESTAMP
);
```

Flow: ADSA emails link → participant clicks → token validates → session
starts → no account creation, no login.

### Session lifecycle state machine

`scheduled` → `briefing` → `tutorial` → `active` → `post-session` →
`surveyed` → `complete` (or `expired` / `interrupted`).

Platform routes participant through each state; participant never has to
figure out what comes next.

### Tutorial / onboarding

- 15-minute in-app structured tutorial with a warm-up domain (something
  trivial like "kinds of pets") teaching the mechanics.
- In-session glossary, tooltips, contextual help.
- Plain-language framing for participant-facing UI ("starting position,"
  "commitments and denials," "rules you endorse"); technical labels
  preserved in the formal record.
- Worked-example walkthrough document, ~3 pages, drawn from the PROV-O case
  study.

### Baseline condition interface

Free-form chat with the same LLM, same visual chrome, same session
machinery. Different system prompt (no opponent-role framing). Produces a
transcript that becomes input to the LLM report generator.

`mode='baseline'` flag on the session routes to the chat UI; everything
else (auth, timing, surveys) is shared with the Elenchus condition.

### Structured LLM-generated report

Extractive prompt that itemizes statements and implications from the
session record (material base for Elenchus, transcript for baseline).
Format-balanced across conditions; pilot validates against the LLM-report
confound.

Endpoint: `POST /api/sessions/{id}/generate-report` produces the report,
stores it, makes it available for judging.

### Researcher admin dashboard

- Per-session status, integrity reports, include/exclude flags.
- Blinded package preparation for judges.
- Aggregate study metrics (recruitment progress, completion rates).
- Per-participant timeline.

### Blinded judge interface

Separate UI (could be a new React route or a small companion webapp):
- Login as judge (invite-based, role='judge').
- Sees paired outputs labeled neutrally (Output A / Output B).
- Captures 1–7 Likert ratings on quality dimensions with written
  justification, pairwise rankings, condition guess + confidence.

### Questionnaire integration

NASA-TLX, SUS, Trust in Automated Systems, custom Epistemic Experience
Questionnaire. Hosted in-platform at session end for continuity (rather
than redirecting to external Qualtrics), keyed to participant session id.

### Per-study data export

- Per-session archive: DuckDB file, JSON state, transcript, generated
  report, questionnaire responses, structural metrics.
- Per-study export: all sessions for downstream analysis.
- Pseudonymization layer: real identities in ADSA tracking, opaque IDs in
  data archive.

### Acceptance criteria

1. A participant can complete both conditions (Elenchus + baseline) without
   PI/researcher intervention.
2. Researcher dashboard shows per-session status and integrity reports.
3. Blinded judges can rate paired outputs without identifying conditions
   above chance.
4. Per-study export produces analysis-ready archives.
5. End-to-end pilot run with 4 participants completes successfully.

---

## Cross-Cutting Principles

These apply across all phases:

- **Logging discipline.** Every state-changing move emits a structured log
  entry. Required by the global instruction and load-bearing for
  post-experimental analysis.
- **Tests proportional to complexity.** Each new speech act, schema change,
  and API route ships with parser/application/integration tests. Target
  150+ tests by end of Phase A, 200+ by end of Phase D.
- **Schema migration discipline.** Each phase that changes schema ships
  with a migration that handles existing data. Establish the pattern in
  Phase A (migrating existing single-user dialectics).
- **Audit-trail invariant.** Every state change carries provenance (who,
  in what session, against what case, when, with what reason). Implemented
  via FK relationships and provenance JSON from Phase A onward.
- **Plain-language UI, technical formal record.** User-facing strings use
  accessible terms; the data model retains formal labels. Both can be
  rendered from the same underlying state.

---

## Future Extensions (Beyond Phase D)

Tracked for posterity but not planned in the current sequence:

- **In-dialectic LLM evaluation primitive** (`EVALUATE` against a sequent).
  Replaces export-then-evaluate workflow.
- **Multi-respondent shared bases.** View-relative endorsement, cross-session
  disputes, coalition views. Requires Phase A foundation.
- **NMMS_Onto integration.** Typed vocabulary (concepts, roles,
  individuals); ABox/TBox split; the seven ontology schemas. Optional
  upgrade when propositional content becomes limiting.
- **Always-on inferential surface.** Entitlements + forced commitments +
  conflicts rendered continuously. Pilot in Phase D would determine
  whether this helps or overwhelms participants.
- **Prover-derived challenges.** When accepted implications cause C to
  derive D, surface as a dialectical move category. (See
  `prover-derived-challenges-plan.md` in MEMORY.)
- **OAuth / ORCID / institutional SSO.** Convenience layer over the
  email/password core.
- **Postgres backend.** For real concurrent multi-user write volume.
  Phase A's DuckDB-per-base model handles modest concurrency; this is
  the swap-in path when it doesn't.
- **Shared cases.** Multiple respondents collaboratively examining the
  same case position. Requires conflict-resolution design.
- **Public benchmark export.** Snapshot the base as benchmark JSON for
  external comparability. Optional secondary feature once in-dialectic
  evaluation is in place.

---

## Decisions Made

- **Sequencing**: multi-user foundation first, study features last
  (decided in 2026-05-31 design session).
- **Auth approach**: email/password + magic link, server-side sessions,
  invite-only signup default.
- **Account vs. participant separation**: invites for long-term accounts,
  session tokens for study participants — different tables, different
  flows.
- **Schema future-proofing**: contributor_id everywhere, sessions and
  cases as first-class, domain + provenance on assessments, view-
  parameterized derivability. Same schema serves Phase A and full
  multi-respondent.
- **API shape**: session-keyed routes from the start.
- **Opponent identity**: special actor kind, uniform contributor_id
  semantics.
- **Positum simplification**: positum can be ontology articulation or
  case description; opponent reads intent and adjusts posture (decided
  in 2026-05-31 design session).

## Open Decisions

These need resolution before or during the relevant phase:

- **Magic-link delivery in Phase A** vs. defer to Phase C alongside other
  email work. Probably do both at once.
- **Transactional email provider**: Postmark vs. SendGrid vs. AWS SES.
  Cost is similar; choose based on team familiarity.
- **Frontend framework decision**: stay with single-file React + Babel,
  or migrate to a build system as complexity grows. Current is fine for
  Phase A; revisit at end of Phase A.
- **Admin dashboard scope in Phase A**: minimum is invites + users;
  decision on whether settings/configuration UI is Phase A or Phase D.
- **Per-base file naming**: `bases/{actor_id}/{base_id}.duckdb` vs.
  `bases/{actor_id}/{slug}.duckdb`. Slug is more readable but breaks if
  name changes; UUID is opaque but stable.
- **Conversation history retention policy**: long sessions accumulate
  history; current summary mechanism partial mitigation. Plan a hard
  cap.

---

## Status

- **Phase 0** (foundations through May 2026): complete. Tension queue,
  toggleable panes, robust JSON parsing, PyPI v0.1.1, mkdocs site.
- **Phase A**: not started. Next active phase.
- **Phase B**: not started.
- **Phase C**: not started.
- **Phase D**: not started; depends on Sloan funding decision.

Last updated: 2026-05-31.
