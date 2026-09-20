"""Tests for what lets the person running a study work without a shell
on the server: a study's own task length, and downloading exports (the
data archive for researchers; the names key for admins only)."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tarfile

import pytest
from fastapi.testclient import TestClient

from elenchus import auth
from elenchus.db import get_registry
from elenchus.db import platform as pdb
from elenchus.server import app

client = TestClient(app)

CONFIG = {
    "topic_a_title": "Occurrence and its relatives in Darwin Core",
    "topic_a_brief": "Occurrence, Organism, Event, MaterialSample.",
    "topic_b_title": "Taxon concepts and names",
    "topic_b_brief": "Name, taxon concept, usage, circumscription.",
    "min_gap_hours": 0,
}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("ELENCHUS_TASK_MINUTES", raising=False)
    reg = get_registry()
    reg.migrate_platform()
    con = reg.platform_con()
    with reg.platform_lock:
        for table in (
            "study_participants",
            "study_configs",
            "study_texts",
            "participant_session_tokens",
            "auth_sessions",
            "sessions",
            "bases",
            "actors",
        ):
            con.execute(f"DELETE FROM {table}")
    for _name, handle in list(reg._handles.items()):
        with contextlib.suppress(Exception):
            handle.state.base.con.close()
    reg._handles.clear()
    exports = os.path.join(os.path.dirname(reg.platform_path), "exports")
    if os.path.isdir(exports):
        for name in os.listdir(exports):
            with contextlib.suppress(OSError):
                os.remove(os.path.join(exports, name))
    client.cookies.clear()
    yield
    client.cookies.clear()


def _login(kind: str = "researcher", who: TestClient = client) -> int:
    con = get_registry().platform_con()
    actor_id = pdb.create_actor(
        con,
        kind=kind,
        email=f"{kind}-{os.urandom(3).hex()}@example.com",
        display_name=kind,
        password_hash=auth.hash_password("pw"),
    )
    who.cookies.set(auth.SESSION_COOKIE, auth.create_session(actor_id))
    return actor_id


def _setup(study: str, **overrides) -> dict:
    r = client.put(f"/api/admin/study/{study}/config", json={**CONFIG, **overrides})
    assert r.status_code == 200, r.text
    return r.json()


def _enrol(study: str, name: str = "Ada") -> dict:
    r = client.post(f"/api/admin/study/{study}/participants", json={"display_name": name})
    assert r.status_code == 200, r.text
    return r.json()


def _open(token: str) -> tuple[TestClient, dict]:
    """Open a participant link; returns the client and the session as
    the participant's page sees it."""
    pclient = TestClient(app)
    assert pclient.post(f"/api/study/{token}").status_code == 200
    r = pclient.get("/api/study/session")
    assert r.status_code == 200, r.text
    return pclient, r.json()


# ─── A study's own task length ───────────────────────────────────────


class TestTaskMinutes:
    def test_a_practice_study_runs_short_beside_a_real_one(self):
        """The point: no environment variable, no restart, and the real
        study isn't put on the short clock while someone practises."""
        _login()
        _setup("TRAINING", task_minutes=5)
        _setup("PILOT")
        _, short = _open(_enrol("TRAINING")["sessions"][0]["token"])
        _, real = _open(_enrol("PILOT")["sessions"][0]["token"])
        assert short["task_minutes"] == 5
        assert short["soft_warning_minutes"] == [1, 5]
        assert real["task_minutes"] == 60
        assert real["soft_warning_minutes"] == [50, 60]

    def test_empty_means_the_servers_default(self, monkeypatch):
        _login()
        assert _setup("PILOT")["task_minutes"] is None
        monkeypatch.setenv("ELENCHUS_TASK_MINUTES", "45")
        _, session = _open(_enrol("PILOT")["sessions"][0]["token"])
        assert session["task_minutes"] == 45
        assert client.get("/api/admin/study/configs").json()["default_task_minutes"] == 45

    def test_it_can_be_changed_and_cleared(self):
        _login()
        _setup("PILOT", task_minutes=30)
        assert client.get("/api/admin/study/PILOT/config").json()["task_minutes"] == 30
        assert _setup("PILOT", task_minutes=None)["task_minutes"] is None

    def test_a_running_session_picks_up_the_studys_length(self):
        _login()
        _setup("TRAINING", task_minutes=5)
        pclient, _ = _open(_enrol("TRAINING")["sessions"][0]["token"])
        assert pclient.post("/api/study/session/begin-tutorial").json()["task_minutes"] == 5

    @pytest.mark.parametrize("bad", [0, -5, 601])
    def test_rejects_a_silly_length(self, bad):
        _login()
        r = client.put("/api/admin/study/PILOT/config", json={**CONFIG, "task_minutes": bad})
        assert r.status_code == 400

    def test_the_change_is_logged(self, caplog):
        _login()
        _setup("PILOT", task_minutes=30)
        with caplog.at_level("INFO", logger="elenchus.server"):
            _setup("PILOT", task_minutes=5)
        assert any(
            "task_minutes=5" in r.message and "(was 30)" in r.message for r in caplog.records
        )

    def test_it_is_in_the_export(self):
        _login()
        _setup("TRAINING", task_minutes=5)
        pclient, _ = _open(_enrol("TRAINING")["sessions"][0]["token"])
        archive = client.post("/api/admin/study/TRAINING/export").json()["archive"]
        with tarfile.open(archive) as tar:
            member = next(m for m in tar.getmembers() if m.name.endswith("study_config.json"))
            assert json.load(tar.extractfile(member))["task_minutes"] == 5


# ─── Downloading exports ─────────────────────────────────────────────


def _exported(study: str = "PILOT") -> str:
    _setup(study)
    _open(_enrol(study, name="Ada Lovelace")["sessions"][0]["token"])
    r = client.post(f"/api/admin/study/{study}/export")
    assert r.status_code == 200, r.text
    return os.path.basename(r.json()["archive"])


class TestExportDownload:
    def test_researcher_lists_and_downloads_the_archive(self, caplog):
        _login("researcher")
        name = _exported()
        (listed,) = client.get("/api/admin/study/PILOT/exports").json()["exports"]
        assert listed["name"] == name and listed["size_bytes"] > 0
        assert listed["pseudonym_file"] == name.replace(".tar.gz", ".pseudonyms.json")
        with caplog.at_level("INFO", logger="elenchus.server"):
            r = client.get(f"/api/admin/study/PILOT/exports/{name}")
        assert r.status_code == 200
        assert name in r.headers["content-disposition"]
        with tarfile.open(fileobj=io.BytesIO(r.content)) as tar:
            names = tar.getnames()
        assert any(n.endswith("manifest.json") for n in names)
        assert any("downloaded" in rec.message and name in rec.message for rec in caplog.records)

    def test_the_archive_holds_no_names(self):
        _login("researcher")
        name = _exported()
        body = client.get(f"/api/admin/study/PILOT/exports/{name}").content
        with tarfile.open(fileobj=io.BytesIO(body)) as tar:
            text = b"".join(
                tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()
            ).decode("utf-8", "replace")
        assert "Ada Lovelace" not in text

    def test_the_names_key_is_admin_only(self, caplog):
        _login("researcher")
        name = _exported()
        assert client.get(f"/api/admin/study/PILOT/exports/{name}/pseudonyms").status_code == 403
        admin = TestClient(app)
        _login("admin", admin)
        with caplog.at_level("WARNING", logger="elenchus.server"):
            r = admin.get(f"/api/admin/study/PILOT/exports/{name}/pseudonyms")
        assert r.status_code == 200
        assert "Ada Lovelace" in json.dumps(r.json())
        assert any("Pseudonym map downloaded" in rec.message for rec in caplog.records)

    def test_not_without_an_account(self):
        _login("researcher")
        name = _exported()
        anon = TestClient(app)
        assert anon.get("/api/admin/study/PILOT/exports").status_code == 401
        assert anon.get(f"/api/admin/study/PILOT/exports/{name}").status_code == 401
        user = TestClient(app)
        _login("user", user)
        assert user.get(f"/api/admin/study/PILOT/exports/{name}").status_code == 403

    def test_only_files_that_are_this_studys_exports(self):
        """The name is matched against the listing, never joined into a
        path — so nothing else on the server can be asked for."""
        _login("admin")
        name = _exported("PILOT")
        other = _exported("OTHER")
        for attempt in (
            "../platform.duckdb",
            "..%2Fplatform.duckdb",
            "platform.duckdb",
            name.replace(".tar.gz", ".pseudonyms.json"),  # the key isn't served as an archive
            other,  # another study's archive
        ):
            r = client.get(f"/api/admin/study/PILOT/exports/{attempt}")
            assert r.status_code == 404, attempt
        assert client.get(f"/api/admin/study/PILOT/exports/{other}/pseudonyms").status_code == 404
        assert client.get(f"/api/admin/study/OTHER/exports/{other}").status_code == 200

    def test_newest_first(self):
        import time

        _login("researcher")
        first = _exported()
        time.sleep(1.1)  # the file name's timestamp has one-second resolution
        second = os.path.basename(client.post("/api/admin/study/PILOT/export").json()["archive"])
        names = [e["name"] for e in client.get("/api/admin/study/PILOT/exports").json()["exports"]]
        assert names == [second, first]

    def test_a_study_with_no_exports(self):
        _login("researcher")
        assert client.get("/api/admin/study/NOTHING/exports").json() == {"exports": []}
