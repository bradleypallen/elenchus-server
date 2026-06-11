"""
personas.py — the simulated cast.

Participant personas carry a domain pair (one per condition, since the
within-subjects design gives each participant structurally-comparable
domains across conditions) and, for the scripted driver, the exact
utterances they'll send. Judge personas carry a scripted rating
disposition. The LLM driver ignores the scripted fields and generates
content from the persona's framing instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ParticipantPersona:
    label: str
    elenchus_domain: str
    baseline_domain: str
    disposition: str = "cooperative"
    tutorial_message: str = "Let me start. A dog is a kind of pet, and a cat is too."
    # Scripted task utterances (ScriptedDriver). The LLM driver derives
    # messages from `elenchus_domain` / `baseline_domain` instead.
    scripted_task_messages: list[str] = field(
        default_factory=lambda: [
            "Here is my starting position on the domain.",
            "That follows — I'll commit to it.",
        ]
    )


@dataclass
class JudgePersona:
    label: str
    # Scripted rating disposition.
    favor: str = "a"  # 'a' | 'b' | 'tie'
    guess: str = "unsure"  # condition guess for both slots
    confidence: int = 2


DEFAULT_PARTICIPANTS: list[ParticipantPersona] = [
    ParticipantPersona(
        label="P-001",
        elenchus_domain="biome classification",
        baseline_domain="soil taxonomy",
        scripted_task_messages=[
            "Biomes should be classified primarily by climate.",
            "Yes, I accept that consequence.",
        ],
    ),
    ParticipantPersona(
        label="P-002",
        elenchus_domain="cell-type ontology",
        baseline_domain="protein-family taxonomy",
        disposition="stubborn",
        scripted_task_messages=[
            "A neuron is a kind of cell that transmits signals.",
            "I'm not sure that follows, but go on.",
        ],
    ),
    ParticipantPersona(
        label="P-003",
        elenchus_domain="legal-document types",
        baseline_domain="contract-clause taxonomy",
        scripted_task_messages=[
            "A contract is a legally binding agreement.",
            "Agreed, that holds.",
        ],
    ),
    ParticipantPersona(
        label="P-004",
        elenchus_domain="astronomical-object types",
        baseline_domain="mineral classification",
        disposition="terse",
        scripted_task_messages=[
            "A planet orbits a star and is roughly spherical.",
            "Fine.",
        ],
    ),
]


DEFAULT_JUDGES: list[JudgePersona] = [
    JudgePersona(label="J-001", favor="a", guess="unsure", confidence=2),
    JudgePersona(label="J-002", favor="b", guess="unsure", confidence=3),
]


def default_participants(n: int) -> list[ParticipantPersona]:
    """First `n` default participants (capped at the library size)."""
    return DEFAULT_PARTICIPANTS[: max(0, n)]


def default_judges(n: int) -> list[JudgePersona]:
    return DEFAULT_JUDGES[: max(0, n)]
