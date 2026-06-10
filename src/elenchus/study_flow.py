"""
study_flow.py — Sloan participant session state machine.

The participant flow has six progress states plus two terminal
alternatives:

    briefing  → tutorial  → active  → post_session  → surveyed  → complete
                                                  ⌃             ⌃
                                                expired      interrupted

`briefing` (initial)   — consent form, study overview shown by the
                         platform. Participant has not entered the
                         working interface yet.
`tutorial`             — 15-minute warm-up with a trivial domain
                         (e.g. "kinds of pets"). Same dialectic
                         interface they'll use in `active`.
`active`               — the timed conceptual-specification task
                         the study is actually measuring. Dialectic
                         base exists during this state.
`post_session`         — end-of-task summary shown to the participant
                         before questionnaires fire.
`surveyed`             — NASA-TLX / SUS / TiAS / EEQ batteries.
`complete`             — terminal success. Token's job done.
`expired`              — scheduled_end passed before the participant
                         reached `complete`.
`interrupted`          — researcher voided or platform crashed; the
                         session is unusable for analysis.

The machine is one-way (no rewinds). `advance_session_state` enforces
only the documented transitions; any other move raises ValueError.
That keeps the platform's "what comes next" routing deterministic —
the participant never sees an inconsistent screen because we never
let the data get there.
"""

from __future__ import annotations

from enum import StrEnum


class SessionState(StrEnum):
    BRIEFING = "briefing"
    TUTORIAL = "tutorial"
    ACTIVE = "active"
    POST_SESSION = "post_session"
    SURVEYED = "surveyed"
    COMPLETE = "complete"
    EXPIRED = "expired"
    INTERRUPTED = "interrupted"


# The directed graph of allowed transitions. Every transition is
# explicit; missing edges are blocked. Terminal states (complete,
# expired, interrupted) have no outgoing edges — once you're done,
# you're done.
ALLOWED_TRANSITIONS: dict[SessionState, frozenset[SessionState]] = {
    SessionState.BRIEFING: frozenset({SessionState.TUTORIAL, SessionState.INTERRUPTED}),
    SessionState.TUTORIAL: frozenset(
        {SessionState.ACTIVE, SessionState.EXPIRED, SessionState.INTERRUPTED}
    ),
    SessionState.ACTIVE: frozenset(
        {
            SessionState.POST_SESSION,
            SessionState.EXPIRED,
            SessionState.INTERRUPTED,
        }
    ),
    SessionState.POST_SESSION: frozenset(
        {SessionState.SURVEYED, SessionState.EXPIRED, SessionState.INTERRUPTED}
    ),
    SessionState.SURVEYED: frozenset({SessionState.COMPLETE, SessionState.INTERRUPTED}),
    # Terminal — no outgoing edges.
    SessionState.COMPLETE: frozenset(),
    SessionState.EXPIRED: frozenset(),
    SessionState.INTERRUPTED: frozenset(),
}


TERMINAL_STATES = frozenset(
    {SessionState.COMPLETE, SessionState.EXPIRED, SessionState.INTERRUPTED}
)

# States during which the participant is actively working in the
# interface (and therefore the platform should keep the session
# cookie alive). Briefing is included because it's the entry screen.
LIVE_STATES = frozenset(
    {
        SessionState.BRIEFING,
        SessionState.TUTORIAL,
        SessionState.ACTIVE,
        SessionState.POST_SESSION,
        SessionState.SURVEYED,
    }
)


def parse_state(s: str) -> SessionState:
    """Strict parser — unknown values raise ValueError. Used by the
    platform DB helpers to validate input from API bodies."""
    return SessionState(s)


def is_terminal(state: SessionState) -> bool:
    return state in TERMINAL_STATES


def is_live(state: SessionState) -> bool:
    return state in LIVE_STATES


def can_transition(from_state: SessionState, to_state: SessionState) -> bool:
    """Whether the documented state machine allows
    `from_state → to_state`. Includes the no-op `from_state → from_state`
    as False; the caller decides whether to surface that as an error."""
    return to_state in ALLOWED_TRANSITIONS.get(from_state, frozenset())


def assert_transition(from_state: SessionState, to_state: SessionState) -> None:
    """Raise `ValueError` if the transition isn't allowed. Used by
    `pdb.advance_session_state` to fail fast on programming errors."""
    if not can_transition(from_state, to_state):
        raise ValueError(
            f"Invalid session state transition: {from_state.value} → {to_state.value}. "
            f"Allowed from {from_state.value}: "
            f"{sorted(s.value for s in ALLOWED_TRANSITIONS.get(from_state, frozenset()))}"
        )
