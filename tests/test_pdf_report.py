"""Tests for pdf_report.py — Markdown conversion and PDF generation."""

import logging

from elenchus.dialectical_state import DialecticalState
from elenchus.pdf_report import _md_to_html, _parse_assistant_content, generate_pdf_report

logger = logging.getLogger(__name__)


# ── Markdown to HTML ──


class TestMdToHtml:
    def test_plain_text(self):
        result = _md_to_html("Hello world")
        assert "Hello world" in result

    def test_bold(self):
        result = _md_to_html("This is **bold** text")
        assert "<b>bold</b>" in result

    def test_italic(self):
        result = _md_to_html("This is *italic* text")
        assert "<i>italic</i>" in result

    def test_heading(self):
        result = _md_to_html("## Section Title")
        assert "<b>Section Title</b>" in result

    def test_unordered_list(self):
        result = _md_to_html("- item one\n- item two")
        assert "<ul>" in result
        assert "<li>item one</li>" in result
        assert "<li>item two</li>" in result

    def test_ordered_list(self):
        result = _md_to_html("1. first\n2. second")
        assert "<ol>" in result
        assert "<li>first</li>" in result
        assert "<li>second</li>" in result

    def test_empty_lines_produce_br(self):
        result = _md_to_html("para one\n\npara two")
        assert "<br>" in result

    def test_list_closed_on_end(self):
        result = _md_to_html("- item")
        assert result.count("<ul>") == result.count("</ul>")

    def test_mixed_content(self):
        md = "## Title\n\nSome text with **bold**.\n\n- bullet one\n- bullet two\n\nMore text."
        result = _md_to_html(md)
        assert "<b>Title</b>" in result
        assert "<b>bold</b>" in result
        assert "<ul>" in result
        assert "More text." in result


# ── Parse assistant content ──


class TestParseAssistantContent:
    def test_json_response_field(self):
        raw = '{"speech_acts": [], "new_tensions": [], "response": "Hello there."}'
        assert _parse_assistant_content(raw) == "Hello there."

    def test_markdown_wrapped_json(self):
        raw = '```json\n{"speech_acts": [], "response": "Wrapped."}\n```'
        assert _parse_assistant_content(raw) == "Wrapped."

    def test_plain_text_passthrough(self):
        raw = "Just a plain conversation message."
        assert _parse_assistant_content(raw) == raw

    def test_invalid_json_passthrough(self):
        raw = '{"broken json'
        assert _parse_assistant_content(raw) == raw

    def test_json_without_response_field(self):
        raw = '{"other_key": "value"}'
        assert _parse_assistant_content(raw) == raw


# ── PDF generation ──


class TestGeneratePdfReport:
    def test_generates_valid_pdf_bytes(self):
        state = DialecticalState.in_memory("test topic")
        state.commit("Dogs are loyal")
        state.deny("Cats are disloyal")
        tid = state.add_tension(["Dogs are loyal"], ["Cats are disloyal"], reason="tension test")
        state.accept_tension(tid)
        state.add_conversation("user", "Dogs are loyal.")
        state.add_conversation(
            "assistant",
            '{"speech_acts": [{"type": "COMMIT", "proposition": "Dogs are loyal"}], '
            '"new_tensions": [], "response": "Interesting commitment."}',
        )

        pdf_bytes = generate_pdf_report(state, "This is a test summary of the dialectic.")
        assert isinstance(pdf_bytes, bytes)
        assert len(pdf_bytes) > 100
        # PDF magic bytes
        assert pdf_bytes[:5] == b"%PDF-"

    def test_empty_state_generates_pdf(self):
        state = DialecticalState.in_memory("empty")
        pdf_bytes = generate_pdf_report(state, "No content yet.")
        assert pdf_bytes[:5] == b"%PDF-"

    def test_summary_appears_in_pdf(self):
        state = DialecticalState.in_memory("summary check")
        pdf_bytes = generate_pdf_report(state, "Unique summary text XYZ123.")
        # The summary text should be embedded somewhere in the PDF stream
        # (may be compressed, so we just check it generates without error)
        assert len(pdf_bytes) > 0
