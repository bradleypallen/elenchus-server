"""
report.py — render the simulation outcome.

Produces a structured `SimReport` (machine-readable, for tests + CI
gating) and a human-readable text rendering (the step-by-step
timeline + problems list + aggregates + blinding analysis the
researcher reads before a pilot).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .client import Recorder, StepRecord


@dataclass
class SimReport:
    total_steps: int
    problems: list[StepRecord]
    p50_latency_ms: int
    p95_latency_ms: int
    participants_completed: int
    participants_total: int
    reports_generated: int
    ratings_submitted: int
    blinding_total: int
    blinding_correct: int
    blinding_unsure: int
    steps: list[StepRecord] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """A clean run: no unexpected non-2xx, every participant did
        both conditions."""
        return not self.problems and self.participants_completed == self.participants_total


def _percentile(values: list[int], pct: float) -> int:
    if not values:
        return 0
    s = sorted(values)
    idx = min(len(s) - 1, int(round((pct / 100.0) * (len(s) - 1))))
    return s[idx]


def build_report(
    rec: Recorder,
    *,
    participants_total: int,
    outcomes: dict,
    blinding: list[dict],
) -> SimReport:
    problems = [s for s in rec.steps if not s.ok and not s.expected_non_2xx]
    latencies = [s.latency_ms for s in rec.steps]

    completed = sum(
        1
        for conds in outcomes.values()
        if conds.get("elenchus", {}).get("session_id")
        and conds.get("baseline", {}).get("session_id")
    )
    reports = sum(1 for conds in outcomes.values() for c in conds.values() if c.get("report_id"))
    ratings = sum(1 for s in rec.steps if s.action == "submit_rating" and s.ok)

    b_total = len(blinding)
    b_correct = sum(1 for r in blinding if r["guess"] == r["truth"])
    b_unsure = sum(1 for r in blinding if r["guess"] == "unsure")

    return SimReport(
        total_steps=len(rec.steps),
        problems=problems,
        p50_latency_ms=_percentile(latencies, 50),
        p95_latency_ms=_percentile(latencies, 95),
        participants_completed=completed,
        participants_total=participants_total,
        reports_generated=reports,
        ratings_submitted=ratings,
        blinding_total=b_total,
        blinding_correct=b_correct,
        blinding_unsure=b_unsure,
        steps=list(rec.steps),
    )


def render_text(report: SimReport, *, show_timeline: bool = True) -> str:
    lines: list[str] = []
    lines.append("═══ Elenchus pilot simulation ═══════════════════════════")
    status = "✓ PASS" if report.ok else "✗ PROBLEMS FOUND"
    lines.append(f"  Result:               {status}")
    lines.append(
        f"  Participants:         {report.participants_completed}/"
        f"{report.participants_total} completed both conditions"
    )
    lines.append(f"  Reports generated:    {report.reports_generated}")
    lines.append(f"  Judge ratings:        {report.ratings_submitted}")
    lines.append(f"  Total HTTP steps:     {report.total_steps}")
    lines.append(f"  Latency p50 / p95:    {report.p50_latency_ms} / {report.p95_latency_ms} ms")

    if report.blinding_total:
        lines.append("")
        lines.append("  Blinding analysis (judge condition-guess vs truth):")
        decisive = report.blinding_total - report.blinding_unsure
        lines.append(
            f"    {report.blinding_total} guesses · {report.blinding_unsure} unsure · "
            f"{report.blinding_correct} correct"
        )
        if decisive:
            acc = report.blinding_correct / decisive
            lines.append(
                f"    decisive accuracy: {acc:.0%} "
                f"(≈50% = good blinding; scripted runs guess 'unsure')"
            )

    if report.problems:
        lines.append("")
        lines.append(f"  ⚠ PROBLEMS ({len(report.problems)}):")
        for p in report.problems:
            lines.append(
                f"      [{p.actor}] {p.action} {p.method} {p.path} → "
                f"HTTP {p.status}" + (f"  ({p.note})" if p.note else "")
            )
    else:
        lines.append("")
        lines.append("  ✓ No unexpected non-2xx responses.")

    if show_timeline:
        lines.append("")
        lines.append("  ─── Step timeline ───")
        for s in report.steps:
            mark = "ok " if s.ok else "ERR"
            note = f"  · {s.note}" if s.note else ""
            lines.append(
                f"    {mark} [{s.actor:<16}] {s.action:<18} "
                f"{s.method} {s.path} → {s.status} ({s.latency_ms}ms){note}"
            )

    lines.append("")
    return "\n".join(lines)
