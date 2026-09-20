# Study Runbook

For the person **running the study day to day**: setting it up, enrolling
participants, sending links, handling what goes wrong, assigning texts to
the panel, exporting. Click by click.

This is the runbook for the Sloan Foundation-funded study (grant
G-2026-79650).

For *why* the study is built this way — the design, counterbalancing,
what is captured, blinding — read [Running a Study](study.md). For
consent, recruitment, scheduling and what to say to participants, follow
the study protocol; this runbook covers only the platform.

**You need:** the site address and an account — `researcher` or `admin`.
You get one from an **invitation link**: open it, choose a display name
and a password (the link works once), and from then on sign in at the
site's address with your email and password.

| Your account | The button, top right | What you see |
|---|---|---|
| `researcher` | **STUDY** | two tabs: **Study** and **Judging** — everything in sections 1–10 |
| `admin` | **ADMIN** | six tabs: the same two, plus **Invites**, **Users**, **Costs** and **System** — see [If you are the admin](#if-you-are-the-admin) |

**A researcher can:** set up a study, enrol participants, watch their
progress, close an abandoned session, assign texts to judges, export and
download the data.
**Only an admin can:** create accounts (including judges'), reset a
password, download the key that links codes to names, change the AI model
or its key, take a backup, see costs and alerts. If you are a researcher,
"ask the admin" below means exactly these.

Almost nothing needs access to the server itself; the few things that do
are listed [at the end](#what-still-needs-access-to-the-server).

---

## 1. Sign in

Go to the site, sign in. Press **STUDY** (or **ADMIN**) at the top right
and open the **Study** tab.

## 2. Set the study up — once

**Study** tab → the *Study* box → **Setup / edit topics** (it opens by
itself if nothing has been set up yet).

| Field | What to enter |
|---|---|
| Study id | A short name with no spaces, e.g. `PILOT`. It appears in export file names. Use a separate id — e.g. `TRAINING` — for practice. |
| Topic A / Topic B — title | What the participant will see as their topic. |
| Topic A / Topic B — brief | One or two sentences of framing, shown under the title. |
| Minimum hours between sessions | How long a participant's second link stays shut after their first session ends. `48` = two days. |
| Length of the main task, in minutes | Leave **empty** for the real study (the usual 60). For a practice study, enter `5`. It only drives the clock and its two reminders — nothing is ever cut off. Set it before the first participant starts and don't change it afterwards. |

Press **Save study**.

> **Get the topic wording right before you enrol anyone.** The wording is
> copied onto a participant's links at enrolment. If you edit a topic
> later, people already enrolled keep the *old* wording; only later
> enrolments get the new one.

## 3. Enrol a participant

**Study** tab → *Enrol participant*. Type the person's name (or whatever
label your tracking sheet uses) and press **Enrol**.

> Use the **ENROL button** — and enrol each person **once**. Every press
> creates a new participant and uses up a place in the randomization.

The platform then:

- gives them the next code — `P01`, `P02`, …;
- **decides at random** which condition and which topic they get first
  (balanced, so you never choose — and shouldn't);
- creates **both** of their session links.

They appear in the **Participants** list:

```
P03  Test Person Three    elenchus first · topic A first
  session 1  elenchus  Occurrence and its relatives…   scheduled   [COPY LINK]
  session 2  baseline  Taxon names and taxon concepts  scheduled   waiting: session 1 not started   [COPY LINK]
```

The name you typed is **never exported** — it is only for you. The code is
what appears in the data. Keep your own record of who is which code.

**Replacing a drop-out.** Tick **Place by hand** and choose the same
"first condition" and "first topic" as the person being replaced, then
Enrol. Only do this for replacements.

**"balance across the four order × topic cells: 3 / 2 / 3 / 2"** next to
the study name is how many people are in each of the four combinations.
It should stay within one of even. You don't need to act on it.

## 4. Send the links

Press **Copy link** next to a session and paste it into your message to
the participant.

- Send **session 1's link first**. Send session 2's when they've done
  session 1 (or send both, clearly labelled — the second simply won't
  open early).
- A link is **personal and works like a key**: anyone who has it can open
  that session. Send it to the participant only; never post it anywhere
  shared.
- A link isn't tied to one device. If a participant closes the browser,
  loses connection, or wants to switch computer mid-session, **the same
  link takes them back to where they were**, with their text intact.

## 5. What the participant goes through

Useful to know when they write to you. One session, roughly 90 minutes:

1. **Welcome** — what the session involves. They press *Begin tutorial*.
2. **Tutorial (~15 min)** — the real screen, on a practice topic ("kinds
   of pets"): the conversation with the AI on the left, **their text on
   the right**. Nothing here counts. They press *Start the main task*.
3. **Main task (~60 min)** — their topic and its brief appear above the
   writing box. They talk with the AI and write their introduction. The
   text **saves by itself** every few seconds.
   - A clock shows their time. About ten minutes before the hour a note
     suggests wrapping up; at the hour another says time is up. **Nothing
     locks or cuts off** — they finish when they're ready.
   - They press **Finish session**. That submits the text. It asks them
     to confirm, because **they can't come back to the task afterwards**.
4. **Questionnaires (~10–15 min)** — four short ones.
5. **Thank you.**

In one of their two sessions the AI is an ordinary chat assistant. In the
other it works differently: it keeps track of what they've committed to
and points out where two things they've said seem to pull against each
other, which they can accept or contest. **Don't tell participants which
is which, which is "the new one", or what the study expects to find.**

## 6. Watching progress

The Participants list shows each session's status:

| Status | Meaning |
|---|---|
| `scheduled` | Link not opened yet |
| `briefing` / `tutorial` / `active` | In progress — the participant is on that screen (or left it open) |
| `post_session` / `surveyed` | Text submitted; doing questionnaires |
| `complete` | Finished. **text ✓** confirms their text is in. |
| `interrupted` | Closed by a researcher (section 7) |
| `voided` / `expired` | Link cancelled / ran out |

Next to a waiting second session you'll see why it's waiting:
*"session 1 not started"*, *"session 1 still open"*, or *"opens Thu 8 Oct,
16:30 CEST"* — shown in **your** time zone. (The participant, if they try
the link early, is told the same moment in *theirs*.)

Reload the page to refresh the list.

## 7. When something goes wrong

| What you hear or see | What's happening | What to do |
|---|---|---|
| "My second link says it isn't open yet." | Working as designed: it opens after the minimum gap. The message tells them when. | Tell them the date. If the protocol genuinely allows an earlier session, the study lead can shorten the gap in Setup — it takes effect immediately. |
| "My second link says to do my first session first." | They clicked link 2 before link 1. | Point them to link 1. |
| "It says my first session is still open" — but they say they finished. | They reached the questionnaires (or earlier) and stopped. | Ask them to open **link 1** again and complete it. |
| A participant gave up partway and won't be finishing. Their session sits at `tutorial` / `active` forever, and link 2 won't open. | An unfinished first session holds the second one shut. | In the list, press **Close as interrupted** on that session. Everything recorded so far is kept; the session can't be reopened. Link 2 then opens once the gap has passed. **Tell the study lead** — an interrupted session is a protocol deviation to log. |
| "I closed the window / my laptop died." | Nothing is lost; the text autosaves. | They open the **same link** again. |
| "The AI isn't answering" / "Couldn't reach the AI service…" / "Something went wrong. Please try again." | A temporary problem reaching the AI. Their text is safe, and the failed message is logged. | Ask them to wait a minute and send the message again. If it persists for more than a few minutes, contact the admin — and note the time. The clock keeps running, so tell the study lead how long they lost. |
| "This study link has already been used." | The session behind that link is finished. | Expected once they're done. If they *weren't* done, contact the admin with the participant's code. |
| "I pressed Finish by mistake." | The text they had at that moment was submitted. **It can't be reopened.** | Tell the study lead, with the code and session number. Don't enrol them again. |
| You enrolled the wrong person / a duplicate. | A participant row can't be deleted. | Leave their links unsent. Tell the study lead so the code can be excluded — and so the randomization record stays honest. |
| A typo in a topic, found after enrolling people. | Enrolled participants keep the old wording. | Tell the study lead before changing anything. |
| The list shows nothing / "Researcher privilege required". | You're signed out, or signed in with a non-researcher account. | Sign in again. |
| **You** (or a judge) forgot a password. | If the site can't send email, "forgot password?" says so rather than pretending. | An admin presses **reset password** next to the person in the **Users** tab and sends them the one-time link it shows. With two admins, each can do this for the other. |

If in doubt: **write down the participant's code, the session number, the
time, and what they told you**, and pass it on. Never ask a participant to
send you their link "to check it" over a shared channel.

## 8. Getting the texts rated

When sessions have finished (you don't have to wait for all of them):

**Judging** tab → choose the study → choose a judge → **Assign all
submitted texts**.

- Do this **once per judge**. Every judge should get every text, unless
  the study lead says otherwise.
- **Press it again later** to hand over texts that came in since — it only
  adds new ones, and tells you how many.
- *Panel progress* shows how far each judge has got (`12 / 40`);
  *Submitted texts* shows how many judges have rated each text.

If the judge list is empty, nobody has created the judges' accounts yet —
an admin does that ([below](#creating-a-judges-account)). Send each judge
the [Guide for Judges](judge-guide.md) with their invitation. You don't
see the texts or the ratings here, and judges never see who wrote a text
or how — keep it that way: don't discuss individual participants or
sessions with a judge.

## 9. Exporting

**Study** tab → *Studies* → **Export** next to the study id. You can
export at any time — it's a snapshot, and doesn't change anything. Then
press **downloads** beside it: every export made for that study is listed,
newest first.

Each export makes **two files**:

- `study-PILOT-….tar.gz` — **the data. No names in it**: participants
  appear by code. Click the file name to download it. It is an ordinary
  compressed folder of JSON files (your computer can open it); the
  [study guide](study.md) says what each file holds.
- **names key (keep separate)** — the key that links codes to the names
  you typed when enrolling. **Only an admin sees this link.** It must
  never travel with the archive, sit on a shared drive, or be uploaded to
  any repository. Most days you don't need it at all.

Exports also stay on the server, so you can download one again later.

## 10. Ground rules

- Enrol each person once. Never choose anyone's condition or topic except
  to replace a drop-out.
- Links are keys. One person, private channel.
- Don't tell participants which condition is which, or what we hope to
  see. Don't help with the *content* of their text.
- Don't discuss participants with judges.
- Log anything unusual — code, session, time — and tell the study lead.
- Practise on a separate study id. Never put test people in the real one.

---

## Practice run

Do this once, start to finish, before the first real participant. It
takes about half an hour, and nothing in it needs anyone else's help.

1. Sign in → **STUDY** (or **ADMIN**) → **Study** tab. Set up a study
   called `TRAINING` with two topics, a gap of `0` hours and a task length
   of `5` minutes.
2. Enrol "Practice Person". Note their code, which condition is first,
   and that session 2 says *waiting: session 1 not started*.
3. Copy **session 2's** link and open it in a **private / incognito
   window**. Read the refusal. Close it.
4. Open **session 1's** link in the private window. Go through the
   welcome and the tutorial; type something in the practice text box.
5. Start the main task. Write two sentences. **Reload the page** — the
   text is still there and the clock kept going.
6. Close the window entirely. Open the same link again — you're back in
   the task.
7. Wait for the two time reminders — with a five-minute task they come at
   one minute and at five. Notice nothing locks.
8. Press **Finish session** with a very short text; read the
   confirmation; confirm. Do the questionnaires.
9. Back in your researcher window, reload: session 1 is `complete`,
   **text ✓**, and session 2 no longer says it's waiting.
10. Do session 2 the same way. Notice the AI behaves differently.
11. Enrol "Drop Out", open their session 1 link, stop at the tutorial.
    Back in the list, press **Close as interrupted**.
12. A practice judge. If you are an admin, [create one](#creating-a-judges-account)
    and open its invitation link in a private window to sign up; if you
    are a researcher, ask an admin for one. Then **Judging** → assign all
    texts to them; as the judge, in the private window, rate one text,
    reopen it and revise; check *Panel progress* from your own window.
    Read the [Guide for Judges](judge-guide.md) as you go — it is what you
    will send the real panel.
13. **Export** `TRAINING`, press **downloads**, and download the archive.
    Open it: find your practice person's text and the judge's rating. If
    you are an admin, download the **names key** once too, to see what it
    is — then delete your copy.
14. If you are an admin: open **System** and press **Back up now**; open
    **Costs** and find what your practice run cost under *Studies*.

Leave `TRAINING` in place — you can practise in it again, and it never
mixes with the real study's data.

---

## If you are the admin

Everything above works the same for you. These are the extra things only
you can do. For more detail on any of them, see
[Administration](administration.md).

### Creating a judge's account

**Invites** tab → choose the role **judge** → enter the judge's email (so
you can tell the invitations apart) → **Issue**. The page shows **a
link**. If the site can send email, the judge gets it by email too;
otherwise **copy the link and send it yourself**, with the
[Guide for Judges](judge-guide.md). The link works once and lasts 30 days.

The same goes for a second researcher or another admin: choose that role
instead. Accounts can't be created any other way.

> **Never change a judge's role once texts are assigned to them**, and
> don't give a judge a second, more powerful account. A judge who can see
> the Study tab sees which condition produced each text. The platform
> refuses the role change; don't work around it.

### People and passwords

**Users** tab. Next to each person: **reset password** (gives you a
one-time link to send them — it also signs them out), **deactivate**
(they can no longer sign in; everything they did is kept), and the *kind*
menu to change someone's role. You can't change your own role or remove
the last admin — that is what a **second admin** is for. Have one.

### The names key

In **downloads** (section 9) you — and only you — see a second link,
*names key (keep separate)*. It asks before downloading, and the download
is recorded. Keep it somewhere the data archive is not.

### Before and after each day of sessions

**System** tab:

- **Status** should be all green: *API key set*, enough disk. If it says
  **NO API KEY**, participants can't get replies — set the key with the
  gear icon on the home page before anyone starts.
- **Alerts** lists what the platform has complained about: an AI outage,
  a rejected key, a day's spend far above normal. If a participant
  reports the AI not answering, look here first; note the time.
- **Back up now** — before the first session of the day and after the
  last. A backup protects against a mistake or a bad upgrade. It stays on
  the server; the *export* is what you download to keep the data.

**Don't change the AI model while a study is running** (gear icon → model).
Every turn records the model it used, so a change would show — but it
would still muddy the comparison.

### Costs

**Costs** tab: what the AI has cost — by day, by model, and per study
session under *Studies* — against the budget you enter, with a ledger for
hosting bills underneath. It raises an alert if a single day's spend
passes a threshold you can set there. See
[Cost and usage](administration.md#cost-and-usage).

### What still needs access to the server

Not much, and none of it is part of running a session:

- **upgrading** to a new release, and **restoring** from a backup;
- **copying backups off the server** (a backup on the server doesn't
  survive losing the server);
- **setting up email** (until then: copy links by hand, as above) and
  having alerts emailed;
- **fixing the server's clock** to UTC if the System tab says it isn't.

If the site is down altogether, or System shows a problem you can't
clear, that is for whoever maintains the server — send them what the
System tab says and the time.
