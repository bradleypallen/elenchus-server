"""Tests for Phase D/8 — post-session questionnaire integration.

Three slices:
  1. Instrument definitions — the four Sloan instruments exist with
     the expected item counts and scales.
  2. `validate_responses` — strict completeness + range + type rules.
  3. HTTP routes — definitions endpoint, submission (validation,
     authorization), per-session listing, researcher cohort view.
"""

from __future__ import annotations

import contextlib
import os

import pytest
from fastapi.testclient import TestClient

from elenchus import auth, questionnaires
from elenchus.db import get_registry
from elenchus.db import platform as pdb
from elenchus.questionnaires import (
    INSTRUMENT_VERSION,
    INSTRUMENTS,
    list_instruments,
    validate_responses,
)
from elenchus.server import app

client = TestClient(app)
_test_data_dir = os.environ["ELENCHUS_DATA"]


@pytest.fixture(autouse=True)
def _clean():
    reg = get_registry()
    reg.migrate_platform()
    con = reg.platform_con()
    with reg.platform_lock:
        for table in (
            "survey_responses",
            "judge_ratings",
            "judge_assignments",
            "judge_packages",
            "study_reports",
            "participant_session_tokens",
            "usage",
            "auth_sessions",
            "magic_links",
            "invites",
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


def _full_responses(instrument: str) -> dict:
    """Build a valid mid-scale submission for any instrument."""
    spec = INSTRUMENTS[instrument]
    return {item["id"]: (item["scale_min"] + item["scale_max"]) // 2 for item in spec["items"]}


def _make_participant_session() -> tuple[int, int]:
    """Create a participant + study session. Returns (actor_id, session_id)."""
    con = get_registry().platform_con()
    actor_id = pdb.create_actor(
        con, kind="participant", email=None, display_name="P", password_hash=None
    )
    sid = pdb.create_study_session(
        con,
        actor_id=actor_id,
        study_token="tok",
        condition="elenchus",
        initial_state="surveyed",
    )
    return actor_id, sid


def _login_actor(actor_id: int) -> TestClient:
    c = TestClient(app)
    c.cookies.set(auth.SESSION_COOKIE, auth.create_session(actor_id))
    return c


def _login_kind(kind: str) -> TestClient:
    con = get_registry().platform_con()
    email = f"{kind}@example.com"
    existing = pdb.find_actor_by_email(con, email)
    actor_id = (
        existing["id"]
        if existing
        else pdb.create_actor(
            con,
            kind=kind,
            email=email,
            display_name=kind.title(),
            password_hash=auth.hash_password("pw"),
        )
    )
    return _login_actor(actor_id)


# ─── Instrument definitions ──────────────────────────────────────────


class TestInstrumentDefinitions:
    def test_four_instruments_present(self):
        assert set(INSTRUMENTS.keys()) == {"nasa_tlx", "sus", "tias", "eeq"}

    def test_nasa_tlx_shape(self):
        items = INSTRUMENTS["nasa_tlx"]["items"]
        assert len(items) == 6
        assert all(i["scale_min"] == 0 and i["scale_max"] == 100 for i in items)
        ids = {i["id"] for i in items}
        assert "mental_demand" in ids
        assert "frustration" in ids

    def test_sus_shape(self):
        items = INSTRUMENTS["sus"]["items"]
        assert len(items) == 10
        assert all(i["scale_min"] == 1 and i["scale_max"] == 5 for i in items)

    def test_tias_shape(self):
        items = INSTRUMENTS["tias"]["items"]
        assert len(items) == 12
        assert all(i["scale_min"] == 1 and i["scale_max"] == 7 for i in items)

    def test_eeq_shape(self):
        items = INSTRUMENTS["eeq"]["items"]
        assert len(items) == 8
        assert all(i["scale_min"] == 1 and i["scale_max"] == 7 for i in items)

    def test_item_ids_unique_within_instrument(self):
        for name, spec in INSTRUMENTS.items():
            ids = [i["id"] for i in spec["items"]]
            assert len(ids) == len(set(ids)), f"duplicate item ids in {name}"

    def test_list_instruments_carries_version(self):
        listed = list_instruments()
        assert len(listed) == 4
        assert all(entry["version"] == INSTRUMENT_VERSION for entry in listed)
        assert all("items" in entry and "title" in entry for entry in listed)


# ─── Validation ──────────────────────────────────────────────────────


class TestValidateResponses:
    def test_valid_submission_passes(self):
        for name in INSTRUMENTS:
            assert validate_responses(name, _full_responses(name)) == []

    def test_unknown_instrument(self):
        errors = validate_responses("mood_ring", {})
        assert len(errors) == 1
        assert "Unknown instrument" in errors[0]

    def test_missing_item_rejected(self):
        responses = _full_responses("sus")
        del responses["q3"]
        errors = validate_responses("sus", responses)
        assert any("Missing response for item 'q3'" in e for e in errors)

    def test_extra_item_rejected(self):
        responses = _full_responses("sus")
        responses["q99"] = 3
        errors = validate_responses("sus", responses)
        assert any("Unexpected item 'q99'" in e for e in errors)

    def test_out_of_range_rejected(self):
        responses = _full_responses("sus")
        responses["q1"] = 6  # SUS max is 5
        errors = validate_responses("sus", responses)
        assert any("between 1 and 5" in e for e in errors)

    def test_non_integer_rejected(self):
        responses = _full_responses("sus")
        responses["q1"] = "three"
        errors = validate_responses("sus", responses)
        assert any("must be an integer" in e for e in errors)

    def test_boolean_rejected(self):
        """bool is an int subclass — a stray `true` must not pass as 1."""
        responses = _full_responses("sus")
        responses["q1"] = True
        errors = validate_responses("sus", responses)
        assert any("must be an integer" in e for e in errors)

    def test_non_dict_responses_rejected(self):
        errors = validate_responses("sus", ["not", "a", "dict"])
        assert errors == ["responses must be an object mapping item id to value"]

    def test_tlx_boundary_values_accepted(self):
        responses = _full_responses("nasa_tlx")
        responses["mental_demand"] = 0
        responses["frustration"] = 100
        assert validate_responses("nasa_tlx", responses) == []


# ─── HTTP routes ─────────────────────────────────────────────────────


class TestInstrumentsEndpoint:
    def test_authenticated_actor_gets_definitions(self):
        actor_id, _ = _make_participant_session()
        c = _login_actor(actor_id)
        r = c.get("/api/study/instruments")
        assert r.status_code == 200
        names = {entry["instrument"] for entry in r.json()["instruments"]}
        assert names == {"nasa_tlx", "sus", "tias", "eeq"}

    def test_unauthenticated_401(self):
        r = TestClient(app).get("/api/study/instruments")
        assert r.status_code == 401


class TestSubmitSurvey:
    def test_happy_path_stores_with_version(self):
        actor_id, sid = _make_participant_session()
        c = _login_actor(actor_id)
        r = c.post(
            f"/api/study/session/{sid}/survey",
            json={"instrument": "sus", "responses": _full_responses("sus")},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["instrument"] == "sus"

        rows = pdb.list_survey_responses_for_session(get_registry().platform_con(), sid)
        assert len(rows) == 1
        assert rows[0]["instrument_version"] == INSTRUMENT_VERSION
        assert rows[0]["responses"] == _full_responses("sus")

    def test_all_four_instruments_submittable(self):
        actor_id, sid = _make_participant_session()
        c = _login_actor(actor_id)
        for name in INSTRUMENTS:
            r = c.post(
                f"/api/study/session/{sid}/survey",
                json={"instrument": name, "responses": _full_responses(name)},
            )
            assert r.status_code == 200, f"{name}: {r.text}"
        rows = pdb.list_survey_responses_for_session(get_registry().platform_con(), sid)
        assert {r["instrument"] for r in rows} == set(INSTRUMENTS.keys())

    def test_validation_errors_rejected_whole(self):
        actor_id, sid = _make_participant_session()
        c = _login_actor(actor_id)
        responses = _full_responses("sus")
        del responses["q2"]
        responses["q1"] = 99
        r = c.post(
            f"/api/study/session/{sid}/survey",
            json={"instrument": "sus", "responses": responses},
        )
        assert r.status_code == 400
        errors = r.json()["detail"]["errors"]
        assert len(errors) == 2  # both problems reported
        # Nothing stored.
        rows = pdb.list_survey_responses_for_session(get_registry().platform_con(), sid)
        assert rows == []

    def test_unknown_instrument_400(self):
        actor_id, sid = _make_participant_session()
        c = _login_actor(actor_id)
        r = c.post(
            f"/api/study/session/{sid}/survey",
            json={"instrument": "mood_ring", "responses": {}},
        )
        assert r.status_code == 400

    def test_unknown_session_404(self):
        actor_id, _ = _make_participant_session()
        c = _login_actor(actor_id)
        r = c.post(
            "/api/study/session/99999/survey",
            json={"instrument": "sus", "responses": _full_responses("sus")},
        )
        assert r.status_code == 404

    def test_other_participant_403(self):
        _, sid = _make_participant_session()
        con = get_registry().platform_con()
        other = pdb.create_actor(
            con, kind="participant", email=None, display_name="P2", password_hash=None
        )
        c = _login_actor(other)
        r = c.post(
            f"/api/study/session/{sid}/survey",
            json={"instrument": "sus", "responses": _full_responses("sus")},
        )
        assert r.status_code == 403

    def test_resubmission_keeps_newest(self):
        actor_id, sid = _make_participant_session()
        c = _login_actor(actor_id)
        first = _full_responses("sus")
        c.post(f"/api/study/session/{sid}/survey", json={"instrument": "sus", "responses": first})
        second = dict(first)
        second["q1"] = 5
        c.post(f"/api/study/session/{sid}/survey", json={"instrument": "sus", "responses": second})

        rows = pdb.list_survey_responses_for_session(get_registry().platform_con(), sid)
        # Newest first; both retained for audit.
        assert len(rows) == 2
        assert rows[0]["responses"]["q1"] == 5


class TestListSessionSurveys:
    def test_owner_sees_own(self):
        actor_id, sid = _make_participant_session()
        c = _login_actor(actor_id)
        c.post(
            f"/api/study/session/{sid}/survey",
            json={"instrument": "eeq", "responses": _full_responses("eeq")},
        )
        r = c.get(f"/api/study/session/{sid}/surveys")
        assert r.status_code == 200
        assert len(r.json()["surveys"]) == 1

    def test_researcher_sees_any(self):
        actor_id, sid = _make_participant_session()
        pc = _login_actor(actor_id)
        pc.post(
            f"/api/study/session/{sid}/survey",
            json={"instrument": "eeq", "responses": _full_responses("eeq")},
        )
        rc = _login_kind("researcher")
        r = rc.get(f"/api/study/session/{sid}/surveys")
        assert r.status_code == 200
        assert len(r.json()["surveys"]) == 1

    def test_other_participant_403(self):
        _, sid = _make_participant_session()
        con = get_registry().platform_con()
        other = pdb.create_actor(
            con, kind="participant", email=None, display_name="P2", password_hash=None
        )
        c = _login_actor(other)
        r = c.get(f"/api/study/session/{sid}/surveys")
        assert r.status_code == 403


class TestAdminSurveyList:
    def test_researcher_cohort_view_with_filter(self):
        actor_id, sid = _make_participant_session()
        pc = _login_actor(actor_id)
        pc.post(
            f"/api/study/session/{sid}/survey",
            json={"instrument": "sus", "responses": _full_responses("sus")},
        )
        pc.post(
            f"/api/study/session/{sid}/survey",
            json={"instrument": "eeq", "responses": _full_responses("eeq")},
        )
        rc = _login_kind("researcher")
        all_r = rc.get("/api/admin/study/surveys")
        assert all_r.status_code == 200
        assert len(all_r.json()["surveys"]) == 2
        sus_only = rc.get("/api/admin/study/surveys?instrument=sus")
        assert len(sus_only.json()["surveys"]) == 1
        assert sus_only.json()["surveys"][0]["instrument"] == "sus"

    def test_regular_user_forbidden(self):
        c = _login_kind("user")
        r = c.get("/api/admin/study/surveys")
        assert r.status_code == 403
        _ = questionnaires  # keep module import referenced
