"""One derivation of the role tokens a publish interface exports.

Found by the hansa_silk_road driver's uncalled-predicate scan, which listed
`exported_role_tokens` among five public `domain/` functions referenced only by their own
file. It was not dead: `exported_role_tokens_from_unit`, ten lines below it, had the same
comprehension written out again. Two derivations of one value, one of them uncalled, in
adjacent functions -- and only the copy ran.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from vfx_harness.domain import publish_interfaces


def test_the_unit_form_delegates_rather_than_repeating_the_comprehension() -> None:
    tree = ast.parse(Path(inspect.getfile(publish_interfaces)).read_text(encoding="utf-8"))
    site = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "exported_role_tokens_from_unit"
    )
    called = {
        node.func.id
        for node in ast.walk(site)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "exported_role_tokens" in called


def test_the_two_forms_agree_on_the_same_interfaces() -> None:
    """The property the delegation protects, asserted on values rather than on source."""
    from types import SimpleNamespace

    interface = publish_interfaces.PublishInterface(
        id="cam-rig-interface",
        kind="camera_interface",
        producer_layer_id="1",
        producer_unit_id="camera",
        producer_unit_digest="0" * 64,
        exports=(
            ("role", "cam_rig"),
            ("target_role", "cam_rig.target"),
            ("control", "dolly"),
        ),
    )
    unit = SimpleNamespace(
        publishes=(interface,),
        mutates=SimpleNamespace(roles=("cam_rig",)),
    )

    from_unit = publish_interfaces.exported_role_tokens_from_unit(unit)
    assert from_unit == publish_interfaces.exported_role_tokens((interface,))
    assert from_unit == frozenset({"cam_rig", "cam_rig.target"})
