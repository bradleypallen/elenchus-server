"""
Fixtures for the browser-driven UI E2E tests.

These are heavier than the rest of the suite — they launch a real
`elenchus` server in a subprocess and drive it with a headless
chromium — so they are OFF by default. They collect and run only when
`RUN_UI_E2E=1` is set AND Playwright + its browser are installed:

    pip install -e ".[e2e]" && python -m playwright install chromium
    RUN_UI_E2E=1 pytest tests/e2e/

The server runs against an isolated temp data dir seeded by a separate
process (`seed.py`) before launch, so DuckDB's single-writer lock is
never contended between the seeder and the server.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

_ENABLED = os.environ.get("RUN_UI_E2E") == "1"

# Don't even collect the browser tests unless explicitly enabled — keeps
# the default `pytest` run fast and free of the Playwright dependency.
collect_ignore_glob = [] if _ENABLED else ["test_*.py"]

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="session")
def live_server():
    """Seed an isolated data dir, launch `elenchus` against it, wait for
    readiness, and yield connection details + seeded credentials."""
    import requests

    data_dir = tempfile.mkdtemp(prefix="elenchus_e2e_")
    env = {
        **os.environ,
        "ELENCHUS_DATA": data_dir,
        "SESSION_COOKIE_SECURE": "false",  # http in the test
        "ELENCHUS_API_KEY": os.environ.get("ELENCHUS_API_KEY", "sk-e2e-dummy"),
        "BCRYPT_ROUNDS": "4",  # keep seeded-password hashing fast
    }

    # 1. Seed everything the browser tests need (separate process so the
    #    DuckDB writer lock is released before the server opens the file).
    seeded = json.loads(
        subprocess.check_output(
            [sys.executable, str(_HERE / "seed.py"), data_dir], env=env, text=True
        )
    )

    # 2. Launch the real server.
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        ["elenchus", "serve", "--port", str(port)],
        env=env,
        cwd=str(_REPO),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    # 3. Wait for /healthz.
    deadline = time.monotonic() + 30
    ready = False
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read() if proc.stdout else ""
            raise RuntimeError(f"server exited early (code {proc.returncode}):\n{out}")
        try:
            if requests.get(f"{base_url}/healthz", timeout=1).status_code == 200:
                ready = True
                break
        except requests.RequestException:
            time.sleep(0.25)
    if not ready:
        proc.terminate()
        raise RuntimeError("server did not become ready within 30s")

    try:
        yield SimpleNamespace(base_url=base_url, data_dir=data_dir, **seeded)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture(scope="session")
def _browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        yield browser
        browser.close()


@pytest.fixture
def page(_browser):
    """A fresh browser context (isolated cookie jar) + page per test."""
    context = _browser.new_context()
    pg = context.new_page()
    yield pg
    context.close()
