"""The admin Dialectics tab (docs/data-access.md, content_access.py).

An administrator gets a list of **metadata**, and — with a stated reason,
read-only, every fetch logged — the content of one ordinary dialectic at
a time: a view, a PDF, the raw records. Study records are refused. The
owner can always download their own records.
"""

from __future__ import annotations

import contextlib
import io
import json
import tarfile
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from elenchus import auth, content_access
from elenchus.db import get_registry
from elenchus.db import platform as pdb
from elenchus.server import app

REASON = {"category": "support_request", "reason": "Alice reported the reply never appeared"}
SECRET = "Fossils are not organisms"


@pytest.fixture(autouse=True)
def _clean():
    reg = get_registry()
    reg.migrate_platform()
    con = reg.platform_con()
    with reg.platform_lock:
        for table in (
            "content_access_log",
            "study_texts",
            "participant_session_tokens",
            "study_configs",
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


def _as(kind: str, email: str, name: str | None = None) -> tuple[TestClient, int]:
    actor_id = pdb.create_actor(
        _con(),
        kind=kind,
        email=email,
        display_name=name or email.split("@")[0],
        password_hash=auth.hash_password("pw"),
    )
    c = TestClient(app)
    c.cookies.set(auth.SESSION_COOKIE, auth.create_session(actor_id))
    return c, actor_id


def _alice_with_a_dialectic() -> tuple[TestClient, int]:
    alice, alice_id = _as("user", "alice@example.com", "Alice Example")
    assert alice.post("/api/sessions", json={"name": "alice notes"}).status_code == 200
    state = get_registry().get("alice notes")
    state.commit("An occurrence is an organism at a place and time")
    state.deny(SECRET)
    state.add_conversation("user", "I deny that fossils are organisms.")
    state.add_conversation("assistant", "Then is a fossil record not an occurrence?")
    return alice, alice_id


def _log_rows() -> list[tuple]:
    return (
        _con()
        .execute(
            "SELECT action, actor_id, base_id, category, grant_id FROM content_access_log ORDER BY id"
        )
        .fetchall()
    )


def _snapshot(name: str) -> dict:
    state = get_registry().get(name)
    return {**state.to_dict(), "conversation": state.get_conversation()}


def _study_task_base() -> tuple[TestClient, str, int]:
    researcher, _ = _as("researcher", "r@example.com")
    researcher.put(
        "/api/admin/study/AD/config",
        json={"topic_a_title": "A", "topic_b_title": "B", "min_gap_hours": 0, "task_minutes": 5},
    )
    token = researcher.post(
        "/api/admin/study/tokens",
        json={"study_id": "AD", "condition": "elenchus", "display_name": "Real Name"},
    ).json()["token"]
    p = TestClient(app)
    p.post(f"/api/study/{token}")
    p.post("/api/study/session/begin-tutorial")
    session = p.post("/api/study/session/begin-task").json()
    return p, pdb.find_study_session(_con(), session["id"])["base_id"], session["id"]


class TestTheListIsMetadata:
    def test_it_names_owners_and_carries_no_content(self):
        _alice_with_a_dialectic()
        admin, _ = _as("admin", "admin@example.com")
        body = admin.get("/api/admin/dialectics").json()
        (row,) = body["dialectics"]
        assert row["base_id"] == "alice notes" and row["kind"] == "ordinary" and row["viewable"]
        assert row["owner"]["display_name"] == "Alice Example"
        assert row["owner"]["email"] == "alice@example.com"
        assert row["mine"] is False and row["access"] is None
        assert row["created_at_utc"].endswith("Z")
        # Nothing people wrote, and nothing counted from it.
        blob = json.dumps(body)
        assert SECRET not in blob and "occurrence" not in blob
        assert not {"conversation", "commitments", "denials", "tensions"} & set(row)
        assert [c["value"] for c in body["categories"]] == list(content_access.CATEGORIES)
        assert body["grant_minutes"] == 30 and body["reason_min_chars"] == 10

    def test_listing_writes_nothing(self):
        _alice_with_a_dialectic()
        admin, _ = _as("admin", "admin@example.com")
        sessions = _con().execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        for _ in range(2):
            assert admin.get("/api/admin/dialectics").status_code == 200
            assert admin.get("/api/admin/access-log").status_code == 200
        assert _con().execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == sessions
        assert _log_rows() == []

    def test_study_records_are_listed_by_code_and_not_viewable(self):
        _study_task_base()
        admin, _ = _as("admin", "admin@example.com")
        rows = {r["base_id"]: r for r in admin.get("/api/admin/dialectics").json()["dialectics"]}
        kinds = sorted(r["kind"] for r in rows.values())
        assert kinds == ["study_practice", "study_task"]
        for r in rows.values():
            assert r["viewable"] is False and r["study"]["study_id"] == "AD"
            assert "Real Name" not in json.dumps(r)  # the code, never the enrolment name

    @pytest.mark.parametrize("kind", ["researcher", "user", "judge"])
    def test_only_admins(self, kind):
        _alice_with_a_dialectic()
        other, _ = _as(kind, f"{kind}@example.com")
        assert other.get("/api/admin/dialectics").status_code == 403
        assert other.get("/api/admin/access-log").status_code == 403
        for path in ("view", "report.pdf", "records"):
            r = other.post(
                f"/api/admin/dialectics/{path}", json={"base_id": "alice notes", **REASON}
            )
            assert r.status_code == 403, path
        assert _log_rows() == []


class TestAReasonBeforeAnyContent:
    @pytest.mark.parametrize(
        "body,fragment",
        [
            ({}, "Choose what this is for"),
            ({"category": "support_request"}, "Say why"),
            ({"category": "support_request", "reason": "help"}, "Say why"),
            ({"category": "curiosity", "reason": "I would like to have a look"}, "Choose what"),
            ({"category": "other", "reason": "x" * 501}, "under 500"),
        ],
    )
    def test_no_reason_no_content(self, body, fragment):
        _alice_with_a_dialectic()
        admin, _ = _as("admin", "admin@example.com")
        for path in ("view", "report.pdf", "records"):
            r = admin.post(
                f"/api/admin/dialectics/{path}", json={"base_id": "alice notes", **body}
            )
            assert r.status_code == 422, (path, r.status_code)
            assert fragment in r.json()["detail"]["user_message"]
            assert SECRET not in r.text
        assert _log_rows() == []

    def test_a_stray_field_is_refused(self):
        _alice_with_a_dialectic()
        admin, _ = _as("admin", "admin@example.com")
        r = admin.post(
            "/api/admin/dialectics/view", json={"base_id": "alice notes", "why": "x", **REASON}
        )
        assert r.status_code == 422 and _log_rows() == []

    def test_view_is_read_only_and_logged(self):
        _alice_with_a_dialectic()
        before = _snapshot("alice notes")
        admin, admin_id = _as("admin", "admin@example.com")
        r = admin.post("/api/admin/dialectics/view", json={"base_id": "alice notes", **REASON})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["read_only"] is True and body["mine"] is False
        assert body["owner"]["display_name"] == "Alice Example"
        assert SECRET in body["dialectic"]["denials"]
        assert [m["role"] for m in body["dialectic"]["conversation"]] == ["user", "assistant"]
        grant = body["grant"]
        assert (
            grant["category_label"] == "Owner asked for help"
            and grant["reason"] == REASON["reason"]
        )

        rows = _log_rows()
        assert [(a, who, base, cat) for a, who, base, cat, _ in rows] == [
            ("grant", admin_id, "alice notes", "support_request"),
            ("view", admin_id, "alice notes", "support_request"),
        ]
        assert rows[1][4] == grant["grant_id"]
        # Nothing about the dialectic changed, and nothing was planted on it.
        assert _snapshot("alice notes") == before
        assert (
            _con()
            .execute("SELECT COUNT(*) FROM sessions WHERE base_id = 'alice notes'")
            .fetchone()[0]
            == 1
        )

    def test_one_reason_covers_the_window_and_every_fetch_is_logged(self):
        _alice_with_a_dialectic()
        admin, _ = _as("admin", "admin@example.com")
        grant_id = admin.post(
            "/api/admin/dialectics/view", json={"base_id": "alice notes", **REASON}
        ).json()["grant"]["grant_id"]
        under = {"base_id": "alice notes", "grant_id": grant_id}

        assert admin.post("/api/admin/dialectics/view", json=under).status_code == 200
        pdf = admin.post("/api/admin/dialectics/report.pdf", json=under)
        assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
        assert pdf.headers["content-type"] == "application/pdf"
        records = admin.post("/api/admin/dialectics/records", json=under)
        assert records.status_code == 200

        actions = [a for a, *_ in _log_rows()]
        assert actions == ["grant", "view", "view", "pdf", "records"]
        assert {g for *_, g in _log_rows()[1:]} == {grant_id}

    def test_a_lapsed_or_borrowed_grant_is_refused(self):
        _alice_with_a_dialectic()
        alice2, _ = _as("user", "carol@example.com")
        alice2.post("/api/sessions", json={"name": "carol notes"})
        admin, _ = _as("admin", "admin@example.com")
        other_admin, _ = _as("admin", "admin2@example.com")
        grant_id = admin.post(
            "/api/admin/dialectics/view", json={"base_id": "alice notes", **REASON}
        ).json()["grant"]["grant_id"]

        # Someone else's grant; this grant on another dialectic.
        r = other_admin.post(
            "/api/admin/dialectics/view", json={"base_id": "alice notes", "grant_id": grant_id}
        )
        assert r.status_code == 403 and r.json()["detail"]["grant_expired"] is True
        r = admin.post(
            "/api/admin/dialectics/view", json={"base_id": "carol notes", "grant_id": grant_id}
        )
        assert r.status_code == 403

        # Past its window.
        with get_registry().platform_lock:
            _con().execute(
                "UPDATE content_access_log SET expires_at_utc = ? WHERE id = ?",
                [content_access.now_utc() - timedelta(minutes=1), grant_id],
            )
        r = admin.post(
            "/api/admin/dialectics/records", json={"base_id": "alice notes", "grant_id": grant_id}
        )
        assert r.status_code == 403 and "give it again" in r.json()["detail"]["user_message"]
        assert [a for a, *_ in _log_rows()] == ["grant", "view"]

    def test_your_own_dialectic_needs_no_reason_and_leaves_no_record(self):
        admin, _ = _as("admin", "admin@example.com")
        admin.post("/api/sessions", json={"name": "admin notes"})
        r = admin.post("/api/admin/dialectics/view", json={"base_id": "admin notes"})
        assert r.status_code == 200 and r.json()["mine"] is True and r.json()["grant"] is None
        assert _log_rows() == []

    def test_a_missing_dialectic_is_a_plain_404(self):
        admin, _ = _as("admin", "admin@example.com")
        r = admin.post("/api/admin/dialectics/view", json={"base_id": "nope", **REASON})
        assert r.status_code == 404 and _log_rows() == []


class TestStudyRecordsAreRefused:
    def test_no_view_pdf_or_records_of_a_study_base(self):
        p, task_base, session_id = _study_task_base()
        admin, _ = _as("admin", "admin@example.com")
        for base in (task_base, f"practice-{session_id}"):
            for path in ("view", "report.pdf", "records"):
                r = admin.post(f"/api/admin/dialectics/{path}", json={"base_id": base, **REASON})
                assert r.status_code == 409, (base, path, r.status_code)
                assert r.json()["detail"]["study_record"] is True
                assert "study export" in r.json()["detail"]["user_message"]
        assert _log_rows() == []

    def test_a_participant_cannot_download_their_study_record(self):
        p, task_base, _ = _study_task_base()
        r = p.get(f"/api/dialectics/{task_base}/records")
        assert r.status_code == 409 and r.json()["detail"]["study_record"] is True


class TestTheRecords:
    def _members(self, data: bytes) -> dict[str, bytes]:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            return {
                m.name.split("/", 1)[1]: tar.extractfile(m).read()
                for m in tar.getmembers()
                if m.isfile()
            }

    def test_the_owner_downloads_their_own(self):
        alice, _ = _alice_with_a_dialectic()
        r = alice.get("/api/dialectics/alice notes/records")
        assert r.status_code == 200 and r.headers["content-type"] == "application/gzip"
        assert 'filename="dialectic-alice_notes-' in r.headers["content-disposition"]
        files = self._members(r.content)
        assert {
            "manifest.json",
            "state.json",
            "transcript.json",
            "turn_log.json",
            "state_events.json",
            "integrity.json",
        } <= set(files)
        assert any(name.startswith("base/") for name in files)
        manifest = json.loads(files["manifest.json"])
        assert (
            manifest["format"] == "elenchus-dialectic-records"
            and manifest["exported_by"] == "owner"
        )
        assert manifest["owner"] == "OWNER" and manifest["versions"]["platform_schema"] >= 23
        assert SECRET in json.loads(files["state.json"])["denials"]
        events = json.loads(files["state_events.json"])
        assert len(events) >= 2 and {e["outcome"] for e in events} == {"applied"}
        # Who it belongs to is not in the archive.
        text = b"".join(files[n] for n in ("manifest.json", "state_events.json", "integrity.json"))
        assert b"alice@example.com" not in text and b"Alice Example" not in text
        assert _log_rows() == []  # your own data: no gate, no record

        sid = alice.get("/api/sessions").json()[0]["session_id"]
        assert alice.get(f"/api/sessions/{sid}/records").status_code == 200

    def test_nobody_else_gets_them_through_the_owners_route(self):
        alice, _ = _alice_with_a_dialectic()
        for kind in ("admin", "researcher", "user"):
            other, _ = _as(kind, f"{kind}@example.com")
            assert other.get("/api/dialectics/alice notes/records").status_code == 404

    def test_an_admin_gets_the_same_archive_under_a_reason(self):
        _alice_with_a_dialectic()
        admin, admin_id = _as("admin", "admin@example.com")
        r = admin.post(
            "/api/admin/dialectics/records",
            json={
                "base_id": "alice notes",
                "category": "owner_agreed_analysis",
                "reason": "Alice agreed to share this one for the pilot analysis",
            },
        )
        assert r.status_code == 200
        files = self._members(r.content)
        assert json.loads(files["manifest.json"])["exported_by"] == "administrator"
        assert len(json.loads(files["transcript.json"])) == 2
        assert [(a, c) for a, _, _, c, _ in _log_rows()] == [
            ("grant", "owner_agreed_analysis"),
            ("records", "owner_agreed_analysis"),
        ]


class TestTheLog:
    def test_it_reads_on_its_own(self):
        _alice_with_a_dialectic()
        admin, _ = _as("admin", "admin@example.com", "Ada Admin")
        admin.post("/api/admin/dialectics/view", json={"base_id": "alice notes", **REASON})
        (entry,) = admin.get("/api/admin/access-log").json()["entries"]  # fetches, not grants
        assert entry["action"] == "view" and entry["actor_name"] == "Ada Admin"
        assert entry["owner_name"] == "Alice Example" and entry["base_name"] == "alice notes"
        assert (
            entry["category_label"] == "Owner asked for help"
            and entry["reason"] == REASON["reason"]
        )
        assert entry["at_utc"].endswith("Z")

        row = admin.get("/api/admin/dialectics").json()["dialectics"][0]
        assert row["access"]["count"] == 1 and row["access"]["last_by"] == "Ada Admin"

    def test_the_log_outlives_the_dialectic(self):
        alice, _ = _alice_with_a_dialectic()
        admin, _ = _as("admin", "admin@example.com")
        admin.post("/api/admin/dialectics/view", json={"base_id": "alice notes", **REASON})
        assert alice.delete("/api/dialectics/alice notes").status_code == 200
        (entry,) = admin.get("/api/admin/access-log").json()["entries"]
        assert entry["base_id"] == "alice notes" and entry["base_exists"] is False
