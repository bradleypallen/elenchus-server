"""The text as the positum (design-notes/text-as-positum.md).

In the study flow the participant's first draft opens the dialogue, the
dialogue is locked until it exists, every later turn carries the current
draft (and the draft the model saw last time), and in the Elenchus
condition a speech act the opponent reads off the text is logged with
source 'text'. The ordinary interface is untouched.
"""

from __future__ import annotations

import contextlib
import json

import pytest
from fastapi.testclient import TestClient

from elenchus import auth, study_text
from elenchus import server as srv
from elenchus.db import get_registry
from elenchus.db import platform as pdb
from elenchus.llm_client import ChatCategory, ChatResult
from elenchus.server import app
from elenchus.turn_log import list_state_events, list_turns

POSITUM = (
    "Tides are the periodic rise and fall of sea level. They are driven mainly by the "
    "Moon's gravity, and less by the Sun's. Spring tides occur at new and full moon, when "
    "the two pull together. Neap tides occur at the quarters, when they pull across each "
    "other. The range of a tide also depends on the shape of the coast and the sea floor."
)
SHORT = "Tides rise and fall."


class RecordingClient:
    """A canned model that keeps every request it was shown. Answers the
    opponent with a JSON turn that may carry a text-sourced act."""

    model = "sim-canned"

    def __init__(self):
        self.requests: list[tuple[list[dict], str]] = []
        self.next_acts: list[dict] = []

    def _respond(self, messages, system) -> ChatResult:
        self.requests.append((messages, system or ""))
        if "prover-skeptic" in (system or ""):
            acts, self.next_acts = self.next_acts, []
            text = json.dumps(
                {"speech_acts": acts, "new_tensions": [], "response": "Noted. Go on."}
            )
        else:
            text = "Here is a suggestion for your second paragraph."
        return ChatResult(
            category=ChatCategory.SUCCESS,
            text=text,
            attempts=1,
            latency_ms=1,
            prompt_tokens=10,
            completion_tokens=5,
            model=self.model,
        )

    def chat(self, messages, *, system=None, max_tokens=2000, model=None):
        return self._respond(messages, system)

    async def achat(self, messages, *, system=None, max_tokens=2000, model=None):
        return self._respond(messages, system)

    def last_user_content(self) -> str:
        messages, _ = self.requests[-1]
        return messages[-1]["content"]


@pytest.fixture(autouse=True)
def _clean():
    reg = get_registry()
    reg.migrate_platform()
    con = reg.platform_con()
    with reg.platform_lock:
        for table in (
            "study_texts",
            "participant_session_tokens",
            "study_participants",
            "study_configs",
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
    yield


@pytest.fixture
def llm(monkeypatch):
    client = RecordingClient()
    monkeypatch.setattr(srv.opponent, "_llm_client", client)
    return client


def _con():
    return get_registry().platform_con()


def _participant(condition: str) -> tuple[TestClient, dict]:
    """A participant in the task state, with the task base created."""
    researcher_id = pdb.create_actor(
        _con(), kind="researcher", email="r@example.com", display_name="R", password_hash="h"
    )
    researcher = TestClient(app)
    researcher.cookies.set(auth.SESSION_COOKIE, auth.create_session(researcher_id))
    researcher.put(
        "/api/admin/study/P/config",
        json={"topic_a_title": "Tides", "topic_b_title": "Clouds", "min_gap_hours": 0},
    )
    token = researcher.post(
        "/api/admin/study/tokens",
        json={"study_id": "P", "condition": condition, "display_name": "Pat"},
    ).json()["token"]
    p = TestClient(app)
    assert p.post(f"/api/study/{token}").status_code == 200
    return p, {"researcher": researcher}


def _in_task(p: TestClient) -> str:
    p.post("/api/study/session/begin-tutorial")
    body = p.post("/api/study/session/begin-task").json()
    return body["task_base_id"]


def _save(p: TestClient, text: str) -> None:
    assert p.put("/api/study/session/text", json={"content": text}).status_code == 200


class TestTheGate:
    def test_no_dialogue_before_the_first_draft(self, llm):
        p, _ = _participant("elenchus")
        base = _in_task(p)
        session = p.get("/api/study/session").json()
        assert session["positum_done"] is False
        assert session["positum_min"] == {"words": 50, "sentences": 3}
        r = p.post(f"/api/dialectics/{base}/message", json={"message": "hello"})
        assert r.status_code == 409 and r.json()["detail"]["positum_required"] is True
        assert llm.requests == []

    def test_a_short_draft_is_refused(self, llm):
        p, _ = _participant("elenchus")
        _in_task(p)
        _save(p, SHORT)
        r = p.post("/api/study/session/positum")
        assert r.status_code == 422 and r.json()["detail"]["positum_short"] is True
        assert "50 words" in r.json()["detail"]["user_message"]
        assert llm.requests == []

    def test_the_tutorial_has_a_lower_bar(self, llm):
        p, _ = _participant("elenchus")
        p.post("/api/study/session/begin-tutorial")
        assert p.get("/api/study/session").json()["positum_min"] == {"words": 20, "sentences": 2}
        _save(p, "Clouds are water. They float.")
        assert p.post("/api/study/session/positum").status_code == 200

    def test_outside_the_working_states(self, llm):
        p, _ = _participant("elenchus")
        r = p.post("/api/study/session/positum")  # still at the briefing
        assert r.status_code == 409
        assert p.get("/api/study/session").json()["positum_done"] is None


class TestThePositum:
    def test_opens_the_dialogue_from_the_draft(self, llm):
        p, _ = _participant("elenchus")
        base = _in_task(p)
        _save(p, POSITUM)
        r = p.post("/api/study/session/positum")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["response"] == "Noted. Go on." and body["condition"] == "elenchus"
        assert body["session"]["positum_done"] is True
        # The model was shown the draft as the positum, with the state.
        content = llm.last_user_content()
        assert "RESPONDENT'S FIRST DRAFT (the positum" in content and POSITUM in content
        assert "CURRENT DRAFT" not in content
        # Recorded: a positum snapshot, the turn's draft id, the draft in request_content.
        handle = get_registry().get_handle(base)
        bcon = handle.state.base.con
        snap = study_text.positum_snapshot(bcon)
        assert snap and snap["content"] == POSITUM
        (turn,) = list_turns(bcon)
        assert turn["draft_snapshot_id"] == snap["id"] and POSITUM in turn["request_content"]
        # The positum is the first user message of the conversation.
        conversation = handle.state.get_conversation()
        assert conversation[0]["role"] == "user" and conversation[0]["content"] == POSITUM
        # Once only.
        assert p.post("/api/study/session/positum").status_code == 409

    def test_later_turns_carry_the_current_and_previous_drafts(self, llm):
        p, _ = _participant("elenchus")
        base = _in_task(p)
        _save(p, POSITUM)
        assert p.post("/api/study/session/positum").status_code == 200
        # Unchanged draft: the model is told so.
        r = p.post(f"/api/dialectics/{base}/message", json={"message": "What next?"})
        assert r.status_code == 200, r.text
        content = llm.last_user_content()
        assert "CURRENT DRAFT (the respondent's introduction as it stands)" in content
        assert "(unchanged since your last turn)" in content
        assert 'RESPONDENT SAYS: "What next?"' in content
        # Changed draft: both versions are shown.
        revised = POSITUM + " Tidal bores occur in some estuaries."
        _save(p, revised)
        r = p.post(f"/api/dialectics/{base}/message", json={"message": "I added a sentence."})
        assert r.status_code == 200
        content = llm.last_user_content()
        assert "PREVIOUS DRAFT (as of your last turn)" in content
        assert "Tidal bores" in content.split("PREVIOUS DRAFT")[0]
        assert "Tidal bores" not in content.split("PREVIOUS DRAFT")[1]
        bcon = get_registry().get_handle(base).state.base.con
        turns = list_turns(bcon)
        ids = [t["draft_snapshot_id"] for t in turns]
        assert len(ids) == 3 and ids[0] == ids[1] and ids[2] > ids[1]

    def test_the_baseline_sees_the_draft_too(self, llm):
        p, _ = _participant("baseline")
        base = _in_task(p)
        _save(p, POSITUM)
        r = p.post("/api/study/session/positum")
        assert r.status_code == 200 and r.json()["condition"] == "baseline"
        content = llm.last_user_content()
        assert (
            content.startswith("Here is my first draft of the introduction:")
            and POSITUM in content
        )
        assert "prover-skeptic" not in llm.requests[-1][1]
        _save(p, POSITUM + " More.")
        r = p.post(f"/api/dialectics/{base}/message", json={"message": "Tighten paragraph two?"})
        assert r.status_code == 200
        content = llm.last_user_content()
        assert content.startswith("MY DRAFT AS IT STANDS (changed since your last reply):")
        assert content.endswith("Tighten paragraph two?")

    def test_an_act_read_off_the_text_is_logged_as_such(self, llm):
        p, _ = _participant("elenchus")
        base = _in_task(p)
        _save(p, POSITUM)
        llm.next_acts = [{"type": "COMMIT", "proposition": "Tides are driven mainly by the Moon"}]
        assert p.post("/api/study/session/positum").status_code == 200
        _save(p, POSITUM + " Tidal bores occur in some estuaries.")
        llm.next_acts = [
            {
                "type": "COMMIT",
                "proposition": "Tidal bores occur in some estuaries",
                "source": "text",
            },
            {"type": "COMMIT", "proposition": "The Sun matters less than the Moon"},
        ]
        assert (
            p.post(
                f"/api/dialectics/{base}/message", json={"message": "See the new line."}
            ).status_code
            == 200
        )
        bcon = get_registry().get_handle(base).state.base.con
        by_source = {
            (e["payload"].get("proposition"), e["source"])
            for e in list_state_events(bcon)
            if e["event_type"] == "COMMIT"
        }
        assert ("Tidal bores occur in some estuaries", "text") in by_source
        assert ("The Sun matters less than the Moon", "opponent") in by_source
        assert ("Tides are driven mainly by the Moon", "opponent") in by_source
        state = get_registry().get_handle(base).state.to_dict()
        assert "Tidal bores occur in some estuaries" in state["commitments"]

    def test_the_task_clock_still_rules(self, llm, monkeypatch):
        p, _ = _participant("elenchus")
        _in_task(p)
        _save(p, POSITUM)
        monkeypatch.setattr(srv, "_time_is_up", lambda session: True)
        r = p.post("/api/study/session/positum")
        assert r.status_code == 409 and r.json()["detail"]["task_ended"] is True


class TestTheOrdinaryInterface:
    def test_carries_no_draft(self, llm):
        uid = pdb.create_actor(
            _con(), kind="user", email="u@example.com", display_name="U", password_hash="h"
        )
        u = TestClient(app)
        u.cookies.set(auth.SESSION_COOKIE, auth.create_session(uid))
        assert u.post("/api/sessions", json={"name": "mine"}).status_code == 200
        r = u.post("/api/dialectics/mine/message", json={"message": "Tides follow the Moon."})
        assert r.status_code == 200, r.text
        content = llm.last_user_content()
        assert "DRAFT" not in content and 'RESPONDENT SAYS: "Tides follow the Moon."' in content
        (turn,) = list_turns(get_registry().get_handle("mine").state.base.con)
        assert turn["draft_snapshot_id"] is None
