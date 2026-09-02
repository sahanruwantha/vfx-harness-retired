"""Frame and image-claim authority for work units.

This module owns the rules that relate layer judge frames, unit judge frames, claim
moments, and look evidence.  Keeping these rules together makes frame authority
independent from the much larger work-unit serialization contract.
"""

from __future__ import annotations

from typing import Any

EXTRA_FRAME_BINDING_RULE = (
    "composition_context.frames and claim.moments must be a subset of this unit's "
    "judge frames, which must themselves be a subset of the layer judge list. "
    "The layer judge list is structural and cannot change in materialization. "
    "Scene contracts may still measure other frames; bind those contract ids "
    "through composition_context.contract_ids or a required claim whose moments "
    "stay in the unit judge — do not put the extra frames on composition_context.frames, "
    "claim.moments, unit evaluation.judge, or layer.judge."
)

UNIT_JUDGE_CLAIM_COVERAGE_RULE = (
    "every unit evaluation.judge frame must appear in at least one required "
    "claim.moments. A judge frame with no required claim is a contract_gap, "
    "not a critic look vote. Extra-frame scene contracts still bind through "
    "composition_context.contract_ids; do not add those frames to the unit judge."
)

LOOK_REQUIRES_IMAGE_DOMAIN_RULE = (
    "a unit that declares look_capabilities must cover every evaluation.judge "
    "frame with a required claim that asserts image and binds image-domain "
    "evidence (image_contract, qualification, or human_decision). Scene counts "
    "cannot certify appearance. That hole is a contract_gap, not a 5.0 "
    "executable seal and not a critic look vote."
)

LOOK_IMAGE_EVIDENCE_KINDS = frozenset({"image_contract", "qualification"})


def _evidence_kind(item: Any) -> str:
    kind = getattr(item, "kind", None)
    if kind:
        return str(kind)
    if isinstance(item, dict):
        return str(item.get("kind") or "")
    return ""


def required_claim_certifies_look(claim: Any) -> bool:
    """True when a required claim can certify appearance (HIR-0046)."""
    if not getattr(claim, "required", False):
        return False
    if getattr(claim, "asserts", None) != "image":
        return False
    kinds = {_evidence_kind(item) for item in (getattr(claim, "evidence", ()) or ())}
    return bool(kinds & LOOK_IMAGE_EVIDENCE_KINDS)


def unearned_look_judge_frames(unit: Any) -> tuple[int, ...]:
    """Look-owning judge frames with no image-domain required claim (HIR-0046)."""
    if not tuple(getattr(unit, "look_capabilities", ()) or ()):
        return ()
    evaluation = getattr(unit, "evaluation", None)
    judged = {int(point.frame) for point in (getattr(evaluation, "judges", ()) or ())}
    certified = {
        int(moment)
        for claim in (getattr(evaluation, "claims", ()) or ())
        if required_claim_certifies_look(claim)
        for moment in (getattr(claim, "moments", ()) or ())
    }
    return tuple(sorted(judged - certified))


def uncovered_unit_judge_frames(unit: Any) -> tuple[int, ...]:
    """Judge frames that no required claim covers (HIR-0045)."""
    evaluation = getattr(unit, "evaluation", None)
    judges = tuple(getattr(evaluation, "judges", ()) or ())
    claims = tuple(getattr(evaluation, "claims", ()) or ())
    judged = {int(point.frame) for point in judges}
    claimed = {
        int(moment)
        for claim in claims
        if getattr(claim, "required", False)
        for moment in getattr(claim, "moments", ()) or ()
    }
    return tuple(sorted(judged - claimed))


def layer_judge_frames(global_row: dict[str, Any]) -> tuple[int, ...]:
    """Extract declared judge frames from a global layer row, preserving order."""
    frames: list[int] = []
    seen: set[int] = set()
    for point in global_row.get("judge") or []:
        if not isinstance(point, dict):
            continue
        frame = point.get("frame")
        if isinstance(frame, bool) or not isinstance(frame, int) or frame in seen:
            continue
        seen.add(frame)
        frames.append(frame)
    return tuple(frames)


def compile_frame_authority(global_row: dict[str, Any]) -> dict[str, Any]:
    """Compile the frame-subset card a materialization session must not rediscover."""
    return {
        "layer_judge_frames": list(layer_judge_frames(global_row)),
        "unit_evaluation_judge": ("subset of layer_judge_frames; copy those frames, do not add"),
        "claim_moments": "subset of that unit's judge frames",
        "composition_context.frames": "subset of that unit's judge frames",
        "extra_frame_scene_contracts": (
            "Scene contracts may declare frames outside layer_judge_frames. "
            "Bind those ids through composition_context.contract_ids while keeping "
            "composition_context.frames on the unit judge, or through a required claim "
            "whose moments stay in the unit judge. Do not add those frames to "
            "layer.judge, unit evaluation.judge, composition_context.frames, or "
            "claim.moments."
        ),
        "required_claim_coverage": UNIT_JUDGE_CLAIM_COVERAGE_RULE,
        "look_image_domain": LOOK_REQUIRES_IMAGE_DOMAIN_RULE,
    }
