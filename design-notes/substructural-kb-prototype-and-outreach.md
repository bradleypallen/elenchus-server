# A Prototype for Substructural Knowledge Bases — Gaps from 0.8.2, and an Outreach Mode

Design notes on what it would take to turn Elenchus into the interactive
prototype for a proposed follow-on project on **substructural knowledge
bases**: a second reasoning engine, querying and updating as first-class
activities, comparison of two sides of a debate (**crux detection**),
case studies in law and philosophy, and a way to put the tool in front of
people outside the team. Assessed against **Elenchus 0.8.2** (September
2026), claim by claim, in the code.

Two caveats. The requirements below are read from a *summary* of the
proposed project, not its full narrative, so they are the floor rather
than the ceiling. And like everything in this directory this is reference
material, not a plan of record — nothing here is sequenced in
[`ROADMAP.md`](../ROADMAP.md).

The note has three parts: what the project asks for and what already
exists (§1–2); the gaps (§3); and an **outreach mode** (§4) — inviting
people to use Elenchus itself, without the study harness, and deciding
**whose LLM account each of them spends** — which turns out to be the piece
that most of the project's external-facing promises rest on, and the
cheapest to build. §5 is about running this work beside a
study that needs the protocol frozen. §6 proposes an order.

## 1. What the project asks of Elenchus

| | Requirement |
|---|---|
| **R1** | Integrate a second, external substructural reasoner — the motivating case is **ROLE.jl**, a Julia engine built by collaborators around implication-space semantics — alongside or in place of the current proof-search reasoner. |
| **R2** | Techniques for **updating and querying** substructural KBs: the KB as something you ask questions of and revise, not only something you build. |
| **R3** | **Crux detection**: given two incompatible sets of commitments — two sides of a debate — find the claims neither side is in explicit conflict over whose settlement would move the debate. Milestones: a formal definition, an algorithm, an implementation, a demonstration on a small case where the right answer is known. |
| **R4** | Case studies: **legal conceptual spaces from case law**, and philosophical concepts (causation, parthood, intentionality), encoded with domain experts. |
| **R5** | Mathematical results that are meant to become tool affordances: **composition** of knowledge bases (vocabularies glued along an overlap) and an extension to **predicate logic**. |
| **R6** | A KR-venue paper with the **code and data behind its empirical claims** linked from the public repository. |
| **R7** | Uptake: outside researchers and legal scholars **using the prototype**, a **publicly accessible demonstration instance**, worked examples, tutorials or workshops, and engagement figures reported to the funder. |
| **R8** | "KB-grounded LLM dialogue": an LLM front end that is grounded in the explicit KB and can *tentatively* suggest what is implicit, with the difference visible. |
| **R9** | Semantic Web interoperability and **LLM metrology** — configurations of the LLM instrument calibrated against a curated reference KB. |

## 2. What 0.8.2 already provides

The premise is real. A domain expert develops a bilateral position
`[C : D]` in natural-language dialogue with an LLM opponent that proposes
tensions but cannot assert; each accepted tension becomes a material
implication in a DuckDB-backed material base; derivability is decided
deterministically by pyNMMS's `NMMSReasoner` (no Weakening, no Cut), never
by the LLM. Around that core:

- accounts, roles and invitations; web, command-line and HTTP interfaces;
- **append-only research capture** — every LLM exchange (what the model was
  shown, its verbatim output, how it was parsed, the prompt's name and
  SHA-256, the model) and every state transition with its source — which is
  exactly the record R6 and R9 need;
- `scripts/run_dialectic.py` (an LLM as respondent against the real
  opponent) and the simulation harness with LLM personas: the beginnings of
  R9;
- read-time cost accounting per account, per purpose and per study, with a
  daily spend alert;
- a dashboard an admin can run with no shell on the server; backups;
  documentation; CI; PyPI releases.

What follows is what is *not* there.

## 3. Gaps

Sizes are rough: **S** days, **M** a few weeks, **L** a research-and-build
effort.

### G1 · A knowledge base holds exactly one position (blocks R3) — M

`positions` is keyed on `(atom, side)`: one stance per claim per base. The
`actor_id` and `case_id` columns added in base migration `0002` are
future-proofing that nothing reads. And a base is private to its owner —
`require_base_owner` answers a non-owner with 404, even for reading. So
there is no second respondent in a base, no comparison of two bases, and no
notion of "the other side".

Two designs, not exclusive:

- **Several positions in one base.** Re-key `positions` on
  `(actor_id, atom, side)`; the consequence relation `|~_B` stays shared
  (it is the common ground the two sides argue *within*); tensions and
  speech acts carry their actor. This is the view-relative endorsement of
  [`architecture-vision.md`](architecture-vision.md), and what a formal
  definition of a crux most naturally quantifies over: one `|~`, two
  `[C : D]`.
- **Two bases, compared.** Each side builds its own base, implications
  included, and the comparison runs over their composition. This is the
  harder and more interesting case — the two sides may disagree about what
  follows from what, not only about what is so — and it is where R5's
  "vocabularies glued along an overlap" stops being category theory and
  becomes an import dialog.

Either way there is a problem the mathematics won't see: **atoms are
free-text sentences**, so two experts stating the same claim do not produce
the same atom. Crux detection needs **proposition alignment** — a proposed
mapping between two vocabularies (an LLM is good at proposing one), which a
person confirms claim by claim, recorded as data with provenance like any
other move. The overlap along which vocabularies are glued has to be
*established* before it can be used.

### G2 · Crux detection does not exist (R3) — L

No definition, no algorithm, no interface. The nearest existing idea is
the planned, unbuilt *prover-derived challenges* (have the prover look for
commitments that, through accepted implications, derive denials). A crux
search is the two-party version of that, and it is query-hungry: candidate
claims × contested claims × two positions. With proof search that is
exponential per query — a reason for R1, and a reason for G4's
instrumentation to come first, so the cost is measured rather than feared.

Whatever the algorithm, the interface question is already visible: a crux
is only useful if each side can see *why* it is one — the two derivations
it would unlock — so crux results need traces, and traces need to read in
the experts' own sentences (they do today: `derive_with_trace` unquotes
atoms).

### G3 · The reasoner is hard-wired (blocks R1) — M, plus packaging

`material_base.py` imports pyNMMS directly, keeps an in-memory pyNMMS
`MaterialBase` mirrored from DuckDB, and carries a quoting layer
(`quote_atom` / `to_nmms_sentence` / `unquote_atoms`) built for pyNMMS's
sentence grammar. There is no reasoner interface.

The interface wants to be small and stated in Elenchus's terms — atoms as
plain sentences, a base as a set of sequents:

```text
load(base_sequents, language)      sync(added, retracted)
derives(gamma, delta) -> bool      explain(gamma, delta) -> trace
capabilities() -> {connectives, quantifiers, roles, incompatibility, …}
```

`capabilities()` matters because the two engines do different things.
Proof search answers "does Γ |~ Δ, and show me"; an implication-space
engine can also answer "what is the inferential role of p", "what is
incompatible with p", "over what range of added premises does this
implication survive" — query *kinds* (G4) the API and interface currently
have no words for.

Running a Julia engine is its own problem. Elenchus is a pip-installable
package and a single server process; a Julia runtime is neither. A
**sidecar process** speaking a small JSON protocol (stdin/stdout or a
local socket) keeps the package pure Python, keeps Julia's start-up cost
out of the request path, and lets the engine be absent — the interface
falls back to pyNMMS. The very first integration needs no live bridge at
all: G6's interchange file, loaded offline.

### G4 · Querying is not a first-class activity (R2, R8) — S to M

`/derive` exists in the HTTP API and the CLI REPL. **The web interface
never calls it** — there is no way to ask a question of a KB in the
browser. Queries are written in pyNMMS syntax over angle-quoted sentences;
derivations are neither timed nor logged with the size of the base; there
are input caps but no budget.

In rising order of cost:

1. **A query panel** (S): pick premises and conclusions from the base's own
   atoms, see derivable or not, with the trace. Record every query —
   premises, conclusions, base size, depth reached, cache hits, wall time —
   in the capture log. This is the instrument that makes "real knowledge
   exposes the computational bottlenecks" an observation instead of a hope.
2. **Natural-language questions** (M): the LLM translates a question into
   one or more sequents *over the existing vocabulary*; the reasoner
   answers; the LLM phrases the answer. Same discipline as the opponent:
   the model proposes, the engine disposes.
3. **Tentative answers** (M): when the question needs a claim or an
   implication the base lacks, the LLM may suggest it — shown as
   *suggested, not derived*, and offered to the owner as a tension to
   accept or contest. R8's "grounded in explicit knowledge, reasoning
   statistically to tentatively derive implicit knowledge" is this
   distinction made visible; it is also the existing protocol, run from the
   query side.

### G5 · Updating implications, and doing it per dialectic (R2) — S to M

Implications enter a base only through accepted tensions, unless the
theory-articulation speech acts (`ASSERT_IMPLICATION`, `INTRODUCE_BEARER`,
`RETRACT_IMPLICATION`) are enabled — and that is **one environment
variable for the whole server**, kept off so the running study's
vocabulary matches its protocol. `REFINE_IMPLICATION` and
`DISPUTE_IMPLICATION` from
[`speech-acts-extensions.md`](speech-acts-extensions.md) are unbuilt. There
is no versioning or diff of a base, and no undo beyond retraction.

The fix that matters most is structural: a **protocol profile per
dialectic** — which speech acts are live, which system prompt, which
reasoner, which model — chosen at creation, recorded in the base's `meta`,
stamped on every turn (the capture log already stamps the prompt's name and
hash). The server-wide flag becomes the default profile. §5 depends on
this.

### G6 · No interchange format; nothing composes (R1, R5, R6) — S, then L

A base can be read as a PDF, as a text report, or inside a study export.
None is a KB another tool can load. There is no import, merge, fork or
copy.

- **A canonical JSON export/import** of one base (S): language, positions
  with their actors, active sequents with provenance, tensions with
  outcomes, the protocol profile, a schema version. It unblocks three
  things at once: the external engine can load Elenchus-built KBs offline
  (R1, before any bridge exists), a paper can cite a data file (R6), and
  "fork this example" becomes possible (§4).
- **RDF** for the Semantic Web audience (R9): pyNMMS 0.15 already ships a
  `pynmms.rdf` module (RDF-backed bases, defeasible rules) that Elenchus
  does not use. (`pynmms.onto` is deprecated.)
- **Composition** (L): the tool-side meaning of R5. Import is its trivial
  case, alignment (G1) its hard one.

### G7 · Atoms are opaque sentences (R5) — L

A predicate-logic extension has nowhere to land: there are no terms,
predicates or variables, only sentences. The relational concepts named for
the philosophical case study — causation, parthood — will strain
sentence-level atoms early ("x is part of y" for which x and y?).
[`nmms-onto-integration.md`](nmms-onto-integration.md) sketched a typed
vocabulary; with `pynmms.onto` deprecated, the RDF module is now the more
likely carrier. This should follow the mathematics rather than anticipate
it; G3's `capabilities()` is how the interface finds out what it may
offer.

### G8 · No sources (R4) — M

There is no document ingestion, and no commitment can point at a passage.
Implications carry a free-form `provenance` JSON; positions carry nothing.
The `cases` table is a one-row stub — and means *theory versus case*, not
case law. A legal scholar will want every claim traceable to an authority,
and will want to start from the opinion, not a blank box.

Sketch: a `sources` table per base (citation, locator, excerpt); an
optional `source_id` and locator on positions and implications; an
ingestion step in which the LLM *proposes* candidate commitments from a
pasted or uploaded text, each with the passage it rests on, and the expert
commits, denies or discards — the opponent's discipline again, applied to
reading. Mind copyright on uploaded texts: store citations and short
excerpts, not corpora.

### G9 · Metrology stops at the transcript (R9) — M

`run_dialectic.py` and the sim produce dialectics with LLM respondents, and
the capture log records the instrument's configuration turn by turn. What
is missing is the measurement: a **reference KB**, a **scoring layer**
comparing a produced base with it (needs G6's format and G1's alignment), a
batch runner over models, prompts and seeds, and a record of which
configuration scored what.

## 4. Outreach mode

### What it is

**Inviting people to use Elenchus itself**, with no study around it: no
participant token, no condition assignment, no briefing → tutorial → task →
questionnaire state machine, no writing pane, no blinding, no judge. A
legal scholar who has agreed to try the tool; forty people at a conference
tutorial; eventually, anyone who follows a link from a blog post. It is
the thing R7 assumes exists.

It is *not* open self-registration, and it is *not* a study arm. Anything
learned from outreach use is usage feedback, not experimental data, and the
two must not be able to contaminate each other (§5).

### What already works

More than one might think. An admin can issue an invitation with the role
`user`; that person signs up, creates dialectics of their own, works with
the opponent, downloads a PDF report, and deletes what they made. A `user`
is fenced off from everything else — verified on 0.8.2: the study and admin
routes answer 403, another person's dialectic answers 404. The study
harness is reached only through a participant token or a researcher's
account, so an outreach account never touches it.

### What stands in the way

**O1 · Invitations are one person at a time.** Single-use, one address
each, issued by hand in the Invites tab. `invites.metadata` exists and
nothing uses it. A workshop needs **a link that seats forty**.

**O2 · No limit on what an invited person can spend.** No per-account
allowance, no limit on messages, no limit on the number of dialectics. The
daily spend alert tells an admin after the fact. For study participants
that is deliberate — a hard stop mid-task costs more than the overspend.
For outreach it is the wrong default: the host's API key is being lent to
strangers.

**O3 · Nothing greets a new user.** The interface has no first-run
guidance at all — no explanation of commitments and denials, of what a
tension is, of what accepting one does. Study participants get a tutorial
on a practice topic; everyone else gets an empty list and a box. The
vocabulary (bilateral position, material implication) is the project's, not
the visitor's.

**O4 · Dialectic names are global.** `bases.id` is the name, and it is the
primary key. Verified on 0.8.2: once anyone has a dialectic called
`Causation`, the next person to try gets `409 — Dialectic 'Causation' is
already registered`. At a tutorial where everyone is asked to encode the
same concept, the second person in the room hits this. It also tells them
that someone else has a dialectic of that name, which the rest of the API
is careful never to reveal (404, not 403).

**O5 · The protocol is set for the whole server** (G5). Outreach users
should get the full speech-act vocabulary; a study on the same instance
must not.

**O6 · Nothing can be shown or shared** (G1, G6). There is no example to
look at before starting, no way to fork one, and no way for a visitor to
show the team what they built. The team can't see it either, short of an
admin opening it.

**O7 · No way to hear back.** Uptake is evidenced by people attesting that
the tool gave them something. There is nowhere to say so.

**O8 · No notice, no choice, about what is recorded.** Research capture is
on for every base — every message, every model output, every move. For a
study participant that is covered by consent. An outreach user signs up
without being told, and without being asked whether what they make may be
used.

**O9 · The instance speaks for one project.** The landing pages'
institutions and funding acknowledgement ship inside the package
(`static/partners.json`); an instance run for a different project shows the
wrong acknowledgement until there is a release. And mail: an instance whose
provider is still sandboxed invites nobody
([`deploy/ses-production-access.md`](../deploy/ses-production-access.md)).

**O10 · Everyone spends the same LLM account.** The server holds one
process-wide `Opponent` with one client — one API key, one model, one
endpoint — set in the Settings modal and stored encrypted as
`platform_settings['llm_api_key_enc']`. The message route chooses nothing
per person. So a study funded by one grant, a prototype funded by another,
a workshop someone else is paying for, and a collaborator happy to use
their own key all draw on the same bill, and the only way to separate them
today is **a separate instance per billable account**. (That is not a bad
answer — §5 wants separate instances anyway — but it is the only one.)

### Design sketch: cohorts

One new concept carries most of it. A **cohort** is a named group of
invited users and the terms they were invited on:

```text
cohorts
  id, label                    "Legal scholars, spring 2027" · "ISWC tutorial"
  code                         the secret in the link: /?cohort=<code>
  seats, seats_taken           40 · how many have signed up
  opens_at, closes_at          the link works only in this window
  llm_account_id               whose LLM account this cohort spends (below)
  allowance_usd                LLM spend per person before a hard stop
  max_dialectics               per account
  protocol_profile             default profile for their dialectics (G5)
  welcome_md                   what they see first (O3)
  terms_version                the notice they accepted (O8)
  created_by, created_at, closed_at
actors.cohort_id               who came in through which door
```

- **Joining.** `/?cohort=<code>` opens the sign-up form while the cohort is
  open and has seats; the person gives an email, a name and a password and
  accepts the notice. The account is an ordinary `user`. A single-use
  invitation remains the way to add one named person — it gains an optional
  cohort. An **open cohort** (no seat limit, a small allowance, a long
  window) *is* the public demonstration: the demo is a configuration of
  outreach mode, not another feature.
- **The allowance** is checked before the opponent is called, against the
  account's spend priced the way the cost dashboard prices it (from tokens,
  at read time). At the limit the message route answers with a plain
  sentence — *you've used the allowance for this trial; ask for more* —
  and an admin can raise it per person. **Never applied to study
  participants or to staff.** A message rate limit per account belongs
  here too.
- **Names.** Make a dialectic's identity `(owner, name)`. The schema
  already says `UNIQUE(owner_id, name)`; the obstacle is that `bases.id` —
  which the per-base files, the `usage` rows and the name-keyed API all use
  — *is* the name. The session-keyed API (`/api/sessions/{id}/…`) was built
  to get off name-keyed routes; this is the reason to finish that job. A
  migration gives bases opaque ids and keeps names as labels.
- **First run.** The cohort's welcome text; a glossary in the visitor's
  words; and a **worked example to fork** (needs G6) — watching one
  tension get raised and accepted teaches more than a page about bilateral
  positions.
- **Sharing.** A read-only link to one's own dialectic — revocable,
  unlisted, rendering the position, the implications and the transcript —
  is enough for "look what I built" and for a team member to help. Forking
  is import of an export (G6). Co-editing waits for G1.
- **Feedback.** A short, optional form reachable from any dialectic — what
  were you trying to work out, did it show you something you hadn't seen,
  may we quote you — stored with the cohort and the dialectic's id. It is
  the attestation R7's indicators ask for, collected where the experience
  happened.
- **Notice and choice.** At sign-up: what is recorded, who can see it, how
  to delete it (deleting a dialectic already removes its file). One
  explicit, revocable choice per account — *my dialectics may be used in
  the project's research* — defaulting to no. Exports and worked examples
  draw only on accounts that said yes. Versioned text, the version stored
  with the acceptance.
- **Reporting.** Per cohort: invited, signed up, active this week,
  dialectics, turns, accepted implications, spend, feedback received. The
  cost dashboard already rolls spend up by account; grouping by cohort is
  one join. These are the engagement figures a funder asks for, without
  anyone counting by hand.
- **The instance's own face.** Let a deployment override `partners.json`
  (a file in the data directory, served in place of the packaged one), so an
  instance acknowledges the project it serves.

### Who pays: LLM accounts

Make the billable account a thing the platform knows about, and choose it
per call.

```text
llm_accounts
  id, label                    "Study (grantee's workspace)" · "Prototype" · "A. Scholar's own key"
  protocol, base_url           anthropic | openai-compatible, as in Settings today
  default_model
  api_key_enc                  Fernet, under ELENCHUS_SECRET_KEY — as the single key is now
  owner_actor_id               NULL = the platform's; set = a person's own key
  created_by, created_at, disabled_at
actors.llm_account_id          one person → one account (their own key, or a sponsor's)
cohorts.llm_account_id         a cohort → one account
study_configs.llm_account_id   a study → one account
usage.llm_account_id           which account paid for this call
```

- **Resolution, per call:** the person's own assignment, else their
  cohort's, else their study's, else the server default — which is exactly
  what exists today, so nothing changes for an instance that defines no
  accounts. A dialectic's protocol profile (G5) names the *model*; the
  account supplies the key and endpoint, and must be one that can serve
  that model.
- **Where it plugs in.** There are four places the server calls a model:
  `Opponent._chat` and `_async_chat` (all dialectic and baseline traffic,
  and the summaries), the legacy study-report generator, and the
  simulator's personas. `LLMClient` already takes its protocol, model and
  SDK clients as constructor arguments, so the change is a resolver —
  `client_for(actor_id, base_id)` — and a small cache of clients keyed by
  account, rebuilt when an account is edited. `Opponent.reconfigure` becomes
  "edit the default account".
- **Costs follow the money.** With `usage.llm_account_id`, the cost
  dashboard shows spend per account, **budget lines attach to accounts**
  (one grant's LLM line against one account, another's against another — on
  one page, with no arithmetic), and provider reconciliation becomes per
  account: a provider report is already scoped to a workspace, which is
  what an account is on the provider's side. The daily spend alert should
  be per account too, since a runaway on a workshop's key is the workshop
  sponsor's problem and not the study's.
- **Bringing your own key** is the same mechanism with `owner_actor_id`
  set: a person enters a key in their own profile, it is used for their
  dialectics only, they can replace or remove it, and it is never shown
  again after entry — to them or to an admin. Three things differ. The
  **allowance does not apply** (they are paying), though the rate limit
  does. **Failures are theirs to hear about:** a rejected or exhausted key
  should tell the person, in the conversation, and must not page the
  platform's admin as the `critical` alert a rejected platform key
  deserves. And **custody is a real obligation:** the server holds someone
  else's secret, encrypted at rest but decryptable by whoever holds the
  master key, so the sign-up notice must say so, and should recommend a
  dedicated workspace key with its own spend limit on the provider's side
  rather than a key that can do anything else.
- **Keys are the most sensitive thing the platform stores.** Admin-only
  management; every create, edit, assignment and removal logged with its
  actor; never in an export, a report, a log line or an API response
  (`has_api_key`, as now). They live in the platform database, so a backup
  contains them — encrypted, and useless without `ELENCHUS_SECRET_KEY`,
  which lives in the server's environment and not in the backup. Keep it
  that way: the two must never travel together. Rotating the master key
  means re-encrypting every account, which wants a command.
- **A study stays on one account and one model** for as long as data is
  being collected. A different account serving the same model is
  scientifically harmless; what is not harmless is sharing: provider rate
  limits are per account, so a workshop on the study's account can throttle
  a participant mid-task. Another reason the study gets an account, and an
  instance, of its own.

### Sizing

LLM accounts — the table, the resolver and client cache, the account on
every usage row, costs and budgets per account, the admin screen, and the
alerting distinction — are **M, about a week**, and independent of
everything else in this note; bringing your own key adds the profile screen
and the notice.

Cohort invitations, the allowance, per-owner names, a welcome and
glossary, and the notice with its choice are **S to M together**, touch
nothing in the protocol or the reasoner, and are enough to put the tool in
front of a named group of outside experts. Sharing, feedback and reporting
are S each. The fork-an-example experience needs G6 first.

## 5. Two projects, one codebase

A running study needs the protocol **frozen** — the speech-act vocabulary,
the opponent's system prompt, the model — for as long as data is being
collected. A prototype under development needs to **change** exactly those
things, continually. Outreach sharpens this: outreach users should get the
newest protocol, while the study's participants must get the one in the
study's protocol document.

What holds the line:

1. **Separate instances.** The study's instance is pinned to a release and
   upgraded only between data-collection periods; prototype and outreach
   run elsewhere. A second small box costs a few dollars a month
   ([`deploy/manual-poc.md`](../deploy/manual-poc.md)); the alternative is
   a confound nobody can rule out afterwards.
2. **Protocol profiles per dialectic** (G5), so that even on one instance
   a dialectic's rules are fixed at creation and recorded, not inherited
   from a server flag someone may flip. The model is part of the profile:
   today it is a single server-wide setting, which is the one thing a
   shared instance could not reconcile.
3. **The capture log as witness.** Every turn already records the prompt's
   name and hash and the model. A study's integrity report should assert
   that these never varied within a study; today it doesn't look at them.
4. **Different doors.** Study participants are passwordless token actors;
   outreach users are `user` accounts in a cohort; neither can reach the
   other's routes. Costs are already attributed per person, so study spend
   and outreach spend separate cleanly — they should be shown separately.
5. **Different purses.** With LLM accounts (§4), each project's calls are
   paid from its own provider account and counted against its own budget
   line — and the study's rate limit is nobody else's to exhaust.

## 6. An order

Ordered so that each step is useful on its own and nothing is built before
the thing that tells you whether it was worth building:

1. **Outreach minimum** — cohort invitations, allowance and rate limit,
   per-owner dialectic names, welcome and glossary, the notice and its
   choice (§4). *Lets a named group of outside experts use the tool at all,
   which the case studies and the uptake indicators both start from.*
2. **LLM accounts** (§4): named, encrypted provider accounts chosen per
   call — person, else cohort, else study, else the server default — with
   spend, budgets and reconciliation per account. *Needed as soon as two
   funders, or one sponsor's workshop, share an instance; harmless to build
   before then, since an instance with no accounts behaves as it does now.*
3. **Interchange export/import; a query panel; derivation timing in the
   capture log** (G6, G4.1). *Lets the external engine load Elenchus-built
   KBs offline, gives papers a data file to cite, makes examples forkable,
   and starts measuring where proof search hurts.*
4. **Protocol profiles per dialectic** (G5). *Makes it safe to move
   quickly.*
5. **A reasoner interface**, pyNMMS as the first backend, the Julia engine
   as a sidecar (G3). *By now there are real KBs and real timings to justify
   it.*
6. **Several positions per base, then alignment, then crux detection**
   (G1, G2) — demonstrated first on a small case where the cruxes are known.
7. **Sources and citations** for the legal study (G8); natural-language
   and tentative answers (G4.2–3).
8. **The public demonstration**: an open cohort with a small allowance,
   on an account of its own, seeded with forkable examples from the case
   studies.
9. Structured vocabulary and composition (G7, G6) as the mathematics
   delivers them; metrology (G9) once there is a reference KB to score
   against.

## Open questions

- **One `|~` or two?** Is a crux defined over two positions within a
  shared consequence relation, or over two bases that also disagree about
  what follows from what? The schema change is small for the first and the
  composition problem is real for the second; the formal definition should
  say which before either is built.
- **Who owns an alignment?** When an LLM proposes that your atom and mine
  are the same claim, does each of us confirm, or does a third party?
  Alignments are moves with consequences and should have authors.
- **What does the external engine need that a sequent list doesn't carry?**
  If implication-space semantics wants more than `base_sequents` — ranges of
  subjunctive robustness, say — the interchange format should be designed
  with its authors, not for them.
- **Hard stops and honesty.** An allowance that ends a conversation
  mid-thought is a bad experience; one that warns at 80% is better. What is
  the smallest allowance that lets someone reach a first accepted
  implication? The cost dashboard can answer that from existing use: the
  PoC's history runs at about five cents a turn.
- **Should the platform hold other people's keys at all?** Bringing your
  own key is convenient and is the honest way to let a collaborator pay
  their own way — but it makes the server a store of third-party secrets,
  with everything that implies for whoever operates it. The alternative is
  to give such a person a sponsored account with an allowance and settle up
  outside the software. Worth deciding before the first person asks.
- **Is outreach data research data?** If opted-in outreach dialectics feed
  worked examples or papers, that is research use of personal data and
  wants the same ethical review as the study — a question for the
  institution, to be settled before the first cohort, not after.
