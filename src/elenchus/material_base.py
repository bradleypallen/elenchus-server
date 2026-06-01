"""
material_base.py — DuckDB-backed material bases for NMMS

A material base B = ⟨L_B, |∼_B⟩ consists of an atomic language
and a base consequence relation. This module stores both in DuckDB
and delegates derivability to pyNMMS (NMMSReasoner).

Schema management is handled by `migrations/runner.py`. The per-base
schema is defined in `migrations/base/*.sql`; calling
`apply_migrations(con, "base")` brings a connection up to the current
schema version idempotently.
"""

import logging

import duckdb
from pynmms import MaterialBase as NMMSBase
from pynmms import NMMSReasoner

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
                "INSERT OR IGNORE INTO atoms VALUES (?, ?, CURRENT_TIMESTAMP, ?)",
                [a, contributor, description],
            )
            if self._nmms_base is not None:
                self._nmms_base.add_atom(a)

    def accept(self, premises, conclusions, contributor, reason="", domain=""):
        self.con.execute(
            "INSERT INTO assessments (premises, conclusions, judgment, "
            "contributor, reason, domain) VALUES (?,?,'holds',?,?,?)",
            [set_to_str(premises), set_to_str(conclusions), contributor, reason, domain],
        )
        if self._nmms_base is not None:
            self._nmms_base.add_consequence(frozenset(premises), frozenset(conclusions))
            self._reasoner = None  # rebuild reasoner with updated base

    def reject(self, premises, conclusions, contributor, reason="", domain=""):
        self.con.execute(
            "INSERT INTO assessments (premises, conclusions, judgment, "
            "contributor, reason, domain) VALUES (?,?,'rejected',?,?,?)",
            [set_to_str(premises), set_to_str(conclusions), contributor, reason, domain],
        )
        self._invalidate_reasoner()  # full rebuild needed — most-recent-wins logic

    # ── pyNMMS reasoner (in-memory mirror of DuckDB base) ──

    def _ensure_reasoner(self):
        """Build or rebuild the pyNMMS reasoner from DuckDB state."""
        if self._reasoner is not None:
            return
        base = NMMSBase()
        for (atom,) in self.con.execute("SELECT sentence FROM atoms").fetchall():
            base.add_atom(atom)
        for p, c in self.con.execute("SELECT premises, conclusions FROM base_sequents").fetchall():
            base.add_consequence(str_to_set(p), str_to_set(c))
        self._nmms_base = base
        self._reasoner = NMMSReasoner(base)
        logger.info(
            "Built pyNMMS reasoner: %d atoms, %d consequences",
            len(base.language),
            len(base.consequences),
        )

    def _invalidate_reasoner(self):
        """Force full rebuild on next query (e.g. after reject)."""
        self._nmms_base = None
        self._reasoner = None

    def derives(self, premises, conclusions):
        self._ensure_reasoner()
        result = self._reasoner.derives(frozenset(premises), frozenset(conclusions))
        logger.info(
            "derives %s |~ %s → %s (depth=%d)",
            fmt_set(premises),
            fmt_set(conclusions),
            result.derivable,
            result.depth_reached,
        )
        return result.derivable

    def derive_with_trace(self, premises, conclusions):
        """Return full ProofResult including trace."""
        self._ensure_reasoner()
        return self._reasoner.derives(frozenset(premises), frozenset(conclusions))

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
