"""
migrations — schema migration runner for Elenchus DuckDB files.

Each database (the process-wide `platform.duckdb` and each per-base
`*.duckdb`) carries a `meta.schema_version` row. The runner reads it,
finds any SQL files under `migrations/<kind>/` with a higher version,
and applies them in order inside per-file transactions.

Migration files are named `NNNN_description.sql` and start with a
`-- version: N` header. Migrations are forward-only; reversal happens
through restore from backup (see scripts/backup.py).

Usage:

    from elenchus.migrations import apply_migrations

    con = duckdb.connect(path)
    new_version = apply_migrations(con, "base")
"""

from .runner import apply_migrations, current_schema_version, list_migrations

__all__ = ["apply_migrations", "current_schema_version", "list_migrations"]
