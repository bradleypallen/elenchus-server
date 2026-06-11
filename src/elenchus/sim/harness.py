"""
harness.py — the study orchestrator.

Drives the full pilot end-to-end against the real HTTP API:
  researcher issues tokens → each participant walks both conditions'
  state machines with driver-supplied turns → reports are generated →
  judge packages are assembled and assigned → judges rate → the study
  is exported.

The harness never aborts on an individual failure. A wedged session is
recorded and skipped so a single run surfaces *all* the rough edges,
not just the first — which is the point of a robustness harness.
"""

from __future__ import annotations

import logging

from .. import auth
from ..db import get_registry
from ..db import platform as pdb
from .client import Recorder, SimClient
from .personas import JudgePersona, ParticipantPersona

logger = logging.getLogger(__name__)


class StudyHarness:
    def __init__(
        self,
        app,
        driver,
        *,
        participants: list[ParticipantPersona],
        judges: list[JudgePersona],
        study_id: str = "SIM",
        recorder: Recorder | None = None,
    ):
        self.app = app
        self.driver = driver
        self.participants = participants
        self.judges = judges
        self.study_id = study_id
        self.rec = recorder or Recorder()
        # Outcome tracking: label → condition → {session_id, report_id}
        self.outcomes: dict[str, dict[str, dict]] = {}
        # Blinding analysis rows: {guess, truth} per slot.
        self.blinding: list[dict] = []

    # ── Actor setup (out-of-band; not HTTP) ──

    def _make_staff(self, kind: str, label: str) -> SimClient:
        con = get_registry().platform_con()
        actor_id = pdb.create_actor(
            con,
            kind=kind,
            email=f"{label}@sim.local",
            display_name=label,
            password_hash=None,
        )
        c = SimClient(label, self.app, self.rec)
        c.set_session_cookie(auth.create_session(actor_id))
        c._actor_id = actor_id  # type: ignore[attr-defined]
        return c

    # ── Run ──

    def run(self) -> Recorder:
        researcher = self._make_staff("admin", "researcher")

        for i, persona in enumerate(self.participants):
            # Counterbalance condition order across participants.
            order = ["elenchus", "baseline"] if i % 2 == 0 else ["baseline", "elenchus"]
            self.outcomes[persona.label] = {}
            for cond in order:
                try:
                    self._run_session(researcher, persona, cond)
                except Exception:
                    logger.exception("Session crashed: %s / %s", persona.label, cond)

        try:
            self._run_judging(researcher)
        except Exception:
            logger.exception("Judging phase crashed")

        try:
            researcher.post(f"/api/admin/study/{self.study_id}/export", action="export_study")
        except Exception:
            logger.exception("Export crashed")

        return self.rec

    # ── One participant session, one condition ──

    def _run_session(self, researcher: SimClient, persona: ParticipantPersona, cond: str):
        label = persona.label
        # 1. Researcher issues the token.
        st, body = researcher.post(
            "/api/admin/study/tokens",
            json={"study_id": self.study_id, "condition": cond, "display_name": label},
            action="issue_token",
            note=f"{label}/{cond}",
        )
        if st != 200:
            return
        token = body["token"]

        # 2. Participant consumes it (sets the session cookie).
        participant = SimClient(f"{label}/{cond}", self.app, self.rec)
        st, body = participant.post(f"/api/study/{token}", action="consume_token")
        if st != 200:
            return
        session_id = body["session_id"]

        # 3. briefing → tutorial.
        participant.get("/api/study/session", action="get_session", note="briefing")
        st, body = participant.post("/api/study/session/begin-tutorial", action="begin_tutorial")
        if st != 200:
            return
        practice_base = body["practice_base_id"]

        # 4. One tutorial turn (warm-up).
        participant.post(
            f"/api/dialectics/{practice_base}/message",
            json={"message": self.driver.participant_tutorial_message(persona)},
            action="tutorial_turn",
        )

        # 5. tutorial → active (creates + attaches the task base).
        st, body = participant.post("/api/study/session/begin-task", action="begin_task")
        if st != 200:
            return
        task_base = body["task_base_id"]

        # 6. Task turns.
        state = {}
        st, body = participant.post(
            f"/api/dialectics/{task_base}/message",
            json={"message": self.driver.participant_task_message(persona, cond, 0, state)},
            action="task_turn",
            note="turn 1",
        )
        if st == 200 and body:
            state = body.get("state", {})

        # 6b. Elenchus only: accept the focal tension (two-phase flow).
        if cond == "elenchus":
            focal = (state.get("tensions") or [None])[0]
            if focal:
                participant.post(
                    f"/api/dialectics/{task_base}/tensions/{focal['id']}",
                    json={"action": "accept"},
                    action="accept_tension",
                )
                participant.post(
                    f"/api/dialectics/{task_base}/message",
                    json={"message": "I accept that tension."},
                    action="task_turn",
                    note="accept follow-up",
                )

        # 6c. One more task turn.
        participant.post(
            f"/api/dialectics/{task_base}/message",
            json={"message": self.driver.participant_task_message(persona, cond, 1, state)},
            action="task_turn",
            note="turn 2",
        )

        # 7. active → post_session → surveyed.
        participant.post(
            "/api/study/session/advance",
            json={"to_state": "post_session"},
            action="advance",
            note="post_session",
        )
        participant.post(
            "/api/study/session/advance",
            json={"to_state": "surveyed"},
            action="advance",
            note="surveyed",
        )

        # 8. Questionnaires.
        st, body = participant.get("/api/study/instruments", action="get_instruments")
        instruments = [i["instrument"] for i in (body or {}).get("instruments", [])]
        for inst in instruments:
            participant.post(
                f"/api/study/session/{session_id}/survey",
                json={"instrument": inst, "responses": self.driver.survey_response(inst)},
                action="submit_survey",
                note=inst,
            )

        # 9. surveyed → complete.
        participant.post(
            "/api/study/session/advance",
            json={"to_state": "complete"},
            action="advance",
            note="complete",
        )

        # 10. Researcher generates the structured report.
        st, body = researcher.post(
            f"/api/study/session/{session_id}/generate-report",
            action="generate_report",
            note=f"{label}/{cond}",
        )
        report_id = body.get("id") if st == 200 and body else None
        self.outcomes[label][cond] = {"session_id": session_id, "report_id": report_id}

    # ── Judging ──

    def _run_judging(self, researcher: SimClient):
        judge_clients = [self._make_staff("judge", j.label) for j in self.judges]

        for label, conds in self.outcomes.items():
            e = conds.get("elenchus", {}).get("report_id")
            b = conds.get("baseline", {}).get("report_id")
            if not (e and b):
                continue  # both reports must exist to pair them
            st, pkg = researcher.post(
                "/api/admin/study/judge-packages",
                json={
                    "study_id": self.study_id,
                    "report_id_elenchus": e,
                    "report_id_baseline": b,
                },
                action="create_package",
                note=label,
            )
            if st != 200:
                continue
            # Truth map for the blinding analysis.
            truth = {
                "a": pkg["slot_a_condition"],
                "b": pkg["slot_b_condition"],
            }
            for jc, jp in zip(judge_clients, self.judges, strict=False):
                st, asg = researcher.post(
                    "/api/admin/study/judge-assignments",
                    json={"package_id": pkg["id"], "judge_actor_id": jc._actor_id},
                    action="assign_judge",
                    note=f"{label}→{jp.label}",
                )
                if st != 200:
                    continue
                aid = asg["id"]
                st, view = jc.get(f"/api/judge/assignments/{aid}", action="view_assignment")
                if st != 200:
                    continue
                rating = self.driver.judge_rating(
                    jp,
                    view["slot_a"]["content"],
                    view["slot_b"]["content"],
                )
                jc.post(
                    f"/api/judge/assignments/{aid}/rate",
                    json=rating,
                    action="submit_rating",
                    note=f"{jp.label} {label}",
                )
                # Record blinding outcome for both slots.
                for slot in ("a", "b"):
                    self.blinding.append(
                        {"guess": rating.get(f"condition_guess_{slot}"), "truth": truth[slot]}
                    )
