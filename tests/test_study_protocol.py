"""Three things the study's registration asks the platform to keep
(design note G1–G3): the maximum gap between a participant's sessions
(flagged, not gated), screening covariates recorded at enrolment, and
protocol deviations logged at the time — by a researcher, or by the
platform when it ends a task by the clock or closes a session as
interrupted. All three reach the roster and the export.
"""

from __future__ import annotations

import contextlib
import io
import json
import tarfile

import pytest
from fastapi.testclient import TestClient

from elenchus import auth
from elenchus.db import get_registry
from elenchus.db import platform as pdb
from elenchus.server import app

client = TestClient(app)

CONFIG = {
    "topic_a_title": "Tides",
    "topic_b_title": "Clouds",
    "min_gap_hours": 0,
    "task_minutes": 5,
    "max_gap_days": 21,
}


@pytest.fixture(autouse=True)
def _clean():
    reg = get_registry()
    reg.migrate_platform()
    con = reg.platform_con()
    with reg.platform_lock:
        for table in (
            "session_deviations",
            "study_participants",
            "study_configs",
            "study_texts",
            "participant_session_tokens",
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


def _login(kind: str = "researcher") -> int:
    actor_id = pdb.create_actor(
        _con(),
        kind=kind,
        email=f"{kind}@example.com",
        display_name=kind,
        password_hash=auth.hash_password("pw"),
    )
    client.cookies.set(auth.SESSION_COOKIE, auth.create_session(actor_id))
    return actor_id


def _setup(**overrides) -> dict:
    r = client.put("/api/admin/study/PILOT/config", json={**CONFIG, **overrides})
    assert r.status_code == 200, r.text
    return r.json()


def _enrol(name: str = "Ada", **fields) -> dict:
    r = client.post("/api/admin/study/PILOT/participants", json={"display_name": name, **fields})
    assert r.status_code == 200, r.text
    return r.json()


def _roster() -> list[dict]:
    return client.get("/api/admin/study/PILOT/participants").json()["participants"]


def _open(token: str) -> TestClient:
    p = TestClient(app)
    assert p.post(f"/api/study/{token}").status_code == 200
    return p


def _complete(p: TestClient) -> None:
    assert p.post("/api/study/session/begin-tutorial").status_code == 200
    assert p.post("/api/study/session/begin-task").status_code == 200
    assert p.post("/api/study/session/finish", json={"content": "My text."}).status_code == 200
    for to_state in ("surveyed", "complete"):
        assert p.post("/api/study/session/advance", json={"to_state": to_state}).status_code == 200


def _shift_closed_at(session_id: int, days: int) -> None:
    with get_registry().platform_lock:
        _con().execute(
            "UPDATE sessions SET closed_at = closed_at - INTERVAL (?) DAY WHERE id = ?",
            [days, session_id],
        )


class TestMaximumGap:
    def test_set_and_default(self):
        _login()
        assert _setup()["max_gap_days"] == 21
        assert _setup(max_gap_days=30)["max_gap_days"] == 30
        assert _setup(max_gap_days=0)["max_gap_days"] == 0
        r = client.put("/api/admin/study/PILOT/config", json={**CONFIG, "max_gap_days": -1})
        assert r.status_code == 400

    def test_the_window_is_reported_not_enforced(self):
        _login()
        _setup(max_gap_days=21)
        person = _enrol()
        assert _roster()[0]["window"] is None  # nothing has ended yet
        first, second = person["sessions"]
        _complete(_open(first["token"]))
        row = _roster()[0]
        w = row["window"]
        assert w["max_gap_days"] == 21 and w["straddled"] is False and w["closed"] is False
        # 22 days pass without the second session.
        _shift_closed_at(row["sessions"][0]["session_id"], 22)
        w = _roster()[0]["window"]
        assert w["closed"] is True and w["straddled"] is False
        # The second link still opens — the window is a flag, not a gate —
        # and the pair is now marked as straddling it.
        _open(second["token"])
        w = _roster()[0]["window"]
        assert w["straddled"] is True

    def test_no_maximum_means_no_window(self):
        _login()
        _setup(max_gap_days=0)
        person = _enrol()
        _complete(_open(person["sessions"][0]["token"]))
        assert _roster()[0]["window"] is None


class TestScreening:
    def test_recorded_at_enrolment_and_in_the_roster(self):
        _login()
        _setup()
        person = _enrol(
            ontology_experience="extensive", prior_llm_use="occasional", nominated_topic=True
        )
        assert person["ontology_experience"] == "extensive"
        assert person["prior_llm_use"] == "occasional" and person["nominated_topic"] is True
        row = _roster()[0]
        assert (row["ontology_experience"], row["prior_llm_use"], row["nominated_topic"]) == (
            "extensive",
            "occasional",
            True,
        )

    def test_not_recorded_is_the_default(self):
        _login()
        _setup()
        person = _enrol()
        assert person["ontology_experience"] == "" and person["prior_llm_use"] == ""
        assert person["nominated_topic"] is False

    @pytest.mark.parametrize("bad", [{"ontology_experience": "lots"}, {"prior_llm_use": "daily"}])
    def test_only_the_coded_levels(self, bad):
        _login()
        _setup()
        r = client.post("/api/admin/study/PILOT/participants", json={"display_name": "X", **bad})
        assert r.status_code == 400 and "must be one of" in r.text


class TestDeviations:
    def test_logged_by_a_researcher(self):
        rid = _login()
        _setup()
        person = _enrol()
        p = _open(person["sessions"][0]["token"])
        sid = p.get("/api/study/session").json()["id"]
        r = client.post(
            f"/api/admin/study/sessions/{sid}/deviations",
            json={"kind": "technical_failure", "note": "Wi-Fi dropped for 12 minutes"},
        )
        assert r.status_code == 200
        record = r.json()
        assert record["kind"] == "technical_failure" and record["logged_by"] == rid
        assert record["logged_at"]
        # In the roster, on the session…
        session_row = _roster()[0]["sessions"][0]
        assert [d["kind"] for d in session_row["deviations"]] == ["technical_failure"]
        # …and in the study-wide list, with the vocabulary.
        listed = client.get("/api/admin/study/PILOT/deviations").json()
        assert [d["note"] for d in listed["deviations"]] == ["Wi-Fi dropped for 12 minutes"]
        assert "timed_out" in listed["kinds"]

    def test_only_known_kinds(self):
        _login()
        _setup()
        person = _enrol()
        p = _open(person["sessions"][0]["token"])
        sid = p.get("/api/study/session").json()["id"]
        r = client.post(f"/api/admin/study/sessions/{sid}/deviations", json={"kind": "oops"})
        assert r.status_code == 400 and r.json()["detail"]["user_message"]

    def test_unknown_session_404(self):
        _login()
        r = client.post("/api/admin/study/sessions/999/deviations", json={"kind": "other"})
        assert r.status_code == 404

    def test_interrupting_a_session_logs_one(self):
        rid = _login()
        _setup()
        person = _enrol()
        p = _open(person["sessions"][0]["token"])
        sid = p.get("/api/study/session").json()["id"]
        assert p.post("/api/study/session/begin-tutorial").status_code == 200
        assert client.post(f"/api/admin/study/sessions/{sid}/interrupt").status_code == 200
        (record,) = pdb.list_session_deviations(_con(), session_id=sid)
        assert record["kind"] == "interruption" and record["logged_by"] == rid
        assert "while tutorial" in record["note"]

    def test_the_clock_logs_one_itself(self):
        _login()
        _setup(task_minutes=5)
        person = _enrol()
        p = _open(person["sessions"][0]["token"])
        assert p.post("/api/study/session/begin-tutorial").status_code == 200
        s = p.post("/api/study/session/begin-task").json()
        with get_registry().platform_lock:
            _con().execute(
                "UPDATE sessions SET state_changed_at = state_changed_at - INTERVAL 6 MINUTE "
                "WHERE id = ?",
                [s["id"]],
            )
        assert p.get("/api/study/session").json()["state"] == "post_session"
        (record,) = pdb.list_session_deviations(_con(), session_id=s["id"])
        assert record["kind"] == "timed_out" and record["logged_by"] is None
        assert "the text was empty" in record["note"]

    def test_the_export_carries_them(self):
        _login("admin")
        _setup()
        person = _enrol(ontology_experience="some", prior_llm_use="regular")
        p = _open(person["sessions"][0]["token"])
        sid = p.get("/api/study/session").json()["id"]
        client.post(
            f"/api/admin/study/sessions/{sid}/deviations",
            json={"kind": "ended_early", "note": "left after 20 minutes"},
        )
        _complete(p)
        r = client.post("/api/admin/study/PILOT/export")
        assert r.status_code == 200, r.text
        (listed,) = client.get("/api/admin/study/PILOT/exports").json()["exports"]
        archive = client.get(f"/api/admin/study/PILOT/exports/{listed['name']}").content
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            members = {
                m.name.split("/", 1)[1]: tar.extractfile(m).read() for m in tar if m.isfile()
            }
        deviations = json.loads(members["deviations.json"])
        assert [d["kind"] for d in deviations] == ["ended_early"]
        assert deviations[0]["logged_by"].startswith("R-")  # pseudonymized researcher
        participants = json.loads(members["participants.json"])
        assert participants[0]["ontology_experience"] == "some"
        assert participants[0]["prior_llm_use"] == "regular"
        per_session = [v for k, v in members.items() if k.endswith("/deviations.json")]
        assert any(
            json.loads(v) and json.loads(v)[0]["kind"] == "ended_early" for v in per_session
        )
