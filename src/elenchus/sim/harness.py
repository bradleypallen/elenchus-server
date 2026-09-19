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
from .driver import SIM_TOPICS
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
        self.researcher: SimClient | None = None  # set in run()
        # Outcome tracking: label → condition → {session_id, text_submitted}
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
        self.researcher = researcher  # retained for the access-probe phase

        # Study setup: the two topics every participant meets (one per
        # condition), and no waiting time between a participant's two
        # sessions — the sim runs them back to back.
        researcher.request(
            "PUT",
            f"/api/admin/study/{self.study_id}/config",
            json={
                "topic_a_title": SIM_TOPICS["A"],
                "topic_a_brief": f"Introduce {SIM_TOPICS['A']} to a colleague new to it.",
                "topic_b_title": SIM_TOPICS["B"],
                "topic_b_brief": f"Introduce {SIM_TOPICS['B']} to a colleague new to it.",
                "min_gap_hours": 0,
            },
            action="study_config",
        )

        for persona in self.participants:
            self.outcomes[persona.label] = {}
            # Enrolment allocates the participant's cell (which condition
            # and topic come first) and issues both of their links.
            st, enrolled = researcher.post(
                f"/api/admin/study/{self.study_id}/participants",
                json={"display_name": persona.label},
                action="enrol",
                note=persona.label,
            )
            if st != 200:
                continue
            first, second = enrolled["sessions"]
            # The second link must stay shut until the first session is done.
            SimClient(f"{persona.label}/early", self.app, self.rec).probe(
                "POST",
                f"/api/study/{second['token']}",
                action="second_link_early_probe",
                expect=409,
                note="second session can't start before the first",
            )
            for planned in (first, second):
                try:
                    self._run_session(persona, planned)
                except Exception:
                    logger.exception(
                        "Session crashed: %s / %s", persona.label, planned["condition"]
                    )

        try:
            self._run_judging(researcher)
        except Exception:
            logger.exception("Judging phase crashed")

        try:
            researcher.post(f"/api/admin/study/{self.study_id}/export", action="export_study")
        except Exception:
            logger.exception("Export crashed")

        # Adversarial access/auth phase last, so it can reuse the real
        # assignments the run produced. Runs after export so its
        # throwaway probe users never land in the study archive.
        try:
            from .access import run_access_probes

            run_access_probes(self)
        except Exception:
            logger.exception("Access-probe phase crashed")

        return self.rec

    # ── One participant session, one condition ──

    def _run_session(self, persona: ParticipantPersona, planned: dict):
        """One of a participant's two sessions, as enrolment planned it
        (`planned` is a roster session row: token, condition, topic)."""
        label = persona.label
        cond = planned["condition"]
        token = planned["token"]

        # 2. Participant opens their link (sets the session cookie).
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

        # 7. The writing pane: an autosaved draft, a paste event, then
        # the submitted text. The text is the judged artifact, so the
        # task can't be left without one — probe that first.
        text = self.driver.participant_text(persona, cond, state, topic=planned["topic_title"])
        participant.put(
            "/api/study/session/text",
            json={"content": text[: len(text) // 2], "trigger": "autosave"},
            action="autosave_text",
        )
        participant.post(
            "/api/study/session/text/events",
            json={"events": [{"type": "paste", "length": 42}]},
            action="editor_events",
        )
        participant.probe(
            "POST",
            "/api/study/session/advance",
            json={"to_state": "post_session"},
            action="skip_text_probe",
            expect=400,
            note="can't leave the task without submitting a text",
        )
        # active → post_session (via finish) → surveyed.
        finish_status, _ = participant.post(
            "/api/study/session/finish",
            json={"content": text},
            action="finish",
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

        # The submitted text is what the panel rates; the LLM-generated
        # structured report of the old design is no longer part of the
        # flow (and would cost an LLM call per session in `--driver llm`).
        self.outcomes[label][cond] = {
            "session_id": session_id,
            "text_submitted": finish_status == 200,
        }

    # ── Judging ──

    def _run_judging(self, researcher: SimClient):
        """The panel rates the submitted texts: every judge gets every
        text (in their own random order), sees it blinded, and gives
        absolute ratings on the rubric's four dimensions."""
        judge_clients = [self._make_staff("judge", j.label) for j in self.judges]

        researcher.get(f"/api/admin/study/{self.study_id}/texts", action="list_texts")

        for jc, jp in zip(judge_clients, self.judges, strict=False):
            researcher.post(
                f"/api/admin/study/{self.study_id}/text-assignments",
                json={"judge_actor_id": jc._actor_id},
                action="assign_texts",
                note=jp.label,
            )
            st, queue = jc.get("/api/judge/texts", action="judge_queue")
            for item in (queue or {}).get("assignments", []):
                aid = item["assignment_id"]
                st, view = jc.get(f"/api/judge/texts/{aid}", action="view_text")
                if st != 200:
                    continue
                rating = self.driver.judge_text_rating(jp, view)
                jc.post(
                    f"/api/judge/texts/{aid}/rate",
                    json=rating,
                    action="rate_text",
                    note=jp.label,
                )
                # Blinding outcome. The ground truth comes from the
                # platform DB — it is, by design, nowhere in what the
                # judge was sent.
                truth = (
                    get_registry()
                    .platform_con()
                    .execute(
                        "SELECT x.condition FROM text_assignments a "
                        "JOIN study_texts x ON x.id = a.text_id WHERE a.id = ?",
                        [aid],
                    )
                    .fetchone()
                )
                if truth is not None:
                    self.blinding.append(
                        {"guess": rating.get("condition_guess"), "truth": truth[0]}
                    )
