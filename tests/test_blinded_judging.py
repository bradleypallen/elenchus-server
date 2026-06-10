"""Tests for Phase D/7 — blinded judge interface.

Four slices:
  1. Platform DB CRUD — packages, assignments, ratings.
  2. require_judge dependency — role gating (judge / admin pass;
     researcher, user, anonymous don't).
  3. Researcher endpoints — create package (slot randomization),
     create assignment (judge id validation).
  4. Judge endpoints — queue, view (NO condition leak), submit
     rating (validation, single-submission marks completed).
"""

from __future__ import annotations

import contextlib
import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from elenchus import auth
from elenchus.db import get_registry
from elenchus.db import platform as pdb
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


def _create_actor(kind: str, email: str | None = None) -> int:
    con = get_registry().platform_con()
    return pdb.create_actor(
        con,
        kind=kind,
        email=email or f"{kind}@example.com",
        display_name=kind.title(),
        password_hash=auth.hash_password("pw"),
    )


def _login(kind: str) -> tuple[TestClient, int]:
    """Find-or-create an actor of `kind` and return (test_client,
    actor_id) for tests that need to switch identities."""
    con = get_registry().platform_con()
    email = f"{kind}@example.com"
    existing = pdb.find_actor_by_email(con, email)
    actor_id = existing["id"] if existing else _create_actor(kind, email)
    c = TestClient(app)
    c.cookies.set(auth.SESSION_COOKIE, auth.create_session(actor_id))
    return c, actor_id


def _seed_two_reports() -> tuple[int, int]:
    """Insert one Elenchus + one baseline report. Returns
    (elenchus_id, baseline_id)."""
    con = get_registry().platform_con()
    e_id = pdb.record_study_report(
        con,
        session_id=1,
        condition="elenchus",
        content="# Domain\nE\n# Atomic statements\n1. E1.",
        generator_model="m",
        prompt_tokens=10,
        completion_tokens=5,
        cost_usd=0.01,
    )
    b_id = pdb.record_study_report(
        con,
        session_id=2,
        condition="baseline",
        content="# Domain\nB\n# Atomic statements\n1. B1.",
        generator_model="m",
        prompt_tokens=10,
        completion_tokens=5,
        cost_usd=0.01,
    )
    return e_id, b_id


# ─── Platform DB CRUD ────────────────────────────────────────────────


class TestPlatformDB:
    def test_create_and_find_package(self):
        con = get_registry().platform_con()
        e_id, b_id = _seed_two_reports()
        researcher = _create_actor("researcher")
        pid = pdb.create_judge_package(
            con,
            study_id="S",
            slot_a_report_id=e_id,
            slot_b_report_id=b_id,
            slot_a_condition="elenchus",
            slot_b_condition="baseline",
            created_by=researcher,
            notes="pilot",
        )
        row = pdb.find_judge_package(con, pid)
        assert row["study_id"] == "S"
        assert row["slot_a_report_id"] == e_id
        assert row["slot_a_condition"] == "elenchus"
        assert row["notes"] == "pilot"

    def test_assignment_lifecycle(self):
        con = get_registry().platform_con()
        e_id, b_id = _seed_two_reports()
        researcher = _create_actor("researcher")
        judge = _create_actor("judge")
        pid = pdb.create_judge_package(
            con,
            study_id="S",
            slot_a_report_id=e_id,
            slot_b_report_id=b_id,
            slot_a_condition="elenchus",
            slot_b_condition="baseline",
            created_by=researcher,
        )
        aid = pdb.create_judge_assignment(
            con, judge_actor_id=judge, package_id=pid, assigned_by=researcher
        )
        row = pdb.find_judge_assignment(con, aid)
        assert row["status"] == "pending"
        # Queue lists the assignment.
        queue = pdb.list_assignments_for_judge(con, judge, status="pending")
        assert len(queue) == 1
        # Mark completed.
        assert pdb.mark_assignment_completed(con, aid) is True
        # Idempotent on already-completed.
        assert pdb.mark_assignment_completed(con, aid) is False
        # Status updated.
        assert pdb.find_judge_assignment(con, aid)["status"] == "completed"

    def test_rating_stored_and_recovered(self):
        con = get_registry().platform_con()
        e_id, b_id = _seed_two_reports()
        researcher = _create_actor("researcher")
        judge = _create_actor("judge")
        pid = pdb.create_judge_package(
            con,
            study_id="S",
            slot_a_report_id=e_id,
            slot_b_report_id=b_id,
            slot_a_condition="elenchus",
            slot_b_condition="baseline",
            created_by=researcher,
        )
        aid = pdb.create_judge_assignment(
            con, judge_actor_id=judge, package_id=pid, assigned_by=researcher
        )
        rid = pdb.record_judge_rating(
            con,
            assignment_id=aid,
            ratings={"completeness": {"a": 5, "b": 6}},
            justification_a="solid",
            justification_b="more thorough",
            pairwise_winner="b",
            condition_guess_a="baseline",
            condition_guess_b="elenchus",
            confidence=4,
        )
        assert rid > 0
        row = pdb.find_rating_for_assignment(con, aid)
        assert row["pairwise_winner"] == "b"
        assert row["condition_guess_a"] == "baseline"
        assert row["confidence"] == 4
        assert row["ratings"] == {"completeness": {"a": 5, "b": 6}}


# ─── require_judge dependency ────────────────────────────────────────


class TestRequireJudge:
    def test_judge_passes(self):
        c, _ = _login("judge")
        r = c.get("/api/judge/queue")
        assert r.status_code == 200

    def test_admin_passes(self):
        c, _ = _login("admin")
        r = c.get("/api/judge/queue")
        assert r.status_code == 200

    def test_researcher_forbidden(self):
        c, _ = _login("researcher")
        r = c.get("/api/judge/queue")
        assert r.status_code == 403

    def test_user_forbidden(self):
        c, _ = _login("user")
        r = c.get("/api/judge/queue")
        assert r.status_code == 403

    def test_unauthenticated_unauthorized(self):
        c = TestClient(app)
        r = c.get("/api/judge/queue")
        assert r.status_code == 401


# ─── Researcher endpoints ────────────────────────────────────────────


class TestResearcherCreatesPackage:
    def test_happy_path_with_randomized_slots(self):
        # Run package creation many times with randomization on; both
        # slot configurations should appear over a meaningful sample.
        e_id, b_id = _seed_two_reports()
        c, _ = _login("researcher")
        seen_slot_a = set()
        for _ in range(40):
            r = c.post(
                "/api/admin/study/judge-packages",
                json={
                    "study_id": "S",
                    "report_id_elenchus": e_id,
                    "report_id_baseline": b_id,
                    "randomize_slots": True,
                },
            )
            assert r.status_code == 200
            seen_slot_a.add(r.json()["slot_a_condition"])
        # Probability of all-one-side over 40 trials with p=0.5 is
        # 2 * (0.5)**40 ≈ 1.8e-12 — vanishingly small.
        assert seen_slot_a == {"elenchus", "baseline"}, (
            f"randomization should hit both slots; got {seen_slot_a}"
        )

    def test_randomize_off_puts_elenchus_in_a(self):
        e_id, b_id = _seed_two_reports()
        c, _ = _login("researcher")
        r = c.post(
            "/api/admin/study/judge-packages",
            json={
                "study_id": "S",
                "report_id_elenchus": e_id,
                "report_id_baseline": b_id,
                "randomize_slots": False,
            },
        )
        assert r.status_code == 200
        assert r.json()["slot_a_condition"] == "elenchus"
        assert r.json()["slot_b_condition"] == "baseline"

    def test_wrong_condition_for_slot_400(self):
        e_id, b_id = _seed_two_reports()
        c, _ = _login("researcher")
        # Swap the IDs — claiming a baseline id is the Elenchus one.
        r = c.post(
            "/api/admin/study/judge-packages",
            json={
                "study_id": "S",
                "report_id_elenchus": b_id,  # actually baseline
                "report_id_baseline": e_id,  # actually elenchus
                "randomize_slots": False,
            },
        )
        assert r.status_code == 400

    def test_unknown_report_400(self):
        c, _ = _login("researcher")
        r = c.post(
            "/api/admin/study/judge-packages",
            json={
                "study_id": "S",
                "report_id_elenchus": 99999,
                "report_id_baseline": 99998,
            },
        )
        assert r.status_code == 400

    def test_non_researcher_forbidden(self):
        e_id, b_id = _seed_two_reports()
        c, _ = _login("user")
        r = c.post(
            "/api/admin/study/judge-packages",
            json={
                "study_id": "S",
                "report_id_elenchus": e_id,
                "report_id_baseline": b_id,
            },
        )
        assert r.status_code == 403


class TestResearcherAssignsPackage:
    def _make_package(self) -> int:
        e_id, b_id = _seed_two_reports()
        c, _ = _login("researcher")
        return c.post(
            "/api/admin/study/judge-packages",
            json={
                "study_id": "S",
                "report_id_elenchus": e_id,
                "report_id_baseline": b_id,
                "randomize_slots": False,
            },
        ).json()["id"]

    def test_creates_pending_assignment(self):
        pid = self._make_package()
        judge_id = _create_actor("judge")
        c, _ = _login("researcher")
        r = c.post(
            "/api/admin/study/judge-assignments",
            json={"judge_actor_id": judge_id, "package_id": pid},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["judge_actor_id"] == judge_id
        assert body["package_id"] == pid
        assert body["status"] == "pending"

    def test_judge_must_be_judge_kind(self):
        pid = self._make_package()
        c, _ = _login("researcher")
        # Try to assign to a regular user.
        user_id = _create_actor("user", email="u2@example.com")
        r = c.post(
            "/api/admin/study/judge-assignments",
            json={"judge_actor_id": user_id, "package_id": pid},
        )
        assert r.status_code == 400

    def test_unknown_package_400(self):
        c, _ = _login("researcher")
        judge_id = _create_actor("judge")
        r = c.post(
            "/api/admin/study/judge-assignments",
            json={"judge_actor_id": judge_id, "package_id": 99999},
        )
        assert r.status_code == 400


# ─── Judge endpoints ─────────────────────────────────────────────────


def _make_assigned_package(*, randomize: bool = False) -> tuple[int, int, int]:
    """Set up a package and assign it to a judge. Returns
    (judge_actor_id, package_id, assignment_id)."""
    e_id, b_id = _seed_two_reports()
    rc, _ = _login("researcher")
    pid = rc.post(
        "/api/admin/study/judge-packages",
        json={
            "study_id": "S",
            "report_id_elenchus": e_id,
            "report_id_baseline": b_id,
            "randomize_slots": randomize,
        },
    ).json()["id"]
    judge_id = _create_actor("judge")
    aid = rc.post(
        "/api/admin/study/judge-assignments",
        json={"judge_actor_id": judge_id, "package_id": pid},
    ).json()["id"]
    return judge_id, pid, aid


class TestJudgeQueue:
    def test_lists_only_my_assignments(self):
        judge_id, _, aid = _make_assigned_package()
        # Different judge — should NOT see this assignment.
        other_judge = _create_actor("judge", email="other-judge@example.com")
        c = TestClient(app)
        c.cookies.set(auth.SESSION_COOKIE, auth.create_session(other_judge))
        r = c.get("/api/judge/queue")
        assert r.status_code == 200
        assert r.json()["assignments"] == []

        # The actual judge sees their assignment.
        c2 = TestClient(app)
        c2.cookies.set(auth.SESSION_COOKIE, auth.create_session(judge_id))
        r2 = c2.get("/api/judge/queue")
        assert r2.status_code == 200
        assigns = r2.json()["assignments"]
        assert len(assigns) == 1
        assert assigns[0]["id"] == aid

    def test_filter_by_status_pending(self):
        judge_id, _, _ = _make_assigned_package()
        c = TestClient(app)
        c.cookies.set(auth.SESSION_COOKIE, auth.create_session(judge_id))
        r = c.get("/api/judge/queue?status=pending")
        assert r.status_code == 200
        assert len(r.json()["assignments"]) == 1


class TestJudgeViewAssignment:
    def test_returns_content_under_neutral_labels(self):
        judge_id, _, aid = _make_assigned_package(randomize=False)
        c = TestClient(app)
        c.cookies.set(auth.SESSION_COOKIE, auth.create_session(judge_id))
        r = c.get(f"/api/judge/assignments/{aid}")
        assert r.status_code == 200
        body = r.json()
        # Slot keys are 'slot_a' / 'slot_b' — no 'elenchus' / 'baseline'.
        assert set(body.keys()) >= {"slot_a", "slot_b"}
        # Body contains the content from each report.
        assert "E1" in body["slot_a"]["content"]
        assert "B1" in body["slot_b"]["content"]

    def test_response_never_leaks_condition(self):
        """Important blinding invariant — no field anywhere in the
        response identifies the condition behind a slot."""
        judge_id, _, aid = _make_assigned_package()
        c = TestClient(app)
        c.cookies.set(auth.SESSION_COOKIE, auth.create_session(judge_id))
        r = c.get(f"/api/judge/assignments/{aid}")

        def collect_strings(obj):
            if isinstance(obj, str):
                yield obj
            elif isinstance(obj, dict):
                for k, v in obj.items():
                    yield k
                    yield from collect_strings(v)
            elif isinstance(obj, list):
                for v in obj:
                    yield from collect_strings(v)

        body = r.json()
        all_strings = list(collect_strings(body))
        # Body must not contain the literal condition names anywhere
        # (the test reports themselves don't either — see _seed_two_reports
        # — so any leak would come from the route).
        for s in all_strings:
            lower = s.lower()
            assert "elenchus" not in lower, f"condition leaked via {s!r}"
            assert "baseline" not in lower, f"condition leaked via {s!r}"

    def test_other_judges_assignment_forbidden(self):
        judge_id, _, aid = _make_assigned_package()
        other_judge = _create_actor("judge", email="other-judge@example.com")
        c = TestClient(app)
        c.cookies.set(auth.SESSION_COOKIE, auth.create_session(other_judge))
        r = c.get(f"/api/judge/assignments/{aid}")
        assert r.status_code == 403
        _ = judge_id  # silence unused

    def test_unknown_assignment_404(self):
        c, _ = _login("judge")
        r = c.get("/api/judge/assignments/99999")
        assert r.status_code == 404

    def test_admin_can_view_any_assignment(self):
        judge_id, _, aid = _make_assigned_package()
        admin = _create_actor("admin", email="admin@example.com")
        c = TestClient(app)
        c.cookies.set(auth.SESSION_COOKIE, auth.create_session(admin))
        r = c.get(f"/api/judge/assignments/{aid}")
        assert r.status_code == 200
        _ = judge_id  # silence unused


class TestJudgeRating:
    def _full_rating_body(self) -> dict:
        return {
            "ratings": {
                "completeness": {"a": 5, "b": 6},
                "correctness": {"a": 4, "b": 5},
            },
            "justification_a": "Adequate but thin.",
            "justification_b": "More thorough.",
            "pairwise_winner": "b",
            "condition_guess_a": "baseline",
            "condition_guess_b": "elenchus",
            "confidence": 3,
        }

    def test_happy_path_stores_and_marks_completed(self):
        judge_id, _, aid = _make_assigned_package()
        c = TestClient(app)
        c.cookies.set(auth.SESSION_COOKIE, auth.create_session(judge_id))
        r = c.post(f"/api/judge/assignments/{aid}/rate", json=self._full_rating_body())
        assert r.status_code == 200, r.text
        # Assignment is now completed.
        con = get_registry().platform_con()
        assert pdb.find_judge_assignment(con, aid)["status"] == "completed"
        # Rating row exists with the right shape.
        rating = pdb.find_rating_for_assignment(con, aid)
        assert rating["pairwise_winner"] == "b"
        assert rating["ratings"]["completeness"] == {"a": 5, "b": 6}

    def test_pairwise_winner_validated(self):
        judge_id, _, aid = _make_assigned_package()
        c = TestClient(app)
        c.cookies.set(auth.SESSION_COOKIE, auth.create_session(judge_id))
        body = self._full_rating_body()
        body["pairwise_winner"] = "c"
        r = c.post(f"/api/judge/assignments/{aid}/rate", json=body)
        assert r.status_code == 400

    def test_confidence_validated(self):
        judge_id, _, aid = _make_assigned_package()
        c = TestClient(app)
        c.cookies.set(auth.SESSION_COOKIE, auth.create_session(judge_id))
        body = self._full_rating_body()
        body["confidence"] = 0
        r = c.post(f"/api/judge/assignments/{aid}/rate", json=body)
        assert r.status_code == 400

    def test_condition_guess_validated(self):
        judge_id, _, aid = _make_assigned_package()
        c = TestClient(app)
        c.cookies.set(auth.SESSION_COOKIE, auth.create_session(judge_id))
        body = self._full_rating_body()
        body["condition_guess_a"] = "experimental"
        r = c.post(f"/api/judge/assignments/{aid}/rate", json=body)
        assert r.status_code == 400

    def test_optional_condition_guess_allowed(self):
        judge_id, _, aid = _make_assigned_package()
        c = TestClient(app)
        c.cookies.set(auth.SESSION_COOKIE, auth.create_session(judge_id))
        body = self._full_rating_body()
        body["condition_guess_a"] = None
        body["condition_guess_b"] = None
        body["confidence"] = None
        r = c.post(f"/api/judge/assignments/{aid}/rate", json=body)
        assert r.status_code == 200

    def test_other_judges_assignment_forbidden(self):
        judge_id, _, aid = _make_assigned_package()
        other_judge = _create_actor("judge", email="other-judge@example.com")
        c = TestClient(app)
        c.cookies.set(auth.SESSION_COOKIE, auth.create_session(other_judge))
        r = c.post(f"/api/judge/assignments/{aid}/rate", json=self._full_rating_body())
        assert r.status_code == 403
        _ = judge_id

    def test_resubmission_overwrites_via_newer_row(self):
        """A judge can re-rate; analysis picks the newest row."""
        judge_id, _, aid = _make_assigned_package()
        c = TestClient(app)
        c.cookies.set(auth.SESSION_COOKIE, auth.create_session(judge_id))

        body = self._full_rating_body()
        body["pairwise_winner"] = "a"
        c.post(f"/api/judge/assignments/{aid}/rate", json=body)
        # Second submission with a different winner.
        body["pairwise_winner"] = "b"
        c.post(f"/api/judge/assignments/{aid}/rate", json=body)

        con = get_registry().platform_con()
        # find_rating returns the newest.
        assert pdb.find_rating_for_assignment(con, aid)["pairwise_winner"] == "b"


# ─── Blinding stress test ────────────────────────────────────────────


class TestBlindingMetadata:
    """Smoke check that the slot randomization actually produces
    roughly equal proportions over a representative sample. This is
    a property test — fails would indicate a stuck PRNG or wiring
    bug."""

    def test_slot_distribution_balanced(self):
        e_id, b_id = _seed_two_reports()
        c, _ = _login("researcher")
        elenchus_in_a = 0
        n = 200
        for _ in range(n):
            r = c.post(
                "/api/admin/study/judge-packages",
                json={
                    "study_id": "S",
                    "report_id_elenchus": e_id,
                    "report_id_baseline": b_id,
                    "randomize_slots": True,
                },
            )
            assert r.status_code == 200
            if r.json()["slot_a_condition"] == "elenchus":
                elenchus_in_a += 1
        # Binomial 99.9% CI for p=0.5, n=200: roughly [85, 115].
        # Use generous bounds to keep the test stable.
        assert 70 <= elenchus_in_a <= 130, (
            f"slot randomization looks unbalanced: {elenchus_in_a}/{n} in A"
        )
        _ = patch  # silence unused import in case the test runs alone
