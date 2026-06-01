# Architecture Vision

The broader conceptual framing the speech-act extensions and NMMS_Onto
integration sit within. This is the vision the operational ROADMAP is
future-proofed against: Phase A's data model, API surface, and identity
model are designed so that the moves described here are *extensions*
rather than refactors.

This document is reference material, not a plan of record. It does not
commit to building all of what's described here; it commits to *not
foreclosing* it.

## The core claim

**The dialectic is the knowledge base.** Not a metaphor — a structural
claim. Conventional KR treats the KB as a stored artifact (a file, a
graph database, an ontology) that the practice of knowledge engineering
*produces*. The Elenchus claim inverts this: the KB is the score-keeping
record of an ongoing dialectical practice; the practice is the artifact,
and any static export is a projection of the practice at a moment.

This isn't a vague philosophical commitment — it has specific
architectural consequences:

- Every contribution to the base is a normatively significant speech
  act with a contributor, a timestamp, and a dialectical provenance.
- The base accumulates by speech acts, modifies by speech acts, and is
  queried by speech acts. There is no separate "edit interface" or
  "query language" because everything happens in the protocol.
- Disagreement is structural data, not noise to resolve. The KB carries
  multi-valued endorsement; consensus is one view among many, not the
  privileged final state.
- The KB is constituted by the practice rather than being its output.
  Take the practice away and there's nothing left — no facts hovering
  independent of the moves that asserted them.

Philosophical underwriting: Brandomian inferentialism, specifically the
score-keeping framing in *Making It Explicit* (1994). Meaning is
inferential role; inferential role is constituted by giving-and-asking-
for-reasons practices; the KB is the record of those practices.

## Theory vs case: the master distinction

The most important conceptual move the architecture forces is separating
**theory** (the inferential structure, case-independent) from **case**
(the bilateral position about a specific situation).

- **Theory** lives in the shared base: `L_B` (atomic vocabulary) plus
  `|~_B` (the consequence relation, built up via asserted implications,
  refinements, and tension-derived holdings).
- **Cases** are first-class entities: each case is a named snapshot of
  `[C : D]` for a specific situation, plus the conversation segment that
  produced it.

Within a session, the respondent works through one or more cases
against the shared theory. The theory may evolve under pressure from
the case ("this rule doesn't fire here — let me refine it"), but the
theory's existence is independent of any one case.

Implications for the data model:

- `assessments` (theory) is **not** keyed by case_id.
- `positions` and `tensions` (case content) **are** keyed by case_id.
- Conversation turns carry the active case_id at write time.
- Switching cases swaps which `[C : D]` is live without affecting
  the theory.

This distinction makes several things tractable:

- **Multiple cases per session.** Clinician works through Patient 0,
  saves it, opens Patient 1. Same theory, different positions, different
  forced commitments.
- **What-if exploration.** A hypothetical commitment temporarily pushes
  the position; popping returns to the saved state. The theory is
  unaffected.
- **Cross-case comparison.** `/case diff P0 P1` shows how the same
  theory shapes two different positions differently. Pedagogically
  powerful when teaching defeasible reasoning.

## Use-mode as navigation in the space of reasons

Once theory and case are separated, two engagement modes become visible:

- **Build-mode**: contribute to theory. `ASSERT_IMPLICATION`,
  `REFINE_IMPLICATION`, `INTRODUCE_BEARER`, etc. Modifies `|~_B`.
- **Use-mode**: query the theory against a case. `COMMIT`, `DENY`,
  `RETRACT`. Modifies `[C : D]` without modifying theory.

But these aren't separate UI modes — they're descriptions of which
speech-act pattern dominates at a moment. The same protocol carries
both. The respondent fluidly crosses between articulating a rule and
testing it against a case, with the opponent adjusting its posture
based on the state and recent moves.

The deep claim: **use-mode is the inferentialist analog of
representationalist querying**. Where a representationalist KB query
retrieves facts from storage, an inferentialist KB query pushes a
position and reads off what surrounds it in the space of reasons —
forced commitments, entitlements, conflicts. Brandom would call this
making explicit what the position already commits one to.

The architecture renders this literally: the inferential surface
(entitlements, forced commitments, conflicts) recomputes after every
state-changing speech act. The participant pushes a position, the
surface re-renders, they read off the consequences. Every cell is
click-through to a derivation trace.

Querying isn't a separate operation; it's the act of moving in the
space.

## Multi-respondent view-relative endorsement

Pluralizing the practice — multiple respondents engaging the same shared
base — requires a refinement of what "shared" means. The right structure:

- `L_B` is shared. One discursive vocabulary across all agents.
- The catalog of *engaged* sequents is shared. Anyone's contribution is
  visible to everyone.
- Each respondent's **endorsement** of sequents is private. R1 may
  endorse `{γ} |~ {δ}` while R2 rejects it; both rows coexist in the
  `assessments` table tagged by `contributor_id`.

A respondent's "view" of the base is the projection onto their own
endorsements (plus any explicit ratifications). Derivability is
view-parameterized: `derives(γ, δ, view=R)` filters to R's endorsements
before running the proof search.

This makes inter-respondent disagreement substantive:

1. **Direct contradiction.** R1 endorses `{γ} |~ {δ}`, R2 endorses
   `{γ} |~ {¬δ}`. Under a union view, γ trivializes to ⊥.
2. **Defeater disagreement.** R1 holds `{γ} |~ {δ}` but not `{γ, σ}
   |~ {δ}` (treats σ as defeater); R2 holds both (σ doesn't defeat).
   Same case yields different consequences.
3. **Silent disagreement.** R1 has engaged a sequent and rejected it;
   R2 has never engaged it. R1's view says "actively rejected"; R2's
   says "absent." Behaviorally similar, semantically distinct.

Inter-respondent disagreement isn't a problem to resolve — it's data.
The benchmark export's `analyst_verdicts` aggregation surfaces it as
per-analyst verdicts on each item, with the dispute history attached.

Phase A's schema supports this from the start (`contributor_id` on
every row, view-parameterized derivability default to "all rows" =
single-user case). The multi-respondent extension is a UI and
permissioning addition, not a schema change.

## The LLM's roles in the game of giving and asking for reasons

The LLM is structural infrastructure in Elenchus, not the system's
content. The asymmetry that makes this work: the opponent has no
bilateral position of its own. It is a scorekeeper-with-speech, not a
participant with stakes.

Inventory of roles the same LLM plays:

| Role | What it does |
|---|---|
| **Opponent / dialectical scorekeeper** | Tracks respondent's commitments, proposes tensions, narrates the inferential surface, moderates UI-driven moves. The Socratic asymmetry. |
| **Speech-act parser** | Extracts structured moves from respondent prose. |
| **Trace narrator** | Translates pyNMMS proof traces into engageable narratives. Bridges formal and informal registers. |
| **Articulation partner** | Helps the respondent formulate rules they're reaching for. Speaker-work in service of the respondent's eventual assertion. |
| **Counter-instance generator** | Generates cases that test an asserted rule during dispute. |
| **Vocabulary curator** | Validates atomicity, suggests paraphrase unification, drafts references. |
| **Cross-respondent moderator** | (Multi-respondent.) Surfaces the substance of disagreements, summarizes trajectories, identifies reconciliation paths. |
| **Comparative scorekeeper** | (Multi-respondent.) Surfaces divergence between respondents' views on the same case. |
| **Respondent stand-in** | (Evaluation mode.) Drives the LLM through Elenchus as itself a subject, exporting its verdict pattern for comparison. |
| **Evaluation target** | (Sub-evaluation.) Receives the verification prompt against a sequent; emits GOOD/BAD/ABSTAIN. |

What unifies these: **the LLM bridges formal and informal registers**.
pyNMMS reasons formally; the human respondent thinks in prose; the data
model is structured rows. The LLM is the only entity that translates
between them in both directions. Take it out and the system can derive
and check but can't be *used* by anyone who doesn't write in JSON.

The opponent doesn't have a position. Its outputs enter the base only
as proposals the respondent accepts. This is what makes the LLM's
contributions audit-able rather than authoritative — discursive
presence without scorekeeping authority.

## In-dialectic LLM evaluation

The benchmark format used by infereval is structurally a projection of
the material base: bearers ↔ atoms, items ↔ assessments aggregated by
sequent, analyst verdicts ↔ per-contributor judgments. The two
representations are nearly isomorphic.

This makes export/import unnecessary as a *bridge*: evaluation can
happen *within* the dialectic via an `EVALUATE` primitive that runs the
verification prompt against a sequent currently in the base. The result
(GOOD / BAD / ABSTAIN) is recorded with the LLM as contributor, slotting
into the same multi-valued endorsement structure as human respondents'
verdicts.

What this enables that static export doesn't:

- **Time-series evaluation against evolving theory.** Re-query as the
  theory changes.
- **LLM disagreement as dialectical input.** When the LLM rejects an
  asserted implication, that's pressure the opponent can surface for the
  respondent.
- **Cross-role LLM examination.** The same model evaluated as opponent,
  respondent, and evaluator — checking internal consistency across roles.

Static export survives as a *secondary* feature for external
comparability ("LLM X scored Y on benchmark Z, published as a snapshot")
rather than the primary evaluation mechanism.

Philosophically: the benchmark format becomes a *projection* of the
practice, not an artifact independent of it. The representationalist
assumption that the benchmark is a thing separable from the dialectic
that produced it is dropped. The artifact is the practice; projections
are useful for specific purposes (external comparability, citation,
publication) but ontologically downstream.

## Comparison with Protégé

These tools answer different questions about what a knowledge base is.

| Dimension | Protégé | Elenchus |
|---|---|---|
| Logic | Description Logic (SROIQ); monotonic, classical, open-world | NMMS; defeasible, no-Weakening, no-Cut, bilateral |
| Semantics | Truth-conditional, model-theoretic | Inferentialist, commitment/entitlement |
| Output | OWL file (static, citable) | The dialectical practice itself; benchmark JSON as snapshot |
| Modification | Edit axioms in a GUI | Dialectical speech acts in prose + protocol |
| Reasoner | External; validates on demand | Continuous; computes view-relative derivability after every move |
| Disagreement | Resolved into consensus or branched | Preserved as multi-valued endorsement |
| Query | SPARQL; separate from data | Push a position; surface re-renders. No separate query language |
| Defeasibility | Awkward (DL is monotonic) | Native |
| Hierarchy | Native taxonomic subsumption | None native (express as implications, or NMMS_Onto schemas) |
| LLM integration | Peripheral | Structural infrastructure |
| Expert's role | Informant / co-editor; needs OWL fluency | Respondent; needs only dialectical fluency |

**Protégé's strength**: standardized formats, taxonomic structure,
semantic web infrastructure, scalability of static artifacts. Right tool
for publishing terminologies, biomedical ontologies, anything that needs
SPARQL/OWL interop.

**Elenchus's strength**: defeasibility as first-class, disagreement
preserved structurally, dialectical accountability, no barrier between
building and querying, LLM as accessibility bridge for non-formal
experts.

**Complementarity**: a serious knowledge engineering effort could use
both. Protégé/OWL for the vocabulary layer (standardized, citable,
interoperable); Elenchus/NMMS for the inferential layer (defeasible,
dialectically constructed, multi-respondent endorsed). The bridge:
bearers in `L_B` ground in URI-referenced OWL entities. With NMMS_Onto
integration, the typed vocabulary makes this bridge native.

## Bootstrap: there is no blank slate

The opponent's posture on an empty base shifts to maieutic mode —
eliciting vocabulary and asserted rules rather than probing existing
commitments. This is the Socratic role: the respondent already has the
competence; the dialogue makes it explicit.

The arc is roughly:

1. **Context-setting positum.** Respondent describes the domain and
   inquiry in their own words. Opponent reads this as scene-setting.
2. **Vocabulary elicitation.** Opponent asks for central concepts;
   respondent rattles off candidate bearers; opponent extracts
   `INTRODUCE_BEARER` with aggressive confirmation.
3. **Rule articulation.** Opponent asks for inferential commitments;
   respondent describes defeasible rules; opponent extracts
   `ASSERT_IMPLICATION` with confirmation.
4. **Stress-testing with a case.** Once a small theory exists, the
   respondent commits to a concrete case; rules fire; tensions and
   forced commitments surface; theory evolves under pressure.

No mode flag. The opponent reads `|L_B| == 0` (empty base) and adjusts
posture accordingly. As the base fills, the posture shifts naturally
toward interrogation and use-mode probing. Same protocol throughout.

## Where this all converges

The architectural vision is coherent rather than a grab-bag of features
because the same commitment underlies each piece:

- The KB is the practice → the database is the score-keeping record.
- Speech acts are normatively significant → every state change carries
  provenance.
- Disagreement is data → multi-valued endorsement, view-relative
  derivability.
- Use is querying → continuous inferential surface, no separate query
  language.
- Theory and case are separable → cases as first-class, theory shared
  across cases.
- LLM is infrastructure → opponent has no position, contributions enter
  only as accepted proposals.

Phase A's data model and API surface are designed so each of these can
be realized as additions, not refactors. The current implementation is
the seed; the architecture vision is what it grows into.

## What this isn't committing to

- That all of this will be built. Phases A–D realize a subset; further
  phases would extend.
- That Elenchus replaces Protégé. They answer different questions.
- That the inferentialist framing is the only legitimate KR position.
  It's the one this system embodies.
- That every domain needs this richness. Many KR tasks are well-served
  by representationalist tools; this is for the cases where they aren't.

## Connections to other notes

- [`speech-acts-extensions.md`](speech-acts-extensions.md) — the
  speech-act vocabulary that operationalizes build-mode and the dispute
  lifecycle.
- [`nmms-onto-integration.md`](nmms-onto-integration.md) — the typed-
  vocabulary extension for richer ontology articulation.
- [`../ROADMAP.md`](../ROADMAP.md) — the operational sequencing for what's
  actually being built.
- `prover-derived-challenges-plan.md` in user memory (`.claude/projects/
  -Users-bradleyallen-Documents-GitHub-elenchus-server/memory/`) — a
  specific dialectical move category for when accepted implications
  cause C to derive D. Fits inside this vision but predates it.
