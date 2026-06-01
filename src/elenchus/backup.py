"""
backup.py — snapshot the platform DB and every per-base DuckDB file.

DuckDB's `EXPORT DATABASE 'path/'` writes a consistent SQL+parquet dump
of the database the connection is attached to. The snapshot is taken
under DuckDB's MVCC, so concurrent writers see no impact and the dump
reflects the database state at the moment the statement begins. We
collect the platform dump and one dump per registered base into a
timestamped staging directory, then tar.gz the lot.

This module runs **in-process**: it must be called from within the
FastAPI server (an admin route) or a test, because it reuses the
running registry's connections rather than opening new file handles
(which would conflict with DuckDB's single-writer-per-file lock).
The `scripts/backup.py` standalone CLI invokes the admin route via
HTTP; it does not call `make_backup` directly.

Retention is simple LRU: the N most recent archives are kept, older
ones are deleted. The default (14) matches the Phase A ROADMAP's
"14 dailies" recommendation. Weekly snapshots can be layered on top
by an external cron rotation.
"""

from __future__ import annotations

import datetime
import glob
import logging
import os
import shutil
import tarfile

from .db import get_registry
from .db import platform as pdb

logger = logging.getLogger(__name__)

DEFAULT_RETENTION = 14
ARCHIVE_GLOB = "elenchus-*.tar.gz"


def _safe_sql_literal(path: str) -> str:
    """Escape a filesystem path for inclusion in a single-quoted SQL
    string literal. DuckDB does not support bound parameters for
    `EXPORT DATABASE` paths, so we inline the path; the only special
    case is a literal single quote, which is doubled."""
    return path.replace("'", "''")


def make_backup(data_dir: str, output_dir: str | None = None) -> dict:
    """Snapshot the platform DB and every registered base into a single
    tar.gz archive. Returns metadata about the archive.

    The archive lives at ``{output_dir}/elenchus-{YYYYmmdd-HHMMSS}.tar.gz``
    (default: ``{data_dir}/backups/``). The staging directory is removed
    after the archive is sealed.
    """
    if output_dir is None:
        output_dir = os.path.join(data_dir, "backups")
    os.makedirs(output_dir, exist_ok=True)

    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    staging = os.path.join(output_dir, f".staging-{ts}")
    os.makedirs(staging)

    bases_dumped: list[str] = []
    bases_failed: list[dict] = []
    try:
        reg = get_registry()

        # Platform DB.
        platform_dst = os.path.join(staging, "platform")
        os.makedirs(platform_dst, exist_ok=True)
        with reg.platform_lock:
            reg.platform_con().execute(f"EXPORT DATABASE '{_safe_sql_literal(platform_dst)}'")
        logger.info("Backed up platform DB → %s", platform_dst)

        # Per-base DBs. We iterate via `list_bases` rather than walking
        # the filesystem so legacy flat-layout files that haven't been
        # registered yet are flagged as "unregistered" rather than
        # silently included or silently dropped.
        bases = pdb.list_bases(reg.platform_con())
        for base in bases:
            base_id = base["id"]
            base_dst = os.path.join(staging, "bases", base_id)
            try:
                os.makedirs(base_dst, exist_ok=True)
                state = reg.get(base_id)
                state.base.con.execute(f"EXPORT DATABASE '{_safe_sql_literal(base_dst)}'")
                bases_dumped.append(base_id)
                logger.info("Backed up base %r → %s", base_id, base_dst)
            except Exception as e:
                logger.exception("Backup failed for base %r", base_id)
                bases_failed.append({"id": base_id, "error": str(e)})

        # Seal the archive.
        archive_path = os.path.join(output_dir, f"elenchus-{ts}.tar.gz")
        with tarfile.open(archive_path, "w:gz") as tar:
            tar.add(staging, arcname=f"elenchus-{ts}")

        return {
            "archive": archive_path,
            "timestamp": ts,
            "bases_dumped": bases_dumped,
            "bases_failed": bases_failed,
        }
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def list_backups(output_dir: str) -> list[str]:
    """Return absolute paths of every backup archive in `output_dir`,
    newest first."""
    if not os.path.isdir(output_dir):
        return []
    archives = glob.glob(os.path.join(output_dir, ARCHIVE_GLOB))
    return sorted(archives, reverse=True)


def prune_backups(output_dir: str, keep: int = DEFAULT_RETENTION) -> list[str]:
    """Delete every archive beyond the `keep` newest. Returns the list
    of paths actually removed."""
    if keep < 0:
        raise ValueError("keep must be non-negative")
    archives = list_backups(output_dir)
    removed: list[str] = []
    for old in archives[keep:]:
        try:
            os.remove(old)
            removed.append(old)
            logger.info("Pruned old backup %s", old)
        except OSError as e:
            logger.warning("Failed to prune %s: %s", old, e)
    return removed
