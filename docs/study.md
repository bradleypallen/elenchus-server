# Running a Study

Elenchus ships a complete harness for the Sloan-funded human-subjects
study: issuing participant sessions, running them through a controlled
flow under one of two conditions, collecting questionnaires, generating
post-session reports, having blinded judges rate them, and exporting an
analysis-ready, pseudonymized dataset.

This guide is for the **researcher** running a study. It assumes the
server is already deployed ([Deployment](deployment.md)) and an admin has
given you a `researcher` account ([Administration](administration.md)).

> **Ethics first.** A live study with real participants processes personal
> data — it must clear your institution's DPO / ethics review before
> launch. For mechanics and dress rehearsals, drive the harness with
> synthetic data only (`elenchus sim`). See the
> [pre-study checklist](#pre-study-checklist).

## The design in one paragraph

Each participant is assigned to one **condition** and builds a knowledge
representation of a domain. In the **`elenchus`** condition the LLM is a
Socratic *opponent*: it tracks a bilateral position, proposes tensions,
and applies speech acts. In the **`baseline`** condition the LLM is an
ordinary *assistant* in free-form chat — no tensions, no formal state.
After the task, each participant fills four questionnaires. A
condition-agnostic structured report is generated from each session;
blinded judges rate matched `elenchus`/`baseline` report pairs without
knowing which is which. All measures are computed post-hoc from the same
exported dataset.

> **Phase B stays off.** The study runs with the
> `{COMMIT, DENY, ACCEPT_TENSION, CONTEST_TENSION, RETRACT, REFINE}`
> vocabulary only. Leave `ELENCHUS_ENABLE_PHASE_B` unset (the default) so
> the theory-articulation acts never appear. `/healthz` reports
> `phase_b_enabled:false` — alert on it.

## 1. Issue participant tokens

A participant logs in with a **single-use, passwordless token** — the
token *is* the credential. Issue one per participant from the dashboard
(**ADMIN → Study**) or the API:

```bash
curl -sf -b cookies.txt -X POST \
  https://elenchus.example.edu/api/admin/study/tokens \
  -H 'Content-Type: application/json' \
  -d '{"study_id":"PILOT","condition":"elenchus","display_name":"P01",
       "scheduled_start":"2026-07-01T09:00:00Z",
       "scheduled_end":"2026-07-01T17:00:00Z"}'
```

This creates a passwordless `participant` actor bound to the study and
condition, and returns a token. Send the participant their link
(`/api/study/<token>`). Outside the optional scheduling window, or on a
second use, the link returns **410 Gone**. Void a still-scheduled token
with `DELETE /api/admin/study/tokens/{token}`.

For a within-subjects design, issue each participant one token per
condition.

## 2. The participant flow

Opening the token link trades it for a session cookie and starts a
state machine. Participants are routed entirely by **session state** (not
browser storage), so the flow is safe on a shared machine and they never
see the home screen or a "create dialectic" control.

| State | What the participant sees | Advances on |
|---|---|---|
| `briefing` | Consent + study overview | "Begin tutorial" |
| `tutorial` | A throwaway *practice* base in their real condition's UI | "Start the real task" |
| `active` | The *task* base — full dialectic (or baseline chat) | "End session" |
| `post_session` | A short summary | "Continue to questionnaires" |
| `surveyed` | The questionnaires, one after another | final submit |
| `complete` | "Thank you" | — |
| `expired` / `interrupted` | "Session ended — contact the researcher" | terminal |

Each participant works in their assigned condition for both the practice
and task bases. The condition is fixed at token issuance and enforced
server-side at message time.

## 3. Questionnaires

After the task, the participant completes four instruments in sequence
(`instrument_version` is stamped on every submission so the export
reproduces exactly what they saw):

| Instrument | What it measures | Scale |
|---|---|---|
| `nasa_tlx` | Task load (6 dimensions) | 0–100 (steps of 5) |
| `sus` | System Usability Scale (10 items) | 1–5 |
| `tias` | Trust in Automated Systems (12 items) | 1–7 |
| `eeq` | Epistemic Experience — ownership, articulation, challenge (8 items) | 1–7 |

Submissions are validated strictly (every item present, in range, no
extras) and rejected whole on any error. The EEQ is custom to this study;
**review and sign off its wording before launch** — see the
[EEQ review packet](eeq-review.md) — and bump `INSTRUMENT_VERSION` on any
reword.

## 4. Structured reports

For each completed session, generate a structured report (`POST
/api/study/session/{id}/generate-report`). The LLM distills the session
into a uniform format — **Domain, Atomic statements, Implications, Notes**
— using the *same* condition-agnostic template for both conditions (only
the input differs: bilateral position + transcript for `elenchus`,
transcript only for `baseline`). The report is the unit judges rate, so
its uniform shape is what keeps judging blind.

## 5. Blinded judging

1. **Package** a matched pair — one `elenchus` report and one `baseline`
   report — with `POST /api/admin/study/judge-packages`. The two reports
   are placed in neutral **slot A / slot B**, randomized per package so
   slot position carries no signal. The real condition→slot mapping is
   stored for analysis but never shown to judges.
2. **Assign** the package to one or more judges (`POST
   /api/admin/study/judge-assignments`) — assign the same package to
   several judges for inter-rater reliability.
3. A **judge** logs in (email + password) and works their queue (`GET
   /api/judge/queue`). Each assignment shows the two reports with all
   condition labels, models, and costs stripped. They rate on five
   dimensions, 1–7, **per side**:

   > Completeness · Correctness · Conciseness · Fidelity · Coherence

   plus a justification per side, a pairwise winner (A / B / tie), and —
   to validate the blind — a condition guess per side with a confidence
   rating. If judges guess at chance, blinding held.

## 6. Export the data

When the study is done, export it (dashboard **Study → Export**, or `POST
/api/admin/study/{study_id}/export`). This writes two things:

- **The archive** — `$ELENCHUS_DATA/exports/study-{id}-{ts}.tar.gz`: a
  per-session tree (lifecycle, dialectic state, transcript, reports,
  surveys, integrity, and a DuckDB dump) plus pseudonymized judging data.
  Participants and judges appear only as opaque IDs (`P-001`, `J-001`,
  `R-001`); **no emails or names**.
- **The pseudonym map** — `…​.pseudonyms.json`, written *next to* the
  archive, **never inside it**. It links opaque IDs back to real people.

> **Keep these apart.** The pseudonym map stays with your
> participant-tracking records and must be **excluded from any public
> deposit** (Zenodo, OSF). The archive alone is safe to share/deposit.

Archives stay on the server; retrieve them out-of-band (`scp`/`sftp`).
Individual session failures are recorded in the manifest, not fatal.

## Pre-study checklist

Run before the first real participant (full version in [Operations
Runbook §10](OPERATIONS.md)):

- [ ] `elenchus sim` (scripted) passes end-to-end through every role,
      including the access/auth probes (tenant isolation, single-use
      tokens, judge-view blinding).
- [ ] `elenchus sim --driver llm` dress rehearsal against the production
      model: participants/judges complete, cost + p95 latency look sane,
      judge condition-guess accuracy is near chance.
- [ ] `RUN_UI_E2E=1 pytest tests/e2e/` passes (real browser: login,
      invite signup, participant link, blinded judge view).
- [ ] `/healthz` → `llm_configured:true`, `phase_b_enabled:false`.
- [ ] A test token walks `briefing → tutorial → active` cleanly.
- [ ] Invite + alert emails actually arrive (`EMAIL_BACKEND=smtp`).
- [ ] Backup cron has produced a readable archive.
- [ ] EEQ wording reviewed and signed off ([packet](eeq-review.md)).
- [ ] DPO / ethics approval in hand for a live launch.

## A worked study design

For a concrete, end-to-end example of designing a study on top of this
harness — research question, positum, conditions, measures, and analysis
— see the (parked) [ARDS dialectical study design](ards-study-design.md).

## Study API reference

Participant- and judge-facing routes (researcher/admin routes are in the
[Administration reference](administration.md#admin-api-reference)):

| Method & path | Role | Purpose |
|---|---|---|
| `POST /api/study/{token}` | public | Consume a token, open a session |
| `GET /api/study/session` | participant | Current session + state |
| `POST /api/study/session/begin-tutorial` | participant | `briefing → tutorial` |
| `POST /api/study/session/begin-task` | participant | `tutorial → active` |
| `POST /api/study/session/advance?to_state=` | participant | Advance the state machine |
| `GET /api/study/instruments` | participant | Questionnaire definitions |
| `POST /api/study/session/{id}/survey` | participant | Submit one instrument |
| `POST /api/study/session/{id}/generate-report` | participant/researcher | Generate the structured report |
| `GET /api/judge/queue` | judge | Assigned packages |
| `GET /api/judge/assignments/{id}` | judge | A blinded report pair |
| `POST /api/judge/assignments/{id}/rate` | judge | Submit ratings |
