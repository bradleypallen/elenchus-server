"""The hard stop (Registered Report §2.4): the main task ends at the
study's task length whether or not FINISH was pressed, the last saved
draft is the text, and a study base is frozen once its session moves
on. The clock is the database's, so the tests backdate the moment the
session entered `active`.
"""

from __future__ import annotations

import contextlib
import logging

import pytest
from fastapi.testclient import TestClient

from elenchus import auth, study_text
from elenchus.db import get_registry
from elenchus.db import platform as pdb
from elenchus.server import app

client = TestClient(app)


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
    client.cookies.clear()
    yield
    client.cookies.clear()


def _con():
    return get_registry().platform_con()


def _at_task(condition: str = "elenchus", task_minutes: int = 5) -> tuple[TestClient, dict]:
    """A participant on the main task of a study with a short limit."""
    con = _con()
    rid = pdb.create_actor(
        con,
        kind="researcher",
        email="r@example.com",
        display_name="R",
        password_hash=auth.hash_password("pw"),
    )
    client.cookies.set(auth.SESSION_COOKIE, auth.create_session(rid))
    r = client.put(
        "/api/admin/study/HS/config",
        json={
            "topic_a_title": "A",
            "topic_b_title": "B",
            "min_gap_hours": 0,
            "task_minutes": task_minutes,
        },
    )
    assert r.status_code == 200, r.text
    r = client.post(
        "/api/admin/study/tokens",
        json={"study_id": "HS", "condition": condition, "display_name": "P"},
    )
    assert r.status_code == 200, r.text
    client.cookies.clear()
    p = TestClient(app)
    assert p.post(f"/api/study/{r.json()['token']}").status_code == 200
    assert p.post("/api/study/session/begin-tutorial").status_code == 200
    r = p.post("/api/study/session/begin-task")
    assert r.status_code == 200, r.text
    return p, r.json()


def _backdate(session_id: int, minutes: int) -> None:
    """Pretend the session entered its current state `minutes` ago."""
    with get_registry().platform_lock:
        _con().execute(
            "UPDATE sessions SET state_changed_at = state_changed_at - INTERVAL (?) MINUTE "
            "WHERE id = ?",
            [minutes, session_id],
        )


def _snapshots(base_id: str) -> list[dict]:
    return study_text.list_snapshots(get_registry().get(base_id).base.con)


class TestTheClockEndsTheTask:
    def test_the_session_page_ends_an_overdue_task(self, caplog):
        p, s = _at_task()
        assert (
            p.put(
                "/api/study/session/text", json={"content": "Tides rise.", "trigger": "autosave"}
            ).status_code
            == 200
        )
        _backdate(s["id"], 6)
        with caplog.at_level(logging.WARNING, logger="elenchus.server"):
            r = p.get("/api/study/session")
        body = r.json()
        assert r.status_code == 200 and body["state"] == "post_session"
        assert body["timed_out"] is True and body["text_submitted"] is True
        text = pdb.find_study_text_for_session(_con(), s["id"])
        assert text["content"] == "Tides rise." and text["active_elapsed_seconds"] == 300
        assert _snapshots(s["base_id"])[-1]["trigger"] == "timeout"
        assert "ended by the clock" in caplog.text
        assert text["submitted_by"] == "timeout"
        # A reload still says so (it is read from the record), and nothing
        # is submitted twice.
        again = p.get("/api/study/session").json()
        assert again["state"] == "post_session" and again["timed_out"] is True
        assert len([t for t in _snapshots(s["base_id"]) if t["trigger"] == "timeout"]) == 1

    def test_an_empty_text_is_submitted_and_shouted_about(self, caplog):
        p, s = _at_task()
        _backdate(s["id"], 6)
        with caplog.at_level(logging.WARNING, logger="elenchus.server"):
            body = p.get("/api/study/session").json()
        assert body["state"] == "post_session"
        assert pdb.find_study_text_for_session(_con(), s["id"])["content"] == ""
        assert "THE TEXT IS EMPTY" in caplog.text

    def test_before_the_limit_nothing_happens(self):
        p, s = _at_task()
        _backdate(s["id"], 4)
        body = p.get("/api/study/session").json()
        assert body["state"] == "active" and body["timed_out"] is False

    def test_the_tutorial_has_no_limit(self):
        con = _con()
        rid = pdb.create_actor(
            con,
            kind="researcher",
            email="r2@example.com",
            display_name="R",
            password_hash=auth.hash_password("pw"),
        )
        client.cookies.set(auth.SESSION_COOKIE, auth.create_session(rid))
        client.put(
            "/api/admin/study/HS2/config",
            json={
                "topic_a_title": "A",
                "topic_b_title": "B",
                "min_gap_hours": 0,
                "task_minutes": 5,
            },
        )
        r = client.post(
            "/api/admin/study/tokens",
            json={"study_id": "HS2", "condition": "elenchus", "display_name": "P"},
        )
        client.cookies.clear()
        q = TestClient(app)
        q.post(f"/api/study/{r.json()['token']}")
        s = q.post("/api/study/session/begin-tutorial").json()
        _backdate(s["id"], 60)
        assert q.get("/api/study/session").json()["state"] == "tutorial"

    def test_finish_after_the_limit_is_a_timeout(self):
        """What arrived late doesn't count; the last saved draft does."""
        p, s = _at_task()
        p.put("/api/study/session/text", json={"content": "Saved in time.", "trigger": "autosave"})
        _backdate(s["id"], 6)
        r = p.post("/api/study/session/finish", json={"content": "Typed after the bell."})
        assert r.status_code == 200 and r.json()["timed_out"] is True
        text = pdb.find_study_text_for_session(_con(), s["id"])
        assert text["content"] == "Saved in time." and text["submitted_by"] == "timeout"

    def test_saves_and_events_are_refused_after_the_limit(self):
        p, s = _at_task()
        _backdate(s["id"], 6)
        r = p.put("/api/study/session/text", json={"content": "late", "trigger": "autosave"})
        assert r.status_code == 409 and r.json()["detail"]["task_ended"] is True
        r = p.post(
            "/api/study/session/text/events", json={"events": [{"type": "paste", "length": 3}]}
        )
        assert r.status_code == 409
        # The session itself is still `active` until its page asks — and then it ends.
        assert p.get("/api/study/session").json()["state"] == "post_session"


class TestTheRecordIsClosed:
    def _message_status(self, p: TestClient, base: str) -> int:
        return p.post(f"/api/dialectics/{base}/message", json={"message": "hello"}).status_code

    def test_the_task_base_refuses_turns_after_the_limit(self):
        p, s = _at_task()
        _backdate(s["id"], 6)
        r = p.post(f"/api/dialectics/{s['base_id']}/message", json={"message": "hello"})
        assert r.status_code == 409 and r.json()["detail"]["task_ended"] is True
        assert r.json()["detail"]["user_message"].startswith("Time is up")

    def test_the_task_base_is_frozen_once_the_session_moves_on(self):
        p, s = _at_task()
        p.put("/api/study/session/text", json={"content": "Done.", "trigger": "autosave"})
        r = p.post("/api/study/session/finish", json={"content": "Done."})
        assert r.status_code == 200 and r.json()["timed_out"] is False
        assert pdb.find_study_text_for_session(_con(), s["id"])["submitted_by"] == "participant"
        base = s["base_id"]
        for path, body in (
            (f"/api/dialectics/{base}/message", {"message": "hello"}),
            (f"/api/dialectics/{base}/retract", {"proposition": "x"}),
            (f"/api/dialectics/{base}/tensions/1", {"action": "accept"}),
        ):
            r = p.post(path, json=body)
            assert r.status_code == 409, (path, r.text)
            assert "record is closed" in r.json()["detail"]["user_message"]

    def test_the_practice_base_is_frozen_once_the_task_starts(self):
        p, s = _at_task()
        r = p.post(f"/api/dialectics/practice-{s['id']}/message", json={"message": "hello"})
        assert r.status_code == 409 and "record is closed" in r.json()["detail"]["user_message"]

    def test_a_base_without_a_session_is_untouched(self):
        """Ordinary (non-study) dialectics have no clock and no freeze."""
        con = _con()
        uid = pdb.create_actor(
            con,
            kind="user",
            email="u@example.com",
            display_name="U",
            password_hash=auth.hash_password("pw"),
        )
        client.cookies.set(auth.SESSION_COOKIE, auth.create_session(uid))
        assert client.post("/api/dialectics", json={"name": "Free"}).status_code in (200, 201)
        r = client.post("/api/dialectics/Free/retract", json={"proposition": "nothing"})
        assert r.status_code != 409
