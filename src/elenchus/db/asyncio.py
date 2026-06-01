"""
asyncio.py — helpers for running DuckDB work from async route handlers.

DuckDB's Python API is synchronous. From a `async def` FastAPI route
handler, calling `con.execute(...)` directly blocks the event loop for
the duration of the query. For most queries this is sub-millisecond and
fine; the helpers here let us push longer operations onto a worker
thread when needed.

**When to use `run_in_db`:**
- Operations that touch DuckDB and may take meaningful time
  (multi-statement state mutations, derivation sweeps, exports)
- Operations that will be wrapped by a per-base async lock in D5

**When NOT to use it:**
- Fast SELECTs from async routes — the thread-switch overhead exceeds
  the work, just call directly
- Sync code paths (CLI, tests) — they don't have an event loop

D4 scope: this helper exists and is correct, but route handlers that
acquire the per-base async lock land in D5. The pattern is in place so
D5's per-base locking integrates cleanly.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")


async def run_in_db(handle: Any, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run `fn(handle.state.base.con, *args, **kwargs)` in a worker
    thread, awaiting the result.

    `handle` is a `BaseHandle` from the registry. The function receives
    the underlying DuckDB connection as its first argument. Use this
    when an async route needs to run a non-trivial DuckDB operation
    without blocking the event loop:

        result = await run_in_db(handle, lambda con: con.execute(...).fetchall())
    """

    def _runner() -> T:
        return fn(handle.state.base.con, *args, **kwargs)

    return await asyncio.to_thread(_runner)


async def run_blocking(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Generic alias for `asyncio.to_thread` with a project-specific
    name. Use for synchronous operations called from async route
    handlers that don't need the BaseHandle convention of `run_in_db`
    — e.g. opponent state mutations, PDF generation."""
    return await asyncio.to_thread(fn, *args, **kwargs)


__all__ = ["run_blocking", "run_in_db"]
