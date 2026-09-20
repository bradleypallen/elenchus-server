# Running a Study

Elenchus ships a complete harness for the Sloan Foundation-funded
human-subjects study (Alfred P. Sloan Foundation grant G-2026-79650 to the
Alliance for Data Science and AI):
setting a study up, enrolling participants, running their sessions under
one of two conditions, capturing everything needed for later analysis,
collecting questionnaires, having a blinded expert panel rate what
participants wrote, and exporting an analysis-ready, pseudonymized
dataset.

This guide explains **how the study works and why it is built the way it
is**. If you are the person running sessions and want click-by-click
instructions, use the [Study Runbook](study-runbook.md).

It assumes the server is already deployed ([Deployment](deployment.md))
and an admin has given you a `researcher` account
([Administration](administration.md)).

> **Ethics first.** A live study with real participants processes personal
> data — it must clear your institution's DPO / ethics review before
> launch. For mechanics, training and dress rehearsals, use synthetic data
> only (`elenchus sim`, or a throwaway study with test names). See the
> [pre-study checklist](#pre-study-checklist).

## The design in one page

Each participant is given a **topic** — a named area of their field's
conceptual vocabulary — and about sixty minutes with an LLM to **write a
short introduction to it**: the kind a well-informed colleague would want
to read before working in the area. Two or three paragraphs, in their own
words, with no preparation and no reference material.

They do this **twice**, in two sessions at least a couple of days apart —
once in each **condition**, on a **different topic** each time:

| Condition | What the LLM is |
|---|---|
| `baseline` | An ordinary chat assistant. The participant asks, directs, and keeps what is useful. |
| `elenchus` | A Socratic *opponent*. As the participant states a position, the system reads it as explicit commitments and denials and proposes **tensions** — places where two things they have said may not both hold — which they accept, refine, or contest. |

The writing pane, the task wording and the timer are identical in both.
Neither condition supplies content: the concepts, definitions and
commitments in the finished text are the participant's.

A blinded panel of domain experts then rates each **text**, on its own
merits, for coverage, correctness, concision, and whether the reasoning
holds together. Formal analysis of *how* each text came about — the
dialectical state, NMMS derivability, translation to RDF — happens
**offline**, from the data captured during the sessions. That is why the
platform records far more than the text ([what is captured](#5-what-is-captured)).

> **Phase B stays off.** The study runs with the
> `{COMMIT, DENY, ACCEPT_TENSION, CONTEST_TENSION, RETRACT, REFINE}`
> vocabulary only. Leave `ELENCHUS_ENABLE_PHASE_B` unset (the default).
> `/healthz` reports `phase_b_enabled:false` — alert on it.

## 1. Set the study up

A study is set up once: its **two topics** (A and B) and the **minimum
gap** between a participant's two sessions. In the dashboard: **STUDY →
Study → Setup**. Or:

```bash
curl -sf -b cookies.txt -X PUT \
  https://elenchus.example.edu/api/admin/study/PILOT/config \
  -H 'Content-Type: application/json' \
  -d '{"topic_a_title":"Occurrence and its relatives in Darwin Core",
       "topic_a_brief":"Define Occurrence, Organism, Event and MaterialSample; say what distinguishes an Occurrence from the Organism it records.",
       "topic_b_title":"Taxon names and taxon concepts",
       "topic_b_brief":"Distinguish a name, a taxon concept and a usage.",
       "min_gap_hours":48}'
```

Each topic has a **title** (shown to the participant, and the name the
Elenchus opponent sees as the dialectic's topic) and a **brief** (one or
two sentences of framing). Good topics have a handful of genuinely
interrelated core concepts and at least one contested boundary; the two
should be of comparable difficulty and not overlap.

Topic wording is **copied onto a participant's links when they are
enrolled**. Editing a topic later changes future enrolments only — a
participant already holding links keeps the wording they were issued. The
gap, by contrast, is read at the moment a second link is opened.

## 2. Enrol participants

Enrol a **person**, not a session (**Study → Enrol participant**, or
`POST /api/admin/study/{study_id}/participants`). One step:

- gives them a code — `P01`, `P02`, … — that links their two sessions in
  the data;
- **allocates** which condition and which topic they meet first;
- issues **both** session links, each carrying its condition and topic.

### Counterbalancing

Two things vary between participants and either can bias the comparison:
which *condition* comes first (practice and fatigue carry over), and which
*topic* is met in which condition (topics differ in difficulty however
carefully matched). Crossing them gives four cells. Participants are
allocated by **permuted-block randomization**: every consecutive block of
four enrolments contains each cell exactly once, in random order. The
cells therefore stay balanced at every point in recruitment, and the next
allocation can't be predicted. The roster shows the running count per
cell.

If someone drops out and you recruit a **replacement**, place them by hand
into the cell of the person they replace ("Place by hand"). Hand-placed
participants sit outside the blocks, so they don't disturb the balance of
the randomized sequence.

### The two links

Each link is **passwordless — the link is the credential**. The first
click opens the session. Clicking it again *while that session is still
live* drops the participant back where they were, so they can **resume
from another device or after closing the browser**. Once a session is
finished the link stops working.

The **second link stays shut** until the first session has ended *and* the
study's minimum gap has passed; opened early, it tells the participant
when it will work. (Resuming a second session already under way is never
blocked.) If a participant abandons their first session, close it from
the roster ("Close as interrupted") — everything captured so far is kept —
and their second link can then open.

## 3. The participant's session

Participants are routed entirely by **session state** (not browser
storage), so the flow is safe on a shared machine and they never see the
home screen.

| State | What the participant sees | Advances on |
|---|---|---|
| `briefing` | What the session involves | "Begin tutorial" |
| `tutorial` | The real interface — dialogue plus writing pane — on a throwaway practice topic | "Start the main task" |
| `active` | The main task: their topic, the dialogue, the writing pane, the clock | "Finish session" (submits the text) |
| `post_session` | Confirmation that the text is in | "Continue to questionnaires" |
| `surveyed` | The questionnaires, one after another | final submit |
| `complete` | "Thank you" | — |
| `expired` / `interrupted` | "Session ended — contact the researcher" | terminal |

**The writing pane** sits beside the dialogue throughout the tutorial and
the task. It shows the topic, the brief and the standing instruction, and
holds an editor that **saves automatically**; a reload, or resuming on
another device, restores the draft.

**The clock is guidance, not a cutoff.** It shows time on task against the
intended length, warns softly ten minutes before and again at the limit,
and never locks anything: the participant ends the task themselves. The
length is **the study's own** — *Length of the main task* in the study's
setup (leave it empty for the server's default: `ELENCHUS_TASK_MINUTES`,
else 60). That is how a `TRAINING` study runs a five-minute task beside
the real study's sixty, with nobody touching the server. Set it before
the first participant starts; it is recorded in the export's
`study_config.json`, and every change is logged.

**Finishing submits the text.** An empty text can't be submitted, a very
short one asks for confirmation, and there is no route to the
questionnaires that skips the text — so every finished session has
something for the panel to rate. The first submission stands.

## 4. Questionnaires

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

## 5. What is captured

Because the formal analysis is done offline, **anything not recorded
during a session cannot be recovered afterwards.** Each session records:

- **The text**: the submitted version, *and* every distinct autosaved
  draft with its timestamp — so the text's growth can be set against the
  dialogue.
- **Editor events**: pastes (by **length and time only — never content**)
  and each soft time warning shown.
- **Every exchange with the LLM**, in both conditions: the participant's
  message, exactly what the LLM was shown, its **verbatim** output, which
  parse-recovery path was used, the dialectical state before and after,
  the system prompt's name and hash, and model / latency / tokens.
  Exchanges whose LLM call *failed* are recorded too.
- **Every change to the position**: commits, denials, retractions,
  refinements, tensions proposed / accepted / contested — each with its
  time, the turn that caused it, whether it came from the opponent or a
  button in the interface, what it overwrote, and whether it was applied,
  a no-op, or dropped.
- Session lifecycle, questionnaires, and usage / integrity metrics.

The integrity report's `capture.uncaptured_assistant_turns` should be `0`
for every study session; anything else means an exchange is missing from
the log.

## 6. Blinded rating

The panel gives each text **absolute** ratings — texts on different topics
can't sensibly be compared head to head, so there is no pairing.

1. An **admin** creates the judges' accounts (Invites tab, kind `judge`).
   A researcher can assign work to judges but not create them.
2. The researcher opens **STUDY → Judging**, picks a judge, and presses
   **Assign all submitted texts**. It is safe to press again as more
   sessions finish — only texts the judge doesn't already have are added.
   Assign every text to several judges for inter-rater reliability.
3. Each **judge** logs in (email + password) to a queue of texts **in
   their own random order**. For each they see the topic the writer was
   given, its brief, and the text — and **nothing about the condition,
   the participant, the session, or even the text's id**. They rate
   1–7 on:

   > Coverage · Correctness · Concision · Reasoning holds together

   with an optional note, and — to validate the blind — a guess at which
   way of working produced the text, with a confidence rating. If the
   panel guesses at chance, blinding held.

A judge may reopen a text and revise; every submission is kept and the
newest counts. The rubric is versioned (`text_judging.py`) and its version
is stamped on every rating — **don't change its wording mid-study**, and
bump `RUBRIC_VERSION` if you change it between studies.

Researchers deliberately can't rate, and the researcher's view of
submitted texts is metadata only: reading the texts is the panel's job.

## 7. Export the data

When the study is done — or at any point, for a progress snapshot — export
it (**Study → Export**, or `POST /api/admin/study/{study_id}/export`).
This writes two things:

- **The archive** — `$ELENCHUS_DATA/exports/study-{id}-{ts}.tar.gz`:

  | File | Contents |
  |---|---|
  | `manifest.json` | What was exported, and any sessions that failed |
  | `study_config.json` | The study's topics and gap |
  | `participants.json` | Codes and allocations — **no names** |
  | `text_judging.json` | **Unblinded** analysis set: each text's condition / participant / period beside every judge's ratings (full revision history), plus the rubric wording |
  | `sessions/<id>-<condition>/` | One directory per session: |
  | &nbsp;&nbsp;`session.json` | Lifecycle, topic, `participant_code`, `period`, allocation |
  | &nbsp;&nbsp;`text.json`, `text_snapshots.json`, `editor_events.json` | The submitted text, its draft history, pastes and warnings |
  | &nbsp;&nbsp;`turn_log.json`, `state_events.json` | The capture log |
  | &nbsp;&nbsp;`state.json`, `transcript.json` | Final dialectical state and the conversation |
  | &nbsp;&nbsp;`surveys.json`, `integrity.json` | Questionnaires; usage and content metrics |
  | &nbsp;&nbsp;`base/` | A DuckDB dump of the session's own database |

  Times: the capture log's `at_utc` fields are UTC. Other `*_at` fields
  are naive timestamps in the **server's** time zone, which the manifest
  records as `server_timezone` — run the server in UTC and the question
  never arises.

  People appear only as opaque IDs (`P-001`, `J-001`, `R-001`) and
  participant codes (`P01`). A participant's two sessions have different
  per-session IDs **by design**; `participant_code` is what links them.
  No names, no emails, no session links.

- **The pseudonym map** — `…​.pseudonyms.json`, written *next to* the
  archive, **never inside it**. It links opaque IDs to accounts and
  participant codes to the names you enrolled them under.

> **Keep these apart.** The pseudonym map stays with your
> participant-tracking records and must be **excluded from any public
> deposit** (Zenodo, OSF). The archive alone is safe to share/deposit.

**Downloading.** In the Study tab, *downloads* beside a study lists every
export made for it, newest first. The archive is a link any researcher can
download (`GET /api/admin/study/{id}/exports/{name}`). The pseudonym map
is a **separate link that only an admin sees** (`…/{name}/pseudonyms`); it
asks before downloading and each download is logged at warning level.
Exports also stay on the server, under `{data_dir}/exports/`. Individual
session failures are recorded in the manifest, not fatal.

## Pre-study checklist

Run before the first real participant (full version in [Operations
Runbook §10](OPERATIONS.md)):

- [ ] `elenchus sim` (scripted) passes end-to-end through every role —
      setup, enrolment, both sessions per participant, the panel — including
      the access probes (tenant isolation, single-use links, the early
      second link, no task exit without a text, judge-view blinding).
- [ ] `elenchus sim --driver llm` dress rehearsal against the production
      model: participants and judges complete, cost and p95 latency look
      sane, the panel's condition-guess accuracy is near chance.
- [ ] `RUN_UI_E2E=1 pytest tests/e2e/` passes (real browser).
- [ ] `/healthz` → `llm_configured:true`, `phase_b_enabled:false`.
- [ ] A test participant walks `briefing → tutorial → active`, writes,
      reloads (draft restored, clock continues), finishes; their second
      link refuses until the gap has passed.
- [ ] A test judge rates a text; the export's `text_judging.json` shows it.
- [ ] After any upgrade, a browser that had the old version loads the new
      one on its next visit (the service worker fetches the page
      network-first; verify in Chrome and Safari).
- [ ] Invite + alert emails actually arrive (`EMAIL_BACKEND=smtp`).
- [ ] Backup cron has produced a readable archive.
- [ ] Topics, the baseline prompt, the task instruction, the rubric
      wording and the EEQ wording reviewed and signed off
      ([EEQ packet](eeq-review.md)).
- [ ] DPO / ethics approval in hand for a live launch.

## Legacy: structured reports and paired judging

An earlier design had the LLM distil each session into a uniform
structured report and judges compare matched `elenchus` / `baseline`
report *pairs*. The pilot does not use it — the judged artifact has to be
the participant's own words — but the routes remain
(`generate-report`, `judge-packages`, `judge-assignments`,
`/api/judge/queue`, `/api/judge/assignments/*`), are still tested, and
appear in the Judging tab under a "Legacy" heading. A structured report
can still be useful as an offline analysis aid.

## A worked study design

For a concrete example of designing a different study on top of this
harness — research question, positum, conditions, measures, and analysis —
see the (parked) [ARDS dialectical study design](ards-study-design.md).

## Study API reference

Researcher routes (admin or researcher):

| Method & path | Purpose |
|---|---|
| `GET /api/admin/study/configs` | Studies that have been set up |
| `PUT` / `GET /api/admin/study/{study_id}/config` | Set / read a study's topics and gap |
| `POST /api/admin/study/{study_id}/participants` | Enrol a person: allocate, issue both links |
| `GET /api/admin/study/{study_id}/participants` | Roster, session status, cell balance |
| `POST /api/admin/study/sessions/{session_id}/interrupt` | Close an abandoned session |
| `POST` / `GET` / `DELETE /api/admin/study/tokens[/{token}]` | Issue / list / void a single link (test links, one-offs) |
| `GET /api/admin/study/judges` | Judge accounts |
| `GET /api/admin/study/{study_id}/texts` | Submitted texts (metadata) and panel progress |
| `POST /api/admin/study/{study_id}/text-assignments` | Assign texts to a judge |
| `GET /api/admin/study/surveys` | Cohort questionnaire view |
| `POST /api/admin/study/{study_id}/export` | Export |

Participant routes:

| Method & path | Purpose |
|---|---|
| `POST /api/study/{token}` | Open (or resume) a session; `409` if a second link is opened early |
| `GET /api/study/session` | Current session, topic, clock |
| `POST /api/study/session/begin-tutorial` · `begin-task` | `briefing → tutorial → active` |
| `GET` / `PUT /api/study/session/text` | Load / autosave the draft |
| `POST /api/study/session/text/events` | Editor events (paste length, warning shown) |
| `POST /api/study/session/finish` | Submit the text; `active → post_session` |
| `POST /api/study/session/advance?to_state=` | Later transitions |
| `GET /api/study/instruments` · `POST /api/study/session/{id}/survey` | Questionnaires |

Judge routes (admin or judge):

| Method & path | Purpose |
|---|---|
| `GET /api/judge/rubric` | The rubric |
| `GET /api/judge/texts` | The judge's queue |
| `GET /api/judge/texts/{assignment_id}` | One blinded text |
| `POST /api/judge/texts/{assignment_id}/rate` | Submit or revise a rating |
