"""
study_enrolment.py — allocating participants in the crossover design.

Each participant does two sessions: one per condition, a different
topic each time. Two things vary between participants and both can
bias the comparison if left to chance or to the researcher's habit:

  * which **condition** comes first (practice / fatigue carry over), and
  * which **topic** is met in which condition (topics differ in
    difficulty however carefully they are matched).

Crossing them gives four cells. Participants are allocated by
**permuted-block randomization**: every consecutive block of four
enrolments in a study contains each cell exactly once, in random order.
So the cells stay balanced at every point in recruitment (not just in
expectation), and within a block the next allocation can't be predicted
by the researcher until the block's last place.

Pure functions only — no database access — so the allocation logic can
be tested exhaustively. `db/platform.py` stores the result.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass

logger = logging.getLogger(__name__)

CONDITIONS = ("elenchus", "baseline")
TOPICS = ("A", "B")


@dataclass(frozen=True)
class Cell:
    """One counterbalancing cell: what the participant meets first."""

    first_condition: str
    first_topic: str

    def __post_init__(self):
        if self.first_condition not in CONDITIONS:
            raise ValueError(f"first_condition must be one of {CONDITIONS}")
        if self.first_topic not in TOPICS:
            raise ValueError(f"first_topic must be one of {TOPICS}")

    @property
    def key(self) -> str:
        return f"{self.first_condition}-first/topic-{self.first_topic}-first"


ALL_CELLS: tuple[Cell, ...] = tuple(Cell(c, t) for c in CONDITIONS for t in TOPICS)
BLOCK_SIZE = len(ALL_CELLS)


def other(value: str, pair: tuple[str, str]) -> str:
    return pair[1] if value == pair[0] else pair[0]


def session_plan(cell: Cell) -> list[dict]:
    """The two sessions a participant in `cell` does, in order."""
    return [
        {"period": 1, "condition": cell.first_condition, "topic": cell.first_topic},
        {
            "period": 2,
            "condition": other(cell.first_condition, CONDITIONS),
            "topic": other(cell.first_topic, TOPICS),
        },
    ]


def next_cell(previous: list[Cell], rng: random.Random | None = None) -> Cell:
    """Allocate the next participant, given the study's block-allocated
    participants so far, in enrolment order.

    The current block is the tail of `previous` after the last complete
    block of four; the draw is uniform over the cells that block hasn't
    used yet. Manually-allocated participants must be left out of
    `previous` — they sit outside the blocks (a replacement takes the
    cell of the person replaced), and counting them would unbalance the
    block they happened to land in.
    """
    rng = rng or random.SystemRandom()
    in_block = previous[len(previous) - (len(previous) % BLOCK_SIZE) :]
    remaining = [c for c in ALL_CELLS if c not in in_block]
    if not remaining:  # a block can't repeat a cell, but don't trust the data
        logger.warning("Enrolment block had duplicate cells; starting a fresh block")
        remaining = list(ALL_CELLS)
    cell = rng.choice(remaining)
    logger.info(
        "Allocated cell %s (enrolment #%d, place %d of block %d; %d cells were open)",
        cell.key,
        len(previous) + 1,
        len(in_block) + 1,
        len(previous) // BLOCK_SIZE + 1,
        len(remaining),
    )
    return cell


def participant_code(sequence_number: int) -> str:
    """P01, P02, … — a code carries no information about the person or
    their allocation."""
    return f"P{sequence_number:02d}"
