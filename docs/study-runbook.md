# Study Runbook

For the person **running the study day to day**: setting it up, enrolling
participants, sending links, handling what goes wrong, assigning texts to
the panel, exporting. Click by click.

For *why* the study is built this way — the design, counterbalancing,
what is captured, blinding — read [Running a Study](study.md). For
consent, recruitment, scheduling and what to say to participants, follow
the study protocol; this runbook covers only the platform.

**You need:** the site address, and a `researcher` account (email +
password) from the study admin.

**You can:** set up a study, enrol participants, watch their progress,
close an abandoned session, assign texts to judges, export.
**You can't** (ask the admin): create or reset accounts, create judges,
change the AI model or its key, fix the server.

---

## 1. Sign in

Go to the site, sign in. Press **STUDY** (top right). You'll see two
tabs: **Study** and **Judging**.

## 2. Set the study up — once

**Study** tab → the *Study* box → **Setup / edit topics** (it opens by
itself if nothing has been set up yet).

| Field | What to enter |
|---|---|
| Study id | A short name with no spaces, e.g. `PILOT`. It appears in export file names. Use a separate id — e.g. `TRAINING` — for practice. |
| Topic A / Topic B — title | What the participant will see as their topic. |
| Topic A / Topic B — brief | One or two sentences of framing, shown under the title. |
| Minimum hours between sessions | How long a participant's second link stays shut after their first session ends. `48` = two days. |

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

If the judge list is empty, the admin hasn't created the judges' accounts
yet. You don't see the texts or the ratings here, and judges never see
who wrote a text or how — keep it that way: don't discuss individual
participants or sessions with a judge.

## 9. Exporting

**Study** tab → *Studies* → **Export** next to the study id. It reports
where the archive was written **on the server**; ask the admin to fetch
it. You can export at any time — it's a snapshot, and doesn't change
anything.

Each export makes **two files**:

- `study-PILOT-….tar.gz` — the data. No names in it.
- `study-PILOT-….pseudonyms.json` — **the key that links codes to
  names.** It must never travel with the archive or be uploaded to any
  repository.

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
takes about half an hour if the site has been started with a short task
clock (ask the admin for `ELENCHUS_TASK_MINUTES=5`).

1. Sign in → **STUDY**. Set up a study called `TRAINING` with two topics
   and a gap of `0` hours.
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
7. Wait for the first time warning, then the second. Notice nothing locks.
8. Press **Finish session** with a very short text; read the
   confirmation; confirm. Do the questionnaires.
9. Back in your researcher window, reload: session 1 is `complete`,
   **text ✓**, and session 2 no longer says it's waiting.
10. Do session 2 the same way. Notice the AI behaves differently.
11. Enrol "Drop Out", open their session 1 link, stop at the tutorial.
    Back in the list, press **Close as interrupted**.
12. If a practice judge account exists: **Judging** → assign all texts to
    them; sign in as the judge in a private window, rate one text, reopen
    it and revise; check *Panel progress* from your researcher window.
13. **Export** `TRAINING`. Ask the admin to show you the two files.
