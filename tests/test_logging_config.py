"""The server configures Python logging at startup (`server._configure_logging`).

uvicorn configures only its own loggers; without this, every
`logger.info` in the app was dropped before reaching the journal, so
the audit trail CLAUDE.md promises (ledger edits, LLM calls, alerts)
did not exist in production.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

import elenchus.server as srv


@pytest.fixture
def basic_config():
    with patch("elenchus.server.logging.basicConfig") as m:
        yield m


def _level(mock) -> int:
    return mock.call_args.kwargs["level"]


class TestConfigureLogging:
    def test_serving_logs_at_info(self, basic_config, monkeypatch):
        monkeypatch.delenv(srv.LOG_LEVEL_ENV, raising=False)
        srv._configure_logging("serve")
        assert _level(basic_config) == logging.INFO
        srv._configure_logging(None)  # bare `elenchus` is `serve`
        assert _level(basic_config) == logging.INFO

    @pytest.mark.parametrize("command", ["costs", "audit", "admin", "migrate-legacy", "sim"])
    def test_one_off_commands_log_only_problems(self, basic_config, monkeypatch, command):
        monkeypatch.delenv(srv.LOG_LEVEL_ENV, raising=False)
        srv._configure_logging(command)
        assert _level(basic_config) == logging.WARNING

    def test_the_environment_overrides_either(self, basic_config, monkeypatch):
        monkeypatch.setenv(srv.LOG_LEVEL_ENV, "debug")
        srv._configure_logging("costs")
        assert _level(basic_config) == logging.DEBUG
        monkeypatch.setenv(srv.LOG_LEVEL_ENV, "ERROR")
        srv._configure_logging("serve")
        assert _level(basic_config) == logging.ERROR

    def test_nonsense_falls_back_and_says_so(self, basic_config, monkeypatch, caplog):
        monkeypatch.setenv(srv.LOG_LEVEL_ENV, "loud")
        with caplog.at_level(logging.WARNING, logger="elenchus.server"):
            srv._configure_logging("serve")
        assert _level(basic_config) == logging.INFO
        assert srv.LOG_LEVEL_ENV in caplog.text and "loud" in caplog.text

    def test_the_format_names_the_logger(self, basic_config):
        """`elenchus.cost_ledger` in each line is what makes the journal
        greppable per subsystem after a run."""
        srv._configure_logging("serve")
        assert "%(name)s" in basic_config.call_args.kwargs["format"]
        assert "%(levelname)s" in basic_config.call_args.kwargs["format"]

    def test_main_configures_before_dispatch(self, monkeypatch):
        """`elenchus costs` (a real subcommand run) must configure
        logging before it does anything."""
        calls = []
        monkeypatch.setattr(srv, "_configure_logging", lambda c: calls.append(c))
        monkeypatch.setattr(srv, "_run_costs", lambda a: calls.append("ran"))
        monkeypatch.setattr(srv.sys, "argv", ["elenchus", "costs"])
        srv.main()
        assert calls == ["costs", "ran"]
