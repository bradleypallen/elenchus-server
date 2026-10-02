# Who may see or change a dialectic

*Decision record, 2026-10-02. Status: policy adopted; first of four work
packages built (0.9.5). The user-facing statement of the policy is
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
2. **The Dialectics tab — planned (platform migration 23).** An
   append-only access log; admin routes for a metadata list with owner
   names, and for view, PDF and delete actions that take a reason, write
   the log and, for view and PDF, return read-only content; all three
   refuse study records. A read-only viewer in the dashboard and a block
   of recent accesses in the System tab. The legacy study-report routes
   become participant-only.
3. **Owner-side transparency — planned.** The owner sees when an
   administrator viewed their dialectic; a "who can see this" link to the
   policy page from inside the application.
4. **Notice and research-use choice — planned, with the outreach mode.**
   The sign-up notice and revocable research-use flag described in
   [the outreach note](substructural-kb-prototype-and-outreach.md) (O8),
   and a pseudonymised export limited to those who opted in.

## 6. Rules for whoever adds a route

- Content of a dialectic reaches a non-owner only through the
  content-access module (package 2): a POST carrying a reason, a log row,
  a read-only response. Never through `_authorize_base_access`.
- A GET changes no state.
- A study session is looked up by its token. Anything that asks "which
  session is on this base" means the study session and must say so.
- A study record is never written or deleted by anyone but its own
  participant, and never after its session has moved on.
- A change to who can see what is a change to
  [docs/data-access.md](../docs/data-access.md): bump its policy version
  and say so in the changelog.

## 7. What remains open

- How long the access log is kept, and whether the owner is told at the
  time or only on looking (package 3).
- Retention of backups, which hold everything: a policy for the
  production host rather than for the code.
- Whether an admin's deletion of an ordinary dialectic (abuse, account
  closure) needs a second admin's confirmation, or the log suffices.
