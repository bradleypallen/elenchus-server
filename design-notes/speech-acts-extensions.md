# Speech-Act Extensions for Material Base Construction

Design notes on extending the dialectical protocol's speech-act vocabulary
to support direct theory articulation by the respondent, alongside the
existing tension-cycle path. These extensions preserve the inferentialist
character of the protocol — every state-changing move remains a normatively
significant speech act with provenance — while expanding the kinds of
contributions a respondent can make to the material base.

## Motivation

The current protocol has six speech acts (`COMMIT`, `DENY`, `RETRACT`,
`REFINE`, `ACCEPT_TENSION`, `CONTEST_TENSION`). Material implications can
only enter `|~_B` via accepted tensions: the opponent proposes a tension,
the respondent accepts, and the assessment is recorded with
`domain='tension'`. This is dialectically rich but operationally
roundabout when the respondent simply wants to articulate a rule they
hold — they have to wait for the opponent to surface it as a tension and
then accept.

For ontology engineering and conceptual specification tasks, the respondent
often arrives with rules they want to articulate directly. The protocol
should accommodate this without surrendering the inferentialist commitments.

## New speech acts

### `INTRODUCE_BEARER`

Adds an atom to `L_B` (the discursive vocabulary) without committing to or
denying it. The atom enters the shared language but does not enter `[C : D]`.

```json
{
  "type": "INTRODUCE_BEARER",
  "proposition": "the patient has bilateral pulmonary infiltrates",
  "paraphrases": ["the patient has BPI", "bilateral infiltrates on imaging"],
  "references": []
}
```

Application semantics:
- Inserts a row in `atoms` with `contributor_id` set to the respondent.
- `paraphrases` and `references` populate the corresponding JSON columns
  (added to the schema in Phase A).
- No change to `[C : D]`.

The opponent should aggressively confirm extraction during early
vocabulary-building, especially on an empty base ("I've added these as
bearers — confirm?"), and should propose unification when probable
synonymy is detected against existing atoms.

### `ASSERT_IMPLICATION`

Records a defeasible material implication `{γ} |~ {δ}` as a direct
respondent commitment, bypassing the tension cycle.

```json
{
  "type": "ASSERT_IMPLICATION",
  "gamma": ["the patient has fever", "the patient has productive cough"],
  "delta": ["the patient has a bacterial infection"],
  "reason": "defeasibly supported in adult primary care",
  "gamma_sides": ["C", "C"],
  "delta_sides": ["C"]
}
```

`gamma_sides` and `delta_sides` are optional per-atom side mappings.
Defaults: γ atoms → not added to C/D (vocabulary only), δ atoms → not
added to C/D (vocabulary only). When the respondent's prose framing
makes the side assignment clear, the opponent populates these fields.

Application semantics:
- Atoms in γ ∪ δ added to `L_B` if not already present.
- Optionally inserts into `positions` per the side mappings.
- Inserts a row in `assessments` with `domain='asserted'`, `judgment='holds'`,
  `contributor_id` set to the respondent, provenance JSON capturing session,
  case, turn, and reason.

The opponent's extraction should be conservative: only emit
`ASSERT_IMPLICATION` when the respondent's prose explicitly signals
inferential commitment ("I commit to the implication that...", "I hold
that γ entails δ", "if γ then δ as a rule"), not for any conditional
prose. Casual hypotheticals stay atomic via `COMMIT`/`DENY`.

### `RETRACT_IMPLICATION`

Symmetric to `RETRACT` for atomic propositions, but targets an implication
in `I` by id.

```json
{
  "type": "RETRACT_IMPLICATION",
  "target_implication_id": "I7",
  "reason": "I no longer endorse this rule"
}
```

Application semantics:
- Marks the assessment row `status='retracted'`, sets `resolved_at`.
- If the implication was tension-derived (`domain='tension'`), also marks
  the originating tension as `status='revoked'` (a new status to add to
  the tensions table CHECK constraint).
- Triggers re-evaluation of any derived challenges that depended on this
  implication.

### `REFINE_IMPLICATION`

Replaces an asserted implication with a narrower version, typically in
response to a defeater the respondent now accepts.

```json
{
  "type": "REFINE_IMPLICATION",
  "target_implication_id": "I7",
  "new_gamma": ["fever", "productive cough", "patient is immunocompetent"],
  "new_delta": ["bacterial infection"],
  "reason": "narrowing to exclude immunocompromised cases"
}
```

Application semantics:
- Marks the original implication `status='refined'` with a link to the
  successor.
- Inserts a new assessment with the narrower premise/conclusion set,
  carrying provenance that references the predecessor.

The refinement chain is reconstructible from the provenance metadata.

### `DISPUTE_IMPLICATION`

Allows the opponent to challenge an asserted implication without committing
to its negation. Preserves the Socratic asymmetry — the opponent is
demanding justification, not taking a counter-position.

```json
{
  "type": "DISPUTE_IMPLICATION",
  "target_implication_id": "I7",
  "reason": "consider a patient with fever and cough but confirmed viral etiology",
  "suggested_counter_instance": ["fever", "cough", "viral pcr positive"],
  "suggested_defeater": "viral pcr positive"
}
```

Application semantics:
- Marks the assessment `status='disputed'`.
- The respondent's next move must be `defend`, `REFINE_IMPLICATION`, or
  `RETRACT_IMPLICATION` — a forced choice parallel to how a tension
  forces `ACCEPT_TENSION` or `CONTEST_TENSION`.
- The dispute, its proposed counter-instance, and the resolution are
  recorded in a `disputes` table for audit trail and export.

Counter-instance and defeater suggestions are optional but useful — they
give the respondent a concrete handle for the dispute.

The dispute mechanism creates an implication lifecycle:

```
asserted → disputed → defended | refined | retracted
       \____________→ retracted    (direct retraction without dispute)
```

Imported and tension-derived implications can also be disputed.

## The positum-as-ontology simplification

Currently the positum is interpreted as a case-positing first message —
the opponent extracts `COMMIT`/`DENY` speech acts from it about an
implicit subject. The simplification: the positum can also be an ontology
articulation, parsed into `INTRODUCE_BEARER` and `ASSERT_IMPLICATION`
speech acts about a theory rather than a case.

The opponent reads the positum's intent rather than treating one mode as
canonical. Heuristics for the opponent prompt:

- Descriptive framing ("a 58-year-old woman presents with...") → case
  mode; extract atomic commitments about the implicit patient.
- Articulative framing ("an animal is anything that's alive and not a
  plant; birds are animals; penguins are birds...") → ontology mode;
  extract bearers and rules without committing.
- Mixed framing → both extractions apply.

This is the conservative path to ontology engineering in Elenchus: no
typed vocabulary, no ABox/TBox split (that's NMMS_Onto territory), no
schema machinery. Just the existing propositional protocol with the
new speech acts and a smarter opponent first-turn.

For the cases where multi-individual reasoning matters, atoms can be
formed with explicit individual reference in the surface form ("the
patient has fever" vs. "the patient's sister has factor V Leiden");
these become distinct atoms and rules about one don't fire for the
other. This is sufficient for many clinical and scientific domains
without typed vocabulary.

## Bootstrap from an empty base

The opponent's posture on an empty base shifts to maieutic mode (per
Socratic elenchus) — eliciting vocabulary and asserted rules from the
respondent's tacit competence rather than probing existing commitments.
No mode flag; the opponent reads `|L_B| == 0` and adjusts. As the base
fills, the posture shifts naturally toward probing and dispute.

This is also where the LLM interpretive load is heaviest. Confirmation
loops are essential: "I've understood you to be introducing these
bearers — confirm?" "I read that as asserting `{γ} |~ {δ}` — yes?"
Aggressive confirmation early prevents silent corruption of `L_B` and
`|~_B`.

## Decision rules: when does each move apply?

A taxonomy of when the respondent (or opponent on their behalf) should
emit each speech act:

| Respondent intent | Speech act |
|---|---|
| Add a new word to the discourse | `INTRODUCE_BEARER` |
| Endorse a defeasible rule | `ASSERT_IMPLICATION` |
| Take a stance on a specific atomic claim | `COMMIT` or `DENY` |
| Withdraw an atomic stance | `RETRACT` |
| Replace an atomic stance with a sharper version | `REFINE` |
| Agree with an opponent-proposed tension | `ACCEPT_TENSION` |
| Reject an opponent-proposed tension as not a real incoherence | `CONTEST_TENSION` |
| Withdraw an asserted rule entirely | `RETRACT_IMPLICATION` |
| Replace an asserted rule with a narrower version | `REFINE_IMPLICATION` |
| Opponent: challenge an asserted rule without counter-asserting | `DISPUTE_IMPLICATION` |

Some moves overlap with the tension cycle. `ASSERT_IMPLICATION` of a rule
already proposed as a tension can be treated as `ACCEPT_TENSION`. The
opponent should recognize the equivalence and emit the simpler form.

## Provenance and the export round-trip

Every assertion in `|~_B` carries provenance JSON:

```json
{
  "source": "asserted" | "tension" | "imported" | "refined",
  "session_id": <int>,
  "case_id": <int>,
  "turn": <int>,
  "reason": "...",
  "earned_via_tension": <tension_id>,   // if source="tension"
  "refines": <implication_id>,          // if source="refined"
  "disputed_by": [<actor_id>, ...],     // if currently disputed
  "history": [
    {"turn": 14, "event": "asserted", "actor_id": 1},
    {"turn": 23, "event": "disputed", "actor_id": 2, "reason": "..."},
    {"turn": 24, "event": "defended"}
  ]
}
```

This preserves the dialectical history of each implication. The benchmark
export selects from this with appropriate aggregation; the import path
reconstructs assertions with `domain='imported'` and source-benchmark
metadata.

## Connection to ROADMAP.md

Phase B implements:
- `ASSERT_IMPLICATION`, `INTRODUCE_BEARER`, `RETRACT_IMPLICATION`
- The positum-as-ontology recognition in the opponent prompt
- Confirmation loops
- `domain='asserted'` and provenance JSON (schema support added in
  Phase A)

Phase B explicitly **defers** `DISPUTE_IMPLICATION` and `REFINE_IMPLICATION`
to later phases. They require:
- The `disputes` table and the dispute UI (probably alongside the
  always-on inferential surface in a later phase)
- A `status='refined'` link on assessments
- Re-evaluation logic for derived consequences when refinement happens

The deferral is fine because the propositional content of Phase B is
already useful — bootstrap and conceptual specification work — and the
dispute lifecycle becomes more valuable once cases as first-class
entities exist (which they do from Phase A schema, but the UX for
case-driven dispute is later work).

## Open questions

- **Should `ASSERT_IMPLICATION` automatically commit/deny γ and δ atoms,
  or never?** Current proposal: never by default, opt-in via
  `gamma_sides` / `delta_sides`. Articulating a rule isn't asserting
  its premises in a specific case. But some respondents will expect
  the opposite. Worth piloting both behaviors.
- **What's the right confirmation cadence?** Confirming every move is
  exhausting; confirming none is risky. Probably: confirm batches at
  natural pause points (end of a respondent's prose turn, before applying).
- **How aggressively should the opponent propose `ASSERT_IMPLICATION`
  during build-mode?** Could be very useful ("would you like to assert
  `{γ} |~ {δ}` as a rule?") or could feel pushy. Probably depends on
  whether the respondent has signaled they're in articulative mode.
