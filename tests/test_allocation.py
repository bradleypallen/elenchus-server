"""Allocation as the study's registration describes it (§2.1): a list
generated once from a seed, permuted blocks of four, its hash deposited;
each participant's sequence hidden from the session administrator until
session 1 is scheduled. Without a seed (the practice study) enrolment
draws and reveals at once, as before.
"""

from __future__ import annotations

import contextlib
import io
import json
import tarfile

import pytest
from fastapi.testclient import TestClient

from elenchus import auth, study_enrolment
from elenchus.db import get_registry
from elenchus.db import platform as pdb
from elenchus.server import app
from elenchus.study_enrolment import ALL_CELLS, Cell

client = TestClient(app)
CONFIG = {"topic_a_title": "Tides", "topic_b_title": "Clouds", "min_gap_hours": 0}


@pytest.fixture(autouse=True)
def _clean():
    reg = get_registry()
    reg.migrate_platform()
    con = reg.platform_con()
    with reg.platform_lock:
        for table in (
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


def _login(kind: str = "researcher", label: str | None = None) -> int:
    label = label or kind
    actor_id = pdb.create_actor(
        get_registry().platform_con(),
        kind=kind,
        email=f"{label}@example.com",
        display_name=label,
        password_hash=auth.hash_password("pw"),
    )
    client.cookies.set(auth.SESSION_COOKIE, auth.create_session(actor_id))
    return actor_id


def _setup(**overrides) -> dict:
    r = client.put("/api/admin/study/PILOT/config", json={**CONFIG, **overrides})
    assert r.status_code == 200, r.text
    return r.json()


def _seed(seed: str = "registered-2026", planned_n: int = 8) -> dict:
    r = client.post(
        "/api/admin/study/PILOT/allocation-seed", json={"seed": seed, "planned_n": planned_n}
    )
    assert r.status_code == 200, r.text
    return r.json()


def _enrol(name: str = "Ada", **fields) -> dict:
    r = client.post("/api/admin/study/PILOT/participants", json={"display_name": name, **fields})
    assert r.status_code == 200, r.text
    return r.json()


def _roster() -> dict:
    return client.get("/api/admin/study/PILOT/participants").json()


class TestTheList:
    def test_deterministic_permuted_blocks(self):
        a = study_enrolment.allocation_list("seed-1", 12)
        b = study_enrolment.allocation_list("seed-1", 12)
        assert a == b and len(a) == 12
        for i in range(0, 12, 4):
            assert set(a[i : i + 4]) == set(ALL_CELLS)
        assert study_enrolment.allocation_list("seed-2", 12) != a
        # Extending the list never changes its start.
        assert study_enrolment.allocation_list("seed-1", 48)[:12] == a
        assert study_enrolment.cell_at("seed-1", 7) == a[7]

    def test_hashes_and_letters(self):
        cells = study_enrolment.allocation_list("seed-1", 8)
        letters = "".join(study_enrolment.sequence_letter(c) for c in cells)
        assert set(letters) == set("ABCD") and len(letters) == 8
        assert study_enrolment.allocation_hash(cells) == study_enrolment.allocation_hash(
            study_enrolment.allocation_list("seed-1", 8)
        )
        assert study_enrolment.sequence_letter(Cell("elenchus", "A")) == "A"
        assert study_enrolment.sequence_letter(Cell("baseline", "B")) == "D"
        assert len(study_enrolment.seed_hash("x")) == 64


class TestTheSeed:
    def test_set_once_and_never_returned(self):
        rid = _login()
        _setup()
        summary = _seed("registered-2026", 48)
        assert summary["seeded"] is True and summary["planned_n"] == 48
        assert summary["seed_sha256"] == study_enrolment.seed_hash("registered-2026")
        assert summary["list_sha256"] == study_enrolment.allocation_hash(
            study_enrolment.allocation_list("registered-2026", 48)
        )
        assert summary["set_by"] == rid
        config = client.get("/api/admin/study/PILOT/config").json()
        assert "allocation_seed" not in config
        assert config["allocation"]["seeded"] is True
        put = client.put(
            "/api/admin/study/PILOT/config", json={**CONFIG, "task_minutes": 5}
        ).json()
        assert "allocation_seed" not in put and put["allocation"]["seeded"] is True
        assert "allocation_seed" not in json.dumps(client.get("/api/admin/study/configs").json())
        # Once.
        r = client.post("/api/admin/study/PILOT/allocation-seed", json={"seed": "another"})
        assert r.status_code == 409 and "already set" in r.json()["detail"]["user_message"]

    def test_not_after_enrolment_has_started(self):
        _login()
        _setup()
        _enrol()
        r = client.post("/api/admin/study/PILOT/allocation-seed", json={"seed": "late"})
        assert r.status_code == 409 and "before the first enrolment" in r.text

    def test_rejects_nonsense(self):
        _login()
        _setup()
        assert (
            client.post("/api/admin/study/PILOT/allocation-seed", json={"seed": "  "}).status_code
            == 409
        )
        assert (
            client.post(
                "/api/admin/study/PILOT/allocation-seed", json={"seed": "s", "planned_n": 0}
            ).status_code
            == 409
        )
        assert (
            client.post("/api/admin/study/NOPE/allocation-seed", json={"seed": "s"}).status_code
            == 404
        )


class TestConcealment:
    def test_enrolment_hides_the_sequence_until_scheduled(self):
        rid = _login()
        _setup()
        _seed("registered-2026", 8)
        person = _enrol("Ada", ontology_experience="some")
        assert person["concealed"] is True and person["sessions"] == []
        assert "first_condition" not in person and "first_topic" not in person
        assert person["ontology_experience"] == "some"  # screening isn't the secret
        row = _roster()
        assert row["participants"][0]["concealed"] is True
        assert sum(row["cell_counts"].values()) == 0  # hidden from the balance too
        # Scheduling session 1 reveals the sequence and issues both links.
        r = client.post(f"/api/admin/study/PILOT/participants/{person['id']}/schedule")
        assert r.status_code == 200, r.text
        revealed = r.json()
        expected = study_enrolment.cell_at("registered-2026", 0)
        assert revealed["concealed"] is False
        assert (revealed["first_condition"], revealed["first_topic"]) == (
            expected.first_condition,
            expected.first_topic,
        )
        assert [s["period"] for s in revealed["sessions"]] == [1, 2]
        assert revealed["sessions"][0]["condition"] == expected.first_condition
        assert {s["condition"] for s in revealed["sessions"]} == {"elenchus", "baseline"}
        assert revealed["revealed_by"] == rid and revealed["revealed_at"]
        assert sum(_roster()["cell_counts"].values()) == 1
        # Only once.
        r = client.post(f"/api/admin/study/PILOT/participants/{person['id']}/schedule")
        assert r.status_code == 409

    def test_the_sequence_follows_the_list(self):
        _login()
        _setup()
        _seed("registered-2026", 8)
        people = [_enrol(f"Person {i}") for i in range(6)]
        cells = []
        for p in people:
            r = client.post(f"/api/admin/study/PILOT/participants/{p['id']}/schedule").json()
            cells.append(Cell(r["first_condition"], r["first_topic"]))
        assert cells == study_enrolment.allocation_list("registered-2026", 6)

    def test_a_manual_placement_is_revealed_at_once_and_outside_the_list(self):
        _login()
        _setup()
        _seed("registered-2026", 8)
        _enrol("First")  # block index 0, hidden
        placed = _enrol("Replacement", first_condition="baseline", first_topic="B")
        assert placed["concealed"] is False and placed["allocation"] == "manual"
        assert len(placed["sessions"]) == 2
        nxt = _enrol("Third")
        r = client.post(f"/api/admin/study/PILOT/participants/{nxt['id']}/schedule").json()
        # Third is the second *block* enrolment: place 1 in the list.
        expected = study_enrolment.cell_at("registered-2026", 1)
        assert (r["first_condition"], r["first_topic"]) == (
            expected.first_condition,
            expected.first_topic,
        )

    def test_scheduling_an_unknown_or_foreign_participant_404s(self):
        _login()
        _setup()
        assert client.post("/api/admin/study/PILOT/participants/999/schedule").status_code == 404

    def test_without_a_seed_nothing_changes(self):
        _login()
        _setup()
        person = _enrol()
        assert person["concealed"] is False and len(person["sessions"]) == 2
        assert person["revealed_at"] and person["block_index"] is None
        assert sum(_roster()["cell_counts"].values()) == 1
        config = client.get("/api/admin/study/PILOT/config").json()
        assert config["allocation"] == {
            "seeded": False,
            "planned_n": 48,
            "seed_sha256": None,
            "list_sha256": None,
            "set_by": None,
            "set_at": None,
        }


class TestExport:
    def test_allocation_record_and_the_seed_stays_out_of_the_archive(self):
        _login("admin")
        _setup()
        _seed("registered-2026", 4)
        person = _enrol()
        revealed = client.post(
            f"/api/admin/study/PILOT/participants/{person['id']}/schedule"
        ).json()
        # An export needs at least one session: open session 1's link.
        p = TestClient(app)
        assert p.post(f"/api/study/{revealed['sessions'][0]['token']}").status_code == 200
        r = client.post("/api/admin/study/PILOT/export")
        assert r.status_code == 200, r.text
        (listed,) = client.get("/api/admin/study/PILOT/exports").json()["exports"]
        archive = client.get(f"/api/admin/study/PILOT/exports/{listed['name']}").content
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            members = {
                m.name.split("/", 1)[1]: tar.extractfile(m).read() for m in tar if m.isfile()
            }
        assert b"registered-2026" not in b"".join(members.values())
        allocation = json.loads(members["allocation.json"])
        assert allocation["seeded"] is True and allocation["planned_n"] == 4
        assert allocation["seed_sha256"] == study_enrolment.seed_hash("registered-2026")
        assert allocation["set_by"].startswith("R-")
        (seq,) = allocation["sequences"]
        assert seq["participant_code"] == "P01" and seq["sequence"] in "ABCD"
        assert seq["block_index"] == 0 and seq["revealed_at"]
        participants = json.loads(members["participants.json"])
        assert participants[0]["sequence"] == seq["sequence"]
        assert "allocation_seed" not in json.loads(members["study_config.json"])
        # The seed is beside the names, for the person who deposits it.
        key = json.loads(
            client.get(f"/api/admin/study/PILOT/exports/{listed['name']}/pseudonyms").content
        )
        assert key["allocation_seed"] == "registered-2026"
