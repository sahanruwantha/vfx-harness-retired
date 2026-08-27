"""Judge-set rejections name the legal extra-frame binding (HIR-0029)."""

from __future__ import annotations

import pytest

from vfx_harness.domain.work_units import (
    EXTRA_FRAME_BINDING_RULE,
    LOOK_REQUIRES_IMAGE_DOMAIN_RULE,
    UNIT_JUDGE_CLAIM_COVERAGE_RULE,
    EvaluationPolicy,
    compile_frame_authority,
    layer_judge_frames,
    uncovered_unit_judge_frames,
    unearned_look_judge_frames,
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
