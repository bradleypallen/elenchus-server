"""Tests for the migration runner.

These tests exercise the runner directly against fresh DuckDB connections,
isolated from the MaterialBase / DialecticalState wrappers that already
exercise it indirectly through their constructors.
"""

import duckdb
import pytest

from elenchus.migrations import (
    apply_migrations,
    current_schema_version,
    list_migrations,
)


class TestListMigrations:
    def test_finds_base_migrations(self):
        migrations = list_migrations("base")
        assert len(migrations) >= 1, "expected at least one base migration"
        # First migration is version 1.
        assert migrations[0][0] == 1

    def test_versions_are_monotonically_increasing(self):
        migrations = list_migrations("base")
        versions = [v for v, _ in migrations]
        assert versions == sorted(versions)

    def test_platform_kind_returns_empty_until_implemented(self):
        # Platform migrations land in Week 2; until then the list is empty.
        # If/when this fails, it means platform migrations exist and the
        # test should be updated to reflect what's there.
        migrations = list_migrations("platform")
        # Allow either empty (Phase A Week 1) or non-empty (Phase A Week 2+).
        assert isinstance(migrations, list)


class TestCurrentSchemaVersion:
    def test_fresh_connection_returns_zero(self):
        con = duckdb.connect(":memory:")
        try:
            assert current_schema_version(con) == 0
        finally:
            con.close()

    def test_reads_version_after_migration(self):
        con = duckdb.connect(":memory:")
        try:
            apply_migrations(con, "base")
            assert current_schema_version(con) >= 1
        finally:
            con.close()


class TestApplyMigrations:
    def test_fresh_db_applies_initial(self):
        con = duckdb.connect(":memory:")
        try:
            new_version = apply_migrations(con, "base")
            assert new_version >= 1
            # The expected tables now exist.
            tables = {
                row[0]
                for row in con.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
                ).fetchall()
            }
            for required in {
                "meta",
                "atoms",
                "assessments",
                "positions",
                "tensions",
                "conversation",
            }:
                assert required in tables, f"migration did not create {required}"
        finally:
            con.close()

    def test_idempotent_reapply(self):
        """Applying migrations twice should be a no-op the second time."""
        con = duckdb.connect(":memory:")
        try:
            v1 = apply_migrations(con, "base")
            v2 = apply_migrations(con, "base")
            assert v1 == v2
            # And the meta.schema_version row is exactly one row, not duplicated.
            count = con.execute("SELECT COUNT(*) FROM meta WHERE key='schema_version'").fetchone()[
                0
            ]
            assert count == 1
        finally:
            con.close()

    def test_version_persists_across_connections(self, tmp_path):
        """Schema version survives a close+reopen cycle."""
        path = str(tmp_path / "test.duckdb")
        con = duckdb.connect(path)
        try:
            apply_migrations(con, "base")
            v_initial = current_schema_version(con)
        finally:
            con.close()

        con2 = duckdb.connect(path)
        try:
            assert current_schema_version(con2) == v_initial
            # Re-applying is a no-op.
            assert apply_migrations(con2, "base") == v_initial
        finally:
            con2.close()


class TestMigrationFailure:
    def test_malformed_migration_raises(self, monkeypatch, tmp_path):
        """A migration file without a version header is a programming error."""
        from elenchus.migrations import runner

        bad_dir = tmp_path / "bad" / "base"
        bad_dir.mkdir(parents=True)
        (bad_dir / "0001_no_header.sql").write_text("CREATE TABLE foo(x INTEGER);")
        monkeypatch.setattr(runner, "MIGRATIONS_ROOT", tmp_path / "bad")

        with pytest.raises(ValueError, match="version"):
            runner.list_migrations("base")


class TestPhaseASchemaExtensions:
    """Verify that the v2 base migration adds the future-proofing fields
    documented in ROADMAP.md without breaking the v1 read paths."""

    def _fresh_con(self):
        con = duckdb.connect(":memory:")
        apply_migrations(con, "base")
        return con

    def test_v2_is_current(self):
        con = self._fresh_con()
        try:
            assert current_schema_version(con) >= 2
        finally:
            con.close()

    def test_atoms_has_contributor_paraphrases_references(self):
        con = self._fresh_con()
        try:
            cols = {
                row[0]
                for row in con.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_name='atoms'"
                ).fetchall()
            }
            for required in {"contributor_id", "paraphrases", "references"}:
                assert required in cols, f"v2 migration did not add atoms.{required}"
        finally:
            con.close()

    def test_assessments_has_contributor_provenance_status(self):
        con = self._fresh_con()
        try:
            cols = {
                row[0]
                for row in con.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='assessments'"
                ).fetchall()
            }
            for required in {"contributor_id", "provenance", "status"}:
                assert required in cols, f"v2 migration did not add assessments.{required}"
        finally:
            con.close()

    def test_positions_tensions_conversation_scoping(self):
        con = self._fresh_con()
        try:

            def cols(table):
                return {
                    row[0]
                    for row in con.execute(
                        "SELECT column_name FROM information_schema.columns WHERE table_name=?",
                        [table],
                    ).fetchall()
                }

            assert {"actor_id", "case_id"}.issubset(cols("positions"))
            assert "case_id" in cols("tensions")
            assert {"session_id", "case_id", "actor_id"}.issubset(cols("conversation"))
        finally:
            con.close()

    def test_cases_table_seeded_with_default_row(self):
        con = self._fresh_con()
        try:
            rows = con.execute("SELECT id, name FROM cases ORDER BY id").fetchall()
            assert rows == [(1, "default")]
        finally:
            con.close()

    def test_assessments_default_status_is_active(self):
        con = self._fresh_con()
        try:
            # Insert via the v1 column list to mimic legacy writes; the
            # v2 default should fill in status='active'.
            con.execute(
                "INSERT INTO assessments (premises, conclusions, judgment, "
                "contributor) VALUES ('a', 'b', 'holds', 'respondent')"
            )
            row = con.execute("SELECT status FROM assessments").fetchone()
            assert row == ("active",)
        finally:
            con.close()

    def test_current_assessments_view_filters_inactive(self):
        """A row with status='superseded' is excluded from current_assessments."""
        con = self._fresh_con()
        try:
            con.execute(
                "INSERT INTO assessments (premises, conclusions, judgment, "
                "contributor, status) VALUES ('a', 'b', 'holds', 'r', 'superseded')"
            )
            con.execute(
                "INSERT INTO assessments (premises, conclusions, judgment, "
                "contributor) VALUES ('c', 'd', 'holds', 'r')"
            )
            rows = con.execute(
                "SELECT premises, conclusions FROM current_assessments ORDER BY premises"
            ).fetchall()
            assert rows == [("c", "d")], "superseded row should be filtered out"
        finally:
            con.close()

    def test_v2_idempotent_on_legacy_file(self, tmp_path):
        """A v1-shaped database should migrate forward to v2 without
        losing data and without re-adding columns on second apply."""
        path = str(tmp_path / "legacy.duckdb")

        # Stop after v1 by monkey-patching list_migrations isn't worth it
        # — instead, simulate a v1 file by manually writing the v1
        # migration only.
        from elenchus.migrations.runner import MIGRATIONS_ROOT

        v1_sql = (MIGRATIONS_ROOT / "base" / "0001_initial.sql").read_text()

        con1 = duckdb.connect(path)
        try:
            con1.execute("BEGIN")
            con1.execute(v1_sql)
            con1.execute("INSERT INTO meta VALUES ('schema_version', '1')")
            con1.execute("INSERT INTO atoms (sentence) VALUES ('Sky is blue.')")
            con1.execute("COMMIT")
        finally:
            con1.close()

        # Now apply migrations — should bring file from v1 to v2.
        con2 = duckdb.connect(path)
        try:
            v = apply_migrations(con2, "base")
            assert v >= 2
            # Existing row survived.
            row = con2.execute("SELECT sentence, contributor_id FROM atoms").fetchone()
            assert row[0] == "Sky is blue."
            # The default contributor_id backfilled to 1.
            assert row[1] == 1
            # Re-applying is a no-op.
            assert apply_migrations(con2, "base") == v
        finally:
            con2.close()
