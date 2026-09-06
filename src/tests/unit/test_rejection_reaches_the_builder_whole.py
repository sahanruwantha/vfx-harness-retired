"""A rejection built to teach must survive the surface that shows it.

hansa_silk_road: `daae404` made a FRAGILE threshold rejection name the legal `lo` range,
the EMPTY diagnosis, and the note that the floor rises with the content the metric
rewards. It is correct, tested, on main -- and it never reached a builder.

    blender/tools/misc.py:641
    lines.append(f"  REJECTED {cid:10} {v.reasons[0][:120]}")

Cut at 120 characters, mid-word, *before* the window begins. And `reasons[0]` drops the
rest, so a verdict carrying FRAGILE and NOT NECESSARY shows one of them -- the "gate that
returned alone" of HIR-0201, alive at the payment surface.

Its author's own inference died with it: the attempt-5 builder re-proposing thresholds
after fragile rejections looked like a model ignoring advice. It never had the advice.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from vfx_harness.blender.tools import misc
from vfx_harness.evidence.checks import Verdict

FRAGILE = (
    "FRAGILE THRESHOLD — candidate clearance 1.533 is below the measured decision margin "
    "2.673 (max of 2× resampling noise and 5% of the measured value). Do not shave a "
    "one-shot threshold against the current render; choose a stable property or build "
    "more margin. Legal lo for this render: 0.0 .. 1.2"
)
NOT_NECESSARY = "NOT NECESSARY — the pre-unit adversary already reads 0.0835 against >= 0.05"


def test_the_whole_reason_survives() -> None:
    """The window is past character 120, which is where it used to be cut."""
    rendered = Verdict("c", False, reasons=[FRAGILE]).why()

    assert rendered == FRAGILE
    assert "Legal lo for this render" in rendered
    assert len(rendered) > 120


def test_every_reason_survives_not_only_the_first() -> None:
    rendered = Verdict("c", False, reasons=[FRAGILE, NOT_NECESSARY]).why()

    assert "FRAGILE THRESHOLD" in rendered
    assert "NOT NECESSARY" in rendered


def test_a_verdict_with_no_reason_still_says_something() -> None:
    assert Verdict("c", False).why() == "failed verification"
    assert Verdict("c", False, reasons=["", ""]).why() == "failed verification"


def test_the_payment_surface_renders_through_that_function() -> None:
    """Parsed, not grepped: a substring check passes with the call deleted."""
    tree = ast.parse(Path(inspect.getfile(misc)).read_text(encoding="utf-8"))
    calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "why" in calls


def test_no_consumer_in_that_file_subscripts_a_reason_list() -> None:
    """The defect was a private slice, so assert none remains rather than assert the fix.

    Parsed rather than grepped, and the first draft of this test proved why: a substring
    check for "reasons[0]" failed on the comment above the fix, which describes the
    defect. A future consumer adding its own `v.reasons[0]` or `[:n]` here fails; prose
    about the defect does not.
    """
    tree = ast.parse(Path(inspect.getfile(misc)).read_text(encoding="utf-8"))
    sliced = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "reasons"
    ]

    assert not sliced, [ast.dump(node) for node in sliced]
