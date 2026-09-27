"""Every inference call carries its identity — the model the provider
reports, its request id, the sampling parameters sent — from the client,
through the turn log and the usage table, into the export.

This is the study's model-stability protocol: a participant whose two
sessions ran on different model versions ("a straddled pair") can only be
found from what the provider *returned*, and the frozen parameters can
only be checked if they were written down per call.
"""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import MagicMock, patch

import pytest

from elenchus import auth, turn_log
from elenchus.db import get_registry
from elenchus.db import platform as pdb
from elenchus.dialectical_state import DialecticalState
from elenchus.llm_client import DEFAULT_TEMPERATURE, ChatCategory, ChatResult, LLMClient
from elenchus.opponent import TEMPERATURE_ENV, Opponent, _env_temperature
from elenchus.server import app  # noqa: F401 — importing the server initialises the registry


def _response(text="ok", *, model="claude-opus-4-8-20260301", rid="msg_01ABC"):
    block = MagicMock()
    block.text = text
    resp = MagicMock()
    resp.content = [block]
    resp.usage = MagicMock(input_tokens=10, output_tokens=5)
    resp.model = model
    resp.id = rid
    return resp


def _client(responses):
    client = MagicMock()
    client.messages.create.side_effect = responses
    return client


class TestClient:
    def test_the_identity_and_parameters_ride_on_the_result(self):
        client = _client([_response()])
        llm = LLMClient(protocol="anthropic", model="claude-opus-4-8", sync_client=client)
        result = llm.chat([{"role": "user", "content": "hi"}], max_tokens=1234)
        assert result.ok
        assert result.model == "claude-opus-4-8"  # what was asked for
        assert result.response_model == "claude-opus-4-8-20260301"  # what answered
        assert result.request_id == "msg_01ABC"
        assert result.temperature == DEFAULT_TEMPERATURE and result.max_tokens == 1234

    def test_the_temperature_is_actually_sent(self):
        client = _client([_response()])
        LLMClient(protocol="anthropic", model="m", sync_client=client, temperature=0.3).chat(
            [{"role": "user", "content": "hi"}]
        )
        assert client.messages.create.call_args.kwargs["temperature"] == 0.3

    def test_the_default_temperature_is_sent_too(self):
        """Explicitly, so it is frozen and recorded rather than implied."""
        client = _client([_response()])
        LLMClient(protocol="anthropic", model="m", sync_client=client).chat(
            [{"role": "user", "content": "hi"}]
        )
        assert client.messages.create.call_args.kwargs["temperature"] == DEFAULT_TEMPERATURE

    def test_openai_shape(self):
        choice = MagicMock()
        choice.message.content = "ok"
        resp = MagicMock()
        resp.choices = [choice]
        resp.usage = MagicMock(prompt_tokens=10, completion_tokens=5)
        resp.model = "gpt-x-2026"
        resp.id = "chatcmpl-9"
        client = MagicMock()
        client.chat.completions.create.side_effect = [resp]
        result = LLMClient(protocol="openai", model="gpt-x", sync_client=client).chat(
            [{"role": "user", "content": "hi"}]
        )
        assert (result.response_model, result.request_id) == ("gpt-x-2026", "chatcmpl-9")
        assert client.chat.completions.create.call_args.kwargs["temperature"] == 1.0

    def test_a_response_without_identity_is_recorded_as_unknown(self):
        resp = _response()
        del resp.model  # MagicMock would otherwise answer with a mock
        resp.id = 12345  # not a string
        client = _client([resp])
        result = LLMClient(protocol="anthropic", model="m", sync_client=client).chat(
            [{"role": "user", "content": "hi"}]
        )
        assert result.ok and result.response_model == "" and result.request_id == ""

    def test_a_failed_call_still_records_what_was_sent(self):
        client = _client([RuntimeError("boom")])
        llm = LLMClient(protocol="anthropic", model="m", sync_client=client, max_attempts=1)
        result = llm.chat([{"role": "user", "content": "hi"}], max_tokens=99)
        assert not result.ok
        assert result.temperature == DEFAULT_TEMPERATURE and result.max_tokens == 99
        assert result.response_model == "" and result.request_id == ""

    def test_the_async_path_matches(self):
        async def create(**kwargs):
            return _response()

        client = MagicMock()
        client.messages.create = create
        llm = LLMClient(protocol="anthropic", model="m", async_client=client)
        result = asyncio.run(llm.achat([{"role": "user", "content": "hi"}]))
        assert result.response_model == "claude-opus-4-8-20260301"
        assert result.request_id == "msg_01ABC" and result.temperature == DEFAULT_TEMPERATURE


class TestEnvironment:
    def test_default_and_override(self, monkeypatch):
        monkeypatch.delenv(TEMPERATURE_ENV, raising=False)
        assert _env_temperature() == DEFAULT_TEMPERATURE
        monkeypatch.setenv(TEMPERATURE_ENV, "0.2")
        assert _env_temperature() == 0.2
        assert Opponent(api_key=None).temperature == 0.2
        assert Opponent(api_key=None, temperature=0.7).temperature == 0.7

    @pytest.mark.parametrize("bad", ["", "warm", "-1", "2.5", "nan"])
    def test_nonsense_means_the_default(self, monkeypatch, caplog, bad):
        monkeypatch.setenv(TEMPERATURE_ENV, bad)
        assert _env_temperature() == DEFAULT_TEMPERATURE


class TestTurnLog:
    def test_the_columns_are_written(self):
        state = DialecticalState.in_memory("t")
        result = ChatResult(
            category=ChatCategory.SUCCESS,
            text="{}",
            attempts=1,
            latency_ms=3,
            prompt_tokens=1,
            completion_tokens=1,
            model="claude-opus-4-8",
            response_model="claude-opus-4-8-20260301",
            request_id="msg_9",
            temperature=0.5,
            max_tokens=2000,
        )
        turn_log.record_turn(
            state.base.con, mode="elenchus", user_message="hi", chat_result=result
        )
        (row,) = turn_log.list_turns(state.base.con)
        assert row["model"] == "claude-opus-4-8"
        assert row["response_model"] == "claude-opus-4-8-20260301"
        assert row["request_id"] == "msg_9"
        assert row["temperature"] == 0.5 and row["max_tokens"] == 2000
        state.base.con.close()

    def test_absent_identity_is_null_not_empty(self):
        state = DialecticalState.in_memory("t")
        turn_log.record_turn(
            state.base.con,
            mode="elenchus",
            user_message="hi",
            chat_result=ChatResult(category=ChatCategory.SUCCESS, text="{}", model="m"),
        )
        (row,) = turn_log.list_turns(state.base.con)
        assert row["response_model"] is None and row["request_id"] is None
        state.base.con.close()


@pytest.fixture
def _platform():
    reg = get_registry()
    reg.migrate_platform()
    con = reg.platform_con()
    with reg.platform_lock:
        for table in ("usage", "auth_sessions", "actors"):
            con.execute(f"DELETE FROM {table}")
    for _name, handle in list(reg._handles.items()):
        with contextlib.suppress(Exception):
            handle.state.base.con.close()
    reg._handles.clear()
    yield con


class TestUsage:
    def test_record_usage_stores_the_identity(self, _platform):
        con = _platform
        pdb.record_usage(
            con,
            actor_id=None,
            base_id=None,
            model="claude-opus-4-8",
            category="success",
            prompt_tokens=1,
            completion_tokens=1,
            cost_usd=0.0,
            attempts=1,
            latency_ms=1,
            response_model="claude-opus-4-8-20260301",
            request_id="msg_1",
        )
        assert con.execute("SELECT response_model, request_id FROM usage").fetchone() == (
            "claude-opus-4-8-20260301",
            "msg_1",
        )

    def test_the_recorder_passes_it_through(self, _platform):
        """A real turn: the client's result reaches the usage row."""
        con = _platform
        actor_id = pdb.create_actor(
            con,
            kind="user",
            email="u@example.com",
            display_name="u",
            password_hash=auth.hash_password("pw"),
        )
        state = DialecticalState.in_memory("t")
        opp = Opponent(api_key=None, model="claude-opus-4-8")
        reply = ChatResult(
            category=ChatCategory.SUCCESS,
            text='{"speech_acts":[],"new_tensions":[],"response":"ok"}',
            attempts=1,
            latency_ms=5,
            prompt_tokens=10,
            completion_tokens=5,
            model="claude-opus-4-8",
            response_model="claude-opus-4-8-20260301",
            request_id="msg_2",
        )
        with patch.object(opp._llm_client, "chat", return_value=reply):
            opp.respond("hi", state, actor_id=actor_id, base_id="b")
        assert con.execute("SELECT response_model, request_id FROM usage").fetchone() == (
            "claude-opus-4-8-20260301",
            "msg_2",
        )
        state.base.con.close()
