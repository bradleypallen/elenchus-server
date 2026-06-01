"""Tests for the platform/filesystem audit (`elenchus audit`)."""

import contextlib
import os
import shutil

import duckdb
import pytest
from fastapi.testclient import TestClient

from elenchus import audit, auth
from elenchus.db import get_registry
from elenchus.db import platform as pdb
from elenchus.dialectical_state import DialecticalState
from elenchus.migrations import apply_migrations
from elenchus.server import app

client = TestClient(app)
_test_data_dir = os.environ["ELENCHUS_DATA"]


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
    bases_dir = os.path.join(_test_data_dir, "bases")
    if os.path.isdir(bases_dir):
        shutil.rmtree(bases_dir, ignore_errors=True)


def _login_as_admin() -> int:
    con = get_registry().platform_con()
    admin_id = pdb.create_actor(
        con,
        kind="admin",
        email="admin@example.com",
        display_name="Admin",
        password_hash=auth.hash_password("pw"),
    )
    token = auth.create_session(admin_id)
    client.cookies.set(auth.SESSION_COOKIE, token)
    return admin_id


def _create_base_for(user_id: int, name: str) -> str:
    """Create a scoped base file + bases row for the given user. Returns
    the absolute path of the file."""
    reg = get_registry()
    path = reg.db_path(name, actor_id=user_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    state = DialecticalState.create(path, name)
    state.base.con.close()
    with reg.platform_lock:
        pdb.create_base(reg.platform_con(), base_id=name, name=name, owner_id=user_id)
    return path


class TestAuditPlatform:
    def test_clean_install_reports_no_drift(self):
        _login_as_admin()
        report = audit.audit_platform(_test_data_dir)
        assert report["registered_missing_file"] == []
        assert report["orphan_scoped"] == []
        assert report["orphan_flat"] == []
        assert report["dangling_actor_refs"] == []

    def test_registered_with_matching_file(self):
        admin_id = _login_as_admin()
        _create_base_for(admin_id, "alpha")
        report = audit.audit_platform(_test_data_dir)
        names = [r["id"] for r in report["registered_with_file"]]
        assert names == ["alpha"]

    def test_registered_but_file_missing(self):
        admin_id = _login_as_admin()
        path = _create_base_for(admin_id, "ghost")
        os.remove(path)
        report = audit.audit_platform(_test_data_dir)
        assert any(r["id"] == "ghost" for r in report["registered_missing_file"])

    def test_orphan_scoped_file(self):
        admin_id = _login_as_admin()
        # Create the file under bases/{admin_id}/ but never register it.
        scoped_dir = os.path.join(_test_data_dir, "bases", str(admin_id))
        os.makedirs(scoped_dir, exist_ok=True)
        path = os.path.join(scoped_dir, "rogue.duckdb")
        state = DialecticalState.create(path, "rogue")
        state.base.con.close()
        report = audit.audit_platform(_test_data_dir)
        assert any(r["id"] == "rogue" for r in report["orphan_scoped"])

    def test_orphan_flat_file(self):
        _login_as_admin()
        path = os.path.join(_test_data_dir, "legacy.duckdb")
        state = DialecticalState.create(path, "legacy")
        state.base.con.close()
        report = audit.audit_platform(_test_data_dir)
        assert any(r["id"] == "legacy" for r in report["orphan_flat"])

    def test_dangling_actor_refs(self):
        admin_id = _login_as_admin()
        path = _create_base_for(admin_id, "alpha")
        # Inject a contributor_id pointing at a non-existent actor.
        con = duckdb.connect(path)
        try:
            apply_migrations(con, "base")
            con.execute(
                "INSERT INTO atoms (sentence, added_by, description, contributor_id) "
                "VALUES (?, 'respondent', '', ?)",
                ["A claim.", 9999],
            )
        finally:
            con.close()
        report = audit.audit_platform(_test_data_dir)
        hits = [r for r in report["dangling_actor_refs"] if r["path"] == path]
        assert hits, f"expected dangling ref report; got {report['dangling_actor_refs']}"
        assert 9999 in hits[0]["missing_actor_ids"]


class TestAuditFormatReport:
    def test_clean_report_emits_check_marks(self):
        _login_as_admin()
        report = audit.audit_platform(_test_data_dir)
        text = audit.format_report(report)
        assert "registered files missing on disk: none" in text
        assert "orphan scoped files (no `bases` row): none" in text

    def test_drift_emits_warning_markers(self):
        admin_id = _login_as_admin()
        path = _create_base_for(admin_id, "ghost")
        os.remove(path)
        report = audit.audit_platform(_test_data_dir)
        text = audit.format_report(report)
        assert "⚠ registered files missing on disk: 1" in text
        assert "base='ghost'" in text


class TestAuditHttpRoute:
    def test_admin_only(self):
        # Unauthenticated.
        r = client.get("/api/admin/audit")
        assert r.status_code == 401
        # Non-admin: spin up a user.
        con = get_registry().platform_con()
        user_id = pdb.create_actor(
            con,
            kind="user",
            email="u@example.com",
            display_name="U",
            password_hash=auth.hash_password("pw"),
        )
        client.cookies.set(auth.SESSION_COOKIE, auth.create_session(user_id))
        r = client.get("/api/admin/audit")
        assert r.status_code == 403

    def test_returns_structured_report(self):
        _login_as_admin()
        r = client.get("/api/admin/audit")
        assert r.status_code == 200
        data = r.json()
        for key in (
            "registered_with_file",
            "registered_missing_file",
            "orphan_scoped",
            "orphan_flat",
            "dangling_actor_refs",
        ):
            assert key in data
