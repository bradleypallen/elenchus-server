"""
notice.py — what every account holder is told, and the one choice they
are asked to make (docs/data-access.md, policy version 5).

The notice is versioned: `NOTICE_VERSION` is stamped on each acceptance
(`actors.terms_version`, `consent_events`), so a later rewording is
shown again and the record says which wording a person accepted.
**Bump the version on any change to `NOTICE_TEXT`.** The text is plain
paragraphs; the sign-up form and the first-sign-in gate render it as
such, with `POLICY_URL` linked beneath.

Study participants never see this: they come in by a link, not an
account, and their consent is the study's.
"""

from __future__ import annotations

NOTICE_VERSION = "1"

POLICY_URL = "https://bradleypallen.github.io/elenchus-server/data-access/"

NOTICE_TEXT = (
    "Elenchus records everything that happens in a dialectic: what you "
    "write, what the AI answers, every change to your position, and each "
    "exchange with the AI exactly as it was sent and received. That is how "
    "the system works and how it can be studied; it is kept for as long as "
    "the dialectic exists.\n\n"
    "Your dialectics are yours. Only you can open, change or delete them. "
    "An administrator can look at one only for a stated reason — you asked "
    "for help, a problem is being investigated, an abuse or policy concern "
    "— read-only, and every such look is recorded; you can see when it "
    "happened from the dialectic itself. An administrator can delete one of "
    "your dialectics only at your request, when your account is closed, or "
    "for an abuse or policy concern, and that too is recorded.\n\n"
    "You can download everything held about any of your dialectics at any "
    "time, and delete any of them; deleting removes it at once, with copies "
    "surviving only in backups until those are replaced.\n\n"
    "One choice is yours to make: whether your dialectics may be used in "
    "the project's research — read for analysis, quoted in worked examples "
    "or papers under a pseudonym, never with your name. It is off unless "
    "you turn it on, and you can turn it off again at any time from "
    "Privacy; from then on nothing of yours goes into a research export."
)

# What the notice must still say, in one line each, so a rewording
# can't quietly drop a promise (tests/test_notice.py checks these).
PROMISES = (
    "records everything",
    "Only you can open, change or delete",
    "stated reason",
    "recorded",
    "download everything",
    "off unless you turn it on",
    "turn it off again at any time",
)


def payload() -> dict:
    """What the sign-up form and the first-sign-in gate show."""
    return {
        "version": NOTICE_VERSION,
        "text": NOTICE_TEXT,
        "paragraphs": NOTICE_TEXT.split("\n\n"),
        "policy_url": POLICY_URL,
    }


def is_current(version: str | None) -> bool:
    return version == NOTICE_VERSION
