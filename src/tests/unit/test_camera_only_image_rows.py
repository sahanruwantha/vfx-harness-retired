"""Functional image rows are image debts, and World creation is volume work (HIR-0176)."""

from __future__ import annotations

from types import SimpleNamespace

from vfx_harness.domain.atomicity import script_write_family_evidence
from vfx_harness.domain.image_signal import functional_image_debt_ids, image_signal_dependency_gaps


def _binding(kind: str, contract_id: str) -> SimpleNamespace:
    return SimpleNamespace(kind=kind, id=contract_id)


def _claim(*, asserts: str, required: bool = True, evidence=()) -> SimpleNamespace:
    return SimpleNamespace(
        required=required,
        asserts=asserts,
        evidence=tuple(evidence),
        moments=(175,),
        axis="look",
        property="",
        kind="atomic",
        id=f"claim-{asserts}",
        subject_roles=(),
        subject_controls=(),
        repair_owner="",
    )


def _unit(unit_id: str, *, provides, claims, depends_on=()) -> SimpleNamespace:
    return SimpleNamespace(
        id=unit_id,
        provides=tuple(provides),
        depends_on=tuple(depends_on),
        evaluation=SimpleNamespace(claims=tuple(claims), composition_context=None),
        mutates=SimpleNamespace(
            roles=(f"{unit_id}.body",),
            dresses=(),
            controls=(),
            control_roles={},
            mode="scoped",
            script_spans=(),
        ),
        construction=SimpleNamespace(route="procedural", witnesses=()),
        look_capabilities=(),
        publishes=(),
        consumes=(),
    )


_ROWS = [
    {
        "id": "gray-r",
        "kind": "render_region_stat",
        "frame": 175,
        "op": "band",
        "lo": 90,
        "hi": 112,
        "region": [0, 0, 1, 1],
    },
    {
        "id": "spine",
        "kind": "curve_derivative_max",
        "roles": ["camera.rig"],
        "frames": [1, 213],
        "op": "max",
        "hi": 0.1,
    },
]


def test_functional_rows_bound_by_a_required_image_claim_are_image_debts() -> None:
    camera = _unit(
        "camera_rig",
        provides=["camera"],
        claims=[
            _claim(asserts="image", evidence=[_binding("scene_contract", "gray-r")]),
            _claim(asserts="temporal", evidence=[_binding("scene_contract", "spine")]),
        ],
    )
    rows_by_id = {row["id"]: row for row in _ROWS}
    assert functional_image_debt_ids(camera, rows_by_id) == ("gray-r",)
    # A non-image claim over the same functional row, or an optional claim, owes nothing.
    other = _unit(
        "other",
        provides=["camera"],
        claims=[_claim(asserts="scene", evidence=[_binding("scene_contract", "gray-r")])],
    )
    assert functional_image_debt_ids(other, rows_by_id) == ()
    optional = _unit(
        "opt",
        provides=["camera"],
        claims=[_claim(asserts="image", required=False, evidence=[_binding("scene_contract", "gray-r")])],
    )
    assert functional_image_debt_ids(optional, rows_by_id) == ()


def test_world_creation_is_volume_work_in_the_payload_classifier() -> None:
    evidence = script_write_family_evidence(
        "import bpy\n"
        "world = bpy.data.worlds.new('World')\n"
        "world.use_nodes = True\n"
        "cam = bpy.data.cameras.new('camera')\n"
    )
    assert {item.family for item in evidence} == {"volume", "camera"}
    assert any(
        item.operation == "bpy.data.worlds.new" and item.family == "volume" for item in evidence
    )


def test_camera_only_prefix_with_a_functional_image_row_is_a_bootstrap_gap() -> None:
    camera = _unit(
        "camera_rig",
        provides=["camera"],
        claims=[_claim(asserts="image", evidence=[_binding("scene_contract", "gray-r")])],
    )
    gaps = image_signal_dependency_gaps((camera,), _ROWS)
    assert [(gap.unit_id, gap.contract_ids) for gap in gaps] == [("camera_rig", ("gray-r",))]
    assert (
        image_signal_dependency_gaps((camera,), _ROWS, earlier_signal_available=True) == ()
    )
