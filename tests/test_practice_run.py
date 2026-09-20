"""The Study Runbook's practice run, as a test.

`docs/study-runbook.md` promises that someone with an **admin account and
nothing else** — no shell on the server, nobody to ask — can do a complete
run-through: set up a practice study with a five-minute task, enrol,
watch a second link refuse to open early, do both sessions (reload,
close and reopen mid-task), close a drop-out, create a judge and have
them rate and revise, export, download the archive and find their own
data in it, take a backup, and see what it cost.

This walks exactly those steps. If a step here has to change, the
runbook's practice run has to change with it.
"""

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


@pytest.fixture(autouse=True)
def _clean():
    reg = get_registry()
    reg.migrate_platform()
    con = reg.platform_con()
    with reg.platform_lock:
        for table in (
            "text_ratings",
            "text_assignments",
            "study_participants",
            "study_configs",
            "study_texts",
            "survey_responses",
            "participant_session_tokens",
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
    exports = os.path.join(os.path.dirname(reg.platform_path), "exports")
    if os.path.isdir(exports):
        for name in os.listdir(exports):
            with contextlib.suppress(OSError):
                os.remove(os.path.join(exports, name))
    yield


def _ok(response, *codes):
    assert response.status_code in (codes or (200,)), (
        f"{response.request.method} {response.request.url} → "
        f"{response.status_code} {response.text[:300]}"
    )
    return response.json()


def test_the_runbooks_practice_run_needs_only_an_admin_account():
    con = get_registry().platform_con()
    admin_id = pdb.create_actor(
        con,
        kind="admin",
        email="admin@example.org",
        display_name="Practice Admin",
        password_hash=auth.hash_password("not-used"),
    )
    admin = TestClient(app)
    admin.cookies.set(auth.SESSION_COOKIE, auth.create_session(admin_id))

    # 1. TRAINING: two topics, no gap, a five-minute task — from the Study tab.
    config = _ok(
        admin.put(
            "/api/admin/study/TRAINING/config",
            json={
                "topic_a_title": "Kinds of tides",
                "topic_a_brief": "Spring, neap, diurnal.",
                "topic_b_title": "Kinds of clouds",
                "topic_b_brief": "Cumulus, stratus, cirrus.",
                "min_gap_hours": 0,
                "task_minutes": 5,
            },
        )
    )
    assert config["task_minutes"] == 5

    # 2. Enrol; session 2 is waiting on session 1.
    person = _ok(
        admin.post(
            "/api/admin/study/TRAINING/participants", json={"display_name": "Practice Person"}
        )
    )
    first, second = person["sessions"]
    roster = _ok(admin.get("/api/admin/study/TRAINING/participants"))["participants"][0]
    assert roster["sessions"][1]["gate"]["reason"] == "first_not_started"

    # 3. Session 2's link, too early: refused, with a message for the participant.
    early = TestClient(app).post(f"/api/study/{second['token']}")
    assert early.status_code == 409 and early.json()["detail"]["user_message"]

    # 4–6. Session 1: tutorial, task on the short clock; the text survives a
    # reload and closing the window.
    window = TestClient(app)
    _ok(window.post(f"/api/study/{first['token']}"))
    _ok(window.post("/api/study/session/begin-tutorial"))
    task = _ok(window.post("/api/study/session/begin-task"))
    assert task["task_minutes"] == 5 and task["soft_warning_minutes"] == [1, 5]
    _ok(window.put("/api/study/session/text", json={"content": "Tides rise and fall."}))
    assert "Tides rise" in _ok(window.get("/api/study/session/text"))["content"]
    reopened = TestClient(app)
    _ok(reopened.post(f"/api/study/{first['token']}"))
    assert _ok(reopened.get("/api/study/session"))["state"] == "active"
    assert "Tides rise" in _ok(reopened.get("/api/study/session/text"))["content"]

    # 8. Finish with a very short text; the questionnaires.
    done = _ok(
        reopened.post("/api/study/session/finish", json={"content": "Tides rise and fall."})
    )
    assert done["state"] == "post_session" and done["text_submitted"]
    for instrument in _ok(reopened.get("/api/study/instruments"))["instruments"]:
        answers = {i["id"]: (i["scale_min"] + i["scale_max"]) // 2 for i in instrument["items"]}
        _ok(
            reopened.post(
                f"/api/study/session/{done['id']}/survey",
                json={"instrument": instrument["instrument"], "responses": answers},
            )
        )
    for state in ("surveyed", "complete"):
        _ok(reopened.post("/api/study/session/advance", json={"to_state": state}))

    # 9. The roster: session 1 complete with its text; session 2 no longer held.
    sessions = _ok(admin.get("/api/admin/study/TRAINING/participants"))["participants"][0][
        "sessions"
    ]
    assert sessions[0]["session_state"] == "complete" and sessions[0]["text_submitted"]
    assert sessions[1]["gate"] is None

    # 10. Session 2 now opens.
    two = TestClient(app)
    _ok(two.post(f"/api/study/{second['token']}"))
    _ok(two.post("/api/study/session/begin-tutorial"))
    _ok(two.post("/api/study/session/begin-task"))
    _ok(two.post("/api/study/session/finish", json={"content": "Clouds are grouped by height."}))

    # 11. A drop-out, closed as interrupted.
    dropout = _ok(
        admin.post("/api/admin/study/TRAINING/participants", json={"display_name": "Drop Out"})
    )
    gone = TestClient(app)
    _ok(gone.post(f"/api/study/{dropout['sessions'][0]['token']}"))
    _ok(gone.post("/api/study/session/begin-tutorial"))
    _ok(
        admin.post(
            f"/api/admin/study/sessions/{_ok(gone.get('/api/study/session'))['id']}/interrupt"
        )
    )

    # 12. The admin makes a practice judge — carelessly, without an email —
    # who signs up from the link, rates a text, and revises the rating.
    invite = _ok(admin.post("/api/admin/invites", json={"role": "judge"}))
    judge = TestClient(app)
    assert _ok(judge.get(f"/api/auth/invites/{invite['token']}"))["needs_email"] is True
    _ok(
        judge.post(
            "/api/auth/signup",
            json={
                "token": invite["token"],
                "email_override": "judge@example.org",
                "display_name": "Practice Judge",
                "password": "a-practice-password",
            },
        )
    )
    judge_id = _ok(admin.get("/api/admin/study/judges"))["judges"][0]["id"]
    assigned = _ok(
        admin.post("/api/admin/study/TRAINING/text-assignments", json={"judge_actor_id": judge_id})
    )
    assert assigned["created"] == 2
    queue = _ok(judge.get("/api/judge/texts"))["assignments"]
    view = _ok(judge.get(f"/api/judge/texts/{queue[0]['assignment_id']}"))
    assert not {"condition", "participant_code", "period", "session_id", "text_id"} & set(view)
    for score in (4, 5):  # rate, then revise
        _ok(
            judge.post(
                f"/api/judge/texts/{queue[0]['assignment_id']}/rate",
                json={
                    "ratings": {d["key"]: score for d in view["rubric"]["dimensions"]},
                    "justification": "a practice rating",
                    "condition_guess": "unsure",
                    "confidence": 2,
                },
            )
        )
    progress = _ok(admin.get("/api/admin/study/TRAINING/texts"))["judges"][0]
    assert (progress["rated"], progress["assigned"]) == (1, 2)

    # 13. Export, download, open: their text and the judge's rating are in
    # it; no names are. The names key is a separate download.
    _ok(admin.post("/api/admin/study/TRAINING/export"))
    (listed,) = _ok(admin.get("/api/admin/study/TRAINING/exports"))["exports"]
    archive = admin.get(f"/api/admin/study/TRAINING/exports/{listed['name']}")
    assert archive.status_code == 200
    with tarfile.open(fileobj=io.BytesIO(archive.content)) as tar:
        everything = b"".join(
            tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()
        ).decode("utf-8", "replace")
        judging = json.load(
            tar.extractfile(
                next(m for m in tar.getmembers() if m.name.endswith("text_judging.json"))
            )
        )
    assert "Tides rise and fall." in everything
    assert "a practice rating" in json.dumps(judging)
    assert "Practice Person" not in everything and "Drop Out" not in everything
    key = _ok(admin.get(f"/api/admin/study/TRAINING/exports/{listed['name']}/pseudonyms"))
    assert key["participants"] == {"P01": "Practice Person", "P02": "Drop Out"}

    # 14. Back up; the System tab reads; the practice study is in Costs.
    assert _ok(admin.post("/api/admin/backup", json={}))["bases_failed"] == []
    system = _ok(admin.get("/api/admin/system"))
    assert system["backups_total"] >= 1 and system["schema_version"] >= 16
    studies = _ok(admin.get("/api/admin/costs"))["studies"]
    assert [s["study_id"] for s in studies] == ["TRAINING"]
