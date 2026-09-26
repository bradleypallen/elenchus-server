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
| Let the clock **run past the task length** with a five-minute task | The two reminders appear; nothing locks; you can keep writing and finish later | `editor_events.json` has two `soft_warning_shown` rows |
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

Sign up as the judge from an invitation, assign texts, and:

| Do | Expect |
|---|---|
| Look at everything on the judge's screens for a clue to **who wrote a text, in which condition, or when** | Nothing: only the topic, the text and the form. Ids in the queue are handed out in submission order — does the order tell you anything? |
| Rate a text, **reload mid-form** | The unsaved form is gone (a rating is saved only on the button); nothing else changes |
| Submit, reopen, **revise** | Both ratings are kept; the newest counts. `text_judging.json` in the export shows the history |
| Press **assign** twice for the same judge | No duplicates — the second press adds nothing |
| A second judge on the same texts | Their queue is in a **different order** from the first judge's |
| Submit with a dimension missing | The button stays grey until all four have a number |
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
| **System** → *Consistency check* | Everything registered has a file and every file is registered |

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
