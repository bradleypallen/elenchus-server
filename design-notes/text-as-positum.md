# The text as the positum

*Design note, 2026-10-07. Status: **built in 0.14.0** with decisions 1
(the text is authoritative) and 2 (the natural comparator) taken by the
principal investigator on 2026-10-07, and 3–5 as recommended. The
registration text is affected (§6). Nothing here applies to a study whose
allocation seed is set.*

## 1. The problem

In both conditions a participant conducts a dialogue with the model and,
beside it, writes a two-or-three-paragraph introduction to a topic in
their own words. The panel judges the text. The hypothesis is that
building a position dialectically — committing, denying, meeting
tensions, accepting implications — produces a better understanding of
the topic, and that the text is where that understanding shows.

But as built, nothing makes the text come *from* the dialogue:

- **The two are side by side, not connected.** The editor opens empty
  beside the dialogue. A participant can hold a careful dialectic and
  then write from scratch, or write first and chat idly; the platform
  records both artefacts and nothing about the relation between them.
  If the text is not downstream of the dialogue, the comparison measures
  how people write beside two kinds of chatbot, not what the dialectic
  did.
- **The conditions are asymmetric about the deliverable.** The baseline
  assistant's prompt says the expert is writing a short introduction and
  orients to it ("the finished text is the expert's deliverable"). The
  Elenchus opponent's prompt says nothing about a text: it elicits and
  tests a position for its own sake, and learns the topic only from the
  state it is shown. One condition's dialogue is about the deliverable
  and the other's is not — a difference in text quality could come from
  that alone, in either direction.
- **Neither model sees the text.** The baseline prompt says so in as
  many words ("an editor beside this conversation that you cannot see");
  the opponent has no notion of it. So neither can relate what is said
  to what is written.
- **The Elenchus condition has two openings.** In the ordinary interface
  a dialectic begins with a *positum* — the respondent states their
  position in prose in the chat box and the opponent extracts the
  initial [C : D] from it. In the study flow there is no positum step:
  the participant lands in the dialogue view with an empty editor and an
  empty conversation, and the position accrues from whatever is typed
  into the chat.

## 2. The design

**The participant's first draft of the introduction is the positum, in
both conditions, and the dialogue cannot begin until it is written.**

1. The task opens on the editor, empty. The dialogue pane is present but
   locked, with one line saying why: *write a first draft of your
   introduction; the conversation starts from it.*
2. The participant writes an initial draft. When it meets a minimum (§4,
   decision 3) a **Begin** control unlocks the dialogue; pressing it
   saves a snapshot with trigger `positum` and sends the draft as the
   opening of the dialogue.
3. In the Elenchus condition the opponent extracts the initial [C : D]
   from the draft, exactly as it does from a chat-box positum today, and
   replies as it would to one. In the baseline the assistant reads the
   draft and replies as a helpful colleague would to a first draft.
4. From then on **every turn's context is the current draft plus the
   dialogue.** The participant keeps editing throughout; each message to
   the model carries the latest saved text, marked as such, together
   with what it carries today (the state, the history window, the
   message).
5. The clock, the soft reminders, the hard stop and the submission are
   unchanged. The final text is judged as now.

The positum is already, in the protocol, the respondent's position
stated in prose; the opponent already extracts commitments from prose.
The change is where the prose is written — in the editor, where it
stays and grows — and that the opponent keeps it in view.

What this gives, at once:

- **The text is upstream of the dialogue by construction.** It is the
  thing being examined. The dialogue cannot be about anything else.
- **Both conditions have the same shape:** draft, then develop the draft
  in conversation. The difference between them is the interlocutor —
  cross-examination against assistance — which is the thing under
  study.
- **The opponent is no longer blind to the deliverable**, and the
  asymmetry in §1 goes away without a special rule.
- **The capture improves for free.** The draft at each turn is in that
  turn's `request_content`, so a recorded turn can be replayed exactly
  (the rolling summary, when one was in force, is the one remaining
  gap — capturing it per turn is a small separate change). And the
  snapshot
  history from the `positum` snapshot to the `submit` snapshot is a
  direct, per-condition measure of what the dialogue changed in the
  text — something the current design cannot measure at all.

## 3. The text as a channel for speech acts (Elenchus condition)

Once the draft is in the opponent's view, there are two places a
participant can assert something: the chat and the text. The protocol
needs to say how they relate. The proposal:

**The text is the authoritative statement of the position; the dialogue
is where it is tested.**

- A claim that appears in the draft is a commitment. On each turn the
  opponent is shown the current draft and the previous turn's draft, and
  parses the *difference* as speech acts: a claim added is `COMMIT`, a
  claim removed is `RETRACT`, a claim reworded is `REFINE`. These are
  applied like any other speech act and logged as state events with
  `source = 'text'`, so the move log says which commitments came from
  writing and which from conversation.
- Chat messages carry speech acts as they do today (`source =
  'opponent'` for the opponent's parse of them). `ACCEPT_TENSION`,
  `CONTEST_TENSION` and the button-driven acts are unchanged.
- The opponent may raise tensions **within the draft** (two claims in
  the text that are incoherent together), **between draft and position**
  ("in paragraph two you write X; you contested the implication from X
  to Y"), and from the chat as now. Gamma is drawn verbatim from C as
  before; the only new thing is that C now contains what the text says.
- A denial is still made in conversation (prose rarely denies). The
  opponent may ask the respondent to write a denial into the text where
  it matters for the introduction ("where the boundaries lie" is one of
  the brief's own questions).
- **The opponent never writes prose for the respondent.** It can say
  that a sentence commits them to something, or that the draft omits a
  commitment they made in conversation; it does not draft, rephrase or
  dictate text. This rule is what keeps the judged text the
  participant's own words in this condition.

The alternative — the text is context only, the chat the only channel —
is simpler to build but reintroduces the gap: the opponent would see a
claim in the text that is not in C and could do nothing with it except
ask. The proposal makes the text do what a positum does throughout.

Two consequences for the formal side. First, the material base is now
built from the text and the dialogue together, so the offline alignment
of text sentences to atoms (the formal analysis the registration
describes) becomes a matter of record rather than reconstruction.
Second, a retraction by deletion is a real retraction: a participant who
cuts a sentence has withdrawn a commitment, and the opponent should say
what that changes, as it does for a retraction by button.

## 4. Decisions

1. **Where the position lives (Elenchus).** §3 as proposed — the text is
   authoritative and its changes are speech acts — or the text as
   context only. *Recommendation: §3.*
2. **The baseline's prose.** The assistant will see the draft and will
   offer rewrites, and participants will paste them (paste *length* is
   recorded; content is not). Either that is the comparator — "AI as a
   tool" genuinely drafts for people, and the registration says so — or
   the baseline is forbidden from producing replacement prose, in which
   case it is a critic without tensions and no longer the natural
   comparator. *Recommendation: keep the natural comparator and say so;
   the symmetry introduced in §2 makes the difference between conditions
   the interlocutor, not the task. Keep the own-words rule as an
   instruction to the participant in both conditions, and keep logging
   paste length so the analysis can see it.*
3. **How much initial text before the dialogue can begin.** A minimum
   stops a one-line positum; too high a one eats the clock. *Recommendation:
   fifty words or three sentences, whichever comes first, piloted.* The
   tutorial uses the same rule with a lower bar.
4. **The ordinary interface.** The study flow changes; the everyday
   Elenchus interface (no writing pane) keeps its chat-box positum.
   *Recommendation: leave it for now.* The development study, which
   runs the participant flow, gets the new shape — which is where the
   prompts will be tuned.
5. **Does the opponent see the whole draft every turn, or the diff?**
   Both: the whole current draft (so tensions within it are possible)
   and the previous turn's (so the diff is the opponent's to compute).
   Token cost grows with the draft, which is bounded by the task (two
   or three paragraphs). *Not a PI decision; noted so the cost is
   known.*

## 5. What building it involves

Most of the work is in the prompts and the tests; the editor and its
autosave already exist.

- **Gating.** The participant interface in `tutorial` and `active`
  opens on the editor with the dialogue locked; a `positum` snapshot
  trigger; `POST /api/study/session/begin-dialogue` (or the first
  message carrying `positum: true`) refuses below the minimum and
  records the snapshot; the message route refuses before it. The
  tutorial follows the same flow.
- **The draft in every turn.** `Opponent.respond` and the baseline turn
  take the current and previous drafts; `request_content` carries them
  under a marked section; `turn_log` needs no new column.
- **Prompts.** `elenchus.md` gains a THE TEXT section (the draft is the
  position; parse its changes as speech acts; tensions within and
  against it; never write prose) and loses nothing — new label
  `elenchus/<date>`. `baseline.md` drops "that you cannot see" and
  gains the draft (new label). Both go through the revision procedure
  in `docs/prompts.md`; the hashes change and the pins with them.
- **State events from the text.** `EventContext(source="text")`;
  `_apply` already handles the acts. The export's `state_events.json`
  then carries the channel.
- **Capture.** The previous draft shown to the opponent is the latest
  snapshot before the turn; store its snapshot id on the turn so the
  pair is reproducible.
- **Docs and tests.** `docs/study.md` (the task), the runbook's
  practice run and `tests/test_practice_run.py` together, the probes
  page, the sim's scripted and persona drivers (they must write a
  positum before talking), `tests/test_study_text.py`, and the RR
  conformance note's §2.4 rows.

Two or three days, with the prompt work the part that wants iteration —
the replay and eval tooling discussed alongside this note would pay for
itself here.

## 6. The registration

The task as the manuscript describes it — one editor open throughout,
two artefacts per Elenchus session, the base built move by move — stays
true. What changes is the order of the first few minutes and the
relation between the artefacts: *the participant first writes a draft,
which is the position the dialogue starts from and returns to.* If
Stage 1 is not yet accepted, the task description should say that; if
it is, decisions 1–3 are the ones a reviewer would want to see as a
stated change, and 4–5 are implementation. Either way the pilot should
ask one post-session question that the current design cannot: whether
the participant experienced the dialogue and the writing as one task.

## 7. Alternatives considered

- **Show the position in the writing pane** (a read-only rail of
  commitments, denials and implications beside the editor) and instruct
  the participant to write from it. Cheap, and it would make the
  position the material — but it connects the artefacts by instruction,
  not by construction, and does nothing for the baseline. Worth having
  *as well*, in the Elenchus condition, once the text is the positum.
- **Sequence the task**: dialogue first, then writing with the chat
  closed. Downstream by construction, but it forbids returning to test
  a claim that arose while writing, which is the most natural use of
  the dialectic.
- **Tell the opponent about the writing task** and nothing more.
  Removes the asymmetry; leaves the gap.
