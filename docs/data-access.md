# Who Can See What

This page says what Elenchus records about your work, who can see it, and
under what conditions. It applies to every instance run from this code;
the people running an instance may add to it, never take away from it.

*Policy version 1 — in force from release 0.9.5.*

## The short version

- **Your dialectic is yours.** Only you can open it, add to it, change it
  or delete it. That is true of everyone else on the platform, including
  its administrators.
- **Reading someone else's dialectic is never a side effect of a role.**
  When it is allowed at all, it is a deliberate, read-only act with a
  stated reason, and it is recorded.
- **A study session's record is frozen** once the session moves on, for
  everyone: the participant, the researchers and the administrators.

## What is recorded

For every dialectic, whether or not it is part of a study:

- the conversation: what you wrote and what the AI replied;
- your position as it developed: commitments, denials, tensions, and each
  change to them, with the time and whether you or the AI made it;
- for each exchange with the AI, exactly what was sent and what came
  back, the model that answered, and how long it took;
- how much the exchange cost in tokens.

For a study session, in addition: the text you write in the writing
panel (every saved draft), the fact and length of a paste (never its
content), the time you spent, your questionnaire answers, and any note
the study team logged about the session.

Your password is stored only as a one-way hash. The platform emails
account holders only, and only for invitations, login links and password
resets.

## Who can see it

| Purpose | Who | What they can see | How |
|---|---|---|---|
| Writing | you, the owner | everything in your own dialectics | the normal interface |
| Running the platform | administrators | accounts; the names of dialectics and who owns them; how much AI use each has cost; whether the system is healthy | the dashboard — **no content** |
| Helping you with a problem | administrators | one dialectic, read-only | only with a stated reason, recorded each time |
| Research | the study team | what study sessions produced, under participant codes rather than names | an export, never the live interface |

Two rules sit above the table.

**Nobody but the owner writes.** No role can add to, change or delete
another person's dialectic through the application. An administrator who
asks for someone else's dialectic gets the same "not found" as anyone
else.

**Study records are not opened by staff.** While a session is running
and after it ends, the team does not look at a participant's dialogue in
the application. What a session produced reaches the researchers through
the study export, where participants appear by code; the key from codes
to names is a separate file that only an administrator can download, and
each download is recorded. A study session's practice and task records
cannot be deleted through the application by anyone.

### Helping you with a problem: not yet available

The read-only view for administrators, with its reason prompt and its
log of every access, is being built. **Until it exists, nobody but you
can open your dialectic in the application at all.** When it arrives, a
view will require a typed reason, will be read-only by construction, will
be listed where the other administrator can see it, and will not be
offered for study records.

### People with access to the server

Whoever maintains the server an instance runs on can, technically, read
its files and its backups — as with any hosted service. They are bound
by this same policy: no reading of content without a purpose from the
table above, and a note of each occasion. Backups exist to restore the
service after a mistake or a failure, not as a way to read dialogues.

## Research use of ordinary dialectics

Dialectics that are not part of a study are **not used for research**.
A sign-up notice, and a choice about research use that you can withdraw,
are planned for instances that invite people in outside a study; until
they exist, ordinary dialectics are recorded only so that the application
works and so that problems can be diagnosed.

## Deleting

You can delete any of your own dialectics; its file is removed from the
server at once. Copies in backups remain until those backups are
replaced. If you took part in a study and want your data withdrawn, ask
the study team: study records are kept or removed according to the
study's consent terms, not through the application.

## How this is enforced

The rules above are implemented in the application, not left to
discipline: the ownership check has no exception for administrators, a
list of dialectics contains only the caller's own, a study record takes
changes only from its own participant and only while its session is on
it, and each of these is covered by automated tests that run on every
change. The system's consistency check reports any working session held
by someone other than a dialectic's owner.

## Questions

Ask the people running the instance you use. For the code and this
policy's history, see the
[project repository](https://github.com/bradleypallen/elenchus-server).
