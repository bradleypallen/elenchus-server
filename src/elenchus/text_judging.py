"""
text_judging.py — the rubric the expert panel rates texts against.

Each submitted text gets **absolute** ratings on four dimensions — the
ones participants were told about when topics were solicited: "coverage,
correctness, concision, and whether the reasoning holds together."

The rubric lives in code, versioned, for the same reason the
questionnaires do: every rating is stamped with the `RUBRIC_VERSION` it
was made under, so the export reproduces exactly what the judge was
asked. **Bump `RUBRIC_VERSION` on any change to a label, a help text or
the scale** — and don't change it mid-study without a reason you'd be
willing to write in the paper.
"""

from __future__ import annotations

RUBRIC_VERSION = "1"

SCALE_MIN = 1
SCALE_MAX = 7
SCALE_ANCHORS = {SCALE_MIN: "very poor", SCALE_MAX: "excellent"}

DIMENSIONS: tuple[dict, ...] = (
    {
        "key": "coverage",
        "label": "Coverage",
        "help": (
            "Does the text cover the concepts, and the relationships between them, "
            "that a colleague new to this topic would need?"
        ),
    },
    {
        "key": "correctness",
        "label": "Correctness",
        "help": (
            "Is what it says accurate — are the definitions, relationships and "
            "boundaries consistent with sound expert understanding of the topic?"
        ),
    },
    {
        "key": "concision",
        "label": "Concision",
        "help": "Does it say what it needs to say without redundancy, padding or digression?",
    },
    {
        "key": "reasoning",
        "label": "Reasoning holds together",
        "help": (
            "Are the distinctions it draws motivated, its claims consistent with one "
            "another, and the consequences of drawing the boundaries where it does "
            "followed through?"
        ),
    },
)

DIMENSION_KEYS: tuple[str, ...] = tuple(d["key"] for d in DIMENSIONS)

CONDITION_GUESSES = ("elenchus", "baseline", "unsure")

# What the judge is shown for the blinding check. The labels describe a
# way of working, not the study's internal condition names.
CONDITION_GUESS_LABELS = {
    "elenchus": "structured disagreement with the AI",
    "baseline": "ordinary chat with the AI",
    "unsure": "can't tell",
}


def rubric() -> dict:
    """The rubric as the judge UI renders it."""
    return {
        "version": RUBRIC_VERSION,
        # String keys: this is the wire format, and JSON has no integer keys.
        "scale": {
            "min": SCALE_MIN,
            "max": SCALE_MAX,
            "anchors": {str(k): v for k, v in SCALE_ANCHORS.items()},
        },
        "dimensions": list(DIMENSIONS),
        "condition_guess": [
            {"value": v, "label": CONDITION_GUESS_LABELS[v]} for v in CONDITION_GUESSES
        ],
    }


def validate_ratings(ratings: dict) -> dict[str, int]:
    """Strict: every dimension present, an integer on the scale, nothing
    extra. A partial or malformed submission is rejected whole, so every
    stored rating is complete. Returns the cleaned mapping; raises
    ValueError with a message fit to show the judge."""
    if not isinstance(ratings, dict):
        raise ValueError("ratings must be an object mapping each dimension to a score")
    extra = sorted(set(ratings) - set(DIMENSION_KEYS))
    if extra:
        raise ValueError(f"Unknown rating dimension(s): {', '.join(extra)}")
    missing = [k for k in DIMENSION_KEYS if k not in ratings]
    if missing:
        raise ValueError(f"Please rate every dimension — missing: {', '.join(missing)}")
    cleaned: dict[str, int] = {}
    for key in DIMENSION_KEYS:
        value = ratings[key]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"'{key}' must be a whole number from {SCALE_MIN} to {SCALE_MAX}")
        if not SCALE_MIN <= value <= SCALE_MAX:
            raise ValueError(f"'{key}' must be from {SCALE_MIN} to {SCALE_MAX}")
        cleaned[key] = value
    return cleaned
