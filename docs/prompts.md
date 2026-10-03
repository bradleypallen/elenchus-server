# The Opponent's Prompts

The words the LLM runs under are part of the study's method, so they are
kept the way the method is kept: as files, under version labels, with a
history, and a test that fails the moment one changes by accident.

## Where they live

Each prompt is a file in `src/elenchus/prompts/`, one per *family*:

| Family | File | Used for |
| --- | --- | --- |
| `elenchus` | `elenchus.md` | The Elenchus-condition opponent (the study's default, the dialectic interface) |
| `baseline` | `baseline.md` | The baseline assistant of the comparison condition; the topic is appended at run time |
| `phase_b` | `phase_b.md` | The Elenchus prompt plus the theory-articulation speech acts; off unless `ELENCHUS_ENABLE_PHASE_B` is set, never used in the study |

A file is a short header and then the text, byte for byte:

```text
---
family: elenchus
version: elenchus/2026-06-10
date: 2026-06-10
changed: one or two lines on what this revision changed and why
---
You are the opponent in an Elenchus dialectic ...
```

The `version` is the **label**: the family, a slash, and the date of the
revision. The text after the header is what the LLM is sent; the
server's fingerprint of it (SHA-256) is what the capture log records.

## What is recorded

Every exchange with the LLM writes a row to the dialectic's turn log
with the prompt's **name** (`sloan`, `baseline` or `phase_b`), its
**version label** and the **SHA-256 of the text as sent** — for the
baseline, that is the text with the topic appended, so the hash varies
by topic while the label does not. The study export's manifest lists the
label and hash of each family in force when the export was made, beside
the versions of Elenchus, pyNMMS and DuckDB. The System tab shows the
same block, and the server log prints it at startup.

So a session can always be traced to the exact wording it ran under, and
an export can state, in one line, which prompt a registered study used.

## History

| Label | SHA-256 of the text | First shipped | What changed |
| --- | --- | --- | --- |
| `elenchus/2026-06-10` | `9980b4e85362740c36fbb218c7b090af966f5b7007f4cc0cdfc7c8146e3c0f50` | 0.2.0 (unchanged since; as a file from 0.11.0) | The Elenchus-condition opponent as frozen for the Sloan Foundation-funded study: the six speech acts, tension construction, the UI-driven-actions rule. Unchanged since the Phase B firewall of 2026-06-10. |
| `baseline/2026-09-19` | `ac7119d9bd28700c913fef51e4ce4b7fa01ae0325cc1a97cc62969040dd1bb20` | 0.4.0 (as a file from 0.11.0) | The baseline assistant for the prose-introduction task, rewritten when the writing pane arrived. The hash is of the template; each session's recorded hash includes its topic. |
| `phase_b/2026-06-10` | `85b67c96f7a8638b1cec8aa32b3052e7afc06f509a0d77b06dd5e3ee88f8bf9c` | 0.2.0 (as a file from 0.11.0) | The Elenchus prompt plus `ASSERT_IMPLICATION` / `INTRODUCE_BEARER` / `RETRACT_IMPLICATION`. Off by default. |

Turns recorded before 0.11.0 carry the hash but no label; the hashes
above identify them.

## Revising a prompt

A prompt changes only through a release. The steps:

1. Edit the family's file. Set `version` to `family/YYYY-MM-DD` (the
   date of the revision), update `date`, and write `changed` as the one
   or two lines a reader of this page will want.
2. Run the suite. `tests/test_prompts.py` pins each family's label and
   hash and fails until its pins are updated to the new values — that is
   the point: a prompt never changes without the pins, this page and the
   changelog changing with it.
3. Add a row to the table above (keep the old rows: sessions recorded
   under them exist) and a line to the changelog.
4. Release. The next server start logs the new label and hash, and every
   turn from then on records them.

**A registered study's prompt does not change.** Once a study's
allocation seed is set, the prompt its sessions run under is the one its
registration cites; a revision after that point is a protocol deviation
to be reported as such, not a routine release. Tune before the seed is
set, on a development instance or a development study.

## Trying a revision without a release

A development instance can run a candidate prompt from outside the
package: set `ELENCHUS_PROMPT_DIR` to a directory holding files with the
same names and header format, and the server reads those instead. Each
turn then records the candidate's label and hash, so the sessions used to
judge it say which wording they ran under, and the System tab says an
override is in force.

The override is **refused on an instance that carries a registered
study** — one whose configuration has an allocation seed. At startup the
server logs the refusal at ERROR and runs the packaged prompts; nothing
else changes. A study instance therefore cannot be made to run an
untracked prompt by an environment variable, and a tuning run cannot
leak into a study by a copied service file.
