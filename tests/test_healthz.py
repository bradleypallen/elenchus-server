"""Tests for the /healthz liveness + readiness probe."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from elenchus.db import get_registry
from elenchus.server import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _migrated():
    # The platform-DB lifespan migration only fires on first TestClient
    # request; run it explicitly so the `meta` table exists for the
    # health probe.
    get_registry().migrate_platform()
    yield


class TestHealthz:
    def test_ok_when_healthy(self):
        r = client.get("/healthz")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["checks"]["platform_db"] == "ok"
        assert body["checks"]["data_dir"] == "ok"
        assert isinstance(body["schema_version"], int)
        assert body["schema_version"] >= 1

    def test_no_auth_required(self):
        # The probe must work without any session cookie.
        c = TestClient(app)
        c.cookies.clear()
        r = c.get("/healthz")
        assert r.status_code == 200

    def test_reports_phase_b_and_llm_flags(self):
        r = client.get("/healthz")
        body = r.json()
        # Flags are booleans regardless of value.
        assert isinstance(body["phase_b_enabled"], bool)
        assert isinstance(body["llm_configured"], bool)

    def test_degraded_503_when_platform_db_unreachable(self):
        # Force the platform-DB probe to throw.
        with patch("elenchus.server.get_registry") as mock_reg:
            mock_reg.return_value.platform_con.side_effect = RuntimeError("db down")
            r = client.get("/healthz")
        assert r.status_code == 503
        body = r.json()
        assert body["status"] == "degraded"
        assert "error" in body["checks"]["platform_db"]
        assert body["schema_version"] is None

    def test_degraded_503_when_data_dir_not_writable(self):
        with patch("elenchus.server.os.access", return_value=False):
            r = client.get("/healthz")
        assert r.status_code == 503
        assert r.json()["checks"]["data_dir"] == "not writable"

    def test_does_not_touch_per_base_files(self):
        """The probe must not open any per-base DB — a corrupt base
        should never flap the health check. We assert by checking the
        registry's get() (per-base open) is never called."""
        with patch("elenchus.server.get_registry") as mock_reg:
            con = mock_reg.return_value.platform_con.return_value
            con.execute.return_value.fetchone.return_value = ("7",)
            r = client.get("/healthz")
            # platform_con was used; get() (per-base) was not.
            assert mock_reg.return_value.get.called is False
        assert r.status_code == 200
        assert r.json()["schema_version"] == 7
        _ = os  # keep import referenced
