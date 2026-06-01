"""Tests for Phase B protocol extensions.

Three new speech acts route theory articulation directly into the
material base, bypassing the tension loop:

  * ASSERT_IMPLICATION  — directly add a rule {γ} |~ {δ}
  * INTRODUCE_BEARER    — add an atom to L_B without committing/denying
  * RETRACT_IMPLICATION — withdraw a rule by id

Tests cover the DialecticalState API, the Opponent dispatch, the
provenance JSON shape, and the interactions with derivability /
existing-tension flows.
"""

import json

import pytest

from elenchus.dialectical_state import DialecticalState


@pytest.fixture
def state():
    """A fresh in-memory dialectic for each test."""
    s = DialecticalState.in_memory("phase-b")
    yield s
    s.base.con.close()


# ─── DialecticalState API ─────────────────────────────────────────────


class TestAssertImplication:
    def test_adds_atoms_and_assessment(self, state):
        iid = state.assert_implication(
            ["X is a dog"], ["X is a mammal"], reason="biological taxonomy"
        )
        assert iid > 0

        # Both atoms exist in L_B.
        atoms = state.base.atoms
        assert "X is a dog" in atoms
        assert "X is a mammal" in atoms

        # The rule shows up in I with domain='asserted'.
        impls = state.I
        assert len(impls) == 1
        assert impls[0]["gamma"] == ["X is a dog"]
        assert impls[0]["delta"] == ["X is a mammal"]
        assert impls[0]["domain"] == "asserted"
        assert impls[0]["id"] == iid

    def test_provenance_is_recorded(self, state):
        state.assert_implication(["a"], ["b"], reason="because")
        row = state.base.con.execute(
            "SELECT provenance FROM assessments WHERE domain='asserted'"
        ).fetchone()
        prov = json.loads(row[0])
        assert prov["source"] == "asserted"
        assert prov["reason"] == "because"

    def test_caller_can_layer_extra_provenance(self, state):
        state.assert_implication(["x"], ["y"], reason="r", provenance={"turn": 7, "session_id": 3})
        prov = json.loads(
            state.base.con.execute(
                "SELECT provenance FROM assessments WHERE domain='asserted'"
            ).fetchone()[0]
        )
        # `source` is forced; caller-supplied fields ride along.
        assert prov["source"] == "asserted"
        assert prov["turn"] == 7
        assert prov["session_id"] == 3

    def test_asserted_rule_affects_derivability(self, state):
        state.assert_implication(["X is alive"], ["X is an animal"])
        # Containment gives `{X is alive} |~ {X is alive}` trivially, but
        # the new rule should let derivability conclude {X is an animal}
        # from {X is alive}.
        assert state.derives(["X is alive"], ["X is an animal"])

    def test_empty_lists_still_returns_a_row(self, state):
        # Edge case: an assertion with no premises is still allowed
        # (axiom-like). Just confirm no exception.
        iid = state.assert_implication([], ["something"])
        assert iid > 0


class TestIntroduceBearer:
    def test_adds_atom_without_position(self, state):
        state.introduce_bearer("X is a mutable entity")
        assert "X is a mutable entity" in state.base.atoms
        # Position is untouched — not in C, not in D.
        assert "X is a mutable entity" not in state.C
        assert "X is a mutable entity" not in state.D

    def test_idempotent(self, state):
        state.introduce_bearer("A")
        state.introduce_bearer("A")
        # add_atoms is INSERT OR IGNORE, so the second call is a no-op.
        count = state.base.con.execute(
            "SELECT COUNT(*) FROM atoms WHERE sentence=?", ["A"]
        ).fetchone()[0]
        assert count == 1

    def test_empty_proposition_is_ignored(self, state):
        state.introduce_bearer("")
        # No exception, no atom added.
        assert state.base.atoms == frozenset()

    def test_description_stored(self, state):
        state.introduce_bearer("X is provable", description="from formal logic")
        row = state.base.con.execute(
            "SELECT description FROM atoms WHERE sentence=?", ["X is provable"]
        ).fetchone()
        assert row[0] == "from formal logic"


class TestRetractImplication:
    def test_retracts_an_asserted_rule(self, state):
        iid = state.assert_implication(["p"], ["q"])
        assert state.retract_implication(iid) is True

        # Rule no longer shows in I.
        assert state.I == []
        # Underlying row exists but is status='retracted'.
        row = state.base.con.execute("SELECT status FROM assessments WHERE id=?", [iid]).fetchone()
        assert row[0] == "retracted"

    def test_retracted_rule_no_longer_derives(self, state):
        state.assert_implication(["A"], ["B"])
        assert state.derives(["A"], ["B"]) is True
        # Find the id and retract.
        iid = state.I[0]["id"]
        state.retract_implication(iid)
        assert state.derives(["A"], ["B"]) is False

    def test_unknown_id_returns_false(self, state):
        assert state.retract_implication(99999) is False

    def test_double_retract_is_idempotent_returning_false(self, state):
        iid = state.assert_implication(["x"], ["y"])
        assert state.retract_implication(iid) is True
        assert state.retract_implication(iid) is False

    def test_can_retract_tension_earned_rule_too(self, state):
        """ACCEPT_TENSION-earned rules live in the same `assessments`
        table; they're retractable by the same id."""
        state.commit("A")
        state.commit("B")
        tid = state.add_tension(["A", "B"], ["C"], "test")
        accepted = state.accept_tension(tid)
        assert accepted is not None

        iid = state.I[0]["id"]
        assert state.retract_implication(iid) is True
        assert state.I == []


# ─── Opponent dispatch ────────────────────────────────────────────────


def _opp():
    """Build an Opponent with a no-op LLM (we only exercise `_apply`)."""
    from elenchus.opponent import Opponent

    return Opponent(api_key=None, model="test")


class TestOpponentApply:
    def test_assert_implication_routes_to_state(self, state):
        opp = _opp()
        opp._apply(
            {
                "speech_acts": [
                    {
                        "type": "ASSERT_IMPLICATION",
                        "gamma": ["X is alive"],
                        "delta": ["X is an animal"],
                        "reason": "by definition",
                    }
                ]
            },
            state,
        )
        assert len(state.I) == 1
        assert state.I[0]["domain"] == "asserted"
        assert state.I[0]["gamma"] == ["X is alive"]
        assert state.I[0]["delta"] == ["X is an animal"]

    def test_introduce_bearer_routes_to_state(self, state):
        opp = _opp()
        opp._apply(
            {
                "speech_acts": [
                    {
                        "type": "INTRODUCE_BEARER",
                        "proposition": "X is provable",
                        "description": "from formal logic",
                    }
                ]
            },
            state,
        )
        assert "X is provable" in state.base.atoms
        # No position change.
        assert "X is provable" not in state.C
        assert "X is provable" not in state.D

    def test_retract_implication_routes_to_state(self, state):
        opp = _opp()
        iid = state.assert_implication(["a"], ["b"])
        opp._apply(
            {"speech_acts": [{"type": "RETRACT_IMPLICATION", "implication_id": iid}]},
            state,
        )
        assert state.I == []

    def test_retract_implication_accepts_string_id(self, state):
        """LLMs sometimes serialize integer ids as strings."""
        opp = _opp()
        iid = state.assert_implication(["a"], ["b"])
        opp._apply(
            {"speech_acts": [{"type": "RETRACT_IMPLICATION", "implication_id": str(iid)}]},
            state,
        )
        assert state.I == []

    def test_assert_with_empty_gamma_and_delta_is_skipped(self, state, caplog):
        opp = _opp()
        with caplog.at_level("WARNING", logger="elenchus.opponent"):
            opp._apply(
                {"speech_acts": [{"type": "ASSERT_IMPLICATION", "gamma": [], "delta": []}]},
                state,
            )
        assert state.I == []
        assert any("empty γ and δ" in rec.message for rec in caplog.records)

    def test_introduce_bearer_without_proposition_skipped(self, state, caplog):
        opp = _opp()
        with caplog.at_level("WARNING", logger="elenchus.opponent"):
            opp._apply(
                {"speech_acts": [{"type": "INTRODUCE_BEARER", "proposition": ""}]},
                state,
            )
        assert state.base.atoms == frozenset()
        assert any("no proposition" in rec.message for rec in caplog.records)

    def test_retract_implication_unknown_id_logs_and_continues(self, state, caplog):
        opp = _opp()
        with caplog.at_level("INFO", logger="elenchus.opponent"):
            opp._apply(
                {
                    "speech_acts": [
                        {"type": "RETRACT_IMPLICATION", "implication_id": 99999},
                        {
                            "type": "ASSERT_IMPLICATION",
                            "gamma": ["x"],
                            "delta": ["y"],
                        },
                    ]
                },
                state,
            )
        # Second act still applied — one bad act doesn't abort the rest.
        assert len(state.I) == 1
        assert any("Skipped RETRACT_IMPLICATION" in rec.message for rec in caplog.records)


# ─── End-to-end: ontology positum ─────────────────────────────────────


class TestOntologyArticulation:
    def test_small_ontology_flow(self, state):
        """An admin issuing 5 bearer introductions + 3 implications then
        a retraction should leave L_B at 5 atoms and |~_B with 2
        active sequents."""
        opp = _opp()
        opp._apply(
            {
                "speech_acts": [
                    {"type": "INTRODUCE_BEARER", "proposition": "X is an animal"},
                    {"type": "INTRODUCE_BEARER", "proposition": "X is a mammal"},
                    {"type": "INTRODUCE_BEARER", "proposition": "X is a dog"},
                    {"type": "INTRODUCE_BEARER", "proposition": "X is alive"},
                    {"type": "INTRODUCE_BEARER", "proposition": "X has fur"},
                    {
                        "type": "ASSERT_IMPLICATION",
                        "gamma": ["X is a dog"],
                        "delta": ["X is a mammal"],
                    },
                    {
                        "type": "ASSERT_IMPLICATION",
                        "gamma": ["X is a mammal"],
                        "delta": ["X is an animal"],
                    },
                    {
                        "type": "ASSERT_IMPLICATION",
                        "gamma": ["X is a mammal"],
                        "delta": ["X has fur"],
                    },
                ]
            },
            state,
        )
        assert len(state.base.atoms) == 5
        assert len(state.I) == 3

        # Bilateral position is untouched — this is pure theory.
        assert state.C == []
        assert state.D == []

        # Retract the "X has fur" rule (mammals don't all have fur — whales).
        fur_rule = next(i for i in state.I if i["delta"] == ["X has fur"])
        opp._apply(
            {"speech_acts": [{"type": "RETRACT_IMPLICATION", "implication_id": fur_rule["id"]}]},
            state,
        )
        assert len(state.I) == 2
        assert all(i["delta"] != ["X has fur"] for i in state.I)
