"""Compile and stage context for one exact work-unit attempt."""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from vfx_harness.agents.build_prompts import (
    axis_feedback_groups,
    capability_feedback_groups,
)
from vfx_harness.agents.builder.attempt_guard import UnitAttemptGuard
from vfx_harness.agents.builder.evidence import (
    _fault_owner_options_for_unit,
    _geometry_protected_vis_ids,
    _scene_ids_active_at_declared_frames,
    _unit_scene_evidence_ids,
    image_evidence_required_for,
    look_unsettled_for,
)
from vfx_harness.agents.unit_scope import compile_unit_scope_for_shot
from vfx_harness.domain.brief import Shot
from vfx_harness.domain.image_debts import image_contract_debt_cards
from vfx_harness.domain.work_units import WorkUnit
from vfx_harness.domain.work_units.capabilities import DEFERRED_SUBJECT_BBOX_KINDS
from vfx_harness.evidence.scene_checks import (
    deferred_subject_composition_forecast_ids_for_unit,
    load_rows,
)
from vfx_harness.observability.log import log
from vfx_harness.orchestration.authority_capsule_resolution import (
    selected_layer_capsule_digest,
)
from vfx_harness.orchestration.authority_selection import ResolvedSelectedAuthority
from vfx_harness.orchestration.unit_completion_state import (
    authorize_completed_units_for_layer,
)
from vfx_harness.orchestration.unit_state import load as load_unit_state_for_scope
from vfx_harness.orchestration.unit_state import unit_digest


@dataclass(slots=True)
class UnitBuildContext:
    """Mutable phase state plus the bounded prompt/tool context for one unit."""

    feedback_groups: frozenset[str]
    look_actions: bool
    phase: dict[str, Any]
    scope_card: dict[str, Any] | None


def _unit_artifact_path(layer, unit: WorkUnit) -> str:
    spans = tuple(unit.mutates.script_spans)
    if len(spans) != 1:
        raise ValueError(
            f"layer {layer.id} unit {unit.id} must own exactly one replayable script span; "
            f"got {list(spans)}"
        )
    return spans[0]


def _active_unit_layer_view(layer, unit: WorkUnit):
    """Compile unit-local judgment while retaining the parent dependency DAG."""

    unit_axes = tuple(dict.fromkeys(claim.axis for claim in unit.evaluation.claims))
    unit_judges = tuple((point.frame, point.ref) for point in unit.evaluation.judges)
    return replace(
        layer,
        script=_unit_artifact_path(layer, unit),
        title=(layer.title if len(layer.stages) == 1 else f"{layer.title} · {unit.title}"),
        judges=unit_judges,
        reads=f"Work unit {unit.id}: "
        + " ".join(claim.proposition for claim in unit.evaluation.claims),
        owns=unit_axes,
        primary_judge=unit.evaluation.primary_judge,
        stages=layer.stages,
    )


def compile_unit_build_context(
    shot: Shot,
    milestone,
    layer,
    active_unit: WorkUnit,
    axes,
    *,
    layer_units,
    selected_authority: ResolvedSelectedAuthority,
) -> UnitBuildContext:
    """Compile the exact evidence, look, fault-owner, and predecessor context."""

    layer_id = str(getattr(layer, "id", milestone.id))
    declared_capabilities = tuple(
        getattr(active_unit, "look_capabilities", ()) or ()
    )
    feedback_groups = frozenset(
        capability_feedback_groups(declared_capabilities)
        if active_unit is not None
        else axis_feedback_groups(axes)
    )
    active_evidence_ids = _unit_scene_evidence_ids(active_unit)
    diagnostic_evidence_ids: set[str] = set()
    if active_evidence_ids is not None and layer is not None:
        try:
            extra_vis = _geometry_protected_vis_ids(
                shot,
                layer,
                active_unit,
                selected_authority=selected_authority,
            )
        except (OSError, ValueError, KeyError):
            extra_vis = set()
        active_evidence_ids = set(active_evidence_ids) | extra_vis
        frames = [int(milestone.frame)]
        frames.extend(
            int(frame)
            for frame, _ref in (getattr(layer, "judges", None) or ())
        )
        with contextlib.suppress(
            OSError,
            ValueError,
            KeyError,
            json.JSONDecodeError,
        ):
            active_evidence_ids = _scene_ids_active_at_declared_frames(
                shot,
                layer_id,
                active_evidence_ids,
                frames,
                selected_authority=selected_authority,
            )
        with contextlib.suppress(
            OSError,
            ValueError,
            KeyError,
            json.JSONDecodeError,
        ):
            diagnostic_evidence_ids = set(
                deferred_subject_composition_forecast_ids_for_unit(
                    load_rows(shot.folder, selected_authority),
                    tuple(
                        layer_units or getattr(layer, "stages", ()) or ()
                    ),
                    active_unit,
                    layer_id,
                )
            )
    active_image_evidence_ids = {
        binding.id
        for claim in active_unit.evaluation.claims
        if claim.required
        for binding in claim.evidence
        if binding.kind == "image_contract"
    }
    image_debts = [card.as_dict() for card in image_contract_debt_cards(active_unit)]
    fault_owner_options = _fault_owner_options_for_unit(
        shot,
        layer,
        active_unit,
        selected_authority=selected_authority,
    )
    downstream_framing_ids: list[str] = []
    camera_provider = active_unit is not None and "camera" in (getattr(active_unit, "provides", ()) or ())
    if camera_provider and layer is not None:
        context = getattr(active_unit.evaluation, "composition_context", None)
        bound_context = {str(item) for item in (getattr(context, "contract_ids", ()) or ())}
        downstream_framing_ids = sorted(
            str(row.get("id"))
            for row in load_rows(shot.folder, selected_authority)
            if str(row.get("id")) in bound_context
            and str(row.get("kind") or "") in DEFERRED_SUBJECT_BBOX_KINDS
            and str(row.get("owner_layer") or "") == str(layer_id)
            and str(row.get("activates_at") or "") != str(layer_id)
        )
    phase = {
        "mode": "live",
        "round": 1,
        "camera_provider": camera_provider,
        "downstream_framing_ids": downstream_framing_ids,
        "frame": int(milestone.frame),
        "look_actions": bool(feedback_groups),
        "look_unsettled": look_unsettled_for(
            active_image_evidence_ids,
            declared_capabilities,
        ),
        "active_evidence_ids": active_evidence_ids,
        "diagnostic_evidence_ids": diagnostic_evidence_ids,
        "active_image_evidence_ids": active_image_evidence_ids,
        "image_evidence_required": image_evidence_required_for(
            active_image_evidence_ids,
            declared_capabilities,
        ),
        "image_debts": image_debts,
        "unpaid_image_debts": list(image_debts),
        "unit_id": active_unit.id,
        "unit_hash": unit_digest(active_unit),
        "fault_owner_options": fault_owner_options,
    }
    selected_units = tuple(
        layer_units or getattr(layer, "stages", ()) or ()
    )
    if len(selected_units) <= 1:
        selected_units = selected_units or (active_unit,)
    try:
        durable_state = load_unit_state_for_scope(shot.folder, layer_id)
    except ValueError:
        durable_state = {}
    layer_digest = selected_layer_capsule_digest(
        shot.folder,
        layer_id,
        selected_authority,
    )
    completion_authorization = authorize_completed_units_for_layer(
        shot.folder,
        layer_id,
        selected_units,
        expected_plan_hash=layer_digest,
        selected_authority=selected_authority,
    )
    scope_card = compile_unit_scope_for_shot(
        shot,
        active_unit,
        layer_id,
        units=selected_units,
        durable_state=durable_state,
        completion_authorization=completion_authorization,
        selected_authority=selected_authority,
    )
    scope_card["fault_owner_options"] = fault_owner_options
    return UnitBuildContext(
        feedback_groups=feedback_groups,
        look_actions=bool(feedback_groups),
        phase=phase,
        scope_card=scope_card,
    )


def write_attempt_bound_unit_context(
    shot: Shot,
    layer,
    unit_layer,
    unit: WorkUnit,
    guard: UnitAttemptGuard,
    selected_authority: ResolvedSelectedAuthority,
    *,
    load_milestones: Callable[..., Mapping[str, Any]],
    load_axes: Callable[..., list[tuple[str, str]]],
    write_context: Callable[..., Path],
    clear_context: Callable[[Shot], None],
) -> Path:
    """Generate context unlocked, then refuse or clear any stale result."""

    fingerprints = {}
    try:
        fingerprints = {
            milestone.frame: milestone.fingerprint
            for milestone in load_milestones(shot, selected_authority).values()
            if milestone.fingerprint
        }
    except Exception as exc:
        log(f"! no measured fingerprints in the unit contract: {str(exc)[:60]}", 1)
    guard.check(f"start unit {layer.id}.{unit.id} builder context generation")
    try:
        context_path = write_context(
            shot,
            unit_layer,
            load_axes(shot, selected_authority),
            fingerprints,
            unit=unit,
            layer_units=layer.stages,
            selected_authority=selected_authority,
        )
        guard.check(f"finish unit {layer.id}.{unit.id} builder context generation")
    except BaseException:
        clear_context(shot)
        raise
    return context_path
