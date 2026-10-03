# Who may see or change a dialectic

*Decision record, 2026-10-02. Status: policy adopted; the four work
packages built (0.9.5, 0.10.0), plus the development study
(0.11.0), the retirement of the legacy report flow and the
administrator's delete (0.12.0), and the notice, the research-use choice
and owner-side transparency (0.13.0) — all four packages built. The
user-facing statement of the policy is
[docs/data-access.md](../docs/data-access.md).*

## 1. How the question came up

The person running the study asked whether an administrator can retrieve
the sessions of ordinary users — not study participants — to examine
their dialogues. Answering it meant reading what the code allowed, and
what it allowed was more than anyone had decided:

- **An admin bypassed the ownership check on every dialectic route.**
  `_authorize_base_access` returned early for `kind == "admin"`, and the
  session-keyed resolver did the same. Seventeen routes sat behind it:
  read, message, tension, retract, derive, report, **delete**.
- **An admin's home list was every base in the platform**, each opening
  in the working interface, input box and all. No owner was shown.
- **Nothing was recorded** about who opened what, and nothing told a user
  that an administrator could.
- **The list wrote.** For every base it showed, `GET /api/sessions`
  opened a session row belonging to the admin, with the column default
  `state = 'active'` and no study token.

That last point broke something the study depends on. The freeze — "the
archived base is the state at submission and is never edited afterwards",
the registration's §2.4 — asked `find_session_by_base` for "the newest
session on this base". On a study task base the admin's row was newer
than the participant's study session and said `active`. So once any
admin had opened their home page, a submitted session's base accepted
edits again: `409` became `200`, for the participant and for the admin.
The participant's own session, text and hard stop were unaffected; no
real session had been run. It was found by reading the code and
confirmed by reproduction.

## 2. The policy

**Reading someone else's dialogue is an explicit, read-only, logged act
with a stated purpose — never a consequence of being an admin.**

| Purpose | Who | Sees | How |
|---|---|---|---|
| Authoring | the owner | everything, read and write | the working routes |
| Operations | admin | names, owners, sizes, activity, cost | a metadata list, no content |
| Support | admin | one dialectic, read-only | reason required, logged |
| Research | researcher, admin | content under codes, not names | export only |

Above the table: nobody but the owner's own session writes to a
dialectic; and study records are never opened in the working interface
by staff — frozen means frozen for everyone, and study content is
examined through the export. The principal investigator confirmed the
second explicitly: no staff viewing of a study session's dialogue before
analysis.

## 3. The practices behind it

- **Least privilege, separation of duties.** Running the platform and
  reading its content are different jobs.
- **No ambient authority.** Privileged access gets its own routes, built
  for the purpose. Reusing the owner's routes with a bypass is how a
  reader came to be able to write and delete.
- **Break-glass access.** A deliberate action, a typed reason.
- **Audit trail.** Append-only, visible to the other admin — the pattern
  already used for the names-key download.
- **Safe reads.** A GET changes nothing. The list that opened sessions
  was the textbook violation, and it is what broke the freeze.
- **Explicit keys, not heuristics.** A study session is found by its
  token, not by being the newest row.
- **Transparency and purpose limitation.** People are told what is
  recorded, who can see it and why; research use is a separate choice.
- **Pseudonymised research access**, as the study export already does.

## 4. Alternatives considered and rejected

- **A read-only flag on the existing bypass.** Keeps the ambient
  authority and relies on every future route remembering the flag. The
  next mutation route added would reopen the hole.
- **Impersonation ("view as this user").** Powerful for support, but it
  is write access by construction, it confuses the capture log about who
  did what, and it is the hardest thing to explain to a participant.
- **Live monitoring of study sessions by staff.** Useful for spotting a
  stuck participant, but it invites unblinding, and the freeze bug is
  what "staff in the participant's record" looks like when it goes
  wrong. The roster's session state and the deviation log answer the
  operational question without the content.
- **Leaving ordinary dialectics open to admins because the instance is
  small.** The same code will run the outreach mode and a public
  demonstration; the policy has to be right before the people arrive.

## 5. Work packages

1. **Close the hole — built, 0.9.5 (platform migration 22).** No bypass:
   the ownership check and the session resolver admit the owner only.
   Both list routes return the caller's own dialectics and create nothing
   for anyone else's. `find_session_by_base` selects on the study token.
   A study base takes changes only from its own participant and cannot
   be deleted through the API; `practice-N` is a study base only when it
   belongs to session N's participant. The migration removes the session
   rows held by non-owners, and the consistency check reports any that
   reappear (`sessions_not_owned`). Tests: an authorization matrix, a
   listing-creates-nothing check, the freeze regression with and without
   stray rows, the migration, and admin probes in the simulation.
   *Until package 2, an admin cannot open anyone else's dialectic at all.*
2. **The Dialectics tab — built, 0.10.0 (platform migration 23).** What
   the principal investigator asked of it: *find a given dialogue, view
   it, and download its raw logs for offline analysis.* So:
   - **Find** — `GET /api/admin/dialectics`, metadata only, searched,
     filtered and sorted in the browser over names and owners. It is a
     database search interface in every respect but one: it never
     searches or previews content, because that would be reading
     everyone's dialogues with no reason and no record.
   - **View** — a reason first (a category and a sentence), then the
     conversation and the position, read-only by construction: the view
     has no route that writes to a dialectic.
   - **Download** — the PDF and the raw records (state, transcript, turn
     log, state events, integrity, the per-base dump) under the same
     reason, which covers that dialectic for thirty minutes.
   - **The log** — `content_access_log`, append-only: every view, PDF
     and records fetch with who, which, whose, why and when; shown in the
     tab, downloadable, and in the System tab.
   - **Study records** are listed by participant code and refused by all
     three actions: study content comes from the study export.
   - **Your own records** — an owner can download the same archive of
     any of their own dialectics, no questions asked. It came for free
     and it is the right way round: the person a record is about can
     always have it.

   Raw-records download for *analysis* moved the policy on a point
   (version 2): ordinary dialectics are not used for research **without
   the owner's agreement**, which the administrator records as the
   reason *Owner agreed to analysis* until the application can ask for
   it itself (package 4).

   Left out of this package, on purpose: an administrator's **delete**
   (the tab ships with nothing in it that can destroy data; it wants its
   own small change with a typed confirmation), and closing the **legacy
   study-report routes** to staff (`generate-report` / `report` still
   admit a researcher — they belong to the retired paired-report flow,
   and shutting them means retiring that flow and its tests together).
   Both were done in 0.12.0 — see 2c and 2d below.

   **2b. The development study — built, 0.11.0 (platform migration 24,
   policy version 3).** The study team has to read whole sessions to
   tune the opponent's prompt before the main study, and the policy
   closes real study records to staff absolutely. The two are
   reconciled by making the *study* say whose records it holds: a study
   set up with `development` ticked is one the team runs on itself —
   the identical participant flow (practice, task, clock, freeze), so
   what is tuned is what participants will meet — and its records are
   the team's own material, opened in the Dialectics tab like an
   ordinary dialectic (reason, read-only, logged, the log row marked
   `study_dev`). What keeps this from becoming a side door: the mark
   and the allocation seed exclude each other (a seeded study is a
   registered one and can never be marked; a development study can
   never be seeded), and the mark is fixed once anyone is enrolled or
   issued a link, so a study with real participants cannot be opened by
   editing its setup later. The export of a development study says
   what it is in its manifest. The alternative — tuning on the team's
   own ordinary dialectics — was kept too (it needs nothing), but it
   does not exercise the study flow, which is where the prompt is
   actually used.
   **2c. The legacy paired-report flow retired — 0.12.0.** Not brought
   under the access log but removed: `generate-report` sent a
   participant's dialogue to the model at a researcher's request, and a
   reason-gated route that *also* ships study content to a third party
   is a bigger question than a log row answers; nothing needed the
   output. Routes, module, UI, export files and tests went together;
   the tables stay (migrations are forward-only) and nothing writes to
   them. This closed the last path to study content outside the export.

   **2d. An administrator's delete — 0.12.0 (platform migration 25,
   policy version 4).** `POST /api/admin/dialectics/delete`: a reason of
   its own from a narrower set (the owner asked; account closure; abuse
   or policy concern; other) — a reason given to *look* never covers
   destroying — the dialectic's name typed back, and two log rows (the
   grant and `delete`) that outlive it. Study records, real or
   development, are refused as they are everywhere. The question of a
   second administrator's confirmation was settled for now in favour of
   the log: at the pilot's scale one administrator's logged act with the
   name typed is proportionate, and the other administrator sees it.
   Revisit if the instance ever has more than a handful of staff.
3. **Owner-side transparency — built, 0.13.0 (policy version 5).** The
   owner's list marks a dialectic an administrator has looked at, and
   **Access** in the dialectic lists each occasion with the time, the
   action, the administrator's name and the *category* of the reason.
   The sentence is withheld on purpose: it may name a third party (an
   abuse report), and the category already says what kind of thing it
   was. The policy page is linked from the notice, from Privacy and
   from the access notes.
4. **Notice and research-use choice — built, 0.13.0 (platform
   migration 26, policy version 5).** A versioned notice (`notice.py`)
   that no account opens without, shown again on a rewording and once
   to accounts that predate it; a research-use choice that defaults to
   no, is made on the form or under Privacy, is revocable, and whose
   every change is a `consent_events` row; and the research export —
   only the ordinary dialectics of those whose choice is on at the
   moment of export, pseudonymized, the key in a separate admin-only
   file, each dialectic taken logged as a records fetch under *Owner
   agreed to analysis* so the owner can see it. Built ahead of the
   outreach mode: the choice is the same whether people come in by an
   invitation or a cohort, so it belongs with the policy, not the mode.
   Study participants are untouched by all of it — their consent is the
   study's — and study records never enter a research export.

## 6. Rules for whoever adds a route

- Content of a dialectic reaches a non-owner only through the
  content-access module: a POST carrying a reason (or a grant still in
  its window), a log row, a read-only response. Never through
  `_authorize_base_access`.
- The admin's list carries metadata only. Nothing derived from content —
  no preview, no snippet, no match count — goes into it.
- A GET changes no state.
- A study session is looked up by its token. Anything that asks "which
  session is on this base" means the study session and must say so.
- A study record is never written or deleted by anyone but its own
  participant, and never after its session has moved on.
- A change to who can see what is a change to
  [docs/data-access.md](../docs/data-access.md): bump its policy version
  and say so in the changelog.

## 7. What remains open

- How long the access log is kept. The owner is told on looking
  (Access), not at the time; a notification would need email to users,
  which the platform sends only to account holders and only for
  sign-in — a later question.
- Whether opted-in outreach dialectics feeding papers needs the same
  ethical review as the study (the outreach note's question): for the
  institution, before the first research export is used.
- Retention of backups, which hold everything: a policy for the
  production host rather than for the code.
- Whether an administrator's deletion should one day need a second
  administrator's confirmation (settled for the pilot: the log suffices;
  see 2d).
