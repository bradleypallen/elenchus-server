"""
audit.py — operational checks across the platform DB and per-base files.

DuckDB does not enforce foreign keys across separate database files;
this module walks the data layout and reports drift between what the
platform DB says exists and what's actually on disk. Used by both the
`elenchus audit` CLI (run by operators) and the `/api/admin/audit`
endpoint (admin dashboard surface, if/when a UI lands).

What it checks:
  * registered_with_file       — `bases` row + scoped file exists
  * registered_missing_file    — `bases` row but file is gone
  * orphan_scoped              — file under `bases/{owner}/…` with no row
  * orphan_flat                — flat-layout `.duckdb` with no row (pre-migration)
  * actor_refs_with_no_actor   — per-base contributor_id/actor_id pointing
                                 at an actor that doesn't exist (cross-DB
                                 drift)

The audit reopens per-base files read-only via `MaterialBase.open` so
the migration runner stays consistent; this means an audit run also
checks that every base parses cleanly.
"""

from __future__ import annotations

import glob
import logging
import os

from .db import get_registry
from .db import platform as pdb
from .material_base import MaterialBase

logger = logging.getLogger(__name__)


def _walk_scoped_files(data_dir: str) -> list[tuple[int, str, str]]:
    """Yield (owner_id, base_name, path) for every file under
    `bases/{owner_id}/{name}.duckdb`."""
    out: list[tuple[int, str, str]] = []
    root = os.path.join(data_dir, "bases")
    if not os.path.isdir(root):
        return out
    for owner_dir in sorted(os.listdir(root)):
        owner_path = os.path.join(root, owner_dir)
        if not os.path.isdir(owner_path):
            continue
        try:
            owner_id = int(owner_dir)
        except ValueError:
            # Unexpected subdir name; report it as unscoped.
            continue
        for f in sorted(os.listdir(owner_path)):
            if f.endswith(".duckdb"):
                out.append((owner_id, os.path.splitext(f)[0], os.path.join(owner_path, f)))
    return out


def _walk_flat_files(data_dir: str) -> list[tuple[str, str]]:
    """Yield (base_name, path) for every flat-layout .duckdb file at
    the top of `data_dir`. Skips `platform.duckdb`."""
    out: list[tuple[str, str]] = []
    if not os.path.isdir(data_dir):
        return out
    for f in sorted(glob.glob(os.path.join(data_dir, "*.duckdb"))):
        base = os.path.basename(f)
        if base == "platform.duckdb":
            continue
        out.append((os.path.splitext(base)[0], f))
    return out


def _actor_refs_in_base(path: str) -> dict:
    """Return the set of distinct actor IDs referenced in a per-base
    file's contributor_id / actor_id columns. Logs and returns empty
    on parse failure rather than aborting the audit."""
    refs: set[int] = set()
    try:
        mb = MaterialBase.open(path)
    except Exception as e:
        logger.warning("audit: failed to open %s: %s", path, e)
        return {"refs": set(), "open_error": str(e)}
    try:
        for table, col in (
            ("atoms", "contributor_id"),
            ("assessments", "contributor_id"),
            ("positions", "actor_id"),
            ("conversation", "actor_id"),
        ):
            try:
                rows = mb.con.execute(
                    f"SELECT DISTINCT {col} FROM {table} WHERE {col} IS NOT NULL"
                ).fetchall()
                for (rid,) in rows:
                    if rid is not None:
                        refs.add(int(rid))
            except Exception as e:
                # Column may not exist yet on an unmigrated file; skip it
                # and keep going.
                logger.debug("audit: skipped %s.%s on %s: %s", table, col, path, e)
    finally:
        mb.con.close()
    return {"refs": refs, "open_error": None}


def audit_platform(data_dir: str) -> dict:
    """Walk the platform DB and the data directory; return a report
    describing drift between the two."""
    reg = get_registry()
    con = reg.platform_con()

    bases_rows = {b["id"]: b for b in pdb.list_bases(con)}
    actors_by_id = {a["id"]: a for a in pdb.list_actors(con, include_deactivated=True)}

    scoped = _walk_scoped_files(data_dir)
    flat = _walk_flat_files(data_dir)

    # Group scoped files by base name; we expect base ids to be unique.
    scoped_by_name: dict[str, tuple[int, str]] = {n: (o, p) for o, n, p in scoped}

    registered_with_file: list[dict] = []
    registered_missing_file: list[dict] = []
    orphan_scoped: list[dict] = []
    orphan_flat: list[dict] = []
    dangling_actor_refs: list[dict] = []

    # 1. Registered bases: file present or missing?
    for base_id, row in bases_rows.items():
        if base_id in scoped_by_name:
            owner_id, path = scoped_by_name[base_id]
            registered_with_file.append(
                {
                    "id": base_id,
                    "owner_id": row["owner_id"],
                    "file_owner_id": owner_id,
                    "path": path,
                    "path_owner_matches_row": owner_id == row["owner_id"],
                }
            )
        else:
            registered_missing_file.append({"id": base_id, "owner_id": row["owner_id"]})

    # 2. Orphan files: scoped or flat, with no `bases` row.
    for owner_id, base_id, path in scoped:
        if base_id not in bases_rows:
            orphan_scoped.append({"id": base_id, "owner_id": owner_id, "path": path})
    for base_id, path in flat:
        if base_id not in bases_rows:
            orphan_flat.append({"id": base_id, "path": path})

    # 3. Cross-DB actor refs: for every base file we can open, list
    # actor IDs referenced that don't exist in platform.actors.
    paths_to_check = [r["path"] for r in registered_with_file] + [
        o["path"] for o in orphan_scoped + orphan_flat
    ]
    for path in paths_to_check:
        info = _actor_refs_in_base(path)
        missing = sorted(rid for rid in info["refs"] if rid not in actors_by_id)
        if missing:
            dangling_actor_refs.append({"path": path, "missing_actor_ids": missing})

    summary = {
        "registered_with_file": registered_with_file,
        "registered_missing_file": registered_missing_file,
        "orphan_scoped": orphan_scoped,
        "orphan_flat": orphan_flat,
        "dangling_actor_refs": dangling_actor_refs,
        "actor_count": len(actors_by_id),
        "base_row_count": len(bases_rows),
        "scoped_file_count": len(scoped),
        "flat_file_count": len(flat),
    }
    return summary


def format_report(report: dict) -> str:
    """Pretty-print an audit report as a multi-line string for the CLI."""
    out: list[str] = []
    out.append("─── Elenchus audit ─────────────────────────────────────")
    out.append(f"  actors:           {report['actor_count']}")
    out.append(f"  registered bases: {report['base_row_count']}")
    out.append(f"  scoped files:     {report['scoped_file_count']}")
    out.append(f"  flat files:       {report['flat_file_count']}")
    out.append("")

    def section(label: str, rows: list, formatter):
        if not rows:
            out.append(f"  ✓ {label}: none")
            return
        out.append(f"  ⚠ {label}: {len(rows)}")
        for r in rows:
            out.append(f"      {formatter(r)}")

    section(
        "registered files missing on disk",
        report["registered_missing_file"],
        lambda r: f"base={r['id']!r} (owner_id={r['owner_id']})",
    )
    section(
        "orphan scoped files (no `bases` row)",
        report["orphan_scoped"],
        lambda r: f"{r['path']} (owner={r['owner_id']})",
    )
    section(
        "orphan flat-layout files (pre-migrate-legacy)",
        report["orphan_flat"],
        lambda r: f"{r['path']}",
    )
    section(
        "bases whose file owner directory != bases.owner_id",
        [r for r in report["registered_with_file"] if not r["path_owner_matches_row"]],
        lambda r: f"base={r['id']!r}: row owner_id={r['owner_id']} but file under owner_id={r['file_owner_id']}",
    )
    section(
        "dangling actor refs (cross-DB drift)",
        report["dangling_actor_refs"],
        lambda r: f"{r['path']}: missing actors {r['missing_actor_ids']}",
    )

    out.append("")
    return "\n".join(out)
