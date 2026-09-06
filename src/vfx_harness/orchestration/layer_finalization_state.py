"""Claimed durable state for the public layer-finalization boundary.

Unit completion proves the independently executable constituents.  This module owns
the next boundary: one exact selected generation claims those constituents, publishes
an immutable cumulative replay receipt, and commits one terminal receipt before any
layer outcome or ledger projection is allowed to appear.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from vfx_harness.domain.authority_head_records import (
    AuthoritySelectionTokenProjection,
    parse_authority_selection_token,
)
from vfx_harness.domain.authority_state_records import AuthorityStateRecordRef
from vfx_harness.domain.layer_finalization_releases import (
    LayerFinalizationReleaseReceipt,
    LayerFinalizationReleaseReference,
    layer_finalization_release_claim_evidence,
)
from vfx_harness.domain.layer_finalizations import (
    LAYER_FINALIZATION_CLAIM_ARCHIVE_SCHEMA,
    LayerEvaluationReceipt,
    LayerFinalizationClaim,
    LayerFinalizationPredecessorInput,
    LayerFinalizationReceipt,
    LayerFinalizationUnitInput,
)
from vfx_harness.domain.unit_completion_receipts import UnitCompletionReceipt
from vfx_harness.domain.work_units import WorkUnit, dependency_ordered_units
from vfx_harness.infrastructure.trusted_files import (
    TrustedFileError,
)
from vfx_harness.orchestration import unit_state
from vfx_harness.orchestration.authority_receipt_lineage import (
    LayerFinalizationLineageAuthorization,
    require_preserved_layer_finalization_authorization,
)
from vfx_harness.orchestration.authority_selection import (
    ResolvedSelectedAuthority,
    resolve_selected_authority,
)
from vfx_harness.orchestration.authority_selection_heads import (
    read_authority_selection_heads,
)
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionToken,
    authority_selection_lock,
    require_matching_authority_selection_token,
)
from vfx_harness.orchestration.authority_state_context import (
    resolve_current_authority_state,
)
from vfx_harness.orchestration.layer_finalization_authorizations import (
    AuthorizedLayerFinalizationMutation,
)
from vfx_harness.orchestration.layer_finalization_lifecycle import (
    archive_layer_finalization,
    ensure_finalization_slot,
)
from vfx_harness.orchestration.layer_finalization_predecessors import (
    require_exact_selected_finalization_predecessors,
)
from vfx_harness.orchestration.layer_finalization_release_sources import (
    LayerFinalizationReleaseSourceConflict,
    require_released_claim_history_current,
)
from vfx_harness.orchestration.layer_terminal_sources import (
    CapturedTerminalLayerSources,
    capture_terminal_layer_sources,
    require_terminal_layer_sources_unchanged,
)
from vfx_harness.orchestration.unit_completion_authorizations import (
    AuthorizedUnitCompletionSet,
)
from vfx_harness.orchestration.unit_completion_state import (
    authorize_completed_units_for_layer,
)
from vfx_harness.orchestration.unit_state_lock import unit_state_lock, unit_state_path
from vfx_harness.orchestration.unit_state_storage import now

if TYPE_CHECKING:
    from vfx_harness.orchestration.ledger import Layer


class LayerFinalizationConflict(ValueError):
    """The claimed layer boundary is absent, stale, or internally inconsistent."""



ACTIVE_CLAIM_RECOVERY_RULE = (
    "A live owner may still hold this claim. Only after the builder fence proves no live "
    "owner may an operator release it, and release is a reviewed transaction that archives "
    "the named unsealed claim, preserves every accepted unit receipt, and marks unsealed "
    "judgment output non-reusable (HIR-0170)."
)


def describe_active_claim_conflict(layer_id: object, active: Mapping[str, object]) -> str:
    """Name the claim, its revision and owner, and the transaction that clears it."""
    claim_id = str(active.get("claim_id") or "<unknown>")
    revision = active.get("attempt_revision")
    run_id = active.get("run_id")
    owner = f", minted by run {run_id}" if run_id else ""
    revised = f" (attempt_revision {revision})" if revision is not None else ""
    return (
        f"layer {layer_id} already has an active finalization claim {claim_id}{revised}"
        f"{owner}. {ACTIVE_CLAIM_RECOVERY_RULE} The transaction is: "
        f"vfx finalizations release <shot> --layer {layer_id} --claim-id {claim_id} "
        "--reason <why> --evidence <path>"
    )

def _ordered_units(units: Sequence[WorkUnit]) -> tuple[WorkUnit, ...]:
    try:
        return dependency_ordered_units(tuple(units))
    except (TypeError, ValueError) as exc:
        raise LayerFinalizationConflict(
            f"layer finalization requires a valid dependency-ordered unit DAG: {exc}"
        ) from exc


def _projection(token: AuthoritySelectionToken) -> AuthoritySelectionTokenProjection:
    if not isinstance(token, AuthoritySelectionToken):
        raise LayerFinalizationConflict("layer finalization requires an exact authority selection token")
    try:
        return parse_authority_selection_token(
            token.to_dict(),
            "layer finalization authority selection token",
        )
    except ValueError as exc:
        raise LayerFinalizationConflict(str(exc)) from exc


def _token_from_projection(
    projection: AuthoritySelectionTokenProjection,
) -> AuthoritySelectionToken:
    return AuthoritySelectionToken(
        plan_revision=projection.plan_revision,
        plan_pointer_sha256=projection.plan_pointer_sha256,
        jit_revision=projection.jit_revision,
        jit_pointer_sha256=projection.jit_pointer_sha256,
    )


def _require_claim_selection(
    claim: LayerFinalizationClaim,
    selection_token: AuthoritySelectionToken,
) -> None:
    require_matching_authority_selection_token(
        _token_from_projection(claim.selection_token),
        selection_token,
    )


def _unit_inputs_from_state(
    value: Mapping,
    layer_id: str,
    units: tuple[WorkUnit, ...],
) -> tuple[LayerFinalizationUnitInput, ...]:
    units = _ordered_units(units)
    try:
        unit_state.validate_current(dict(value), layer_id, units)
    except ValueError as exc:
        raise LayerFinalizationConflict(str(exc)) from exc
    rows: list[LayerFinalizationUnitInput] = []
    for unit in units:
        try:
            slot = value["units"][unit.id]
        except (KeyError, TypeError) as exc:
            raise LayerFinalizationConflict(f"layer {layer_id} is missing work-unit state for {unit.id}") from exc
        if not isinstance(slot, Mapping) or slot.get("status") != "passed":
            raise LayerFinalizationConflict(f"layer {layer_id} unit {unit.id} is not a completed executable checkpoint")
        try:
            receipt = UnitCompletionReceipt.parse(
                slot.get("completion_receipt"),
                f"layer {layer_id} unit {unit.id} completion receipt",
            )
        except ValueError as exc:
            raise LayerFinalizationConflict(str(exc)) from exc
        rows.append(
            LayerFinalizationUnitInput.mint(
                unit_id=unit.id,
                unit_digest=unit_state.unit_digest(unit),
                completion_receipt_digest=receipt.receipt_digest,
                script_path=receipt.script_path,
                script_sha256=receipt.script_hash,
            )
        )
    return tuple(rows)


def _require_verified_unit_inputs(
    inputs: Sequence[LayerFinalizationUnitInput],
    verified: AuthorizedUnitCompletionSet,
    *,
    layer_id: str,
) -> None:
    for row in inputs:
        observed = verified.receipt_digest(row.unit_id)
        if observed != row.completion_receipt_digest:
            raise LayerFinalizationConflict(
                f"layer {layer_id} unit {row.unit_id} has no exact source-verified completion receipt"
            )


def _authorized_unit_receipts(
    folder: str | Path,
    layer_id: str,
    units: tuple[WorkUnit, ...],
    *,
    expected_plan_hash: str,
    selection_token: AuthoritySelectionToken,
) -> AuthorizedUnitCompletionSet:
    selected = resolve_selected_authority(folder)
    require_matching_authority_selection_token(
        selection_token,
        selected.selection_token,
    )
    return authorize_completed_units_for_layer(
        folder,
        str(layer_id),
        _ordered_units(units),
        expected_plan_hash=expected_plan_hash,
        selected_authority=selected,
    )


def _require_predecessors_in_state(
    folder: str | Path,
    inputs: Sequence[LayerFinalizationPredecessorInput],
) -> None:
    for row in inputs:
        state = unit_state.load(folder, row.layer_id)
        slot = state.get("layer_finalization") if isinstance(state, Mapping) else None
        raw = slot.get("terminal_receipt") if isinstance(slot, Mapping) else None
        try:
            receipt = LayerFinalizationReceipt.parse(
                raw,
                f"predecessor layer {row.layer_id} terminal receipt",
            )
        except ValueError as exc:
            raise LayerFinalizationConflict(str(exc)) from exc
        if receipt.final_status != "passed":
            raise LayerFinalizationConflict(
                f"predecessor layer {row.layer_id} finalization is {receipt.final_status!r}"
            )
        expected = (
            row.finalization_receipt_digest,
            row.script_path,
            row.script_sha256,
        )
        observed = (
            receipt.receipt_digest,
            receipt.layer_script_path,
            receipt.layer_script_sha256,
        )
        if observed != expected:
            raise LayerFinalizationConflict(f"predecessor layer {row.layer_id} finalization changed")


@contextmanager
def _selected_layer_state_locks(
    folder: str | Path,
    layer_id: str,
    predecessors: Sequence[LayerFinalizationPredecessorInput],
    selection_token: AuthoritySelectionToken,
    *,
    current_exclusive: bool,
    authority_state_head_ref: AuthorityStateRecordRef | None = None,
) -> Iterator[None]:
    """Acquire selection first, then every involved state path canonically."""

    _projection(selection_token)
    ids = {str(layer_id), *(row.layer_id for row in predecessors)}
    with authority_selection_lock(folder, exclusive=False):
        observed = read_authority_selection_heads(folder).token
        require_matching_authority_selection_token(selection_token, observed)
        if authority_state_head_ref is not None:
            context = resolve_current_authority_state(folder)
            if context is None or context.head_ref != authority_state_head_ref:
                raise LayerFinalizationConflict(
                    "authority-state receipt-lineage authorization changed before use"
                )
        with ExitStack() as stack:
            for current in sorted(ids, key=lambda value: str(unit_state_path(folder, value))):
                stack.enter_context(
                    unit_state_lock(
                        folder,
                        current,
                        exclusive=current_exclusive and current == str(layer_id),
                    )
                )
            yield


def claim_layer_finalization(
    folder: str | Path,
    layer: Layer,
    *,
    expected_plan_hash: str,
    run_id: str,
    layer_script_sha256: str,
    predecessor_inputs: Sequence[LayerFinalizationPredecessorInput],
    selection_token: AuthoritySelectionToken,
) -> LayerFinalizationClaim:
    """Atomically claim one complete current layer before composition spend."""

    units = _ordered_units(tuple(layer.stages))
    mode = "singleton_passthrough" if len(units) == 1 else "multi_unit_fan_in"
    predecessors = tuple(predecessor_inputs)
    require_exact_selected_finalization_predecessors(
        folder, str(layer.id), predecessors, selection_token=selection_token
    )
    verified = _authorized_unit_receipts(
        folder,
        str(layer.id),
        units,
        expected_plan_hash=expected_plan_hash,
        selection_token=selection_token,
    )
    with _selected_layer_state_locks(
        folder,
        str(layer.id),
        predecessors,
        selection_token,
        current_exclusive=True,
        authority_state_head_ref=verified.authority_state_head_ref,
    ):
        value = unit_state.load(folder, str(layer.id))
        if not value:
            raise LayerFinalizationConflict("work-unit state is not initialized")
        if value.get("plan_hash") != expected_plan_hash:
            raise LayerFinalizationConflict("layer finalization plan identity changed before claim")
        unit_state.authorized_passed_unit_ids(
            value,
            units,
            completion_authorization=verified,
        )
        inputs = _unit_inputs_from_state(value, str(layer.id), units)
        _require_verified_unit_inputs(inputs, verified, layer_id=str(layer.id))
        _require_predecessors_in_state(folder, predecessors)
        slot = ensure_finalization_slot(value)
        try:
            require_released_claim_history_current(
                folder,
                slot["claim_history"],
            )
        except LayerFinalizationReleaseSourceConflict as exc:
            raise LayerFinalizationConflict(str(exc)) from exc
        active = slot.get("active_claim")
        if active is not None:
            # The refusal holds the claim it is refusing on. Naming only the condition
            # made an operator grep state/work-units/layer_<id>.json for the id this
            # frame already has, and the message reaches summary.json through the
            # envelope's `detail`, so it is the one surface they read (HIR-0242).
            raise LayerFinalizationConflict(describe_active_claim_conflict(layer.id, active))
        if slot.get("terminal_receipt") is not None:
            raise LayerFinalizationConflict(f"layer {layer.id} already has a terminal finalization receipt")
        revision = int(slot.get("attempt_revision") or 0) + 1
        claim = LayerFinalizationClaim.mint(
            attempt_revision=revision,
            run_id=run_id,
            layer_id=str(layer.id),
            mode=mode,
            selection_token=_projection(selection_token),
            plan_hash=expected_plan_hash,
            layer_script_path=str(layer.script),
            layer_script_sha256=layer_script_sha256,
            unit_inputs=inputs,
            predecessor_inputs=predecessors,
            claimed_at=now(),
        )
        slot["attempt_revision"] = revision
        slot["active_claim"] = claim.as_dict()
        value["updated"] = now()
        unit_state._write(unit_state_path(folder, str(layer.id)), value)
        return claim


def require_active_layer_finalization_claim(
    value: Mapping,
    claim: LayerFinalizationClaim,
    units: tuple[WorkUnit, ...],
) -> LayerFinalizationClaim:
    """Validate one active claim against a caller-held layer-state lock."""

    units = _ordered_units(units)
    try:
        current = LayerFinalizationClaim.parse(
            (value.get("layer_finalization") or {}).get("active_claim"),
            f"layer {claim.layer_id} active finalization claim",
        )
    except (AttributeError, ValueError) as exc:
        raise LayerFinalizationConflict(str(exc)) from exc
    if current != claim:
        raise LayerFinalizationConflict(
            f"layer {claim.layer_id} finalization claim changed; expected={claim.claim_id}, current={current.claim_id}"
        )
    if value.get("plan_hash") != claim.plan_hash:
        raise LayerFinalizationConflict("layer finalization plan identity changed")
    inputs = _unit_inputs_from_state(value, claim.layer_id, units)
    if inputs != claim.unit_inputs:
        raise LayerFinalizationConflict("layer finalization unit input closure changed")
    return current


def release_layer_finalization_claim(
    folder: str | Path,
    receipt: LayerFinalizationReleaseReceipt,
    reference: LayerFinalizationReleaseReference,
    units: tuple[WorkUnit, ...],
    *,
    selected_authority: ResolvedSelectedAuthority,
) -> LayerFinalizationClaim:
    """Archive one exact active claim while preserving all completed unit state."""

    typed_inputs = (
        isinstance(receipt, LayerFinalizationReleaseReceipt),
        isinstance(reference, LayerFinalizationReleaseReference),
    )
    if not all(typed_inputs):
        raise LayerFinalizationConflict(
            "reviewed release requires a typed receipt and content reference"
        )
    if reference.receipt_digest != receipt.receipt_digest:
        raise LayerFinalizationConflict(
            "reviewed release reference does not name the exact release receipt"
        )
    claim = receipt.request.claim
    units = _ordered_units(units)
    token = selected_authority.selection_token
    _require_claim_selection(claim, token)
    verified = authorize_completed_units_for_layer(
        folder,
        claim.layer_id,
        units,
        expected_plan_hash=claim.plan_hash,
        selected_authority=selected_authority,
    )
    if verified.selection_token != claim.selection_token:
        raise LayerFinalizationConflict(
            "reviewed release completion authorization belongs to another selection"
        )
    with _selected_layer_state_locks(
        folder,
        claim.layer_id,
        claim.predecessor_inputs,
        token,
        current_exclusive=True,
        authority_state_head_ref=verified.authority_state_head_ref,
    ):
        value = unit_state.load(folder, claim.layer_id)
        if not value:
            raise LayerFinalizationConflict("work-unit state is not initialized")
        passed = unit_state.authorized_passed_unit_ids(
            value, units, completion_authorization=verified
        )
        if passed != {unit.id for unit in units}:
            raise LayerFinalizationConflict(
                "reviewed release requires every claimed unit completion to remain current"
            )
        require_active_layer_finalization_claim(value, claim, units)
        _require_verified_unit_inputs(
            claim.unit_inputs, verified, layer_id=claim.layer_id
        )
        _require_predecessors_in_state(folder, claim.predecessor_inputs)
        slot = ensure_finalization_slot(value)
        if slot.get("terminal_receipt") is not None:
            raise LayerFinalizationConflict(
                "reviewed release refuses a terminal layer finalization"
            )
        slot["claim_history"].append(
            {
                "schema": LAYER_FINALIZATION_CLAIM_ARCHIVE_SCHEMA,
                "claim": claim.as_dict(),
                "disposition": "released",
                "reason": receipt.request.reason,
                "evidence": list(
                    layer_finalization_release_claim_evidence(receipt, reference)
                ),
                "at": receipt.released_at,
            }
        )
        slot["active_claim"] = None
        value["updated"] = receipt.released_at
        unit_state._write(unit_state_path(folder, claim.layer_id), value)
        return claim


@contextmanager
def active_layer_finalization_guard(
    folder: str | Path,
    claim: LayerFinalizationClaim,
    units: tuple[WorkUnit, ...],
    *,
    selection_token: AuthoritySelectionToken,
) -> Iterator[LayerFinalizationClaim]:
    """Hold the exact claim current for one bounded execution/publication step."""

    units = _ordered_units(units)
    _require_claim_selection(claim, selection_token)
    require_exact_selected_finalization_predecessors(
        folder, claim.layer_id, claim.predecessor_inputs, selection_token=selection_token
    )
    verified = _authorized_unit_receipts(
        folder,
        claim.layer_id,
        units,
        expected_plan_hash=claim.plan_hash,
        selection_token=selection_token,
    )
    _require_verified_unit_inputs(
        claim.unit_inputs,
        verified,
        layer_id=claim.layer_id,
    )
    with _selected_layer_state_locks(
        folder,
        claim.layer_id,
        claim.predecessor_inputs,
        selection_token,
        current_exclusive=False,
        authority_state_head_ref=verified.authority_state_head_ref,
    ):
        value = unit_state.load(folder, claim.layer_id)
        unit_state.authorized_passed_unit_ids(
            value,
            units,
            completion_authorization=verified,
        )
        current = require_active_layer_finalization_claim(value, claim, units)
        _require_predecessors_in_state(folder, claim.predecessor_inputs)
        yield current


def complete_layer_finalization(
    folder: str | Path,
    receipt: LayerFinalizationReceipt,
    evaluation_receipt: LayerEvaluationReceipt,
    units: tuple[WorkUnit, ...],
    *,
    selection_token: AuthoritySelectionToken,
) -> LayerFinalizationReceipt:
    """Commit terminal authority before any mutable layer-level projection."""

    units = _ordered_units(units)
    receipt.assert_matches_evaluation(evaluation_receipt)
    claim = receipt.claim
    _require_claim_selection(claim, selection_token)
    require_exact_selected_finalization_predecessors(
        folder, claim.layer_id, claim.predecessor_inputs, selection_token=selection_token
    )
    try:
        terminal_sources = capture_terminal_layer_sources(
            folder,
            receipt,
        )
        if terminal_sources.stored_evaluation.receipt != evaluation_receipt:
            raise LayerFinalizationConflict(
                "terminal layer finalization evaluation receipt bytes disagree with input"
            )
    except (OSError, TrustedFileError, ValueError) as exc:
        raise LayerFinalizationConflict(str(exc)) from exc
    verified = _authorized_unit_receipts(
        folder,
        claim.layer_id,
        units,
        expected_plan_hash=claim.plan_hash,
        selection_token=selection_token,
    )
    _require_verified_unit_inputs(
        claim.unit_inputs,
        verified,
        layer_id=claim.layer_id,
    )
    with _selected_layer_state_locks(
        folder,
        claim.layer_id,
        claim.predecessor_inputs,
        selection_token,
        current_exclusive=True,
        authority_state_head_ref=verified.authority_state_head_ref,
    ):
        value = unit_state.load(folder, claim.layer_id)
        unit_state.authorized_passed_unit_ids(
            value,
            units,
            completion_authorization=verified,
        )
        require_active_layer_finalization_claim(value, claim, units)
        _require_predecessors_in_state(folder, claim.predecessor_inputs)
        try:
            require_terminal_layer_sources_unchanged(terminal_sources)
        except TrustedFileError as exc:
            raise LayerFinalizationConflict(str(exc)) from exc
        slot = ensure_finalization_slot(value)
        at = now()
        slot["claim_history"].append(
            {
                "schema": "vfx-harness.layer-finalization-claim-archive/v1",
                "claim": claim.as_dict(),
                "disposition": "completed",
                "reason": "terminal layer finalization receipt committed",
                "evidence": [
                    *(
                        "layer-replay-receipt:"
                        f"{binding.receipt.receipt_digest}"
                        for binding in evaluation_receipt.replay_receipts
                    ),
                    f"layer-evaluation-receipt:{evaluation_receipt.receipt_digest}",
                    f"layer-finalization-receipt:{receipt.receipt_digest}",
                ],
                "at": at,
            }
        )
        slot["active_claim"] = None
        slot["terminal_receipt"] = receipt.as_dict()
        value["updated"] = at
        unit_state._write(unit_state_path(folder, claim.layer_id), value)
        return receipt


def current_layer_finalization_receipt(
    folder: str | Path,
    layer_id: str,
) -> LayerFinalizationReceipt | None:
    """Return the structurally current terminal receipt, or ``None`` when unfinalized."""

    value = unit_state.load(folder, str(layer_id))
    slot = value.get("layer_finalization") if isinstance(value, Mapping) else None
    raw = slot.get("terminal_receipt") if isinstance(slot, Mapping) else None
    if raw is None:
        return None
    try:
        return LayerFinalizationReceipt.parse(
            raw,
            f"layer {layer_id} terminal finalization receipt",
        )
    except ValueError as exc:
        raise LayerFinalizationConflict(str(exc)) from exc


def require_terminal_layer_finalization_receipt(
    value: Mapping,
    receipt: LayerFinalizationReceipt,
    units: tuple[WorkUnit, ...],
) -> LayerFinalizationReceipt:
    """Validate exact terminal authority against a caller-held state lock."""

    units = _ordered_units(units)
    try:
        current = LayerFinalizationReceipt.parse(
            (value.get("layer_finalization") or {}).get("terminal_receipt"),
            f"layer {receipt.claim.layer_id} terminal finalization receipt",
        )
    except (AttributeError, ValueError) as exc:
        raise LayerFinalizationConflict(str(exc)) from exc
    if current != receipt:
        raise LayerFinalizationConflict(
            f"layer {receipt.claim.layer_id} terminal receipt changed; "
            f"expected={receipt.receipt_digest}, current={current.receipt_digest}"
        )
    if value.get("plan_hash") != receipt.claim.plan_hash:
        raise LayerFinalizationConflict("terminal layer finalization plan identity changed")
    inputs = _unit_inputs_from_state(value, receipt.claim.layer_id, units)
    if inputs != receipt.claim.unit_inputs:
        raise LayerFinalizationConflict("terminal layer finalization unit input closure changed")
    return current


@contextmanager
def terminal_layer_finalization_guard(
    folder: str | Path,
    receipt: LayerFinalizationReceipt,
    units: tuple[WorkUnit, ...],
    *,
    selection_token: AuthoritySelectionToken,
    lineage_authorization: LayerFinalizationLineageAuthorization | None = None,
) -> Iterator[LayerFinalizationReceipt]:
    """Retain one terminal receipt and every external causal byte through use."""

    units = _ordered_units(units)
    claim = receipt.claim
    current_projection = _projection(selection_token)
    require_exact_selected_finalization_predecessors(
        folder, claim.layer_id, claim.predecessor_inputs, selection_token=selection_token
    )
    if claim.selection_token == current_projection:
        if lineage_authorization is not None:
            raise LayerFinalizationConflict(
                "current-selection terminal receipt must not carry lineage adoption"
            )
        authority_state_head_ref = None
    else:
        expected_completions = tuple(
            sorted(
                (row.unit_id, row.completion_receipt_digest)
                for row in claim.unit_inputs
            )
        )
        authorization = lineage_authorization
        if (
            authorization is None
            or authorization.layer_id != claim.layer_id
            or authorization.receipt_digest != receipt.receipt_digest
            or authorization.execution_selection_token != claim.selection_token
            or authorization.current_selection_token != current_projection
            or authorization.layer_generation_digest != claim.plan_hash
            or authorization.completion_receipt_digests != expected_completions
            or not authorization.traversed_head_digests
        ):
            raise LayerFinalizationConflict(
                "historical terminal receipt lacks exact contiguous authority-state "
                "lineage authorization"
            )
        authority_state_head_ref = authorization.current_head_ref
    try:
        terminal_sources = capture_terminal_layer_sources(
            folder,
            receipt,
        )
    except (OSError, TrustedFileError, ValueError) as exc:
        raise LayerFinalizationConflict(str(exc)) from exc
    verified = _authorized_unit_receipts(
        folder,
        claim.layer_id,
        units,
        expected_plan_hash=receipt.claim.plan_hash,
        selection_token=selection_token,
    )
    _require_verified_unit_inputs(
        claim.unit_inputs,
        verified,
        layer_id=claim.layer_id,
    )
    if authority_state_head_ref is None:
        authority_state_head_ref = verified.authority_state_head_ref
    elif verified.authority_state_head_ref != authority_state_head_ref:
        raise LayerFinalizationConflict(
            "terminal layer and unit receipt authorizations name different current heads"
        )
    with _selected_layer_state_locks(
        folder,
        claim.layer_id,
        claim.predecessor_inputs,
        selection_token,
        current_exclusive=False,
        authority_state_head_ref=authority_state_head_ref,
    ):
        value = unit_state.load(folder, claim.layer_id)
        unit_state.authorized_passed_unit_ids(
            value,
            units,
            completion_authorization=verified,
        )
        current = require_terminal_layer_finalization_receipt(
            value,
            receipt,
            units,
        )
        _require_predecessors_in_state(folder, claim.predecessor_inputs)
        try:
            require_terminal_layer_sources_unchanged(terminal_sources)
        except TrustedFileError as exc:
            raise LayerFinalizationConflict(str(exc)) from exc
        yield current
    try:
        require_terminal_layer_sources_unchanged(terminal_sources)
    except TrustedFileError as exc:
        raise LayerFinalizationConflict(str(exc)) from exc


def authorize_terminal_layer_finalization_mutation(
    folder: str | Path,
    receipt: LayerFinalizationReceipt,
    units: tuple[WorkUnit, ...],
    selected_authority: ResolvedSelectedAuthority,
) -> AuthorizedLayerFinalizationMutation:
    """Authorize one atomic unit-state projection from a terminal receipt.

    Its mutation boundary rechecks the exact current selection, coordinator head,
    terminal receipt, and unit-completion projection under a state-exclusive lock.
    """

    units = _ordered_units(units)
    current_projection = _projection(selected_authority.selection_token)
    lineage = (
        None
        if receipt.claim.selection_token == current_projection
        else require_preserved_layer_finalization_authorization(
            folder,
            receipt,
            selected_authority,
        )
    )
    verified = _authorized_unit_receipts(
        folder,
        receipt.claim.layer_id,
        units,
        expected_plan_hash=receipt.claim.plan_hash,
        selection_token=selected_authority.selection_token,
    )
    authorization = AuthorizedLayerFinalizationMutation(
        receipt=receipt,
        completion_authorization=verified,
        lineage_authorization=lineage,
    )
    with terminal_layer_finalization_guard(
        folder,
        receipt,
        units,
        selection_token=selected_authority.selection_token,
        lineage_authorization=lineage,
    ):
        return authorization


__all__ = [
    "CapturedTerminalLayerSources",
    "LayerFinalizationConflict",
    "active_layer_finalization_guard",
    "archive_layer_finalization",
    "authorize_terminal_layer_finalization_mutation",
    "capture_terminal_layer_sources",
    "claim_layer_finalization",
    "complete_layer_finalization",
    "current_layer_finalization_receipt",
    "ensure_finalization_slot",
    "release_layer_finalization_claim",
    "require_active_layer_finalization_claim",
    "require_terminal_layer_finalization_receipt",
    "require_terminal_layer_sources_unchanged",
    "terminal_layer_finalization_guard",
]
