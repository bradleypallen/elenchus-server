# ARDS dialectical study — design plan (PARKED)

Status: **parked** pending (a) the ARDS gold-standard codebook and (b) a
few open design decisions below. The dialogue machinery needed to run it
already exists; the remaining work is mostly scholarship + post-hoc
coding, not engineering. Resume from "Open decisions" and "Build."

## Research question

Can an LLM, as the *respondent* in an Elenchus dialectic, participate in
the game of giving and asking for reasons (GOGAR) in a way that tracks
**reasons** rather than **recall** or **conversational pressure** — at a
level comparable to a domain expert, measured against a documented
record of competent practice?

This operationalizes the open question the stop-sign note leaves
(*"Instrument for Probing LLM Endorsement of Material Inferential
Rules"*, Remark 8): the endorsement instrument reaches only the
*committive* dimension (endorsement-when-asked); the dialectic reaches
the *entitlement* dimension (commitment assessed against the agent's
other commitments, sustained under challenge). The study is the
sustained-challenge protocol the note gestures at.

## Why the dialectic, not a challenge battery

A form that presents "positum + a challenge → accept/contest + justify"
is just the note's verification-prompt instrument with a text box — it
measures isolated endorsement-with-reasons and discards the very thing
the dialectic adds (accumulating commitments, tensions drawn from the
respondent's *own* prior commitments, defense/withdrawal under sustained
challenge). **Humans therefore engage the positum in the standard
server-supported manner** (the open dialectic), not a battery. This is a
deliberate trade of experimental control for the validity that makes
this a study of GOGAR participation rather than a re-run of the
endorsement probe.

## Domain & positum

- **Domain:** the ARDS (Acute Respiratory Distress Syndrome) diagnostic
  definition, which has a documented revision lineage — AECC (1994) →
  Berlin (2012) → Global (~2024).
- **Positum:** a fixed excerpt of the **Berlin (2012)** definition prose
  (the core diagnostic-criteria paragraph), analogous to the PROV-O
  "Starting Point Terms" 350-word excerpt in the Elenchus case study.
  *Exact excerpt: TBD (see Open decisions).*
- **Why ARDS over PROV-O:** (1) the experts are *users*, not authors, so
  the human arm genuinely reasons rather than re-articulating its own
  decisions (PROV's respondent was a co-editor); (2) clinical diagnostic
  criteria are the paradigm of *defeasible material inference with an
  explicit defeater* — hypoxemia + bilateral opacities ⊢ ARDS, defeated
  by "fully explained by cardiac failure" — an exact structural twin of
  the stop-sign inference; (3) a documented design-rationale record
  exists, with rejected options and a conceptual/empirical split (below).

## Gold standard (post-hoc adjudicator, not a script)

The opponent generates tensions autonomously; the ARDS record is used
**after the fact** to label whatever it raised and what entered each
base. Construct a codebook from the AECC/Berlin/Global papers + their
consensus-methodology supplements:

- Enumerate the documented design tensions behind the Berlin→Global
  revision (e.g. ABG requirement excludes resource-limited settings →
  SpO₂/FiO₂; PEEP/intubation requirement excludes high-flow patients →
  non-intubated category; CXR/CT → lung ultrasound; AECC's invasive PAWP
  → "not fully explained by cardiac failure").
- Label each **real vs. rejected/spurious** (rejected options = the
  roads not taken; these are the discrimination targets).
- Partition each **conceptual** (coherence/scope — Elenchus-tractable)
  vs. **empirical** (thresholds chosen from cohort data — *not*
  conceptually derivable; recovery of these = recall, not reasoning).

The codebook is the long pole of the whole project.

## Arms & controls

- **Human arm:** n practicing intensivists/pulmonologists (expert
  *users*), each running ONE standard dialectic on the platform from the
  fixed Berlin positum.
- **Model arm:** m models as respondent via `scripts/run_dialectic.py`
  (same standard-manner dialectic), multiple runs per model to average
  opponent stochasticity.
- **Opponent held constant** across both arms (same model + system
  prompt + positum; `enable_phase_b=False` = Elenchus/prover-skeptic
  vocabulary). Only the respondent varies.

## Measures (all post-hoc, off the same standard sessions)

- **DV1 — coverage:** rate at which the final material base recovers the
  documented *conceptual* design tensions (vs. the codebook).
- **DV2 — reasons-responsiveness:** within-respondent κ of accept/contest
  decisions against post-hoc validity labels (concede the real, contest
  the rejected/weak).
- **DV3 — recall probe:** did the respondent's commitments state
  *empirical specifics* (exact thresholds) it could not have derived?
  High recovery flags retrieval, not reasoning.

**Headline framing:** because the domain is partly in model training
data, *parity* (human ≈ model coverage) is the contamination's own
prediction and is weak evidence. Make **human–model divergence + the DV3
recall probe** primary; treat parity as secondary.

## Trade-offs accepted by the standard-flow choice

- Reactive challenge sets: humans and models face *different* opponent-
  generated tensions (drawn from their own commitments) → comparison is
  at the **rate/κ level, not item-matched**.
- DV1 coverage conflates extraction/articulation + reasoning + what the
  opponent surfaced — fine for the end-to-end *participation* question,
  not for isolating reasoning (that's DV2's job).
- DV2-spurious and DV3 are **opportunistic / possibly underpowered**: a
  competent opponent raises mostly *real* tensions, so few clearly-
  spurious items per session. If a pilot shows this is too thin, run a
  **model-only** controlled challenge battery as a follow-up — never put
  the humans through a battery.
- Contamination is harder to isolate in an open flow → lean on divergence
  + DV3.

## What runs now, unmodified

- Human arm: the platform exactly as it stands (session-keyed dialectic,
  opponent, tension accept/contest, accumulating [C:D] + material
  implications, structured report, full traceability).
- Model arm: `scripts/run_dialectic.py` (LLM respondent that defends —
  contests vs. accepts per tension — vs. the real opponent; JSON out).
- Fixed opponent via `ELENCHUS_MODEL`.

The standard-flow decision means **near-zero new server code**.

## Build (minimal, deferred)

1. **ARDS codebook** (scholarship — the long pole). Not code.
2. **Post-hoc coding layer:** map each session's tensions + final base to
   the codebook (real/spurious, conceptual/empirical, accept/contest).
   Could be external (expert coders + codebook) or a light annotation
   tool; the blinded-judging apparatus is reusable if the rubric is
   adapted.
3. **Artifact normalization:** ~small exporter producing a uniform
   per-session record (positum, final [C:D], implications, contested
   tensions, transcript, per-tension verdicts) for *both* arms (platform
   export vs. run_dialectic JSON differ today).
4. **(Optional) model-only controlled battery** — only if DV2-spurious is
   underpowered in the pilot.

## Non-code inputs the team must supply

- The codebook + panel labels on spurious/empirical items.
- Expert recruitment + **IRB** (clinicians as subjects, even for a
  conceptual elicitation, likely needs ethics review).
- Coders/judges for scoring.

## Open decisions (settle before building)

- **Positum:** exact Berlin excerpt + scope.
- **Stopping rule:** run-to-convergence (faithful, variable length, like
  the PROV 7-challenge arc) vs. fixed turn/challenge budget (comparable).
  Lean: convergence, for standard-manner fidelity.
- **Opponent stochasticity:** multiple runs for models; one session per
  human — design around the asymmetry.
- **n, m, runs-per-model.**
- **Tutorial:** do human experts get the platform's tutorial/practice
  phase first? (Probably yes — part of "standard manner.")
- **Headline:** divergence-primary (recommended) vs. parity-primary.

## Suggested phasing

- **Phase 0:** codebook + open decisions + IRB.
- **Phase 1 (pilot):** 1–2 experts + a few model runs, open dialectic;
  check feasibility and DV2-spurious power.
- **Phase 2:** full run (n experts, m models × runs).
- **Phase 2b (conditional):** model-only controlled battery if needed.
- **Phase 3:** post-hoc coding + analysis (divergence, DV1/DV2/DV3).

## Pointers

- Theory: stop-sign note (`Simonelli response/stop_sign_note_arxiv.pdf`),
  Elenchus paper (`Elenchus/elenchus-arxiv-proof.pdf`), PROV-O case study
  (Elenchus §7) + Moreau et al. 2015 as the design-record analog.
- Code: `scripts/run_dialectic.py` (model arm), the session-keyed
  platform (human arm).
