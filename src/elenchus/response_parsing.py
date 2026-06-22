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
import re

logger = logging.getLogger(__name__)


def _salvage_response_field(text: str) -> str | None:
    """Best-effort extraction of the `response` prose when the structured
    parse dropped it.

    This handles the case where json-repair recovered `new_tensions` from a
    malformed envelope but lost the trailing `response` string (the
    malformation sits *before* `response`, so the repair desyncs and returns
    an empty response). `response` is the protocol's last field, so we take
    everything from the key to the closing `"}`. Returns None if there is no
    `response` key or it's empty.
    """
    m = re.search(r'"response"\s*:\s*"', text)
    if not m:
        return None
    rest = text[m.end() :]
    end = re.search(r'"\s*\}\s*$', rest)  # prefer the proper "} terminator
    if end:
        body = rest[: end.start()]
    else:
        cut = rest.rfind('"')
        body = rest[:cut] if cut > 0 else rest
    # Decode the escapes the model actually emits; leave other chars
    # (em-dashes, accented letters) untouched.
    body = (
        body.replace('\\"', '"').replace("\\n", "\n").replace("\\t", "\t").replace("\\r", "\r")
    ).strip()
    return body or None


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

    # Strategy 1: direct parse. strict=False allows literal control
    # characters (raw newlines/tabs) inside strings — models routinely emit
    # real line breaks in a multi-paragraph "response" instead of "\n",
    # which strict JSON rejects and which was the main cause of raw JSON
    # leaking into the transcript.
    try:
        return json.loads(clean, strict=False)
    except json.JSONDecodeError:
        pass

    # Strategy 2: locate the first `{` and walk braces (string-aware), then
    # parse that slice. On a parse error, fall through to Strategy 3.
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
                    parsed = json.loads(candidate, strict=False)
                    if clean[:start].strip():
                        logger.warning("Recovered JSON from a mixed-content response")
                    return parsed
                except json.JSONDecodeError:
                    break  # malformed slice — try repair below

    # Strategy 3: repair LLM-malformed JSON the strict parser can't handle —
    # most often an unescaped double-quote inside a long natural-language
    # string value (which ends the string early), or a missing comma. This
    # is the common cause of opponent turns dropping their `new_tensions`.
    # json-repair returns a best-effort object; accept it only if it has the
    # opponent's expected shape so prose isn't passed through as "parsed".
    try:
        import json_repair

        repaired = json_repair.loads(clean[start:])
        if isinstance(repaired, dict) and any(
            k in repaired for k in ("response", "new_tensions", "speech_acts")
        ):
            # json-repair tends to drop the trailing `response` when the
            # malformation is inside an earlier field (e.g. new_tensions).
            # Salvage the prose directly so the live turn still shows a reply.
            resp = repaired.get("response")
            if not (isinstance(resp, str) and resp.strip()):
                salvaged = _salvage_response_field(clean[start:])
                if salvaged:
                    repaired["response"] = salvaged
            logger.warning("Recovered opponent payload via json-repair (len=%d)", len(text))
            return repaired
    except Exception:
        logger.debug("json-repair recovery failed", exc_info=True)

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
