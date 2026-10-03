"""
research_export.py — the dialectics whose owners said yes.

An account holder's ordinary dialectics are used in the project's
research only if that person turned the research-use choice on
(notice.py; docs/data-access.md, policy version 5). This module builds
the one artefact that use starts from: a tar.gz, under
`{data_dir}/exports/research/`, holding the records of every ordinary
dialectic whose owner's choice is **on at the moment of export**, each
owner replaced by a pseudonym (`U-001`, …) whose key is written to a
separate, admin-only side file — the same arrangement as a study export.

Each dialectic taken is also a records fetch under the reason *Owner
agreed to analysis*, written to the access log like any other, so the
owner's access notes show that the export happened.

A choice turned off afterwards is honoured from then on: nothing of that
person's goes into a later export. What an earlier export already holds
is the research team's to handle under the policy.
"""

from __future__ import annotations

import logging
import os
import re
import tarfile
import tempfile
from datetime import UTC, datetime

from . import content_access, notice
from .db import platform as pdb
from .db.registry import sanitize_base_name

logger = logging.getLogger(__name__)

FORMAT = "elenchus-research-export"
FORMAT_VERSION = "1"
SUBDIR = "research"
_NAME = re.compile(r"^research-(\d{8}T\d{6}Z)\.tar\.gz$")


def exports_dir(data_dir: str) -> str:
    return os.path.join(data_dir, "exports", SUBDIR)


def opted_in(con) -> dict:
    """What the dashboard shows before an export: how many account
    holders have the choice on, and how many ordinary dialectics that
    covers. No content, no names."""
    actors = pdb.research_use_actors(con)
    bases = _bases_of(con, actors)
    return {"accounts": len(actors), "dialectics": len(bases)}


def _bases_of(con, actors: list[dict]) -> list[dict]:
    out = []
    for actor in actors:
        for base in pdb.list_bases_for_actor(con, actor["id"]):
            # A study record is never research material by this route,
            # whoever owns it.
            if pdb.study_session_for_base(con, base["id"]) is None:
                out.append(base)
    return out


def export_research(reg, con, *, data_dir: str, exported_by: int) -> dict:
    """Build the archive. Returns `{archive, pseudonym_file, dialectics,
    accounts, skipped}`; a dialectic that can't be read is skipped and
    named, not fatal."""
    from .integrity import compute_base_integrity
    from .study_export import _export_base, _pseudonymize, _versions, _write_json

    actors = pdb.research_use_actors(con)
    pseudonyms = {a["id"]: f"U-{i:03d}" for i, a in enumerate(actors, 1)}
    bases = _bases_of(con, actors)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    top = f"research-{stamp}"
    directory = exports_dir(data_dir)
    os.makedirs(directory, exist_ok=True)
    reason = f"Research export {stamp} of the dialectics whose owners agreed to research use"

    exported, skipped = [], []
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, top)
        os.makedirs(root)
        for base in bases:
            code = pseudonyms[base["owner_id"]]
            dest = os.path.join(root, f"{code}-{sanitize_base_name(base['id'])}")
            try:
                os.makedirs(dest)
                _write_json(
                    os.path.join(dest, "dialectic.json"),
                    {"id": base["id"], "name": base["name"], "owner": code},
                )
                _write_json(
                    os.path.join(dest, "integrity.json"),
                    _pseudonymize(compute_base_integrity(base["id"]), pseudonyms),
                )
                with reg.hold(base["id"], transient=True) as handle:
                    _export_base(handle.state, dest, base["id"], pseudonyms)
            except Exception as e:  # noqa: BLE001 — one unreadable file must not sink the export
                logger.warning("Research export: skipping %r: %s", base["id"], e)
                skipped.append({"dialectic": base["id"], "owner": code, "error": str(e)})
                continue
            # The owner sees this in their access notes.
            with reg.platform_lock:
                grant = content_access.open_grant(
                    con,
                    actor_id=exported_by,
                    base_id=base["id"],
                    owner_id=base["owner_id"],
                    category="owner_agreed_analysis",
                    reason=reason,
                )
                content_access.record(con, grant=grant, action="records")
            exported.append({"dialectic": base["id"], "owner": code})
        _write_json(
            os.path.join(root, "manifest.json"),
            {
                "format": FORMAT,
                "format_version": FORMAT_VERSION,
                "exported_at_utc": f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}T{stamp[9:11]}:{stamp[11:13]}:{stamp[13:15]}Z",
                "notice_version_current": notice.NOTICE_VERSION,
                "versions": _versions(con),
                "accounts": [
                    {
                        "owner": pseudonyms[a["id"]],
                        "notice_version_accepted": a.get("terms_version"),
                        "research_use_set_at": _iso(a.get("research_use_set_at")),
                    }
                    for a in actors
                ],
                "dialectics": exported,
                "skipped": skipped,
                "pseudonymization": (
                    "Owners are replaced by opaque IDs (U-*). The key is held separately "
                    "by the research team and is NOT part of this archive."
                ),
            },
        )
        archive = os.path.join(directory, f"{top}.tar.gz")
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(root, arcname=top)
    pseudonym_file = os.path.join(directory, f"{top}.pseudonyms.json")
    _write_json(
        pseudonym_file,
        {
            str(a["id"]): {
                "pseudonym": pseudonyms[a["id"]],
                "email": a.get("email"),
                "display_name": a.get("display_name"),
            }
            for a in actors
        },
    )
    logger.warning(
        "Research export built by actor %s: %s — %d accounts, %d dialectics, %d skipped",
        exported_by,
        archive,
        len(actors),
        len(exported),
        len(skipped),
    )
    return {
        "archive": archive,
        "name": f"{top}.tar.gz",
        "pseudonym_file": pseudonym_file,
        "accounts": len(actors),
        "dialectics": exported,
        "skipped": skipped,
    }


def list_exports(data_dir: str) -> list[dict]:
    """The research archives already made, newest first — the file list
    the download routes serve from (a name is matched here, never joined
    into a path from the request)."""
    directory = exports_dir(data_dir)
    found = []
    if os.path.isdir(directory):
        for name in os.listdir(directory):
            m = _NAME.match(name)
            path = os.path.join(directory, name)
            if not m or not os.path.isfile(path):
                continue
            stem = name[: -len(".tar.gz")]
            found.append(
                {
                    "name": name,
                    "created": m.group(1),
                    "size_bytes": os.path.getsize(path),
                    "pseudonym_file": (
                        f"{stem}.pseudonyms.json"
                        if os.path.isfile(os.path.join(directory, f"{stem}.pseudonyms.json"))
                        else None
                    ),
                }
            )
    return sorted(found, key=lambda e: e["created"], reverse=True)


def _iso(ts) -> str | None:
    if ts is None:
        return None
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ") if hasattr(ts, "strftime") else str(ts)
