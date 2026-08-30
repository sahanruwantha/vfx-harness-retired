"""Judge-set rejections name the legal extra-frame binding (HIR-0029)."""

from __future__ import annotations

import json

import pytest
from jsonschema import Draft202012Validator

from vfx_harness.domain.work_units import (
    EXTRA_FRAME_BINDING_RULE,
    LOOK_REQUIRES_IMAGE_DOMAIN_RULE,
    UNIT_JUDGE_CLAIM_COVERAGE_RULE,
    EvaluationPolicy,
    WorkUnit,
    compile_clustered_mutation_roles,
    compile_deferred_subject_activation,
    compile_frame_authority,
    deferred_subject_activation_gaps,
    layer_judge_frames,
    uncovered_unit_judge_frames,
    unearned_look_judge_frames,
    work_unit_authoring_schema,
)


def _evaluation(*, extra_context_frame: int | None = None) -> dict:
    frames = [1]
    if extra_context_frame is not None:
        frames = [1, extra_context_frame]
    return {
        "primary_judge": 1,
        "judge": [{"frame": 1, "ref": "refs/a.png"}],
        "temporal_evidence": "none",
        "claims": [{
            "id": "claim.a",
            "proposition": "the unit has the declared state",
            "axis": "form",
            "property": "state.a",
            "subject_roles": ["a.role"],
            "subject_controls": [],
            "moments": [1],
            "kind": "atomic",
            "required": True,
            "authority": "executable_required",
            "repair_owner": "a",
            "asserts": "scene",
            "evidence": [{"kind": "scene_contract", "id": "a-contract"}],
        }],
        "composition_context": {"frames": frames, "contract_ids": ["a-vis"]},
    }


def test_compile_frame_authority_preserves_declared_judge_order() -> None:
    row = {
        "judge": [
            {"frame": 1, "ref": "refs/a.png"},
            {"frame": 240, "ref": "refs/b.png"},
            {"frame": 1, "ref": "refs/dup.png"},
        ]
    }
    assert layer_judge_frames(row) == (1, 240)
    card = compile_frame_authority(row)
    assert card["layer_judge_frames"] == [1, 240]
    assert "composition_context.contract_ids" in card["extra_frame_scene_contracts"]
    assert card["required_claim_coverage"] == UNIT_JUDGE_CLAIM_COVERAGE_RULE
    assert card["look_image_domain"] == LOOK_REQUIRES_IMAGE_DOMAIN_RULE


def test_uncovered_unit_judge_frames_names_the_hole() -> None:
    from types import SimpleNamespace

    unit = SimpleNamespace(
        evaluation=SimpleNamespace(
            judges=(SimpleNamespace(frame=72), SimpleNamespace(frame=150)),
            claims=(SimpleNamespace(required=True, moments=(72,)),),
        )
    )
    assert uncovered_unit_judge_frames(unit) == (150,)


def test_unearned_look_judge_frames_names_scene_only_look() -> None:
    from types import SimpleNamespace

    from tests.architecture.test_staged_architecture import _claim
    from vfx_harness.domain.work_units import WorkUnit

    row = {
        "id": "materials_energy",
        "title": "Materials",
        "plan": "plans/units/materials_energy.md",
        "depends_on": [],
        "mutates": {
            "mode": "scoped",
            "roles": ["lookdev.material_primary"],
            "controls": [],
            "script_spans": ["build/units/02/materials_energy.py"],
        },
        "protects": {
            "selector": "all_active_upstream_interfaces",
            "resolve_to_explicit_ids_at": "freeze",
        },
        "evaluation": {
            "primary_judge": 72,
            "judge": [
                {"frame": 72, "ref": "refs/f072.png"},
                {"frame": 150, "ref": "refs/f150.png"},
            ],
            "temporal_evidence": "none",
            "claims": [
                _claim("materials_energy", frame=72),
                _claim(
                    "materials_energy",
                    cid="claim.materials_energy.f150",
                    frame=150,
                ),
            ],
        },
        "completion": "all_required_claims_and_protected_contracts_pass",
        "look_capabilities": ["material", "color"],
    }
    unit = WorkUnit.parse(row, "unit.materials_energy")
    assert unearned_look_judge_frames(unit) == (72, 150)

    lookless = SimpleNamespace(
        look_capabilities=(),
        evaluation=unit.evaluation,
    )
    assert unearned_look_judge_frames(lookless) == ()


def test_composition_context_frames_outside_judge_name_extra_frame_binding() -> None:
    with pytest.raises(ValueError) as raised:
        EvaluationPolicy.parse(_evaluation(extra_context_frame=12), "unit.evaluation")
    message = str(raised.value)
    assert "outside the judge set: [12]" in message
    assert EXTRA_FRAME_BINDING_RULE in message
    assert "composition_context.contract_ids" in message


def _camera_ticket(role: str) -> dict:
    return {
        "id": "camera",
        "title": "Camera",
        "plan": "plans/units/camera.md",
        "depends_on": ["target"],
        "consumes": [{
            "producer": "target",
            "interface_id": "target.publish",
            "kind": "placement_control",
        }],
        "mutates": {
            "mode": "scoped",
            "roles": ["camera.rig"],
            "controls": [],
            "control_roles": {},
            "script_spans": ["build/units/01/camera.py"],
        },
        "protects": {
            "selector": "all_active_upstream_interfaces",
            "resolve_to_explicit_ids_at": "freeze",
        },
        "look_capabilities": [],
        "provides": ["camera"],
        "evaluation": {
            "primary_judge": 1,
            "judge": [{"frame": 1, "ref": "refs/a.png"}],
            "temporal_evidence": "none",
            "claims": [{
                "id": "camera-aligns",
                "proposition": "the camera aligns to the predecessor point",
                "axis": "camera_alignment",
                "property": "projected_origin_x",
                "subject_roles": ["camera.rig", role],
                "subject_controls": [],
                "moments": [1],
                "kind": "atomic",
                "required": True,
                "authority": "executable_required",
                "repair_owner": "camera",
                "asserts": "projected_composition",
                "evidence": [{"kind": "scene_contract", "id": "point-x"}],
            }],
        },
        "completion": "all_required_claims_and_protected_contracts_pass",
    }


@pytest.mark.parametrize("role", ["product.camera_target", "motion.aim_control"])
def test_unit_ticket_schema_exposes_exact_consumes_and_optional_context(role: str) -> None:
    schema = work_unit_authoring_schema()
    ticket = _camera_ticket(role)

    assert list(Draft202012Validator(schema).iter_errors(ticket)) == []
    assert WorkUnit.parse(ticket, "ticket").consumes[0].producer == "target"

    wrong = json.loads(json.dumps(ticket))
    wrong["consumes"] = [{
        "unit": "target", "control": "placement_control", "roles": [role]
    }]
    errors = list(Draft202012Validator(schema).iter_errors(wrong))
    assert errors
    assert any(list(error.absolute_path)[:1] == ["consumes"] for error in errors)

    wrong = json.loads(json.dumps(ticket))
    wrong["evaluation"]["temporal_evidence"] = "curve_derivative_max and schedule"
    errors = list(Draft202012Validator(schema).iter_errors(wrong))
    assert any("is not one of" in error.message for error in errors)

    wrong = json.loads(json.dumps(ticket))
    wrong["evaluation"]["composition_context"] = {"frames": [], "contract_ids": []}
    errors = list(Draft202012Validator(schema).iter_errors(wrong))
    assert len(errors) >= 2

    wrong = json.loads(json.dumps(ticket))
    wrong["family"] = "camera"
    errors = list(Draft202012Validator(schema).iter_errors(wrong))
    assert any(
        list(error.absolute_path) == []
        and "Additional properties are not allowed" in error.message
        and "'family'" in error.message
        for error in errors
    )


def test_materialization_ticket_encodes_one_mutation_namespace() -> None:
    schema = work_unit_authoring_schema(clustered_mutation_roles=True)
    ticket = _camera_ticket("product.camera_target")
    ticket["mutates"].pop("roles")
    ticket["mutates"].update({
        "role_namespace": "building.mass",
        "role_members": ["tower", "base"],
    })

    assert list(Draft202012Validator(schema).iter_errors(ticket)) == []
    compiled = compile_clustered_mutation_roles(ticket)
    assert compiled["mutates"]["roles"] == [
        "building.mass.tower",
        "building.mass.base",
    ]
    assert "role_namespace" not in compiled["mutates"]
    assert "role_members" not in compiled["mutates"]

    mixed = json.loads(json.dumps(ticket))
    mixed["mutates"]["roles"] = [
        "building.mass.tower", "building.roof.silhouette"
    ]
    errors = list(Draft202012Validator(schema).iter_errors(mixed))
    assert any(
        list(error.absolute_path)[:2] == ["mutates"]
        and "Additional properties are not allowed" in error.message
        for error in errors
    )

    wrong_namespace = json.loads(json.dumps(ticket))
    wrong_namespace["mutates"]["role_namespace"] = "building.mass.extra"
    assert list(Draft202012Validator(schema).iter_errors(wrong_namespace))


def test_clustered_role_compiler_cannot_emit_a_second_absolute_namespace() -> None:
    ticket = _camera_ticket("product.camera_target")
    ticket["mutates"].pop("roles")
    ticket["mutates"].update({
        "role_namespace": "building.mass",
        "role_members": ["tower", "roof.silhouette"],
    })

    compiled = compile_clustered_mutation_roles(ticket)

    assert compiled["mutates"]["roles"] == [
        "building.mass.tower",
        "building.mass.roof.silhouette",
    ]
    assert all(
        role == "building.mass" or role.startswith("building.mass.")
        for role in compiled["mutates"]["roles"]
    )


def test_unit_ticket_schema_exposes_active_layer_script_directory() -> None:
    """HIR-0126: script location is visible before the staging transaction."""
    schema = work_unit_authoring_schema(layer_id="3")
    ticket = _camera_ticket("product.camera_target")
    ticket["mutates"]["script_spans"] = ["build/units/03/camera.py"]
    assert list(Draft202012Validator(schema).iter_errors(ticket)) == []

    for invalid in (
        "build/03_scene.py#camera",
        "build/03_scene.py",
        "build/units/02/camera.py",
    ):
        ticket["mutates"]["script_spans"] = [invalid]
        errors = list(Draft202012Validator(schema).iter_errors(ticket))
        assert any(list(error.absolute_path)[:2] == ["mutates", "script_spans"] for error in errors)


def test_unit_ticket_schema_enumerates_active_layer_axes() -> None:
    schema = work_unit_authoring_schema(axis_ids=["iris_ingress_sequence"])
    ticket = _camera_ticket("product.camera_target")
    claim = ticket["evaluation"]["claims"][0]

    claim["axis"] = "iris_ingress_sequence"
    assert list(Draft202012Validator(schema).iter_errors(ticket)) == []

    claim["axis"] = "iris.mechanism_topology"
    errors = list(Draft202012Validator(schema).iter_errors(ticket))
    assert any(
        list(error.absolute_path)[-1:] == ["axis"]
        and "iris_ingress_sequence" in error.message
        for error in errors
    )


def test_unit_ticket_schema_encodes_complete_interaction_shape() -> None:
    """HIR-0123: interaction fields are one conditional ticket, not parser guesses."""
    schema = work_unit_authoring_schema()
    ticket = _camera_ticket("product.camera_target")
    claim = ticket["evaluation"]["claims"][0]
    claim["kind"] = "interaction"

    errors = list(Draft202012Validator(schema).iter_errors(ticket))
    messages = "\n".join(error.message for error in errors)
    assert "coordination_owner" in messages
    assert "participants" in messages
    assert "controls" in messages

    claim["coordination_owner"] = "camera"
    claim["participants"] = ["camera", "target"]
    claim["controls"] = ["camera.balance"]
    assert list(Draft202012Validator(schema).iter_errors(ticket)) == []

    claim["kind"] = "atomic"
    errors = list(Draft202012Validator(schema).iter_errors(ticket))
    assert errors
    assert any(list(error.absolute_path)[-2:] == ["claims", 0] for error in errors)


def test_interaction_parser_reports_complete_coordination_shape() -> None:
    ticket = _camera_ticket("product.camera_target")
    claim = ticket["evaluation"]["claims"][0]
    claim["kind"] = "interaction"

    with pytest.raises(ValueError) as raised:
        WorkUnit.parse(ticket, "ticket")

    message = str(raised.value)
    assert "coordination_owner must be a same-layer work-unit id" in message
    assert "participants needs at least two same-layer work-unit ids" in message
    assert "controls must bound interaction balancing" in message
    assert "not semantic roles or controls" in message


def test_deferred_subject_activation_compiles_earliest_geometry_successor() -> None:
    from vfx_harness.domain.work_units import DEFERRED_SUBJECT_BBOX_KINDS
    from vfx_harness.evidence.scene_checks import BBOX_KINDS

    assert DEFERRED_SUBJECT_BBOX_KINDS == BBOX_KINDS
    layers = [
        {
            "id": "1",
            "title": "Camera",
            "jit": {
                "depends_on_layers": [],
                "provides": {"camera": ["camera.rig"]},
                "reserved_roles": ["camera.rig"],
            },
        },
        {
            "id": "2",
            "title": "Form",
            "jit": {
                "depends_on_layers": ["1"],
                "provides": {},
                "reserved_roles": ["subject.mass"],
            },
        },
        {
            "id": "3",
            "title": "Lookdev",
            "jit": {
                "depends_on_layers": ["1", "2"],
                "reserved_roles": ["lookdev.material"],
            },
        },
    ]
    card = compile_deferred_subject_activation(layers, "1")
    assert card["owner_provides_camera"] is True
    assert card["earliest_geometry_layer"] == "2"
    assert [row["id"] for row in card["successors"]] == ["2", "3"]

    wrong = {
        "id": "bbox-later",
        "kind": "bbox_height",
        "owner_layer": "1",
        "activates_at": "3",
    }
    gaps = deferred_subject_activation_gaps(card, [wrong])
    assert len(gaps) == 1
    assert gaps[0].found == "3"
    assert gaps[0].expected == "2"
    assert not deferred_subject_activation_gaps(card, [{**wrong, "activates_at": "2"}])
