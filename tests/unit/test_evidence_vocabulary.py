"""Vocabulary honesty: new temporal/projected kinds validate, vacuous shapes fail closed.

Run 20260824T153427Z-91b7c1 bound R4 to a bbox_center_x with lo=-1.0 (passes anything the
metric can read) and R29 to an invented builder-writable custom property (self-
certification) after 8 validator rounds failed to express clearance — the vocabulary
lacked the kinds, and padding was representable. The measured values of the three kinds
are pinned by a real-Blender fixture recorded in HIR-0017; this suite pins the schema
and the linting."""

from __future__ import annotations

from vfx_harness.evidence.scene_checks import (
    KIND_DEFINITIONS,
    KIND_DOMAINS,
    SUPPORTED_KINDS,
    _blender_probe,
    validate_row,
)


def _row(**overrides) -> dict:
    base = {
        "id": "row",
        "axis": "axis",
        "owner_layer": "1",
        "fault_owner": "1",
        "activates_at": "1",
        "lifecycle": "layer",
    }
    base.update(overrides)
    return base


def test_new_kinds_are_registered_and_defined() -> None:
    for kind in ("curve_derivative_max", "path_clearance_min", "parallax_displacement_profile"):
        assert kind in SUPPORTED_KINDS
        assert kind in KIND_DEFINITIONS
    assert KIND_DOMAINS["curve_derivative_max"] == "temporal"
    assert KIND_DOMAINS["path_clearance_min"] == "temporal"
    assert KIND_DOMAINS["parallax_displacement_profile"] == "projected_composition"


def test_new_kinds_validate_and_compile_into_the_probe() -> None:
    rows = [
        _row(id="d", kind="curve_derivative_max", roles=["cam_rig"],
             property="rotation_euler", frames=[1, 240], op="max", hi=0.35),
        _row(id="c", kind="path_clearance_min", roles=["cam_rig"],
             compare_roles=["geo.*"], frames=[1, 240], frame_step=4,
             op="min", lo=0.5, lifecycle="persistent"),
        _row(id="p", kind="parallax_displacement_profile", roles=["fg.*"],
             compare_roles=["bg.*"], frames=[1, 240], op="min", lo=1.2),
    ]
    for row in rows:
        assert validate_row(row) is None, (row["id"], validate_row(row))
    compile(_blender_probe(rows, 1), "probe", "exec")


def test_projected_bounds_outside_the_frame_are_vacuous() -> None:
    error = validate_row(
        _row(id="v", kind="bbox_center_x", roles=["cam_rig"], frame=1, op="min", lo=-1.0)
    )
    assert error is not None and "outside the normalized frame" in error


def test_builder_writable_custom_properties_cannot_certify() -> None:
    error = validate_row(
        _row(id="s", kind="object_property", roles=["cam_rig"], frame=1,
             property="clearance_min_distance", op="min", lo=0.5)
    )
    assert error is not None and "self-certification" in error

    assert validate_row(
        _row(id="ok", kind="object_property", roles=["cam_rig"], frame=1,
             property="data.lens", op="eq", value=18, tol=0.001)
    ) is None


def test_two_sided_kinds_require_disjoint_selectors() -> None:
    for kind in ("path_clearance_min", "parallax_displacement_profile"):
        error = validate_row(
            _row(id="o", kind=kind, roles=["x"], compare_roles=["x"],
                 frames=[1, 2], op="min", lo=0.5)
        )
        assert error is not None and "disjoint" in error
