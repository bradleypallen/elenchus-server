"""
study_export.py — per-study, analysis-ready data export.

Walks everything one study produced and packages it as a tar.gz the
research team can archive (Zenodo, CC BY 4.0 per the proposal) and
analyze downstream. Layout inside the archive:

    study-{study_id}-{timestamp}/
      manifest.json                  — export metadata + content listing
      judging.json                   — packages, assignments, ratings
      sessions/{pseudonym}-{cond}/   — one directory per session
        session.json                 — lifecycle row (pseudonymized)
        state.json                   — dialectic state (position, T, I, atoms)
        transcript.json              — conversation turns
        reports.json                 — generated structured report(s)
        surveys.json                 — questionnaire submissions
        integrity.json               — usage stats + content metrics
        base/                        — DuckDB EXPORT DATABASE dump
                                       (schema.sql + load.sql + data)

Pseudonymization. The archive uses opaque IDs (P-001, P-002 for
participants in actor-id order; J-001... for judges). The mapping
from real actor_id → pseudonym is written to a SEPARATE file next to
the archive (`{archive}.pseudonyms.json`), never inside it — that
file stays with ADSA's participant tracking and is excluded from any
public deposit. No emails or display names appear anywhere in the
archive; numeric actor ids inside the per-base DuckDB dumps are
unlinkable without platform.actors, which is not exported.

The per-base dump uses DuckDB `EXPORT DATABASE` (same mechanism as
backup.py) rather than copying the live .duckdb file — the export is
a consistent MVCC snapshot even if a session were somehow still
writing.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import shutil
import tarfile

from .db import get_registry
from .db import platform as pdb
from .integrity import compute_base_integrity

logger = logging.getLogger(__name__)

EXPORT_FORMAT_VERSION = "1"


def _safe_sql_literal(path: str) -> str:
    return path.replace("'", "''")


def _json_default(obj):
    """Serialize datetimes and anything else json doesn't know."""
    return str(obj)


def _write_json(path: str, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=_json_default, ensure_ascii=False)


def _build_pseudonyms(con, study_id: str) -> dict[int, str]:
    """Deterministic opaque IDs: participants P-001... in actor-id
    order, judges J-001... in actor-id order. Researchers/admins who
    appear as issued_by/created_by are mapped to R-001... so no real
    id leaks through metadata either."""
    pseudonyms: dict[int, str] = {}

    participant_ids = sorted(
        {t["actor_id"] for t in pdb.list_participant_tokens(con, study_id=study_id)}
    )
    for i, actor_id in enumerate(participant_ids, 1):
        pseudonyms[actor_id] = f"P-{i:03d}"

    judge_ids: set[int] = set()
    staff_ids: set[int] = set()
    for package in pdb.list_judge_packages(con, study_id=study_id):
        staff_ids.add(package["created_by"])
        for assignment in pdb.list_assignments_for_package(con, package["id"]):
            judge_ids.add(assignment["judge_actor_id"])
            staff_ids.add(assignment["assigned_by"])
    for token in pdb.list_participant_tokens(con, study_id=study_id):
        staff_ids.add(token["issued_by"])

    for i, actor_id in enumerate(sorted(judge_ids - set(pseudonyms)), 1):
        pseudonyms[actor_id] = f"J-{i:03d}"
    for i, actor_id in enumerate(sorted(staff_ids - set(pseudonyms)), 1):
        pseudonyms[actor_id] = f"R-{i:03d}"
    return pseudonyms


def _pseudonymize(value, pseudonyms: dict[int, str]):
    """Recursively replace any dict key ending in `actor_id` /
    `_by` (issuer/creator fields) with the pseudonym. Conservative:
    only known identity-bearing keys are touched, so report content
    and survey responses pass through untouched."""
    identity_keys = {
        "actor_id",
        "judge_actor_id",
        "issued_by",
        "assigned_by",
        "created_by",
        "owner_id",
        "participant_actor_id",
    }
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k in identity_keys and isinstance(v, int):
                out[k] = pseudonyms.get(v, f"UNMAPPED-{v}")
            else:
                out[k] = _pseudonymize(v, pseudonyms)
        return out
    if isinstance(value, list):
        return [_pseudonymize(v, pseudonyms) for v in value]
    return value


def export_study(
    study_id: str,
    *,
    output_dir: str | None = None,
) -> dict:
    """Export everything `study_id` produced. Returns metadata:
    {archive, pseudonym_file, sessions_exported, sessions_failed}.

    Individual session failures (corrupt base file, missing data) are
    recorded in the manifest and the return value rather than
    aborting the run — a single broken session must not block the
    rest of the cohort's export.
    """
    reg = get_registry()
    con = reg.platform_con()

    if output_dir is None:
        # Default next to the platform DB: {data_dir}/exports/.
        output_dir = os.path.join(os.path.dirname(reg.platform_path), "exports")
    os.makedirs(output_dir, exist_ok=True)

    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    archive_name = f"study-{study_id}-{ts}"
    staging = os.path.join(output_dir, f".staging-{archive_name}")
    os.makedirs(staging)

    pseudonyms = _build_pseudonyms(con, study_id)
    sessions = pdb.list_sessions_for_study(con, study_id)

    exported: list[dict] = []
    failed: list[dict] = []
    try:
        sessions_dir = os.path.join(staging, "sessions")
        os.makedirs(sessions_dir, exist_ok=True)

        for session in sessions:
            pseudonym = pseudonyms.get(session["actor_id"], f"UNMAPPED-{session['actor_id']}")
            label = f"{pseudonym}-{session['condition'] or 'unknown'}"
            try:
                _export_one_session(
                    reg, con, session, pseudonyms, os.path.join(sessions_dir, label)
                )
                exported.append({"session_id": session["id"], "label": label})
            except Exception as e:
                logger.exception("Export failed for session %d", session["id"])
                failed.append({"session_id": session["id"], "label": label, "error": str(e)})

        # Study-level judging data (unblinded — this is the analysis set).
        judging = []
        for package in pdb.list_judge_packages(con, study_id=study_id):
            assignments = []
            for assignment in pdb.list_assignments_for_package(con, package["id"]):
                rating = pdb.find_rating_for_assignment(con, assignment["id"])
                assignments.append({"assignment": assignment, "rating": rating})
            judging.append({"package": package, "assignments": assignments})
        _write_json(
            os.path.join(staging, "judging.json"),
            _pseudonymize(judging, pseudonyms),
        )

        manifest = {
            "study_id": study_id,
            "export_format_version": EXPORT_FORMAT_VERSION,
            "exported_at": ts,
            "sessions_exported": exported,
            "sessions_failed": failed,
            "judge_packages": len(judging),
            "pseudonymization": (
                "Actor identities are replaced by opaque IDs (P-*, J-*, R-*). "
                "The identity mapping is held separately by the research team "
                "and is NOT part of this archive."
            ),
        }
        _write_json(os.path.join(staging, "manifest.json"), manifest)

        # Seal the archive.
        archive_path = os.path.join(output_dir, f"{archive_name}.tar.gz")
        with tarfile.open(archive_path, "w:gz") as tar:
            tar.add(staging, arcname=archive_name)

        # The identity map lives NEXT TO the archive, never inside it.
        pseudonym_file = os.path.join(output_dir, f"{archive_name}.pseudonyms.json")
        _write_json(
            pseudonym_file,
            {str(actor_id): pseudonym for actor_id, pseudonym in pseudonyms.items()},
        )

        return {
            "archive": archive_path,
            "pseudonym_file": pseudonym_file,
            "sessions_exported": exported,
            "sessions_failed": failed,
        }
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _export_one_session(reg, con, session: dict, pseudonyms: dict[int, str], dest: str) -> None:
    """Write one session's directory. Raises on unrecoverable errors;
    the caller records the failure and moves on."""
    os.makedirs(dest, exist_ok=True)

    _write_json(os.path.join(dest, "session.json"), _pseudonymize(session, pseudonyms))

    sid = session["id"]
    _write_json(
        os.path.join(dest, "reports.json"),
        _pseudonymize(
            [r for r in pdb.list_study_reports(con) if r["session_id"] == sid],
            pseudonyms,
        ),
    )
    _write_json(
        os.path.join(dest, "surveys.json"),
        _pseudonymize(pdb.list_survey_responses_for_session(con, sid), pseudonyms),
    )

    base_id = session.get("base_id")
    if not base_id:
        # Briefing/tutorial-only session — no dialectic artifacts.
        _write_json(os.path.join(dest, "state.json"), None)
        _write_json(os.path.join(dest, "transcript.json"), [])
        _write_json(os.path.join(dest, "integrity.json"), None)
        return

    _write_json(
        os.path.join(dest, "integrity.json"),
        _pseudonymize(compute_base_integrity(base_id), pseudonyms),
    )

    state = reg.get(base_id)  # raises FileNotFoundError / ValueError on broken bases
    _write_json(os.path.join(dest, "state.json"), state.to_dict())
    _write_json(os.path.join(dest, "transcript.json"), state.get_conversation())

    # Consistent MVCC snapshot of the per-base DB (same mechanism as
    # backup.py). Numeric ids inside are unlinkable without
    # platform.actors, which is not exported.
    base_dump = os.path.join(dest, "base")
    os.makedirs(base_dump, exist_ok=True)
    state.base.con.execute(f"EXPORT DATABASE '{_safe_sql_literal(base_dump)}'")
