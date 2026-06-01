# NMMS_Onto Integration

Design notes on extending Elenchus with the NMMS_Onto ontology schema
extensions developed alongside pyNMMS. Captures the conceptual mapping
into the Elenchus data model and dialectical protocol; this work is
**not currently in the ROADMAP** and is intended as an optional future
upgrade when propositional ontology articulation (Phase B) becomes
limiting.

## What NMMS_Onto adds

NMMS_Onto extends the propositional NMMS proof-theoretic framework with
seven defeasible axiom schema types, functioning as macros for expressing
material inferential commitments and incompatibilities. The schemas are
lazily evaluated during base-level axiom checking, via a third axiom
check (Ax3) layered on top of Ax1 (Containment) and Ax2 (exact base
consequence).

### The seven schemas

**Inferential commitment schemas:**

| Schema | Generated sequent | Reading |
|---|---|---|
| `subClassOf(C, D)` | `{C(x)} \|~_B {D(x)}` | Membership in C defeasibly entails membership in D |
| `range(R, C)` | `{R(x,y)} \|~_B {C(y)}` | Standing in R to something commits to its second argument instantiating C |
| `domain(R, C)` | `{R(x,y)} \|~_B {C(x)}` | Standing in R commits to its first argument instantiating C |
| `subPropertyOf(R, S)` | `{R(x,y)} \|~_B {S(x,y)}` | R-assertions defeasibly entail corresponding S-assertions |
| `jointCommitment([C1, …, Cn], D)` | `{C1(x), …, Cn(x)} \|~_B {D(x)}` | Multiple antecedents jointly entail D; min arity 2 |

**Incompatibility schemas:**

| Schema | Generated sequent | Reading |
|---|---|---|
| `disjointWith(C, D)` | `{C(x), D(x)} \|~_B {}` | Holding both simultaneously is incoherent |
| `disjointProperties(R, S)` | `{R(x,y), S(x,y)} \|~_B {}` | Holding both R(x,y) and S(x,y) is incoherent |

### Key semantics

- **Exact-match defeasibility**: schemas only fire when antecedents and
  consequents precisely match the schema pattern. Adding any premise
  defeats schema-based inferences (no Weakening).
- **No transitivity**: subClassOf chains don't compose automatically.
  Each link is registered separately; the framework deliberately lacks
  transitivity to leave domain control over which chains hold.
- **Lazy evaluation**: schemas are abstract patterns, not eagerly
  grounded over individuals.
- **Vocabulary borrowed, semantics divergent**: terminology mirrors
  W3C RDFS/OWL for familiarity, but the proof theory is NMMS — defeasible
  throughout.
- **Containment preservation**: schemas don't violate Ax1 (Containment).

## Mapping into Elenchus

The propositional Elenchus model treats atoms as flat sentences; NMMS_Onto
introduces typed vocabulary and an ABox/TBox distinction.

### Vocabulary structure

| Current Elenchus | With NMMS_Onto |
|---|---|
| Atom: flat sentence `"the patient has bilateral infiltrates"` | Concept `BilateralInfiltrates`; ABox assertion `BilateralInfiltrates(patient0)` |
| Implicit single-subject ("the patient") | Explicit individuals: `patient0`, `relative1`, `physician2` |
| No relational vocabulary | Roles: `hasComorbidity(patient0, hypertension)`, `treats(physician2, patient0)` |
| Asserted implication `{bi, ad} \|~ {cpe}` (propositional, case-specific) | Schema `jointCommitment([BI, AD], CPE)` (universal); or propositional sequent over ground ABox facts |
| Position `[C : D]` of atoms | Position `[C : D]` of ABox assertions over a case's individuals |
| `L_B`: flat list | `L_B`: concepts (unary), roles (binary), individuals |
| `\|~_B`: propositional sequents | `\|~_B`: propositional sequents *plus* schema patterns evaluated lazily |

### Base partition

- **TBox**: schemas (the seven types), case-independent inferential structure.
- **ABox**: concept and role assertions about individuals; slots into
  per-case `[C : D]` exactly as propositional commitments do today.

This maps cleanly onto the theory/case distinction already in the
architecture vision: TBox = theory, ABox-per-case = position.

### Schema storage

Schemas would live in a new `schemas` table alongside the existing
`assessments` (or as a specialized `domain` value on `assessments` with
a JSON schema-shape column):

```sql
CREATE TABLE schemas (
    id INTEGER PRIMARY KEY,
    type VARCHAR NOT NULL CHECK(type IN
        ('subClassOf','range','domain','subPropertyOf',
         'jointCommitment','disjointWith','disjointProperties')),
    spec JSON NOT NULL,           -- schema-specific parameters
    contributor_id INTEGER NOT NULL,
    domain VARCHAR DEFAULT 'asserted',
    status VARCHAR DEFAULT 'active',
    provenance JSON DEFAULT '{}',
    asserted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    resolved_at TIMESTAMP
);
```

Or — preferred for uniformity — schemas are encoded as assessments with
a typed-vocabulary premise/conclusion shape. The `spec` JSON carries
the schema type and parameters; the propositional codepath gets the
ground sequents when relevant.

### Vocabulary tables

Atoms expand into concepts, roles, individuals:

```sql
CREATE TABLE concepts (
    id INTEGER PRIMARY KEY,
    name VARCHAR NOT NULL UNIQUE,
    display_name VARCHAR,
    paraphrases JSON DEFAULT '[]',
    references JSON DEFAULT '[]',
    contributor_id INTEGER NOT NULL,
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE roles (
    id INTEGER PRIMARY KEY,
    name VARCHAR NOT NULL UNIQUE,
    display_name VARCHAR,
    domain_concept VARCHAR,        -- optional typing
    range_concept VARCHAR,
    paraphrases JSON DEFAULT '[]',
    references JSON DEFAULT '[]',
    contributor_id INTEGER NOT NULL,
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE individuals (
    id INTEGER PRIMARY KEY,
    name VARCHAR NOT NULL UNIQUE,
    display_name VARCHAR,
    case_id INTEGER,               -- usually case-bound
    contributor_id INTEGER NOT NULL,
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE abox_assertions (
    id INTEGER PRIMARY KEY,
    concept_id INTEGER REFERENCES concepts(id),    -- if unary
    role_id INTEGER REFERENCES roles(id),           -- if binary
    individual_a_id INTEGER REFERENCES individuals(id),
    individual_b_id INTEGER REFERENCES individuals(id), -- if binary
    case_id INTEGER NOT NULL,
    contributor_id INTEGER NOT NULL,
    side VARCHAR CHECK(side IN ('C','D')),
    status VARCHAR DEFAULT 'open',
    introduced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

This is a meaningful schema expansion. The existing `atoms` table could
remain as a propositional fallback (for atomic content that isn't yet
typed), with concepts/roles/individuals as the richer typed layer.

## New speech acts NMMS_Onto requires

Beyond the propositional speech acts in `speech-acts-extensions.md`:

- `INTRODUCE_CONCEPT(name, display_name, paraphrases, references)`
- `INTRODUCE_ROLE(name, display_name, domain, range, paraphrases, references)`
- `INTRODUCE_INDIVIDUAL(name, display_name, case)`
- `ASSERT_SUBCLASS(child, parent)` — `subClassOf` schema
- `ASSERT_DOMAIN(role, concept)` / `ASSERT_RANGE(role, concept)`
- `ASSERT_SUBPROPERTY(child_role, parent_role)`
- `ASSERT_JOINT_COMMITMENT(antecedents, consequent)` — schema-shaped
  `jointCommitment`; this is what `ASSERT_IMPLICATION` becomes when
  premises are typed concepts
- `ASSERT_DISJOINT(concept_a, concept_b)`
- `ASSERT_DISJOINT_PROPERTIES(role_a, role_b)`
- `COMMIT_ABOX(concept_or_role, individuals, side)` — typed version of
  `COMMIT`/`DENY`
- Plus retraction, refinement, dispute variants for each

The opponent's prompt grows substantially to recognize these from prose.
*"ARDS is a kind of acute lung injury"* parses to `ASSERT_SUBCLASS(ARDS,
AcuteLungInjury)`. *"Cardiogenic pulmonary edema and ARDS are mutually
exclusive"* parses to `ASSERT_DISJOINT(CPE, ARDS)`. *"The `hasComorbidity`
relation has Patient as domain and Condition as range"* parses to two
schema assertions. Heavy interpretive work.

## Inferential surface impact

The always-on derivation sweep needs to handle Ax3. For each case, the
sweep queries:

- Propositional: `C |~ {atom}` against `|~_B` (current behavior).
- ABox: `ABox(case) |~ {ConceptAssertion(individual)}` against
  `|~_B ∪ S_ground`, where `S_ground` is the lazy schema-grounding.

pyNMMS handles this; the integration is on the Elenchus side — extract
typed atoms from speech acts, store in the typed vocabulary, query the
typed reasoner, render the typed forced commitments.

User-visible: forced commitments appear as ABox assertions like
`CardiogenicPulmonaryEdema(patient0)`, with click-through traces that
cite schemas as well as ground sequents. Inferential surface becomes
richer — patient-specific consequences derived from universal schemas.

## What it buys compared to propositional Elenchus

1. **Generalization across cases.** Schemas apply to every patient
   automatically. Propositional rules apply only to atoms they were
   stated over.
2. **Compositional vocabulary.** Concept hierarchies, role typing,
   disjointness as first-class moves.
3. **Multi-individual cases.** Patient + family + treating physician,
   with relations between them, all natively expressible.
4. **OWL/RDFS bridge.** L_B becomes URI-bearing typed vocabulary that
   can ground in standardized terminologies (SNOMED, ICD-11, Gene
   Ontology). The Protégé/Elenchus complementarity becomes native.
5. **Schema-level disagreement.** Multi-respondent extension applies to
   schemas, not just propositional implications. R1 may endorse
   `subClassOf(Sepsis, SystemicInflammation)`; R2 may dispute it.

## What it costs

1. **Schema expansion in the data model**: new tables for concepts,
   roles, individuals, abox_assertions, schemas. Non-trivial.
2. **Opponent prompt complexity**: many new speech-act types to recognize
   and extract from prose. More LLM interpretive work, more confirmation
   loops needed.
3. **Reasoner integration**: pyNMMS Ax3 path needs to be exercised; the
   inferential surface query layer needs to handle typed sequents.
4. **UI complexity**: typed atoms render differently from flat ones;
   forced commitments are typed; tracing shows schemas as well as
   propositional sequents.
5. **Backward compatibility**: existing propositional dialectics need a
   migration path or coexistence with typed content.

## When this becomes worth doing

Defer until propositional content becomes limiting. Triggers:

- The domain genuinely requires multi-individual reasoning beyond what
  indexed atoms can express (legal disputes with multiple parties,
  multi-organism biology, kinship reasoning).
- The base grows large enough that propositional duplication across cases
  becomes unwieldy (each case needs to restate atoms about its specific
  patient because rules are propositional).
- Interoperability with OWL/RDFS infrastructure becomes a real
  requirement — publishing to a SPARQL endpoint, importing a standardized
  ontology, federating with semantic-web tools.

For the Sloan study, none of these trigger. Phase B's propositional
ontology articulation suffices for clinical reasoning at the case level.
NMMS_Onto integration is a Phase 2.5 or later upgrade.

## A staged path if it does become necessary

Rather than a big-bang integration, a staged path:

1. **Add typed vocabulary tables** alongside propositional atoms.
   Propositional content continues to work; typed content opt-in.
2. **Add `INTRODUCE_CONCEPT` / `INTRODUCE_ROLE` speech acts.** Validate
   that the LLM can extract these reliably from prose.
3. **Add `ASSERT_SUBCLASS` and `ASSERT_JOINT_COMMITMENT` schemas.**
   Validate against a small typed domain (e.g., translated pulmonary
   edema benchmark).
4. **Add the remaining schemas.** `disjointWith` is particularly
   interesting because it directly encodes the kind of incompatibility
   tensions surface.
5. **Mixed-content queries.** Propositional sequents and schema-derived
   sequents coexist in the same derivation; the reasoner handles both.
6. **Migration path** for existing propositional dialectics: opt-in
   typing, propositional content unchanged.

This is months of work, not weeks. Not warranted unless a specific use
case demands it.

## Connection to the broader architecture vision

NMMS_Onto fits the architecture vision (see `architecture-vision.md`)
without requiring changes to it. The dialectic-as-knowledge-base claim,
theory/case distinction, multi-respondent endorsement, in-dialectic
evaluation — all carry over to typed content. The same protocol moves
apply at the schema layer; the same provenance metadata captures schema
endorsements; the same view-relative derivability extends to typed
queries.

The architectural commitments hold across the propositional/typed
boundary. NMMS_Onto is an expressivity upgrade, not a different kind of
system.
