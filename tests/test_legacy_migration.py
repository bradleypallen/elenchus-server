"""Tests for the legacy-migration workflow (`elenchus migrate-legacy`).

Builds a synthetic legacy file in the shared test data dir, runs
`legacy.migrate_legacy`, and verifies the file is registered, relocated,
and that the run is idempotent.
"""

import contextlib
import os
import shutil

import pytest

# conftest.py sets ELENCHUS_DATA / API key / BCRYPT_ROUNDS before any
# elenchus.* import.
from elenchus import auth, legacy
from elenchus.db import get_registry
from elenchus.db import platform as pdb
from elenchus.dialectical_state import DialecticalState

# Importing `elenchus.server` for its side effect: it calls
# `init_registry(DATA_DIR)` at module load. Without this import, the
# registry singleton is None and every test errors out at first access.
from elenchus.server import app as _app  # noqa: F401

_test_data_dir = os.environ["ELENCHUS_DATA"]


@pytest.fixture(autouse=True)
def _reset_platform_state():
    """Each test starts with an empty platform DB and a clean data dir
    (no stray flat-layout or scoped .duckdb files)."""
    reg = get_registry()
    reg.migrate_platform()
    con = reg.platform_con()
    with reg.platform_lock:
        for table in ("auth_sessions", "magic_links", "invites", "sessions", "bases", "actors"):
            con.execute(f"DELETE FROM {table}")
    # Drop any cached per-base handles so the file system is the source of truth.
    for _name, handle in list(reg._handles.items()):
        with contextlib.suppress(Exception):
            handle.state.base.con.close()
    reg._handles.clear()

    _wipe_data_dir()
    yield
    _wipe_data_dir()


def _wipe_data_dir():
    """Remove every per-base .duckdb file (flat + scoped) so tests don't
    leak state. Keeps platform.duckdb in place since the registry holds
    its connection open."""
    for root, _dirs, files in os.walk(_test_data_dir):
        for f in files:
            if f.endswith(".duckdb") and f != "platform.duckdb":
                with contextlib.suppress(OSError):
                    os.remove(os.path.join(root, f))
    bases_dir = os.path.join(_test_data_dir, "bases")
    if os.path.isdir(bases_dir):
        for sub in os.listdir(bases_dir):
            sub_path = os.path.join(bases_dir, sub)
            with contextlib.suppress(OSError):
                shutil.rmtree(sub_path)


def _make_legacy_file(name: str) -> str:
    """Create a synthetic legacy dialectic at `{DATA_DIR}/{name}.duckdb`
    with a couple of atoms so we can verify they survive the move."""
    path = os.path.join(_test_data_dir, f"{name}.duckdb")
    state = DialecticalState.create(path, name)
    state.commit("Sky is blue.")
    state.deny("Sky is green.")
    # Close so the file isn't locked when migrate_legacy reopens it.
    state.base.con.close()
    return path


def _create_admin(email: str = "admin@local") -> int:
    con = get_registry().platform_con()
    return pdb.create_actor(
        con,
        kind="admin",
        email=email,
        display_name="Admin",
        password_hash=auth.hash_password("admin-pw"),
    )


class TestMigrateLegacy:
    def test_moves_flat_file_into_scoped_layout(self):
        admin_id = _create_admin()
        legacy_path = _make_legacy_file("legacy_one")

        summary = legacy.migrate_legacy(_test_data_dir)

        assert summary["admin_id"] == admin_id
        assert len(summary["migrated"]) == 1
        item = summary["migrated"][0]
        assert item["name"] == "legacy_one"
        assert item["action"] == "moved"

        # File is no longer at the legacy path.
        assert not os.path.exists(legacy_path)
        # It's at the scoped path.
        scoped = os.path.join(_test_data_dir, "bases", str(admin_id), "legacy_one.duckdb")
        assert os.path.exists(scoped)

    def test_registers_base_under_admin(self):
        admin_id = _create_admin()
        _make_legacy_file("legacy_two")

        legacy.migrate_legacy(_test_data_dir)

        base = pdb.find_base(get_registry().platform_con(), "legacy_two")
        assert base is not None
        assert base["owner_id"] == admin_id

    def test_data_survives_migration(self):
        admin_id = _create_admin()
        _make_legacy_file("legacy_three")

        legacy.migrate_legacy(_test_data_dir)

        scoped = os.path.join(_test_data_dir, "bases", str(admin_id), "legacy_three.duckdb")
        state = DialecticalState.open(scoped)
        try:
            assert "Sky is blue." in state.C
            assert "Sky is green." in state.D
        finally:
            state.base.con.close()

    def test_contributor_id_remapped_to_admin(self):
        # Admins created in this test get whatever id the sequence is at.
        # If the admin is id != 1, contributor_id values seeded with
        # default 1 should be updated to the admin's id.
        # We force a non-1 admin by creating a placeholder actor first.
        con = get_registry().platform_con()
        pdb.create_actor(
            con,
            kind="user",
            email="placeholder@example.com",
            display_name="Placeholder",
            password_hash=None,
        )
        admin_id = _create_admin()
        assert admin_id != 1, "test setup expected non-1 admin id"

        _make_legacy_file("legacy_four")
        legacy.migrate_legacy(_test_data_dir)

        scoped = os.path.join(_test_data_dir, "bases", str(admin_id), "legacy_four.duckdb")
        state = DialecticalState.open(scoped)
        try:
            row = state.base.con.execute("SELECT contributor_id FROM atoms LIMIT 1").fetchone()
            assert row[0] == admin_id, "contributor_id was not remapped to admin"
        finally:
            state.base.con.close()

    def test_idempotent_second_run(self):
        admin_id = _create_admin()
        _make_legacy_file("legacy_five")

        first = legacy.migrate_legacy(_test_data_dir)
        assert first["migrated"][0]["action"] == "moved"

        # Second run: nothing to do — the legacy file no longer exists at
        # the flat path. _list_legacy_files only sees flat-layout files,
        # so a re-run reports zero migrations.
        second = legacy.migrate_legacy(_test_data_dir)
        assert second["migrated"] == []
        assert second["errors"] == []
        # Scoped file is still present.
        scoped = os.path.join(_test_data_dir, "bases", str(admin_id), "legacy_five.duckdb")
        assert os.path.exists(scoped)

    def test_missing_admin_errors_without_create_flag(self):
        # No admin exists yet.
        _make_legacy_file("legacy_six")
        with pytest.raises(ValueError, match="No admin actor"):
            legacy.migrate_legacy(_test_data_dir, admin_email="ghost@example.com")

    def test_create_admin_flag_provisions_admin(self):
        _make_legacy_file("legacy_seven")
        summary = legacy.migrate_legacy(
            _test_data_dir,
            admin_email="new-admin@example.com",
            create_admin=True,
            admin_password="hunter2",
        )
        assert summary["admin_email"] == "new-admin@example.com"

        # The new admin can authenticate with the supplied password.
        actor = auth.authenticate("new-admin@example.com", "hunter2")
        assert actor is not None
        assert actor["kind"] == "admin"

    def test_non_admin_email_refuses_to_migrate(self):
        con = get_registry().platform_con()
        pdb.create_actor(
            con,
            kind="user",
            email="not-an-admin@example.com",
            display_name="Regular User",
            password_hash=auth.hash_password("pw"),
        )
        _make_legacy_file("legacy_eight")
        with pytest.raises(ValueError, match="not admin"):
            legacy.migrate_legacy(_test_data_dir, admin_email="not-an-admin@example.com")

    def test_platform_db_not_treated_as_legacy(self):
        _create_admin()
        # platform.duckdb is in the data dir but must not be migrated.
        platform_path = os.path.join(_test_data_dir, "platform.duckdb")
        assert os.path.exists(platform_path), "platform DB should exist after fixture"
        summary = legacy.migrate_legacy(_test_data_dir)
        names = [item["name"] for item in summary["migrated"]]
        assert "platform" not in names
