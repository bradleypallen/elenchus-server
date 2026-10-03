"""The notice, the research-use choice, owner-side access notes and the
research export (docs/data-access.md, policy version 5; notice.py,
research_export.py; platform migration 0026).

Three things are guarded here: nobody opens an account without the
notice; the research-use choice defaults to no, is recorded each way,
and bounds the research export exactly; and an owner can see when an
administrator looked at their dialectic — but not the sentence given.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tarfile

import pytest
from fastapi.testclient import TestClient

from elenchus import auth, notice, research_export
from elenchus.db import get_registry
from elenchus.db import platform as pdb
from elenchus.server import app

REASON = {"category": "support_request", "reason": "Alice reported the reply never appeared"}


@pytest.fixture(autouse=True)
def _clean():
    reg = get_registry()
    reg.migrate_platform()
    con = reg.platform_con()
    with reg.platform_lock:
        for table in (
            "consent_events",
            "content_access_log",
            "study_texts",
            "participant_session_tokens",
            "study_participants",
            "study_configs",
            "invites",
            "usage",
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
    yield


def _con():
    return get_registry().platform_con()


def _as(kind: str, email: str, *, accepted: bool = True) -> tuple[TestClient, int]:
    actor_id = pdb.create_actor(
        _con(),
        kind=kind,
        email=email,
        display_name=email.split("@")[0].title(),
        password_hash=auth.hash_password("pw"),
    )
    if accepted:
        with get_registry().platform_lock:
            pdb.record_terms_acceptance(_con(), actor_id, notice.NOTICE_VERSION)
    c = TestClient(app)
    c.cookies.set(auth.SESSION_COOKIE, auth.create_session(actor_id))
    return c, actor_id


def _dialectic(client: TestClient, name: str, line: str) -> None:
    assert client.post("/api/sessions", json={"name": name}).status_code == 200
    state = get_registry().get(name)
    state.commit(line)
    state.add_conversation("user", f"I hold that {line.lower()}")


def _events(actor_id: int) -> list[tuple[str, str]]:
    return [(e["kind"], e["notice_version"]) for e in pdb.list_consent_events(_con(), actor_id)]


class TestTheNotice:
    def test_it_is_public_versioned_and_keeps_its_promises(self):
        body = TestClient(app).get("/api/notice").json()
        assert body["version"] == notice.NOTICE_VERSION
        assert body["policy_url"].startswith("https://")
        assert "\n\n".join(body["paragraphs"]) == body["text"]
        for promise in notice.PROMISES:
            assert promise in notice.NOTICE_TEXT, promise

    def test_no_account_without_it(self):
        admin, _ = _as("admin", "admin@example.com")
        token = admin.post("/api/admin/invites", json={"role": "user"}).json()["token"]
        anon = TestClient(app)
        body = {
            "token": token,
            "display_name": "New User",
            "password": "a-long-password",
            "email_override": "new@example.com",
        }
        r = anon.post("/api/auth/signup", json=body)
        assert r.status_code == 422 and "notice" in r.json()["detail"]["user_message"]
        assert pdb.find_actor_by_email(_con(), "new@example.com") is None
        r = anon.post("/api/auth/signup", json={**body, "accept_terms": True})
        assert r.status_code == 200, r.text
        me = anon.get("/api/auth/me").json()
        assert me["terms_version"] == notice.NOTICE_VERSION and me["terms_current"] is True
        assert me["research_use"] is False  # the default is no
        assert _events(me["id"]) == [
            ("terms_accepted", notice.NOTICE_VERSION),
            ("research_use_off", notice.NOTICE_VERSION),
        ]

    def test_saying_yes_on_the_form_is_recorded(self):
        admin, _ = _as("admin", "admin@example.com")
        token = admin.post("/api/admin/invites", json={"role": "user"}).json()["token"]
        anon = TestClient(app)
        r = anon.post(
            "/api/auth/signup",
            json={
                "token": token,
                "display_name": "Keen",
                "password": "a-long-password",
                "email_override": "keen@example.com",
                "accept_terms": True,
                "research_use": True,
            },
        )
        assert r.status_code == 200 and anon.get("/api/auth/me").json()["research_use"] is True

    def test_an_existing_account_is_gated_until_it_accepts(self):
        old, old_id = _as("user", "old@example.com", accepted=False)
        me = old.get("/api/auth/me").json()
        assert me["terms_version"] is None and me["terms_current"] is False
        r = old.post("/api/auth/accept-terms", json={"research_use": True})
        assert r.status_code == 200
        me = old.get("/api/auth/me").json()
        assert me["terms_current"] is True and me["research_use"] is True
        assert _events(old_id) == [
            ("terms_accepted", notice.NOTICE_VERSION),
            ("research_use_on", notice.NOTICE_VERSION),
        ]

    def test_a_rewording_shows_again(self, monkeypatch):
        user, _ = _as("user", "u@example.com")
        assert user.get("/api/auth/me").json()["terms_current"] is True
        monkeypatch.setattr(notice, "NOTICE_VERSION", "2")
        assert user.get("/api/auth/me").json()["terms_current"] is False

    def test_the_choice_can_change_either_way_and_is_recorded(self):
        user, uid = _as("user", "u@example.com")
        assert user.put("/api/auth/research-use", json={"research_use": True}).status_code == 200
        assert user.get("/api/auth/me").json()["research_use"] is True
        assert user.put("/api/auth/research-use", json={"research_use": False}).status_code == 200
        assert user.get("/api/auth/me").json()["research_use"] is False
        assert [k for k, _v in _events(uid)] == [
            "terms_accepted",
            "research_use_on",
            "research_use_off",
        ]

    def test_participants_are_never_asked(self):
        researcher, _ = _as("researcher", "r@example.com")
        researcher.put(
            "/api/admin/study/N/config",
            json={"topic_a_title": "A", "topic_b_title": "B", "min_gap_hours": 0},
        )
        token = researcher.post(
            "/api/admin/study/tokens",
            json={"study_id": "N", "condition": "elenchus", "display_name": "P"},
        ).json()["token"]
        p = TestClient(app)
        p.post(f"/api/study/{token}")
        me = p.get("/api/auth/me").json()
        assert me["kind"] == "participant" and me["terms_current"] is True
        assert p.post("/api/auth/accept-terms", json={"research_use": True}).status_code == 400
        assert p.put("/api/auth/research-use", json={"research_use": True}).status_code == 400


class TestOwnerAccessNotes:
    def test_the_owner_sees_when_who_and_what_for_but_not_the_sentence(self):
        alice, _ = _as("user", "alice@example.com")
        _dialectic(alice, "alice notes", "Tides follow the moon")
        assert alice.get("/api/dialectics").json()[0]["looked_at"] is None
        admin, _ = _as("admin", "admin@example.com")
        r = admin.post("/api/admin/dialectics/view", json={"base_id": "alice notes", **REASON})
        assert r.status_code == 200
        admin.post(
            "/api/admin/dialectics/report.pdf",
            json={"base_id": "alice notes", "grant_id": r.json()["grant"]["grant_id"]},
        )
        rows = alice.get("/api/dialectics").json()
        assert rows[0]["looked_at"]["count"] == 2 and rows[0]["looked_at"]["last_at_utc"]
        sessions = alice.get("/api/sessions").json()
        assert sessions[0]["looked_at"]["count"] == 2
        notes = alice.get("/api/dialectics/alice notes/access").json()
        assert [e["action"] for e in notes["entries"]] == ["pdf", "view"]
        assert notes["entries"][0]["by"] == "Admin"
        assert notes["entries"][0]["category_label"] == "Owner asked for help"
        assert "Alice reported" not in json.dumps(notes)  # the sentence stays with the admins
        sid = sessions[0]["session_id"]
        assert alice.get(f"/api/sessions/{sid}/access").json()["entries"] == notes["entries"]

    def test_nobody_else_reads_them(self):
        alice, _ = _as("user", "alice@example.com")
        _dialectic(alice, "alice notes", "Tides follow the moon")
        for kind in ("admin", "researcher", "user"):
            other, _ = _as(kind, f"{kind}@example.com")
            assert other.get("/api/dialectics/alice notes/access").status_code == 404


class TestTheResearchExport:
    def _members(self, data: bytes) -> dict[str, bytes]:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            return {m.name: tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()}

    def test_only_those_who_said_yes_and_only_their_ordinary_dialectics(self, tmp_path):
        yes, yes_id = _as("user", "yes@example.com")
        no, _ = _as("user", "no@example.com")
        _dialectic(yes, "yes notes", "Spring tides follow a new moon")
        _dialectic(no, "no notes", "Neap tides are the weakest")
        yes.put("/api/auth/research-use", json={"research_use": True})
        admin, admin_id = _as("admin", "admin@example.com")
        body = admin.get("/api/admin/research-exports").json()
        assert body["opted_in"] == {"accounts": 1, "dialectics": 1} and body["exports"] == []
        r = admin.post("/api/admin/research-export")
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["accounts"] == 1 and out["dialectics"] == 1 and out["skipped"] == []
        name = out["name"]
        assert name.startswith("research-") and name.endswith(".tar.gz")
        data = admin.get(f"/api/admin/research-exports/{name}").content
        members = self._members(data)
        blob = b"".join(members.values())
        assert b"Spring tides" in blob and b"Neap tides" not in blob
        assert (
            b"yes@example.com" not in blob
            and b"Yes"
            not in json.dumps(json.loads(members[f"{name[:-7]}/manifest.json"])).encode()
        )
        manifest = json.loads(members[f"{name[:-7]}/manifest.json"])
        assert manifest["dialectics"] == [{"dialectic": "yes notes", "owner": "U-001"}]
        assert manifest["accounts"][0]["notice_version_accepted"] == notice.NOTICE_VERSION
        # The key is a separate, admin-only file.
        key = admin.get(f"/api/admin/research-exports/{name}/pseudonyms").json()
        assert key[str(yes_id)]["pseudonym"] == "U-001"
        assert key[str(yes_id)]["email"] == "yes@example.com"
        # The owner can see that it happened, as a records fetch under the agreement.
        notes = yes.get("/api/dialectics/yes notes/access").json()["entries"]
        assert notes[0]["action"] == "records"
        assert notes[0]["category"] == "owner_agreed_analysis" and notes[0]["by"] == "Admin"
        assert no.get("/api/dialectics/no notes/access").json()["entries"] == []

    def test_withdrawing_takes_you_out_of_the_next_export(self):
        yes, _ = _as("user", "yes@example.com")
        _dialectic(yes, "yes notes", "Spring tides follow a new moon")
        yes.put("/api/auth/research-use", json={"research_use": True})
        admin, _ = _as("admin", "admin@example.com")
        assert admin.post("/api/admin/research-export").status_code == 200
        yes.put("/api/auth/research-use", json={"research_use": False})
        assert admin.get("/api/admin/research-exports").json()["opted_in"] == {
            "accounts": 0,
            "dialectics": 0,
        }
        r = admin.post("/api/admin/research-export")
        assert r.status_code == 409 and "Nobody has agreed" in r.json()["detail"]["user_message"]

    def test_study_records_stay_out_even_if_the_owner_said_yes(self):
        # A study participant's actor can't say yes (participants are never
        # asked); the guard matters for a staff account that owns a study
        # record, so flip the flag directly.
        researcher, rid = _as("researcher", "r@example.com")
        researcher.put(
            "/api/admin/study/N/config",
            json={"topic_a_title": "A", "topic_b_title": "B", "min_gap_hours": 0},
        )
        token = researcher.post(
            "/api/admin/study/tokens",
            json={"study_id": "N", "condition": "elenchus", "display_name": "P"},
        ).json()["token"]
        p = TestClient(app)
        p.post(f"/api/study/{token}")
        p.post("/api/study/session/begin-tutorial")
        session = p.post("/api/study/session/begin-task").json()
        task_base = pdb.find_study_session(_con(), session["id"])["base_id"]
        participant_id = pdb.find_base(_con(), task_base)["owner_id"]
        with get_registry().platform_lock:
            pdb.set_research_use(_con(), participant_id, True, notice.NOTICE_VERSION)
        assert research_export.opted_in(_con()) == {"accounts": 1, "dialectics": 0}

    @pytest.mark.parametrize("kind", ["researcher", "user", "judge"])
    def test_admin_only(self, kind):
        other, _ = _as(kind, f"{kind}@example.com")
        assert other.get("/api/admin/research-exports").status_code == 403
        assert other.post("/api/admin/research-export").status_code == 403
        assert other.get("/api/admin/research-exports/research-x.tar.gz").status_code == 403

    def test_a_name_is_matched_never_joined(self):
        admin, _ = _as("admin", "admin@example.com")
        r = admin.get("/api/admin/research-exports/..%2F..%2Fplatform.duckdb")
        assert r.status_code == 404
        exports_dir = research_export.exports_dir(os.path.dirname(get_registry().platform_path))
        assert not os.path.exists(os.path.join(exports_dir, "..", "..", "nothing"))
