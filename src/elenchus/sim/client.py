"""
client.py — per-actor HTTP client + step recorder for the simulation.

Each simulated actor (the researcher, each participant, each judge)
gets its own `SimClient`, which wraps a FastAPI `TestClient` with an
isolated cookie jar — the stand-in for a separate browser session.
Every request is timed and recorded as a `StepRecord` on a shared
`Recorder`, which is what the report renderer reads.

`TestClient` runs the full ASGI app — real routing, real auth, real
DuckDB, real cookies — over an in-process transport. It is "real HTTP"
in every sense except the TCP socket, which is exactly what makes the
harness CI-able without port management while still exercising the
integration surface.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from fastapi.testclient import TestClient


@dataclass
class StepRecord:
    """One recorded interaction. `ok` is the 2xx test; the report's
    problems list is every record where `ok` is False and the caller
    didn't mark the non-2xx as expected."""

    actor: str
    action: str
    method: str
    path: str
    status: int
    latency_ms: int
    ok: bool
    note: str = ""
    expected_non_2xx: bool = False


@dataclass
class Recorder:
    steps: list[StepRecord] = field(default_factory=list)

    def record(self, step: StepRecord) -> None:
        self.steps.append(step)


def _safe_json(resp) -> Any:
    try:
        return resp.json()
    except Exception:
        return None


class SimClient:
    """A single actor's HTTP session against the app under test."""

    def __init__(self, name: str, app, recorder: Recorder):
        self.name = name
        self._tc = TestClient(app)
        self._rec = recorder

    def set_session_cookie(self, token: str) -> None:
        from .. import auth

        self._tc.cookies.set(auth.SESSION_COOKIE, token)

    def request(
        self,
        method: str,
        path: str,
        *,
        action: str,
        json: Any = None,
        note: str = "",
        expect_non_2xx: bool = False,
    ) -> tuple[int, Any]:
        t0 = time.monotonic()
        resp = self._tc.request(method, path, json=json)
        dt = int((time.monotonic() - t0) * 1000)
        ok = 200 <= resp.status_code < 300
        self._rec.record(
            StepRecord(
                actor=self.name,
                action=action,
                method=method,
                path=path,
                status=resp.status_code,
                latency_ms=dt,
                ok=ok,
                note=note,
                expected_non_2xx=expect_non_2xx,
            )
        )
        return resp.status_code, _safe_json(resp)

    def get(self, path: str, **kw) -> tuple[int, Any]:
        return self.request("GET", path, **kw)

    def post(self, path: str, json: Any = None, **kw) -> tuple[int, Any]:
        return self.request("POST", path, json=json, **kw)

    def delete(self, path: str, **kw) -> tuple[int, Any]:
        return self.request("DELETE", path, **kw)
