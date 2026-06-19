"""Tests for the shared LLM-response parser.

The opponent, the PDF report, and the frontend history loader all need
to extract the natural-language `response` field from a stored payload
that may have a prose preamble, a code fence, or both. This locks in
the behavior the original Phase A 0.2.0 demo regressed on (raw JSON
leaking into the PDF transcript).
"""

import pytest

from elenchus.response_parsing import extract_response_text, parse_llm_response


class TestParseLlmResponse:
    def test_plain_json(self):
        text = '{"response": "hi", "speech_acts": []}'
        assert parse_llm_response(text) == {"response": "hi", "speech_acts": []}

    def test_code_fenced_json(self):
        text = '```json\n{"response": "hello"}\n```'
        assert parse_llm_response(text) == {"response": "hello"}

    def test_code_fenced_no_language_tag(self):
        text = '```\n{"response": "hello"}\n```'
        assert parse_llm_response(text) == {"response": "hello"}

    def test_prose_preamble_then_json(self):
        """The case that bit the PDF: 'Here's my analysis:' before JSON."""
        text = 'Here is my analysis:\n{"response": "hello", "speech_acts": []}'
        assert parse_llm_response(text) == {"response": "hello", "speech_acts": []}

    def test_json_then_trailing_chatter(self):
        text = '{"response": "hi"}\n\nLet me know if you need more.'
        assert parse_llm_response(text) == {"response": "hi"}

    def test_braces_inside_strings_dont_throw_off_walker(self):
        # The closing `}` inside the string must not terminate the walk.
        text = 'Preamble. {"response": "weird } char", "speech_acts": []}'
        assert parse_llm_response(text) == {"response": "weird } char", "speech_acts": []}

    def test_no_json_returns_none(self):
        assert parse_llm_response("just prose, no braces here") is None

    def test_unbalanced_json_returns_none(self):
        assert parse_llm_response("{ not actually json") is None

    def test_empty_returns_none(self):
        assert parse_llm_response("") is None
        assert parse_llm_response("   \n  ") is None

    def test_literal_newlines_in_strings(self):
        # Models emit real line breaks in multi-paragraph responses; strict
        # JSON rejects them. We tolerate them (strict=False) so the prose
        # parses instead of the whole envelope leaking into the transcript.
        text = '{"speech_acts": [], "new_tensions": [], "response": "Para one.\n\nPara two."}'
        parsed = parse_llm_response(text)
        assert parsed is not None
        assert parsed["response"] == "Para one.\n\nPara two."

    def test_literal_newlines_with_preamble(self):
        text = 'Here you go:\n{"response": "Line A.\nLine B.", "speech_acts": []}'
        parsed = parse_llm_response(text)
        assert parsed is not None
        assert parsed["response"] == "Line A.\nLine B."


class TestExtractResponseText:
    def test_returns_response_field(self):
        assert extract_response_text('{"response": "hi"}') == "hi"

    def test_extracts_from_preamble_case(self):
        """The exact shape that leaked into the PDF."""
        text = 'Sure, here is my analysis:\n{"response": "Tell me more.", "speech_acts": [{"type":"COMMIT","proposition":"Sky is blue."}]}'
        assert extract_response_text(text) == "Tell me more."

    def test_falls_back_to_raw_when_no_json(self):
        assert extract_response_text("plain text") == "plain text"

    def test_falls_back_when_response_missing(self):
        # JSON parses but has no `response` field.
        text = '{"speech_acts": []}'
        assert extract_response_text(text) == text

    def test_falls_back_when_response_empty(self):
        text = '{"response": "   "}'
        assert extract_response_text(text) == text

    def test_falls_back_when_response_not_string(self):
        text = '{"response": null, "speech_acts": []}'
        assert extract_response_text(text) == text


class TestOpponentParserStillBehaves:
    """Smoke test that the opponent's _parse_response — now a thin
    wrapper around the shared parser — still produces the dict shape
    callers expect, including the plain-text fallback."""

    def test_unparseable_falls_back_to_plain_text_payload(self):
        from elenchus.opponent import Opponent

        opp = Opponent(api_key=None, model="test")
        result = opp._parse_response("nothing useful here")
        assert result == {"speech_acts": [], "new_tensions": [], "response": "nothing useful here"}

    def test_preamble_json_recovered_by_opponent_too(self):
        from elenchus.opponent import Opponent

        opp = Opponent(api_key=None, model="test")
        result = opp._parse_response(
            'Sure thing: {"speech_acts": [], "new_tensions": [], "response": "ok"}'
        )
        assert result["response"] == "ok"

    @pytest.mark.parametrize(
        "text",
        [
            '{"speech_acts": [], "new_tensions": [], "response": "first"}',
            '```json\n{"speech_acts": [], "new_tensions": [], "response": "fenced"}\n```',
        ],
    )
    def test_happy_paths_unchanged(self, text):
        from elenchus.opponent import Opponent

        opp = Opponent(api_key=None, model="test")
        result = opp._parse_response(text)
        assert result["response"] in ("first", "fenced")
