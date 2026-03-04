"""Tests for material_base.py — set serialization and derivability."""

import logging

from material_base import MaterialBase, fmt_set, set_to_str, str_to_set

logger = logging.getLogger(__name__)


# ── set_to_str / str_to_set round-trip ──


class TestSetSerialization:
    def test_empty_set(self):
        assert set_to_str(frozenset()) == ""
        assert str_to_set("") == frozenset()

    def test_single_element(self):
        s = frozenset({"alpha"})
        serialized = set_to_str(s)
        assert str_to_set(serialized) == s

    def test_multiple_elements_roundtrip(self):
        s = frozenset({"alpha", "beta", "gamma"})
        assert str_to_set(set_to_str(s)) == s

    def test_propositions_with_commas(self):
        """Commas in propositions must not split (new \\x1e delimiter)."""
        s = frozenset({"If it rains, the ground is wet", "Grass grows"})
        serialized = set_to_str(s)
        assert str_to_set(serialized) == s

    def test_legacy_comma_format(self):
        """Old data used comma delimiters — str_to_set must still read them."""
        legacy = "alpha,beta,gamma"
        result = str_to_set(legacy)
        assert result == frozenset({"alpha", "beta", "gamma"})

    def test_deterministic_ordering(self):
        """set_to_str must produce sorted output for stable DB storage."""
        s = frozenset({"z", "a", "m"})
        serialized = set_to_str(s)
        parts = [p for p in serialized.split("\x1e") if p]
        assert parts == sorted(parts)


class TestFmtSet:
    def test_empty(self):
        assert fmt_set(frozenset()) == "\u2205"
        assert fmt_set(set()) == "\u2205"

    def test_single(self):
        assert fmt_set({"alpha"}) == "{alpha}"

    def test_sorted_output(self):
        result = fmt_set({"gamma", "alpha", "beta"})
        assert result == "{alpha, beta, gamma}"


# ── MaterialBase in-memory ──


class TestMaterialBaseCreation:
    def test_in_memory_creation(self):
        base = MaterialBase.in_memory("test")
        assert base.name == "test"
        assert base.atoms == frozenset()

    def test_add_atoms(self):
        base = MaterialBase.in_memory("test")
        base.add_atoms({"p", "q", "r"})
        assert base.atoms == frozenset({"p", "q", "r"})

    def test_add_duplicate_atoms_no_error(self):
        base = MaterialBase.in_memory("test")
        base.add_atoms({"p"})
        base.add_atoms({"p"})  # should not raise
        assert base.atoms == frozenset({"p"})


# ── Derivability ──


class TestDerivability:
    def test_containment(self):
        """Containment: if premises and conclusions overlap, it derives."""
        base = MaterialBase.in_memory("test")
        base.add_atoms({"p", "q"})
        assert base.derives({"p"}, {"p"}) is True
        assert base.derives({"p", "q"}, {"q", "r"}) is True

    def test_no_derivation_empty_base(self):
        """With no assessments, non-overlapping sets don't derive."""
        base = MaterialBase.in_memory("test")
        base.add_atoms({"p", "q"})
        assert base.derives({"p"}, {"q"}) is False

    def test_direct_sequent(self):
        """A sequent in the base derives directly."""
        base = MaterialBase.in_memory("test")
        base.add_atoms({"p", "q"})
        base.accept({"p"}, {"q"}, "tester", reason="test")
        assert base.derives({"p"}, {"q"}) is True

    def test_projection_superset_premises(self):
        """Projection: {p} |~ {q} in base implies {p, r} |~ {q}."""
        base = MaterialBase.in_memory("test")
        base.add_atoms({"p", "q", "r"})
        base.accept({"p"}, {"q"}, "tester")
        assert base.derives({"p", "r"}, {"q"}) is True

    def test_projection_superset_conclusions(self):
        """Projection: {p} |~ {q} in base implies {p} |~ {q, r}."""
        base = MaterialBase.in_memory("test")
        base.add_atoms({"p", "q", "r"})
        base.accept({"p"}, {"q"}, "tester")
        assert base.derives({"p"}, {"q", "r"}) is True

    def test_rejected_sequent_does_not_derive(self):
        """A rejected assessment should not appear in base_sequents."""
        base = MaterialBase.in_memory("test")
        base.add_atoms({"p", "q"})
        base.reject({"p"}, {"q"}, "tester")
        assert base.derives({"p"}, {"q"}) is False

    def test_accept_then_reject_does_not_derive(self):
        """Most recent assessment wins (current_assessments view)."""
        base = MaterialBase.in_memory("test")
        base.add_atoms({"p", "q"})
        base.accept({"p"}, {"q"}, "tester")
        base.reject({"p"}, {"q"}, "tester")
        assert base.derives({"p"}, {"q"}) is False


class TestCompleteness:
    def test_empty_base(self):
        base = MaterialBase.in_memory("test")
        r = base.completeness()
        assert r["assessed"] == 0

    def test_with_assessments(self):
        base = MaterialBase.in_memory("test")
        base.add_atoms({"p", "q"})
        base.accept({"p"}, {"q"}, "tester")
        r = base.completeness()
        assert r["assessed"] >= 1
        assert 0 <= r["pct"] <= 1


class TestReport:
    def test_report_runs(self):
        base = MaterialBase.in_memory("test")
        base.add_atoms({"p", "q"})
        base.accept({"p"}, {"q"}, "tester")
        report = base.report()
        assert "test" in report
        assert "L_B" in report


class TestMigrateDelimiter:
    def test_migrate_comma_to_delim(self):
        base = MaterialBase.in_memory("test")
        base.add_atoms({"p", "q"})
        # Insert legacy comma-format directly
        base.con.execute(
            "INSERT INTO assessments (premises, conclusions, judgment, "
            "contributor) VALUES ('p,q', 'q', 'holds', 'tester')"
        )
        migrated = base._migrate_delimiter()
        assert migrated == 1
        # Verify it's now in new format
        row = base.con.execute("SELECT premises FROM assessments").fetchone()
        assert "\x1e" in row[0]
