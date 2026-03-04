"""Tests for dialectical_state.py — bilateral positions, tensions, implications."""

import logging

from dialectical_state import DialecticalState

logger = logging.getLogger(__name__)


class TestPositions:
    def test_empty_state(self):
        state = DialecticalState.in_memory("test")
        assert state.C == []
        assert state.D == []

    def test_commit(self):
        state = DialecticalState.in_memory("test")
        state.commit("Dogs are good")
        assert "Dogs are good" in state.C
        assert "Dogs are good" not in state.D

    def test_deny(self):
        state = DialecticalState.in_memory("test")
        state.deny("Cats are bad")
        assert "Cats are bad" in state.D
        assert "Cats are bad" not in state.C

    def test_commit_then_deny_adds_to_both_sides(self):
        """PK is (atom, side), so commit+deny creates rows on both sides."""
        state = DialecticalState.in_memory("test")
        state.commit("P")
        assert "P" in state.C
        state.deny("P")
        assert "P" in state.D
        # Both rows exist — the opponent coordinates side switching
        assert "P" in state.C

    def test_retract(self):
        state = DialecticalState.in_memory("test")
        state.commit("P")
        assert state.retract_prop("P") is True
        assert "P" not in state.C
        assert "P" in state.retracted

    def test_retract_nonexistent(self):
        state = DialecticalState.in_memory("test")
        assert state.retract_prop("nonexistent") is False


class TestTensions:
    def test_add_tension(self):
        state = DialecticalState.in_memory("test")
        state.commit("P")
        state.commit("Q")
        tid = state.add_tension(["P"], ["Q"], reason="test reason")
        assert tid >= 1
        tensions = state.T
        assert len(tensions) == 1
        assert tensions[0]["reason"] == "test reason"

    def test_accept_tension_creates_implication(self):
        state = DialecticalState.in_memory("test")
        state.commit("P")
        state.deny("Q")
        tid = state.add_tension(["P"], ["Q"], reason="conflict")
        result = state.accept_tension(tid)
        assert result is not None
        assert "P" in result["gamma"]
        # Tension is gone from open, implication created
        assert len(state.T) == 0
        assert len(state.I) == 1

    def test_contest_tension(self):
        state = DialecticalState.in_memory("test")
        state.commit("P")
        tid = state.add_tension(["P"], ["Q"], reason="test")
        assert state.contest_tension(tid) is True
        assert len(state.T) == 0
        contested = state.contested_tensions
        assert len(contested) == 1

    def test_accept_nonexistent_tension(self):
        state = DialecticalState.in_memory("test")
        assert state.accept_tension(999) is None

    def test_contest_nonexistent_tension(self):
        state = DialecticalState.in_memory("test")
        assert state.contest_tension(999) is False


class TestDerivability:
    def test_derives_via_containment(self):
        state = DialecticalState.in_memory("test")
        assert state.derives(["P"], ["P"]) is True

    def test_derives_via_accepted_tension(self):
        state = DialecticalState.in_memory("test")
        state.commit("P")
        tid = state.add_tension(["P"], ["Q"], reason="test")
        state.accept_tension(tid)
        assert state.derives(["P"], ["Q"]) is True


class TestConversation:
    def test_conversation_empty(self):
        state = DialecticalState.in_memory("test")
        assert state.get_conversation() == []

    def test_add_and_get_conversation(self):
        state = DialecticalState.in_memory("test")
        state.add_conversation("user", "Hello")
        state.add_conversation("assistant", "Hi there")
        conv = state.get_conversation()
        assert len(conv) == 2
        assert conv[0]["role"] == "user"
        assert conv[1]["content"] == "Hi there"


class TestSummary:
    def test_summary_initially_empty(self):
        state = DialecticalState.in_memory("test")
        assert state.get_summary() == ""

    def test_set_and_get_summary(self):
        state = DialecticalState.in_memory("test")
        state.set_summary("This is a test summary")
        assert state.get_summary() == "This is a test summary"

    def test_update_summary(self):
        state = DialecticalState.in_memory("test")
        state.set_summary("First")
        state.set_summary("Second")
        assert state.get_summary() == "Second"


class TestToDict:
    def test_to_dict_structure(self):
        state = DialecticalState.in_memory("test")
        state.commit("P")
        state.deny("Q")
        d = state.to_dict()
        assert d["name"] == "test"
        assert "commitments" in d
        assert "denials" in d
        assert "tensions" in d
        assert "implications" in d
        assert "retracted" in d
        assert "contested" in d
        assert "P" in d["commitments"]
        assert "Q" in d["denials"]
