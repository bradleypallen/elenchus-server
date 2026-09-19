"""Tests for cli.py — the `/derive` REPL command."""

from elenchus.cli import _derive
from elenchus.dialectical_state import DialecticalState


def _whales():
    state = DialecticalState.in_memory("cli-test")
    state.commit("Whales are mammals")
    state.deny("Whales breathe water")
    state.accept_tension(state.add_tension(["Whales are mammals"], ["Whales breathe water"]))
    return state


class TestDeriveCommand:
    def test_natural_language_atoms(self, capsys):
        _derive("/derive Whales are mammals |~ Whales breathe water", _whales())
        out = capsys.readouterr().out
        assert "✓ {Whales are mammals} |~ {Whales breathe water}" in out
        assert "AXIOM: Whales are mammals => Whales breathe water" in out
        assert "<" not in out  # no pyNMMS quoting leaks into the trace

    def test_malformed_query_prints_error_instead_of_crashing(self, capsys):
        _derive("/derive Whales are mammals & |~ Whales breathe water", _whales())
        out = capsys.readouterr().out
        assert "✗ Malformed query sentence 'Whales are mammals &'" in out
