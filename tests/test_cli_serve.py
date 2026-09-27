"""`elenchus serve` startup: the two things that decide where it runs
and what it calls — the data directory and the model — must mean what
the operator said.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import patch

import elenchus.server as srv
from elenchus.db import get_registry, init_registry
from elenchus.opponent import DEFAULT_MODEL, Opponent


class TestDataDir:
    def test_data_dir_repoints_the_registry(self, tmp_path):
        """The registry is built at import on the env directory; the flag
        must move it, not just the log line."""
        wanted = str(tmp_path / "elsewhere")
        original_dir, original_reg = srv.DATA_DIR, get_registry()
        args = SimpleNamespace(
            data_dir=wanted, model=None, api_key=None, base_url=None, protocol=None, port=18799
        )
        try:
            with (
                patch("uvicorn.run") as run,
                patch.object(srv.opponent, "reconfigure"),
            ):
                srv._run_serve(args)
            assert run.called
            assert wanted == srv.DATA_DIR and os.path.isdir(wanted)
            assert get_registry() is not original_reg
            assert get_registry().platform_path == os.path.join(wanted, "platform.duckdb")
        finally:
            srv.DATA_DIR = original_dir
            init_registry(original_dir)

    def test_without_the_flag_nothing_moves(self):
        original_dir, original_reg = srv.DATA_DIR, get_registry()
        args = SimpleNamespace(
            data_dir=None, model=None, api_key=None, base_url=None, protocol=None, port=18799
        )
        with patch("uvicorn.run"), patch.object(srv.opponent, "reconfigure"):
            srv._run_serve(args)
        assert original_dir == srv.DATA_DIR and get_registry() is original_reg


class TestModel:
    def test_empty_env_means_the_default(self, monkeypatch):
        monkeypatch.setenv("ELENCHUS_MODEL", "")
        assert srv._env_model() == DEFAULT_MODEL
        monkeypatch.setenv("ELENCHUS_MODEL", "   ")
        assert srv._env_model() == "   "  # not our problem to trim; it is not empty
        monkeypatch.delenv("ELENCHUS_MODEL")
        assert srv._env_model() == DEFAULT_MODEL
        monkeypatch.setenv("ELENCHUS_MODEL", "claude-opus-4-8")
        assert srv._env_model() == "claude-opus-4-8"

    def test_the_opponent_never_holds_an_empty_model(self):
        assert Opponent(api_key=None, model="").model == DEFAULT_MODEL
        assert Opponent(api_key=None, model=None).model == DEFAULT_MODEL
        assert Opponent(api_key=None).model == DEFAULT_MODEL
        assert Opponent(api_key=None, model="claude-opus-4-8").model == "claude-opus-4-8"
