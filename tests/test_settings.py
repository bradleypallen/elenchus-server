"""Tests for runtime LLM settings: admin-gating, server-side persistence,
and at-rest encryption of the API key (secretbox)."""

import contextlib
import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from elenchus import auth, secretbox, server
from elenchus.db import get_registry
from elenchus.db import platform as pdb
from elenchus.server import app

client = TestClient(app)
_test_data_dir = os.environ["ELENCHUS_DATA"]

_LLM_KEYS = (server._S_MODEL, server._S_BASE_URL, server._S_PROTOCOL, server._S_API_KEY_ENC)
_MASTER = "test-master-key-do-not-use-in-prod"


@pytest.fixture(autouse=True)
def _clean():
    reg = get_registry()
    reg.migrate_platform()
    con = reg.platform_con()
    with reg.platform_lock:
        for table in ("auth_sessions", "magic_links", "invites", "sessions", "bases", "actors"):
            con.execute(f"DELETE FROM {table}")
        for k in _LLM_KEYS:
            con.execute("DELETE FROM platform_settings WHERE key = ?", [k])
    for _name, handle in list(reg._handles.items()):
        with contextlib.suppress(Exception):
            handle.state.base.con.close()
    reg._handles.clear()
    client.cookies.clear()
    yield
    client.cookies.clear()
    with reg.platform_lock:
        for k in _LLM_KEYS:
            con.execute("DELETE FROM platform_settings WHERE key = ?", [k])


def _login(kind: str, email: str) -> int:
    con = get_registry().platform_con()
    actor_id = pdb.create_actor(
        con,
        kind=kind,
        email=email,
        display_name=email.split("@")[0],
        password_hash=auth.hash_password("pw"),
    )
    client.cookies.set(auth.SESSION_COOKIE, auth.create_session(actor_id))
    return actor_id


# ── secretbox ──


def test_secretbox_roundtrip(monkeypatch):
    monkeypatch.setenv("ELENCHUS_SECRET_KEY", _MASTER)
    assert secretbox.is_available()
    token = secretbox.encrypt("sk-ant-secret")
    assert token != "sk-ant-secret"  # actually encrypted
    assert secretbox.decrypt(token) == "sk-ant-secret"


def test_secretbox_unavailable_without_master_key(monkeypatch):
    monkeypatch.delenv("ELENCHUS_SECRET_KEY", raising=False)
    assert not secretbox.is_available()
    assert secretbox.decrypt("anything") is None
    with pytest.raises(RuntimeError):
        secretbox.encrypt("x")


def test_secretbox_wrong_master_key_returns_none(monkeypatch):
    monkeypatch.setenv("ELENCHUS_SECRET_KEY", _MASTER)
    token = secretbox.encrypt("sk-ant-secret")
    monkeypatch.setenv("ELENCHUS_SECRET_KEY", "a-different-master-key")
    assert secretbox.decrypt(token) is None  # rotated/changed key invalidates


# ── admin gating ──


def test_get_settings_requires_authentication():
    assert client.get("/api/settings").status_code == 401


def test_settings_forbidden_for_non_admin():
    _login("user", "user@example.com")
    assert client.get("/api/settings").status_code == 403
    assert client.put("/api/settings", json={"model": "gpt-4o"}).status_code == 403


@patch("elenchus.server.opponent.reconfigure")
def test_admin_get_payload_shape_never_leaks_key(_mock_reconfig):
    _login("admin", "admin@example.com")
    r = client.get("/api/settings")
    assert r.status_code == 200
    data = r.json()
    for field in (
        "model",
        "base_url",
        "protocol",
        "has_api_key",
        "key_persisted",
        "persistence_available",
    ):
        assert field in data
    assert "api_key" not in data and "_api_key" not in data


# ── persistence + encryption ──


@patch("elenchus.server.opponent.reconfigure")
def test_put_persists_key_encrypted(_mock_reconfig, monkeypatch):
    monkeypatch.setenv("ELENCHUS_SECRET_KEY", _MASTER)
    _login("admin", "admin@example.com")
    r = client.put(
        "/api/settings",
        json={"api_key": "sk-ant-live-123", "base_url": "https://openrouter.ai/api/v1"},
    )
    assert r.status_code == 200
    assert r.json()["key_persisted"] is True

    con = get_registry().platform_con()
    stored = pdb.get_setting(con, server._S_API_KEY_ENC)
    assert stored is not None and "sk-ant-live-123" not in stored  # ciphertext, not plaintext
    assert secretbox.decrypt(stored) == "sk-ant-live-123"
    assert pdb.get_setting(con, server._S_BASE_URL) == "https://openrouter.ai/api/v1"


@patch("elenchus.server.opponent.reconfigure")
def test_put_without_master_key_does_not_persist(_mock_reconfig, monkeypatch):
    monkeypatch.delenv("ELENCHUS_SECRET_KEY", raising=False)
    _login("admin", "admin@example.com")
    r = client.put("/api/settings", json={"api_key": "sk-ant-ephemeral"})
    assert r.status_code == 200
    assert r.json()["key_persisted"] is False
    # Live opponent still reconfigured (key applied in-memory)...
    _mock_reconfig.assert_called_once()
    # ...but nothing encrypted was stored.
    assert pdb.get_setting(get_registry().platform_con(), server._S_API_KEY_ENC) is None


def test_persisted_settings_applied_on_restart(monkeypatch):
    """Simulate a restart: persist settings, then run the startup loader and
    confirm the opponent is reconfigured with the decrypted key + endpoint."""
    monkeypatch.setenv("ELENCHUS_SECRET_KEY", _MASTER)
    server._persist_llm_settings(
        model="claude-sonnet-4-6",
        base_url="https://openrouter.ai/api/v1",
        api_key="sk-ant-persisted",
    )
    with patch("elenchus.server.opponent.reconfigure") as mock_reconfig:
        server._apply_persisted_llm_settings()
        mock_reconfig.assert_called_once()
        kwargs = mock_reconfig.call_args.kwargs
        assert kwargs["model"] == "claude-sonnet-4-6"
        assert kwargs["base_url"] == "https://openrouter.ai/api/v1"
        assert kwargs["api_key"] == "sk-ant-persisted"  # decrypted on load


def test_restart_loader_skips_key_when_master_key_missing(monkeypatch):
    """A persisted (encrypted) key cannot be decrypted without the master
    key; the loader must not pass a garbage/None-derived key as if valid."""
    monkeypatch.setenv("ELENCHUS_SECRET_KEY", _MASTER)
    server._persist_llm_settings(model="claude-sonnet-4-6", api_key="sk-ant-persisted")
    monkeypatch.delenv("ELENCHUS_SECRET_KEY", raising=False)
    with patch("elenchus.server.opponent.reconfigure") as mock_reconfig:
        server._apply_persisted_llm_settings()
        # model still applies; api_key resolves to None (undecryptable)
        kwargs = mock_reconfig.call_args.kwargs
        assert kwargs["model"] == "claude-sonnet-4-6"
        assert kwargs["api_key"] is None
