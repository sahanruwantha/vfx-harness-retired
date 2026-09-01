"""Prepare immutable HIR-0171 transition intent bytes without selecting them."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from vfx_harness.domain.authority_capsules import AuthorityCapsuleSet
from vfx_harness.domain.authority_head_records import (
    JIT_CURRENT_PATH,
    JIT_VIEW_POINTER_SCHEMA,
    AuthoritySelectionTokenProjection,
)
from vfx_harness.domain.authority_state_records import (
    AuthorityPointerImage,
    AuthorityPointerTransition,
    AuthorityStateMemberImage,
    AuthorityStateMemberTransition,
    AuthorityStateRecordRef,
    AuthorityStateTransitionIntent,
    AuthorityStateTransitionProposal,
    LayerAuthorityBinding,
)
from vfx_harness.domain.stop_envelope_primitives import require_digest
from vfx_harness.orchestration.authority_capsule_resolution import (
    capture_selected_authority_capsules,
)
from vfx_harness.orchestration.authority_layer_finalization_sources import (
    PreparedPreservedLayerFinalizationSources,
    PreservedLayerFinalizationSourceConflict,
    prepare_transition_preserved_finalization_sources,
)
from vfx_harness.orchestration.authority_selection import (
    resolve_selected_authority_from_heads,
)
from vfx_harness.orchestration.authority_selection_heads import (
    AuthoritySelectionHeads,
    read_authority_selection_heads,
)
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionToken,
    authority_selection_lock,
    authority_selection_token_from_pointer_bytes,
    require_matching_authority_selection_token,
)
from vfx_harness.orchestration.authority_state_context import (
    ResolvedAuthorityStateContext,
    install_record_ref,
    resolve_current_authority_state,
)
from vfx_harness.orchestration.authority_state_effects import (
    AuthorityStateEffectsProjection,
    compile_authority_state_effects,
)
from vfx_harness.orchestration.authority_state_live_members import (
    AuthorityStateLiveMemberError,
    authority_state_layer_ids,
)
from vfx_harness.orchestration.authority_state_store import (
    install_authority_state_bytes,
    install_authority_state_record,
)
from vfx_harness.orchestration.authority_unit_completion_sources import (
    PreparedPreservedUnitCompletionSources,
    PreservedUnitCompletionSourceConflict,
    prepare_transition_preserved_unit_sources,
)
from vfx_harness.orchestration.plan_pointer import (
    PLAN_POINTER_PATH,
    PLAN_POINTER_SCHEMA,
)
from vfx_harness.orchestration.unit_state import load_snapshot
from vfx_harness.orchestration.unit_state_lock import (
    unit_state_lock,
    unit_state_path,
)
from vfx_harness.orchestration.unit_state_serialization import (
    serialize_work_unit_state,
)

if TYPE_CHECKING:
    from vfx_harness.orchestration.authority_selection import (
        ResolvedSelectedAuthority,
    )

WORK_UNIT_STATE_SCHEMA = "vfx-harness.work-unit-state/v1"


class AuthorityStatePreparationError(ValueError):
    """An exact transition intent cannot be derived from the current generation."""


@dataclass(frozen=True, slots=True)
class PreparedAuthorityStateTransition:
    """Every immutable byte needed to roll one transition forward."""

    intent: AuthorityStateTransitionIntent
    intent_ref: AuthorityStateRecordRef
    proposal_ref: AuthorityStateRecordRef
    capsule_set: AuthorityCapsuleSet
    capsule_set_ref: AuthorityStateRecordRef
    effects_projection: AuthorityStateEffectsProjection
    preserved_unit_sources: tuple[PreparedPreservedUnitCompletionSources, ...]
    preserved_finalization_sources: tuple[
        PreparedPreservedLayerFinalizationSources, ...
    ]
    after_state_payloads: Mapping[str, bytes]
    after_plan_pointer_bytes: bytes | None
    after_jit_pointer_bytes: bytes | None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _projection(token: AuthoritySelectionToken) -> AuthoritySelectionTokenProjection:
    return AuthoritySelectionTokenProjection(
        token.plan_revision,
        token.plan_pointer_sha256,
        token.jit_revision,
        token.jit_pointer_sha256,
    )


def _ready_layer_ids(capsules: AuthorityCapsuleSet) -> tuple[str, ...]:
    return tuple(
        layer.layer_id for layer in capsules.layers if layer.unit_capsule_digests
    )


def capture_authority_state_snapshot_locked(
    shot: Path,
    after_capsules: AuthorityCapsuleSet,
) -> tuple[dict[str, dict], dict[str, bytes]]:
    """Capture every existing/proposed layer state under sorted state locks."""

    try:
        layer_ids = tuple(
            sorted(
                {*authority_state_layer_ids(shot), *_ready_layer_ids(after_capsules)},
                key=lambda value: str(unit_state_path(shot, value)),
            )
        )
    except AuthorityStateLiveMemberError as exc:
        raise AuthorityStatePreparationError(str(exc)) from exc
    states: dict[str, dict] = {}
    payloads: dict[str, bytes] = {}
    with ExitStack() as stack:
        for layer_id in layer_ids:
            stack.enter_context(unit_state_lock(shot, layer_id, exclusive=True))
        for layer_id in layer_ids:
            state, payload = load_snapshot(shot, layer_id)
            if payload is None:
                continue
            if not state:
                raise AuthorityStatePreparationError(
                    f"present work-unit state for layer {layer_id!r} parsed empty"
                )
            states[layer_id] = state
            payloads[layer_id] = payload
    return states, payloads


def _record_ref_for_bytes(
    shot: Path,
    payload: bytes,
    *,
    record_schema: str,
    record_digest: str,
) -> AuthorityStateRecordRef:
    stored = install_authority_state_bytes(shot, payload)
    return AuthorityStateRecordRef.mint(
        locator=stored.locator,
        sha256=stored.sha256,
        record_schema=record_schema,
        record_digest=require_digest(record_digest, "authority-state staged record digest"),
    )


def _pointer_image(
    shot: Path,
    payload: bytes | None,
    *,
    revision: int,
    record_schema: str,
) -> tuple[AuthorityPointerImage | None, AuthorityStateRecordRef | None]:
    if payload is None:
        if revision != 0:
            raise AuthorityStatePreparationError(
                "absent authority pointer must have revision zero"
            )
        return None, None
    if revision <= 0:
        raise AuthorityStatePreparationError(
            "present authority pointer must have a positive revision"
        )
    digest = hashlib.sha256(payload).hexdigest()
    reference = _record_ref_for_bytes(
        shot,
        payload,
        record_schema=record_schema,
        record_digest=digest,
    )
    return (
        AuthorityPointerImage.mint(
            revision=revision,
            locator=reference.locator,
            sha256=reference.sha256,
        ),
        reference,
    )


def _capsule_ref(
    shot: Path,
    capsules: AuthorityCapsuleSet,
) -> AuthorityStateRecordRef:
    stored = install_authority_state_record(shot, capsules.as_dict())
    return AuthorityStateRecordRef.mint(
        locator=stored.locator,
        sha256=stored.sha256,
        record_schema=capsules.SCHEMA,
        record_digest=capsules.capsule_set_digest,
    )


def _transaction_id(
    *,
    revision: int,
    before: AuthoritySelectionToken,
    after: AuthoritySelectionToken,
    producer_digest: str,
    capsule_digest: str,
) -> str:
    payload = json.dumps(
        {
            "revision": revision,
            "before": before.to_dict(),
            "after": after.to_dict(),
            "producer": producer_digest,
            "capsules": capsule_digest,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return f"ast-{hashlib.sha256(payload).hexdigest()}"


def _prior_bindings(
    context: ResolvedAuthorityStateContext | None,
) -> dict[str, LayerAuthorityBinding]:
    if context is None:
        return {}
    return {
        image.layer_id: image.binding for image in context.commit.installed_states
    }


def prepare_authority_state_transition_locked(
    shot_folder: str | Path,
    *,
    heads: AuthoritySelectionHeads,
    selected_before: ResolvedSelectedAuthority | None,
    after_capsules: AuthorityCapsuleSet,
    after_plan_pointer_bytes: bytes | None,
    after_plan_revision: int,
    after_jit_pointer_bytes: bytes | None,
    after_jit_revision: int,
    producer_payload: bytes,
    producer_schema: str,
    producer_digest: str,
    prepared_at: str | None = None,
) -> PreparedAuthorityStateTransition:
    """Prepare one intent while the caller holds authority-selection exclusive/shared."""

    shot = Path(os.path.abspath(Path(shot_folder).expanduser()))
    prepared_at = prepared_at or _now()
    before = heads.token
    after = authority_selection_token_from_pointer_bytes(
        plan_revision=after_plan_revision,
        plan_pointer_bytes=after_plan_pointer_bytes,
        jit_revision=after_jit_revision,
        jit_pointer_bytes=after_jit_pointer_bytes,
    )
    if before == after:
        raise AuthorityStatePreparationError(
            "authority-state preparation refuses a semantic no-op selection"
        )
    context = resolve_current_authority_state(shot, allow_absent=True)
    if context is None:
        if before.plan_revision != 0 or before.jit_revision != 0:
            raise AuthorityStatePreparationError(
                "selected authority exists without an evaluated authority-state head"
            )
        predecessor_revision = 0
        predecessor_ref = None
        predecessor_digest = None
        before_capsules = None
    else:
        if context.head.selection_token != _projection(before):
            raise AuthorityStatePreparationError(
                "authority-state head does not match the locked predecessor selection"
            )
        if selected_before is None:
            raise AuthorityStatePreparationError(
                "non-genesis transition requires verified predecessor authority"
            )
        captured_before = capture_selected_authority_capsules(shot, selected_before)
        captured_before.require_sources_unchanged()
        before_capsules = captured_before.capsule_set
        predecessor_revision = context.head.revision
        predecessor_ref = context.head_ref
        predecessor_digest = context.head.digest
    states, state_payloads = capture_authority_state_snapshot_locked(
        shot,
        after_capsules,
    )
    effects = compile_authority_state_effects(
        before_capsules=before_capsules,
        after_capsules=after_capsules,
        states=states,
        prior_bindings=_prior_bindings(context),
        predecessor_head_revision=predecessor_revision,
        before_selection_token=_projection(before),
        at=prepared_at,
    )
    after_states = {
        row.layer_id: row.after_state
        for row in effects.layers
        if row.after_state is not None
    }
    try:
        preserved_unit_sources = prepare_transition_preserved_unit_sources(
            shot,
            capsules=after_capsules,
            states=after_states,
            effects=effects.effects,
        )
    except PreservedUnitCompletionSourceConflict as exc:
        raise AuthorityStatePreparationError(
            f"authority-state transition cannot preserve stale completed work: {exc}"
        ) from exc
    try:
        preserved_finalization_sources = (
            prepare_transition_preserved_finalization_sources(
                shot,
                capsules=after_capsules,
                states=after_states,
                effects=effects.effects,
            )
        )
    except PreservedLayerFinalizationSourceConflict as exc:
        raise AuthorityStatePreparationError(
            "authority-state transition cannot preserve a stale terminal layer "
            f"publication: {exc}"
        ) from exc
    producer_ref = _record_ref_for_bytes(
        shot,
        producer_payload,
        record_schema=producer_schema,
        record_digest=producer_digest,
    )
    capsule_ref = _capsule_ref(shot, after_capsules)
    before_plan, _before_plan_ref = _pointer_image(
        shot,
        heads.plan_pointer_bytes,
        revision=before.plan_revision,
        record_schema=PLAN_POINTER_SCHEMA,
    )
    after_plan, after_plan_ref = _pointer_image(
        shot,
        after_plan_pointer_bytes,
        revision=after.plan_revision,
        record_schema=PLAN_POINTER_SCHEMA,
    )
    before_jit, _before_jit_ref = _pointer_image(
        shot,
        heads.jit_pointer_bytes,
        revision=before.jit_revision,
        record_schema=JIT_VIEW_POINTER_SCHEMA,
    )
    after_jit, after_jit_ref = _pointer_image(
        shot,
        after_jit_pointer_bytes,
        revision=after.jit_revision,
        record_schema=JIT_VIEW_POINTER_SCHEMA,
    )
    plan_transition = AuthorityPointerTransition.mint(
        pointer_kind="plan",
        live_locator=PLAN_POINTER_PATH.as_posix(),
        before=before_plan,
        after=after_plan,
    )
    jit_transition = AuthorityPointerTransition.mint(
        pointer_kind="jit",
        live_locator=JIT_CURRENT_PATH.as_posix(),
        before=before_jit,
        after=after_jit,
    )
    revision = predecessor_revision + 1
    proposal = AuthorityStateTransitionProposal.mint(
        transaction_id=_transaction_id(
            revision=revision,
            before=before,
            after=after,
            producer_digest=producer_digest,
            capsule_digest=after_capsules.capsule_set_digest,
        ),
        transition_revision=revision,
        predecessor_head_revision=predecessor_revision,
        predecessor_head_ref=predecessor_ref,
        predecessor_head_digest=predecessor_digest,
        before_selection_token=_projection(before),
        after_selection_token=_projection(after),
        plan_pointer=plan_transition,
        jit_pointer=jit_transition,
        producer_ref=producer_ref,
        capsule_set_ref=capsule_ref,
        effects=effects.effects,
        proposed_at=prepared_at,
    )
    proposal_ref = install_record_ref(shot, proposal)
    after_state_payloads: dict[str, bytes] = {}
    member_transitions: list[AuthorityStateMemberTransition] = []
    staged: dict[str, AuthorityStateRecordRef] = {
        row.locator: row
        for row in (
            producer_ref,
            capsule_ref,
            proposal_ref,
            *((after_plan_ref,) if after_plan_ref is not None else ()),
            *((after_jit_ref,) if after_jit_ref is not None else ()),
        )
    }
    for row, after_binding in effects.bind_after_authority(proposal):
        before_state = None
        if row.before_state is not None:
            before_payload = state_payloads[row.layer_id]
            if row.before_binding is None:
                raise AuthorityStatePreparationError(
                    f"layer {row.layer_id!r} predecessor state lacks a binding"
                )
            before_ref = _record_ref_for_bytes(
                shot,
                before_payload,
                record_schema=WORK_UNIT_STATE_SCHEMA,
                record_digest=hashlib.sha256(before_payload).hexdigest(),
            )
            staged[before_ref.locator] = before_ref
            before_state = AuthorityStateMemberImage.mint(
                layer_id=row.layer_id,
                locator=before_ref.locator,
                sha256=before_ref.sha256,
                state_revision=int(row.before_state["revision"]),
                binding=row.before_binding,
            )
        after_state = None
        if row.after_state is not None:
            if after_binding is None:
                raise AuthorityStatePreparationError(
                    f"layer {row.layer_id!r} successor state lacks a binding"
                )
            after_payload = serialize_work_unit_state(row.after_state)
            after_state_payloads[row.layer_id] = after_payload
            state_ref = _record_ref_for_bytes(
                shot,
                after_payload,
                record_schema=WORK_UNIT_STATE_SCHEMA,
                record_digest=hashlib.sha256(after_payload).hexdigest(),
            )
            staged[state_ref.locator] = state_ref
            after_state = AuthorityStateMemberImage.mint(
                layer_id=row.layer_id,
                locator=state_ref.locator,
                sha256=state_ref.sha256,
                state_revision=int(row.after_state["revision"]),
                binding=after_binding,
            )
        member_transitions.append(
            AuthorityStateMemberTransition.mint(
                layer_id=row.layer_id,
                live_locator=unit_state_path(shot, row.layer_id).relative_to(shot).as_posix(),
                before=before_state,
                after=after_state,
            )
        )
    intent = AuthorityStateTransitionIntent.mint(
        proposal_ref=proposal_ref,
        proposal=proposal,
        state_members=member_transitions,
        staged_members=staged.values(),
        prepared_at=prepared_at,
    )
    intent_ref = install_record_ref(shot, intent)
    return PreparedAuthorityStateTransition(
        intent=intent,
        intent_ref=intent_ref,
        proposal_ref=proposal_ref,
        capsule_set=after_capsules,
        capsule_set_ref=capsule_ref,
        effects_projection=effects,
        preserved_unit_sources=preserved_unit_sources,
        preserved_finalization_sources=preserved_finalization_sources,
        after_state_payloads=MappingProxyType(dict(after_state_payloads)),
        after_plan_pointer_bytes=after_plan_pointer_bytes,
        after_jit_pointer_bytes=after_jit_pointer_bytes,
    )


def prepare_authority_state_transition(
    shot_folder: str | Path,
    *,
    expected_base_selection: AuthoritySelectionToken,
    after_capsules: AuthorityCapsuleSet,
    after_plan_pointer_bytes: bytes | None,
    after_plan_revision: int,
    after_jit_pointer_bytes: bytes | None,
    after_jit_revision: int,
    producer_payload: bytes,
    producer_schema: str,
    producer_digest: str,
    prepared_at: str | None = None,
) -> PreparedAuthorityStateTransition:
    """Prepare against one exact live base while leaving all live members untouched."""

    shot = Path(os.path.abspath(Path(shot_folder).expanduser()))
    with authority_selection_lock(shot, exclusive=False):
        heads = read_authority_selection_heads(shot)
        require_matching_authority_selection_token(expected_base_selection, heads.token)
        selected = (
            None
            if heads.plan is None
            else resolve_selected_authority_from_heads(shot, heads)
        )
        return prepare_authority_state_transition_locked(
            shot,
            heads=heads,
            selected_before=selected,
            after_capsules=after_capsules,
            after_plan_pointer_bytes=after_plan_pointer_bytes,
            after_plan_revision=after_plan_revision,
            after_jit_pointer_bytes=after_jit_pointer_bytes,
            after_jit_revision=after_jit_revision,
            producer_payload=producer_payload,
            producer_schema=producer_schema,
            producer_digest=producer_digest,
            prepared_at=prepared_at,
        )


__all__ = [
    "WORK_UNIT_STATE_SCHEMA",
    "AuthorityStatePreparationError",
    "PreparedAuthorityStateTransition",
    "authority_state_layer_ids",
    "capture_authority_state_snapshot_locked",
    "prepare_authority_state_transition",
    "prepare_authority_state_transition_locked",
]
