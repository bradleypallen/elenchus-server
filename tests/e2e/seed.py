"""
seed.py — populate an isolated data dir for the UI E2E run, then exit.

Runs as its own process *before* the server starts, so it can open
platform.duckdb as the sole writer, seed everything the browser tests
need, and release the file lock on exit (DuckDB is single-writer — the
server couldn't open a file this process still held).

Seeds, and prints as JSON on stdout:
  * an admin with a known password (login + admin-UI tests);
  * an unconsumed invite token (signup-from-?token= test);
  * a participant study token (the ?study= routing test);
  * a judge with a password + a fully-formed blinded assignment
    (two reports, a package, an assignment) for the judge-UI /
    blinding test. The report bodies deliberately avoid the words
    'elenchus' / 'baseline' so those remain clean blinding sentinels.

Usage: python tests/e2e/seed.py <data_dir>
"""

from __future__ import annotations

import json
import logging
import secrets
import sys

logging.basicConfig(level=logging.WARNING)

# Report bodies use neutral domains ('widget' / 'gadget') and never the
# condition words, so the tests can assert those words don't surface as
# slot labels in the rendered judge view.
_REPORT_A = (
    "# Domain\nA conceptual specification of widget kinds.\n\n"
    "# Atomic statements\n1. A widget is a discrete artifact.\n"
    "2. Every widget has a unique serial.\n\n"
    "# Implications\n1. If a thing is a widget, then it is discrete.\n\n"
    "# Notes\nEdge cases around composite widgets remain open.\n"
)
_REPORT_B = (
    "# Domain\nA conceptual specification of gadget kinds.\n\n"
    "# Atomic statements\n1. A gadget is a composite artifact.\n"
    "2. A gadget is assembled from parts.\n\n"
    "# Implications\nNone.\n\n"
    "# Notes\nSeveral open questions about part boundaries remain.\n"
)


def main(data_dir: str) -> None:
    from elenchus import auth, invites
    from elenchus.db import get_registry, init_registry
    from elenchus.db import platform as pdb

    init_registry(data_dir)
    reg = get_registry()
    reg.migrate_platform()
    con = reg.platform_con()
    out: dict = {}

    admin_pw = "Admin-pw-123456"
    judge_pw = "Judge-pw-123456"

    with reg.platform_lock:
        admin_id = pdb.create_actor(
            con,
            kind="admin",
            email="admin@e2e.local",
            display_name="E2E Admin",
            password_hash=auth.hash_password(admin_pw),
        )
    out["admin_email"] = "admin@e2e.local"
    out["admin_password"] = admin_pw

    # Invite for the signup test (real, unconsumed).
    out["invite_token"] = invites.issue_invite(
        role="user",
        issued_by=admin_id,
        intended_email="newuser@e2e.local",
        send_email=False,
    )

    with reg.platform_lock:
        # Participant + study token for the ?study= routing test.
        p_actor = pdb.create_actor(
            con, kind="participant", email=None, display_name="P-UI", password_hash=None
        )
        study_token = secrets.token_urlsafe(24)
        pdb.create_participant_token(
            con,
            token=study_token,
            actor_id=p_actor,
            study_id="E2E",
            condition="elenchus",
            issued_by=admin_id,
        )
        out["study_token"] = study_token

        # Judge with a password (production judges use magic links, but a
        # password lets the browser test drive the real login form).
        judge_id = pdb.create_actor(
            con,
            kind="judge",
            email="judge@e2e.local",
            display_name="J-UI",
            password_hash=auth.hash_password(judge_pw),
        )
        out["judge_email"] = "judge@e2e.local"
        out["judge_password"] = judge_pw

        # Two reports (one per condition) to hang a blinded package on.
        report_ids: dict[str, int] = {}
        for cond, body in (("elenchus", _REPORT_A), ("baseline", _REPORT_B)):
            pa = pdb.create_actor(
                con,
                kind="participant",
                email=None,
                display_name=f"P-{cond}",
                password_hash=None,
            )
            tok = secrets.token_urlsafe(24)
            pdb.create_participant_token(
                con,
                token=tok,
                actor_id=pa,
                study_id="E2E",
                condition=cond,
                issued_by=admin_id,
            )
            sid = pdb.create_study_session(
                con, actor_id=pa, study_token=tok, condition=cond, initial_state="complete"
            )
            report_ids[cond] = pdb.record_study_report(
                con,
                session_id=sid,
                condition=cond,
                content=body,
                generator_model="e2e-seed",
                prompt_tokens=0,
                completion_tokens=0,
                cost_usd=0.0,
            )

        # Slot A = elenchus, slot B = baseline — the UI must NOT reveal this.
        pkg_id = pdb.create_judge_package(
            con,
            study_id="E2E",
            slot_a_report_id=report_ids["elenchus"],
            slot_b_report_id=report_ids["baseline"],
            slot_a_condition="elenchus",
            slot_b_condition="baseline",
            created_by=admin_id,
        )
        out["assignment_id"] = pdb.create_judge_assignment(
            con, judge_actor_id=judge_id, package_id=pkg_id, assigned_by=admin_id
        )

    # A ready-made judge session so the judge-VIEW test can inject the
    # cookie and land straight in the interface — login forms are
    # already covered by the admin/signup tests, and going through the
    # form there would couple the blinding check to login-form timing.
    out["judge_session"] = auth.create_session(judge_id)

    reg.close_all()
    print(json.dumps(out))


if __name__ == "__main__":
    main(sys.argv[1])
