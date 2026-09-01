"""Semantic closure of mutable live state against one immutable coordinator head.

Coordinator commits bind the authority generation, not the future lifecycle bytes.
This verifier therefore permits ordinary attempts, completions, and finalizations while
refusing namespace, generation, receipt-lineage, and source substitutions.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from vfx_harness.domain import unit_attempts, unit_completion_receipts
from vfx_harness.domain.authority_capsule_parsing import parse_authority_capsule_set
from vfx_harness.domain.authority_capsules import AuthorityCapsuleSet, LayerAuthorityCapsule
from vfx_harness.domain.authority_head_records import AuthoritySelectionTokenProjection
from vfx_harness.domain.layer_finalizations import (
    LayerFinalizationClaim,
    LayerFinalizationReceipt,
    validate_state_layer_finalization_contracts,
)
from vfx_harness.domain.stop_envelope_primitives import require_digest
from vfx_harness.domain.work_units import UNIT_STATES, WorkUnit
from vfx_harness.orchestration.authority_capsule_resolution import (
    capture_selected_authority_capsules,
)
from vfx_harness.orchestration.authority_receipt_lineage import (
    require_preserved_layer_finalization_authorization,
)
from vfx_harness.orchestration.authority_selection import (
    resolve_selected_authority_from_heads,
)
from vfx_harness.orchestration.authority_selection_heads import (
    read_authority_selection_heads,
)
from vfx_harness.orchestration.authority_state_context import (
    ResolvedAuthorityStateContext,
)
from vfx_harness.orchestration.authority_state_live_members import (
    require_live_state_namespace,
)
from vfx_harness.orchestration.authority_state_store import (
    read_authority_state_record,
)
from vfx_harness.orchestration.layer_finalization_state import (
    terminal_layer_finalization_guard,
)
from vfx_harness.orchestration.unit_completion_state import (
    authorize_completed_units_for_layer,
)
from vfx_harness.orchestration.unit_state_identity import DIGEST_SCHEMA, unit_digest
from vfx_harness.orchestration.unit_state_lock import (
    read_state_file_bytes,
    unit_state_lock,
    unit_state_path,
)
from vfx_harness.orchestration.unit_state_queries import validate_current
from vfx_harness.orchestration.unit_state_serialization import (
    parse_work_unit_state_bytes,
)


class AuthorityStateLiveValidationError(ValueError):
    """Live mutable state is not authorized by the selected coordinator head."""


_REQUIRED_STATE_FIELDS = frozenset(
    {
        "schema",
        "digest_schema",
        "layer",
        "plan_hash",
        "revision",
        "units",
        "superseded",
        "replans",
        "attempt_lineage",
        "updated",
    }
)
_OPTIONAL_STATE_FIELDS = frozenset(
    {
        "falsifications",
        "invalidations",
        "layer_finalization",
    }
)


def _recorded_capsules(
    shot: Path,
    context: ResolvedAuthorityStateContext,
) -> AuthorityCapsuleSet:
    reference = context.proposal.capsule_set_ref
    value, _stored = read_authority_state_record(
        shot,
        locator=reference.locator,
        sha256=reference.sha256,
    )
    capsules = parse_authority_capsule_set(
        value,
        "current authority-state capsule set",
    )
    if capsules.capsule_set_digest != reference.record_digest:
        raise AuthorityStateLiveValidationError(
            "current authority-state capsule set does not match its immutable reference"
        )
    return capsules


def _work_units(
    capsules: AuthorityCapsuleSet,
    layer: LayerAuthorityCapsule,
) -> tuple[WorkUnit, ...]:
    units: list[WorkUnit] = []
    for unit_id, unit_generation_digest in layer.unit_capsule_digests:
        try:
            capsule = capsules.unit(layer.layer_id, unit_id)
            unit = WorkUnit.parse(
                capsule.projection["work_unit"]["row"],
                f"live authority unit {layer.layer_id}.{unit_id}",
            )
        except (KeyError, StopIteration, TypeError, ValueError) as exc:
            raise AuthorityStateLiveValidationError(str(exc)) from exc
        if capsule.capsule_digest != unit_generation_digest:
            raise AuthorityStateLiveValidationError(
                f"live authority unit {layer.layer_id}.{unit_id} changed generation"
            )
        units.append(unit)
    return tuple(units)


def _require_state_shape(
    state: Mapping[str, Any],
    *,
    layer_id: str,
    baseline_revision: int,
) -> None:
    fields = set(state)
    missing = sorted(_REQUIRED_STATE_FIELDS - fields)
    unexpected = sorted(fields - _REQUIRED_STATE_FIELDS - _OPTIONAL_STATE_FIELDS)
    if missing or unexpected:
        raise AuthorityStateLiveValidationError(
            f"live work-unit state for layer {layer_id!r} has unsupported fields; "
            f"missing={missing}; unexpected={unexpected}"
        )
    revision = state.get("revision")
    if (
        state.get("schema") != 1
        or state.get("digest_schema") != DIGEST_SCHEMA
        or state.get("layer") != layer_id
        or not isinstance(revision, int)
        or isinstance(revision, bool)
        or revision != baseline_revision
        or not isinstance(state.get("units"), Mapping)
        or not isinstance(state.get("superseded"), list)
        or not isinstance(state.get("replans"), list)
        or not isinstance(state.get("attempt_lineage"), Mapping)
        or not isinstance(state.get("updated"), str)
        or not state["updated"].strip()
    ):
        raise AuthorityStateLiveValidationError(
            f"live work-unit state for layer {layer_id!r} has an invalid current-schema shape"
        )
    require_digest(state.get("plan_hash"), f"live layer {layer_id} plan_hash")
    for unit_id, slot in state["units"].items():
        if (
            not isinstance(unit_id, str)
            or not unit_id
            or not isinstance(slot, Mapping)
            or slot.get("status") not in UNIT_STATES
            or not isinstance(slot.get("history"), list)
            or not isinstance(slot.get("updated"), str)
            or not slot["updated"].strip()
        ):
            raise AuthorityStateLiveValidationError(
                f"live work-unit state slot {layer_id}.{unit_id} has an invalid shape"
            )
        require_digest(
            slot.get("unit_hash"),
            f"live work-unit state {layer_id}.{unit_id}.unit_hash",
        )


def _require_active_claims_current(
    state: Mapping[str, Any],
    *,
    selection_token: AuthoritySelectionTokenProjection,
) -> None:
    layer_id = str(state["layer"])
    for unit_id, slot in state["units"].items():
        raw = slot.get("active_attempt")
        if raw is None:
            continue
        claim = unit_attempts.UnitAttemptClaim.parse(
            raw,
            f"live active attempt {layer_id}.{unit_id}",
        )
        if claim.selection_token != selection_token:
            raise AuthorityStateLiveValidationError(
                f"live active attempt {layer_id}.{unit_id} belongs to another selection"
            )
    finalization = state.get("layer_finalization")
    raw_claim = (
        finalization.get("active_claim")
        if isinstance(finalization, Mapping)
        else None
    )
    if raw_claim is not None:
        claim = LayerFinalizationClaim.parse(
            raw_claim,
            f"live active layer finalization {layer_id}",
        )
        if claim.selection_token != selection_token:
            raise AuthorityStateLiveValidationError(
                f"live active layer finalization {layer_id} belongs to another selection"
            )


def _terminal_receipt(state: Mapping[str, Any]) -> LayerFinalizationReceipt | None:
    slot = state.get("layer_finalization")
    raw = slot.get("terminal_receipt") if isinstance(slot, Mapping) else None
    if raw is None:
        return None
    return LayerFinalizationReceipt.parse(
        raw,
        f"live terminal layer receipt {state.get('layer')}",
    )


def _require_binding(
    *,
    context: ResolvedAuthorityStateContext,
    image,
    layer: LayerAuthorityCapsule,
    units: tuple[WorkUnit, ...],
    state: Mapping[str, Any],
) -> None:
    binding = image.binding
    expected_units = dict(layer.unit_capsule_digests)
    observed_units = {
        row.unit_id: row.unit_generation_digest for row in binding.units
    }
    expected_predecessors = dict(layer.predecessor_layer_digests)
    observed_predecessors = {
        row.layer_id: row.layer_generation_digest for row in binding.predecessors
    }
    if (
        binding.transition_revision != context.head.revision
        or binding.transition_proposal_digest != context.proposal.digest
        or binding.selection_token != context.head.selection_token
        or binding.layer_id != layer.layer_id
        or binding.layer_generation_digest != layer.capsule_digest
        or observed_units != expected_units
        or observed_predecessors != expected_predecessors
        or state.get("plan_hash") != layer.capsule_digest
        or set(state["units"]) != {unit.id for unit in units}
        or any(
            state["units"][unit.id].get("unit_hash") != unit_digest(unit)
            for unit in units
        )
    ):
        raise AuthorityStateLiveValidationError(
            f"live state for layer {layer.layer_id!r} does not match the immutable "
            "coordinator generation binding"
        )
    live_receipts = {}
    for unit_id, slot in state["units"].items():
        raw = slot.get("completion_receipt")
        if raw is not None:
            live_receipts[unit_id] = unit_completion_receipts.UnitCompletionReceipt.parse(
                raw,
                f"live completion receipt {layer.layer_id}.{unit_id}",
            )
    binding_units = {row.unit_id: row for row in binding.units}
    for unit_id, unit_binding in binding_units.items():
        receipt = live_receipts.get(unit_id)
        preserved_digest = unit_binding.completion_receipt_digest
        if preserved_digest is not None and (
            receipt is None or receipt.receipt_digest != preserved_digest
        ):
            raise AuthorityStateLiveValidationError(
                f"preserved completion receipt {layer.layer_id}.{unit_id} is absent "
                "or differs from the current coordinator binding"
            )
        if (
            receipt is not None
            and preserved_digest is None
            and receipt.claim.selection_token != context.head.selection_token
        ):
            raise AuthorityStateLiveValidationError(
                f"historical completion receipt {layer.layer_id}.{unit_id} is not "
                "preserved by the current coordinator binding"
            )
    terminal = _terminal_receipt(state)
    preserved_terminal_digest = binding.finalization_receipt_digest
    if preserved_terminal_digest is not None and (
        terminal is None or terminal.receipt_digest != preserved_terminal_digest
    ):
        raise AuthorityStateLiveValidationError(
            f"preserved terminal receipt for layer {layer.layer_id!r} is absent or "
            "differs from the current coordinator binding"
        )
    if (
        terminal is not None
        and preserved_terminal_digest is None
        and terminal.claim.selection_token != context.head.selection_token
    ):
        raise AuthorityStateLiveValidationError(
            f"historical terminal receipt for layer {layer.layer_id!r} is not "
            "preserved by the current coordinator binding"
        )


def require_live_authority_state_generation(
    shot_folder: str | Path,
    context: ResolvedAuthorityStateContext,
) -> None:
    """Require the exact live namespace and every current semantic state binding.

    The caller must retain the authority-selection lock.  This function performs no
    model, Blender, network, mutation, or other paid work.
    """

    if not isinstance(context, ResolvedAuthorityStateContext):
        raise AuthorityStateLiveValidationError(
            "live authority-state validation requires a resolved coordinator context"
        )
    shot = Path(shot_folder).expanduser().absolute()
    try:
        heads = read_authority_selection_heads(shot)
        observed_token = AuthoritySelectionTokenProjection(
            heads.token.plan_revision,
            heads.token.plan_pointer_sha256,
            heads.token.jit_revision,
            heads.token.jit_pointer_sha256,
        )
        if observed_token != context.head.selection_token:
            raise AuthorityStateLiveValidationError(
                "live plan/JIT heads differ from the current authority-state head"
            )
        selected = resolve_selected_authority_from_heads(shot, heads)
        captured = capture_selected_authority_capsules(shot, selected)
        recorded = _recorded_capsules(shot, context)
        if (
            captured.capsule_set != recorded
            or context.proposal.capsule_set_ref.record_digest
            != captured.capsule_set.capsule_set_digest
        ):
            raise AuthorityStateLiveValidationError(
                "current coordinator capsules differ from selected semantic authority"
            )
        images = {image.layer_id: image for image in context.commit.installed_states}
        if len(images) != len(context.commit.installed_states):
            raise AuthorityStateLiveValidationError(
                "current coordinator has duplicate installed state bindings"
            )
        expected_ids = tuple(sorted(images))
        with ExitStack() as stack:
            for layer_id in expected_ids:
                stack.enter_context(unit_state_lock(shot, layer_id, exclusive=False))
            require_live_state_namespace(
                shot,
                required_layer_ids=expected_ids,
                where="current authority-state head",
            )
            states: dict[str, dict[str, Any]] = {}
            units_by_layer: dict[str, tuple[WorkUnit, ...]] = {}
            for layer_id in expected_ids:
                image = images[layer_id]
                expected_locator = unit_state_path(shot, layer_id).relative_to(shot).as_posix()
                if image.locator != expected_locator:
                    raise AuthorityStateLiveValidationError(
                        f"current coordinator state locator for layer {layer_id!r} is non-canonical"
                    )
                payload = read_state_file_bytes(shot / image.locator)
                if payload is None:
                    raise AuthorityStateLiveValidationError(
                        f"current coordinator state for layer {layer_id!r} is missing"
                    )
                state = parse_work_unit_state_bytes(
                    payload,
                    f"live work-unit state for layer {layer_id}",
                )
                _require_state_shape(
                    state,
                    layer_id=layer_id,
                    baseline_revision=image.state_revision,
                )
                unit_attempts.validate_state_attempt_contracts(state)
                unit_completion_receipts.validate_state_completion_contracts(state)
                validate_state_layer_finalization_contracts(state)
                try:
                    layer = captured.capsule_set.layer(layer_id)
                except StopIteration as exc:
                    raise AuthorityStateLiveValidationError(
                        f"selected authority has no layer {layer_id!r}"
                    ) from exc
                units = _work_units(captured.capsule_set, layer)
                validate_current(state, layer_id, units)
                _require_binding(
                    context=context,
                    image=image,
                    layer=layer,
                    units=units,
                    state=state,
                )
                _require_active_claims_current(
                    state,
                    selection_token=context.head.selection_token,
                )
                states[layer_id] = state
                units_by_layer[layer_id] = units

            for layer_id in expected_ids:
                state = states[layer_id]
                units = units_by_layer[layer_id]
                authorize_completed_units_for_layer(
                    shot,
                    layer_id,
                    units,
                    expected_plan_hash=str(state["plan_hash"]),
                    selected_authority=selected,
                )
                terminal = _terminal_receipt(state)
                if terminal is None:
                    continue
                lineage = (
                    None
                    if terminal.claim.selection_token == context.head.selection_token
                    else require_preserved_layer_finalization_authorization(
                        shot,
                        terminal,
                        selected,
                    )
                )
                with terminal_layer_finalization_guard(
                    shot,
                    terminal,
                    units,
                    selection_token=selected.selection_token,
                    lineage_authorization=lineage,
                ):
                    pass
            require_live_state_namespace(
                shot,
                required_layer_ids=expected_ids,
                where="current authority-state head after semantic verification",
            )
        captured.require_sources_unchanged()
    except AuthorityStateLiveValidationError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise AuthorityStateLiveValidationError(str(exc)) from exc


__all__ = [
    "AuthorityStateLiveValidationError",
    "require_live_authority_state_generation",
]
