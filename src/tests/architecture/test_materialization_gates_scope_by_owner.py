"""A layer's materialization is gated on findings that layer owns (HIR-0189).

Rematerialization `20260903T211552Z-8ad44d` staged all five layer-2 units and closed all
21 requirement bindings, then could not finalize:

    ✗ [composition-coverage] layer 1 judge f200 subject layer 3: camera layer authors no
      persistent bbox_* row for hero_window.* at a shared judge frame

Layer 2 has no scope to author layer 1's camera framing. The session spent its remaining
turns against a constraint it could not satisfy, and the run had to be stopped.

Both gates must scope the same way: if the finalize tool attested against one set of
findings and publication refused on another, a session could attest CLEAN and still fail
to publish.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from vfx_harness.evaluation import plan_gate
from vfx_harness.evaluation.plan_gate.types import Finding, GateResult

ROOT = Path(__file__).resolve().parents[2] / "vfx_harness"
GATE_SITES = (
    ROOT / "agents" / "plan_tools" / "materialize_mcp.py",
    ROOT / "orchestration" / "jit_materialization" / "publish.py",
)


def _functions_deciding_on_a_gate_verdict(path: Path) -> list[ast.AST]:
    """Functions that branch on a GateResult's cleanliness."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if any(
            isinstance(inner, ast.Attribute) and inner.attr == "clean"
            for inner in ast.walk(node)
        ):
            out.append(node)
    return out


@pytest.mark.parametrize("path", GATE_SITES, ids=lambda p: p.name)
def test_a_materialization_gate_scopes_its_verdict_to_the_owning_layer(path: Path) -> None:
    deciding = _functions_deciding_on_a_gate_verdict(path)
    assert deciding, f"{path.name} no longer decides on a gate verdict; retarget this rule"
    unscoped = [
        node.name for node in deciding if "scoped_to_layer" not in ast.unparse(node)
    ]
    assert not unscoped, (
        f"{path.name}: {', '.join(unscoped)} gates a layer's materialization on the whole "
        "plan. Pass the result through plan_gate.scoped_to_layer(result, layer_id) so a "
        "finding another layer owns cannot block a session that has no scope to repair it."
    )


def _result() -> GateResult:
    return GateResult(
        "shot",
        [
            Finding.in_layer("composition-coverage", True, "1", "judge f200", "layer 1 owns this"),
            Finding.in_layer("data-block-carrier", True, "2", "unit u", "layer 2 owns this"),
            Finding("authority", True, "plans/current.json", "nobody owns this alone"),
            Finding.in_layer("style", False, "1", "warn", "another layer's warning"),
        ],
        {"layers": 4},
    )


def test_a_layer_sees_its_own_findings_and_every_plan_wide_one() -> None:
    owned = plan_gate.scoped_to_layer(_result(), "2")
    assert [f.check for f in owned.blocking] == ["data-block-carrier", "authority"]


def test_another_layers_finding_and_warning_are_both_dropped() -> None:
    owned = plan_gate.scoped_to_layer(_result(), "2")
    assert all(f.layer != "1" for f in owned.findings)


def test_a_layer_with_nothing_of_its_own_still_sees_plan_wide_blockers() -> None:
    """Scoping narrows ownership; it never makes a plan-wide defect disappear."""
    owned = plan_gate.scoped_to_layer(_result(), "3")
    assert not owned.clean
    assert [f.check for f in owned.blocking] == ["authority"]


def test_scoping_preserves_the_stats_the_report_renders() -> None:
    assert plan_gate.scoped_to_layer(_result(), "2").stats == {"layers": 4}


def test_an_unowned_finding_would_otherwise_block_every_layer() -> None:
    """The injected failure: without scoping, layer 2 is blocked by layer 1's defect."""
    assert not _result().clean
    assert "composition-coverage" in [f.check for f in _result().blocking]
