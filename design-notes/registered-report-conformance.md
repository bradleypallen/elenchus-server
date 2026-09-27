# Conformance to the Study's Stage 1 Registered Report

What the platform implements of the experimental design in the study's
**Stage 1 Registered Report** manuscript (draft dated 2026-09-27), what it
implements differently, and what it doesn't implement — assessed against
**Elenchus 0.8.4** plus the three unreleased fix branches of 2026-09-27,
by reading the manuscript section by section against the code. Section
numbers below are the manuscript's.

The point of the exercise: a Registered Report freezes the method before
data are collected, and the platform *is* most of the method. Every place
where the two disagree has to be settled one way or the other before
registration — by changing the code, or by changing the text so that it
describes what the code does. Nothing here decides which; §6 proposes.

Like everything in this directory this is reference material, not a plan
of record. Two things in the manuscript are still marked `[Set: …]` and
bear on the platform directly (the session cutoff and the maximum gap);
they are called out where they arise and collected in §7.

## 1. Where the manuscript's design lives in the platform

| Manuscript | Platform |
|---|---|
| §2.1 conditions, crossover, counterbalancing | `study_enrolment.py` (the 2×2 cells, permuted blocks), `study_participants` / `participant_session_tokens` (platform migration 0010), the message route's baseline switch |
| §2.1 washout | `pdb.second_session_gate`, checked when a second link is first opened |
| §2.3 platform, baseline mode, text editor | one release; `<WritingPane>` in both conditions; `baseline_system_prompt` |
| §2.3 instruments | `questionnaires.py` (`nasa_tlx`, `sus`, `trust`, `eeq`) |
| §2.4 procedure | the session state machine (`briefing → tutorial → active → post_session → surveyed → complete`), `study_configs.task_minutes`, the soft reminders |
| §2.4–2.5 artifacts | `study_text.py` (drafts, submission), `turn_log.py` (raw output, prompt hash, state before/after), `state_events` (the move log), the per-base DuckDB file (the material base), `study_export.py` |
| §2.6 judging | `text_judging.py` (rubric, blinded view), `text_assignments` / `text_ratings` (platform migration 0011) |
| §2.8 logging | `turn_log`, `usage` |

## 2. Implemented as described

Each row was checked in the code and, where marked, in a browser
walk-through of both conditions on 2026-09-26.

| Manuscript | What the platform does | Checked |
|---|---|---|
| §2.1 Two-period, two-condition within-subjects crossover; every participant does both | One participant row, two tokens with `period` 1 and 2 and opposite conditions | code |
| §2.1 Same model in both conditions; baseline is a separate mode of the same application | One `Opponent` / `LLMClient`; the message route dispatches on the session's condition | code, browser |
| §2.1 Interface of comparable visual design; the model supplies no content in either | Same layout, editor, brief, clock and reminders in both; the only differences are the two position/sequents badges and the message-box placeholder (and the pane leak, fixed on `fix/baseline-pane-leak`) | browser |
| §2.1 Task identical in both; topic + brief; two or three paragraphs, own words, no reference material | The writing pane's brief text; the topic title and brief copied onto the token at enrolment | browser |
| §2.1 Four sequences: condition order × topic assignment | `ALL_CELLS` is exactly that 2×2 | code |
| §2.1 Permuted blocks of four in enrolment order | `next_cell`: each block of four holds each cell once | code, tests |
| §2.1 Sessions at least 48 hours apart | `min_gap_hours` (default 48), enforced at the second link's first opening; the participant is told when it opens, in their time zone | code, tests |
| §2.1 Participants cannot be blinded; judges blinded by neutral labelling | The judge view carries topic, text and the form only; the sim's `blinding_no_leak` probe and `test_view_is_blinded` guard it | code, tests |
| §2.3 Session length 60 minutes, revisable after the pilot | `ELENCHUS_TASK_MINUTES` default 60; per-study `task_minutes` overrides without a release | code |
| §2.3 Instruments after every session | Four instruments, presented immediately after submission, stored per session | code, browser |
| §2.4 Topic revealed at briefing/tutorial, not before | Shown when the main task starts; the tutorial uses a practice topic | browser |
| §2.4 One plain text editor, open throughout, submitted at the end | Autosaved append-only drafts; a `submit` snapshot; `post_session` refuses to advance without a text | code, browser |
| §2.4 Two artifacts per Elenchus session: text and material base, the base built move by move | The per-base DuckDB file is the dialogue state; `state_events` records every accept, contest, retract and commitment with `source` and timestamp | code |
| §2.5 Four holistic dimensions, 1–7: coverage, correctness, concision, reasoning coherence | `text_judging.DIMENSIONS`, `RUBRIC_VERSION` stamped on every rating | code |
| §2.6 Fully crossed panel; each judge in their own order; judging separate from construction | Assignment defaults to every submitted text; random `position` per assignment; a judge cannot hold a research role, and a judge with assignments cannot be re-roled | code, tests |
| §2.6 Condition guess with confidence | In the rubric block, the only place the condition vocabulary appears | code |
| §2.6 Judging begins after all sessions | Researcher-controlled; nothing forces early assignment | — |
| §2.7 Analysis from archived data | The export: `text.json`, `text_snapshots.json`, `transcript.json`, `turn_log.json`, `state_events.json`, `base/` (full DuckDB dump), `surveys.json`, `integrity.json`, `text_judging.json`; pseudonymized, names key separate | code, tests |
| §2.8 Access path: the provider's first-party API | Default endpoint is the provider's; no intermediary unless configured | code |
| §3 Pilot: "each archived export and move log reproduces the session's final dialogue state" | `state_events` is written inside the same transaction as the turn; `integrity.json` reports `uncaptured_assistant_turns` | code, tests |

## 3. Deviations that bear on the analysis

Where the manuscript and the platform disagree and the disagreement
touches a registered analysis. For each: what the manuscript says, what
the platform does, and the options. **D1–D3 are one decision**: the
judging procedure.

### D1 — Judges see pairs and rank them (§2.5, §2.6)

*Manuscript.* For each participant the two finished texts are presented
as "Text A" and "Text B", each with its topic name; A/B is randomized
per participant and per judge, and the order of participants per judge.
For each pair the judge gives a **comparative ranking**. The ranking is
a registered secondary analysis (sign test on the per-participant
majority preference) and the **fallback primary outcome** if the
reliability gate fails and fewer than two dimensions qualify.

*Platform.* Texts are rated singly, in a random order that ignores which
participant they belong to; there is no pair, no A/B label and no
ranking. This was a deliberate 0.4-era decision ("texts on different
topics aren't comparable head to head, so there is no pairing"); the
paired machinery that exists (`judge_packages`, migration 0006) is for
the old LLM-generated reports and is labelled legacy.

*Options.* (a) Build pair presentation: assignments grouped by
participant, an A/B label drawn per (participant, judge), a ranking
field per pair with its own confidence, `text_judging.json` carrying the
label map. Roughly the size of the 0.4 judging work. (b) Amend §2.5–2.6
to drop the ranking; the fallback then ends at the reduced composite.
Since the ranking is the safety net for the primary outcome, (a) is the
faithful choice.

### D2 — A justification per dimension (§2.5, §2.6)

*Manuscript.* Each of the four ratings comes with a one-sentence
justification; the pilot reads the justifications for style cues and
revises the rubric if it finds them.

*Platform.* One optional free-text field per text ("What most shaped
your ratings?").

*Options.* Four justification fields, required, stored beside the
ratings; `RUBRIC_VERSION` bumped and `docs/judge-guide.md` updated.
Small. Or amend the text to "a justification for the text as a whole",
which weakens the pilot's style check.

### D3 — When the condition guess is asked (§2.6)

*Manuscript.* "After all assessments are complete", per text, with a
confidence.

*Platform.* On each text's rating form, under the ratings.

*Options.* A separate guessing pass once a judge's queue is empty
(server: a `guesses` step; UI: a second list), or amend the text to say
the guess is collected per text at rating time. The manuscript's order
is the safer one for the ratings; the platform's is simpler. Decide
with D1, since a pairs view changes what "all assessments" means.

### D4 — Allocation: seeded list, deposited, concealed (§2.1)

*Manuscript.* The allocation list is generated once from a seeded random
number generator by a team member who does not administer sessions; the
seed and a hash of the list are deposited with the registration; a
participant's sequence is revealed to the session administrator only
when their first session is scheduled.

*Platform.* The cell is drawn at enrolment (`random.SystemRandom`), so
there is no seed to deposit and the list is not reproducible; the
enrolling researcher sees the cell, both conditions and both topics at
once. Within a block the next draw cannot be predicted until the last
place, which is the manuscript's balance property but not its
concealment property.

*Options.* (a) Amend the manuscript to describe the platform: permuted
blocks drawn at enrolment by the platform's cryptographic RNG, cell
recorded with the enrolment, no pre-generated list. Honest and adequate
for a within-subjects design, where allocation affects order and topic,
not who gets treatment. (b) Build the deposited-seed design: import a
pre-generated list (or generate one in the platform from a seed entered
by the non-administering member), keep each participant's cell hidden
from the Study tab until a *schedule session 1* action, and export the
list's hash. Moderate; also changes the runbook's enrolment steps.

### D5 — What each inference call records (§2.8)

*Manuscript.* Every call records the **returned** model identifier, the
request identifier, timestamp, sampling parameters and prompt hashes.
Straddled pairs (a participant whose two sessions returned different
identifiers) are detected from the response logs. Frozen parameters
include temperature, max tokens, prompt hashes, the platform release tag
and the pyNMMS version.

*Platform.* `turn_log` records the *requested* model name, `at_utc`,
tokens, latency, attempts and the system prompt's name and SHA-256. It
does not record the response's own `model` field or the request id;
temperature is neither set nor recorded (the provider's default
applies); `max_tokens` is a constant (2000) that is not written
anywhere. The export manifest records the export format version, not
the platform release or the pyNMMS version.

*Options.* Build: capture `response.model` and the request id from the
SDK response into `turn_log` (base migration 0005) and `usage`; set and
record temperature and `max_tokens`; write `elenchus.__version__` and
`pynmms.__version__` into the export manifest and `integrity.json`.
Small, and it is what makes §2.8's straddled-pair rule and "frozen as"
table checkable at all. No sensible amendment on the manuscript side.

### D6 — End of session: automatic export, no later edits, cutoff (§2.4)

*Manuscript.* The exported base is the dialogue state "at the moment the
participant submits the finished text, or at the session time limit if
that comes first `[Set: confirm cutoff]`"; taken automatically and not
edited afterwards.

*Platform.* There is deliberately no cutoff: the clock and its two
reminders are guidance, and the participant finishes when they press
the button. Export is on demand by the researcher, later; the state at
submission is reconstructible offline from `state_events` timestamps and
the `submit` snapshot, but the base is not frozen — nothing refuses a
mutation after `post_session`, and the participant's session cookie is
still valid during the questionnaires.

*Options.* Settle the `[Set:]`: either "at submission" (matches the
platform) or a hard cutoff (a change to the pane and the finish route,
against the design's own "nothing is cut off"). Either way, freeze the
task base at finish — refuse turns and text saves on a base whose
session is past `active` — so "not edited afterwards" is enforced rather
than assumed. Small.

## 4. Smaller gaps

| | Manuscript | Platform | Suggested |
|---|---|---|---|
| **G1** | §2.1 sessions at most `[Set: 21]` days apart; pairs outside the window excluded in a sensitivity analysis | Only the minimum gap exists | A `max_gap_days` on the study, shown as a warning in the roster and a flag in the export — not a gate |
| **G2** | §2.4 protocol deviations logged by the administrator at the time, before judging; §2.7 excluded in a named sensitivity analysis | `Close as interrupted` and a free-text `notes` per participant | A per-session deviation record (kind, note, timestamp, who) in the Study tab and the export |
| **G3** | §2.2 screening covariates: ontology-engineering experience, prior LLM-tool use, topic nomination | Not captured | Either outside the platform, joined on the participant code (which the export carries), or three fields on the participant row |
| **G4** | §2.3, §2.7 EEQ with five named scales (epistemic agency, novelty, completeness, traceability, cognitive flow), Holm-corrected across the five | Eight items, no subscale mapping in the code | The instrument is expected to change after the pilot; whatever it becomes needs its scale structure in `questionnaires.py` so the export can carry it |
| **G5** | §2.3 briefs identical apart from the description of how the model behaves | The welcome page is identical and describes neither mode; participants learn the mode in the tutorial | Acceptable as is, or one condition-specific sentence on the welcome page — a text change |
| **G6** | §2.7 the analyst works from A/B condition labels until the confirmatory test has run | `text_judging.json` is unblinded by design | Do the coding in the analysis script, or add a coded variant of the file with the key beside the names key |
| **G7** | §2.8 "frozen as": release tag, pyNMMS version | Not in the manifest | Part of D5 |

## 5. Outside the platform, by design

Named so nobody looks for them in the code: the ground-truth coverage
checklist and its two coders; the LLM judges and their batch; the
drift-monitoring probe set (`scripts/run_dialectic.py` drives an LLM
respondent through a dialectic and is the nearest existing tool, but
nothing scores tension-proposal or spurious-contradiction rates); screen
recording; the comparative interview; the model-authored share
(computable from `text.json` and `transcript.json`); the analysis
scripts; ethics, consent and compensation.

## 6. Proposed order

Everything here is after the training of 2026-10-01 (see the freeze) and
before the pilot, except where noted.

1. **D5** (logging) — smallest, no UI, and the pilot's exports should
   already carry it so that the pilot can confirm the "frozen as" table.
2. **D6** freeze-at-finish, and the cutoff sentence settled in the text.
3. **D1–D3** as one piece of judging work, once the PI has chosen (a) or
   (b) for D1 — the pilot's judges are the first to use it, and the pilot
   is where the rubric gets revised, so it must exist before the pilot.
4. **G1, G2** — small, Study tab and export.
5. **G4** — after the pilot, when the EEQ is final.
6. **D4** — a manuscript amendment unless the deposited-seed design is
   wanted, in which case it is the largest item and should be decided
   early.

G3, G5 and G6 are text or analysis-script matters unless the PI prefers
them in the platform.

## 7. Decisions the platform is waiting on

- The session cutoff `[Set: confirm cutoff]` in §2.4 (D6).
- The maximum gap `[Set: 21]` days in §2.1 (G1).
- Pairs and ranking: build or drop (D1); with it, the guess timing (D3).
- Allocation: describe the platform's draw, or build the deposited-seed
  list (D4).
- Whether screening covariates live in the platform (G3).
