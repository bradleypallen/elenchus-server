"""Tests for Phase D/5 — structured LLM report generation.

Four slices:
  1. Template format-balance — the single prompt the LLM sees must
     not mention either condition; the LLM must not be able to tell
     which condition's source material it's reading.
  2. Input formatters — Elenchus material renders position +
     implications + transcript; baseline renders transcript only.
  3. `generate_report` pipeline — calls LLMClient, returns
     token/cost data, raises LLMCallError on classified failure.
  4. HTTP routes — generate + retrieve + admin listing.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from elenchus import auth
from elenchus.db import get_registry
from elenchus.db import platform as pdb
from elenchus.dialectical_state import DialecticalState
from elenchus.llm_client import ChatCategory, ChatResult
from elenchus.opponent import Opponent
from elenchus.server import app
from elenchus.study_reports import (
    REPORT_PROMPT_TEMPLATE,
    _format_baseline_input,
    _format_elenchus_input,
    format_source_material,
    generate_report,
)

client = TestClient(app)
_test_data_dir = os.environ["ELENCHUS_DATA"]


@pytest.fixture(autouse=True)
def _clean():
    reg = get_registry()
    reg.migrate_platform()
    con = reg.platform_con()
    with reg.platform_lock:
        for table in (
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
    for root, _dirs, files in os.walk(_test_data_dir):
        for f in files:
            if f.endswith(".duckdb") and f != "platform.duckdb":
                with contextlib.suppress(OSError):
                    os.remove(os.path.join(root, f))


def _make_participant_session_with_base(
    *, condition: str, base_name: str = "participant-base"
) -> tuple[int, int, str]:
    """Create participant + their base + study session bound to it.
    Returns (actor_id, session_id, base_id)."""
    con = get_registry().platform_con()
    actor_id = pdb.create_actor(
        con,
        kind="participant",
        email=None,
        display_name="P",
        password_hash=None,
    )
    pdb.create_base(con, base_id=base_name, name=base_name, owner_id=actor_id)
    sid = pdb.create_study_session(
        con,
        actor_id=actor_id,
        study_token="tok",
        condition=condition,
        initial_state="active",
    )
    pdb.attach_base_to_session(con, sid, base_name)

    # Make the per-base file exist on disk.
    reg = get_registry()
    path = reg.db_path(base_name, actor_id=actor_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    DialecticalState.create(path, base_name).base.con.close()
    return actor_id, sid, base_name


# ─── Template format-balance ─────────────────────────────────────────


class TestTemplateFormatBalance:
    """The single prompt the LLM sees must not leak which condition
    produced the source material. A judge reading two reports must not
    be able to tell from structure alone which is which."""

    def test_template_mentions_neither_condition(self):
        lower = REPORT_PROMPT_TEMPLATE.lower()
        for word in ["elenchus", "baseline", "tension", "opponent", "socratic", "prover-skeptic"]:
            assert word not in lower, f"REPORT_PROMPT_TEMPLATE mentions {word!r}; breaks blinding"

    def test_template_has_required_sections(self):
        # The four-section structure judges expect.
        for section in ("# Domain", "# Atomic statements", "# Implications", "# Notes"):
            assert section in REPORT_PROMPT_TEMPLATE

    def test_template_has_source_material_slot(self):
        assert "{source_material}" in REPORT_PROMPT_TEMPLATE


# ─── Input formatters ────────────────────────────────────────────────


class TestElenchusFormatter:
    def test_renders_position(self):
        state = DialecticalState.in_memory("test")
        state.commit("Biomes are climate-based.")
        state.deny("Biomes are arbitrary.")

        rendered = _format_elenchus_input(state)
        assert "Commitments (C):" in rendered
        assert "Biomes are climate-based." in rendered
        assert "Denials (D):" in rendered
        assert "Biomes are arbitrary." in rendered
        state.base.con.close()

    def test_renders_implications(self):
        state = DialecticalState.in_memory("test")
        state.commit("X is alive.")
        tid = state.add_tension(["X is alive."], ["X is an animal."], "by def")
        state.accept_tension(tid)

        rendered = _format_elenchus_input(state)
        assert "Accepted material implications (I):" in rendered
        assert "X is alive." in rendered
        assert "X is an animal." in rendered
        state.base.con.close()

    def test_empty_state_shows_none(self):
        state = DialecticalState.in_memory("test")
        rendered = _format_elenchus_input(state)
        assert "(none)" in rendered or "(empty)" in rendered
        state.base.con.close()

    def test_transcript_included(self):
        state = DialecticalState.in_memory("test")
        state.add_conversation("user", "What is a biome?")
        state.add_conversation("assistant", "An ecological region.")

        rendered = _format_elenchus_input(state)
        assert "CONVERSATION TRANSCRIPT" in rendered
        assert "What is a biome?" in rendered
        assert "An ecological region." in rendered
        state.base.con.close()

    def test_transcript_windowed_to_recent_turns(self):
        state = DialecticalState.in_memory("test")
        # 60 messages total — formatter should keep only the last 40.
        for i in range(30):
            state.add_conversation("user", f"u-{i}")
            state.add_conversation("assistant", f"a-{i}")

        rendered = _format_elenchus_input(state)
        # The earliest messages drop out.
        assert "u-0" not in rendered
        assert "u-29" in rendered
        state.base.con.close()


class TestBaselineFormatter:
    def test_renders_transcript_only(self):
        state = DialecticalState.in_memory("test")
        state.add_conversation("user", "Hello")
        state.add_conversation("assistant", "Hi")

        rendered = _format_baseline_input(state)
        assert "CONVERSATION TRANSCRIPT" in rendered
        assert "Hello" in rendered
        assert "Hi" in rendered
        # Critically — no dialectical structure leaked in.
        assert "Commitments" not in rendered
        assert "Denials" not in rendered
        assert "implications" not in rendered.lower()
        state.base.con.close()

    def test_empty_shows_empty_marker(self):
        state = DialecticalState.in_memory("test")
        rendered = _format_baseline_input(state)
        assert "(empty)" in rendered
        state.base.con.close()


class TestFormatSourceMaterialDispatch:
    def test_dispatches_to_elenchus_formatter(self):
        state = DialecticalState.in_memory("test")
        state.commit("X.")
        rendered = format_source_material(state, condition="elenchus")
        assert "Commitments" in rendered
        state.base.con.close()

    def test_dispatches_to_baseline_formatter(self):
        state = DialecticalState.in_memory("test")
        state.add_conversation("user", "hi")
        rendered = format_source_material(state, condition="baseline")
        # Baseline doesn't include any dialectical headings.
        assert "Commitments" not in rendered
        assert "CONVERSATION TRANSCRIPT" in rendered
        state.base.con.close()

    def test_unknown_condition_raises(self):
        state = DialecticalState.in_memory("test")
        with pytest.raises(ValueError, match="Unknown condition"):
            format_source_material(state, condition="placebo")
        state.base.con.close()


# ─── Generation pipeline ─────────────────────────────────────────────


def _success(text: str, prompt: int = 100, completion: int = 50) -> ChatResult:
    return ChatResult(
        category=ChatCategory.SUCCESS,
        text=text,
        attempts=1,
        latency_ms=42,
        prompt_tokens=prompt,
        completion_tokens=completion,
        model="claude-opus-4-6",
    )


class TestGenerateReport:
    def test_returns_structured_dict(self):
        opp = Opponent(api_key=None, model="claude-opus-4-6")
        state = DialecticalState.in_memory("test")
        state.commit("Domain claim.")
        with patch.object(
            opp._llm_client,
            "achat",
            new=AsyncMock(return_value=_success("# Domain\nBiology.")),
        ):
            result = asyncio.run(
                generate_report(state, condition="elenchus", opponent=opp, session_id=99)
            )
        assert result["content"] == "# Domain\nBiology."
        assert result["model"] == "claude-opus-4-6"
        assert result["session_id"] == 99
        assert result["condition"] == "elenchus"
        assert result["prompt_tokens"] == 100
        assert result["completion_tokens"] == 50
        state.base.con.close()

    def test_failure_raises_llmcallerror(self):
        from elenchus.opponent import LLMCallError

        opp = Opponent(api_key=None, model="claude-opus-4-6")
        state = DialecticalState.in_memory("test")
        fail = ChatResult(
            category=ChatCategory.RATE_LIMIT,
            attempts=3,
            model="claude-opus-4-6",
            error_message="429",
        )
        with patch.object(opp._llm_client, "achat", new=AsyncMock(return_value=fail)):
            with pytest.raises(LLMCallError) as excinfo:
                asyncio.run(generate_report(state, condition="elenchus", opponent=opp))
            assert excinfo.value.result.category == ChatCategory.RATE_LIMIT
        state.base.con.close()

    def test_uses_full_template_in_user_message(self):
        """The entire template (not just `source_material`) reaches
        the LLM, in a single one-shot user message — no system slot
        used so the LLM doesn't think it's continuing a conversation."""
        opp = Opponent(api_key=None, model="claude-opus-4-6")
        state = DialecticalState.in_memory("test")
        state.commit("X.")

        mock = AsyncMock(return_value=_success("ok"))
        with patch.object(opp._llm_client, "achat", new=mock):
            asyncio.run(generate_report(state, condition="elenchus", opponent=opp))

        messages, kwargs = mock.call_args
        sent = messages[0][0]
        assert sent["role"] == "user"
        assert "# Domain" in sent["content"]
        assert "# Atomic statements" in sent["content"]
        # The expert's commitment made it into the source slot.
        assert "X." in sent["content"]
        # System slot intentionally empty for one-shot extraction.
        assert kwargs.get("system") is None
        state.base.con.close()


# ─── Platform DB row storage ─────────────────────────────────────────


class TestStudyReportRows:
    def test_record_and_find(self):
        con = get_registry().platform_con()
        rid = pdb.record_study_report(
            con,
            session_id=1,
            condition="elenchus",
            content="# Domain\nX",
            generator_model="claude-opus-4-6",
            prompt_tokens=100,
            completion_tokens=20,
            cost_usd=0.005,
            metadata={"attempts": 1, "latency_ms": 80},
        )
        assert rid > 0
        row = pdb.find_study_report_for_session(con, 1)
        assert row["id"] == rid
        assert row["condition"] == "elenchus"
        assert row["content"] == "# Domain\nX"
        assert row["metadata"]["attempts"] == 1

    def test_find_returns_newest(self):
        con = get_registry().platform_con()
        pdb.record_study_report(
            con,
            session_id=1,
            condition="elenchus",
            content="old",
            generator_model="m",
            prompt_tokens=1,
            completion_tokens=1,
            cost_usd=0,
        )
        pdb.record_study_report(
            con,
            session_id=1,
            condition="elenchus",
            content="new",
            generator_model="m",
            prompt_tokens=1,
            completion_tokens=1,
            cost_usd=0,
        )
        row = pdb.find_study_report_for_session(con, 1)
        assert row["content"] == "new"

    def test_list_filter_by_condition(self):
        con = get_registry().platform_con()
        pdb.record_study_report(
            con,
            session_id=1,
            condition="elenchus",
            content="e",
            generator_model="m",
            prompt_tokens=1,
            completion_tokens=1,
            cost_usd=0,
        )
        pdb.record_study_report(
            con,
            session_id=2,
            condition="baseline",
            content="b",
            generator_model="m",
            prompt_tokens=1,
            completion_tokens=1,
            cost_usd=0,
        )
        elenchus_only = pdb.list_study_reports(con, condition="elenchus")
        assert len(elenchus_only) == 1
        assert elenchus_only[0]["content"] == "e"


# ─── HTTP routes ─────────────────────────────────────────────────────


class TestGenerateEndpoint:
    def test_generates_and_stores(self):
        actor_id, sid, _ = _make_participant_session_with_base(condition="elenchus")
        c = TestClient(app)
        c.cookies.set(auth.SESSION_COOKIE, auth.create_session(actor_id))

        # Add some content to the state.
        reg = get_registry()
        state = reg.get("participant-base")
        state.commit("Biomes are climate-based.")

        with patch(
            "elenchus.server.opponent._llm_client.achat",
            new=AsyncMock(return_value=_success("# Domain\nBiomes\n\n# Atomic statements\n1. X.")),
        ):
            r = c.post(f"/api/study/session/{sid}/generate-report")
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["condition"] == "elenchus"
        assert "# Domain" in data["content"]
        # Stored row.
        row = pdb.find_study_report_for_session(get_registry().platform_con(), sid)
        assert row is not None
        assert row["content"] == data["content"]

    def test_unknown_session_404(self):
        actor_id, _, _ = _make_participant_session_with_base(condition="elenchus")
        c = TestClient(app)
        c.cookies.set(auth.SESSION_COOKIE, auth.create_session(actor_id))
        r = c.post("/api/study/session/99999/generate-report")
        assert r.status_code == 404

    def test_wrong_participant_403(self):
        # Participant 1 owns their session.
        actor1, sid, _ = _make_participant_session_with_base(condition="elenchus")
        # Participant 2 — different actor.
        con = get_registry().platform_con()
        actor2 = pdb.create_actor(
            con,
            kind="participant",
            email=None,
            display_name="P2",
            password_hash=None,
        )
        c = TestClient(app)
        c.cookies.set(auth.SESSION_COOKIE, auth.create_session(actor2))
        r = c.post(f"/api/study/session/{sid}/generate-report")
        assert r.status_code == 403
        _ = actor1  # silence unused

    def test_admin_can_generate_any_session(self):
        actor_id, sid, _ = _make_participant_session_with_base(condition="baseline")
        # Add transcript so baseline formatter has material.
        reg = get_registry()
        state = reg.get("participant-base")
        state.add_conversation("user", "Hello AI")
        state.add_conversation("assistant", "Hi expert")

        # Admin user.
        con = reg.platform_con()
        admin = pdb.create_actor(
            con,
            kind="admin",
            email="admin@example.com",
            display_name="Admin",
            password_hash=auth.hash_password("pw"),
        )
        c = TestClient(app)
        c.cookies.set(auth.SESSION_COOKIE, auth.create_session(admin))

        with patch(
            "elenchus.server.opponent._llm_client.achat",
            new=AsyncMock(return_value=_success("# Domain\nX")),
        ):
            r = c.post(f"/api/study/session/{sid}/generate-report")
        assert r.status_code == 200
        assert r.json()["condition"] == "baseline"
        _ = actor_id  # silence unused

    def test_session_with_no_base_400(self):
        # Create a session without attaching a base.
        con = get_registry().platform_con()
        actor_id = pdb.create_actor(
            con,
            kind="participant",
            email=None,
            display_name="P",
            password_hash=None,
        )
        sid = pdb.create_study_session(
            con,
            actor_id=actor_id,
            study_token="t",
            condition="elenchus",
            initial_state="briefing",
        )
        c = TestClient(app)
        c.cookies.set(auth.SESSION_COOKIE, auth.create_session(actor_id))
        r = c.post(f"/api/study/session/{sid}/generate-report")
        assert r.status_code == 400


class TestFetchReportEndpoint:
    def test_returns_stored_report(self):
        actor_id, sid, _ = _make_participant_session_with_base(condition="elenchus")
        con = get_registry().platform_con()
        pdb.record_study_report(
            con,
            session_id=sid,
            condition="elenchus",
            content="# Domain\nseeded",
            generator_model="m",
            prompt_tokens=1,
            completion_tokens=1,
            cost_usd=0,
        )
        c = TestClient(app)
        c.cookies.set(auth.SESSION_COOKIE, auth.create_session(actor_id))
        r = c.get(f"/api/study/session/{sid}/report")
        assert r.status_code == 200
        assert r.json()["content"] == "# Domain\nseeded"

    def test_unknown_session_404(self):
        actor_id, _, _ = _make_participant_session_with_base(condition="elenchus")
        c = TestClient(app)
        c.cookies.set(auth.SESSION_COOKIE, auth.create_session(actor_id))
        r = c.get("/api/study/session/99999/report")
        assert r.status_code == 404

    def test_no_report_yet_404(self):
        actor_id, sid, _ = _make_participant_session_with_base(condition="elenchus")
        c = TestClient(app)
        c.cookies.set(auth.SESSION_COOKIE, auth.create_session(actor_id))
        r = c.get(f"/api/study/session/{sid}/report")
        assert r.status_code == 404


class TestAdminListReports:
    def test_admin_lists_all(self):
        # Seed two reports from different conditions.
        con = get_registry().platform_con()
        pdb.record_study_report(
            con,
            session_id=1,
            condition="elenchus",
            content="e",
            generator_model="m",
            prompt_tokens=1,
            completion_tokens=1,
            cost_usd=0,
        )
        pdb.record_study_report(
            con,
            session_id=2,
            condition="baseline",
            content="b",
            generator_model="m",
            prompt_tokens=1,
            completion_tokens=1,
            cost_usd=0,
        )
        # Login as admin.
        admin = pdb.create_actor(
            con,
            kind="admin",
            email="a@example.com",
            display_name="A",
            password_hash=auth.hash_password("pw"),
        )
        c = TestClient(app)
        c.cookies.set(auth.SESSION_COOKIE, auth.create_session(admin))
        r = c.get("/api/admin/study/reports")
        assert r.status_code == 200
        assert len(r.json()["reports"]) == 2

    def test_filter_by_condition(self):
        con = get_registry().platform_con()
        pdb.record_study_report(
            con,
            session_id=1,
            condition="elenchus",
            content="e",
            generator_model="m",
            prompt_tokens=1,
            completion_tokens=1,
            cost_usd=0,
        )
        pdb.record_study_report(
            con,
            session_id=2,
            condition="baseline",
            content="b",
            generator_model="m",
            prompt_tokens=1,
            completion_tokens=1,
            cost_usd=0,
        )
        admin = pdb.create_actor(
            con,
            kind="admin",
            email="a@example.com",
            display_name="A",
            password_hash=auth.hash_password("pw"),
        )
        c = TestClient(app)
        c.cookies.set(auth.SESSION_COOKIE, auth.create_session(admin))
        r = c.get("/api/admin/study/reports?condition=baseline")
        assert r.status_code == 200
        assert len(r.json()["reports"]) == 1
        assert r.json()["reports"][0]["condition"] == "baseline"

    def test_non_researcher_forbidden(self):
        con = get_registry().platform_con()
        uid = pdb.create_actor(
            con,
            kind="user",
            email="u@example.com",
            display_name="U",
            password_hash=auth.hash_password("pw"),
        )
        c = TestClient(app)
        c.cookies.set(auth.SESSION_COOKIE, auth.create_session(uid))
        r = c.get("/api/admin/study/reports")
        assert r.status_code == 403
