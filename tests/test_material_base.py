"""Tests for material_base.py — set serialization and derivability."""

import logging

import pytest
from pynmms import MaterialBase as NMMSBase

from elenchus.material_base import (
    MAX_QUERY_NESTING,
    MAX_QUERY_SENTENCE_CHARS,
    MaterialBase,
    QuerySyntaxError,
    fmt_set,
    quote_atom,
    set_to_str,
    str_to_set,
    to_nmms_sentence,
    unquote_atoms,
)

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

    def test_no_weakening_premises(self):
        """NMMS rejects Weakening: {p} |~ {q} does NOT imply {p, r} |~ {q}."""
        base = MaterialBase.in_memory("test")
        base.add_atoms({"p", "q", "r"})
        base.accept({"p"}, {"q"}, "tester")
        assert base.derives({"p", "r"}, {"q"}) is False

    def test_no_weakening_conclusions(self):
        """NMMS rejects Weakening: {p} |~ {q} does NOT imply {p} |~ {q, r}."""
        base = MaterialBase.in_memory("test")
        base.add_atoms({"p", "q", "r"})
        base.accept({"p"}, {"q"}, "tester")
        assert base.derives({"p"}, {"q", "r"}) is False

    def test_derive_with_trace(self):
        """derive_with_trace returns ProofResult with trace and depth."""
        base = MaterialBase.in_memory("test")
        base.add_atoms({"p", "q"})
        base.accept({"p"}, {"q"}, "tester")
        result = base.derive_with_trace({"p"}, {"q"})
        assert result.derivable is True
        assert len(result.trace) >= 1
        assert result.depth_reached >= 0

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


# ── pyNMMS boundary: natural-language atoms ──
#
# pyNMMS >= 0.6.2 rejects any atom that is not an identifier or quoted as
# `<...>`. Real dialectics use natural-language propositions, so these
# tests deliberately avoid identifier-style atoms ("p", "q"), which mask
# boundary bugs.

WHALES = "Whales are mammals"
AIR = "Whales breathe air"
LUNGS = "Whales have lungs"

# Propositions built to collide with pyNMMS syntax or the quoting scheme.
AWKWARD = [
    "PaO2/FiO2 < 300 mmHg",
    "x > 5",
    "<b>bold</b> claim",
    "50% of whales sing",
    "%3C is not an escape here",
    "R&D spending is up",
    "Either it rains | it pours",
    "~5 mg is a safe dose",
    "Heat -> expansion",
    "Whales (cetaceans) are mammals",
    "C(a)",
    "If it rains, the ground is wet",
    "Line one\nline two",
    "  padded  ",
]


class TestAtomQuoting:
    @pytest.mark.parametrize("prop", AWKWARD + [WHALES, "p", ""])
    def test_quote_roundtrips_and_pynmms_accepts_it(self, prop):
        quoted = quote_atom(prop)
        assert unquote_atoms(quoted) == prop
        nmms = NMMSBase()
        nmms.add_atom(quoted)  # must not raise, whatever the proposition
        assert quoted in nmms.language

    def test_quoting_is_injective(self):
        props = AWKWARD + [WHALES, "p", "<p>", "%3Cp%3E", "%253Cp%253E"]
        assert len({quote_atom(p) for p in props}) == len(set(props))

    def test_unquote_leaves_sequent_arrow_alone(self):
        line = f"  AXIOM: {quote_atom('x > 5')}, {quote_atom(WHALES)} => {quote_atom('a < b')}"
        assert unquote_atoms(line) == f"  AXIOM: x > 5, {WHALES} => a < b"


def canon(nmms_sentence: str) -> str:
    """pyNMMS's own canonical spelling of a sentence. `to_nmms_sentence`
    returns that form (its bracketing differs across pyNMMS releases, so
    the expectations below are stated through the parser, not as literals)."""
    from pynmms import parse_sentence

    return str(parse_sentence(nmms_sentence))


class TestQueryTranslation:
    KNOWN = frozenset({WHALES, AIR, "A", "B", "R&D spending is up", "x > 5", "a < b"})

    def test_known_atom_is_taken_verbatim(self):
        assert to_nmms_sentence(WHALES, self.KNOWN) == f"<{WHALES}>"
        # Even when it contains connective characters.
        assert to_nmms_sentence("R&D spending is up", self.KNOWN) == "<R&D spending is up>"
        assert to_nmms_sentence(f"  {WHALES} ", self.KNOWN) == f"<{WHALES}>"

    def test_unknown_plain_sentence_is_one_atom(self):
        assert to_nmms_sentence("Whales are fish", self.KNOWN) == "<Whales are fish>"

    def test_identifier_style_complex_query(self):
        assert to_nmms_sentence("A -> B", self.KNOWN) == canon("<A> -> <B>")
        assert to_nmms_sentence("~(A & B) | A", self.KNOWN) == canon("~(<A> & <B>) | <A>")

    def test_natural_language_complex_query(self):
        assert to_nmms_sentence(f"{WHALES} -> {AIR}", self.KNOWN) == canon(
            f"<{WHALES}> -> <{AIR}>"
        )
        assert to_nmms_sentence(f"~{WHALES}", self.KNOWN) == f"~<{WHALES}>"

    def test_explicit_quotes_are_verbatim(self):
        got = to_nmms_sentence("<R&D spending is up> -> <A>", self.KNOWN)
        assert got == canon("<R&D spending is up> -> <A>")

    def test_quoted_known_atom_may_contain_angle_brackets(self):
        assert to_nmms_sentence("~<x > 5>", self.KNOWN) == "~<x %3E 5>"
        assert to_nmms_sentence("<a < b> & <x > 5>", self.KNOWN) == canon("<a %3C b> & <x %3E 5>")

    def test_unquoted_known_atom_with_syntax_chars_is_an_error(self):
        """Reading a stored proposition as syntax would silently answer a
        different question — demand quotes instead."""
        with pytest.raises(ValueError, match="must be quoted as <R&D spending is up>"):
            to_nmms_sentence("~R&D spending is up", self.KNOWN)

    @pytest.mark.parametrize(
        "bad",
        ["", "   ", "A &", "-> B", "A -> ", "~", "()", "(A & B", "A & B)", "A B <C>", "<unclosed"],
    )
    def test_malformed_query_raises_value_error(self, bad):
        with pytest.raises(ValueError, match="Malformed query sentence"):
            to_nmms_sentence(bad, self.KNOWN)

    def test_malformed_query_has_its_own_error_type(self):
        """Callers catch QuerySyntaxError, not bare ValueError, so a
        ValueError from inside pyNMMS isn't mistaken for a bad query."""
        with pytest.raises(QuerySyntaxError):
            to_nmms_sentence("A &", self.KNOWN)
        assert issubclass(QuerySyntaxError, ValueError)

    def test_rejections_are_logged(self, caplog):
        with (
            caplog.at_level(logging.INFO, logger="elenchus.material_base"),
            pytest.raises(QuerySyntaxError),
        ):
            to_nmms_sentence("<unclosed", self.KNOWN)
        assert any("rejected '<unclosed'" in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize(
        ("query", "canonical"),
        [("(A)", "<A>"), ("((A))", "<A>"), (" ( A ) ", "<A>"), ("~(A)", "~<A>")],
    )
    def test_result_is_canonical(self, query, canonical):
        """pyNMMS 0.6.2 compares sentences as strings: `(<A>)` left as
        assembled would fail Containment and the exact base match there."""
        assert to_nmms_sentence(query, frozenset({"A"})) == canonical

    def test_padded_quote_resolves_to_the_known_atom(self):
        known = frozenset({"A", " padded "})
        assert to_nmms_sentence("< A >", known) == quote_atom("A")
        assert to_nmms_sentence("~< A >", known) == "~" + quote_atom("A")
        # A genuinely padded atom still matches verbatim first...
        assert to_nmms_sentence("< padded >", known) == quote_atom(" padded ")
        # ...and unknown quoted text stays verbatim.
        assert to_nmms_sentence("< new >", known) == quote_atom(" new ")

    @pytest.mark.parametrize(
        "bad",
        [
            "~" * 3000 + "A",
            "(" * 3000 + "A" + ")" * 3000,
            "~" * (MAX_QUERY_NESTING + 1) + "A",
            "A & " * MAX_QUERY_SENTENCE_CHARS + "A",
        ],
    )
    def test_oversized_query_is_a_syntax_error_not_a_crash(self, bad):
        """A RecursionError here used to escape `except ValueError` and
        surface as an HTTP 500 / REPL crash."""
        with pytest.raises(QuerySyntaxError, match="Malformed query sentence"):
            to_nmms_sentence(bad, frozenset({"A"}))

    def test_nesting_at_the_cap_still_parses(self):
        assert to_nmms_sentence("~" * MAX_QUERY_NESTING + "A", frozenset({"A"}))

    def test_known_atom_is_exempt_from_the_caps(self):
        """A stored proposition is an atom however many parentheses it has."""
        atom = "f" + "(" * (MAX_QUERY_NESTING + 5)
        assert to_nmms_sentence(atom, frozenset({atom})) == quote_atom(atom)


class TestNaturalLanguageDerivability:
    def test_derives_with_natural_language_atoms(self):
        base = MaterialBase.in_memory("test")
        base.add_atoms({WHALES, AIR, LUNGS})
        base.accept({WHALES}, {AIR}, "tester")
        assert base.derives({WHALES}, {AIR}) is True
        assert base.derives({WHALES}, {WHALES}) is True  # Containment
        assert base.derives({WHALES}, {LUNGS}) is False
        assert base.derives({WHALES, LUNGS}, {AIR}) is False  # no Weakening

    def test_incremental_sync_after_reasoner_built(self):
        """add_atoms / accept after the reasoner exists go through the
        incremental mirror path, which must quote too."""
        base = MaterialBase.in_memory("test")
        base.add_atoms({WHALES, AIR})
        assert base.derives({WHALES}, {AIR}) is False  # builds the reasoner
        assert base._nmms_base is not None

        base.add_atoms({LUNGS})
        assert quote_atom(LUNGS) in base._nmms_base.language
        base.accept({WHALES}, {AIR}, "tester")
        assert (
            frozenset({quote_atom(WHALES)}),
            frozenset({quote_atom(AIR)}),
        ) in base._nmms_base.consequences

        assert base.derives({WHALES}, {AIR}) is True
        assert base.derives({WHALES}, {LUNGS}) is False

    def test_rebuild_after_reject(self):
        base = MaterialBase.in_memory("test")
        base.add_atoms({WHALES, AIR, LUNGS})
        base.accept({WHALES}, {AIR}, "tester")
        base.accept({WHALES}, {LUNGS}, "tester")
        assert base.derives({WHALES}, {AIR}) is True
        base.reject({WHALES}, {AIR}, "tester")
        assert base._nmms_base is None  # full rebuild pending
        assert base.derives({WHALES}, {AIR}) is False
        assert base.derives({WHALES}, {LUNGS}) is True

    def test_rebuild_after_retract_assessment(self):
        base = MaterialBase.in_memory("test")
        base.add_atoms({WHALES, AIR})
        base.accept({WHALES}, {AIR}, "tester")
        assert base.derives({WHALES}, {AIR}) is True
        (aid,) = base.con.execute("SELECT id FROM assessments").fetchone()
        assert base.retract_assessment(aid) is True
        assert base.derives({WHALES}, {AIR}) is False

    def test_awkward_propositions_build_and_derive(self):
        """One proposition containing `<` must not break the whole base."""
        base = MaterialBase.in_memory("test")
        base.add_atoms(set(AWKWARD))
        for premise, conclusion in zip(AWKWARD, AWKWARD[1:], strict=False):
            base.accept({premise}, {conclusion}, "tester")
        for premise, conclusion in zip(AWKWARD, AWKWARD[1:], strict=False):
            assert base.derives({premise}, {conclusion}) is True
        assert base.derives({AWKWARD[0]}, {AWKWARD[2]}) is False  # no Cut

    def test_trace_shows_plain_propositions(self):
        base = MaterialBase.in_memory("test")
        base.add_atoms({"PaO2/FiO2 < 300 mmHg", "50% of whales sing"})
        base.accept({"PaO2/FiO2 < 300 mmHg"}, {"50% of whales sing"}, "tester")
        result = base.derive_with_trace({"PaO2/FiO2 < 300 mmHg"}, {"50% of whales sing"})
        assert result.derivable is True
        assert result.depth_reached == 0
        assert len(result.trace) == 1
        line = result.trace[0]
        assert "PaO2/FiO2 < 300 mmHg" in line and "50% of whales sing" in line
        assert "%3C" not in line and "%25" not in line and "<PaO2" not in line

    def test_complex_query_over_natural_language_atoms(self):
        base = MaterialBase.in_memory("test")
        base.add_atoms({WHALES, AIR})
        base.accept({WHALES}, {AIR}, "tester")
        result = base.derive_with_trace(set(), {f"{WHALES} -> {AIR}"})
        assert result.derivable is True
        assert any(f"{WHALES} -> {AIR}" in line for line in result.trace)
        assert not any("<" in line for line in result.trace)
        # Contraposition via the Ketonen rules, and excluded middle.
        assert base.derives({f"~{AIR}"}, {f"~{WHALES}"}) is True
        assert base.derives(set(), {f"<{LUNGS}> | ~<{LUNGS}>"}) is True
        assert base.derives(set(), {f"{AIR} -> {WHALES}"}) is False

    def test_identifier_style_queries_still_work(self):
        base = MaterialBase.in_memory("test")
        base.add_atoms({"A", "B"})
        base.accept({"A"}, {"B"}, "tester")
        assert base.derives({"A"}, {"B"}) is True
        assert base.derives(set(), {"A -> B"}) is True
        assert base.derives({"~B"}, {"~A"}) is True
        assert base.derives(set(), {"B -> A"}) is False

    def test_malformed_query_raises_value_error(self):
        base = MaterialBase.in_memory("test")
        base.add_atoms({WHALES})
        with pytest.raises(ValueError, match="Malformed query sentence"):
            base.derive_with_trace({f"{WHALES} &"}, {WHALES})

    def test_duckdb_never_sees_quoted_atoms(self):
        base = MaterialBase.in_memory("test")
        base.add_atoms({WHALES, "x > 5"})
        base.accept({WHALES}, {"x > 5"}, "tester")
        base.derives({WHALES}, {"x > 5"})
        assert base.atoms == frozenset({WHALES, "x > 5"})
        premises, conclusions = base.con.execute(
            "SELECT premises, conclusions FROM base_sequents"
        ).fetchone()
        assert str_to_set(premises) == frozenset({WHALES})
        assert str_to_set(conclusions) == frozenset({"x > 5"})

    def test_quoting_is_logged_for_post_run_analysis(self, caplog):
        base = MaterialBase.in_memory("test")
        base.add_atoms({"x > 5", WHALES})
        with caplog.at_level(logging.DEBUG, logger="elenchus.material_base"):
            base.derives({"x > 5"}, {WHALES})
        messages = [r.getMessage() for r in caplog.records]
        assert any("quote_atom: escaped 'x > 5'" in m for m in messages)
        assert any("2 atoms (1 needed escaping)" in m for m in messages)
        assert any(m.startswith("derives {x > 5} |~ {Whales are mammals}") for m in messages)


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
