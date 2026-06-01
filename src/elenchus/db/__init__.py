"""
db — database registry and connection management for Elenchus.

The `DBRegistry` owns the lifecycle of DuckDB connections across the
process. It will eventually hold the platform.duckdb connection and a
bounded LRU cache of per-base connections; for now it mirrors the
single-cache behavior of the previous `_states` dict, with the structure
in place for the LRU and per-base locking that arrives later in Phase A.

**Critical design constraint:** Elenchus is built around a single writer
process. DuckDB supports multi-threaded writes within one process via
MVCC, but multi-process writes require the experimental Quack remote
protocol (beta as of v1.5.2). Do not introduce a second writer process.
The scale path beyond a single process is Postgres, not multi-process
DuckDB.
"""

from .registry import BaseHandle, DBRegistry, get_registry, init_registry, registry

__all__ = [
    "BaseHandle",
    "DBRegistry",
    "get_registry",
    "init_registry",
    "registry",
]
