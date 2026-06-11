"""
runner.py — set up an isolated environment and run the harness.

Owns the lifecycle concerns the harness shouldn't care about:
  * point the process-wide registry at an isolated data directory so
    the simulation never touches a real deployment's data;
  * in scripted mode, swap the server opponent's LLM client for the
    canned one (restoring it afterward) so the whole stack runs free;
  * apply platform migrations;
  * build the report.
"""

from __future__ import annotations

import logging
import tempfile

from . import personas
from .driver import LLMDriver, ScriptedDriver
from .harness import StudyHarness
from .report import SimReport, build_report

logger = logging.getLogger(__name__)


def run_simulation(
    *,
    driver_mode: str = "scripted",
    participants: int = 4,
    judges: int = 2,
    study_id: str = "SIM",
    data_dir: str | None = None,
) -> SimReport:
    """Run a full pilot simulation and return its `SimReport`.

    `driver_mode`:
      * "scripted" — deterministic, free, CI-able. The server opponent's
        LLM is stubbed at the network boundary; everything else runs.
      * "llm" — real-LLM personas, using the server opponent's configured
        LLMClient (requires a real API key in the environment).
    """
    # Import the server FIRST so its module-level `init_registry(DATA_DIR)`
    # runs (and binds to whatever the env points at), THEN re-point the
    # registry at an isolated sim directory. Doing it in the other order
    # lets the server import clobber our sim registry. Routes read the
    # registry lazily via get_registry(), so the final binding wins.
    import elenchus.server as srv

    from ..db import get_registry, init_registry

    sim_dir = data_dir or tempfile.mkdtemp(prefix="elenchus_sim_")
    init_registry(sim_dir)
    reg = get_registry()
    reg.migrate_platform()

    pcts = personas.default_participants(participants)
    jdgs = personas.default_judges(judges)

    saved_client = None
    try:
        if driver_mode == "scripted":
            driver = ScriptedDriver()
            # Swap the opponent's network client for the canned one;
            # the entire request stack still runs.
            saved_client = srv.opponent._llm_client
            srv.opponent._llm_client = driver.canned_llm_client()
            # Phase B must stay off (Sloan default) regardless of env.
            srv.opponent.enable_phase_b = False
        elif driver_mode == "llm":
            if not srv.opponent._has_api_key:
                raise RuntimeError(
                    "driver_mode='llm' requires a configured API key "
                    "(ELENCHUS_API_KEY / ANTHROPIC_API_KEY)."
                )
            driver = LLMDriver(srv.opponent._llm_client)
        else:
            raise ValueError(f"Unknown driver_mode: {driver_mode!r}")

        harness = StudyHarness(
            srv.app,
            driver,
            participants=pcts,
            judges=jdgs,
            study_id=study_id,
        )
        rec = harness.run()
        return build_report(
            rec,
            participants_total=len(pcts),
            outcomes=harness.outcomes,
            blinding=harness.blinding,
        )
    finally:
        if saved_client is not None:
            srv.opponent._llm_client = saved_client
