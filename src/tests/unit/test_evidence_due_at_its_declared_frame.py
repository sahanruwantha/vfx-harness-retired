"""A bound row is required at the frame it measures, not at every moment of its claim.

Room run 20260904T154222Z-bc1735 built both layer-1 units to PASS (canonical 5.0 at 113, 151
and 176) and then could not mint the layer evaluation receipt: the group plan demanded the
113-pinned rows at 176, the 176-pinned rows at 113, and all eight frame-pinned rows at 151,
where none is declared. failed was empty at every point and status was "missing" (HIR-0204).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vfx_harness.domain.layer_replay_observations import (
    LayerReplayClaimRequirement,
    LayerReplayEvaluationGroupPlan,
)
from vfx_harness.evidence.scene_checks import scene_contract_declared_frames


def _plan(claims) -> LayerReplayEvaluationGroupPlan:
    return LayerReplayEvaluationGroupPlan(
        group_index=0,
        planned_group_count=1,
        requirement_ids=(),
        debt_id=None,
        definition_digest=None,
        activation_digest=None,
        payment_generation_digest=None,
        evidence_kind="executable_only",
        render_mode=None,
        render_scale=None,
        judge_points=((113, "refs/a.png"), (151, "refs/b.png"), (176, "refs/c.png")),
        claims=tuple(claims),
        axes=(),
    )


def test_frame_pinned_rows_are_due_only_at_their_own_frame() -> None:
    """The exact room shape: one claim over three moments binding rows pinned to two."""
    plan = _plan(
        [
            LayerReplayClaimRequirement.mint(
                claim_id="cam-projected-framing",
                authority="executable_required",
                judge_frames=(113, 151, 176),
                evidence_ids=("cam-aim-x-113", "cam-aim-x-176", "cam-rot-smooth"),
                evidence_frames=(("cam-aim-x-113", (113,)), ("cam-aim-x-176", (176,))),
            )
        ]
    )

    assert plan.executable_evidence_ids(113) == ("cam-aim-x-113", "cam-rot-smooth")
    assert plan.executable_evidence_ids(176) == ("cam-aim-x-176", "cam-rot-smooth")
    assert plan.executable_evidence_ids(151) == ("cam-rot-smooth",), (
        "no projected row is pinned to 151, so only the unframed temporal row is due there"
    )


def test_a_row_declaring_several_frames_is_due_at_each_of_them() -> None:
    plan = _plan(
        [
            LayerReplayClaimRequirement.mint(
                claim_id="targets-fixed",
                authority="executable_required",
                judge_frames=(113, 151, 176),
                evidence_ids=("targets-object-count", "aim-target-height"),
                evidence_frames=(("aim-target-height", (113, 176)),),
            )
        ]
    )

    assert plan.executable_evidence_ids(113) == ("targets-object-count", "aim-target-height")
    assert plan.executable_evidence_ids(176) == ("targets-object-count", "aim-target-height")
    assert plan.executable_evidence_ids(151) == ("targets-object-count",)


def test_the_map_must_name_ids_the_claim_binds_and_may_not_repeat_them() -> None:
    with pytest.raises(ValueError, match="outside evidence_ids"):
        LayerReplayClaimRequirement.mint(
            claim_id="c",
            authority="executable_required",
            judge_frames=(1,),
            evidence_ids=("a",),
            evidence_frames=(("b", (1,)),),
        )
    with pytest.raises(ValueError, match="repeats id"):
        LayerReplayClaimRequirement.mint(
            claim_id="c",
            authority="executable_required",
            judge_frames=(1,),
            evidence_ids=("a",),
            evidence_frames=(("a", (1,)), ("a", (2,))),
        )


def test_declared_frames_come_from_the_selected_contract_rows(tmp_path: Path, monkeypatch) -> None:
    rows = [
        {"id": "cam-height-113", "kind": "bbox_height", "frame": 113},
        {"id": "cam-lens-schedule", "kind": "keyframe_schedule"},
        {"id": "cam-move", "kind": "curve_derivative_max", "frames": [113, 176]},
        {"id": "", "kind": "ignored", "frame": 1},
        {"id": "bad-frame", "kind": "x", "frame": True},
    ]
    from vfx_harness.evidence.scene_checks import deferred_subject

    monkeypatch.setattr(deferred_subject, "load_rows", lambda folder, authority=None: rows)

    declared = scene_contract_declared_frames(tmp_path)

    assert declared == {"cam-height-113": (113,), "cam-move": (113, 176)}
    assert "cam-lens-schedule" not in declared, "an unframed row falls back to the active judge"
    assert "bad-frame" not in declared and "" not in declared


def test_the_receipt_round_trips_the_new_field() -> None:
    claim = LayerReplayClaimRequirement.mint(
        claim_id="c",
        authority="executable_required",
        judge_frames=(1, 2),
        evidence_ids=("a", "b"),
        evidence_frames=(("a", (2,)),),
    )
    payload = json.loads(json.dumps(claim.as_dict()))

    assert LayerReplayClaimRequirement.from_dict(payload, "claim") == claim
