"""Tests for backup.py and the /api/admin/backup HTTP routes.

Exercise the in-process backup function directly, then test the HTTP
surface (admin-only, returns archive path, prunes correctly).
"""

import contextlib
import os
import shutil
import tarfile

import pytest
from fastapi.testclient import TestClient

# conftest.py sets ELENCHUS_DATA / ELENCHUS_API_KEY / BCRYPT_ROUNDS.
from elenchus import auth, backup
from elenchus.db import get_registry
from elenchus.db import platform as pdb
from elenchus.server import app

_test_data_dir = os.environ["ELENCHUS_DATA"]
client = TestClient(app)


@pytest.fixture(autouse=True)
def _clean():
    reg = get_registry()
    reg.migrate_platform()
    con = reg.platform_con()
    with reg.platform_lock:
        for table in ("auth_sessions", "magic_links", "invites", "sessions", "bases", "actors"):
            con.execute(f"DELETE FROM {table}")
    for _name, handle in list(reg._handles.items()):
        with contextlib.suppress(Exception):
            handle.state.base.con.close()
    reg._handles.clear()
    client.cookies.clear()
    _wipe()
    yield
    client.cookies.clear()
    _wipe()


def _wipe():
    for root, _dirs, files in os.walk(_test_data_dir):
        for f in files:
            if f.endswith(".duckdb") and f != "platform.duckdb":
                with contextlib.suppress(OSError):
                    os.remove(os.path.join(root, f))
    for sub in ("bases", "backups"):
        path = os.path.join(_test_data_dir, sub)
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)


def _create_admin(email="admin@example.com") -> dict:
    con = get_registry().platform_con()
    actor_id = pdb.create_actor(
        con,
        kind="admin",
        email=email,
        display_name="Admin",
        password_hash=auth.hash_password("admin-pw"),
    )
    token = auth.create_session(actor_id)
    client.cookies.set(auth.SESSION_COOKIE, token)
    return {"id": actor_id, "email": email, "password": "admin-pw"}


def _create_user_with_base(email: str, base_name: str) -> dict:
    """Create a non-admin user, log them in, create a base, return their info."""
    con = get_registry().platform_con()
    user_id = pdb.create_actor(
        con,
        kind="user",
        email=email,
        display_name=email.split("@")[0],
        password_hash=auth.hash_password("pw"),
    )
    # Use the API to create the base under this user.
    client.cookies.clear()
    token = auth.create_session(user_id)
    client.cookies.set(auth.SESSION_COOKIE, token)
    r = client.post("/api/dialectics", json={"name": base_name, "topic": base_name})
    assert r.status_code == 200, r.text
    return {"id": user_id, "email": email, "base_name": base_name}


class TestMakeBackup:
    def test_returns_archive_path_with_no_bases(self):
        _create_admin()
        result = backup.make_backup(_test_data_dir)
        assert os.path.exists(result["archive"])
        assert result["archive"].endswith(".tar.gz")
        assert result["bases_dumped"] == []
        assert result["bases_failed"] == []

    def test_archive_contains_platform_dump(self):
        _create_admin()
        result = backup.make_backup(_test_data_dir)
        with tarfile.open(result["archive"]) as tar:
            names = tar.getnames()
        # DuckDB EXPORT DATABASE writes schema.sql + load.sql + (optionally) tables.
        assert any(n.endswith("platform/schema.sql") for n in names), names
        assert any(n.endswith("platform/load.sql") for n in names), names

    def test_includes_each_registered_base(self):
        _create_user_with_base("u1@example.com", "alpha")
        _create_user_with_base("u2@example.com", "beta")
        _create_admin()  # switch back so backup has admin context (not required but mirrors prod)

        result = backup.make_backup(_test_data_dir)
        assert set(result["bases_dumped"]) == {"alpha", "beta"}
        with tarfile.open(result["archive"]) as tar:
            names = tar.getnames()
        assert any(n.endswith("bases/alpha/schema.sql") for n in names), names
        assert any(n.endswith("bases/beta/schema.sql") for n in names), names

    def test_staging_directory_is_cleaned_up(self):
        _create_admin()
        backup.make_backup(_test_data_dir)
        backups_dir = os.path.join(_test_data_dir, "backups")
        leftovers = [d for d in os.listdir(backups_dir) if d.startswith(".staging-")]
        assert leftovers == [], f"staging dir should be removed: {leftovers}"

    def test_custom_output_dir(self, tmp_path):
        _create_admin()
        out = str(tmp_path / "custom-backups")
        result = backup.make_backup(_test_data_dir, output_dir=out)
        assert result["archive"].startswith(out)


class TestListAndPrune:
    def test_list_backups_sorted_newest_first(self, tmp_path):
        out = str(tmp_path)
        # Touch three fake archives with different timestamps in the name.
        for name in (
            "elenchus-20250101-000000.tar.gz",
            "elenchus-20250201-000000.tar.gz",
            "elenchus-20250115-000000.tar.gz",
        ):
            open(os.path.join(out, name), "w").close()
        # Also drop a non-archive file that should be ignored.
        open(os.path.join(out, "not-a-backup.txt"), "w").close()

        archives = backup.list_backups(out)
        assert [os.path.basename(a) for a in archives] == [
            "elenchus-20250201-000000.tar.gz",
            "elenchus-20250115-000000.tar.gz",
            "elenchus-20250101-000000.tar.gz",
        ]

    def test_prune_removes_oldest_beyond_keep(self, tmp_path):
        out = str(tmp_path)
        names = [
            "elenchus-20250101-000000.tar.gz",
            "elenchus-20250201-000000.tar.gz",
            "elenchus-20250301-000000.tar.gz",
            "elenchus-20250401-000000.tar.gz",
        ]
        for name in names:
            open(os.path.join(out, name), "w").close()

        removed = backup.prune_backups(out, keep=2)
        assert {os.path.basename(r) for r in removed} == {
            "elenchus-20250101-000000.tar.gz",
            "elenchus-20250201-000000.tar.gz",
        }
        remaining = {os.path.basename(a) for a in backup.list_backups(out)}
        assert remaining == {
            "elenchus-20250301-000000.tar.gz",
            "elenchus-20250401-000000.tar.gz",
        }

    def test_prune_keep_zero_removes_all(self, tmp_path):
        out = str(tmp_path)
        for i in range(3):
            open(os.path.join(out, f"elenchus-2025010{i}-000000.tar.gz"), "w").close()
        removed = backup.prune_backups(out, keep=0)
        assert len(removed) == 3
        assert backup.list_backups(out) == []

    def test_prune_negative_keep_raises(self, tmp_path):
        with pytest.raises(ValueError):
            backup.prune_backups(str(tmp_path), keep=-1)


class TestBackupHttpRoutes:
    def test_post_backup_admin_only(self):
        # No auth at all.
        r = client.post("/api/admin/backup", json={})
        assert r.status_code == 401

        # Logged in as a non-admin user.
        _create_user_with_base("u@example.com", "x")
        r = client.post("/api/admin/backup", json={})
        assert r.status_code == 403

    def test_admin_can_trigger_backup(self):
        _create_admin()
        r = client.post("/api/admin/backup", json={})
        assert r.status_code == 200, r.text
        data = r.json()
        assert os.path.exists(data["archive"])
        assert data["bases_dumped"] == []
        assert data["bases_failed"] == []

    def test_backup_with_retention(self):
        _create_admin()
        # Pre-seed older fake archives so the prune step has something to drop.
        out = os.path.join(_test_data_dir, "backups")
        os.makedirs(out, exist_ok=True)
        for name in (
            "elenchus-19990101-000000.tar.gz",
            "elenchus-19990201-000000.tar.gz",
        ):
            open(os.path.join(out, name), "w").close()

        r = client.post("/api/admin/backup", json={"keep": 1})
        assert r.status_code == 200, r.text
        data = r.json()
        # At most one archive remains (the one we just made).
        archives = backup.list_backups(out)
        assert len(archives) == 1
        # Both pre-seeded archives were pruned (and not the freshly made one).
        pruned_names = {os.path.basename(p) for p in data["pruned"]}
        assert "elenchus-19990101-000000.tar.gz" in pruned_names
        assert "elenchus-19990201-000000.tar.gz" in pruned_names

    def test_list_backups_admin_only(self):
        r = client.get("/api/admin/backup")
        assert r.status_code == 401

        _create_admin()
        # Make one backup so the list isn't empty.
        client.post("/api/admin/backup", json={})
        r = client.get("/api/admin/backup")
        assert r.status_code == 200
        data = r.json()
        assert len(data["backups"]) >= 1
        assert data["output_dir"].endswith("backups")
