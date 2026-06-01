"""
response_parsing.py — robust extraction of structured payloads from
LLM responses.

The opponent's protocol asks the LLM for JSON of the form

    {"speech_acts": [...], "new_tensions": [...], "response": "..."}

but in practice the LLM occasionally emits:

  * plain JSON                              ← happy path
  * JSON inside a ```json ... ``` fence      ← also common
  * "Here's my analysis:\\n{...}"             ← prose preamble + JSON
  * "{...}\\nLet me know if you need more."   ← JSON + trailing chatter

This module owns the recovery logic so multiple sites (the opponent
itself, the PDF report, the frontend-loaded conversation history)
agree on what counts as a parseable response. The PDF report used to
have a weaker parser that only handled code fences, so prose-preamble
turns showed up as raw JSON in the report — see issue surfaced during
the Phase A 0.2.0 demo.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)


def parse_llm_response(text: str) -> dict | None:
    """Try to parse `text` as the opponent's expected JSON payload.

    Returns a dict on success, or None if no candidate JSON object can
    be recovered. Callers decide how to react to None (the opponent
    treats it as a plain-text response; the PDF / frontend just show
    the raw text).

    Strategies, in order:
      1. Direct `json.loads` after stripping a leading code fence.
      2. Locate the first `{` and walk braces (string-aware) to find
         the matching `}`; parse that slice.
    """
    if not text:
        return None

    clean = text.strip()

    # Strategy 0: drop a leading code fence (```json or plain ```).
    if clean.startswith("```"):
        clean = clean.split("\n", 1)[1] if "\n" in clean else clean[3:]
        if clean.endswith("```"):
            clean = clean[:-3]
        clean = clean.strip()

    # Strategy 1: direct parse.
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        pass

    # Strategy 2: locate the first `{` and walk braces (string-aware).
    start = clean.find("{")
    if start < 0:
        return None

    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(clean)):
        ch = clean[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = clean[start : i + 1]
                try:
                    parsed = json.loads(candidate)
                    preamble = clean[:start].strip()
                    logger.warning(
                        "Recovered JSON from mixed-content response "
                        "(preamble=%d chars, candidate=%d chars)",
                        len(preamble),
                        len(candidate),
                    )
                    return parsed
                except json.JSONDecodeError:
                    return None
    return None


def extract_response_text(content: str) -> str:
    """Return the natural-language `response` field from a stored
    opponent turn, falling back to the raw content if it doesn't
    look like a structured response.

    Used by the PDF report and any other historical-read surface that
    wants to render only the prose the opponent actually said,
    suppressing the JSON envelope that includes speech_acts / tensions.
    """
    parsed = parse_llm_response(content)
    if parsed is None:
        return content
    response = parsed.get("response")
    if isinstance(response, str) and response.strip():
        return response
    return content
