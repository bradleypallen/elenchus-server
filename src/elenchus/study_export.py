"""
study_export.py — per-study, analysis-ready data export.

Walks everything one study produced and packages it as a tar.gz the
research team can archive (Zenodo, CC BY 4.0 per the proposal) and
analyze downstream. Layout inside the archive:

    study-{study_id}-{timestamp}/
      manifest.json                  — export metadata + content listing
      text_judging.json              — the panel's ratings of the submitted
                                       texts, UNBLINDED for analysis: each
                                       text's condition / participant /
                                       period beside every judge's ratings
                                       (full revision history, newest last)
      deviations.json                — protocol deviations, every session
      allocation.json                — the seed's hash, the list's hash, each
                                       participant's sequence (the seed itself
                                       is in the side file with the names)
      participants.json              — enrolled participants: code, cell
                                       (first condition × first topic),
                                       allocation method — no names
      judging.json                   — packages, assignments, ratings
      sessions/{pseudonym}-{cond}/   — one directory per session
        deviations.json              — this session's protocol deviations
        session.json                 — lifecycle row (pseudonymized), with the
                                       session's topic
        text.json                    — the submitted text: the judged artifact
        text_snapshots.json          — every autosaved draft that led to it
        editor_events.json           — pastes (length + time only), soft
                                       timer warnings shown
        state.json                   — dialectic state (position, T, I, atoms)
        transcript.json              — conversation turns
        turn_log.json                — per-exchange capture: raw LLM output,
                                       parse path, state before/after, timing
        state_events.json            — every state transition, with its
                                       source (opponent / ui / direct) and turn
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

from . import study_enrolment, study_text, text_judging, turn_log
from .db import get_registry
from .db import platform as pdb
from .integrity import compute_base_integrity

logger = logging.getLogger(__name__)

# "2": sessions carry the participant code, period and allocation
# (crossover linkage), the submitted text and its draft history, and the
# capture log; `participants.json` added; session tokens no longer exported.
EXPORT_FORMAT_VERSION = "3"


def _versions(con) -> dict:
    """What produced this archive — the "frozen as" table of the study's
    registration, checkable from the archive alone."""
    import importlib.metadata

    import duckdb

    from . import __version__ as elenchus_version
    from . import prompts
    from .migrations.runner import current_schema_version
    from .text_judging import RUBRIC_VERSION

    try:
        pynmms = importlib.metadata.version("pynmms")
    except importlib.metadata.PackageNotFoundError:
        pynmms = "unknown"
    return {
        "elenchus": elenchus_version,
        "pynmms": pynmms,
        "duckdb": duckdb.__version__,
        "platform_schema": current_schema_version(con),
        "rubric": RUBRIC_VERSION,
        "export_format": EXPORT_FORMAT_VERSION,
        # The prompts in force when the export was taken: label + hash per
        # family. Each turn carries its own; this is the "frozen as" line.
        "prompts": prompts.versions(),
    }


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
    for participant in pdb.list_study_participants(con, study_id):
        staff_ids.add(participant["enrolled_by"])
    config = pdb.find_study_config(con, study_id)
    if config is not None:
        staff_ids.add(config["created_by"])
    for assignment in pdb.list_text_assignments_for_study(con, study_id):
        judge_ids.add(assignment["judge_actor_id"])
        staff_ids.add(assignment["assigned_by"])

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
        "logged_by",  # session_deviations: the researcher, or None for the platform
        "set_by",  # allocation.json: who set the seed
        "revealed_by",  # who scheduled session 1
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
            # A development study is the team's own tuning material, not a
            # registered run (migration 0024); an analysis must not
            # mistake its export for one.
            "development": bool((pdb.find_study_config(con, study_id) or {}).get("development")),
            "export_format_version": EXPORT_FORMAT_VERSION,
            "versions": _versions(con),
            "exported_at": ts,
            # Columns named `*_at` in the platform tables (opened_at,
            # submitted_at, …) are naive timestamps in THIS zone — DuckDB
            # fills them in the server's local time. The capture log's
            # `at_utc` fields are UTC regardless.
            "server_timezone": con.execute("SELECT current_setting('TimeZone')").fetchone()[0],
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

        # The panel's ratings of the submitted texts. Unblinded — this
        # is the analysis set: condition, participant and period sit
        # beside each judge's ratings. Every submission is included
        # (a judge may revise); the last in `ratings` is the one that
        # counts. `rubric` records the wording the panel rated against.
        text_assignments = pdb.list_text_assignments_for_study(con, study_id)
        text_judging_rows = []
        for text in pdb.list_study_texts(con, study_id=study_id):
            session = pdb.find_study_session(con, text["session_id"]) or {}
            token = pdb.find_participant_token(con, session.get("study_token") or "") or {}
            person = (
                pdb.find_study_participant(con, token["participant_id"])
                if token.get("participant_id") is not None
                else None
            )
            text_judging_rows.append(
                {
                    "text_id": text["id"],
                    "session_id": text["session_id"],
                    "actor_id": text["actor_id"],
                    "participant_code": person["participant_code"] if person else None,
                    "period": token.get("period"),
                    "condition": text["condition"],
                    "topic_title": text["topic_title"],
                    "word_count": text["word_count"],
                    "active_elapsed_seconds": text["active_elapsed_seconds"],
                    "assignments": [
                        {
                            "assignment_id": a["id"],
                            "pair_id": a["pair_id"],
                            "judge_actor_id": a["judge_actor_id"],
                            "assigned_by": a["assigned_by"],
                            "assigned_at": a["assigned_at"],
                            "queue_position": a["position"],
                            "status": a["status"],
                            "completed_at": a["completed_at"],
                            "ratings": pdb.list_text_ratings(con, a["id"]),
                        }
                        for a in text_assignments
                        if a["text_id"] == text["id"]
                    ],
                }
            )
        # The pairs (migration 0020): which text each judge saw as "A",
        # their rankings (every submission; the last counts), and their
        # condition guesses per text.
        pairs_out = []
        for pair in pdb.list_text_pairs_for_study(con, study_id):
            person = pdb.find_study_participant(con, pair["participant_id"])
            pairs_out.append(
                {
                    "pair_id": pair["id"],
                    "participant_code": person["participant_code"] if person else None,
                    "judge_actor_id": pair["judge_actor_id"],
                    "labels": {"A": pair["label_a_text_id"], "B": pair["label_b_text_id"]},
                    "assigned_by": pair["assigned_by"],
                    "assigned_at": pair["assigned_at"],
                    "queue_position": pair["position"],
                    "status": pair["status"],
                    "completed_at": pair["completed_at"],
                    "rankings": pdb.list_pair_rankings(con, pair["id"]),
                }
            )
        guesses_out = [
            g
            for text in text_judging_rows
            for g in pdb.list_condition_guesses(con, text_id=text["text_id"])
        ]
        _write_json(
            os.path.join(staging, "text_judging.json"),
            _pseudonymize(
                {
                    "rubric": text_judging.rubric(),
                    "texts": text_judging_rows,
                    "pairs": pairs_out,
                    "condition_guesses": guesses_out,
                },
                pseudonyms,
            ),
        )
        logger.info(
            "Exported text judging for %s: %d texts, %d assignments, %d completed",
            study_id,
            len(text_judging_rows),
            len(text_assignments),
            sum(1 for a in text_assignments if a["status"] == "completed"),
        )

        # The roster, minus anything identifying: the code is the
        # researcher-assigned sequence number, the name stays out.
        participants = pdb.list_study_participants(con, study_id)
        _write_json(
            os.path.join(staging, "participants.json"),
            [
                {
                    "participant_code": p["participant_code"],
                    "first_condition": p["first_condition"],
                    "first_topic": p["first_topic"],
                    "allocation": p["allocation"],
                    "sequence": study_enrolment.sequence_letter(
                        study_enrolment.Cell(p["first_condition"], p["first_topic"])
                    ),
                    "block_index": p["block_index"],
                    "revealed_at": p["revealed_at"],
                    # participants.json is written without `_pseudonymize`
                    # (its keys are mapped by hand, like `enrolled_by`).
                    "revealed_by": (
                        pseudonyms.get(p["revealed_by"], f"UNMAPPED-{p['revealed_by']}")
                        if p["revealed_by"] is not None
                        else None
                    ),
                    # Screening covariates (Registered Report §2.2).
                    "ontology_experience": p["ontology_experience"],
                    "prior_llm_use": p["prior_llm_use"],
                    "nominated_topic": p["nominated_topic"],
                    "enrolled_at": p["enrolled_at"],
                    "enrolled_by": pseudonyms.get(
                        p["enrolled_by"], f"UNMAPPED-{p['enrolled_by']}"
                    ),
                }
                for p in participants
            ],
        )
        config = pdb.find_study_config(con, study_id)
        seed = (config or {}).get("allocation_seed") or ""
        public_config = {k: v for k, v in (config or {}).items() if k != "allocation_seed"}
        _write_json(
            os.path.join(staging, "study_config.json"), _pseudonymize(public_config, pseudonyms)
        )
        # The allocation record (Registered Report §2.1): the hashes that
        # were deposited, and each participant's sequence. The seed itself
        # goes in the side file with the names key, not in the archive.
        planned_n = int((config or {}).get("planned_n") or 48)
        _write_json(
            os.path.join(staging, "allocation.json"),
            _pseudonymize(
                {
                    "seeded": bool(seed),
                    "planned_n": planned_n,
                    "seed_sha256": study_enrolment.seed_hash(seed) if seed else None,
                    "list_sha256": (
                        study_enrolment.allocation_hash(
                            study_enrolment.allocation_list(seed, planned_n)
                        )
                        if seed
                        else None
                    ),
                    "set_by": (config or {}).get("allocation_seed_set_by"),
                    "set_at": (config or {}).get("allocation_seed_set_at"),
                    "sequences": [
                        {
                            "participant_code": p["participant_code"],
                            "sequence": study_enrolment.sequence_letter(
                                study_enrolment.Cell(p["first_condition"], p["first_topic"])
                            ),
                            "allocation": p["allocation"],
                            "block_index": p["block_index"],
                            "revealed_at": p["revealed_at"],
                        }
                        for p in participants
                    ],
                },
                pseudonyms,
            ),
        )
        # Protocol deviations (Registered Report §2.4, §2.7): logged at the
        # time, by a researcher (pseudonymized) or the platform (null).
        _write_json(
            os.path.join(staging, "deviations.json"),
            _pseudonymize(pdb.list_session_deviations(con, study_id=study_id), pseudonyms),
        )

        # Seal the archive.
        archive_path = os.path.join(output_dir, f"{archive_name}.tar.gz")
        with tarfile.open(archive_path, "w:gz") as tar:
            tar.add(staging, arcname=archive_name)

        # The identity map lives NEXT TO the archive, never inside it.
        pseudonym_file = os.path.join(output_dir, f"{archive_name}.pseudonyms.json")
        _write_json(
            pseudonym_file,
            {
                **{str(actor_id): pseudonym for actor_id, pseudonym in pseudonyms.items()},
                # Participant code → the researcher's label for the
                # person. Same rule as the rest of this file: it stays
                # with the research team, never in a deposit.
                "participants": {p["participant_code"]: p["display_name"] for p in participants},
                # The allocation seed: deposited with the registration by
                # the person who set it; kept here, beside the names,
                # never in the archive.
                "allocation_seed": seed or None,
            },
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

    sid = session["id"]
    token = pdb.find_participant_token(con, session.get("study_token") or "") or {}
    participant = (
        pdb.find_study_participant(con, token["participant_id"])
        if token.get("participant_id") is not None
        else None
    )
    session_out = {
        **{k: v for k, v in session.items() if k != "study_token"},  # a credential
        "topic_title": token.get("topic_title", ""),
        "topic_brief": token.get("topic_brief", ""),
        # Crossover linkage: `participant_code` is the key that ties a
        # person's two sessions together (the per-session actor ids
        # differ by design). None for hand-issued, unenrolled tokens.
        "participant_code": participant["participant_code"] if participant else None,
        "period": token.get("period"),
        "first_condition": participant["first_condition"] if participant else None,
        "first_topic": participant["first_topic"] if participant else None,
        "allocation": participant["allocation"] if participant else None,
    }
    _write_json(os.path.join(dest, "session.json"), _pseudonymize(session_out, pseudonyms))
    _write_json(
        os.path.join(dest, "deviations.json"),
        _pseudonymize(pdb.list_session_deviations(con, session_id=sid), pseudonyms),
    )
    _write_json(
        os.path.join(dest, "text.json"),
        _pseudonymize(pdb.find_study_text_for_session(con, sid), pseudonyms),
    )
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
        _write_json(os.path.join(dest, "text_snapshots.json"), [])
        _write_json(os.path.join(dest, "editor_events.json"), [])
        _write_json(os.path.join(dest, "integrity.json"), None)
        return

    _write_json(
        os.path.join(dest, "integrity.json"),
        _pseudonymize(compute_base_integrity(base_id), pseudonyms),
    )

    # Raises FileNotFoundError / ValueError on broken bases. A transient
    # hold: a base opened only for the export is closed again once its
    # files are written, so a cohort's export keeps one base open at a
    # time rather than all of them.
    with reg.hold(base_id, transient=True) as handle:
        _export_base(handle.state, dest, sid, pseudonyms)


def _export_base(state, dest: str, sid: int, pseudonyms: dict) -> None:
    """Everything in a session's export that reads the per-base DB."""
    _write_json(os.path.join(dest, "state.json"), state.to_dict())
    _write_json(os.path.join(dest, "transcript.json"), state.get_conversation())
    # The capture log — the input to offline formal analysis. Also
    # present in the DuckDB dump below; written as JSON too so analysis
    # scripts don't need DuckDB, and so `actor_id` is pseudonymized.
    turns = turn_log.list_turns(state.base.con)
    events = turn_log.list_state_events(state.base.con)
    _write_json(os.path.join(dest, "turn_log.json"), _pseudonymize(turns, pseudonyms))
    _write_json(os.path.join(dest, "state_events.json"), _pseudonymize(events, pseudonyms))
    snapshots = study_text.list_snapshots(state.base.con)
    editor_events = study_text.list_editor_events(state.base.con)
    _write_json(os.path.join(dest, "text_snapshots.json"), _pseudonymize(snapshots, pseudonyms))
    _write_json(os.path.join(dest, "editor_events.json"), _pseudonymize(editor_events, pseudonyms))
    logger.info(
        "Exported capture log for session %s: %d turns, %d state events, "
        "%d text snapshots, %d editor events",
        sid,
        len(turns),
        len(events),
        len(snapshots),
        len(editor_events),
    )

    # Consistent MVCC snapshot of the per-base DB (same mechanism as
    # backup.py). Numeric ids inside are unlinkable without
    # platform.actors, which is not exported.
    base_dump = os.path.join(dest, "base")
    os.makedirs(base_dump, exist_ok=True)
    state.base.con.execute(f"EXPORT DATABASE '{_safe_sql_literal(base_dump)}'")
