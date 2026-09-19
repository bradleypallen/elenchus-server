"""Tests for the writing pane's backend: the topic a session carries,
the autosaved draft history, editor events (paste = length only), the
soft timer's data, and finishing the task by submitting the text — the
study's judged artifact.
"""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from elenchus import auth, study_text, turn_log
from elenchus.db import get_registry
from elenchus.db import platform as pdb
from elenchus.dialectical_state import DialecticalState
from elenchus.llm_client import ChatCategory, ChatResult
from elenchus.opponent import BASELINE_SYSTEM_PROMPT, Opponent, baseline_system_prompt
from elenchus.server import app

client = TestClient(app)

TOPIC = "Occurrence and its relatives in Darwin Core"
BRIEF = "Define Occurrence, Organism, Event and MaterialSample and how they relate."


@pytest.fixture(autouse=True)
def _clean():
    reg = get_registry()
    reg.migrate_platform()
    con = reg.platform_con()
    with reg.platform_lock:
        for table in (
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


def _participant(condition: str = "elenchus", **token_fields) -> tuple[TestClient, dict]:
    """Issue a token as a researcher, consume it as a participant."""
    con = get_registry().platform_con()
    rid = pdb.create_actor(
        con,
        kind="researcher",
        email="r@example.com",
        display_name="R",
        password_hash=auth.hash_password("pw"),
    )
    client.cookies.set(auth.SESSION_COOKIE, auth.create_session(rid))
    r = client.post(
        "/api/admin/study/tokens",
        json={"study_id": "S", "condition": condition, "display_name": "P", **token_fields},
    )
    assert r.status_code == 200, r.text
    client.cookies.clear()
    pclient = TestClient(app)
    r = pclient.post(f"/api/study/{r.json()['token']}")
    assert r.status_code == 200, r.text
    return pclient, r.json()


def _at_task(condition: str = "elenchus", **token_fields) -> tuple[TestClient, dict]:
    pclient, body = _participant(condition, **token_fields)
    assert pclient.post("/api/study/session/begin-tutorial").status_code == 200
    r = pclient.post("/api/study/session/begin-task")
    assert r.status_code == 200, r.text
    return pclient, {**body, **r.json()}


def _base_con(base_id: str):
    return get_registry().get(base_id).base.con


# ── Topic ────────────────────────────────────────────────────────────


class TestTopic:
    def test_session_payload_carries_the_writing_task(self):
        pclient, _ = _participant(topic_title=TOPIC, topic_brief=BRIEF)
        s = pclient.get("/api/study/session").json()
        assert (s["topic_title"], s["topic_brief"]) == (TOPIC, BRIEF)
        assert s["practice_topic_title"].startswith("Practice")
        assert s["text_submitted"] is False

    def test_task_base_is_named_after_the_topic(self):
        """That name heads the state the Elenchus opponent is shown."""
        _, body = _at_task(topic_title=TOPIC)
        assert get_registry().get(body["task_base_id"]).base.name == TOPIC

    def test_no_topic_falls_back(self):
        pclient, body = _at_task()
        assert get_registry().get(body["task_base_id"]).base.name == "Study task"
        assert pclient.get("/api/study/session").json()["topic_title"] == ""

    def test_tokens_listing_shows_topic(self):
        _participant(topic_title=TOPIC)
        rows = pdb.list_participant_tokens(get_registry().platform_con(), study_id="S")
        assert rows[0]["topic_title"] == TOPIC

    def test_baseline_prompt_carries_the_topic(self):
        assert baseline_system_prompt("") == BASELINE_SYSTEM_PROMPT
        assert baseline_system_prompt("Study task") == BASELINE_SYSTEM_PROMPT
        assert baseline_system_prompt(TOPIC).endswith(f"THE EXPERT'S TOPIC: {TOPIC}")
        assert "transcript itself is the deliverable" not in BASELINE_SYSTEM_PROMPT

    def test_baseline_turn_fingerprints_the_prompt_actually_sent(self):
        opp = Opponent(api_key="fake-key")
        state = DialecticalState.in_memory(TOPIC)
        ok = ChatResult(category=ChatCategory.SUCCESS, text="Sure.", model="m")
        achat = AsyncMock(return_value=ok)
        with patch.object(opp._llm_client, "achat", new=achat):
            asyncio.run(opp.async_baseline_respond("help me outline", state))
        assert achat.call_args.kwargs["system"] == baseline_system_prompt(TOPIC)
        (turn,) = turn_log.list_turns(state.base.con)
        assert turn["system_prompt_sha256"] == turn_log.prompt_fingerprint(
            baseline_system_prompt(TOPIC)
        )


# ── Timer data ───────────────────────────────────────────────────────


class TestTimer:
    def test_defaults_to_sixty_minutes_with_two_soft_warnings(self):
        pclient, _ = _at_task()
        s = pclient.get("/api/study/session").json()
        assert (s["task_minutes"], s["soft_warning_minutes"]) == (60, [50, 60])
        assert 0 <= s["state_elapsed_seconds"] < 30

    def test_task_minutes_override_for_training_runs(self, monkeypatch):
        monkeypatch.setenv("ELENCHUS_TASK_MINUTES", "8")
        pclient, _ = _at_task()
        s = pclient.get("/api/study/session").json()
        assert (s["task_minutes"], s["soft_warning_minutes"]) == (8, [1, 8])

    def test_garbage_override_is_ignored(self, monkeypatch):
        monkeypatch.setenv("ELENCHUS_TASK_MINUTES", "soon")
        pclient, _ = _at_task()
        assert pclient.get("/api/study/session").json()["task_minutes"] == 60


# ── Drafts ───────────────────────────────────────────────────────────


class TestDrafts:
    def test_empty_before_anything_is_written(self):
        pclient, _ = _at_task()
        assert pclient.get("/api/study/session/text").json() == {
            "content": "",
            "word_count": 0,
            "saved_at": None,
        }

    def test_autosave_appends_history_and_get_returns_latest(self):
        pclient, body = _at_task()
        r1 = pclient.put("/api/study/session/text", json={"content": "An Occurrence"})
        r2 = pclient.put(
            "/api/study/session/text",
            json={"content": "An Occurrence records an Organism.", "trigger": "blur"},
        )
        assert (r1.json()["stored"], r2.json()["word_count"]) == (True, 5)
        assert pclient.get("/api/study/session/text").json()["content"].endswith("Organism.")

        snaps = study_text.list_snapshots(_base_con(body["task_base_id"]))
        assert [(s["trigger"], s["word_count"]) for s in snaps] == [("autosave", 2), ("blur", 5)]
        assert snaps[0]["actor_id"] == body["actor_id"]
        assert snaps[0]["at_utc"].endswith("+00:00")

    def test_identical_autosave_is_not_stored_twice(self):
        pclient, body = _at_task()
        first = pclient.put("/api/study/session/text", json={"content": "Same."}).json()
        again = pclient.put("/api/study/session/text", json={"content": "Same."}).json()
        assert (again["stored"], again["id"]) == (False, first["id"])
        assert len(study_text.list_snapshots(_base_con(body["task_base_id"]))) == 1

    def test_tutorial_drafts_go_to_the_practice_base(self):
        """Participants practise the pane too — without touching the task."""
        pclient, body = _participant()
        pclient.post("/api/study/session/begin-tutorial")
        pclient.put("/api/study/session/text", json={"content": "Cats and dogs."})
        practice = _base_con(f"practice-{body['session_id']}")
        assert [s["content"] for s in study_text.list_snapshots(practice)] == ["Cats and dogs."]

        task = pclient.post("/api/study/session/begin-task").json()
        assert pclient.get("/api/study/session/text").json()["content"] == ""
        assert study_text.list_snapshots(_base_con(task["task_base_id"])) == []

    def test_no_text_outside_tutorial_and_task(self):
        pclient, _ = _participant()  # still in briefing
        assert pclient.get("/api/study/session/text").status_code == 409
        assert pclient.put("/api/study/session/text", json={"content": "x"}).status_code == 409

    def test_oversized_text_rejected(self):
        pclient, _ = _at_task()
        big = "x" * (study_text.MAX_TEXT_CHARS + 1)
        r = pclient.put("/api/study/session/text", json={"content": big})
        assert r.status_code == 413
        assert "longer than" in r.json()["detail"]["user_message"]

    def test_bad_trigger_rejected(self):
        pclient, _ = _at_task()
        r = pclient.put("/api/study/session/text", json={"content": "x", "trigger": "submit"})
        assert r.status_code == 400

    def test_requires_a_participant_session(self):
        assert TestClient(app).get("/api/study/session/text").status_code == 401


# ── Editor events ────────────────────────────────────────────────────


class TestEditorEvents:
    def test_paste_keeps_length_only(self):
        """The promise to participants is length and time — a client
        that sent the pasted text must not get it stored."""
        pclient, body = _at_task()
        r = pclient.post(
            "/api/study/session/text/events",
            json={
                "events": [
                    {"type": "paste", "length": 412, "text": "SECRET PASTED CONTENT"},
                    {"type": "soft_warning_shown", "threshold_minutes": 50},
                    {"type": "keylogger", "keys": "abc"},
                ]
            },
        )
        assert r.json() == {"stored": 2}
        events = study_text.list_editor_events(_base_con(body["task_base_id"]))
        assert [(e["event_type"], e["payload"]) for e in events] == [
            ("paste", {"length": 412}),
            ("soft_warning_shown", {"threshold_minutes": 50}),
        ]
        assert events[0]["actor_id"] == body["actor_id"]
        dump = _base_con(body["task_base_id"]).execute("SELECT * FROM editor_events").fetchall()
        assert "SECRET" not in repr(dump)


# ── Finishing the task ───────────────────────────────────────────────


class TestFinish:
    def test_finish_submits_the_text_and_advances(self):
        pclient, body = _at_task("baseline", topic_title=TOPIC)
        pclient.put("/api/study/session/text", json={"content": "Draft."})
        text = "An Occurrence records an Organism at a place and time.\n\nIt is not the Organism."
        r = pclient.post("/api/study/session/finish", json={"content": f"  {text}\n"})
        assert r.status_code == 200, r.text
        assert (r.json()["state"], r.json()["text_submitted"]) == ("post_session", True)

        row = pdb.find_study_text_for_session(get_registry().platform_con(), body["session_id"])
        assert row["content"] == text  # stripped
        assert (row["condition"], row["topic_title"]) == ("baseline", TOPIC)
        assert (row["word_count"], row["char_count"]) == (15, len(text))
        assert row["actor_id"] == body["actor_id"]
        assert row["active_elapsed_seconds"] is not None

        snaps = study_text.list_snapshots(_base_con(body["task_base_id"]))
        assert [s["trigger"] for s in snaps] == ["autosave", "submit"]

    def test_submit_is_recorded_even_if_text_unchanged_since_autosave(self):
        pclient, body = _at_task()
        pclient.put("/api/study/session/text", json={"content": "Final."})
        pclient.post("/api/study/session/finish", json={"content": "Final."})
        snaps = study_text.list_snapshots(_base_con(body["task_base_id"]))
        assert [s["trigger"] for s in snaps] == ["autosave", "submit"]

    @pytest.mark.parametrize("content", ["", "   \n\t "])
    def test_empty_text_cannot_finish(self, content):
        pclient, _ = _at_task()
        r = pclient.post("/api/study/session/finish", json={"content": content})
        assert r.status_code == 400
        assert "empty" in r.json()["detail"]["user_message"]
        assert pclient.get("/api/study/session").json()["state"] == "active"

    def test_cannot_skip_to_questionnaires_without_a_text(self):
        pclient, _ = _at_task()
        r = pclient.post("/api/study/session/advance", json={"to_state": "post_session"})
        assert r.status_code == 400
        assert "submit your text" in r.json()["detail"]["user_message"]

    def test_only_from_the_task(self):
        pclient, _ = _participant()  # briefing
        r = pclient.post("/api/study/session/finish", json={"content": "Too early."})
        assert r.status_code == 400

    def test_first_submission_stands(self):
        """A double-clicked Finish must not replace what was submitted."""
        pclient, body = _at_task()
        assert (
            pclient.post("/api/study/session/finish", json={"content": "First."}).status_code
            == 200
        )
        r = pclient.post("/api/study/session/finish", json={"content": "Second."})
        assert r.status_code == 400  # no longer in the task
        con = get_registry().platform_con()
        assert pdb.find_study_text_for_session(con, body["session_id"])["content"] == "First."
        assert (
            pdb.create_study_text(
                con,
                session_id=body["session_id"],
                actor_id=body["actor_id"],
                condition="elenchus",
                topic_title="",
                content="Third.",
                word_count=1,
                active_elapsed_seconds=0,
            )
            is None
        )

    def test_list_study_texts_scoped_to_study(self):
        pclient, _ = _at_task(topic_title=TOPIC)
        pclient.post("/api/study/session/finish", json={"content": "Mine."})
        con = get_registry().platform_con()
        assert [t["content"] for t in pdb.list_study_texts(con, study_id="S")] == ["Mine."]
        assert pdb.list_study_texts(con, study_id="OTHER") == []


# ── study_text module ────────────────────────────────────────────────


class TestModule:
    def test_word_count(self):
        assert study_text.word_count("") == 0
        assert study_text.word_count("  one\ttwo\n\nthree — four  ") == 5

    def test_snapshot_ids_continue_across_reopen(self, tmp_path):
        path = str(tmp_path / "b.duckdb")
        state = DialecticalState.create(path, "t")
        study_text.save_snapshot(state.base.con, "a", trigger="autosave")
        state.base.con.close()
        state = DialecticalState.open(path)
        second = study_text.save_snapshot(state.base.con, "ab", trigger="autosave")
        assert second["id"] == 2
        state.base.con.close()

    def test_unknown_trigger_rejected(self):
        state = DialecticalState.in_memory("t")
        with pytest.raises(ValueError, match="trigger"):
            study_text.save_snapshot(state.base.con, "a", trigger="keystroke")
