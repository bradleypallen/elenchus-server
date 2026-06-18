# EEQ review packet — for PI sign-off

The **Epistemic Experience Questionnaire (EEQ)** is the one post-session
instrument in the study that is *custom* — the other three (NASA-TLX,
SUS, TIAS) are validated and used as-is. EEQ is also the instrument
tied most directly to the hypothesis: it is meant to detect whether the
**AI-as-collaborator** (Elenchus) condition produces deeper epistemic
engagement — ownership, articulation, challenge — than the
**AI-as-tool** (baseline) condition. Because it is custom and
hypothesis-bearing, it needs the PI's explicit review before any real
data is collected.

This document lays out the eight items as currently shipped, the
construct each appears to target, concerns worth a psychometrician's
eye, and candidate rewordings. **Nothing here is a prescription** — the
flags are to make the PI's review fast and to catch the obvious issues
a non-specialist can see. The decision column is yours.

> **Operational note.** The items live in
> [`src/elenchus/questionnaires.py`](https://github.com/bradleypallen/elenchus-server/blob/main/src/elenchus/questionnaires.py)
> under `INSTRUMENTS["eeq"]`, scored 1–7 (Strongly disagree → Strongly
> agree). **Any change to item text or scale must bump
> `INSTRUMENT_VERSION`** (currently `"1"`) — it is stamped onto every
> stored response so cross-cohort data stays interpretable. Edit the
> items, bump the version, and `tests/test_questionnaires.py` /
> `tests/test_migrations.py` will lock in the new shape.

---

## Current items (verbatim)

| # | Item | Apparent construct |
|---|------|--------------------|
| 1 | The ideas in the final output feel like my own. | Ownership / authorship |
| 2 | The session helped me make explicit things I knew but had not put into words. | Articulation (tacit → explicit) |
| 3 | I was pushed to consider aspects of the domain I would not have considered on my own. | Challenge / perspective expansion |
| 4 | I changed my mind about something during the session. | Belief revision |
| 5 | I can defend every claim in the final output. | Justification / defensibility |
| 6 | The AI's contributions shaped the substance of the output, not just its wording. | Substantive contribution (collaborator-vs-tool manipulation check) |
| 7 | The session surfaced inconsistencies in my own thinking. | Challenge / elenctic function |
| 8 | I understand my own views on this domain better than I did before the session. | Self-understanding / clarity |

Declared constructs (from the module docstring): **ownership,
articulation, challenge**. Item 6 reads more as a manipulation check
than a construct item; item 4 and item 8 don't map cleanly onto the
three declared constructs (see per-item notes).

---

## Cross-cutting concerns (read these first)

These apply to the instrument as a whole and are the higher-leverage
decisions:

1. **No reverse-worded items → acquiescence bias.** All eight items are
   positively keyed (agreement = "more" of the good thing). SUS
   deliberately alternates polarity and TIAS includes five
   distrust-worded items precisely to counter agreement bias; EEQ has
   none. With expert participants who may want to be agreeable, a
   uniformly positive scale can inflate scores and mask the
   between-condition difference you're trying to detect. *Consider
   reverse-wording 2–3 items* (candidates noted below).

2. **Is EEQ a scored scale, a set of subscales, or a checklist?** This
   should be **pre-registered**, not decided at analysis time. If it's a
   summed/averaged score, the all-positive structure + single-item
   constructs undermine internal-consistency reporting (you can't report
   a meaningful Cronbach's α for a 1-item construct). Three options:
   - **(a) One unidimensional "epistemic engagement" score** — then
     verify the items hang together (and add items so α is reportable).
   - **(b) A priori subscales** (ownership / articulation / challenge) —
     then each needs ≥2–3 items; today ownership and articulation are
     thin.
   - **(c) A profile of distinct single-item indicators** — defensible,
     but then don't sum them; analyze item-by-item and correct for
     multiple comparisons.

3. **Manipulation-check vs outcome-measure conflation.** Items 3 and 7
   ("pushed to consider…", "surfaced inconsistencies…") describe the
   Elenchus *mechanism* almost literally. If EEQ is an **outcome**
   (did the participant engage more deeply?), near-verbatim mechanism
   items will simply detect "the dialectic happened," confounding
   mechanism with effect. If it's a **manipulation check**, that's fine
   — but then label it as such and keep it separate from any engagement
   outcome score. Decide which role EEQ plays.

4. **Condition-neutral wording (blinding within the instrument).** Good
   news: every item refers to a generic "the session" / "the AI" /
   "the final output", identical across conditions, so a baseline
   participant can answer all eight without the wording revealing which
   arm they were in. Keep this property if you reword.

5. **Reference target drift.** Items 1, 5, 6 ask about *the final
   output*; items 2, 3, 7, 8 ask about *the session/process*; item 8
   also about *understanding of the domain*. Output-focused and
   process-focused items may load on different factors — fine if
   intentional, worth confirming.

---

## Per-item notes & candidate rewordings

**1. "The ideas in the final output feel like my own."**
Construct: ownership. Generally clean and well-targeted. Minor: "feel
like my own" measures *felt* ownership (appropriate for a subjective
scale). Only item carrying ownership — thin if you want an ownership
subscale. *Possible companion (reverse-keyed):* "The final output reads
more like the AI's view than mine."

**2. "The session helped me make explicit things I knew but had not put
into words."**
Construct: articulation of tacit knowledge — well-phrased, arguably the
single best item. Mild compound clause ("knew but had not put into
words") but reads as one idea. Only articulation item.

**3. "I was pushed to consider aspects of the domain I would not have
considered on my own."**
Construct: challenge. Strong item, but see cross-cutting #3 (mechanism
vs outcome). "Pushed" is slightly loaded — neutral alt: "The session
led me to consider aspects of the domain I would not have considered on
my own."

**4. "I changed my mind about something during the session."**
Construct: belief revision. **Weakest fit.** It measures the
*occurrence* of a binary event on an agreement scale, and a participant
who entered with well-formed, correct views (and didn't change their
mind) is not thereby less epistemically engaged — so this may be a poor
indicator of the target construct and could even penalize the better
participants. "something" is vague. *Consider dropping, or reframing
toward depth:* "The session prompted me to reconsider views I came in
with."

**5. "I can defend every claim in the final output."**
Construct: justification. **"every" is an absolute** → ceiling/floor
effects and social-desirability pull (few will admit they *can't*
defend a claim). Softening helps variance: "I could justify the claims
in the final output if asked." Good reverse-key candidate: "There are
claims in the final output I'm not sure I could defend."

**6. "The AI's contributions shaped the substance of the output, not
just its wording."**
Reads as the **collaborator-vs-tool manipulation check** — exactly the
distinction the study manipulates. Recommend explicitly designating it
as a manipulation-check item (and expecting it to differ by condition
by design), rather than folding it into an engagement score.

**7. "The session surfaced inconsistencies in my own thinking."**
Construct: challenge / elenctic function. Same mechanism-vs-outcome
caution as #3. Also slightly presupposes inconsistencies *existed*; a
participant with internally consistent views answers low for a reason
unrelated to engagement quality.

**8. "I understand my own views on this domain better than I did before
the session."**
Construct: self-understanding. Clean, outcome-oriented, condition-
neutral — a good item. Doesn't map to the three declared constructs
(ownership/articulation/challenge); if you keep the subscale framing,
either add a "clarity/self-understanding" construct or assign it
deliberately.

---

## Suggested decision sheet (PI fills in)

| # | Item (short) | Flag | Keep / Reword / Drop | Notes |
|---|--------------|------|----------------------|-------|
| 1 | ideas feel like my own | thin construct | | |
| 2 | made tacit explicit | — | | |
| 3 | pushed to consider | mechanism-y; "pushed" loaded | | |
| 4 | changed my mind | weak construct fit | | |
| 5 | can defend *every* claim | absolute; soc. desirability | | |
| 6 | shaped substance not wording | = manipulation check | | |
| 7 | surfaced inconsistencies | mechanism-y; presupposes | | |
| 8 | understand my views better | — | | |

**Instrument-level decisions to record before launch:**
- [ ] EEQ's role: unidimensional score / a priori subscales / item profile (cross-cutting #2)
- [ ] Reverse-word ≥2 items to counter acquiescence? which? (#1)
- [ ] Designate item 6 (± 3, 7) as manipulation check vs engagement outcome (#3)
- [ ] If subscales: add items so ownership & articulation have ≥2–3 each
- [ ] Final wording approved → bump `INSTRUMENT_VERSION`, update tests
- [ ] Pre-register the scoring/analysis plan for EEQ specifically

---

*Prepared as an engineering aid to speed PI review — not a psychometric
authority. Final item wording, scoring, and analysis plan are the PI's
call. Once approved, the only code step is editing the item list in
`questionnaires.py` and bumping `INSTRUMENT_VERSION`.*
