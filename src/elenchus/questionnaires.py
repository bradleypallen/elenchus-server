"""
questionnaires.py — post-session instrument definitions + validation.

The Sloan study administers four instruments after each session:

  * `nasa_tlx` — NASA Task Load Index (Hart & Staveland 1988).
    Six workload dimensions, each rated 0–100 in steps of 5.
  * `sus` — System Usability Scale (Brooke 1996). Ten items, 1–5
    Likert, alternating positive/negative polarity (scoring happens
    at analysis time, not here — we store raw responses).
  * `tias` — Trust in Automated Systems (Jian, Bisantz & Drury 2000).
    Twelve items, 1–7 Likert; items 1–5 are distrust-worded.
  * `eeq` — custom Epistemic Experience Questionnaire. Eight items,
    1–7 Likert, probing ownership, articulation, and challenge —
    the constructs the AI-as-collaborator manipulation targets.

Definitions live in code (not the DB) so they're versioned with the
release; `INSTRUMENT_VERSION` is stamped onto every stored response
so the per-study export can reproduce exactly what each participant
saw even if items are revised between cohorts.

`validate_responses(instrument, responses)` enforces completeness
(every item answered, no extras) and range. It returns a list of
error strings — empty means valid. Study data quality depends on
this being strict; partial submissions are rejected whole.
"""

from __future__ import annotations

# Bump when any item text or scale changes. Stamped on stored rows.
INSTRUMENT_VERSION = "1"


def _items(scale_min: int, scale_max: int, *texts: str) -> list[dict]:
    return [
        {
            "id": f"q{i + 1}",
            "text": text,
            "scale_min": scale_min,
            "scale_max": scale_max,
        }
        for i, text in enumerate(texts)
    ]


INSTRUMENTS: dict[str, dict] = {
    "nasa_tlx": {
        "title": "NASA Task Load Index",
        "description": (
            "Rate each dimension of your experience during the session on the scale shown."
        ),
        "scale_labels": {"min": "Very low", "max": "Very high"},
        # TLX uses 0–100 in steps of 5; we accept any integer in range
        # and leave step enforcement to the frontend slider.
        "items": [
            {
                "id": "mental_demand",
                "text": "How mentally demanding was the task?",
                "scale_min": 0,
                "scale_max": 100,
            },
            {
                "id": "physical_demand",
                "text": "How physically demanding was the task?",
                "scale_min": 0,
                "scale_max": 100,
            },
            {
                "id": "temporal_demand",
                "text": "How hurried or rushed was the pace of the task?",
                "scale_min": 0,
                "scale_max": 100,
            },
            {
                "id": "performance",
                "text": ("How successful were you in accomplishing what you were asked to do?"),
                "scale_min": 0,
                "scale_max": 100,
            },
            {
                "id": "effort",
                "text": ("How hard did you have to work to accomplish your level of performance?"),
                "scale_min": 0,
                "scale_max": 100,
            },
            {
                "id": "frustration",
                "text": ("How insecure, discouraged, irritated, stressed, and annoyed were you?"),
                "scale_min": 0,
                "scale_max": 100,
            },
        ],
    },
    "sus": {
        "title": "System Usability Scale",
        "description": "Rate your agreement with each statement.",
        "scale_labels": {"min": "Strongly disagree", "max": "Strongly agree"},
        "items": _items(
            1,
            5,
            "I think that I would like to use this system frequently.",
            "I found the system unnecessarily complex.",
            "I thought the system was easy to use.",
            "I think that I would need the support of a technical person to be able to use this system.",
            "I found the various functions in this system were well integrated.",
            "I thought there was too much inconsistency in this system.",
            "I would imagine that most people would learn to use this system very quickly.",
            "I found the system very cumbersome to use.",
            "I felt very confident using the system.",
            "I needed to learn a lot of things before I could get going with this system.",
        ),
    },
    "tias": {
        "title": "Trust in Automated Systems",
        "description": ("Rate your agreement with each statement about the AI you worked with."),
        "scale_labels": {"min": "Not at all", "max": "Extremely"},
        "items": _items(
            1,
            7,
            "The system is deceptive.",
            "The system behaves in an underhanded manner.",
            "I am suspicious of the system's intent, action, or outputs.",
            "I am wary of the system.",
            "The system's actions will have a harmful or injurious outcome.",
            "I am confident in the system.",
            "The system provides security.",
            "The system has integrity.",
            "The system is dependable.",
            "The system is reliable.",
            "I can trust the system.",
            "I am familiar with the system.",
        ),
    },
    "eeq": {
        "title": "Epistemic Experience Questionnaire",
        "description": (
            "Rate your agreement with each statement about the knowledge work you just did."
        ),
        "scale_labels": {"min": "Strongly disagree", "max": "Strongly agree"},
        "items": _items(
            1,
            7,
            "The ideas in the final output feel like my own.",
            "The session helped me make explicit things I knew but had not put into words.",
            "I was pushed to consider aspects of the domain I would not have considered on my own.",
            "I changed my mind about something during the session.",
            "I can defend every claim in the final output.",
            "The AI's contributions shaped the substance of the output, not just its wording.",
            "The session surfaced inconsistencies in my own thinking.",
            "I understand my own views on this domain better than I did before the session.",
        ),
    },
}


def list_instruments() -> list[dict]:
    """Public shape for `GET /api/study/instruments` — definitions
    with version, so the frontend renders them dynamically and the
    export records what was shown."""
    return [
        {
            "instrument": name,
            "version": INSTRUMENT_VERSION,
            "title": spec["title"],
            "description": spec["description"],
            "scale_labels": spec["scale_labels"],
            "items": spec["items"],
        }
        for name, spec in INSTRUMENTS.items()
    ]


def validate_responses(instrument: str, responses: dict) -> list[str]:
    """Strict validation of a submission against the instrument
    definition. Returns a list of human-readable error strings;
    empty list means valid.

    Rules: the instrument must exist; every item must be answered;
    no extra keys; every value must be an integer within the item's
    scale. Booleans are rejected (bool is an int subclass in Python
    — a `true` from sloppy frontend code must not slip through as 1).
    """
    errors: list[str] = []
    spec = INSTRUMENTS.get(instrument)
    if spec is None:
        return [f"Unknown instrument: {instrument!r}"]
    if not isinstance(responses, dict):
        return ["responses must be an object mapping item id to value"]

    expected_ids = {item["id"] for item in spec["items"]}
    got_ids = set(responses.keys())

    for missing in sorted(expected_ids - got_ids):
        errors.append(f"Missing response for item {missing!r}")
    for extra in sorted(got_ids - expected_ids):
        errors.append(f"Unexpected item {extra!r}")

    by_id = {item["id"]: item for item in spec["items"]}
    for item_id in sorted(expected_ids & got_ids):
        value = responses[item_id]
        item = by_id[item_id]
        if isinstance(value, bool) or not isinstance(value, int):
            errors.append(f"Item {item_id!r} must be an integer, got {value!r}")
            continue
        if not (item["scale_min"] <= value <= item["scale_max"]):
            errors.append(
                f"Item {item_id!r} must be between {item['scale_min']} "
                f"and {item['scale_max']}, got {value}"
            )
    return errors
