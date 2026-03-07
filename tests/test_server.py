"""Tests for server.py — FastAPI API endpoints."""

import contextlib
import logging
import os
import tempfile
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

logger = logging.getLogger(__name__)

# Use a temp directory for test dialectics to avoid polluting the real data dir
_test_data_dir = tempfile.mkdtemp(prefix="elenchus_test_")
os.environ["ELENCHUS_DATA"] = _test_data_dir

# Must set before importing server (which reads env at module level)
os.environ.setdefault("ELENCHUS_API_KEY", "test-key-for-ci")

from elenchus.server import app, _states  # noqa: E402  # isort: skip

client = TestClient(app)


@pytest.fixture(autouse=True)
def _clean_states():
    """Clean up state cache and test DB files between tests."""
    _states.clear()
    yield
    _states.clear()
    # Clean up any .duckdb files created during tests
    for f in os.listdir(_test_data_dir):
        if f.endswith(".duckdb"):
            with contextlib.suppress(OSError):
                os.remove(os.path.join(_test_data_dir, f))


# ── Dialectic CRUD ──


class TestDialecticCRUD:
    def test_create_dialectic(self):
        r = client.post("/api/dialectics", json={"name": "test1", "topic": "Test Topic"})
        assert r.status_code == 200
        data = r.json()
        assert data["name"] == "test1"
        assert data["state"]["name"] == "Test Topic"

    def test_create_duplicate_returns_409(self):
        client.post("/api/dialectics", json={"name": "dup"})
        r = client.post("/api/dialectics", json={"name": "dup"})
        assert r.status_code == 409

    def test_create_empty_name_returns_400(self):
        r = client.post("/api/dialectics", json={"name": "  ", "topic": "x"})
        assert r.status_code == 400

    def test_list_dialectics(self):
        client.post("/api/dialectics", json={"name": "list1"})
        client.post("/api/dialectics", json={"name": "list2"})
        r = client.get("/api/dialectics")
        assert r.status_code == 200
        names = [d["name"] for d in r.json()]
        assert "list1" in names
        assert "list2" in names

    def test_get_dialectic(self):
        client.post("/api/dialectics", json={"name": "get1", "topic": "Get Test"})
        r = client.get("/api/dialectics/get1")
        assert r.status_code == 200
        data = r.json()
        assert data["name"] == "Get Test"
        assert "conversation" in data
        assert "commitments" in data

    def test_get_nonexistent_returns_404(self):
        r = client.get("/api/dialectics/nonexistent")
        assert r.status_code == 404

    def test_delete_dialectic(self):
        client.post("/api/dialectics", json={"name": "del1"})
        r = client.delete("/api/dialectics/del1")
        assert r.status_code == 200
        assert r.json()["deleted"] == "del1"
        # Confirm it's gone
        r = client.get("/api/dialectics/del1")
        assert r.status_code == 404

    def test_delete_nonexistent_returns_404(self):
        r = client.delete("/api/dialectics/nope")
        assert r.status_code == 404


# ── Tensions ──


class TestTensionEndpoints:
    def _setup_dialectic_with_tension(self):
        client.post("/api/dialectics", json={"name": "tens"})
        state = _states["tens"]
        state.commit("P")
        state.deny("Q")
        tid = state.add_tension(["P"], ["Q"], reason="conflict")
        return tid

    def test_accept_tension(self):
        tid = self._setup_dialectic_with_tension()
        r = client.post(f"/api/dialectics/tens/tensions/{tid}", json={"action": "accept"})
        assert r.status_code == 200
        assert "accepted" in r.json()
        assert len(r.json()["state"]["implications"]) == 1

    def test_contest_tension(self):
        tid = self._setup_dialectic_with_tension()
        r = client.post(f"/api/dialectics/tens/tensions/{tid}", json={"action": "contest"})
        assert r.status_code == 200
        assert r.json()["contested"] == tid

    def test_invalid_tension_action(self):
        tid = self._setup_dialectic_with_tension()
        r = client.post(f"/api/dialectics/tens/tensions/{tid}", json={"action": "invalid"})
        assert r.status_code == 400

    def test_accept_nonexistent_tension(self):
        client.post("/api/dialectics", json={"name": "tens2"})
        r = client.post("/api/dialectics/tens2/tensions/999", json={"action": "accept"})
        assert r.status_code == 404


# ── Retract ──


class TestRetractEndpoint:
    def test_retract_proposition(self):
        client.post("/api/dialectics", json={"name": "ret1"})
        _states["ret1"].commit("Some claim")
        r = client.post("/api/dialectics/ret1/retract", json={"proposition": "Some claim"})
        assert r.status_code == 200
        assert r.json()["retracted"] == "Some claim"
        assert "Some claim" not in r.json()["state"]["commitments"]


# ── Derive ──


class TestDeriveEndpoint:
    def test_derive_containment(self):
        client.post("/api/dialectics", json={"name": "der1"})
        _states["der1"].commit("P")
        r = client.post("/api/dialectics/der1/derive", json={"gamma": ["P"], "delta": ["P"]})
        assert r.status_code == 200
        assert r.json()["derives"] is True

    def test_derive_no_derivation(self):
        client.post("/api/dialectics", json={"name": "der2"})
        _states["der2"].commit("P")
        _states["der2"].commit("Q")
        r = client.post("/api/dialectics/der2/derive", json={"gamma": ["P"], "delta": ["Q"]})
        assert r.status_code == 200
        assert r.json()["derives"] is False


# ── Message (mocked LLM) ──


class TestMessageEndpoint:
    @patch("elenchus.server.opponent.respond")
    def test_send_message(self, mock_respond):
        mock_respond.return_value = {
            "response": "Interesting claim.",
            "speech_acts": [{"type": "COMMIT", "proposition": "Test prop"}],
            "new_tensions": [],
        }
        client.post("/api/dialectics", json={"name": "msg1"})
        r = client.post("/api/dialectics/msg1/message", json={"message": "I believe X."})
        assert r.status_code == 200
        data = r.json()
        assert data["response"] == "Interesting claim."
        assert len(data["speech_acts"]) == 1

    @patch("elenchus.server.opponent.respond")
    def test_message_to_nonexistent_dialectic(self, mock_respond):
        r = client.post("/api/dialectics/nope/message", json={"message": "Hello"})
        assert r.status_code == 404
        mock_respond.assert_not_called()


# ── Settings ──


class TestSettingsEndpoints:
    def test_get_settings(self):
        r = client.get("/api/settings")
        assert r.status_code == 200
        data = r.json()
        assert "model" in data
        assert "protocol" in data
        assert "has_api_key" in data

    @patch("elenchus.server.opponent.reconfigure")
    def test_update_settings(self, mock_reconfig):
        r = client.put("/api/settings", json={"model": "gpt-4o"})
        assert r.status_code == 200
        mock_reconfig.assert_called_once()


# ── Report ──


class TestReportEndpoint:
    def test_report_text(self):
        client.post("/api/dialectics", json={"name": "rpt1"})
        _states["rpt1"].commit("P")
        r = client.get("/api/dialectics/rpt1/report")
        assert r.status_code == 200
        assert "report" in r.json()
        assert "L_B" in r.json()["report"]
