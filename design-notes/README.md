# Design Notes

Architectural and conceptual design notes for Elenchus. These documents capture
thinking from design sessions that may or may not translate into committed
implementation work — they are reference material, not plans of record.

**For operational planning** (what's being built next, in what order), see
[`ROADMAP.md`](../ROADMAP.md) at the repo root.

These notes are decoupled from `ROADMAP.md` on purpose: some of what's here
extends well beyond the work currently sequenced, and some of it is
foundational framing that the sequenced work depends on but doesn't itself
restate. Both stay useful when picked up later.

## Index

### [Speech-act extensions for material base construction](speech-acts-extensions.md)

Design notes on extending the dialectical protocol with respondent-side speech
acts that support direct theory articulation — `ASSERT_IMPLICATION`,
`INTRODUCE_BEARER`, `RETRACT_IMPLICATION`, `REFINE_IMPLICATION`, and
`DISPUTE_IMPLICATION`. Includes the positum-as-ontology simplification, the
implication dispute lifecycle, and connections to the existing tension cycle.

Phase B of the ROADMAP implements a subset of these (the first three plus the
positum simplification); the dispute lifecycle is deferred.

### [A prototype for substructural knowledge bases — gaps from 0.8.2, and an outreach mode](substructural-kb-prototype-and-outreach.md)

What it would take to make Elenchus the interactive prototype for a proposed
follow-on project on substructural knowledge bases, assessed against 0.8.2
claim by claim in the code: a base holds exactly one position (so there is
no "other side" for crux detection, and free-text atoms need alignment
before two vocabularies can be compared); the reasoner is hard-wired to
pyNMMS (a small reasoner interface, a Julia engine as a sidecar); querying
isn't in the web interface at all; the speech-act vocabulary is one
server-wide flag (protocol profiles per dialectic); no interchange format,
no sources, opaque atoms.

Its longest section designs an **outreach mode** — inviting people to use
Elenchus itself, without the study harness — around one new concept, the
*cohort* (a link that seats forty, an LLM allowance with a hard stop that
is never applied to study participants, a welcome, a recorded notice and a
choice about research use), and notes that a public demonstration is an
open cohort rather than another feature. Also: why dialectic names must
stop being global, how to run a changing prototype beside a study that
needs the protocol frozen, and a suggested order.

Not in the ROADMAP.

### [NMMS_Onto integration](nmms-onto-integration.md)

Design notes on extending Elenchus with the NMMS_Onto ontology schemas —
typed vocabulary (concepts, roles, individuals), ABox/TBox split, the seven
defeasible schema types (`subClassOf`, `range`, `domain`, `subPropertyOf`,
`jointCommitment`, `disjointWith`, `disjointProperties`). Includes mapping
to the current data model, new speech acts that would be required, and the
relationship to the simpler propositional-ontology approach already covered
in `speech-acts-extensions.md`.

Not currently in the ROADMAP. Optional upgrade when propositional content
becomes limiting; the propositional approach in Phase B gets ~80% of the value
at ~5% of the cost.

### [Architecture vision](architecture-vision.md)

The broader conceptual framing the speech-act and NMMS_Onto extensions sit
within. Covers: the dialectic-as-knowledge-base framing, theory vs case as
the master distinction, use-mode as navigation in the space of reasons,
always-on inferential surface, multi-respondent view-relative endorsement,
in-dialectic LLM evaluation, the LLM's roles in the game of giving and
asking for reasons, and the comparison with Protégé / representationalist KR.

This is the vision that ROADMAP.md's Phase A schema is future-proofed against,
and that Phases B–D progressively realize.
