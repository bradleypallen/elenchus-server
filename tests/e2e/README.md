# Browser-driven UI end-to-end tests

These drive the **real served React frontend** in a headless Chromium
against a live `elenchus` server. Where `tests/test_sim.py` and the
access-probe phase exercise the *API*, these confirm the *user
experience* holds up: the login / signup / magic-link forms actually
submit and route, a participant's study link drops them into the
briefing, the judge view renders a blinded package, and a wrong password
produces a graceful error rather than a broken page.

They are **off by default** — heavy (a real server subprocess + a
browser) and not part of the unit run. They collect only when
`RUN_UI_E2E=1`.

## Running

```bash
pip install -e ".[e2e]"
python -m playwright install chromium     # one-time browser download
RUN_UI_E2E=1 pytest tests/e2e/
```

## How it works

- `seed.py` runs as its **own process** before the server starts and
  populates an isolated temp data dir (admin, an invite token, a
  participant study token, and a fully-formed blinded judge assignment).
  Running it separately means DuckDB's single-writer lock is released
  before the server opens the file.
- `conftest.py` launches `elenchus serve` against that dir on an
  ephemeral port, waits for `/healthz`, and exposes a Playwright `page`.
- `test_ui.py` drives the flows. The judge test injects the seeded
  session cookie (login forms are already covered by the admin/signup
  tests) so the blinding assertion isn't coupled to login-form timing.

## Why this matters

This layer caught a real server bug that every sequential API test
missed: the single platform DuckDB connection was not safe for the
concurrent authenticated requests a browser fires on load, so session
lookups intermittently returned 401 and bounced users to the login
screen. The fix is `registry._SerializedConnection`; the regression
guard is `tests/test_concurrency.py::TestPlatformConnectionConcurrency`.
