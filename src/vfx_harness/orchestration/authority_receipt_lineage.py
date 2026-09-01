"""Contiguous coordinator authorization for preserved immutable receipts.

A receipt executed under the live selection is authorized through its claim and source
closure. Once authority changes, equality with today's semantic capsule is insufficient.
Every intervening edge must preserve the exact unit binding for a unit receipt; a terminal
layer receipt additionally requires the complete layer binding to remain unchanged. This
module proves either chain without consulting mutable history or accepting an A-to-B-to-A
semantic coincidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from vfx_harness.domain.authority_head_records import (
    AuthoritySelectionTokenProjection,
    parse_authority_selection_token,
)
from vfx_harness.domain.authority_state_records import (
    AuthorityStateRecordRef,
    LayerAuthorityBinding,
)
from vfx_harness.domain.layer_finalizations import LayerFinalizationReceipt
from vfx_harness.domain.unit_completion_receipts import UnitCompletionReceipt
from vfx_harness.domain.work_units import WorkUnit
from vfx_harness.orchestration.authority_capsule_resolution import (
    AuthorityCapsuleResolutionError,
    capture_selected_authority_capsules,
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
    AuthorityStateContextError,
    ResolvedAuthorityStateContext,
    resolve_authority_state_context_from_ref,
    resolve_current_authority_state,
)
from vfx_harness.orchestration.unit_state_identity import unit_digest

if TYPE_CHECKING:
    from vfx_harness.orchestration.authority_selection import (
        ResolvedSelectedAuthority,
    )


class AuthorityReceiptLineageError(ValueError):
    """A historical receipt lacks one contiguous preservation authorization."""


@dataclass(frozen=True, slots=True)
class LayerFinalizationLineageAuthorization:
    """Exact proof consumed by the terminal state/source guard."""

    layer_id: str
    receipt_digest: str
    execution_selection_token: AuthoritySelectionTokenProjection
    current_selection_token: AuthoritySelectionTokenProjection
    current_head_ref: AuthorityStateRecordRef
    layer_generation_digest: str
    completion_receipt_digests: tuple[tuple[str, str], ...]
    traversed_head_digests: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class UnitCompletionLineageAuthorization:
    """Exact coordinator proof for one historical immutable unit receipt."""

    layer_id: str
    unit_id: str
    receipt_digest: str
    unit_digest: str
    execution_selection_token: AuthoritySelectionTokenProjection
    current_selection_token: AuthoritySelectionTokenProjection
    current_head_ref: AuthorityStateRecordRef
    execution_layer_generation_digest: str
    current_layer_generation_digest: str
    unit_generation_digest: str
    traversed_head_digests: tuple[str, ...]


def _projection(
    token: AuthoritySelectionToken,
) -> AuthoritySelectionTokenProjection:
    if not isinstance(token, AuthoritySelectionToken):
        raise AuthorityReceiptLineageError(
            "receipt-lineage authorization requires an exact authority selection token"
        )
    try:
        return parse_authority_selection_token(
            token.to_dict(),
            "receipt-lineage current authority selection token",
        )
    except ValueError as exc:
        raise AuthorityReceiptLineageError(str(exc)) from exc


def _binding(
    context: ResolvedAuthorityStateContext,
    layer_id: str,
) -> LayerAuthorityBinding:
    matches = tuple(
        image.binding
        for image in context.commit.installed_states
        if image.layer_id == layer_id
    )
    if len(matches) != 1:
        raise AuthorityReceiptLineageError(
            f"authority-state head {context.head.revision} has {len(matches)} "
            f"installed bindings for layer {layer_id!r}; expected one"
        )
    binding = matches[0]
    if (
        binding.transition_revision != context.head.revision
        or binding.transition_proposal_digest != context.proposal.digest
        or binding.selection_token != context.head.selection_token
    ):
        raise AuthorityReceiptLineageError(
            f"authority-state head {context.head.revision} has a stale binding for "
            f"layer {layer_id!r}"
        )
    return binding


def _unit_binding(
    binding: LayerAuthorityBinding,
    unit_id: str,
):
    matches = tuple(row for row in binding.units if row.unit_id == unit_id)
    if len(matches) != 1:
        raise AuthorityReceiptLineageError(
            f"authority binding for layer {binding.layer_id!r} has {len(matches)} "
            f"unit bindings for {unit_id!r}; expected one"
        )
    return matches[0]


def _effect(
    context: ResolvedAuthorityStateContext,
    layer_id: str,
):
    matches = tuple(
        row for row in context.proposal.effects if row.layer_id == layer_id
    )
    if len(matches) != 1:
        raise AuthorityReceiptLineageError(
            f"authority-state transition {context.head.revision} has {len(matches)} "
            f"effects for layer {layer_id!r}; expected one"
        )
    return matches[0]


def _completion_digests(
    receipt: LayerFinalizationReceipt,
) -> tuple[tuple[str, str], ...]:
    return tuple(
        sorted(
            (
                row.unit_id,
                row.completion_receipt_digest,
            )
            for row in receipt.claim.unit_inputs
        )
    )


def _binding_completion_digests(
    binding: LayerAuthorityBinding,
) -> tuple[tuple[str, str | None], ...]:
    return tuple(
        (row.unit_id, row.completion_receipt_digest) for row in binding.units
    )


def _require_preserving_edge(
    context: ResolvedAuthorityStateContext,
    *,
    receipt: LayerFinalizationReceipt,
    expected_completion_digests: tuple[tuple[str, str], ...],
) -> None:
    layer_id = receipt.claim.layer_id
    binding = _binding(context, layer_id)
    effect = _effect(context, layer_id)
    if (
        binding.layer_generation_digest != receipt.claim.plan_hash
        or binding.finalization_receipt_digest != receipt.receipt_digest
        or _binding_completion_digests(binding) != expected_completion_digests
        or effect.effect_kind != "unchanged"
        or effect.preserved_finalization_receipt_digest != receipt.receipt_digest
        or effect.preserved_units != binding.units
    ):
        raise AuthorityReceiptLineageError(
            f"authority-state transition {context.head.revision} did not preserve "
            f"the exact terminal receipt and unit closure for layer {layer_id!r}"
        )


def _require_unit_preserving_edge(
    context: ResolvedAuthorityStateContext,
    *,
    receipt: UnitCompletionReceipt,
    unit_generation_digest: str,
) -> None:
    """Require this successor to carry one exact unit receipt from its predecessor."""

    binding = _binding(context, receipt.claim.layer_id)
    current = _unit_binding(binding, receipt.claim.unit_id)
    effect = _effect(context, receipt.claim.layer_id)
    if (
        current.unit_generation_digest != unit_generation_digest
        or current.completion_receipt_digest != receipt.receipt_digest
        or current not in effect.preserved_units
    ):
        raise AuthorityReceiptLineageError(
            f"authority-state transition {context.head.revision} did not preserve "
            f"the exact completion receipt and unit generation for "
            f"{receipt.claim.layer_id}.{receipt.claim.unit_id}"
        )


def _require_unit_origin(
    context: ResolvedAuthorityStateContext,
    receipt: UnitCompletionReceipt,
    *,
    unit_generation_digest: str,
) -> None:
    """Close the immutable claim on the semantic generation where it executed."""

    binding = _binding(context, receipt.claim.layer_id)
    origin = _unit_binding(binding, receipt.claim.unit_id)
    if (
        binding.layer_generation_digest != receipt.claim.plan_hash
        or origin.unit_generation_digest != unit_generation_digest
    ):
        raise AuthorityReceiptLineageError(
            f"receipt execution selection does not bind "
            f"{receipt.claim.layer_id}.{receipt.claim.unit_id} to the claimed "
            "semantic generation"
        )


def _require_origin(
    context: ResolvedAuthorityStateContext,
    receipt: LayerFinalizationReceipt,
) -> None:
    binding = _binding(context, receipt.claim.layer_id)
    if binding.layer_generation_digest != receipt.claim.plan_hash:
        raise AuthorityReceiptLineageError(
            f"receipt execution selection does not bind layer {receipt.claim.layer_id!r} "
            "to the claimed semantic generation"
        )


def require_preserved_layer_finalization_authorization(
    shot_folder: str | Path,
    receipt: LayerFinalizationReceipt,
    selected_authority: ResolvedSelectedAuthority,
) -> LayerFinalizationLineageAuthorization:
    """Prove every transition from receipt execution to the live selected head."""

    shot = Path(shot_folder).expanduser().absolute()
    current_token = selected_authority.selection_token
    current_projection = _projection(current_token)
    execution_projection = receipt.claim.selection_token
    if execution_projection == current_projection:
        raise AuthorityReceiptLineageError(
            "a receipt executed under the live selection does not require lineage adoption"
        )
    try:
        with authority_selection_lock(shot, exclusive=False):
            heads = read_authority_selection_heads(shot)
            require_matching_authority_selection_token(current_token, heads.token)
            current = resolve_current_authority_state(shot)
            assert current is not None
            if current.head.selection_token != current_projection:
                raise AuthorityReceiptLineageError(
                    "authority-state head does not authorize the selected plan/JIT token"
                )
            captured = capture_selected_authority_capsules(shot, selected_authority)
            if (
                current.proposal.capsule_set_ref.record_digest
                != captured.capsule_set.capsule_set_digest
            ):
                raise AuthorityReceiptLineageError(
                    "authority-state head capsule set does not match selected authority"
                )
            try:
                current_layer = captured.capsule_set.layer(receipt.claim.layer_id)
            except StopIteration as exc:
                raise AuthorityReceiptLineageError(
                    f"selected authority has no layer {receipt.claim.layer_id!r}"
                ) from exc
            if current_layer.capsule_digest != receipt.claim.plan_hash:
                raise AuthorityReceiptLineageError(
                    f"selected layer {receipt.claim.layer_id!r} is not the receipt's "
                    "semantic generation"
                )

            expected_completions = _completion_digests(receipt)
            traversed: list[str] = []
            cursor = current
            while cursor.head.selection_token != execution_projection:
                if cursor.head.digest in traversed:
                    raise AuthorityReceiptLineageError(
                        "authority-state receipt lineage contains a coordinator cycle"
                    )
                traversed.append(cursor.head.digest)
                _require_preserving_edge(
                    cursor,
                    receipt=receipt,
                    expected_completion_digests=expected_completions,
                )
                predecessor_ref = cursor.head.predecessor_head_ref
                if predecessor_ref is None:
                    raise AuthorityReceiptLineageError(
                        "receipt execution selection is not an ancestor of the current "
                        "authority-state head"
                    )
                predecessor = resolve_authority_state_context_from_ref(
                    shot,
                    predecessor_ref,
                )
                if (
                    predecessor.head.revision != cursor.head.revision - 1
                    or cursor.proposal.before_selection_token
                    != predecessor.head.selection_token
                ):
                    raise AuthorityReceiptLineageError(
                        "authority-state receipt lineage is not one contiguous predecessor chain"
                    )
                cursor = predecessor

            _require_origin(cursor, receipt)
            captured.require_sources_unchanged()
            return LayerFinalizationLineageAuthorization(
                layer_id=receipt.claim.layer_id,
                receipt_digest=receipt.receipt_digest,
                execution_selection_token=execution_projection,
                current_selection_token=current_projection,
                current_head_ref=current.head_ref,
                layer_generation_digest=current_layer.capsule_digest,
                completion_receipt_digests=expected_completions,
                traversed_head_digests=tuple(traversed),
            )
    except AuthorityReceiptLineageError:
        raise
    except (
        AuthorityCapsuleResolutionError,
        AuthorityStateContextError,
        OSError,
        TypeError,
        ValueError,
    ) as exc:
        raise AuthorityReceiptLineageError(str(exc)) from exc


def require_preserved_unit_completion_authorization(
    shot_folder: str | Path,
    receipt: UnitCompletionReceipt,
    selected_authority: ResolvedSelectedAuthority,
) -> UnitCompletionLineageAuthorization:
    """Prove one exact unit receipt survived every selection since execution.

    A layer capsule may change while one constituent unit capsule remains identical.
    Each successor edge must therefore preserve the exact unit binding; equality of a
    later A-like capsule or a receipt recovered from mutable history is insufficient.
    """

    if not isinstance(receipt, UnitCompletionReceipt):
        raise AuthorityReceiptLineageError(
            "unit receipt-lineage authorization requires a typed completion receipt"
        )
    shot = Path(shot_folder).expanduser().absolute()
    current_token = selected_authority.selection_token
    current_projection = _projection(current_token)
    execution_projection = receipt.claim.selection_token
    if execution_projection == current_projection:
        raise AuthorityReceiptLineageError(
            "a unit receipt executed under the live selection does not require "
            "lineage adoption"
        )
    try:
        with authority_selection_lock(shot, exclusive=False):
            heads = read_authority_selection_heads(shot)
            require_matching_authority_selection_token(current_token, heads.token)
            current = resolve_current_authority_state(shot)
            assert current is not None
            if current.head.selection_token != current_projection:
                raise AuthorityReceiptLineageError(
                    "authority-state head does not authorize the selected plan/JIT token"
                )
            captured = capture_selected_authority_capsules(shot, selected_authority)
            if (
                current.proposal.capsule_set_ref.record_digest
                != captured.capsule_set.capsule_set_digest
            ):
                raise AuthorityReceiptLineageError(
                    "authority-state head capsule set does not match selected authority"
                )
            try:
                current_layer = captured.capsule_set.layer(receipt.claim.layer_id)
                current_unit = captured.capsule_set.unit(
                    receipt.claim.layer_id,
                    receipt.claim.unit_id,
                )
                work_unit = WorkUnit.parse(
                    current_unit.projection["work_unit"]["row"],
                    (
                        "selected authority unit capsule "
                        f"{receipt.claim.layer_id}.{receipt.claim.unit_id}"
                    ),
                )
            except (KeyError, StopIteration) as exc:
                raise AuthorityReceiptLineageError(
                    "selected authority has no exact unit capsule for "
                    f"{receipt.claim.layer_id}.{receipt.claim.unit_id}"
                ) from exc
            if unit_digest(work_unit) != receipt.claim.unit_digest:
                raise AuthorityReceiptLineageError(
                    "selected unit capsule does not match the receipt's immutable "
                    "work-unit digest"
                )
            current_binding = _binding(current, receipt.claim.layer_id)
            current_unit_binding = _unit_binding(
                current_binding,
                receipt.claim.unit_id,
            )
            if (
                current_binding.layer_generation_digest
                != current_layer.capsule_digest
                or current_unit_binding.unit_generation_digest
                != current_unit.capsule_digest
                or current_unit_binding.completion_receipt_digest
                != receipt.receipt_digest
            ):
                raise AuthorityReceiptLineageError(
                    "current authority binding does not authorize the exact selected "
                    f"unit receipt for {receipt.claim.layer_id}.{receipt.claim.unit_id}"
                )

            traversed: list[str] = []
            cursor = current
            while cursor.head.selection_token != execution_projection:
                if cursor.head.digest in traversed:
                    raise AuthorityReceiptLineageError(
                        "authority-state unit receipt lineage contains a coordinator cycle"
                    )
                traversed.append(cursor.head.digest)
                _require_unit_preserving_edge(
                    cursor,
                    receipt=receipt,
                    unit_generation_digest=current_unit.capsule_digest,
                )
                predecessor_ref = cursor.head.predecessor_head_ref
                if predecessor_ref is None:
                    raise AuthorityReceiptLineageError(
                        "unit receipt execution selection is not an ancestor of the "
                        "current authority-state head"
                    )
                predecessor = resolve_authority_state_context_from_ref(
                    shot,
                    predecessor_ref,
                )
                if (
                    predecessor.head.revision != cursor.head.revision - 1
                    or cursor.proposal.before_selection_token
                    != predecessor.head.selection_token
                ):
                    raise AuthorityReceiptLineageError(
                        "authority-state unit receipt lineage is not one contiguous "
                        "predecessor chain"
                    )
                cursor = predecessor

            _require_unit_origin(
                cursor,
                receipt,
                unit_generation_digest=current_unit.capsule_digest,
            )
            captured.require_sources_unchanged()
            return UnitCompletionLineageAuthorization(
                layer_id=receipt.claim.layer_id,
                unit_id=receipt.claim.unit_id,
                receipt_digest=receipt.receipt_digest,
                unit_digest=receipt.claim.unit_digest,
                execution_selection_token=execution_projection,
                current_selection_token=current_projection,
                current_head_ref=current.head_ref,
                execution_layer_generation_digest=receipt.claim.plan_hash,
                current_layer_generation_digest=current_layer.capsule_digest,
                unit_generation_digest=current_unit.capsule_digest,
                traversed_head_digests=tuple(traversed),
            )
    except AuthorityReceiptLineageError:
        raise
    except (
        AuthorityCapsuleResolutionError,
        AuthorityStateContextError,
        OSError,
        TypeError,
        ValueError,
    ) as exc:
        raise AuthorityReceiptLineageError(str(exc)) from exc


__all__ = [
    "AuthorityReceiptLineageError",
    "LayerFinalizationLineageAuthorization",
    "UnitCompletionLineageAuthorization",
    "require_preserved_layer_finalization_authorization",
    "require_preserved_unit_completion_authorization",
]
