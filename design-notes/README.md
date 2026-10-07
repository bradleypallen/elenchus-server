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
open cohort rather than another feature. It also designs **LLM accounts**:
named, encrypted provider accounts chosen per call (person, else cohort,
else study, else the server default), so that two funders or a sponsored
workshop can share an instance with spend, budgets and reconciliation kept
per account — including a person bringing their own key, and what holding
someone else's key obliges. Also: why dialectic names must
stop being global, how to run a changing prototype beside a study that
needs the protocol frozen, and a suggested order.

Not in the ROADMAP.

### [Conformance to the study's Stage 1 Registered Report](registered-report-conformance.md)

The study's Registered Report manuscript read section by section against
Elenchus 0.8.4: what the platform implements as described (the crossover,
the counterbalanced allocation, the washout, the identical editor and
brief, the instruments, the capture and export), six deviations that bear
on a registered analysis (judges rating pairs with a ranking, a
justification per dimension, when the condition guess is asked, seeded and
concealed allocation, what each inference call records, and the end of
session), seven smaller gaps, what is outside the platform by design, a
proposed order, and the decisions the platform is waiting on. Not in the
ROADMAP; the point is to settle each disagreement, in the code or in the
text, before registration.

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

### [Who may see or change a dialectic](content-access-policy.md)

A decision record. Asked whether an admin could read ordinary users'
dialogues, the code turned out to let an admin read, write and delete
every dialectic — and the admin home list quietly unfroze archived study
records. The note sets the policy (access to someone else's dialogue is
explicit, read-only, logged and purpose-stated; nobody but the owner
writes; study records are frozen for everyone), the practices behind it,
the alternatives rejected, the four work packages (the first built in
0.9.5) and the rules for whoever adds a route. The user-facing statement
is [docs/data-access.md](../docs/data-access.md).

### [The text as the positum](text-as-positum.md)

A design note on a problem in the study task: the dialogue and the
written introduction sit side by side with nothing connecting them, and
the two conditions are asymmetric about the deliverable (the baseline
assistant is told about the text; the opponent is not; neither sees it).
Proposes that the participant's first draft *is* the positum in both
conditions, that the dialogue cannot begin until it is written, and that
every turn thereafter sees the current draft — with, in the Elenchus
condition, changes to the text parsed as speech acts (`source = 'text'`)
so the text is the authoritative statement of the position and the
dialogue the place it is tested. Lists the decisions it forces (where the
position lives; whether the baseline may draft prose; the minimum
positum; the ordinary interface), what building it involves, and what it
means for the registration. Proposed, not built.

### [Architecture vision](architecture-vision.md)

The broader conceptual framing the speech-act and NMMS_Onto extensions sit
within. Covers: the dialectic-as-knowledge-base framing, theory vs case as
the master distinction, use-mode as navigation in the space of reasons,
always-on inferential surface, multi-respondent view-relative endorsement,
in-dialectic LLM evaluation, the LLM's roles in the game of giving and
asking for reasons, and the comparison with Protégé / representationalist KR.

This is the vision that ROADMAP.md's Phase A schema is future-proofed against,
and that Phases B–D progressively realize.
