"""
runner.py — discover and apply SQL migrations to a DuckDB connection.

Migrations are SQL files under `src/elenchus/migrations/<kind>/`, where
`<kind>` is `"platform"` (for platform.duckdb) or `"base"` (for per-base
files). Each file is named `NNNN_description.sql` and begins with a
`-- version: N` comment header that the runner uses to determine
ordering.

The runner is forward-only: it applies migrations strictly greater than
the current `meta.schema_version`. Reversal requires restoring from
backup. Each migration runs inside its own transaction; a failure leaves
the database at the previous version with no partial state.

The very first base migration (`0001_initial.sql`) creates the `meta`
table itself, so the runner handles the bootstrap case where
`meta.schema_version` doesn't exist yet by treating the current version
as 0.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

MIGRATIONS_ROOT = Path(__file__).parent

VERSION_HEADER_RE = re.compile(r"^--\s*version:\s*(\d+)", re.MULTILINE)

MigrationKind = Literal["platform", "base"]


def list_migrations(kind: MigrationKind) -> list[tuple[int, Path]]:
    """Return all migration files for the given kind, sorted by version.

    Each file's version comes from its `-- version: N` header. A missing
    header is a programming error (the migration was malformed).
    """
    directory = MIGRATIONS_ROOT / kind
    if not directory.is_dir():
        return []

    out: list[tuple[int, Path]] = []
    for path in sorted(directory.glob("*.sql")):
        text = path.read_text(encoding="utf-8")
        match = VERSION_HEADER_RE.search(text)
        if not match:
            raise ValueError(
                f"Migration {path} is missing the '-- version: N' header. "
                f"Add the header at the top of the file."
            )
        out.append((int(match.group(1)), path))

    out.sort(key=lambda pair: pair[0])

    # Sanity check: versions are unique and monotonically increasing.
    seen: set[int] = set()
    for version, path in out:
        if version in seen:
            raise ValueError(f"Duplicate migration version {version} (file: {path})")
        seen.add(version)

    return out


def current_schema_version(con) -> int:
    """Read the current schema version from `meta.schema_version`.

    Returns 0 if the `meta` table doesn't exist yet (fresh database) or
    if the `schema_version` row is absent.
    """
    try:
        row = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    except Exception:
        # `meta` table doesn't exist yet — fresh database.
        return 0
    if not row or not row[0]:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError):
        logger.warning("meta.schema_version has non-integer value %r; treating as 0", row[0])
        return 0


def apply_migrations(con, kind: MigrationKind) -> int:
    """Apply all unapplied migrations for the given kind. Returns the
    schema version after migration.

    Each migration runs inside its own transaction. On failure, the
    transaction rolls back and the exception propagates; the database
    remains at the previous version.
    """
    starting = current_schema_version(con)
    available = list_migrations(kind)

    if not available:
        logger.debug("No migrations found for kind=%s", kind)
        return starting

    last_applied = starting
    for version, path in available:
        if version <= starting:
            continue

        logger.info("Applying %s migration v%d: %s", kind, version, path.name)
        sql = path.read_text(encoding="utf-8")

        con.execute("BEGIN")
        try:
            con.execute(sql)
            # Upsert pattern works for both DuckDB and (eventually) Postgres.
            con.execute("DELETE FROM meta WHERE key='schema_version'")
            con.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', ?)", [str(version)]
            )
            con.execute("COMMIT")
        except Exception:
            try:
                con.execute("ROLLBACK")
            except Exception:
                logger.exception("Rollback failed during migration v%d", version)
            raise

        last_applied = version

    if last_applied > starting:
        logger.info("Schema %s upgraded from v%d to v%d", kind, starting, last_applied)
    return last_applied
