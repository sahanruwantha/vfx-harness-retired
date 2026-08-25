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


def test_auto_socket_response_sharing_a_pinned_selector_must_declare_its_socket() -> None:
    """Run 20260825: world-bloom-response (auto socket -> literal 'Value') shared its
    selector with world-bloom-threshold-bound (socket 'Threshold'), demanding a node
    interface CompositorNodeGlare does not have; the contradiction surfaced two builds
    and four repairs after publication. The row set must refuse it at authoring."""
    from vfx_harness.evidence.scene_checks import validate_row_set

    pinned = _row(
        id="world-bloom-threshold-bound", kind="node_socket_value", graph="compositor",
        node_roles=["world.bloom.compositor"], socket="Threshold", direction="input",
        op="min", lo=0.8,
    )
    auto = _row(
        id="world-bloom-response", kind="control_render_response", graph="compositor",
        node_roles=["world.bloom.compositor"], probe_values=[0.0, 1.0], frame=150,
        op="max", hi=0.15,
    )
    findings = validate_row_set([pinned, auto])
    assert len(findings) == 1
    assert "world-bloom-response" in findings[0]
    assert "declare 'socket'" in findings[0]

    # Declaring the socket, or measuring a different selector, is clean.
    assert validate_row_set([pinned, {**auto, "socket": "Threshold"}]) == []
    assert validate_row_set([pinned, {**auto, "node_roles": ["world.atmosphere.volume"]}]) == []
    assert validate_row_set([auto]) == []


def test_visible_fraction_is_registered_frame_scoped_and_compiles() -> None:
    """Run 20260825: layer 2's every judged surface sat behind a solid proxy disc at
    both judge frames; bbox rows project THROUGH occluders and nothing measured
    occlusion. The kind exists so a judged-but-hidden subject is a failing number."""
    from vfx_harness.evidence.scene_checks import (
        FRAME_SCOPED_KINDS,
        KIND_DOMAINS,
        SUPPORTED_KINDS,
    )

    assert "visible_fraction" in SUPPORTED_KINDS
    assert "visible_fraction" in FRAME_SCOPED_KINDS
    assert KIND_DOMAINS["visible_fraction"] == "projected_composition"
    row = _row(
        id="vis", kind="visible_fraction", roles=["subject.*"], frame=72,
        op="min", lo=0.25,
    )
    assert validate_row(row) is None
    script = _blender_probe([row], 72)
    compile(script, "<probe>", "exec")
    assert "ray_cast" in script

    assert "frame" in (validate_row({**row, "frame": None}) or "")
    assert "roles" in (validate_row({**row, "roles": [], "control_roles": []}) or "")
    assert "vacuous" in (validate_row({**row, "lo": 0}) or "")
    assert "vacuous" in (validate_row({**row, "op": "max", "lo": None, "hi": 1.0}) or "")
    assert validate_row({**row, "op": "max", "lo": None, "hi": 0.05}) is None


def test_materialization_requires_visibility_at_every_judge_frame(tmp_path) -> None:
    """The hard half of the rule (the gate half is advisory for grandfathered views):
    a new materialization cannot publish a judge frame nobody proves shows anything."""
    import json

    import pytest as _pytest

    from tests.unit.test_plan_records import (
        _add_deferred_layer,
        _candidate,
        _jit_payload,
        _write,
    )
    from vfx_harness.observability import run_artifacts
    from vfx_harness.orchestration.jit_materialization import validate_materialization
    from vfx_harness.orchestration.plan_authority import publish_current

    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layout = run_artifacts.create(tmp_path, "vis-coverage")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    payload = _jit_payload(tmp_path, bundle.content_hash)
    data = json.loads(payload.read_text(encoding="utf-8"))
    data["scene_contracts"] = [
        row for row in data["scene_contracts"] if row["kind"] != "visible_fraction"
    ]
    _write(payload, data)
    with _pytest.raises(ValueError, match="visible_fraction"):
        validate_materialization(
            bundle.root, payload, expected_bundle_hash=bundle.content_hash
        )
