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

LAYER_JUDGE_CLAIM_COVERAGE_RULE = (
    "a layer whose composed canonical is decided mechanically -- no unit declares "
    "look_capabilities and every required claim is executable_required -- must cover "
    "every LAYER judge frame with some unit's required claim.moments. The layer judge "
    "list is structural and materialization cannot shrink it, so the fix is a required "
    "executable claim reaching the uncovered frames, authored on a unit that judges "
    "them. A layer judge frame no required claim covers is a contract_gap the composed "
    "evaluation refuses by construction, not a critic look vote."
)

LOOK_REQUIRES_IMAGE_DOMAIN_RULE = (
    "a unit that declares look_capabilities must cover every evaluation.judge "
    "frame with a required claim that asserts image and binds image-domain "
    "evidence (image_contract or a harness-minted qualification). Scene counts "
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


def _required_claim_moments(claims: Any) -> set[int]:
    """Frames covered by required claims -- the one definition of "covered"."""
    return {
        int(moment)
        for claim in (claims or ())
        if getattr(claim, "required", False)
        for moment in getattr(claim, "moments", ()) or ()
    }


def uncovered_unit_judge_frames(unit: Any) -> tuple[int, ...]:
    """Judge frames that no required claim covers (HIR-0045)."""
    evaluation = getattr(unit, "evaluation", None)
    judges = tuple(getattr(evaluation, "judges", ()) or ())
    judged = {int(point.frame) for point in judges}
    claimed = _required_claim_moments(getattr(evaluation, "claims", ()) or ())
    return tuple(sorted(judged - claimed))


def _unit_claims(units: Any) -> tuple[Any, ...]:
    return tuple(
        claim
        for unit in units
        for claim in (getattr(getattr(unit, "evaluation", None), "claims", ()) or ())
    )


def composed_evaluation_is_lookless(units: Any) -> bool:
    """True when a layer's composed canonical is decided by unit executable claims.

    This is the condition ``_composition_judge_unit`` uses to decide whether it may
    compile a look-less composed judge at all, and the condition under which the layer
    judge list must be covered by required claims.  One definition, so the gate cannot
    demand coverage of a layer a critic will actually decide (HIR-0238).
    """
    units = tuple(units or ())
    if not units:
        return False
    if any(tuple(getattr(unit, "look_capabilities", ()) or ()) for unit in units):
        return False
    required = [claim for claim in _unit_claims(units) if getattr(claim, "required", False)]
    if not required:
        return False
    return all(getattr(claim, "authority", None) == "executable_required" for claim in required)


def uncovered_layer_judge_frames(judge_frames: Any, units: Any) -> tuple[int, ...]:
    """Layer judge frames no unit's required claim covers (HIR-0238).

    HIR-0045 quantifies over one unit's judge list.  The composed canonical judges the
    LAYER's list against the union of the units' claims, so a layer frame outside every
    unit's judge list is unsatisfiable by construction and no unit-scoped check sees it.
    """
    units = tuple(units or ())
    if not composed_evaluation_is_lookless(units):
        return ()
    covered = _required_claim_moments(_unit_claims(units))
    return tuple(sorted({int(frame) for frame in judge_frames} - covered))


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
        "layer_judge_coverage": LAYER_JUDGE_CLAIM_COVERAGE_RULE,
        "look_image_domain": LOOK_REQUIRES_IMAGE_DOMAIN_RULE,
    }
