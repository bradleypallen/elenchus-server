"""Tests for participant enrolment in the crossover design: the
permuted-block allocation, one-step issuance of both session links, the
roster, the gate that keeps a participant's second session shut until
the first is over (and the study's gap has passed), and the researcher's
way out of an abandoned first session.
"""

from __future__ import annotations

import contextlib
import random
from collections import Counter

import pytest
from fastapi.testclient import TestClient

from elenchus import auth
from elenchus.db import get_registry
from elenchus.db import platform as pdb
from elenchus.server import app
from elenchus.study_enrolment import (
    ALL_CELLS,
    BLOCK_SIZE,
    Cell,
    next_cell,
    participant_code,
    session_plan,
)

client = TestClient(app)

CONFIG = {
    "topic_a_title": "Occurrence and its relatives in Darwin Core",
    "topic_a_brief": "Occurrence, Organism, Event, MaterialSample.",
    "topic_b_title": "Taxon concepts and names",
    "topic_b_brief": "Name, taxon concept, usage, circumscription.",
    "min_gap_hours": 48,
}


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


def _login(kind: str = "researcher") -> int:
    con = get_registry().platform_con()
    actor_id = pdb.create_actor(
        con,
        kind=kind,
        email=f"{kind}@example.com",
        display_name=kind,
        password_hash=auth.hash_password("pw"),
    )
    client.cookies.set(auth.SESSION_COOKIE, auth.create_session(actor_id))
    return actor_id


def _setup(study: str = "PILOT", **overrides) -> None:
    _login()
    r = client.put(f"/api/admin/study/{study}/config", json={**CONFIG, **overrides})
    assert r.status_code == 200, r.text


def _enrol(study: str = "PILOT", name: str = "Ada", **fields) -> dict:
    r = client.post(
        f"/api/admin/study/{study}/participants", json={"display_name": name, **fields}
    )
    assert r.status_code == 200, r.text
    return r.json()


def _open(token: str) -> tuple[TestClient, object]:
    pclient = TestClient(app)
    return pclient, pclient.post(f"/api/study/{token}")


def _run_to_completion(pclient: TestClient) -> None:
    assert pclient.post("/api/study/session/begin-tutorial").status_code == 200
    assert pclient.post("/api/study/session/begin-task").status_code == 200
    assert (
        pclient.post("/api/study/session/finish", json={"content": "My text."}).status_code == 200
    )
    for to_state in ("surveyed", "complete"):
        r = pclient.post("/api/study/session/advance", json={"to_state": to_state})
        assert r.status_code == 200, r.text


# ── Allocation (pure) ────────────────────────────────────────────────


class TestAllocation:
    def test_four_cells_cross_condition_and_topic(self):
        assert BLOCK_SIZE == 4
        assert {(c.first_condition, c.first_topic) for c in ALL_CELLS} == {
            ("elenchus", "A"),
            ("elenchus", "B"),
            ("baseline", "A"),
            ("baseline", "B"),
        }

    def test_session_plan_gives_each_condition_and_topic_once(self):
        for cell in ALL_CELLS:
            first, second = session_plan(cell)
            assert (first["period"], second["period"]) == (1, 2)
            assert {first["condition"], second["condition"]} == {"elenchus", "baseline"}
            assert {first["topic"], second["topic"]} == {"A", "B"}
            assert (first["condition"], first["topic"]) == (cell.first_condition, cell.first_topic)

    @pytest.mark.parametrize("seed", range(25))
    def test_every_block_of_four_holds_each_cell_once(self, seed):
        rng = random.Random(seed)
        drawn: list[Cell] = []
        for _ in range(30):  # the pilot's n
            drawn.append(next_cell(drawn, rng))
        for start in range(0, 28, BLOCK_SIZE):
            assert set(drawn[start : start + BLOCK_SIZE]) == set(ALL_CELLS)
        # Balanced at every point, not just at the end: never more than
        # one apart.
        for n in range(1, 31):
            counts = Counter(drawn[:n])
            assert max(counts.values()) - min(counts.get(c, 0) for c in ALL_CELLS) <= 1

    def test_order_within_a_block_varies(self):
        """Not a fixed rotation — the next cell isn't predictable."""
        firsts = {next_cell([], random.Random(seed)) for seed in range(40)}
        assert firsts == set(ALL_CELLS)

    def test_bad_cell_rejected(self):
        with pytest.raises(ValueError):
            Cell("placebo", "A")
        with pytest.raises(ValueError):
            Cell("elenchus", "C")

    def test_codes_carry_no_information(self):
        assert [participant_code(n) for n in (1, 9, 10, 30)] == ["P01", "P09", "P10", "P30"]


# ── Study setup ──────────────────────────────────────────────────────


class TestConfig:
    def test_set_and_get(self):
        _setup()
        config = client.get("/api/admin/study/PILOT/config").json()
        assert config["topics"]["A"]["title"] == CONFIG["topic_a_title"]
        assert config["topics"]["B"]["brief"] == CONFIG["topic_b_brief"]
        assert config["min_gap_hours"] == 48
        assert [
            s["study_id"] for s in client.get("/api/admin/study/configs").json()["studies"]
        ] == ["PILOT"]

    def test_update_keeps_creator(self):
        _setup()
        before = client.get("/api/admin/study/PILOT/config").json()
        client.put("/api/admin/study/PILOT/config", json={**CONFIG, "min_gap_hours": 0})
        after = client.get("/api/admin/study/PILOT/config").json()
        assert after["min_gap_hours"] == 0
        assert (after["created_by"], after["created_at"]) == (
            before["created_by"],
            before["created_at"],
        )

    @pytest.mark.parametrize(
        "bad",
        [
            {"topic_a_title": " "},
            {"topic_b_title": CONFIG["topic_a_title"]},
            {"min_gap_hours": -1},
        ],
    )
    def test_validation(self, bad):
        _login()
        r = client.put("/api/admin/study/PILOT/config", json={**CONFIG, **bad})
        assert r.status_code == 400

    def test_unknown_study_404(self):
        _login()
        assert client.get("/api/admin/study/NOPE/config").status_code == 404

    def test_researcher_only(self):
        _login("user")
        assert client.put("/api/admin/study/PILOT/config", json=CONFIG).status_code == 403
        assert (
            client.post(
                "/api/admin/study/PILOT/participants", json={"display_name": "x"}
            ).status_code
            == 403
        )
        assert client.get("/api/admin/study/PILOT/participants").status_code == 403


# ── Enrolment ────────────────────────────────────────────────────────


class TestEnrol:
    def test_needs_a_configured_study(self):
        _login()
        r = client.post("/api/admin/study/PILOT/participants", json={"display_name": "Ada"})
        assert r.status_code == 409
        assert "Set up study" in r.json()["detail"]

    def test_issues_both_links_crossed(self):
        _setup()
        p = _enrol()
        assert (p["participant_code"], p["allocation"]) == ("P01", "block")
        first, second = p["sessions"]
        assert (first["period"], second["period"]) == (1, 2)
        assert first["condition"] == p["first_condition"]
        assert {first["condition"], second["condition"]} == {"elenchus", "baseline"}
        assert {first["topic_title"], second["topic_title"]} == {
            CONFIG["topic_a_title"],
            CONFIG["topic_b_title"],
        }
        expected_first_topic = CONFIG[f"topic_{p['first_topic'].lower()}_title"]
        assert first["topic_title"] == expected_first_topic
        assert first["token"] != second["token"]
        assert (first["token_status"], second["token_status"]) == ("scheduled", "scheduled")

    def test_links_carry_the_topic_into_the_session(self):
        _setup()
        first = _enrol()["sessions"][0]
        pclient, r = _open(first["token"])
        assert r.status_code == 200
        session = pclient.get("/api/study/session").json()
        assert session["topic_title"] == first["topic_title"]
        assert session["condition"] == first["condition"]

    def test_eight_enrolments_fill_every_cell_twice(self):
        _setup()
        for i in range(8):
            _enrol(name=f"Person {i}")
        roster = client.get("/api/admin/study/PILOT/participants").json()
        assert [p["participant_code"] for p in roster["participants"]] == [
            f"P{i:02d}" for i in range(1, 9)
        ]
        assert set(roster["cell_counts"].values()) == {2}
        assert len(roster["cell_counts"]) == 4

    def test_manual_placement_sits_outside_the_blocks(self):
        _setup()
        for i in range(3):
            _enrol(name=f"Person {i}")
        drawn = [
            (p["first_condition"], p["first_topic"])
            for p in client.get("/api/admin/study/PILOT/participants").json()["participants"]
        ]
        # A replacement placed by hand into an already-used cell...
        manual = _enrol(name="Replacement", first_condition=drawn[0][0], first_topic=drawn[0][1])
        assert manual["allocation"] == "manual"
        # ...doesn't take the block's last place: the 4th block draw still
        # gets the one cell the first three left open.
        fourth = _enrol(name="Person 3")
        assert (fourth["first_condition"], fourth["first_topic"]) not in drawn
        assert fourth["allocation"] == "block"

    def test_manual_needs_both_fields(self):
        _setup()
        r = client.post(
            "/api/admin/study/PILOT/participants",
            json={"display_name": "Ada", "first_condition": "elenchus"},
        )
        assert r.status_code == 400
        r = client.post(
            "/api/admin/study/PILOT/participants",
            json={"display_name": "Ada", "first_condition": "placebo", "first_topic": "A"},
        )
        assert r.status_code == 400

    def test_topic_edits_affect_future_enrolments_only(self):
        _setup()
        before = _enrol(name="Early")
        client.put(
            "/api/admin/study/PILOT/config", json={**CONFIG, "topic_a_title": "A revised topic"}
        )
        after = _enrol(name="Late")
        roster = client.get("/api/admin/study/PILOT/participants").json()["participants"]
        early_topics = {s["topic_title"] for s in roster[0]["sessions"]}
        late_topics = {s["topic_title"] for s in roster[1]["sessions"]}
        assert CONFIG["topic_a_title"] in early_topics and "A revised topic" in late_topics
        assert before["participant_code"] != after["participant_code"]

    def test_studies_have_separate_codes_and_blocks(self):
        _setup("ONE")
        client.put("/api/admin/study/TWO/config", json=CONFIG)
        assert _enrol("ONE")["participant_code"] == "P01"
        assert _enrol("TWO")["participant_code"] == "P01"
        assert len(client.get("/api/admin/study/ONE/participants").json()["participants"]) == 1


# ── The second-session gate ──────────────────────────────────────────


class TestSecondSessionGate:
    def test_second_link_shut_until_first_is_started(self):
        _setup()
        _, second = _enrol()["sessions"]
        _, r = _open(second["token"])
        assert r.status_code == 409
        detail = r.json()["detail"]
        assert (detail["status"], detail["reason"]) == ("not_yet", "first_not_started")
        assert "first session first" in detail["user_message"]
        # Not consumed by the attempt.
        token = pdb.find_participant_token(get_registry().platform_con(), second["token"])
        assert token["status"] == "scheduled"

    def test_shut_while_first_is_in_progress(self):
        _setup()
        first, second = _enrol()["sessions"]
        _open(first["token"])
        _, r = _open(second["token"])
        assert r.status_code == 409
        assert r.json()["detail"]["reason"] == "first_still_open"

    def test_shut_until_the_gap_has_passed(self):
        _setup()  # 48 h
        first, second = _enrol()["sessions"]
        pclient, _ = _open(first["token"])
        _run_to_completion(pclient)
        _, r = _open(second["token"])
        assert r.status_code == 409
        detail = r.json()["detail"]
        assert detail["reason"] == "too_soon"
        assert detail["opens_at"] is not None
        assert "This link will work from" in detail["user_message"]
        # The roster shows the researcher the same thing.
        row = client.get("/api/admin/study/PILOT/participants").json()["participants"][0]
        assert row["sessions"][0]["session_state"] == "complete"
        assert row["sessions"][0]["text_submitted"] is True
        assert row["sessions"][1]["gate"]["reason"] == "too_soon"

    def test_opens_once_first_is_done_and_gap_is_zero(self):
        _setup(min_gap_hours=0)
        first, second = _enrol()["sessions"]
        pclient, _ = _open(first["token"])
        _run_to_completion(pclient)
        p2, r = _open(second["token"])
        assert r.status_code == 200, r.text
        assert p2.get("/api/study/session").json()["condition"] == second["condition"]

    def test_gap_is_read_when_the_link_is_opened(self):
        """Shortening the gap after the fact opens a waiting link."""
        _setup()
        first, second = _enrol()["sessions"]
        pclient, _ = _open(first["token"])
        _run_to_completion(pclient)
        assert _open(second["token"])[1].status_code == 409
        client.put("/api/admin/study/PILOT/config", json={**CONFIG, "min_gap_hours": 0})
        assert _open(second["token"])[1].status_code == 200

    def test_cancelled_first_session_does_not_hold_the_second(self):
        _setup()
        first, second = _enrol()["sessions"]
        assert client.delete(f"/api/admin/study/tokens/{first['token']}").status_code == 200
        assert _open(second["token"])[1].status_code == 200

    def test_resuming_a_second_session_is_never_gated(self):
        _setup(min_gap_hours=0)
        first, second = _enrol()["sessions"]
        pclient, _ = _open(first["token"])
        _run_to_completion(pclient)
        assert _open(second["token"])[1].status_code == 200
        # The gap is lengthened while they are mid-session…
        client.put("/api/admin/study/PILOT/config", json={**CONFIG, "min_gap_hours": 500})
        _, r = _open(second["token"])  # …the resume link still works.
        assert r.status_code == 200 and r.json()["resumed"] is True

    def test_hand_issued_tokens_are_not_gated(self):
        _login()
        r = client.post(
            "/api/admin/study/tokens",
            json={"study_id": "S", "condition": "baseline", "display_name": "P"},
        )
        assert _open(r.json()["token"])[1].status_code == 200


# ── Closing an abandoned session ─────────────────────────────────────


class TestInterrupt:
    def test_interrupting_an_abandoned_first_session_frees_the_second(self):
        _setup(min_gap_hours=0)
        first, second = _enrol()["sessions"]
        _, opened = _open(first["token"])  # started, then abandoned in briefing
        assert _open(second["token"])[1].status_code == 409

        r = client.post(f"/api/admin/study/sessions/{opened.json()['session_id']}/interrupt")
        assert r.status_code == 200, r.text
        assert (r.json()["state"], r.json()["was_state"]) == ("interrupted", "briefing")
        assert _open(second["token"])[1].status_code == 200

    def test_already_closed_409_and_unknown_404(self):
        _setup(min_gap_hours=0)
        first, _ = _enrol()["sessions"]
        pclient, opened = _open(first["token"])
        _run_to_completion(pclient)
        sid = opened.json()["session_id"]
        assert client.post(f"/api/admin/study/sessions/{sid}/interrupt").status_code == 409
        assert client.post("/api/admin/study/sessions/999999/interrupt").status_code == 404

    def test_researcher_only(self):
        _setup()
        first, _ = _enrol()["sessions"]
        _, opened = _open(first["token"])
        client.cookies.clear()
        _login("user")
        sid = opened.json()["session_id"]
        assert client.post(f"/api/admin/study/sessions/{sid}/interrupt").status_code == 403


# ── Export: the two sessions are linked, the name stays out ──────────


class TestExportLinkage:
    def test_sessions_linked_by_code_and_name_kept_out(self, tmp_path):
        import io
        import json
        import tarfile

        from elenchus.study_export import export_study

        _setup(min_gap_hours=0)
        person = _enrol(name="Ada Lovelace-Identifiable")
        for session in person["sessions"]:
            pclient, r = _open(session["token"])
            assert r.status_code == 200, r.text
            _run_to_completion(pclient)

        result = export_study("PILOT", output_dir=str(tmp_path))
        with tarfile.open(result["archive"]) as tar:
            members = {m.name: tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()}
        blob = b"\n".join(members.values())
        assert b"Ada Lovelace-Identifiable" not in blob
        for session in person["sessions"]:
            assert session["token"].encode() not in blob  # a credential

        sessions = [
            json.load(io.BytesIO(v)) for k, v in members.items() if k.endswith("session.json")
        ]
        assert {s["participant_code"] for s in sessions} == {"P01"}
        assert sorted(s["period"] for s in sessions) == [1, 2]
        assert {s["condition"] for s in sessions} == {"elenchus", "baseline"}
        assert len({s["actor_id"] for s in sessions}) == 2  # per-session identities
        first = next(s for s in sessions if s["period"] == 1)
        assert (first["condition"], first["allocation"]) == (person["first_condition"], "block")

        roster = json.load(
            io.BytesIO(next(v for k, v in members.items() if k.endswith("participants.json")))
        )
        assert roster == [
            {
                "participant_code": "P01",
                "first_condition": person["first_condition"],
                "first_topic": person["first_topic"],
                "allocation": "block",
                "enrolled_at": roster[0]["enrolled_at"],
                "enrolled_by": "R-001",
            }
        ]
        config = json.load(
            io.BytesIO(next(v for k, v in members.items() if k.endswith("study_config.json")))
        )
        assert config["topics"]["A"]["title"] == CONFIG["topic_a_title"]
        assert config["created_by"] == "R-001"

        # The name lives only in the file kept next to the archive.
        with open(result["pseudonym_file"], encoding="utf-8") as f:
            assert json.load(f)["participants"] == {"P01": "Ada Lovelace-Identifiable"}
