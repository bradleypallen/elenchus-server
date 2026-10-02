# Conformance to the Study's Stage 1 Registered Report

What the platform implements of the experimental design in the study's
**Stage 1 Registered Report** manuscript (draft dated 2026-09-27), what it
implemented differently when the comparison was made, and what was done
about each difference. The comparison was made against **Elenchus 0.8.4**
on 2026-09-27, section by section against the code; the differences were
then built out, on the manuscript's terms, and released as **0.9.0 and
0.9.1 the same day**, ahead of the training of 2026-10-01. Section numbers
below are the manuscript's.

The point of the exercise: a Registered Report freezes the method before
data are collected, and the platform *is* most of the method. Every place
where the two disagreed had to be settled one way or the other before
registration — by changing the code, or by changing the text so that it
describes what the code does. §7 records how each was settled, and what
the manuscript should now say.

Like everything in this directory this is reference material, not a plan
of record.

## Status (as of 0.9.1, 2026-09-27)

| | Item | Resolution |
|---|---|---|
| D1 | Judges see pairs and rank them | **Built.** Pair assignment, A/B per (participant, judge), ranking. Platform migration 0020. |
| D2 | A justification per dimension | **Built.** Four required sentences per text; rubric version 2. |
| D3 | Condition guess after all assessments | **Built.** A separate guessing pass, open only when the judge's queue is done. |
| D4 | Seeded, deposited, concealed allocation | **Built** (the PI chose to build rather than amend). Platform migration 0021. |
| D5 | What each inference call records | **Built.** Returned model id, request id, temperature, `max_tokens`; versions in the manifest; models seen per session. Base migration 0005, platform 0017. |
| D6 | End of session | **Built**, with the cutoff settled as a **hard stop** at the task length; bases frozen once their session moves on. Platform migration 0018. |
| G1 | Maximum gap | **Built**, as a flag (roster and export), not a gate. Platform migration 0019. |
| G2 | Protocol deviation log | **Built.** Researcher- and platform-logged; in the roster and the export. Migration 0019. |
| G3 | Screening covariates | **Built**, in the platform (the PI's choice). Migration 0019. |
| G4 | EEQ's five scales | **Open** — waits for the pilot's revision of the instrument. |
| G5 | Condition-specific briefing sentence | **Built.** |
| G6 | Analyst's A/B-coded labels | **Open** — the analysis script's job; the export stays unblinded. |
| G7 | Versions in the manifest | **Built** (with D5). |

§2–§4 below are kept as the record of what the comparison found in
0.8.4; each item in §3 ends with what was built. The layout fix of 0.9.1
(both texts reachable while ranking a pair) belongs to D1.

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

## 3. Deviations that bear on the analysis (as found in 0.8.4)

Where the manuscript and the platform disagreed and the disagreement
touched a registered analysis. For each: what the manuscript says, what
the platform did at 0.8.4, the options that were open, and — in
*Resolved* — what 0.9.0 built. **D1–D3 were one decision**: the judging
procedure.

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

*Resolved (0.9.0).* Option (a). `text_pair_assignments` (one per
participant × judge, random queue position, `label_a_text_id` /
`label_b_text_id` drawn per pair), the per-text ratings under each pair,
`text_pair_rankings` (every submission kept, the newest counts); routes
under `/api/judge/pairs`; a pair is assignable once both of a
participant's texts are in; `text_judging.json` carries `pairs` with the
label map and the rankings. The ranking has no confidence of its own —
the manuscript attaches confidence to the guess, not the ranking. 0.9.1
relaid the page so both texts stay reachable while ranking.

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

*Resolved (0.9.0).* Four justification fields, required, one sentence
each (`text_ratings.justifications`, `validate_justifications`);
`RUBRIC_VERSION` is `2`; the Guide for Judges says why the sentences
matter.

### D3 — When the condition guess is asked (§2.6)

*Manuscript.* "After all assessments are complete", per text, with a
confidence.

*Platform.* On each text's rating form, under the ratings.

*Options.* A separate guessing pass once a judge's queue is empty
(server: a `guesses` step; UI: a second list), or amend the text to say
the guess is collected per text at rating time. The manuscript's order
is the safer one for the ratings; the platform's is simpler. Decide
with D1, since a pairs view changes what "all assessments" means.

*Resolved (0.9.0).* The manuscript's order. The rating form carries no
guess; `GET|POST /api/judge/guesses` opens only once every pair in the
judge's queue is rated and ranked, and asks per text (as pair + label)
with a confidence 1–7; `text_condition_guesses` keeps every submission.

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

*Resolved (0.9.0).* Option (b), built as described: a seed set once per
study (`POST /api/admin/study/{id}/allocation-seed`, before the first
block enrolment, meant for a team member who won't administer sessions;
the platform records who), the list `allocation_list(seed, n)` —
permuted blocks of four from `random.Random(seed)`, hashed over the
sequence letters A–D — each participant's cell `cell_at(seed,
block_index)`, concealed (no cell, no links, not in the balance) until
`POST …/participants/{pid}/schedule` reveals it and issues both links.
The seed never leaves the server through the API; the export's
`allocation.json` carries the seed's hash, the list's hash and each
participant's sequence letter; the names-key side file carries the seed.
Without a seed (the practice study) enrolment draws and reveals at once.

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

*Resolved (0.9.0).* Built as proposed: `turn_log` and `usage` carry
`response_model`, `request_id`, `temperature` and `max_tokens`; the
temperature is sent explicitly on every call (`ELENCHUS_TEMPERATURE`,
default 1.0); `integrity.json` lists a session's `requested_models` and
`models_seen`; the export manifest's `versions` block is the "frozen as"
table (Elenchus, pyNMMS, DuckDB, platform schema, rubric, export format).

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

*Resolved (0.9.0).* The PI settled the cutoff as a **hard stop**: at the
task length the editor locks, the last saved draft is submitted as the
text (even empty — logged, and recorded as a `timed_out` deviation) and
the session moves on; `GET /api/study/session` ends an overdue session,
FINISH after the limit is a timeout, and every route that changes the
task or its text refuses after the limit. A study base is frozen once its
session leaves the state that uses it, so the archived base is the state
at submission and is never edited afterwards. `study_texts.submitted_by`
records whether the participant or the clock submitted. The export is
still taken by the researcher rather than at the moment of submission —
which the freeze makes equivalent (see §7).

*Corrected (0.9.5).* The freeze had a hole on the staff side: an admin
bypassed the ownership check, and the admin home list opened a session of
the admin's own on every base, which the freeze then mistook for the
study session — so an archived base took edits again once an admin had
opened their home page. No real session had been run. Only the owner now
reaches a dialectic, the study session is found by its token, and a
study record is never changed or deleted by anyone but its participant
while their session is on it. See
[the content-access note](content-access-policy.md).

## 4. Smaller gaps (as found in 0.8.4, with what was done)

| | Manuscript | Platform at 0.8.4 | Suggested | Done (0.9.0) |
|---|---|---|---|---|
| **G1** | §2.1 sessions at most `[Set: 21]` days apart; pairs outside the window excluded in a sensitivity analysis | Only the minimum gap exists | A `max_gap_days` on the study, shown as a warning in the roster and a flag in the export — not a gate | **Yes**: `study_configs.max_gap_days` (default 21, 0 = none); `_pair_window` reports *straddled* / *closed* in the roster; in the export |
| **G2** | §2.4 protocol deviations logged by the administrator at the time, before judging; §2.7 excluded in a named sensitivity analysis | `Close as interrupted` and a free-text `notes` per participant | A per-session deviation record (kind, note, timestamp, who) in the Study tab and the export | **Yes**: `session_deviations`; *Log deviation* in the Study tab; *Close as interrupted* and the clock's hard stop log their own; `deviations.json` study-wide and per session |
| **G3** | §2.2 screening covariates: ontology-engineering experience, prior LLM-tool use, topic nomination | Not captured | Either outside the platform, joined on the participant code (which the export carries), or three fields on the participant row | **Yes**, in the platform: coded levels for ontology-engineering experience and prior LLM-tool use, a flag for topic nomination, entered at enrolment, exported beside the code |
| **G4** | §2.3, §2.7 EEQ with five named scales (epistemic agency, novelty, completeness, traceability, cognitive flow), Holm-corrected across the five | Eight items, no subscale mapping in the code | The instrument is expected to change after the pilot; whatever it becomes needs its scale structure in `questionnaires.py` so the export can carry it | **Open** — after the pilot |
| **G5** | §2.3 briefs identical apart from the description of how the model behaves | The welcome page is identical and describes neither mode; participants learn the mode in the tutorial | Acceptable as is, or one condition-specific sentence on the welcome page — a text change | **Yes**: one sentence, by condition |
| **G6** | §2.7 the analyst works from A/B condition labels until the confirmatory test has run | `text_judging.json` is unblinded by design | Do the coding in the analysis script, or add a coded variant of the file with the key beside the names key | **Open** — the analysis script's |
| **G7** | §2.8 "frozen as": release tag, pyNMMS version | Not in the manifest | Part of D5 | **Yes**, with D5 |

## 5. Outside the platform, by design

Named so nobody looks for them in the code: the ground-truth coverage
checklist and its two coders; the LLM judges and their batch; the
drift-monitoring probe set (`scripts/run_dialectic.py` drives an LLM
respondent through a dialectic and is the nearest existing tool, but
nothing scores tension-proposal or spurious-contradiction rates); screen
recording; the comparative interview; the model-authored share
(computable from `text.json` and `transcript.json`); the analysis
scripts; ethics, consent and compensation.

## 6. What was done, in the order it was done

All on 2026-09-27, each as its own pull request, merged on green and
released as 0.9.0 (then 0.9.1 for the pair-page layout):

1. **D5** — inference identity and versions (#19).
2. **D6** — the hard stop, bases frozen after the task, `submitted_by`
   (#20).
3. **G1, G2, G3, G5** — maximum gap, deviation log, screening covariates,
   the briefing sentence (#21).
4. **D1–D3** — pair judging, justifications, the guessing pass; the
   Guide for Judges rewritten (#22); the layout of the pair page (#25).
5. **D4** — seeded, concealed allocation (#23).

Every migration involved is additive (platform 17–21, base 5); the
deploy rehearsed them on a copy of the live data first.

## 7. Decisions taken, and what the manuscript should now say

The three choices the platform could not make itself were put to the PI
on 2026-09-27; all three went the way the note recommended:

- **Cutoff (§2.4).** A hard stop at the task length. The manuscript's
  `[Set: confirm cutoff]` can read: *the session ends at the time limit:
  the text is submitted as it stands and the dialogue state at that
  moment is the archived state.* The sentence "The export will be taken
  automatically at that moment and not edited afterwards" is true in
  substance — the base is frozen at that moment and cannot be edited
  afterwards — but the export file itself is produced by the researcher
  later; a wording such as *frozen at that moment and exported without
  alteration* describes what the platform does.
- **Allocation (§2.1).** The deposited-seed design, built. The
  manuscript's description now matches the platform, with one detail to
  add: the "team member who does not administer sessions" enters the
  seed **into the platform**, which generates the list and shows the two
  hashes to deposit; the list itself is regenerable from the seed
  (`study_enrolment.allocation_list`).
- **Screening covariates (§2.2).** Recorded in the platform, as coded
  levels (ontology-engineering experience: none / some / extensive;
  prior LLM-tool use: none / occasional / regular; nominated a topic:
  yes / no). If the manuscript's screening questionnaire uses other
  levels, either the questionnaire or `pdb.SCREENING_LEVELS` should be
  brought into line before the pilot.

Values the platform now assumes for the manuscript's other `[Set:]`
fields that touch it: maximum gap **21 days** (§2.1, flagged not
enforced); judges' justifications **one sentence per dimension**
(§2.5); the guess confidence on a **1–7** scale (§2.6).

Still open: **G4**, the EEQ's five scales — whatever the pilot makes of
the instrument needs its scale structure in `questionnaires.py` so the
export carries it; and **G6**, the analyst's A/B coding — the export is
deliberately unblinded, and the coding belongs in the analysis script
that §2.7 says will be deposited before ratings are unblinded.
