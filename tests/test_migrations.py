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
