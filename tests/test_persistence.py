"""Tests for persistence — create, close, reopen, verify state survives."""

import logging
import os
import tempfile

from elenchus.dialectical_state import DialecticalState

logger = logging.getLogger(__name__)


class TestPersistenceRoundTrip:
    def test_create_close_reopen(self):
        """Full round-trip: create state, add data, close, reopen, verify."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.duckdb")

            # Create and populate
            state = DialecticalState.create(db_path, "persistence test")
            state.commit("Alpha")
            state.commit("Beta")
            state.deny("Gamma")
            tid = state.add_tension(["Alpha"], ["Gamma"], reason="test tension")
            state.accept_tension(tid)
            state.add_conversation("user", "Hello")
            state.add_conversation("assistant", "Hi there")
            state.set_summary("Test summary")

            # Close connection
            state.base.con.close()

            # Reopen
            state2 = DialecticalState.open(db_path)
            assert state2.base.name == "persistence test"
            assert "Alpha" in state2.C
            assert "Beta" in state2.C
            assert "Gamma" in state2.D
            assert len(state2.T) == 0  # tension was accepted
            assert len(state2.I) == 1
            assert state2.I[0]["gamma"] == ["Alpha"] or "Alpha" in state2.I[0]["gamma"]
            conv = state2.get_conversation()
            assert len(conv) == 2
            assert conv[0]["content"] == "Hello"
            assert state2.get_summary() == "Test summary"

            # Derivability works after reopen
            assert state2.derives(["Alpha"], ["Gamma"]) is True
            assert state2.derives(["Alpha"], ["Beta"]) is False

            state2.base.con.close()

    def test_tension_ids_survive_reconnect(self):
        """Tension IDs should continue incrementing after reconnect."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test_ids.duckdb")

            state = DialecticalState.create(db_path, "id test")
            state.commit("P")
            tid1 = state.add_tension(["P"], ["Q"], reason="first")
            state.base.con.close()

            state2 = DialecticalState.open(db_path)
            state2.commit("R")
            tid2 = state2.add_tension(["R"], ["S"], reason="second")
            assert tid2 > tid1
            state2.base.con.close()

    def test_conversation_ids_survive_reconnect(self):
        """Conversation IDs should continue incrementing after reconnect."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test_conv.duckdb")

            state = DialecticalState.create(db_path, "conv test")
            state.add_conversation("user", "First")
            state.add_conversation("assistant", "Second")
            state.base.con.close()

            state2 = DialecticalState.open(db_path)
            state2.add_conversation("user", "Third")
            conv = state2.get_conversation()
            assert len(conv) == 3
            assert conv[2]["content"] == "Third"
            state2.base.con.close()

    def test_retracted_state_persists(self):
        """Retracted propositions should survive reconnect."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test_retract.duckdb")

            state = DialecticalState.create(db_path, "retract test")
            state.commit("P")
            state.retract_prop("P")
            state.base.con.close()

            state2 = DialecticalState.open(db_path)
            assert "P" not in state2.C
            assert "P" in state2.retracted
            state2.base.con.close()
