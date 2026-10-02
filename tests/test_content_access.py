"""Who may see or change a dialectic (docs/data-access.md).

Only its owner — on every working route, for an admin too. Until 0.9.5
an admin bypassed the ownership check everywhere (read, write, delete),
and the admin home list opened a session of the admin's own on every
base in the platform; on a study task base that row stood in for the
participant's study session, so an archived base read as open and took
edits again. These tests pin the policy and that regression.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from elenchus import auth
from elenchus.db import get_registry
from elenchus.db import platform as pdb
from elenchus.server import app

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "src/elenchus/migrations/platform/0022_owner_only_sessions.sql"
)


@pytest.fixture(autouse=True)
def _clean():
    reg = get_registry()
    reg.migrate_platform()
    con = reg.platform_con()
    with reg.platform_lock:
        for table in (
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


def _as(kind: str, email: str) -> tuple[TestClient, int]:
    """A signed-in client of the given kind, and its actor id."""
    actor_id = pdb.create_actor(
        _con(),
        kind=kind,
        email=email,
        display_name=email.split("@")[0],
        password_hash=auth.hash_password("pw"),
    )
    c = TestClient(app)
    c.cookies.set(auth.SESSION_COOKIE, auth.create_session(actor_id))
    return c, actor_id


def _session_rows(base: str) -> list[tuple]:
    return (
        _con()
        .execute(
            "SELECT actor_id, study_token IS NOT NULL FROM sessions WHERE base_id = ? ORDER BY id",
            [base],
        )
        .fetchall()
    )


def _at_task() -> tuple[TestClient, dict, str]:
    """A participant on the main task: (their client, the session, the task base)."""
    researcher, _ = _as("researcher", "r@example.com")
    r = researcher.put(
        "/api/admin/study/CA/config",
        json={"topic_a_title": "A", "topic_b_title": "B", "min_gap_hours": 0, "task_minutes": 5},
    )
    assert r.status_code == 200, r.text
    r = researcher.post(
        "/api/admin/study/tokens",
        json={"study_id": "CA", "condition": "elenchus", "display_name": "P"},
    )
    assert r.status_code == 200, r.text
    p = TestClient(app)
    assert p.post(f"/api/study/{r.json()['token']}").status_code == 200
    assert p.post("/api/study/session/begin-tutorial").status_code == 200
    r = p.post("/api/study/session/begin-task")
    assert r.status_code == 200, r.text
    session = r.json()
    base = pdb.find_study_session(_con(), session["id"])["base_id"]
    return p, session, base


NON_OWNERS = [
    ("admin", "admin@example.com"),
    ("researcher", "res@example.com"),
    ("user", "bob@example.com"),
]


class TestOnlyTheOwner:
    @pytest.mark.parametrize("kind,email", NON_OWNERS)
    def test_nobody_else_reads_writes_or_deletes(self, kind, email):
        alice, _ = _as("user", "alice@example.com")
        created = alice.post("/api/sessions", json={"name": "alice-notes"})
        assert created.status_code == 200, created.text
        sid = created.json()["session_id"]
        other, _ = _as(kind, email)

        attempts = [
            ("GET", "/api/dialectics/alice-notes", None),
            ("GET", "/api/dialectics/alice-notes/report", None),
            ("GET", "/api/dialectics/alice-notes/report.pdf", None),
            ("POST", "/api/dialectics/alice-notes/message", {"message": "let me in"}),
            ("POST", "/api/dialectics/alice-notes/retract", {"proposition": "x"}),
            ("POST", "/api/dialectics/alice-notes/tensions/1", {"action": "accept"}),
            ("DELETE", "/api/dialectics/alice-notes", None),
            ("GET", f"/api/sessions/{sid}", None),
            ("POST", f"/api/sessions/{sid}/message", {"message": "let me in"}),
            ("POST", f"/api/sessions/{sid}/retract", {"proposition": "x"}),
            ("GET", f"/api/sessions/{sid}/report.pdf", None),
            ("DELETE", f"/api/sessions/{sid}", None),
        ]
        for method, path, body in attempts:
            r = other.request(method, path, json=body)
            # 404, not 403: saying the name exists but is someone else's is a leak.
            assert r.status_code == 404, (
                f"{kind} {method} {path} -> {r.status_code} {r.text[:120]}"
            )

        # …and the dialectic is untouched and still its owner's.
        assert alice.get("/api/dialectics/alice-notes").status_code == 200
        assert (
            alice.post(
                "/api/dialectics/alice-notes/retract", json={"proposition": "x"}
            ).status_code
            == 200
        )
        assert pdb.find_session(_con(), sid)["status"] == "open"
        assert alice.delete(f"/api/sessions/{sid}").status_code == 200

    def test_an_admins_lists_hold_their_own_dialectics_only(self):
        alice, _ = _as("user", "alice@example.com")
        alice.post("/api/sessions", json={"name": "alice-notes"})
        admin, admin_id = _as("admin", "admin@example.com")
        admin.post("/api/sessions", json={"name": "admin-notes"})

        for path in ("/api/sessions", "/api/dialectics"):
            names = [d["name"] for d in admin.get(path).json()]
            assert names == ["admin-notes"], (path, names)

    def test_listing_creates_nothing_for_anyone_elses_dialectic(self):
        """A GET that wrote: the old admin list opened a session of the
        admin's own on every base it showed."""
        alice, alice_id = _as("user", "alice@example.com")
        alice.post("/api/sessions", json={"name": "alice-notes"})
        _, _, task_base = _at_task()
        before = _con().execute("SELECT COUNT(*) FROM sessions").fetchone()[0]

        admin, admin_id = _as("admin", "admin@example.com")
        for _ in range(2):
            assert admin.get("/api/sessions").status_code == 200
            assert admin.get("/api/dialectics").status_code == 200

        assert _con().execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == before
        assert _session_rows("alice-notes") == [(alice_id, False)]
        assert [is_study for _, is_study in _session_rows(task_base)] == [True]


class TestStudyRecordsStayFrozen:
    def test_an_admin_visit_does_not_reopen_a_submitted_base(self):
        p, session, base = _at_task()
        admin, _ = _as("admin", "admin@example.com")
        admin.get("/api/sessions")  # used to plant an 'active' session row on the task base
        assert p.post("/api/study/session/finish", json={"content": "Tides."}).status_code == 200

        r = p.post(f"/api/dialectics/{base}/retract", json={"proposition": "x"})
        assert r.status_code == 409 and r.json()["detail"]["task_ended"] is True
        assert (
            admin.post(f"/api/dialectics/{base}/retract", json={"proposition": "x"}).status_code
            == 404
        )

    def test_a_stray_session_row_cannot_stand_in_for_the_study_session(self):
        """Data from before the fix: the study session is found by its
        token, whatever else sits on the base."""
        p, session, base = _at_task()
        admin, admin_id = _as("admin", "admin@example.com")
        with get_registry().platform_lock:
            stray = pdb.create_session(_con(), actor_id=admin_id, base_id=base)
        assert stray > session["id"]

        found = pdb.find_session_by_base(_con(), base)
        assert found["id"] == session["id"] and found["study_token"]

        assert p.post("/api/study/session/finish", json={"content": "Tides."}).status_code == 200
        assert (
            p.post(f"/api/dialectics/{base}/retract", json={"proposition": "x"}).status_code == 409
        )
        assert (
            admin.post(f"/api/dialectics/{base}/retract", json={"proposition": "x"}).status_code
            == 404
        )

    def test_the_participant_still_works_on_their_own_task(self):
        p, session, base = _at_task()
        admin, _ = _as("admin", "admin@example.com")
        admin.get("/api/sessions")
        assert (
            p.post(f"/api/dialectics/{base}/retract", json={"proposition": "x"}).status_code == 200
        )
        assert p.put("/api/study/session/text", json={"content": "Tides rise."}).status_code == 200

    def test_a_study_record_is_never_deleted_through_the_api(self):
        p, session, base = _at_task()
        practice = f"practice-{session['id']}"
        for path in (
            f"/api/dialectics/{base}",
            f"/api/dialectics/{practice}",
            f"/api/sessions/{session['id']}",
        ):
            r = p.delete(path)
            assert r.status_code == 409, f"{path} -> {r.status_code} {r.text[:120]}"
            assert "can't be deleted" in r.json()["detail"]["user_message"]
        reg = get_registry()
        assert pdb.find_base(_con(), base) and pdb.find_base(_con(), practice)
        assert Path(reg.db_path(base)).exists() and Path(reg.db_path(practice)).exists()
        assert pdb.find_study_session(_con(), session["id"])["status"] == "open"

    def test_an_ordinary_dialectic_called_practice_n_is_not_a_study_record(self):
        """`practice-{id}` is a study base only when it belongs to that
        session's own participant."""
        researcher, _ = _as("researcher", "r@example.com")
        researcher.put(
            "/api/admin/study/CA/config",
            json={"topic_a_title": "A", "topic_b_title": "B", "min_gap_hours": 0},
        )
        token = researcher.post(
            "/api/admin/study/tokens",
            json={"study_id": "CA", "condition": "baseline", "display_name": "P"},
        ).json()["token"]
        p = TestClient(app)
        # A session still in briefing: its practice base doesn't exist yet.
        assert p.post(f"/api/study/{token}").status_code == 200
        session = p.get("/api/study/session").json()
        assert session["state"] == "briefing"
        session_id = session["id"]

        alice, _ = _as("user", "alice@example.com")
        name = f"practice-{session_id}"
        assert alice.post("/api/dialectics", json={"name": name}).status_code == 200
        assert (
            alice.post(f"/api/dialectics/{name}/retract", json={"proposition": "x"}).status_code
            == 200
        )
        assert alice.delete(f"/api/dialectics/{name}").status_code == 200


class TestCleanupMigration:
    def test_it_removes_other_peoples_sessions_and_nothing_else(self):
        con = _con()
        _, owner = _as("user", "owner@example.com")
        _, admin = _as("admin", "admin@example.com")
        _, participant = _as("user", "p@example.com")
        with get_registry().platform_lock:
            pdb.create_base(con, base_id="mine", name="mine", owner_id=owner)
            own = pdb.create_session(con, actor_id=owner, base_id="mine")
            pdb.create_session(con, actor_id=admin, base_id="mine")  # stray
            pdb.create_base(con, base_id="task-x", name="t", owner_id=participant)
            study = pdb.create_session(con, actor_id=participant, base_id="task-x")
            con.execute(
                "UPDATE sessions SET study_token = 'tok', state = 'post_session' WHERE id = ?",
                [study],
            )
            pdb.create_session(con, actor_id=admin, base_id="task-x")  # stray, state 'active'
            # A session whose base row is gone is left alone (nothing to compare it to).
            orphan = pdb.create_session(con, actor_id=admin, base_id="gone")
            con.execute(MIGRATION.read_text())

        ids = {r[0] for r in con.execute("SELECT id FROM sessions").fetchall()}
        assert ids == {own, study, orphan}
        assert pdb.find_session_by_base(con, "task-x")["id"] == study
