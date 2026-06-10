"""Tests for opponent.py — protocol detection, response parsing, state application, formatting."""

import asyncio
import logging
from unittest.mock import AsyncMock, patch

from elenchus.dialectical_state import DialecticalState
from elenchus.opponent import Opponent

logger = logging.getLogger(__name__)


# ── Protocol detection ──


class TestDetectProtocol:
    def test_default_is_anthropic(self):
        assert Opponent._detect_protocol(None) == "anthropic"

    def test_openrouter_detected(self):
        assert Opponent._detect_protocol("https://openrouter.ai/api/v1") == "openai"

    def test_openai_detected(self):
        assert Opponent._detect_protocol("https://api.openai.com/v1") == "openai"

    def test_together_detected(self):
        assert Opponent._detect_protocol("https://api.together.xyz/v1") == "openai"

    def test_groq_detected(self):
        assert Opponent._detect_protocol("https://api.groq.com/openai/v1") == "openai"

    def test_unknown_url_defaults_anthropic(self):
        assert Opponent._detect_protocol("https://my-custom-llm.example.com/v1") == "anthropic"

    def test_empty_string_defaults_anthropic(self):
        assert Opponent._detect_protocol("") == "anthropic"


# ── Response parsing ──


class TestParseResponse:
    def setup_method(self):
        self.opp = Opponent.__new__(Opponent)

    def test_valid_json(self):
        raw = '{"speech_acts": [{"type": "COMMIT", "proposition": "P"}], "new_tensions": [], "response": "Ok."}'
        result = self.opp._parse_response(raw)
        assert result["response"] == "Ok."
        assert len(result["speech_acts"]) == 1
        assert result["speech_acts"][0]["type"] == "COMMIT"

    def test_markdown_wrapped_json(self):
        raw = '```json\n{"speech_acts": [], "new_tensions": [], "response": "Hello."}\n```'
        result = self.opp._parse_response(raw)
        assert result["response"] == "Hello."
        assert result["speech_acts"] == []

    def test_conversational_fallback(self):
        raw = "I think you should reconsider your position."
        result = self.opp._parse_response(raw)
        assert result["response"] == raw
        assert result["speech_acts"] == []
        assert result["new_tensions"] == []

    def test_json_with_leading_whitespace(self):
        raw = '  \n  {"speech_acts": [], "new_tensions": [], "response": "Trimmed."}'
        result = self.opp._parse_response(raw)
        assert result["response"] == "Trimmed."

    def test_empty_string_fallback(self):
        result = self.opp._parse_response("")
        assert result["response"] == ""
        assert result["speech_acts"] == []

    def test_prose_preamble_then_json(self):
        """LLM occasionally prefixes JSON with a sentence — extract anyway."""
        raw = (
            "Here is my analysis.\n"
            '{"speech_acts": [{"type": "COMMIT", "proposition": "p"}],'
            ' "new_tensions": [], "response": "Hi."}'
        )
        result = self.opp._parse_response(raw)
        assert result["response"] == "Hi."
        assert len(result["speech_acts"]) == 1
        assert result["speech_acts"][0]["proposition"] == "p"

    def test_json_with_trailing_chatter(self):
        raw = (
            '{"speech_acts": [], "new_tensions": [], "response": "Done."}'
            "\n\nLet me know if you need anything else!"
        )
        result = self.opp._parse_response(raw)
        assert result["response"] == "Done."

    def test_brace_in_string_does_not_break_extraction(self):
        """Strings containing '{' or '}' must not confuse the brace walker."""
        raw = (
            "Preamble.\n"
            '{"speech_acts": [], "new_tensions": [],'
            ' "response": "Consider the set {x : x > 0}."}'
        )
        result = self.opp._parse_response(raw)
        assert result["response"] == "Consider the set {x : x > 0}."

    def test_fenced_with_language_tag(self):
        raw = '```json\n{"speech_acts": [], "new_tensions": [], "response": "Y."}\n```'
        result = self.opp._parse_response(raw)
        assert result["response"] == "Y."


# ── State application (_apply) ──


class TestApply:
    def setup_method(self):
        self.opp = Opponent.__new__(Opponent)
        self.state = DialecticalState.in_memory("test")

    def test_apply_commit(self):
        parsed = {"speech_acts": [{"type": "COMMIT", "proposition": "Dogs are loyal"}]}
        self.opp._apply(parsed, self.state)
        assert "Dogs are loyal" in self.state.C

    def test_apply_deny(self):
        parsed = {"speech_acts": [{"type": "DENY", "proposition": "Cats are lazy"}]}
        self.opp._apply(parsed, self.state)
        assert "Cats are lazy" in self.state.D

    def test_apply_retract(self):
        self.state.commit("P")
        parsed = {"speech_acts": [{"type": "RETRACT", "proposition": "P"}]}
        self.opp._apply(parsed, self.state)
        assert "P" not in self.state.C
        assert "P" in self.state.retracted

    def test_apply_refine(self):
        self.state.commit("Old claim")
        parsed = {
            "speech_acts": [
                {"type": "REFINE", "old_proposition": "Old claim", "proposition": "New claim"}
            ]
        }
        self.opp._apply(parsed, self.state)
        assert "Old claim" not in self.state.C
        assert "New claim" in self.state.C

    def test_apply_accept_tension(self):
        self.state.commit("P")
        self.state.deny("Q")
        tid = self.state.add_tension(["P"], ["Q"], reason="test")
        parsed = {"speech_acts": [{"type": "ACCEPT_TENSION", "target_tension_id": tid}]}
        self.opp._apply(parsed, self.state)
        assert len(self.state.T) == 0
        assert len(self.state.I) == 1

    def test_apply_contest_tension(self):
        self.state.commit("P")
        tid = self.state.add_tension(["P"], ["Q"], reason="test")
        parsed = {"speech_acts": [{"type": "CONTEST_TENSION", "target_tension_id": tid}]}
        self.opp._apply(parsed, self.state)
        assert len(self.state.T) == 0
        assert len(self.state.contested_tensions) == 1

    def test_apply_new_tensions(self):
        self.state.commit("P")
        parsed = {
            "speech_acts": [],
            "new_tensions": [
                {"gamma": ["P"], "delta": ["Q"], "reason": "P entails Q"},
            ],
        }
        self.opp._apply(parsed, self.state)
        assert len(self.state.T) == 1
        assert self.state.T[0]["reason"] == "P entails Q"

    def test_apply_multiple_acts(self):
        parsed = {
            "speech_acts": [
                {"type": "COMMIT", "proposition": "A"},
                {"type": "COMMIT", "proposition": "B"},
                {"type": "DENY", "proposition": "C"},
            ],
            "new_tensions": [],
        }
        self.opp._apply(parsed, self.state)
        assert "A" in self.state.C
        assert "B" in self.state.C
        assert "C" in self.state.D

    def test_apply_skips_missing_tension(self):
        """Accepting a non-existent tension should not raise."""
        parsed = {"speech_acts": [{"type": "ACCEPT_TENSION", "target_tension_id": 999}]}
        self.opp._apply(parsed, self.state)  # should not raise

    def test_apply_empty_proposition_ignored(self):
        parsed = {"speech_acts": [{"type": "COMMIT", "proposition": ""}]}
        self.opp._apply(parsed, self.state)
        assert self.state.C == []


# ── Formatting helpers ──


class TestFormatting:
    def setup_method(self):
        self.opp = Opponent.__new__(Opponent)

    def test_fmt_list_empty(self):
        assert self.opp._fmt_list([]) == " (none)"

    def test_fmt_list_with_items(self):
        result = self.opp._fmt_list(["alpha", "beta"])
        assert '"alpha"' in result
        assert '"beta"' in result

    def test_fmt_list_with_atom_ids(self):
        result = self.opp._fmt_list(["alpha", "beta"], atom_ids={"alpha": 1, "beta": 2})
        assert "P1" in result
        assert "P2" in result

    def test_fmt_tensions_empty(self):
        assert self.opp._fmt_tensions([]) == " (none)"

    def test_fmt_tensions_with_items(self):
        tensions = [{"id": 1, "gamma": ["P"], "delta": ["Q"], "reason": "test"}]
        result = self.opp._fmt_tensions(tensions)
        assert "T1" in result
        assert '"P"' in result
        assert '"Q"' in result

    def test_fmt_implications_empty(self):
        assert self.opp._fmt_implications([]) == " (none)"

    def test_fmt_implications_with_items(self):
        imps = [{"id": 3, "gamma": ["A"], "delta": ["B"]}]
        result = self.opp._fmt_implications(imps)
        assert "I3" in result
        assert '"A"' in result

    def test_fmt_tensions_multi_element_sets(self):
        tensions = [{"id": 2, "gamma": ["P", "Q"], "delta": ["R", "S"], "reason": "complex"}]
        result = self.opp._fmt_tensions(tensions)
        assert "T2" in result
        assert '"P"' in result
        assert '"Q"' in result
        assert '"R"' in result
        assert '"S"' in result


# ── Reconfigure ──


class TestReconfigure:
    def test_reconfigure_model(self):
        opp = Opponent(api_key="fake-key")
        opp.reconfigure(model="gpt-4o")
        assert opp.model == "gpt-4o"

    def test_reconfigure_protocol_explicit(self):
        opp = Opponent(api_key="fake-key")
        assert opp.protocol == "anthropic"
        opp.reconfigure(protocol="openai")
        assert opp.protocol == "openai"

    def test_reconfigure_base_url_auto_detects_protocol(self):
        opp = Opponent(api_key="fake-key")
        opp.reconfigure(base_url="https://openrouter.ai/api/v1")
        assert opp.protocol == "openai"

    def test_reconfigure_clear_base_url(self):
        opp = Opponent(api_key="fake-key", base_url="https://openrouter.ai/api/v1")
        assert opp.protocol == "openai"
        opp.reconfigure(base_url="")
        assert opp.base_url is None
        assert opp.protocol == "anthropic"


# ── Async respond path ──


class TestAsyncRespond:
    """Verify the async respond path runs end-to-end with a mocked LLM
    and that it produces the same state mutations as the sync path."""

    def _make_opponent(self):
        opp = Opponent(api_key="fake-key")
        return opp

    def test_async_respond_applies_speech_acts(self):
        opp = self._make_opponent()
        state = DialecticalState.in_memory("test")

        fake_json = (
            '{"speech_acts":[{"type":"COMMIT","proposition":"P"}],'
            '"new_tensions":[],"response":"Noted."}'
        )

        with patch.object(opp, "_async_chat", new=AsyncMock(return_value=fake_json)):
            result = asyncio.run(opp.async_respond("I assert P", state))

        assert result["response"] == "Noted."
        assert "P" in state.C

    def test_async_respond_extracts_new_tensions(self):
        opp = self._make_opponent()
        state = DialecticalState.in_memory("test")
        state.commit("P")

        fake_json = (
            '{"speech_acts":[],"new_tensions":['
            '{"gamma":["P"],"delta":["Q"],"reason":"P entails Q"}'
            '],"response":"Notice this."}'
        )

        with patch.object(opp, "_async_chat", new=AsyncMock(return_value=fake_json)):
            result = asyncio.run(opp.async_respond("Continue", state))

        assert result["response"] == "Notice this."
        assert len(state.T) == 1

    def test_async_respond_records_conversation(self):
        opp = self._make_opponent()
        state = DialecticalState.in_memory("test")

        with patch.object(
            opp,
            "_async_chat",
            new=AsyncMock(return_value='{"speech_acts":[],"new_tensions":[],"response":"OK."}'),
        ):
            asyncio.run(opp.async_respond("Hello", state))

        history = state.get_conversation()
        assert len(history) == 2
        assert history[0]["role"] == "user"
        assert history[0]["content"] == "Hello"
        assert history[1]["role"] == "assistant"

    def test_async_respond_handles_prose_preamble(self):
        """The robust _parse_response should recover JSON from mixed
        content via the async path too."""
        opp = self._make_opponent()
        state = DialecticalState.in_memory("test")

        fake_response = (
            "Here is my analysis.\n"
            '{"speech_acts":[{"type":"COMMIT","proposition":"X"}],'
            '"new_tensions":[],"response":"Recovered."}'
        )

        with patch.object(opp, "_async_chat", new=AsyncMock(return_value=fake_response)):
            result = asyncio.run(opp.async_respond("Hi", state))

        assert result["response"] == "Recovered."
        assert "X" in state.C


# ── LLM client integration ────────────────────────────────────────────


class TestOpponentLLMClientIntegration:
    """Opponent's chat shims are now thin wrappers over LLMClient.
    These tests prove the structured ChatResult flows through
    correctly, and that classified failures surface as LLMCallError
    (carrying the result for the caller's error path)."""

    def _make_opponent(self):
        opp = Opponent(api_key=None, model="test")
        return opp

    def test_chat_returns_text_on_success(self):
        from elenchus.llm_client import ChatCategory, ChatResult

        opp = self._make_opponent()
        success = ChatResult(
            category=ChatCategory.SUCCESS,
            text="ok",
            attempts=1,
            latency_ms=12,
            prompt_tokens=10,
            completion_tokens=2,
            model="test",
        )
        with patch.object(opp._llm_client, "chat", return_value=success):
            assert opp._chat([{"role": "user", "content": "hi"}]) == "ok"

    def test_chat_raises_llmcallerror_on_failure(self):
        from elenchus.llm_client import ChatCategory, ChatResult
        from elenchus.opponent import LLMCallError

        opp = self._make_opponent()
        fail = ChatResult(
            category=ChatCategory.RATE_LIMIT,
            text="",
            attempts=3,
            latency_ms=7000,
            model="test",
            error_message="429",
            exception_type="RateLimitError",
        )
        with patch.object(opp._llm_client, "chat", return_value=fail):
            try:
                opp._chat([{"role": "user", "content": "hi"}])
            except LLMCallError as e:
                assert e.result.category == ChatCategory.RATE_LIMIT
                assert e.result.attempts == 3
                assert "rate_limit" in str(e)
            else:
                raise AssertionError("expected LLMCallError")

    def test_async_chat_raises_llmcallerror_on_failure(self):
        from elenchus.llm_client import ChatCategory, ChatResult
        from elenchus.opponent import LLMCallError

        opp = self._make_opponent()
        fail = ChatResult(
            category=ChatCategory.AUTH_FAILURE,
            text="",
            attempts=1,
            model="test",
            error_message="invalid key",
            exception_type="AuthenticationError",
        )
        with patch.object(opp._llm_client, "achat", new=AsyncMock(return_value=fail)):
            try:
                asyncio.run(opp._async_chat([{"role": "user", "content": "hi"}]))
            except LLMCallError as e:
                assert e.result.category == ChatCategory.AUTH_FAILURE
            else:
                raise AssertionError("expected LLMCallError")

    def test_reconfigure_rebuilds_llm_client(self):
        """Changing the model has to flow through to the LLMClient so
        retries hit the right model name."""
        opp = self._make_opponent()
        original = opp._llm_client
        opp.reconfigure(model="claude-sonnet-5-0")
        assert opp._llm_client is not original
        assert opp._llm_client.model == "claude-sonnet-5-0"
