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
