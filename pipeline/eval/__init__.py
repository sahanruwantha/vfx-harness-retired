"""Measurement, kept separate from the thing being measured.

Every claim about whether a change helped this pipeline has so far rested on one person
reading one run. In a single session that produced five confident findings that all had
to be retracted: a truncation hypothesis, an acceptance thesis, a "finish doesn't
reproduce" claim, a layer-ownership assignment, and a "detail 46% high" that was an
artifact of the measurer's own image upscaling. That last one is the tell — the
instrument was wrong and there was nothing to catch it.

Nothing in this package writes to a shot. It reads shot state, re-scores frames that
already exist, and re-runs scripts that were already accepted. The one thing it does
write is `evals/` at the repo root, which is deliberately OUTSIDE `shots/` so a rebuild
that wipes `build/`, `renders/` and `shot.json` cannot take the baseline with it.
"""

from __future__ import annotations

from pathlib import Path

# Repo root, then the durable store. NOT under shots/ — see the module docstring.
REPO = Path(__file__).resolve().parent.parent.parent
STORE = REPO / "evals"
BASELINES = STORE / "baselines"
VARIANCE = STORE / "variance"

__all__ = ["REPO", "STORE", "BASELINES", "VARIANCE"]
