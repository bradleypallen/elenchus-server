"""
sim — agent-driven pilot-study simulation harness.

Drives the full Sloan study end-to-end (researcher → participants →
judges) against the *real* HTTP API, so that the platform's plumbing
can be exercised and validated before any human pilot.

What it validates: the platform. Auth, the participant state machine,
condition routing, report generation, the blinding mechanics, judge
rating, per-study export, cost/latency, and error handling under the
real request stack.

What it does NOT validate: the science. LLM-driven participants prove
the machinery is robust; they cannot tell us whether the
AI-as-collaborator condition produces measurably better outputs than
AI-as-tool. That is what the human pilot measures. This harness exists
to de-risk the plumbing so the humans only test the hypothesis.

Two persona drivers:
  * scripted — deterministic canned content, no API key. Fast, free,
    CI-able. The LLM is stubbed at the network boundary only, so the
    entire server stack still runs for real.
  * llm — reuses the platform's own LLMClient. Validates real dialogue
    dynamics, judge behaviour, and cost. For pre-pilot rehearsals.

Entry point: `elenchus sim` (see `runner.run_simulation`).
"""

from .report import SimReport, build_report
from .runner import run_simulation

__all__ = ["run_simulation", "SimReport", "build_report"]
