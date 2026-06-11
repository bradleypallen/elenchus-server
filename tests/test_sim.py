"""Tests for the agent-driven pilot simulation (scripted driver).

The scripted run is the CI regression net: if any role's end-to-end
flow breaks, this test fails. It runs the entire study — researcher →
participants (both conditions) → judges → export — against the real
HTTP API with the LLM stubbed at the network boundary, so the whole
server stack executes for free and deterministically.

`run_simulation` re-points the process-wide registry at its own
isolated temp dir; the fixture restores the shared conftest registry
afterward so the rest of the suite is unaffected.
"""

from __future__ import annotations

import os

import pytest

from elenchus.db import get_registry, init_registry
from elenchus.sim import run_simulation


@pytest.fixture(autouse=True)
def _restore_registry():
    original = os.environ["ELENCHUS_DATA"]
    yield
    # The sim re-inits the registry to a temp dir; restore the shared one.
    init_registry(original)
    get_registry().migrate_platform()


class TestScriptedSimulation:
    def test_full_pilot_passes(self):
        report = run_simulation(driver_mode="scripted", participants=2, judges=2)
        # The headline invariant: a clean run.
        assert report.ok, f"simulation found problems: {report.problems}"
        assert report.problems == []

    def test_all_participants_complete_both_conditions(self):
        report = run_simulation(driver_mode="scripted", participants=3, judges=2)
        assert report.participants_total == 3
        assert report.participants_completed == 3

    def test_reports_and_ratings_produced(self):
        report = run_simulation(driver_mode="scripted", participants=2, judges=2)
        # 2 participants × 2 conditions = 4 reports.
        assert report.reports_generated == 4
        # 2 packages (one per participant) × 2 judges = 4 ratings.
        assert report.ratings_submitted == 4

    def test_every_role_is_exercised(self):
        report = run_simulation(driver_mode="scripted", participants=2, judges=2)
        actors = {s.actor for s in report.steps}
        assert "researcher" in actors
        assert any(a.startswith("P-001") for a in actors)
        assert "J-001" in actors
        # And the key actions all appear and succeeded.
        actions = {s.action for s in report.steps if s.ok}
        for needed in (
            "issue_token",
            "consume_token",
            "begin_tutorial",
            "begin_task",
            "task_turn",
            "accept_tension",
            "submit_survey",
            "generate_report",
            "create_package",
            "assign_judge",
            "view_assignment",
            "submit_rating",
            "export_study",
        ):
            assert needed in actions, f"action {needed!r} never succeeded"

    def test_condition_routing_both_paths(self):
        """Both conditions' message turns must succeed — proving the
        baseline-vs-dialectic routing in the message route works for a
        participant in each condition."""
        report = run_simulation(driver_mode="scripted", participants=2, judges=2)
        task_turns = [s for s in report.steps if s.action == "task_turn"]
        # Every participant ran task turns in both conditions.
        assert all(s.ok for s in task_turns)
        elenchus_turns = [s for s in task_turns if "task-" in s.path]
        assert len(elenchus_turns) >= 4  # 2 participants × 2 conditions × ≥1 turn

    def test_blinding_recorded(self):
        report = run_simulation(driver_mode="scripted", participants=2, judges=2)
        # 2 packages × 2 judges × 2 slots = 8 blinding observations.
        assert report.blinding_total == 8
        # Scripted judges always guess 'unsure'.
        assert report.blinding_unsure == 8

    def test_scripted_run_is_free(self):
        """The canned model isn't in the pricing table, so scripted runs
        record $0 — confirming no real LLM spend. The report now carries
        the usage figures (queried before the sim registry is torn down),
        so we can assert it directly."""
        report = run_simulation(driver_mode="scripted", participants=2, judges=2)
        assert report.cost_usd == 0.0
        # The canned client DID drive the whole stack — calls were made and
        # all succeeded — they just priced to nothing.
        assert report.llm_calls > 0
        assert report.llm_calls_ok == report.llm_calls

    def test_problems_surface_does_not_abort(self):
        """A clean run has an empty problems list and ok=True; this is the
        contract the CI gate relies on."""
        report = run_simulation(driver_mode="scripted", participants=1, judges=1)
        assert isinstance(report.problems, list)
        assert report.ok
        # Single participant, single judge → 1 package × 1 judge = 1 rating.
        assert report.ratings_submitted == 1


class TestAccessProbes:
    """The adversarial access/auth phase: every protected route must
    reject unauthorized access with the right status, and the judge view
    must never leak the condition. A failed probe is also a problem, so
    `report.ok` already gates on these — but we assert the specifics so a
    regression names the exact control that broke."""

    def test_all_access_probes_pass(self):
        report = run_simulation(driver_mode="scripted", participants=2, judges=2)
        assert report.access_probes_total > 0
        assert report.access_probes_passed == report.access_probes_total
        assert report.ok

    def test_critical_controls_exercised_and_held(self):
        report = run_simulation(driver_mode="scripted", participants=2, judges=2)
        passed = {s.action for s in report.steps if s.is_probe and s.ok}
        # Each of these is a security control that must hold for the pilot.
        for control in (
            "login_bad_password",  # wrong password rejected
            "me_unauth",  # no session → 401
            "admin_unauth",  # unauth admin route → 401 (not 403)
            "admin_gate",  # non-admin → 403
            "judge_gate",  # non-judge → 403
            "cross_tenant_read",  # one user can't read another's base (404)
            "cross_tenant_write",
            "revoked_token_reuse",  # logged-out token is dead
            "signup_reuse_token",  # invites are single-use
            "study_token_reuse",  # participant links are single-use (410)
            "blinding_no_leak",  # judge view hides condition
            "judge_view_foreign",  # a judge can't open another's assignment
        ):
            assert control in passed, f"access control {control!r} did not hold"

    def test_blinding_leak_is_a_hard_failure(self):
        """If the judge view ever exposed the ground-truth condition, the
        blinding_no_leak probe would fail and pull report.ok to False —
        guard that the check is actually wired into the gate."""
        report = run_simulation(driver_mode="scripted", participants=2, judges=2)
        leak_checks = [s for s in report.steps if s.action == "blinding_no_leak"]
        assert leak_checks, "blinding leak check never ran"
        assert all(s.ok for s in leak_checks)
