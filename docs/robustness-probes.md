# Trying to Break It

For the person who will run the study, **after** the
[practice run](study-runbook.md#practice-run): a list of things to do
to the system on purpose, what should happen, and where to look to see
whether it did. Everything here is done from a browser, as an admin or
researcher; nothing needs the server.

The point is not to find that it works. It is to find where it
doesn't — before a participant does.

## Ground rules

- **Only on the practice site, never with real participants.** Make a
  study of your own for this (`PROBE-1`, `PROBE-2`, … — one per
  session of probing), so that `TRAINING` stays as you left it and the
  real study's data is never mixed with experiments. Practice sessions
  cost real money — about two dollars each — and that is fine; it shows
  up under *Studies* in the Costs tab.
- **Two windows.** Your own window for the dashboard, and a **private /
  incognito** window for the participant or judge. Two private windows
  (or two browsers) when a probe needs two people at once.
- **Write down what you did as you go**, with the participant code, the
  session number and the time. The system records the session in great
  detail; matching its record to what you did is how a finding gets
  confirmed.

### What "robust" means here

A probe passes when all of these hold; any one failing is a finding:

1. **Nothing is lost.** Text, turns and decisions survive whatever you
   did to the browser or the connection.
2. **Nothing lies.** When something fails, the person is told so in
   plain words (the messages in [section 7 of the runbook](study-runbook.md#7-when-something-goes-wrong)),
   never a spinner forever or a silent success.
3. **Nothing leaks.** A judge can't learn who wrote a text or how; a
   participant can't see another's.
4. **There is a way back.** The runbook has a step for it, and the step
   works.
5. **It was captured.** The export shows the failure or the recovery,
   not just the happy path (see [What was captured](#what-was-captured)).

### Recording a finding

Whatever you and the study lead agree on — an issue on the repository, a
shared document — each finding wants the same five things:

> What I did · What I expected · What happened (the exact words on
> screen) · Participant code, session number, time · How often (once /
> every time)

Include what the export says about it if you looked (the file name and
the line).

## The participant's session under bad conditions

Enrol a practice person and work through session 1 as them.

| Do | Expect | Look |
|---|---|---|
| Send a message, and **reload the page** while the AI is still answering | Either the reply is there when the page comes back (the server finishes the turn without you), or the whole exchange is absent and you send it again. Never a half-applied turn: a message with no reply, or a reply whose buttons don't work. | Transcript on screen; later `turn_log.json` has one row per exchange |
| Type two sentences in the text box, then **close the browser entirely**; open the same link | You are back in the task with the text and the clock where they were | *Session* still `active` in the Participants list |
| Open the link **on a phone** | Usable: you can read, reply, write, finish | — |
| Open the same session in **two tabs** and type in both | No error, and the later save wins; nothing corrupts | `text_snapshots.json` shows both |
| Paste a long block of text into the box | Accepted (or a clear message if there is a size limit); the paste is recorded by length only | `editor_events.json` has a `paste` with a length, no content |
| Send an empty message; send a very long message; send `{"speech_acts":[]}` | A sensible refusal or a normal reply — never a crash | — |
| Let the clock **run out** on a five-minute task, with the tab open | The reminder appears; at five minutes the editor locks and the screen says *Time is up*; the text as it stood is submitted | `text_snapshots.json` ends with a `timeout` row; the session is `post_session` in the list |
| Let the clock run out with the **tab closed**, then reopen the link | The same: you land past the task, not in it. The clock is the server's, not the browser's | Same |
| Send a message, or type, in the **last seconds** before the limit | Either it lands before the limit or it is refused with *Time is up*; nothing lands after | `turn_log.json`, `text_snapshots.json` timestamps |
| Press **Finish session** with the box empty | Refused with a message; the session stays `active` | — |
| Press **Finish**, cancel at the confirmation, keep working | Nothing was submitted | Text still editable |
| Press **Finish** while the AI is mid-reply | The text is submitted and the session moves on; the in-flight turn either completes or is logged as failed | `turn_log.json` |
| After finishing, **open the link again** | "This study link has already been used." — not the task | — |

## Links and gating

| Do | Expect |
|---|---|
| Open **session 2's** link before session 1 was started | Told to do session 1 first; the Participants list says *waiting: session 1 not started* |
| Open session 2 while session 1 is at `tutorial` or `active` | Told session 1 is still open; the list says *waiting: session 1 still open* |
| Set the study's gap to 48 hours, finish session 1, open link 2 | Told the moment it opens — **in your time zone** in the list, in the participant's on their screen. Set the gap back to 0 and try again: opens immediately |
| **Void** a link from the list and open it | Refused; the list shows `voided`. Void the *first* link of a pair: does the second still open? (It should — a voided first token doesn't hold the second.) |
| Open one link in **two browsers** at once | Both show the same session; no second session is created |
| Close an in-progress session with **Close as interrupted**, then open its link | Refused; the list shows `interrupted`; **everything captured so far is in the export** |
| In a study with an **allocation seed**, enrol someone and look for their order or their links anywhere — the roster, the balance line, the export | Nothing until you press **Schedule session 1**; then the order and both links appear, and the export's `allocation.json` gives their sequence letter |
| Try to set the seed **twice**, or after someone is enrolled | Refused, with the reason |
| Make a **typo in a topic** after someone is enrolled, then enrol another | The first keeps the old wording; the new one gets the new. (The runbook says: tell the study lead first.) |

## When the AI fails

This is the one fault you can inject yourself. It affects everyone using
the site while it is in place, so do it on the practice site only, and
put it back.

1. As admin, open the gear-icon **Settings** and change the model name
   to something that doesn't exist (`claude-nonexistent`). Save.
2. As a participant, send a message. **Expect:** one of the messages in
   runbook section 7 (*Couldn't reach the AI service…*, *Something went
   wrong…*), the text you had typed still there, the clock still running.
3. Send another. Reload. Send another. **Expect:** the same, every time;
   never a blank reply that looks like success.
4. **System** tab → *Alerts*: an alert for the failure, with the time.
5. Put the model name back. Send a message. **Expect:** a normal reply,
   and the conversation continues from where it was — the failed
   attempts are not in the transcript.
6. Later, in the export: `turn_log.json` has a row for **each failed
   attempt** with `outcome: llm_error`, and the Costs tab's *waste*
   figure counts them.

Also worth doing once: enrol two practice people, note which
**condition** each has first, and do a session in each. In the
*elenchus* condition the AI proposes tensions and there are accept /
contest buttons; in *baseline* it is an ordinary assistant and there
are none. The writing box, the clock and the questionnaires are the
same in both — if you can tell the conditions apart from anything
**other** than the conversation, that is a finding (a judge must not be
able to).

## Two at once

| Do | Expect |
|---|---|
| Two practice people in two private windows, sending messages at the same time | Both get replies; neither sees the other's conversation; both sessions progress in the list |
| A judge rating a text while a participant is mid-session | Both work |
| **Export** the study while a session is in progress | The export completes; the in-progress session is in it as far as it got |
| **Back up now** (System tab) while a session is in progress | The backup appears in the list; the participant notices nothing |

## The judge

Sign up as the judge from an invitation, assign the complete pairs, and:

| Do | Expect |
|---|---|
| Look at everything on the judge's screens for a clue to **who wrote a text, in which condition, or when** | Nothing: only the two topics, the two texts and the forms. The pair ids are your own; do the *A*/*B* labels or the order tell you anything? |
| Assign pairs to a judge **before** a participant has finished session 2 | That participant isn't offered yet; they appear once both texts are in, and pressing the button again adds exactly them |
| Rate one text of a pair, **reload mid-form** | The unsaved form is gone (a pair is saved only on the button); nothing else changes |
| Try to submit with a dimension unrated, a sentence missing, or no choice of the better text | The button stays grey until all of it is there |
| Submit a pair, reopen, **revise** a rating and the ranking | Both versions are kept; the newest counts. `text_judging.json` in the export shows the history for the ratings and the rankings |
| Press **assign** twice for the same judge | No duplicates — the second press adds nothing |
| A second judge on the same pairs | Their queue is in a **different order**, and the same two texts may carry the **other** labels |
| Look for the *one last step* box before the queue is done | It isn't there; it appears when every pair is done, and the guesses it asks for are per text, each shown again above its question, with a confidence |
| Give a guess, come back, change it | The newest counts; both are in `condition_guesses` in the export |
| As the judge, look for any way into the study side — a button, a menu, a URL | There is none; anything you find is a finding |

## What was captured

After any of the above, **Export** the study, press **downloads**, open
the archive and find the session under `sessions/`. Check the record
against what you did:

- `transcript.json` — every exchange you had, none you didn't.
- `turn_log.json` — one row per exchange **including the failed ones**
  (`outcome`), with what the AI actually returned (`raw_text`).
- `state_events.json` — every accept, contest, retract and commitment,
  with `source` (`opponent` or `ui`) — did the button you pressed
  produce an event?
- `text_snapshots.json` — the drafts, ending with the one marked
  `submit`; `editor_events.json` — your pastes (length only) and the
  reminders you saw.
- `integrity.json` — under `capture`, **`uncaptured_assistant_turns`
  must be 0**. Any other number means an exchange whose raw output
  can't be recovered, which is a finding in itself.
- `surveys.json` — the questionnaires you filled in.
- `text_judging.json` (at the top of the archive) — every rating with its four sentences, every ranking with which text each judge saw as *A*, every guess; all unblinded.

Then, as admin, download the **names key** once, confirm it maps the
pseudonyms in the archive to the accounts you used, and **delete your
copy**.

## Money and alerts

| Do | Expect |
|---|---|
| **Costs** tab → *Studies* | Your probe study, its sessions by condition, a cost per finished session in the range the study lead told you |
| Set the **daily spend alert** to `$1`, do a couple of turns, look at **System** → *Alerts* | An alert for crossing the threshold; press *Reset* afterwards to go back to the default |
| Enter a **budget** and an infrastructure charge, then void the charge | The bars move; the voided entry stays visible but is no longer counted |

## The admin side

| Do | Expect |
|---|---|
| Change a **judge's role** once texts are assigned to them | Refused, with a reason |
| Change **your own** role, or the **last admin's** | Refused |
| **Revoke** an invitation and open its link | Refused; the row says `expired` |
| Issue an invitation **without an email**; open it | Sign-up asks for the email |
| **Reset password** for a practice account and use the link | Works once; the second use is refused |
| **Deactivate** the practice judge, then sign in as them | Refused; their ratings are still in the export |
| On your home page, look for **anyone else's dialectic** — a participant's task or practice record, another account's | Not there: your list is your own, as an admin too |
| With a practice participant mid-task in a private window, put `/api/dialectics/` plus their task's name into the address bar of **your** admin window | *Not found* — the answer anyone who isn't its owner gets |
| After a practice session is complete, reopen its link and try to change anything in its record | You are past the task; nothing can be changed, by the participant or by you |
| **Dialectics** → search for a word that only appears *inside* someone's dialogue | Nothing: the search covers names and owners, never what was written |
| **Dialectics** → **View** on another account's dialectic, and submit with no reason, then a three-letter one | Refused both times; nothing is shown and nothing appears in the access log |
| Give a real reason and view it; look for any way to type, accept, contest, retract or delete | There is none: the view is read-only |
| In the viewer, take the **PDF** and the **Records**; then open **Access log** | Three entries — view, pdf, records — with your name, the owner, the time and your reason; the same three under *Content access* in **System** |
| Filter to **Study records** and try to open one | No *View* at all: the row says *via export* |
| Set up a study with **Development study** ticked, run a session in it yourself, then find it under **Study records** | The kind reads *study · task (development)* and **View** is there; it opens under a reason, and the access log entry says *(development study)* |
| On that development study, try **Set seed**; on a study that has a seed, try to tick **Development study** | Both refused: a development study never carries a seed, and a seeded study is a registered one |
| Enrol someone in an ordinary practice study, then try to tick **Development study** on it | Refused: the choice is fixed once anyone is enrolled |
| Open one of **your own** dialectics from the list | No reason asked, and no entry in the log |
| From one of your own dialectics, press **Records** in the top bar | An archive downloads: the position, the conversation, the turn log, the state events |
| **System** → *Consistency check* | Everything registered has a file, every file is registered, and no session is held by anyone but a dialectic's owner |

## What you can't probe from the browser

These need the server, so they are the admin's or the study lead's, and
worth asking about rather than assuming:

- What happens to a participant mid-turn when the **service restarts**
  (an upgrade, a reboot).
- A **restore** from backup, and whether the restore drill has been done.
- **Email** — on the practice site the mail provider is sandboxed, so
  invitations and reset links are not emailed to anyone but the account
  owner; the Invites tab tells you so each time. The real site will
  differ.
- The **server clock**, which stamps everything (the System tab shows
  it).

If something on this page turns out not to be true, that too is a
finding.
