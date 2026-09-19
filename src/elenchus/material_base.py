"""
material_base.py — DuckDB-backed material bases for NMMS

A material base B = ⟨L_B, |∼_B⟩ consists of an atomic language
and a base consequence relation. This module stores both in DuckDB
and delegates derivability to pyNMMS (NMMSReasoner).

Schema management is handled by `migrations/runner.py`. The per-base
schema is defined in `migrations/base/*.sql`; calling
`apply_migrations(con, "base")` brings a connection up to the current
schema version idempotently.

Atoms are natural-language propositions ("Whales are mammals"). DuckDB
stores them verbatim; pyNMMS (>= 0.6.2) only accepts identifiers or
quoted atoms `<...>`, so every atom is quoted on its way into pyNMMS and
unquoted on its way out — see the "pyNMMS boundary" section below.
"""

import json
import logging
import re
from dataclasses import dataclass

import duckdb
from pynmms import MaterialBase as NMMSBase
from pynmms import NMMSReasoner, parse_sentence

from .migrations import apply_migrations

logger = logging.getLogger(__name__)

_DELIM = "\x1e"  # ASCII Record Separator — safe delimiter for natural-language propositions


def set_to_str(s):
    if not s:
        return ""
    # Trailing _DELIM ensures even single-element sets are marked as new format
    return _DELIM.join(sorted(s)) + _DELIM


def str_to_set(s):
    if not s:
        return frozenset()
    # New format uses \x1e; legacy data uses comma
    if _DELIM in s:
        return frozenset(p for p in s.split(_DELIM) if p)
    return frozenset(s.split(","))


def fmt_set(s):
    if not s:
        return "∅"
    return "{" + ", ".join(sorted(s)) + "}"


# ── pyNMMS boundary: atom quoting ──
#
# pyNMMS >= 0.6.2 has a strict atom grammar: an atom is an identifier, an
# applied identifier `C(a)`, or a quoted atom `<...>` whose content is
# taken verbatim but may not itself contain `<` or `>`. Elenchus atoms are
# arbitrary natural-language sentences, so *every* atom crosses the
# boundary quoted (uniformly — identifier-style atoms too, which keeps the
# mapping one rule and injective), with `<`, `>` and the escape character
# `%` percent-encoded. Nothing quoted is ever written to DuckDB.

_NMMS_ESCAPES = {"%": "%25", "<": "%3C", ">": "%3E"}
_NMMS_UNESCAPES = {v: k for k, v in _NMMS_ESCAPES.items()}
_NMMS_ESCAPE_RE = re.compile(r"[%<>]")
_NMMS_UNESCAPE_RE = re.compile(r"%(?:25|3C|3E)")
_NMMS_QUOTED_RE = re.compile(r"<([^<>]*)>")

# Characters that are syntax, not text, in an unquoted query sentence.
_QUERY_SYNTAX_RE = re.compile(r"[()&|~<]|->")
_QUERY_SYNTAX_HELP = (
    "Join propositions with ~ (not), & (and), | (or), -> (implies) and group "
    "with parentheses; quote a proposition that itself contains any of those "
    "characters as <...>."
)


def quote_atom(sentence):
    """Elenchus proposition → the pyNMMS quoted atom that stands for it."""
    escaped = _NMMS_ESCAPE_RE.sub(lambda m: _NMMS_ESCAPES[m.group()], sentence)
    if escaped != sentence:
        logger.debug("quote_atom: escaped %r → <%s>", sentence, escaped)
    return f"<{escaped}>"


def unquote_atoms(text):
    """Replace every quoted atom in pyNMMS output (e.g. a proof-trace
    line) with the Elenchus proposition it stands for."""
    return _NMMS_QUOTED_RE.sub(
        lambda m: _NMMS_UNESCAPE_RE.sub(lambda e: _NMMS_UNESCAPES[e.group()], m.group(1)),
        text,
    )


def _closing_quote(sentence, start, known_atoms):
    """Index of the `>` closing the quoted proposition opened at `start`,
    or -1. Normally the first `>`; a later one is preferred when it makes
    the content a known atom, so stored propositions that contain `>`
    ("x > 5") stay addressable."""
    first = sentence.find(">", start + 1)
    pos = first
    while pos != -1:
        if sentence[start + 1 : pos] in known_atoms:
            if pos != first:
                logger.debug(
                    "to_nmms_sentence: quote in %r closed at known atom %r",
                    sentence,
                    sentence[start + 1 : pos],
                )
            return pos
        pos = sentence.find(">", pos + 1)
    return first


def to_nmms_sentence(sentence, known_atoms=frozenset()):
    """Translate one derivability-query sentence into pyNMMS syntax.

    A sentence that is verbatim a known atom is that atom, whatever
    characters it contains. Anything else is read as pyNMMS syntax over
    propositions: `<...>` quotes a proposition verbatim, and each
    remaining run of text between connectives / parentheses is a
    proposition (so identifier-style queries like `A -> B` keep working).

    Raises ValueError, phrased in terms of the original sentence, if the
    result is not a well-formed pyNMMS sentence — or if a known atom that
    contains syntax characters ("R&D is up") appears unquoted inside a
    larger sentence, where reading it as syntax would silently answer a
    different question.
    """
    for candidate in (sentence, sentence.strip()):
        if candidate in known_atoms:
            if _QUERY_SYNTAX_RE.search(candidate):
                logger.debug("to_nmms_sentence: %r matched a known atom verbatim", candidate)
            return quote_atom(candidate)
    syntax_atoms = [a for a in known_atoms if _QUERY_SYNTAX_RE.search(a)]

    def malformed(reason):
        return ValueError(f"Malformed query sentence {sentence!r}: {reason}. {_QUERY_SYNTAX_HELP}")

    out = []
    run = []  # characters of the current unquoted proposition
    after_operand = False  # last token emitted was a proposition or ")"

    def emit(token, starts_operand, ends_operand):
        nonlocal after_operand
        if starts_operand and after_operand:
            raise malformed(f"no connective before {unquote_atoms(token)!r}")
        out.append(token)
        after_operand = ends_operand

    def flush_run():
        text = "".join(run).strip()
        run.clear()
        if text:
            emit(quote_atom(text), True, True)

    i = 0
    while i < len(sentence):
        c = sentence[i]
        if not run and not c.isspace():  # a proposition may start here
            for atom in syntax_atoms:
                if sentence.startswith(atom, i):
                    raise malformed(
                        f"the proposition {atom!r} contains syntax characters, so inside "
                        f"a larger sentence it must be quoted as <{atom}>"
                    )
        if c == "<":
            flush_run()
            end = _closing_quote(sentence, i, known_atoms)
            if end == -1:
                raise malformed("'<' opens a quoted proposition that is never closed with '>'")
            emit(quote_atom(sentence[i + 1 : end]), True, True)
            i = end + 1
        elif sentence.startswith("->", i):
            flush_run()
            emit(" -> ", False, False)
            i += 2
        elif c in "&|":
            flush_run()
            emit(f" {c} ", False, False)
            i += 1
        elif c in "(~":
            flush_run()
            emit(c, True, False)
            i += 1
        elif c == ")":
            flush_run()
            emit(c, False, True)
            i += 1
        else:
            if run or not c.isspace():
                run.append(c)
            i += 1
    flush_run()

    translated = "".join(out)
    try:
        parsed = parse_sentence(translated)
    except ValueError as e:
        logger.debug("to_nmms_sentence: pyNMMS rejected %r (from %r): %s", translated, sentence, e)
        raise malformed("connectives and parentheses do not form a sentence") from None
    if parsed.type != "atom":
        logger.debug("to_nmms_sentence: %r read as complex sentence %s", sentence, translated)
    return translated


@dataclass(frozen=True)
class DerivationResult:
    """Outcome of a derivability query, in Elenchus vocabulary: the
    fields of pyNMMS's `ProofResult` that consumers use, with quoted
    atoms in the trace turned back into plain propositions."""

    derivable: bool
    trace: list[str]
    depth_reached: int
    cache_hits: int


class MaterialBase:
    def __init__(self, con, name):
        self.con = con
        self.name = name
        self._nmms_base: NMMSBase | None = None
        self._reasoner: NMMSReasoner | None = None

    @classmethod
    def create(cls, db_path, name):
        con = duckdb.connect(db_path)
        apply_migrations(con, "base")
        con.execute("INSERT OR REPLACE INTO meta VALUES ('name', ?)", [name])
        con.execute("INSERT OR REPLACE INTO meta VALUES ('version', '5')")
        return cls(con, name)

    @classmethod
    def open(cls, db_path):
        con = duckdb.connect(db_path)
        # Validate that the file has the expected schema before letting
        # the migration runner touch it. An empty / non-Elenchus file
        # should not be silently "migrated" into one.
        tables = {
            r[0]
            for r in con.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
            ).fetchall()
        }
        if "meta" not in tables:
            con.close()
            raise ValueError(
                f"Database '{db_path}' is not a valid Elenchus dialectic "
                f"(missing 'meta' table, found tables: {tables or 'none'})"
            )
        # Bring the schema up to current version. Safe on already-current
        # files; the runner skips migrations <= meta.schema_version.
        apply_migrations(con, "base")
        r = con.execute("SELECT value FROM meta WHERE key='name'").fetchone()
        name = r[0] if r else "unnamed"
        return cls(con, name)

    @classmethod
    def in_memory(cls, name="unnamed"):
        con = duckdb.connect(":memory:")
        apply_migrations(con, "base")
        con.execute("INSERT INTO meta VALUES ('name', ?)", [name])
        con.execute("INSERT INTO meta VALUES ('version', '5')")
        return cls(con, name)

    @property
    def atoms(self):
        rows = self.con.execute("SELECT sentence FROM atoms").fetchall()
        return frozenset(r[0] for r in rows)

    def add_atoms(self, atoms, contributor="system", description=""):
        # INSERT OR IGNORE is idempotent and (unlike try/except on
        # ConstraintException) does not abort an outer transaction when
        # the atom already exists. This matters when add_atoms is
        # called from inside `Opponent._record_and_apply`'s transaction.
        for a in atoms:
            self.con.execute(
                "INSERT OR IGNORE INTO atoms "
                "(sentence, added_by, added_at, description) "
                "VALUES (?, ?, CURRENT_TIMESTAMP, ?)",
                [a, contributor, description],
            )
            if self._nmms_base is not None:
                self._nmms_base.add_atom(quote_atom(a))

    def accept(self, premises, conclusions, contributor, reason="", domain="", provenance=None):
        """Insert a 'holds' assessment for `{premises} |~ {conclusions}`.

        `provenance` is an optional dict (JSON-serialized to the
        `provenance` column) recording why this assertion exists —
        typical shape: `{source, session_id, case_id, turn, reason,
        earned_via_tension?}`. Defaults to `{}` so the column stays
        non-null and queryable.
        """
        prov_json = json.dumps(provenance or {})
        self.con.execute(
            "INSERT INTO assessments (premises, conclusions, judgment, "
            "contributor, reason, domain, provenance) "
            "VALUES (?,?,'holds',?,?,?,?)",
            [
                set_to_str(premises),
                set_to_str(conclusions),
                contributor,
                reason,
                domain,
                prov_json,
            ],
        )
        if self._nmms_base is not None:
            self._nmms_base.add_consequence(
                frozenset(quote_atom(p) for p in premises),
                frozenset(quote_atom(c) for c in conclusions),
            )
            self._reasoner = None  # rebuild reasoner with updated base

    def reject(self, premises, conclusions, contributor, reason="", domain="", provenance=None):
        """Insert a 'rejected' assessment. See `accept` for the
        `provenance` parameter."""
        prov_json = json.dumps(provenance or {})
        self.con.execute(
            "INSERT INTO assessments (premises, conclusions, judgment, "
            "contributor, reason, domain, provenance) "
            "VALUES (?,?,'rejected',?,?,?,?)",
            [
                set_to_str(premises),
                set_to_str(conclusions),
                contributor,
                reason,
                domain,
                prov_json,
            ],
        )
        self._invalidate_reasoner()  # full rebuild needed — most-recent-wins logic

    def retract_assessment(self, assessment_id: int) -> bool:
        """Mark an existing assessment row as retracted (status='retracted').

        The `current_assessments` view filters on `status='active'`, so a
        retracted row stops contributing to `base_sequents` immediately.
        Returns True if a row was updated, False if no active row with
        that id existed.
        """
        rows = self.con.execute(
            "UPDATE assessments SET status='retracted' "
            "WHERE id = ? AND status = 'active' RETURNING id",
            [assessment_id],
        ).fetchall()
        if rows:
            # Reasoner cache depends on the (now-changed) base; rebuild
            # next derivability check.
            self._invalidate_reasoner()
            return True
        return False

    # ── pyNMMS reasoner (in-memory mirror of DuckDB base) ──

    def _ensure_reasoner(self):
        """Build or rebuild the pyNMMS reasoner from DuckDB state."""
        if self._reasoner is not None:
            return
        base = NMMSBase()
        escaped = 0
        for (atom,) in self.con.execute("SELECT sentence FROM atoms").fetchall():
            base.add_atom(quote_atom(atom))
            escaped += bool(_NMMS_ESCAPE_RE.search(atom))
        for p, c in self.con.execute("SELECT premises, conclusions FROM base_sequents").fetchall():
            base.add_consequence(
                frozenset(quote_atom(a) for a in str_to_set(p)),
                frozenset(quote_atom(a) for a in str_to_set(c)),
            )
        self._nmms_base = base
        self._reasoner = NMMSReasoner(base)
        logger.info(
            "Built pyNMMS reasoner: %d atoms (%d needed escaping), %d consequences",
            len(base.language),
            escaped,
            len(base.consequences),
        )

    def _invalidate_reasoner(self):
        """Force full rebuild on next query (e.g. after reject)."""
        self._nmms_base = None
        self._reasoner = None

    def derives(self, premises, conclusions):
        return self.derive_with_trace(premises, conclusions).derivable

    def derive_with_trace(self, premises, conclusions):
        """Check `{premises} |~ {conclusions}` and return a `DerivationResult`.

        Each premise / conclusion is a known atom verbatim or a logically
        complex query sentence (see `to_nmms_sentence`). Raises ValueError
        if one is malformed.
        """
        known = self.atoms
        gamma = frozenset(to_nmms_sentence(s, known) for s in premises)
        delta = frozenset(to_nmms_sentence(s, known) for s in conclusions)
        self._ensure_reasoner()
        proof = self._reasoner.derives(gamma, delta)
        trace = [unquote_atoms(line) for line in proof.trace]
        logger.info(
            "derives %s |~ %s → %s (depth=%d, cache_hits=%d, trace_lines=%d)",
            fmt_set(premises),
            fmt_set(conclusions),
            proof.derivable,
            proof.depth_reached,
            proof.cache_hits,
            len(trace),
        )
        return DerivationResult(
            derivable=proof.derivable,
            trace=trace,
            depth_reached=proof.depth_reached,
            cache_hits=proof.cache_hits,
        )

    def gaps_for(self, premises, conclusions):
        """Unassessed weakenings of a sequent."""
        gaps = []
        all_atoms = self.atoms
        assessed = set()
        rows = self.con.execute("SELECT premises, conclusions FROM current_assessments").fetchall()
        for p, c in rows:
            assessed.add((p, c))

        p_str = set_to_str(premises)
        c_str = set_to_str(conclusions)

        for a in all_atoms:
            if a not in premises and a not in conclusions:
                # Weaken left
                wp = set_to_str(premises | {a})
                if (wp, c_str) not in assessed:
                    gaps.append({"premises": premises | {a}, "conclusions": conclusions})
                # Weaken right
                wc = set_to_str(conclusions | {a})
                if (p_str, wc) not in assessed:
                    gaps.append({"premises": premises, "conclusions": conclusions | {a}})
        return gaps

    def completeness(self):
        assessed = self.con.execute("SELECT COUNT(*) FROM current_assessments").fetchone()[0]
        n = len(self.atoms)
        total = max(1, n * (n - 1))  # rough estimate
        return {"assessed": assessed, "total": total, "pct": assessed / total if total else 0}

    def report(self):
        lines = [f"═══ Material Base: {self.name} ═══", ""]
        lines.append(f"L_B: {len(self.atoms)} atoms")
        rows = self.con.execute(
            "SELECT premises, conclusions, n_assessors FROM base_sequents"
        ).fetchall()
        lines.append(f"|∼_B|: {len(rows)} sequents")
        for p, c, _n in rows:
            lines.append(f"  {fmt_set(str_to_set(p))} ∼ {fmt_set(str_to_set(c))}")
        cr = self.completeness()
        lines.append(f"\nCompleteness: {cr['pct']:.0%} ({cr['assessed']}/{cr['total']})")
        return "\n".join(lines)

    def _migrate_delimiter(self):
        """Re-serialize all assessments from comma to \\x1e delimiter.

        Already-shattered propositions (those containing commas that were
        split on ingest) cannot be automatically reconstructed — those
        need manual repair. This only re-writes the stored delimiter so
        that future reads use the new format.
        """
        rows = self.con.execute("SELECT rowid, premises, conclusions FROM assessments").fetchall()
        migrated = 0
        for rowid, p, c in rows:
            if _DELIM not in p and _DELIM not in c:
                new_p = set_to_str(str_to_set(p))
                new_c = set_to_str(str_to_set(c))
                if new_p != p or new_c != c:
                    self.con.execute(
                        "UPDATE assessments SET premises=?, conclusions=? WHERE rowid=?",
                        [new_p, new_c, rowid],
                    )
                    migrated += 1
        logger.info("_migrate_delimiter: re-serialized %d assessment rows", migrated)
        return migrated
