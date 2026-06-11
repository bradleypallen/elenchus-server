#!/usr/bin/env python3
"""
record_demo.py — record narrated MP4 walkthroughs of the Elenchus UI,
one video per persona, so someone can *watch* the system being used.

It runs the real server in-process (so the LLM opponent can be stubbed
for a free, deterministic demo — `--driver scripted`, the default — or
left live with `--driver llm`), drives the actual served React frontend
in a headless Chromium with a deliberate, watchable pace and an on-screen
caption strip, records each persona's session to its own video, and
converts the WebM Playwright produces to MP4 with the ffmpeg binary
bundled by `imageio-ffmpeg` (no system install needed).

Personas (each its own video / cookie jar / perspective):
  * participant-elenchus — study link → briefing → tutorial → dialogue
    (commit, a tension is raised, accept it → it becomes an implication)
    → finish → questionnaires → done
  * participant-baseline — the free-form-chat condition, same arc
  * judge            — log in → queue → open a blinded package → rate → submit
  * researcher       — log in → admin dashboard → Study / Judging tabs → export

Requires the `[e2e]` extra (Playwright) + `imageio-ffmpeg`, and a one-time
`python -m playwright install chromium`. Example:

    pip install -e ".[e2e]" imageio-ffmpeg
    python -m playwright install chromium
    python scripts/record_demo.py --out demo-videos
"""

from __future__ import annotations

import argparse
import contextlib
import glob
import os
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time

# ── On-screen caption strip ──────────────────────────────────────────

_CAPTION_JS = """
(t) => {
  let el = document.getElementById('__demo_cap');
  if (!el) {
    el = document.createElement('div');
    el.id = '__demo_cap';
    el.style.cssText =
      'position:fixed;top:0;left:0;right:0;z-index:99999;pointer-events:none;' +
      'background:rgba(18,18,28,.92);color:#fff;' +
      'font:600 15px/1.5 system-ui,-apple-system,sans-serif;' +
      'padding:10px 18px;text-align:center;letter-spacing:.3px;' +
      'border-bottom:2px solid #818cf8;';
    document.body.appendChild(el);
  }
  el.textContent = t;
}
"""


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ── In-process server ────────────────────────────────────────────────


def _start_server(data_dir: str, driver_mode: str):
    """Import the server, point the registry at an isolated dir, stub the
    LLM in scripted mode, and run uvicorn in a daemon thread. Returns
    (base_url, srv_module, seeded_dict)."""
    import uvicorn

    import elenchus.server as srv
    from elenchus.db import get_registry, init_registry

    init_registry(data_dir)
    reg = get_registry()
    reg.migrate_platform()

    if driver_mode == "scripted":
        from elenchus.sim.driver import CannedLLMClient

        srv.opponent._llm_client = CannedLLMClient()
        srv.opponent.enable_phase_b = False
    elif driver_mode == "llm":
        if not srv.opponent._has_api_key:
            raise RuntimeError("driver=llm needs ELENCHUS_API_KEY / ANTHROPIC_API_KEY")
    else:
        raise ValueError(f"unknown driver {driver_mode!r}")

    seeded = _seed(reg)

    port = _free_port()
    config = uvicorn.Config(srv.app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()

    import requests

    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        with contextlib.suppress(Exception):
            if requests.get(f"{base_url}/healthz", timeout=1).status_code == 200:
                return base_url, srv, seeded, server
        time.sleep(0.25)
    raise RuntimeError("server did not become ready")


def _seed(reg) -> dict:
    """Seed an admin (password), two participant study tokens (one per
    condition, consumed live via the UI), and a judge with a ready-made
    blinded assignment (for the judge video)."""
    from elenchus import auth
    from elenchus.db import platform as pdb

    con = reg.platform_con()
    out: dict = {}
    admin_pw, judge_pw = "Demo-admin-123456", "Demo-judge-123456"

    with reg.platform_lock:
        admin_id = pdb.create_actor(
            con,
            kind="admin",
            email="researcher@demo.local",
            display_name="Researcher",
            password_hash=auth.hash_password(admin_pw),
        )
        out.update(admin_email="researcher@demo.local", admin_password=admin_pw)

        for cond in ("elenchus", "baseline"):
            actor = pdb.create_actor(
                con, kind="participant", email=None, display_name="Participant", password_hash=None
            )
            tok = secrets.token_urlsafe(24)
            pdb.create_participant_token(
                con, token=tok, actor_id=actor, study_id="DEMO", condition=cond, issued_by=admin_id
            )
            out[f"study_token_{cond}"] = tok

        judge_id = pdb.create_actor(
            con,
            kind="judge",
            email="judge@demo.local",
            display_name="Judge",
            password_hash=auth.hash_password(judge_pw),
        )
        out.update(judge_email="judge@demo.local", judge_password=judge_pw)

        report_ids = {}
        bodies = {
            "elenchus": (
                "# Domain\nClassification of research instruments.\n\n"
                "# Atomic statements\n1. An instrument has a measured quantity.\n"
                "2. Calibration fixes the instrument's reference.\n\n"
                "# Implications\n1. If an instrument is uncalibrated, its readings are not comparable.\n\n"
                "# Notes\nThe expert accepted a tension about drift between calibrations.\n"
            ),
            "baseline": (
                "# Domain\nClassification of laboratory apparatus.\n\n"
                "# Atomic statements\n1. Apparatus is grouped by function.\n"
                "2. Each group shares maintenance requirements.\n\n"
                "# Implications\nNone.\n\n"
                "# Notes\nSeveral grouping questions were left open.\n"
            ),
        }
        for cond, body in bodies.items():
            pa = pdb.create_actor(
                con, kind="participant", email=None, display_name=f"P-{cond}", password_hash=None
            )
            tok = secrets.token_urlsafe(24)
            pdb.create_participant_token(
                con, token=tok, actor_id=pa, study_id="DEMO", condition=cond, issued_by=admin_id
            )
            sid = pdb.create_study_session(
                con, actor_id=pa, study_token=tok, condition=cond, initial_state="complete"
            )
            report_ids[cond] = pdb.record_study_report(
                con,
                session_id=sid,
                condition=cond,
                content=body,
                generator_model="demo-seed",
                prompt_tokens=0,
                completion_tokens=0,
                cost_usd=0.0,
            )
        pkg = pdb.create_judge_package(
            con,
            study_id="DEMO",
            slot_a_report_id=report_ids["elenchus"],
            slot_b_report_id=report_ids["baseline"],
            slot_a_condition="elenchus",
            slot_b_condition="baseline",
            created_by=admin_id,
        )
        out["assignment_id"] = pdb.create_judge_assignment(
            con, judge_actor_id=judge_id, package_id=pkg, assigned_by=admin_id
        )
    out["judge_session"] = auth.create_session(judge_id)
    return out


# ── Playwright helpers ───────────────────────────────────────────────


class Demo:
    def __init__(self, page, think_ms: int):
        self.page = page
        self.think_ms = think_ms

    def caption(self, text: str, hold: int = 1700):
        self.page.evaluate(_CAPTION_JS, text)
        self.page.wait_for_timeout(hold)

    def send(self, text: str):
        ta = self.page.get_by_placeholder("Speak naturally", exact=False)
        ta.click()
        ta.fill(text)
        self.page.wait_for_timeout(500)
        self.page.get_by_role("button", name="SEND").click()
        # Composer disables while the opponent responds; wait for it back.
        from playwright.sync_api import expect

        expect(ta).to_be_enabled(timeout=self.think_ms)
        self.page.wait_for_timeout(1200)

    def answer_surveys(self):
        from playwright.sync_api import expect

        submit_re = re.compile(r"SUBMIT AND")
        for _ in range(6):  # at most ~4 instruments; bounded loop
            submit = self.page.get_by_role("button", name=submit_re)
            if submit.count() == 0:
                return
            # NASA-TLX style sliders: a real click fires the frontend's
            # onMouseDown (which records a mid value) and natively moves
            # the thumb — both mark the item answered, which the JS
            # value-setter trick failed to do under React's controlled input.
            sliders = self.page.locator("input[type=range]")
            for i in range(sliders.count()):
                with contextlib.suppress(Exception):
                    sliders.nth(i).click()
            # Likert items: click the mid '4' button in every item group.
            fours = self.page.get_by_role("button", name="4", exact=True)
            for i in range(fours.count()):
                with contextlib.suppress(Exception):
                    fours.nth(i).click()
            self.page.wait_for_timeout(900)
            try:
                expect(submit.first).to_be_enabled(timeout=4000)
            except Exception:
                return
            submit.first.click()
            self.page.wait_for_timeout(1500)


# ── Persona journeys ─────────────────────────────────────────────────


def _participant(demo: Demo, base_url: str, token: str, condition: str):
    page = demo.page
    page.on("dialog", lambda d: d.accept())  # auto-accept confirm() prompts
    label = (
        "ELENCHUS condition (dialectic)"
        if condition == "elenchus"
        else "BASELINE condition (free chat)"
    )

    page.goto(f"{base_url}/?study={token}")
    demo.caption(f"Participant — {label}. Opening the study link.")
    demo.caption("Briefing: the participant reads what the session involves.", 2600)
    page.get_by_role("button", name="BEGIN TUTORIAL").click()
    demo.caption("Tutorial — practising on a warm-up topic.", 1800)
    demo.send("A pendulum's period depends on its length.")
    demo.caption("The AI responded. Now starting the real task.")
    page.get_by_role("button", name="START THE MAIN TASK").click()
    page.wait_for_timeout(1500)

    if condition == "elenchus":
        demo.caption("Main task — the participant states a position.", 1500)
        demo.send("A measuring instrument must have a defined unit of measure.")
        demo.caption("The AI proposed a tension. The participant accepts it…")
        with contextlib.suppress(Exception):
            from playwright.sync_api import expect

            accept = page.get_by_title("Accept").first
            expect(accept).to_be_visible(timeout=8000)
            accept.click()
            page.wait_for_timeout(2500)
        demo.caption("…and the accepted tension becomes a material implication.", 2600)
        demo.send("Instruments of the same kind must share a unit.")
    else:
        demo.caption("Main task — free-form collaborative chat.", 1500)
        demo.send("Lab apparatus can be grouped by the function it serves.")
        demo.send("Each functional group has shared maintenance needs.")

    demo.caption("Wrapping up the task.")
    page.get_by_role("button", name="FINISH SESSION").click()
    page.wait_for_timeout(1500)
    demo.caption("Task complete — on to the questionnaires.", 2000)
    with contextlib.suppress(Exception):
        page.get_by_role("button", name="CONTINUE TO QUESTIONNAIRES").click()
        page.wait_for_timeout(1200)
        demo.caption("Standardised questionnaires (workload, usability, trust…).", 1800)
        demo.answer_surveys()
    demo.caption("All done — the participant's session is complete.", 2600)


def _judge(demo: Demo, base_url: str, seeded: dict):
    page = demo.page
    page.goto(base_url)
    demo.caption("Judge — logging in.")
    page.get_by_placeholder("email").fill(seeded["judge_email"])
    page.get_by_placeholder("password").fill(seeded["judge_password"])
    page.get_by_role("button", name="SIGN IN").click()
    demo.caption("The judge's blinded evaluation queue.", 2200)
    page.get_by_text(f"Evaluation #{seeded['assignment_id']}").click()
    demo.caption("Two anonymised outputs, side by side — no condition shown.", 3000)
    # Rate every dimension (click the mid '5' for each A/B Likert row).
    fives = page.get_by_role("button", name="5", exact=True)
    for i in range(fives.count()):
        with contextlib.suppress(Exception):
            fives.nth(i).click()
            page.wait_for_timeout(120)
    demo.caption("Rating each output on five quality dimensions.", 1800)
    with contextlib.suppress(Exception):
        page.get_by_role("button", name="Output A").click()
    demo.caption("Picking an overall winner, then submitting.", 1600)
    with contextlib.suppress(Exception):
        page.get_by_role("button", name="SUBMIT EVALUATION").click()
        page.wait_for_timeout(2000)
    demo.caption("Evaluation submitted — back to the queue.", 2400)


def _researcher(demo: Demo, base_url: str, seeded: dict):
    page = demo.page
    page.goto(base_url)
    demo.caption("Researcher — logging in.")
    page.get_by_placeholder("email").fill(seeded["admin_email"])
    page.get_by_placeholder("password").fill(seeded["admin_password"])
    page.get_by_role("button", name="SIGN IN").click()
    page.wait_for_timeout(1200)
    demo.caption("Opening the admin dashboard.")
    page.get_by_role("button", name="ADMIN").first.click()
    page.wait_for_timeout(1000)
    # Tab labels render upper-cased in the DOM (`label.toUpperCase()`),
    # so the accessible name is e.g. "STUDY". Narrate first, then click
    # with a short timeout so a miss can't stall the recording.
    for tab, blurb in (
        ("STUDY", "Study tab — issue participant links, watch sessions progress."),
        ("JUDGING", "Judging tab — assemble blinded packages and review ratings."),
        ("INVITES", "Invites — bring researchers and judges onto the platform."),
        ("USERS", "Users — manage who has access."),
    ):
        demo.caption(blurb, 2600)
        with contextlib.suppress(Exception):
            page.get_by_role("button", name=tab, exact=True).click(timeout=4000)
            page.wait_for_timeout(1200)


# ── Orchestration ────────────────────────────────────────────────────

_PERSONAS = {
    "participant-elenchus": ("Participant (Elenchus)", _participant),
    "participant-baseline": ("Participant (baseline)", _participant),
    "judge": ("Judge", _judge),
    "researcher": ("Researcher", _researcher),
}


def _convert(webm: str, mp4: str) -> bool:
    import imageio_ffmpeg

    exe = imageio_ffmpeg.get_ffmpeg_exe()
    r = subprocess.run(
        [
            exe,
            "-y",
            "-i",
            webm,
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            mp4,
        ],
        capture_output=True,
        text=True,
    )
    return r.returncode == 0 and os.path.exists(mp4)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Record per-persona MP4 walkthroughs of the Elenchus UI."
    )
    ap.add_argument("--out", default="demo-videos", help="output directory for the .mp4 files")
    ap.add_argument("--driver", choices=["scripted", "llm"], default="scripted")
    ap.add_argument(
        "--personas",
        default="all",
        help="comma-separated subset of: " + ", ".join(_PERSONAS),
    )
    ap.add_argument("--slow-mo", type=int, default=350, help="ms delay between Playwright actions")
    args = ap.parse_args()

    chosen = list(_PERSONAS) if args.personas == "all" else args.personas.split(",")
    think_ms = 45000 if args.driver == "llm" else 8000
    os.makedirs(args.out, exist_ok=True)
    data_dir = tempfile.mkdtemp(prefix="elenchus_demo_")

    print(f"Starting in-process server (driver={args.driver})…")
    base_url, _srv, seeded, server = _start_server(data_dir, args.driver)
    print(f"  ready at {base_url}")

    from playwright.sync_api import sync_playwright

    produced: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(slow_mo=args.slow_mo)
        for name in chosen:
            if name not in _PERSONAS:
                print(f"  ! unknown persona {name!r}, skipping")
                continue
            label, journey = _PERSONAS[name]
            vid_dir = os.path.join(data_dir, "vid", name)
            os.makedirs(vid_dir, exist_ok=True)
            ctx = browser.new_context(
                viewport={"width": 1360, "height": 860},
                record_video_dir=vid_dir,
                record_video_size={"width": 1360, "height": 860},
            )
            page = ctx.new_page()
            demo = Demo(page, think_ms)
            print(f"Recording: {label} …")
            try:
                if name == "participant-elenchus":
                    journey(demo, base_url, seeded["study_token_elenchus"], "elenchus")
                elif name == "participant-baseline":
                    journey(demo, base_url, seeded["study_token_baseline"], "baseline")
                else:
                    journey(demo, base_url, seeded)
            except Exception as e:  # noqa: BLE001 — never abort the batch
                print(f"  ! {name} journey hit an error (recording what ran): {e}")
            ctx.close()  # finalizes the .webm
            webm = sorted(glob.glob(os.path.join(vid_dir, "*.webm")))
            if not webm:
                print(f"  ! no video captured for {name}")
                continue
            mp4 = os.path.join(args.out, f"{name}.mp4")
            if _convert(webm[-1], mp4):
                produced.append(mp4)
                print(f"  ✓ {mp4}  ({os.path.getsize(mp4) // 1024} KB)")
            else:
                print(f"  ! ffmpeg conversion failed for {name}")
        browser.close()

    with contextlib.suppress(Exception):
        server.should_exit = True

    print("\nDone. Videos:")
    for m in produced:
        print(f"  {m}")
    return 0 if produced else 1


if __name__ == "__main__":
    sys.exit(main())
