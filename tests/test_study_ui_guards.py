"""Static guards on the single-file frontend for the study's two
conditions. The UI has no test harness of its own, so these read the
source: they pin the invariants a browser session confirmed, and fail
loudly if a later edit re-introduces a leak.

The baseline condition must show no formal apparatus — no position or
sequents pane, no badges to open them — whatever this browser's stored
preference says. The panes' open state is a `localStorage` preference,
so without this a crossover participant who opened a pane in their
Elenchus session saw empty "Commitments" / "No sequents yet" panes in
their baseline session, with no way to close them.
"""

from __future__ import annotations

import re
from pathlib import Path

INDEX = Path(__file__).resolve().parents[1] / "src" / "elenchus" / "static" / "index.html"


def _source() -> str:
    return INDEX.read_text(encoding="utf-8")


def test_baseline_never_shows_the_formal_panes():
    src = _source()
    assert "const showLeft = leftVisible && !isBaseline;" in src
    assert "const showRight = rightVisible && !isBaseline;" in src
    # Every place a pane (or its drag handle) is rendered goes through the
    # effective flag, never the stored preference directly.
    assert "{leftVisible && (" not in src and "{rightVisible && (" not in src
    assert src.count("{showLeft && (") == 2 and src.count("{showRight && (") == 2
    # The grid reserves no columns for panes that aren't shown.
    grid = re.search(r"const gridCols = \[(.*?)\]\.filter", src, re.S).group(1)
    assert "leftVisible" not in grid and "rightVisible" not in grid
    assert "showLeft" in grid and "showRight" in grid


def test_baseline_has_no_pane_badges():
    src = _source()
    # The two header badges are each inside a `!isBaseline` guard.
    for title in ("Show position pane", "Show sequents pane"):
        at = src.index(title)
        assert "{!isBaseline && (" in src[at - 400 : at], title
