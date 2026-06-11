"""
access.py — adversarial access-control & authentication probes.

The main harness walks the *happy path*: it mints sessions directly and
every request is meant to succeed. This phase does the opposite — it
drives the real auth routes the way an attacker (or a confused user, or
a buggy client) would, and asserts each one is *correctly rejected*.

Each probe declares the status it expects (`SimClient.probe`). A
correctly-denied request reads as `ok`; a request that should have been
denied but slips through records `ok=False` and surfaces in the report's
problems list — which is exactly a security finding. The probes that
matter most for the Sloan study:

  * tenant isolation — one participant cannot read another's dialectic
    (404, not 403, so a name's existence isn't leaked);
  * privilege gating — a participant can't reach `/api/admin/*` or the
    judge routes;
  * session revocation — a logged-out token stops working;
  * single-use tokens — invites and participant links can't be replayed;
  * blinding integrity — the judge view never exposes the condition,
    and a judge can't open another judge's assignment.

Runs after the study + judging + export so it can reuse the real
assignments the run produced, and creates its own throwaway users via
the genuine signup flow (the part the happy path skips).
"""

from __future__ import annotations

import logging

from .. import auth
from ..db import get_registry
from .client import SimClient, StepRecord

logger = logging.getLogger(__name__)

_PW = "Probe-pw-123456"


def _record_check(rec, actor: str, action: str, ok: bool, note: str) -> None:
    """Record a non-HTTP assertion (e.g. a content-leak check) as a probe
    step so it shows up in the timeline and the pass/fail counts."""
    rec.record(
        StepRecord(
            actor=actor,
            action=action,
            method="--",
            path="(content check)",
            status=200,
            latency_ms=0,
            ok=ok,
            note=note,
            is_probe=True,
            expect="no-leak",
        )
    )


def _condition_leak(body) -> str:
    """Walk a judge-view response for any *key* that leaks the ground-
    truth condition of a slot. Only key names are inspected — report
    content is a string value and may legitimately contain words like
    'baseline', so scanning values would false-positive.

    A key matches if it mentions 'condition' but is NOT one of the
    judge's own `condition_guess_*` answers: the view echoes the
    judge's existing rating back to them (so the UI can render edit
    affordances), and their own guesses are theirs to see — what must
    never appear is the system telling them which condition is which."""

    def walk(obj, path=""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                kl = str(k).lower()
                if "condition" in kl and "guess" not in kl:
                    return f"key '{path}{k}'"
                hit = walk(v, f"{path}{k}.")
                if hit:
                    return hit
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                hit = walk(v, f"{path}[{i}].")
                if hit:
                    return hit
        return ""

    return walk(body)


def run_access_probes(harness) -> None:
    """Run the full adversarial suite against `harness.app`, recording
    onto `harness.rec`. Never raises — a crashed probe is logged and the
    rest continue, same contract as the main harness."""
    app = harness.app
    rec = harness.rec
    admin = harness.researcher
    study_id = harness.study_id
    con = get_registry().platform_con()

    def fresh(name: str) -> SimClient:
        return SimClient(name, app, rec)

    # ── A. Real signup → login flow (positive control + failures) ──
    _, body = admin.probe(
        "POST",
        "/api/admin/invites",
        json={"role": "user", "intended_email": "alice@probe.local"},
        action="issue_invite",
        expect=200,
        note="for alice",
    )
    alice_token = (body or {}).get("token")

    alice = fresh("alice")
    if alice_token:
        alice.probe(
            "POST",
            "/api/auth/signup",
            json={"token": alice_token, "display_name": "Alice", "password": _PW},
            action="signup",
            expect=200,
            note="consume invite (real flow)",
        )

    fresh("anon").probe(
        "POST",
        "/api/auth/login",
        json={"email": "alice@probe.local", "password": "wrong-password"},
        action="login_bad_password",
        expect=401,
    )
    fresh("anon").probe(
        "POST",
        "/api/auth/login",
        json={"email": "nobody@probe.local", "password": _PW},
        action="login_unknown_email",
        expect=401,
    )

    # ── B. Unauthenticated access to protected routes → 401 ──
    anon = fresh("anon")
    anon.probe("GET", "/api/auth/me", action="me_unauth", expect=401)
    anon.probe("GET", "/api/dialectics", action="list_unauth", expect=401)
    anon.probe(
        "POST",
        "/api/admin/invites",
        json={"role": "user"},
        action="admin_unauth",
        expect=401,
        note="no cookie → 401, not 403",
    )

    # ── C. Privilege gating: a logged-in non-admin → 403 ──
    alice.probe("GET", "/api/admin/users", action="admin_gate", expect=403)
    alice.probe("GET", "/api/judge/queue", action="judge_gate", expect=403)

    # ── D. Tenant isolation ──
    alice.probe(
        "POST",
        "/api/dialectics",
        json={"name": "alice-secret", "topic": "Alice private"},
        action="alice_create_base",
        expect=200,
    )
    _, body = admin.probe(
        "POST",
        "/api/admin/invites",
        json={"role": "user", "intended_email": "bob@probe.local"},
        action="issue_invite",
        expect=200,
        note="for bob",
    )
    bob_token = (body or {}).get("token")
    bob = fresh("bob")
    if bob_token:
        bob.probe(
            "POST",
            "/api/auth/signup",
            json={"token": bob_token, "display_name": "Bob", "password": _PW},
            action="signup",
            expect=200,
        )
    bob.probe(
        "GET",
        "/api/dialectics/alice-secret",
        action="cross_tenant_read",
        expect=404,
        note="leak-prevention: 404 not 403",
    )
    bob.probe(
        "POST",
        "/api/dialectics/alice-secret/message",
        json={"message": "let me in"},
        action="cross_tenant_write",
        expect=404,
    )

    # ── E. Session revocation: a revoked token must stop working ──
    alice_tok = alice.session_token()
    alice.probe("POST", "/api/auth/logout", action="logout", expect=200)
    if alice_tok:
        replay = fresh("alice-replay")
        replay.set_session_cookie(alice_tok)
        replay.probe(
            "GET",
            "/api/auth/me",
            action="revoked_token_reuse",
            expect=401,
            note="reuse cookie after logout",
        )

    # ── F. Invite misuse ──
    fresh("anon").probe(
        "POST",
        "/api/auth/signup",
        json={"token": "garbage-token", "display_name": "X", "password": _PW},
        action="signup_bad_token",
        expect=400,
    )
    if alice_token:
        fresh("anon").probe(
            "POST",
            "/api/auth/signup",
            json={"token": alice_token, "display_name": "X", "password": _PW},
            action="signup_reuse_token",
            expect=400,
            note="invite already consumed",
        )

    # ── G. Magic link ──
    fresh("anon").probe(
        "POST",
        "/api/auth/magic-link",
        json={"email": "alice@probe.local"},
        action="magic_link_request",
        expect=200,
        note="200 regardless (no registration leak)",
    )
    fresh("anon").probe(
        "GET",
        "/api/auth/magic/garbage-token",
        action="magic_link_bad",
        expect=400,
    )

    # ── H. Study-token misuse ──
    fresh("anon").probe(
        "POST",
        "/api/study/garbage-token",
        action="study_token_invalid",
        expect=404,
    )
    _, body = admin.probe(
        "POST",
        "/api/admin/study/tokens",
        json={"study_id": study_id, "condition": "elenchus", "display_name": "P-PROBE"},
        action="issue_study_token",
        expect=200,
    )
    stoken = (body or {}).get("token")
    if stoken:
        fresh("p-probe").probe(
            "POST", f"/api/study/{stoken}", action="study_token_consume", expect=200
        )
        fresh("p-probe2").probe(
            "POST",
            f"/api/study/{stoken}",
            action="study_token_reuse",
            expect=410,
            note="single-use → 410 Gone",
        )

    # ── I. Judge blinding + cross-judge isolation ──
    try:
        _judge_probes(harness, con, fresh)
    except Exception:
        logger.exception("Judge access probes crashed")


def _judge_probes(harness, con, fresh) -> None:
    rows = con.execute("SELECT id, judge_actor_id FROM judge_assignments ORDER BY id").fetchall()
    if not rows:
        logger.info("No judge assignments to probe; skipping judge access checks")
        return
    aid, owner_judge = rows[0]

    # The owning judge can view it (200) — and the response must not leak
    # which condition produced either slot.
    jc = fresh(f"judge-{owner_judge}")
    jc.set_session_cookie(auth.create_session(owner_judge))
    st, body = jc.probe(
        "GET", f"/api/judge/assignments/{aid}", action="judge_view_own", expect=200
    )
    if st == 200 and body is not None:
        leak = _condition_leak(body)
        _record_check(
            harness.rec,
            jc.name,
            "blinding_no_leak",
            ok=not leak,
            note=("LEAK: " + leak) if leak else "judge view exposes no condition",
        )

    # A different judge must be refused this assignment (403).
    judge_actors = [
        a for (a,) in con.execute("SELECT id FROM actors WHERE kind = 'judge'").fetchall()
    ]
    other = next((j for j in judge_actors if j != owner_judge), None)
    if other is not None:
        oc = fresh(f"judge-{other}")
        oc.set_session_cookie(auth.create_session(other))
        oc.probe(
            "GET",
            f"/api/judge/assignments/{aid}",
            action="judge_view_foreign",
            expect=403,
            note="not your assignment",
        )
    else:
        logger.info("Only one judge actor; skipping cross-judge isolation probe")
