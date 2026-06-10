"""Tests for Phase D/9 — per-study data export.

Four slices:
  1. `list_sessions_for_study` join helper.
  2. Pseudonymization — opaque IDs inside the archive, identity map
     outside it, no display names or emails anywhere in the tar.
  3. Archive contents — per-session directories with the expected
     files, study-level judging.json + manifest.json, per-base
     EXPORT DATABASE dump.
  4. Route — role gating, 404 on unknown study, failure isolation.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tarfile

import pytest
from fastapi.testclient import TestClient

from elenchus import auth
from elenchus.db import get_registry
from elenchus.db import platform as pdb
from elenchus.dialectical_state import DialecticalState
from elenchus.server import app
from elenchus.study_export import export_study

client = TestClient(app)
_test_data_dir = os.environ["ELENCHUS_DATA"]

PARTICIPANT_REAL_NAME = "Dr. Jane Identifiable"


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
    _wipe()
    yield
    client.cookies.clear()
    _wipe()


def _wipe():
    for root, _dirs, files in os.walk(_test_data_dir):
        for f in files:
            if f.endswith(".duckdb") and f != "platform.duckdb":
                with contextlib.suppress(OSError):
                    os.remove(os.path.join(root, f))
    for sub in ("bases", "exports"):
        path = os.path.join(_test_data_dir, sub)
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)


def _seed_study(study_id: str = "PILOT") -> dict:
    """Build one full study fixture: researcher, participant (with an
    identifiable display name that must NOT appear in the archive),
    consumed token, session with base + content, report, survey,
    judge package + assignment + rating. Returns ids."""
    con = get_registry().platform_con()
    reg = get_registry()

    researcher = pdb.create_actor(
        con,
        kind="researcher",
        email="researcher@example.com",
        display_name="Real Researcher Name",
        password_hash=auth.hash_password("pw"),
    )
    participant = pdb.create_actor(
        con,
        kind="participant",
        email=None,
        display_name=PARTICIPANT_REAL_NAME,
        password_hash=None,
    )
    pdb.create_participant_token(
        con,
        token="tok-1",
        actor_id=participant,
        study_id=study_id,
        condition="elenchus",
        issued_by=researcher,
    )
    sid = pdb.create_study_session(
        con,
        actor_id=participant,
        study_token="tok-1",
        condition="elenchus",
        initial_state="active",
    )

    base_id = "study-base"
    pdb.create_base(con, base_id=base_id, name=base_id, owner_id=participant)
    pdb.attach_base_to_session(con, sid, base_id)
    path = reg.db_path(base_id, actor_id=participant)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    state = DialecticalState.create(path, base_id)
    state.commit("Biomes are climate-based.")
    state.add_conversation("user", "Let me lay out my view of biomes.")
    state.add_conversation("assistant", "Go ahead.")
    state.base.con.close()

    report_id = pdb.record_study_report(
        con,
        session_id=sid,
        condition="elenchus",
        content="# Domain\nBiomes",
        generator_model="claude-opus-4-6",
        prompt_tokens=10,
        completion_tokens=5,
        cost_usd=0.01,
    )
    baseline_report_id = pdb.record_study_report(
        con,
        session_id=sid + 1000,  # synthetic counterpart for the package
        condition="baseline",
        content="# Domain\nBiomes (chat)",
        generator_model="claude-opus-4-6",
        prompt_tokens=10,
        completion_tokens=5,
        cost_usd=0.01,
    )
    pdb.record_survey_response(
        con,
        session_id=sid,
        instrument="sus",
        instrument_version="1",
        responses={f"q{i}": 3 for i in range(1, 11)},
    )

    judge = pdb.create_actor(
        con,
        kind="judge",
        email="judge@example.com",
        display_name="Real Judge Name",
        password_hash=auth.hash_password("pw"),
    )
    pid = pdb.create_judge_package(
        con,
        study_id=study_id,
        slot_a_report_id=report_id,
        slot_b_report_id=baseline_report_id,
        slot_a_condition="elenchus",
        slot_b_condition="baseline",
        created_by=researcher,
    )
    aid = pdb.create_judge_assignment(
        con, judge_actor_id=judge, package_id=pid, assigned_by=researcher
    )
    pdb.record_judge_rating(
        con,
        assignment_id=aid,
        ratings={"completeness": {"a": 5, "b": 4}},
        justification_a="good",
        justification_b="ok",
        pairwise_winner="a",
        condition_guess_a="unsure",
        condition_guess_b="unsure",
        confidence=2,
    )
    return {
        "researcher": researcher,
        "participant": participant,
        "judge": judge,
        "session_id": sid,
        "base_id": base_id,
    }


def _archive_members(archive_path: str) -> dict[str, bytes]:
    """Read every regular file in the tar into {relative_name: bytes}."""
    out: dict[str, bytes] = {}
    with tarfile.open(archive_path) as tar:
        for member in tar.getmembers():
            if member.isfile():
                f = tar.extractfile(member)
                if f is not None:
                    out[member.name] = f.read()
    return out


# ─── Join helper ─────────────────────────────────────────────────────


class TestListSessionsForStudy:
    def test_returns_only_this_studys_sessions(self):
        ids = _seed_study("PILOT")
        # A second study with its own session.
        con = get_registry().platform_con()
        other_participant = pdb.create_actor(
            con, kind="participant", email=None, display_name="Other P", password_hash=None
        )
        pdb.create_participant_token(
            con,
            token="tok-other",
            actor_id=other_participant,
            study_id="OTHER",
            condition="baseline",
            issued_by=ids["researcher"],
        )
        pdb.create_study_session(
            con,
            actor_id=other_participant,
            study_token="tok-other",
            condition="baseline",
            initial_state="briefing",
        )

        pilot = pdb.list_sessions_for_study(con, "PILOT")
        assert len(pilot) == 1
        assert pilot[0]["id"] == ids["session_id"]
        other = pdb.list_sessions_for_study(con, "OTHER")
        assert len(other) == 1

    def test_empty_for_unknown_study(self):
        assert pdb.list_sessions_for_study(get_registry().platform_con(), "GHOST") == []


# ─── Export core ─────────────────────────────────────────────────────


class TestExportStudy:
    def test_archive_and_pseudonym_file_created(self, tmp_path):
        _seed_study()
        result = export_study("PILOT", output_dir=str(tmp_path))
        assert os.path.exists(result["archive"])
        assert result["archive"].endswith(".tar.gz")
        assert os.path.exists(result["pseudonym_file"])
        assert len(result["sessions_exported"]) == 1
        assert result["sessions_failed"] == []

    def test_archive_contains_expected_files(self, tmp_path):
        _seed_study()
        result = export_study("PILOT", output_dir=str(tmp_path))
        names = set(_archive_members(result["archive"]).keys())

        def has(suffix: str) -> bool:
            return any(n.endswith(suffix) for n in names)

        assert has("manifest.json")
        assert has("judging.json")
        assert has("session.json")
        assert has("state.json")
        assert has("transcript.json")
        assert has("reports.json")
        assert has("surveys.json")
        assert has("integrity.json")
        # The per-base EXPORT DATABASE dump.
        assert has("base/schema.sql")
        assert has("base/load.sql")

    def test_session_directory_named_by_pseudonym_and_condition(self, tmp_path):
        _seed_study()
        result = export_study("PILOT", output_dir=str(tmp_path))
        assert result["sessions_exported"][0]["label"] == "P-001-elenchus"
        names = set(_archive_members(result["archive"]).keys())
        assert any("/sessions/P-001-elenchus/" in n for n in names)

    def test_no_identity_leaks_into_archive(self, tmp_path):
        """The blunt test: no member of the tar may contain the
        participant's real display name, the judge's, the
        researcher's, or any email address we seeded."""
        _seed_study()
        result = export_study("PILOT", output_dir=str(tmp_path))
        members = _archive_members(result["archive"])
        forbidden = [
            PARTICIPANT_REAL_NAME.encode(),
            b"Real Researcher Name",
            b"Real Judge Name",
            b"researcher@example.com",
            b"judge@example.com",
        ]
        for name, blob in members.items():
            for needle in forbidden:
                assert needle not in blob, f"identity {needle!r} leaked into {name}"

    def test_actor_ids_pseudonymized_in_session_json(self, tmp_path):
        ids = _seed_study()
        result = export_study("PILOT", output_dir=str(tmp_path))
        members = _archive_members(result["archive"])
        session_blob = next(v for k, v in members.items() if k.endswith("session.json"))
        session = json.loads(session_blob)
        assert session["actor_id"] == "P-001"
        assert session["actor_id"] != ids["participant"]

    def test_judging_pseudonymized(self, tmp_path):
        _seed_study()
        result = export_study("PILOT", output_dir=str(tmp_path))
        members = _archive_members(result["archive"])
        judging_blob = next(v for k, v in members.items() if k.endswith("judging.json"))
        judging = json.loads(judging_blob)
        assignment = judging[0]["assignments"][0]["assignment"]
        assert assignment["judge_actor_id"] == "J-001"
        assert judging[0]["package"]["created_by"] == "R-001"
        # The rating itself rode through unmodified.
        assert judging[0]["assignments"][0]["rating"]["pairwise_winner"] == "a"

    def test_pseudonym_file_maps_real_ids(self, tmp_path):
        ids = _seed_study()
        result = export_study("PILOT", output_dir=str(tmp_path))
        with open(result["pseudonym_file"], encoding="utf-8") as f:
            mapping = json.load(f)
        assert mapping[str(ids["participant"])] == "P-001"
        assert mapping[str(ids["judge"])] == "J-001"
        assert mapping[str(ids["researcher"])] == "R-001"

    def test_pseudonym_file_not_inside_archive(self, tmp_path):
        _seed_study()
        result = export_study("PILOT", output_dir=str(tmp_path))
        names = set(_archive_members(result["archive"]).keys())
        assert not any("pseudonym" in n for n in names)

    def test_state_and_transcript_content(self, tmp_path):
        _seed_study()
        result = export_study("PILOT", output_dir=str(tmp_path))
        members = _archive_members(result["archive"])
        state = json.loads(next(v for k, v in members.items() if k.endswith("state.json")))
        assert "Biomes are climate-based." in state["commitments"]
        transcript = json.loads(
            next(v for k, v in members.items() if k.endswith("transcript.json"))
        )
        assert transcript[0]["content"] == "Let me lay out my view of biomes."

    def test_broken_base_reported_not_fatal(self, tmp_path):
        ids = _seed_study()
        # Delete the base file out from under the session.
        reg = get_registry()
        path = reg.db_path(ids["base_id"], actor_id=ids["participant"])
        for _name, handle in list(reg._handles.items()):
            with contextlib.suppress(Exception):
                handle.state.base.con.close()
        reg._handles.clear()
        os.remove(path)

        result = export_study("PILOT", output_dir=str(tmp_path))
        assert len(result["sessions_failed"]) == 1
        assert result["sessions_exported"] == []
        # Archive still produced, with the failure in the manifest.
        members = _archive_members(result["archive"])
        manifest = json.loads(next(v for k, v in members.items() if k.endswith("manifest.json")))
        assert len(manifest["sessions_failed"]) == 1

    def test_session_without_base_exports_minimal_set(self, tmp_path):
        """A briefing-only session (no base attached) still exports
        session.json + empty placeholders."""
        con = get_registry().platform_con()
        researcher = pdb.create_actor(
            con,
            kind="researcher",
            email="r2@example.com",
            display_name="R2",
            password_hash=auth.hash_password("pw"),
        )
        participant = pdb.create_actor(
            con, kind="participant", email=None, display_name="P2", password_hash=None
        )
        pdb.create_participant_token(
            con,
            token="tok-b",
            actor_id=participant,
            study_id="BRIEF",
            condition="baseline",
            issued_by=researcher,
        )
        pdb.create_study_session(
            con,
            actor_id=participant,
            study_token="tok-b",
            condition="baseline",
            initial_state="briefing",
        )
        result = export_study("BRIEF", output_dir=str(tmp_path))
        assert len(result["sessions_exported"]) == 1
        members = _archive_members(result["archive"])
        state = json.loads(next(v for k, v in members.items() if k.endswith("state.json")))
        assert state is None


# ─── Route ───────────────────────────────────────────────────────────


class TestExportRoute:
    def _login(self, kind: str) -> TestClient:
        con = get_registry().platform_con()
        email = f"{kind}-route@example.com"
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
        c = TestClient(app)
        c.cookies.set(auth.SESSION_COOKIE, auth.create_session(actor_id))
        return c

    def test_researcher_can_export(self):
        _seed_study()
        c = self._login("researcher")
        r = c.post("/api/admin/study/PILOT/export")
        assert r.status_code == 200, r.text
        body = r.json()
        assert os.path.exists(body["archive"])
        assert len(body["sessions_exported"]) == 1

    def test_unknown_study_404(self):
        c = self._login("researcher")
        r = c.post("/api/admin/study/GHOST/export")
        assert r.status_code == 404

    def test_regular_user_forbidden(self):
        _seed_study()
        c = self._login("user")
        r = c.post("/api/admin/study/PILOT/export")
        assert r.status_code == 403

    def test_unauthenticated_401(self):
        r = TestClient(app).post("/api/admin/study/PILOT/export")
        assert r.status_code == 401
        _ = io  # keep stdlib import referenced
