"""Tests for the research capture log (`turn_log` + `state_events`).

The pilot's formal analysis happens offline, from captured data alone —
so what these tests pin down is that nothing slips past the log: every
LLM exchange (including failed ones), every state transition whatever
code path made it, the verbatim LLM output, and the parse path; and that
a rolled-back turn leaves no phantom rows behind.
"""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import duckdb
import pytest

from elenchus import turn_log
from elenchus.dialectical_state import DialecticalState
from elenchus.llm_client import ChatCategory, ChatResult
from elenchus.opponent import BASELINE_SYSTEM_PROMPT, LLMCallError, Opponent
from elenchus.response_parsing import parse_llm_response_with_strategy
from elenchus.turn_log import EventContext


def _opp(**kwargs) -> Opponent:
    return Opponent(api_key="fake-key", **kwargs)


def _envelope(speech_acts=(), new_tensions=(), response="ok") -> str:
    return json.dumps(
        {
            "speech_acts": list(speech_acts),
            "new_tensions": list(new_tensions),
            "response": response,
        }
    )


def _success(text: str) -> ChatResult:
    return ChatResult(
        category=ChatCategory.SUCCESS,
        text=text,
        attempts=1,
        latency_ms=1234,
        prompt_tokens=900,
        completion_tokens=150,
        model="test-model",
    )


def _respond(opp, state, message, llm_text, **kwargs):
    """One opponent turn with the LLM stubbed at the client boundary, so
    the real `_async_chat` (and its on_result capture) still runs."""
    with patch.object(opp._llm_client, "achat", new=AsyncMock(return_value=_success(llm_text))):
        return asyncio.run(opp.async_respond(message, state, **kwargs))


def _events(state, **where):
    rows = turn_log.list_state_events(state.base.con)
    return [r for r in rows if all(r[k] == v for k, v in where.items())]


# ── Schema ───────────────────────────────────────────────────────────


class TestSchema:
    def test_capture_tables_exist(self):
        state = DialecticalState.in_memory("t")
        tables = {
            r[0]
            for r in state.base.con.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
            ).fetchall()
        }
        assert {"turn_log", "state_events"} <= tables

    def test_ids_continue_across_reopen(self, tmp_path):
        """The sequences are re-seeded from MAX(id) on open — a reopened
        (or restored) base must never hand out an id that exists."""
        path = str(tmp_path / "b.duckdb")
        state = DialecticalState.create(path, "t")
        state.commit("P")
        _respond(_opp(), state, "hi", _envelope([{"type": "COMMIT", "proposition": "Q"}]))
        state.base.con.close()

        state = DialecticalState.open(path)
        state.commit("R")
        _respond(_opp(), state, "again", _envelope())
        event_ids = [e["id"] for e in turn_log.list_state_events(state.base.con)]
        turn_ids = [t["id"] for t in turn_log.list_turns(state.base.con)]
        assert event_ids == sorted(set(event_ids)) and len(event_ids) == 3
        assert turn_ids == [1, 2]
        state.base.con.close()

    def test_legacy_base_gains_the_tables_on_open(self, tmp_path):
        """A base created before migration 3 is upgraded on open and
        starts logging from there."""
        from elenchus.migrations.runner import MIGRATIONS_ROOT

        path = str(tmp_path / "legacy.duckdb")
        con = duckdb.connect(path)
        for name in ("0001_initial.sql", "0002_phase_a.sql"):
            con.execute((MIGRATIONS_ROOT / "base" / name).read_text())
        con.execute("INSERT INTO meta VALUES ('schema_version', '2')")
        con.execute("INSERT INTO meta VALUES ('name', 'legacy')")
        con.close()

        state = DialecticalState.open(path)
        state.commit("P")
        assert [e["event_type"] for e in _events(state)] == ["COMMIT"]
        state.base.con.close()


# ── State events ─────────────────────────────────────────────────────


class TestStateEvents:
    def test_direct_calls_are_logged_as_direct(self):
        state = DialecticalState.in_memory("t")
        state.commit("P")
        state.deny("Q")
        rows = _events(state)
        assert [(r["event_type"], r["source"], r["outcome"]) for r in rows] == [
            ("COMMIT", "direct", "applied"),
            ("DENY", "direct", "applied"),
        ]
        assert rows[0]["payload"] == {"proposition": "P", "prior": []}
        assert rows[0]["turn_id"] is None
        assert rows[0]["at_utc"].endswith("+00:00")

    def test_prior_position_survives_the_upsert(self):
        """`positions` overwrites on re-commit; the event keeps what was
        there — here, that P was retracted before being re-committed."""
        state = DialecticalState.in_memory("t")
        state.commit("P")
        state.retract_prop("P")
        state.commit("P")
        last = _events(state, event_type="COMMIT")[-1]
        assert last["payload"]["prior"] == [{"side": "C", "status": "retracted"}]

    def test_retract_records_sides_and_noop(self):
        state = DialecticalState.in_memory("t")
        state.commit("P")
        assert state.retract_prop("P") is True
        assert state.retract_prop("P") is False
        applied, noop = _events(state, event_type="RETRACT")
        assert (applied["outcome"], applied["payload"]["sides"]) == ("applied", ["C"])
        assert (noop["outcome"], noop["note"]) == ("noop", "proposition not held")

    def test_tension_lifecycle(self):
        state = DialecticalState.in_memory("t")
        state.commit("P")
        t1 = state.add_tension(["P"], ["Q"], "because")
        t2 = state.add_tension(["P"], ["R"])
        state.accept_tension(t1)
        state.contest_tension(t2)
        assert state.accept_tension(99) is None
        assert state.contest_tension(t2) is False  # already resolved

        proposed = _events(state, event_type="PROPOSE_TENSION")
        assert proposed[0]["payload"] == {
            "tension_id": t1,
            "gamma": ["P"],
            "delta": ["Q"],
            "reason": "because",
        }
        accepted = _events(state, event_type="ACCEPT_TENSION")
        assert [(a["outcome"], a["payload"]["tension_id"]) for a in accepted] == [
            ("applied", t1),
            ("noop", 99),
        ]
        assert accepted[0]["payload"]["gamma"] == ["P"]
        contested = _events(state, event_type="CONTEST_TENSION")
        assert [c["outcome"] for c in contested] == ["applied", "noop"]

    def test_explicit_context_is_recorded(self):
        state = DialecticalState.in_memory("t")
        state.commit("P", event=EventContext(source="ui", actor_id=42))
        (row,) = _events(state)
        assert (row["source"], row["actor_id"]) == ("ui", 42)

    def test_phase_b_mutators_are_logged(self):
        state = DialecticalState.in_memory("t")
        iid = state.assert_implication(["A"], ["B"], reason="r")
        state.introduce_bearer("C", description="a concept")
        state.retract_implication(iid)
        state.retract_implication(iid)
        kinds = [(e["event_type"], e["outcome"]) for e in _events(state)]
        assert kinds == [
            ("ASSERT_IMPLICATION", "applied"),
            ("INTRODUCE_BEARER", "applied"),
            ("RETRACT_IMPLICATION", "applied"),
            ("RETRACT_IMPLICATION", "noop"),
        ]
        assert (
            _events(state, event_type="ASSERT_IMPLICATION")[0]["payload"]["implication_id"] == iid
        )


# ── Opponent turns ───────────────────────────────────────────────────


class TestOpponentTurn:
    def test_turn_row_captures_request_response_and_timing(self):
        opp = _opp()
        state = DialecticalState.in_memory("Occurrence")
        state.commit("An Occurrence records an Organism")
        raw = _envelope(
            [{"type": "DENY", "proposition": "An Organism is its identifier"}],
            [{"gamma": ["An Occurrence records an Organism"], "delta": ["X"], "reason": "r"}],
            response="Noted.",
        )
        _respond(opp, state, "I deny that.", raw, actor_id=7, action_context={"action": "none"})

        (turn,) = turn_log.list_turns(state.base.con)
        assert (turn["mode"], turn["outcome"], turn["actor_id"]) == ("elenchus", "ok", 7)
        assert turn["user_message"] == "I deny that."
        assert turn["raw_text"] == raw  # verbatim, not the cleaned prose
        assert turn["parse_strategy"] == "direct"
        assert turn["parsed"]["response"] == "Noted."
        assert turn["action_context"] == {"action": "none"}
        assert 'RESPONDENT SAYS: "I deny that."' in turn["request_content"]
        assert (turn["history_window"], turn["summary_included"]) == (0, False)
        # State as the LLM saw it vs. as the turn left it.
        assert turn["state_before"]["denials"] == []
        assert turn["state_after"]["denials"] == ["An Organism is its identifier"]
        assert len(turn["state_after"]["tensions"]) == 1
        # From the ChatResult.
        assert (turn["model"], turn["latency_ms"], turn["attempts"]) == ("test-model", 1234, 1)
        assert (turn["prompt_tokens"], turn["completion_tokens"]) == (900, 150)
        assert turn["error_category"] is None
        # Prompt identity.
        assert turn["system_prompt_name"] == "sloan"
        assert turn["system_prompt_sha256"] == turn_log.prompt_fingerprint(opp._system_prompt())
        # Links into the transcript.
        rows = state.base.con.execute("SELECT id, role FROM conversation ORDER BY id").fetchall()
        assert rows == [
            (turn["user_conversation_id"], "user"),
            (turn["assistant_conversation_id"], "assistant"),
        ]

    def test_events_point_back_at_their_turn(self):
        state = DialecticalState.in_memory("t")
        state.commit("before any turn")
        _respond(
            _opp(),
            state,
            "msg",
            _envelope(
                [{"type": "COMMIT", "proposition": "P"}],
                [{"gamma": ["P"], "delta": ["Q"], "reason": "r"}],
            ),
            actor_id=7,
        )
        (turn,) = turn_log.list_turns(state.base.con)
        from_turn = _events(state, turn_id=turn["id"])
        assert [(e["event_type"], e["source"], e["actor_id"]) for e in from_turn] == [
            ("COMMIT", "opponent", 7),
            ("PROPOSE_TENSION", "opponent", 7),
        ]
        assert _events(state, source="direct")[0]["turn_id"] is None

    def test_refine_links_its_two_halves(self):
        state = DialecticalState.in_memory("t")
        state.commit("Old wording")
        _respond(
            _opp(),
            state,
            "let me rephrase",
            _envelope(
                [
                    {
                        "type": "REFINE",
                        "old_proposition": "Old wording",
                        "proposition": "New wording",
                    }
                ]
            ),
        )
        kinds = [e["event_type"] for e in _events(state, source="opponent")]
        assert kinds == ["REFINE", "RETRACT", "COMMIT"]
        refine = _events(state, event_type="REFINE")[0]
        assert refine["payload"] == {
            "old_proposition": "Old wording",
            "proposition": "New wording",
        }

    def test_dropped_speech_acts_are_accounted_for(self):
        """Phase B is firewalled off by default; an act the firewall
        drops — or one too malformed to apply — must still show up."""
        state = DialecticalState.in_memory("t")
        _respond(
            _opp(),
            state,
            "msg",
            _envelope(
                [
                    {"type": "ASSERT_IMPLICATION", "gamma": ["A"], "delta": ["B"]},
                    {"type": "COMMIT"},
                    {"type": "ACCEPT_TENSION"},
                    {"type": "MYSTERY", "proposition": "P"},
                ],
                [{"gamma": [], "delta": []}],
            ),
        )
        dropped = _events(state, outcome="dropped")
        assert [(d["event_type"], d["note"]) for d in dropped] == [
            ("ASSERT_IMPLICATION", "Phase B firewall (ELENCHUS_ENABLE_PHASE_B is off)"),
            ("COMMIT", "unknown type or missing proposition"),
            ("ACCEPT_TENSION", "no target_tension_id"),
            ("MYSTERY", "unknown type or missing proposition"),
            ("PROPOSE_TENSION", "empty gamma and delta"),
        ]
        assert state.I == [] and state.C == []

    def test_phase_b_prompt_identity(self):
        opp = _opp(enable_phase_b=True)
        state = DialecticalState.in_memory("t")
        _respond(opp, state, "msg", _envelope())
        (turn,) = turn_log.list_turns(state.base.con)
        assert turn["system_prompt_name"] == "phase_b"

    def test_rolled_back_turn_leaves_no_log(self):
        opp = _opp()
        state = DialecticalState.in_memory("t")
        original_apply = opp._apply

        def _boom(parsed, st, **kwargs):
            original_apply(parsed, st, **kwargs)
            raise RuntimeError("simulated post-apply failure")

        with (
            patch.object(
                opp._llm_client,
                "achat",
                new=AsyncMock(
                    return_value=_success(_envelope([{"type": "COMMIT", "proposition": "P"}]))
                ),
            ),
            patch.object(opp, "_apply", side_effect=_boom),
            pytest.raises(RuntimeError),
        ):
            asyncio.run(opp.async_respond("msg", state))

        assert turn_log.list_turns(state.base.con) == []
        assert turn_log.list_state_events(state.base.con) == []
        assert state.C == []

    def test_failed_llm_call_is_logged_and_nothing_else_is(self):
        opp = _opp()
        state = DialecticalState.in_memory("t")
        fail = ChatResult(
            category=ChatCategory.RATE_LIMIT,
            attempts=3,
            latency_ms=9000,
            model="test-model",
            error_message="429 slow down",
        )
        with (
            patch.object(opp._llm_client, "achat", new=AsyncMock(return_value=fail)),
            pytest.raises(LLMCallError),
        ):
            asyncio.run(opp.async_respond("are you there?", state, actor_id=7))

        (turn,) = turn_log.list_turns(state.base.con)
        assert (turn["outcome"], turn["user_message"]) == ("llm_error", "are you there?")
        assert turn["error_category"] == ChatCategory.RATE_LIMIT.value
        assert (turn["error_message"], turn["attempts"]) == ("429 slow down", 3)
        assert turn["raw_text"] is None and turn["state_before"] is not None
        assert state.get_conversation() == []

    def test_sync_respond_is_logged_too(self):
        """The CLI and scripts/run_dialectic.py use the sync path."""
        opp = _opp()
        state = DialecticalState.in_memory("t")
        text = _envelope([{"type": "COMMIT", "proposition": "P"}])
        with patch.object(opp._llm_client, "chat", return_value=_success(text)):
            opp.respond("msg", state)
        (turn,) = turn_log.list_turns(state.base.con)
        assert (turn["outcome"], turn["model"]) == ("ok", "test-model")
        assert [e["event_type"] for e in _events(state, turn_id=turn["id"])] == ["COMMIT"]

    def test_record_and_apply_without_request_metadata(self):
        """Older call shape (no `turn=`) still produces a row."""
        state = DialecticalState.in_memory("t")
        _opp()._record_and_apply("msg", _envelope(), state)
        (turn,) = turn_log.list_turns(state.base.con)
        assert turn["outcome"] == "ok" and turn["model"] is None


class TestBaselineTurn:
    def test_baseline_exchange_is_logged(self):
        opp = _opp()
        state = DialecticalState.in_memory("t")
        with patch.object(
            opp._llm_client, "achat", new=AsyncMock(return_value=_success("Sure — a draft:"))
        ):
            asyncio.run(opp.async_baseline_respond("draft me a definition", state, actor_id=9))

        (turn,) = turn_log.list_turns(state.base.con)
        assert (turn["mode"], turn["outcome"], turn["actor_id"]) == ("baseline", "ok", 9)
        assert turn["raw_text"] == "Sure — a draft:"
        assert turn["parse_strategy"] == "plain"
        assert turn["state_before"] is None and turn["parsed"] is None
        assert turn["system_prompt_name"] == "baseline"
        assert turn["system_prompt_sha256"] == turn_log.prompt_fingerprint(BASELINE_SYSTEM_PROMPT)
        assert turn["user_conversation_id"] is not None
        assert turn_log.list_state_events(state.base.con) == []

    def test_baseline_failure_is_logged(self):
        opp = _opp()
        state = DialecticalState.in_memory("t")
        fail = ChatResult(category=ChatCategory.TIMEOUT, attempts=2, error_message="timed out")
        with (
            patch.object(opp._llm_client, "achat", new=AsyncMock(return_value=fail)),
            pytest.raises(LLMCallError),
        ):
            asyncio.run(opp.async_baseline_respond("hello?", state))
        (turn,) = turn_log.list_turns(state.base.con)
        assert (turn["mode"], turn["outcome"]) == ("baseline", "llm_error")
        assert turn["error_category"] == ChatCategory.TIMEOUT.value


# ── Parse strategies ─────────────────────────────────────────────────


class TestParseStrategy:
    @pytest.mark.parametrize(
        ("text", "strategy"),
        [
            ('{"speech_acts":[],"new_tensions":[],"response":"ok"}', "direct"),
            ('```json\n{"response":"ok"}\n```', "direct"),
            ('Here you go:\n{"speech_acts":[],"response":"ok"} hope that helps', "brace_walk"),
            ('{"speech_acts":[],"response":"she said "no" to that"}', "json_repair"),
            ("just prose, no JSON at all", "unparseable"),
            ("", "unparseable"),
        ],
    )
    def test_strategy_names(self, text, strategy):
        assert parse_llm_response_with_strategy(text)[1].startswith(strategy)

    def test_plain_text_fallback_is_recorded(self):
        state = DialecticalState.in_memory("t")
        result = _respond(_opp(), state, "msg", "I forgot the JSON entirely.")
        assert result["response"] == "I forgot the JSON entirely."
        (turn,) = turn_log.list_turns(state.base.con)
        assert turn["parse_strategy"] == "plain_text_fallback"
        assert turn["raw_text"] == "I forgot the JSON entirely."

    def test_non_object_json_is_treated_as_prose(self):
        """A bare JSON string/list isn't the envelope; it used to crash
        the turn on `.get`."""
        state = DialecticalState.in_memory("t")
        result = _respond(_opp(), state, "msg", '["not", "an", "envelope"]')
        assert result["speech_acts"] == []
        (turn,) = turn_log.list_turns(state.base.con)
        assert turn["parse_strategy"] == "plain_text_fallback"
