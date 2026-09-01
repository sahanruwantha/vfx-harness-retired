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
    OPERATOR_FIELDS,
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


def test_operator_vocabulary_names_exact_threshold_fields() -> None:
    """HIR-0125: equality targets use value, never a guessed eq field."""
    assert OPERATOR_FIELDS["eq"]["required"] == ["value"]
    assert OPERATOR_FIELDS["eq"]["optional"] == ["tol"]
    assert "no `eq` field" in OPERATOR_FIELDS["eq"]["description"]
    assert OPERATOR_FIELDS["min"]["required"] == ["lo"]
    assert OPERATOR_FIELDS["max"]["required"] == ["hi"]
    assert OPERATOR_FIELDS["band"]["required"] == ["lo", "hi"]


def test_threshold_errors_name_the_exact_required_field() -> None:
    eq_error = validate_row(_row(kind="object_count", roles=["x"], op="eq")) or ""
    assert 'op "eq"' in eq_error
    assert "`value`" in eq_error
    assert "do not add an `eq` field" in eq_error

    assert "`tol` must be numeric" in (
        validate_row(
            _row(kind="object_count", roles=["x"], op="eq", value=1, tol="wide")
        )
        or ""
    )
    assert "`lo`" in (
        validate_row(_row(kind="object_count", roles=["x"], op="min")) or ""
    )
    assert "`hi`" in (
        validate_row(_row(kind="object_count", roles=["x"], op="max")) or ""
    )
    assert "`lo` and `hi`" in (
        validate_row(_row(kind="object_count", roles=["x"], op="band")) or ""
    )


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
    probe = _blender_probe(rows, 1)
    assert "segments.append" in probe
    assert "prev_f" in probe


def test_curve_derivative_evidence_names_argmax_segment() -> None:
    """Run 20260826T170413Z-ba2b4c: value 7.391312, empty note. The probe already
    walked every adjacent pair; the note must name f23→f24 (or whichever pair peaked)."""
    from vfx_harness.evidence.scene_checks import _evidence, curve_derivative_note

    note = curve_derivative_note(
        [(1, 2, 1.0), (23, 24, 7.391312), (239, 240, 0.2)],
        hi=6.0,
    )
    assert "argmax f23→f24" in note
    assert "7.391" in note
    assert "exceeds hi" in note
    assert "f23→f24=" in note
    assert curve_derivative_note([(1, 2, 0.5)], hi=6.0) == "argmax f1→f2 (0.5)"
    linear = [(frame, frame + 1, 7.391312) for frame in range(1, 24)]
    linear_note = curve_derivative_note(linear, hi=6.0)
    assert "argmax f1→f24" in linear_note
    assert "×23" in linear_note

    row = _row(
        id="cam-location-smoothness",
        kind="curve_derivative_max",
        roles=["cam_rig"],
        property="location",
        frames=[1, 240],
        op="max",
        hi=6.0,
    )
    [ev] = _evidence(
        [row],
        [{
            "id": "cam-location-smoothness",
            "value": 7.391312,
            "segments": [[1, 2, 1.0], [23, 24, 7.391312]],
        }],
    )
    assert ev["pass"] is False
    assert ev["argmax_frames"] == [23, 24]
    assert ev["argmax_delta"] == 7.391312
    assert "argmax f23→f24" in ev["note"]

    [linear_ev] = _evidence(
        [row],
        [{
            "id": "cam-location-smoothness",
            "value": 7.391312,
            "segments": [[frame, frame + 1, 7.391312] for frame in range(1, 24)],
        }],
    )
    assert linear_ev["argmax_frames"] == [1, 24]
    assert "argmax f1→f24" in linear_ev["note"]


def test_projected_bounds_outside_the_frame_are_vacuous() -> None:
    error = validate_row(
        _row(id="v", kind="bbox_center_x", roles=["cam_rig"], frame=1, op="min", lo=-1.0)
    )
    assert error is not None and "outside the normalized frame" in error


def test_projected_band_wider_than_half_the_frame_is_vacuous() -> None:
    """HIR-0127: 0.2–0.8 is 'somewhere on screen', not a composition target."""
    from vfx_harness.evidence.scene_checks import deferred_subject_composition_ids

    for kind in ("projected_origin_x", "projected_origin_y", "bbox_width", "bbox_height"):
        wide = _row(
            id=f"wide-{kind}", kind=kind, roles=["hero"], frame=1, op="band", lo=0.2, hi=0.8,
        )
        error = validate_row(wide)
        assert error is not None and "vacuous" in error, kind
        tight = {**wide, "lo": 0.35, "hi": 0.65}
        assert validate_row(tight) is None, kind
        half = {**wide, "lo": 0.4, "hi": 0.9}
        assert validate_row(half) is None, kind

    deferred = _row(
        id="subject-bbox-later",
        kind="bbox_height",
        roles=["atrium.shell"],
        frame=38,
        op="band",
        lo=0.35,
        hi=0.55,
        owner_layer="1",
        fault_owner="1",
        activates_at="2",
        lifecycle="persistent",
    )
    assert validate_row(deferred) is None
    assert deferred_subject_composition_ids([deferred], "1", 38) == ()
    assert deferred_subject_composition_ids([deferred], "2", 38) == ("subject-bbox-later",)
    assert deferred_subject_composition_ids([deferred], "2", 1) == ()


def test_deferred_subject_bbox_waits_for_complete_geometry_closure() -> None:
    """HIR-0134: pre-unit replay cannot evaluate a future parent role. The last
    dependency-complete overlapping geometry unit pays it; unrelated geometry does not."""
    from dataclasses import replace

    from tests.unit.test_vis_repair_authority import _detail_unit
    from vfx_harness.evidence.scene_checks import (
        deferred_subject_composition_activation_ids,
        deferred_subject_composition_forecast_ids_for_unit,
        deferred_subject_composition_ids_for_unit,
        deferred_subject_composition_payment_gaps,
        prior_interface_rows,
    )

    mass = _detail_unit(provides=["geometry"])
    mass = replace(
        mass,
        id="mass",
        mutates=replace(mass.mutates, roles=("building.mass.tower",)),
    )
    roof = replace(
        mass,
        id="roof",
        depends_on=("mass",),
        mutates=replace(
            mass.mutates,
            roles=("building.roof.silhouette",),
            script_spans=("build/units/02/roof.py",),
        ),
    )
    site = replace(
        roof,
        id="site",
        depends_on=("mass", "roof"),
        mutates=replace(
            roof.mutates,
            roles=("site.ground.island",),
            script_spans=("build/units/02/site.py",),
        ),
    )
    deferred = _row(
        id="building-bbox",
        kind="bbox_height",
        roles=["building"],
        frame=1,
        op="max",
        hi=0.2,
        owner_layer="1",
        fault_owner="1",
        activates_at="2",
        lifecycle="persistent",
    )
    units = (mass, roof, site)

    assert deferred_subject_composition_activation_ids([deferred], "2") == (
        "building-bbox",
    )
    assert prior_interface_rows([deferred], "2") == ()
    assert {row["id"] for row in prior_interface_rows([deferred], "3")} == {
        "building-bbox"
    }
    assert deferred_subject_composition_ids_for_unit(
        [deferred], units, mass, "2"
    ) == ()
    assert deferred_subject_composition_forecast_ids_for_unit(
        [deferred], units, mass, "2"
    ) == ("building-bbox",)
    assert deferred_subject_composition_ids_for_unit(
        [deferred], units, roof, "2"
    ) == ("building-bbox",)
    assert deferred_subject_composition_forecast_ids_for_unit(
        [deferred], units, roof, "2"
    ) == ()
    assert deferred_subject_composition_ids_for_unit(
        [deferred], units, site, "2"
    ) == ()
    assert deferred_subject_composition_forecast_ids_for_unit(
        [deferred], units, site, "2"
    ) == ()
    unordered = (mass, replace(roof, depends_on=()), site)
    [gap] = deferred_subject_composition_payment_gaps(
        [deferred], unordered, "2"
    )
    assert gap.contract_id == "building-bbox"
    assert gap.roles == ("building",)
    assert gap.producer_ids == ("mass", "roof")
    assert deferred_subject_composition_payment_gaps([deferred], units, "2") == ()


def test_only_irreversible_partial_union_forecast_misses_block_freeze() -> None:
    from vfx_harness.evidence.scene_checks import (
        irreversible_deferred_subject_forecast_failures,
    )

    contracts = [
        _row(
            id="height-max",
            kind="bbox_height",
            roles=["building"],
            frame=1,
            op="max",
            hi=0.15,
        ),
        _row(
            id="height-min",
            kind="bbox_height",
            roles=["building"],
            frame=176,
            op="min",
            lo=0.85,
        ),
        _row(
            id="top-min",
            kind="bbox_top_y",
            roles=["building"],
            frame=39,
            op="min",
            lo=0.2,
        ),
        _row(
            id="center-band",
            kind="bbox_center_y",
            roles=["building"],
            frame=39,
            op="band",
            lo=0.4,
            hi=0.6,
        ),
    ]
    evidence = [
        {"id": "height-max", "metric": "bbox_height", "value": 0.38, "pass": False},
        {"id": "height-min", "metric": "bbox_height", "value": 0.07, "pass": False},
        {"id": "top-min", "metric": "bbox_top_y", "value": 0.1, "pass": False},
        {"id": "center-band", "metric": "bbox_center_y", "value": 0.2, "pass": False},
    ]

    blockers = irreversible_deferred_subject_forecast_failures(contracts, evidence)

    assert {row["id"] for row in blockers} == {"height-max", "top-min"}
    assert all(row["source"] == "deferred_subject_forecast_blocker" for row in blockers)
    assert all(row["acceptance_evidence"] is True for row in blockers)
    assert all("cannot_express_in_scope" in row["note"] for row in blockers)


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


def test_schedule_smoothness_refuses_linear_floor_above_hi() -> None:
    """Run 20260826T170413Z-ba2b4c: f1 y=-30 → f24 y=140 is 170/23 ≈ 7.39 against hi 6.0."""
    from vfx_harness.evidence.scene_checks import validate_row_set

    schedule = _row(
        id="cam-spine-schedule",
        kind="keyframe_schedule",
        roles=["cam_rig"],
        op="max",
        hi=0.01,
        samples=[
            {"frame": 1, "values": {"location": [0.0, -30.0, 0.0]}},
            {"frame": 24, "values": {"location": [2.0, 140.0, 5.0]}},
            {"frame": 40, "values": {"location": [-2.0, 172.0, 7.0]}},
        ],
    )
    smooth = _row(
        id="cam-location-smoothness",
        kind="curve_derivative_max",
        roles=["cam_rig"],
        property="location",
        op="max",
        hi=6.0,
        frames=[1, 240],
    )
    findings = validate_row_set([schedule, smooth])
    assert len(findings) == 1
    assert "cam-location-smoothness" in findings[0]
    assert "cam-spine-schedule" in findings[0]
    assert "linear floor" in findings[0]
    assert "7.39" in findings[0]
    assert "interpolation cannot invent a third option" in findings[0]
    assert validate_row_set([schedule, {**smooth, "hi": 8.0}]) == []
    other_role = {**smooth, "id": "other-smooth", "roles": ["cam.iris_face"]}
    assert validate_row_set([schedule, other_role]) == []

    from vfx_harness.evaluation.plan_gate import _cross_row_contract_findings

    gate = _cross_row_contract_findings([schedule, smooth])
    assert len(gate) == 1 and gate[0].blocking is True
    auto_only = _cross_row_contract_findings([
        _row(
            id="world-bloom-threshold-bound", kind="node_socket_value", graph="compositor",
            node_roles=["world.bloom.compositor"], socket="Threshold", direction="input",
            op="min", lo=0.8,
        ),
        _row(
            id="world-bloom-response", kind="control_render_response", graph="compositor",
            node_roles=["world.bloom.compositor"], probe_values=[0.0, 1.0], frame=150,
            op="max", hi=0.15,
        ),
    ])
    assert auto_only and auto_only[0].blocking is False


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
    assert "_checks.surface_visible_fraction" in script
    assert "def _vis_frac" in script
    assert "role_fractions" in script
    assert "per-role" in script

    assert "frame" in (validate_row({**row, "frame": None}) or "")
    assert "roles" in (validate_row({**row, "roles": [], "control_roles": []}) or "")
    assert "vacuous" in (validate_row({**row, "lo": 0}) or "")
    assert "vacuous" in (validate_row({**row, "op": "max", "lo": None, "hi": 1.0}) or "")
    assert validate_row({**row, "op": "max", "lo": None, "hi": 0.05}) is None


def test_projected_origin_is_frame_scoped_point_evidence() -> None:
    """Control/Empty placement has a projection metric that does not imply a surface."""
    from vfx_harness.evidence.scene_checks import (
        CAMERA_REQUIRED_KINDS,
        FRAME_SCOPED_KINDS,
        PROJECTED_ORIGIN_KINDS,
    )

    for kind in ("projected_origin_x", "projected_origin_y"):
        assert kind in SUPPORTED_KINDS
        assert kind in PROJECTED_ORIGIN_KINDS
        assert kind in FRAME_SCOPED_KINDS
        assert kind in CAMERA_REQUIRED_KINDS
        assert KIND_DOMAINS[kind] == "projected_composition"
        row = _row(
            id=f"point-{kind}", kind=kind, roles=["camera.target"],
            frame=175, op="band", lo=0.45, hi=0.55,
        )
        assert validate_row(row) is None
        assert "frame" in (validate_row({**row, "frame": None}) or "")
        script = _blender_probe([row], 175)
        compile(script, "<probe>", "exec")
        assert "requires exactly one selected object origin" in script


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
        publish_current,
    )
    from vfx_harness.observability import run_artifacts
    from vfx_harness.orchestration.jit_materialization import validate_materialization

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


def test_control_render_response_must_declare_its_render_frame() -> None:
    """Run 17581c: a palette response row with no frame silently rendered the f1
    default, where the swept subject was fully occluded — a structurally-0.0 reading
    that burned two builds and four repairs. Functional render rows are frame-scoped."""
    row = _row(
        id="resp", kind="control_render_response", graph="material",
        material_roles=["m.*"], node_roles=["ctrl"], probe_values=[0.0, 1.0],
        region=[0.35, 0.35, 0.65, 0.65], op="min", lo=0.02,
    )
    error = validate_row(row) or ""
    assert "frame" in error
    assert validate_row({**row, "frame": 150}) is None


def test_render_region_stat_is_the_exposure_anchor() -> None:
    """Run 9ff4c5 sealed four lookdev units over a composed frame reading mean 11 /
    stddev 1.2 against refs at 32-81 / 28-64: every relative metric (responses, deltas,
    socket values) passes at any brightness, and nothing anchored the render to the
    reference. The kind is frame-scoped, region-bound, and refuses vacuous bounds."""
    from vfx_harness.evidence.scene_checks import (
        FRAME_SCOPED_KINDS,
        FUNCTIONAL_KINDS,
        KIND_DOMAINS,
        SUPPORTED_KINDS,
    )

    assert "render_region_stat" in SUPPORTED_KINDS
    assert "render_region_stat" in FUNCTIONAL_KINDS
    assert "render_region_stat" in FRAME_SCOPED_KINDS
    assert KIND_DOMAINS["render_region_stat"] == "image"
    row = _row(
        id="anchor", kind="render_region_stat", stat="stddev", frame=150,
        region=[0.2, 0.2, 0.8, 0.8], op="min", lo=16,
    )
    assert validate_row(row) is None
    assert "frame" in (validate_row({**row, "frame": None}) or "")
    assert "region" in (validate_row({**row, "region": None}) or "")
    assert "stat" in (validate_row({**row, "stat": "median"}) or "")
    assert "vacuous" in (validate_row({**row, "lo": 0}) or "")
    assert "vacuous" in (validate_row({**row, "op": "max", "lo": None, "hi": 255}) or "")
    assert "[0,255]" in (validate_row({**row, "lo": 300}) or "")


def test_node_socket_component_accepts_channel_letters() -> None:
    """Run d2ea42 authored component 'B' for a color socket's blue channel — the
    human-native spelling — and int('B') killed the row as a binding defect. Letters
    map to indices; garbage is refused at authoring."""
    from vfx_harness.evidence.scene_checks import _blender_probe

    row = _row(
        id="chan", kind="node_socket_value", graph="material",
        material_roles=["m.*"], node_roles=["ctrl"], socket="Base Color",
        direction="input", component="B", op="min", lo=0.5,
    )
    assert validate_row(row) is None
    assert validate_row({**row, "component": 2}) is None
    assert "R/G/B/A" in (validate_row({**row, "component": "Q"}) or "")
    script = _blender_probe([row], 1)
    compile(script, "<probe>", "exec")
    assert "'B':2" in script


def test_empty_path_clearance_sentinel_never_passes() -> None:
    """Layer 1 sealed a collision row at 1e9: empty compare_roles reported the
    unmeasured sentinel and every min-bound held. Absence is not clearance."""
    from vfx_harness.evidence.scene_checks import (
        PATH_CLEARANCE_UNMEASURED,
        _blender_probe,
        _evidence,
        _holds,
    )

    row = _row(
        id="collision",
        kind="path_clearance_min",
        roles=["cam_rig"],
        compare_roles=["geo.*"],
        frames=[1, 2],
        op="min",
        lo=0.5,
        lifecycle="persistent",
    )
    assert validate_row(row) is None
    assert not _holds(row, PATH_CLEARANCE_UNMEASURED)
    findings = _evidence([row], [{"id": "collision", "value": 1e9, "error": ""}])
    assert findings[0]["pass"] is False
    assert findings[0]["value"] is None
    assert "1e9" in findings[0]["error"]
    probe = _blender_probe([row], 1)
    assert "value=1e9 if best is None" not in probe
    assert "empty obstacle selection is not clearance" in probe
    assert "mesh roles present" in probe
    compile(probe, "probe", "exec")


def test_path_clearance_sentinel_and_zero_floor_are_vacuous() -> None:
    row = _row(
        id="c",
        kind="path_clearance_min",
        roles=["cam_rig"],
        compare_roles=["geo.*"],
        frames=[1, 2],
        op="min",
        lo=0.5,
    )
    assert "vacuous" in (validate_row({**row, "lo": 0}) or "")
    assert "vacuous" in (validate_row({**row, "lo": 1e9}) or "")
    assert "vacuous" in (validate_row({**row, "op": "max", "lo": None, "hi": 1e9}) or "")
    assert validate_row(row) is None
