"""
registry.py — process-wide registry of DuckDB connections.

The `DBRegistry` is the single point of ownership for all DuckDB
connections in the process. It exposes:

- a lazily-opened, never-evicted `platform_con` for platform.duckdb
  (added in Week 2 of Phase A)
- a bounded LRU cache of `BaseHandle` instances, one per active per-base
  database file

For Week 1 of Phase A, the registry mirrors the previous `_states` dict
behavior — same lazy load, no eviction, no per-base locks. The LRU bound,
idle-TTL eviction, and per-base `asyncio.Lock` are added in Week 1 D5 and
later. The interface here is shaped to absorb those additions without
churning callers.

**Concurrency model.** All registry-dict mutations are guarded by a
single `threading.Lock`. The per-handle async lock that arrives in D5 is
a separate primitive held only by request-handling code; the registry
lock is held only across the OrderedDict mutation, never across
connection use or LLM calls.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import duckdb

if TYPE_CHECKING:
    from ..dialectical_state import DialecticalState

logger = logging.getLogger(__name__)

# Configuration. Not currently env-driven; intentional. One value, in code,
# in this module. Tune with care: each open .duckdb file is at least one
# file descriptor.
DEFAULT_CAPACITY = 64

# Soft warning threshold for RLIMIT_NOFILE. Below this we log a warning at
# startup recommending the operator raise the limit. We do not enforce.
RLIMIT_WARN_THRESHOLD = 256


@dataclass
class BaseHandle:
    """Wraps a per-base DialecticalState with metadata for the registry.

    The `_lock` is created lazily on first access via the `lock`
    property. `asyncio.Lock` must be instantiated inside a running event
    loop, but a BaseHandle may be constructed either in async context
    (from a route handler) or sync (from a CLI / test setup). Lazy
    creation lets the same handle work in both worlds.

    Write contract for callers (route handlers): acquire `handle.lock`
    around any DuckDB *mutation* on the wrapped state. Reads (computing
    formal_state, fetching conversation) do not require the lock; they
    rely on DuckDB MVCC for consistent snapshots. The lock should be
    *released* across the LLM call so concurrent tabs don't freeze each
    other for the 5–30 s LLM wait — see the strict-serialize-default-off
    discussion in ROADMAP.md.
    """

    state: DialecticalState
    last_used: float = field(default_factory=time.monotonic)
    _lock: asyncio.Lock | None = field(default=None, repr=False)

    @property
    def lock(self) -> asyncio.Lock:
        """Lazy-initialized per-base async lock. Created on first
        access; safe to call from any async context."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def touch(self) -> None:
        """Mark this handle as recently used (for LRU ordering)."""
        self.last_used = time.monotonic()


class DBRegistry:
    """Process-wide owner of DuckDB connections.

    Week 1 D1-2 scope: single in-process cache, no eviction, no async
    locks. The class structure exists so subsequent Phase A work can
    plug LRU bounding, idle-TTL eviction, and per-base locks in without
    touching call sites.
    """

    def __init__(
        self,
        data_dir: str,
        capacity: int = DEFAULT_CAPACITY,
        platform_path: str | None = None,
    ) -> None:
        self._data_dir = data_dir
        self._capacity = capacity
        self._platform_path = platform_path or os.path.join(data_dir, "platform.duckdb")
        # OrderedDict so we can promote-on-access for LRU ordering in D5.
        self._handles: OrderedDict[str, BaseHandle] = OrderedDict()
        # Guards _handles mutations only. Held briefly during get/put/remove.
        # Never held during connection use, LLM calls, or migrations.
        self._registry_lock = threading.Lock()
        # Platform connection: lazily opened on first access and held
        # for the registry's lifetime. Guarded by its own lock since
        # writes to platform.duckdb happen from any actor's auth check.
        # RLock (not Lock) so callers can re-enter — `auth.create_session`
        # holds the lock and then calls `platform_con()` which also
        # acquires it for the lazy-init check.
        self._platform_con: duckdb.DuckDBPyConnection | None = None
        self._platform_lock = threading.RLock()

        self._check_fd_limit()

    # ── File handle hygiene ──

    @staticmethod
    def _check_fd_limit() -> None:
        """Log a warning if RLIMIT_NOFILE is dangerously low."""
        try:
            import resource

            soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
            if soft < RLIMIT_WARN_THRESHOLD:
                logger.warning(
                    "RLIMIT_NOFILE soft limit is %d (recommended >= %d). "
                    "Consider raising it (e.g. `ulimit -n 4096`) before "
                    "production deployment; each open base file consumes "
                    "at least one descriptor.",
                    soft,
                    RLIMIT_WARN_THRESHOLD,
                )
        except (ImportError, OSError):
            # resource module unavailable (Windows) or rlimit query failed.
            # Not fatal; just skip the warning.
            pass

    # ── Platform connection ──

    @property
    def platform_path(self) -> str:
        return self._platform_path

    def platform_con(self) -> duckdb.DuckDBPyConnection:
        """Return the platform.duckdb connection, opening it on first
        access. The connection is held for the registry's lifetime —
        every request touches platform.duckdb (auth, authorization,
        session lookup) so eviction is wasteful.

        Callers that *write* to the platform DB must hold
        `platform_lock` (acquired via `acquire_platform_lock()`).
        Read-only callers (auth_sessions lookup, base ownership check)
        can read concurrently — DuckDB MVCC handles this.
        """
        with self._platform_lock:
            if self._platform_con is None:
                # Ensure parent dir exists. data_dir was created by
                # server.py at import time; platform_path may be a
                # different location (custom override).
                os.makedirs(os.path.dirname(self._platform_path) or ".", exist_ok=True)
                self._platform_con = duckdb.connect(self._platform_path)
                logger.info("Opened platform DB at %s", self._platform_path)
            return self._platform_con

    def migrate_platform(self) -> int:
        """Apply any unapplied platform-DB migrations. Idempotent.
        Returns the schema version after migration.

        Called once from server.py during FastAPI lifespan startup,
        before any request is accepted.
        """
        # Local import to avoid a circular dependency: the migrations
        # module imports from elsewhere that may import from here.
        from ..migrations import apply_migrations

        con = self.platform_con()
        with self._platform_lock:
            return apply_migrations(con, "platform")

    @property
    def platform_lock(self) -> threading.RLock:
        """Returns the platform-write lock (reentrant). Use as a context
        manager around any write to platform.duckdb to serialize
        writers; re-entrant from the same thread, so calling
        `platform_con()` while holding the lock is safe."""
        return self._platform_lock

    # ── Path resolution ──

    def db_path(self, name: str) -> str:
        """Compute the on-disk path for a given dialectic name.

        Week 1: same flat layout as the previous `_db_path`. Week 3
        restructures this to `bases/{actor_id}/{base_id}.duckdb` once
        bases gain proper IDs and ownership.
        """
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
        return os.path.join(self._data_dir, f"{safe}.duckdb")

    # ── Handle access ──

    def get_handle(self, name: str) -> BaseHandle:
        """Return the `BaseHandle` for `name`, opening it from disk on
        first access. Same semantics as `get()` but returns the
        full handle (including the per-base async lock) rather than
        just the underlying state.
        """
        # Reuse `get()` to do the open/cache work; then look up the handle.
        self.get(name)  # ensures it's loaded
        with self._registry_lock:
            handle = self._handles[name]
            handle.touch()
            self._handles.move_to_end(name)
            return handle

    def get(self, name: str) -> DialecticalState:
        """Return the cached DialecticalState for `name`, opening it from
        disk on first access. Raises ValueError if the file is missing
        or corrupt — callers translate to appropriate HTTP responses.
        """
        with self._registry_lock:
            handle = self._handles.get(name)
            if handle is not None:
                handle.touch()
                self._handles.move_to_end(name)
                return handle.state

        # Slow path: open from disk. Do this outside the registry lock so
        # the (potentially slow) DialecticalState.open() doesn't block
        # other registry operations.
        from ..dialectical_state import DialecticalState

        path = self.db_path(name)
        if not os.path.exists(path):
            raise FileNotFoundError(f"No dialectic file at {path}")
        state = DialecticalState.open(path)  # may raise ValueError on corruption

        with self._registry_lock:
            # Re-check in case of a race: another thread may have opened
            # the same name. Use the first winner; close the duplicate.
            existing = self._handles.get(name)
            if existing is not None:
                try:
                    state.base.con.close()
                except Exception:
                    logger.warning(
                        "Failed to close duplicate connection for '%s'", name, exc_info=True
                    )
                existing.touch()
                self._handles.move_to_end(name)
                return existing.state

            handle = BaseHandle(state=state)
            self._handles[name] = handle
            self._handles.move_to_end(name)
            # Note: LRU eviction enforcement arrives in D5. For now the
            # cache grows unbounded, matching prior `_states` behavior.
            return state

    def put(self, name: str, state: DialecticalState) -> None:
        """Insert a freshly-created DialecticalState into the registry.

        Used by the create-dialectic path, which constructs the state
        before storing it. Closes and replaces any existing handle for
        the same name (matching prior `_states[name] = state` behavior).
        """
        with self._registry_lock:
            existing = self._handles.pop(name, None)
            if existing is not None:
                try:
                    existing.state.base.con.close()
                except Exception:
                    logger.warning(
                        "Failed to close replaced connection for '%s'", name, exc_info=True
                    )
            self._handles[name] = BaseHandle(state=state)
            self._handles.move_to_end(name)

    def remove(self, name: str) -> bool:
        """Evict and close the handle for `name`. Returns True if a
        handle was removed, False if it wasn't in the cache."""
        with self._registry_lock:
            handle = self._handles.pop(name, None)
        if handle is None:
            return False
        try:
            handle.state.base.con.close()
        except Exception:
            logger.warning(
                "Failed to close connection for '%s' during remove", name, exc_info=True
            )
        return True

    def close_all(self) -> None:
        """Close every cached connection. Called from FastAPI lifespan
        shutdown to release file locks and flush WAL files."""
        with self._registry_lock:
            handles = list(self._handles.items())
            self._handles.clear()
        for name, handle in handles:
            try:
                handle.state.base.con.close()
                logger.info("Closed DuckDB connection for '%s'", name)
            except Exception:
                logger.warning("Failed to close DuckDB connection for '%s'", name, exc_info=True)
        # Close the platform connection too.
        with self._platform_lock:
            if self._platform_con is not None:
                try:
                    self._platform_con.close()
                    logger.info("Closed platform DB connection")
                except Exception:
                    logger.warning("Failed to close platform DB connection", exc_info=True)
                self._platform_con = None

    # ── Introspection ──

    def __contains__(self, name: str) -> bool:
        with self._registry_lock:
            return name in self._handles

    def __len__(self) -> int:
        with self._registry_lock:
            return len(self._handles)

    @property
    def capacity(self) -> int:
        return self._capacity


# ── Process-wide singleton ──
#
# Lazily initialized via init_registry() so test suites can set a custom
# data_dir before any request handlers run. Importing `registry` from the
# package returns this module-level binding.
registry: DBRegistry | None = None


def init_registry(
    data_dir: str,
    capacity: int = DEFAULT_CAPACITY,
    platform_path: str | None = None,
) -> DBRegistry:
    """Initialize the process-wide registry. Idempotent: replacing an
    existing registry closes its connections first.

    Callers (server.py at module import, tests at fixture setup) invoke
    this exactly once per process lifetime. Subsequent reads come through
    `get_registry()`.
    """
    global registry
    if registry is not None:
        registry.close_all()
    registry = DBRegistry(data_dir=data_dir, capacity=capacity, platform_path=platform_path)
    return registry


def get_registry() -> DBRegistry:
    """Return the process-wide registry. Raises if init_registry has not
    been called — this is a programming error, not a user-facing one."""
    if registry is None:
        raise RuntimeError(
            "DBRegistry not initialized. Call db.init_registry(data_dir) "
            "before accessing the registry (typically at server startup)."
        )
    return registry
