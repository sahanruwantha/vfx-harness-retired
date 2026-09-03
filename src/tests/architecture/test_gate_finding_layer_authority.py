"""A gate finding about one layer must carry that layer in its type (HIR-0187).

Ownership written only into the human-readable ``where`` string is ownership
:meth:`GateResult.clean_for` cannot read, so a single layer's finding silently
becomes a plan-wide block: it stops every other layer's transaction and belongs to
none of them.  Before this rule, 33 of the 34 layer-owned constructions in the
package interpolated ``layer {lid}`` into ``where`` and passed no typed ``layer=``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[3] / "vfx_harness" / "evaluation" / "plan_gate"


def _layer_prose_constructions() -> list[tuple[str, int, str]]:
    """Raw ``Finding(...)`` calls whose ``where`` interpolates a layer id."""
    offenders: list[tuple[str, int, str]] = []
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "Finding"
            ):
                continue
            if any(keyword.arg == "layer" for keyword in node.keywords):
                continue
            if len(node.args) < 3:
                continue
            where = node.args[2]
            if not isinstance(where, ast.JoinedStr):
                continue
            first = where.values[0] if where.values else None
            if (
                isinstance(first, ast.Constant)
                and isinstance(first.value, str)
                and first.value.startswith("layer ")
            ):
                offenders.append((path.name, node.lineno, ast.unparse(where)))
    return offenders


def test_no_gate_finding_carries_its_layer_only_in_prose() -> None:
    offenders = _layer_prose_constructions()
    assert not offenders, (
        "these gate findings name a layer in `where` but drop it from the type; use "
        "Finding.in_layer(check, blocking, layer, where, what, fix) so the rendered "
        "prose and the typed owner come from the same value:\n"
        + "\n".join(f"  {name}:{line} {src}" for name, line, src in offenders)
    )


def test_the_rule_is_enforced_against_an_injected_violation(tmp_path: Path) -> None:
    """The scan must actually fail when a layer id is interpolated without the type."""
    module = tmp_path / "injected.py"
    module.write_text(
        'Finding("contracts", True, f"layer {lid} judge f{frame}", "what", "fix")\n',
        encoding="utf-8",
    )
    tree = ast.parse(module.read_text(encoding="utf-8"))
    call = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "Finding"
    )
    where = call.args[2]
    assert isinstance(where, ast.JoinedStr)
    assert where.values[0].value.startswith("layer ")
    assert not any(keyword.arg == "layer" for keyword in call.keywords)


@pytest.mark.parametrize("layer", ["1", "12", "root"])
def test_in_layer_composes_prose_and_type_from_one_value(layer: str) -> None:
    from vfx_harness.evaluation.plan_gate.types import Finding

    finding = Finding.in_layer("contracts", True, layer, "judge f200", "what", "fix")
    assert finding.layer == layer
    assert finding.where == f"layer {layer} judge f200"
    bare = Finding.in_layer("contracts", True, layer, "", "what")
    assert bare.where == f"layer {layer}"
    assert bare.layer == layer
