"""Typed authorization for same-state mutations derived from a terminal receipt."""

from __future__ import annotations

from dataclasses import dataclass

from vfx_harness.domain.layer_finalizations import LayerFinalizationReceipt
from vfx_harness.orchestration.authority_receipt_lineage import (
    LayerFinalizationLineageAuthorization,
)
from vfx_harness.orchestration.unit_completion_authorizations import (
    AuthorizedUnitCompletionSet,
)


class LayerFinalizationMutationAuthorizationError(ValueError):
    """A terminal receipt cannot authorize a mutation of current unit state."""


@dataclass(frozen=True, slots=True)
class AuthorizedLayerFinalizationMutation:
    """Exact current-head proof for one terminal-receipt state projection.

    The completion attestation proves the current coordinator head and every unit
    receipt embedded by the finalization claim.  A historical terminal receipt also
    carries the contiguous preservation proof from its execution selection to that
    same current head.  Production construction is restricted to the verified
    terminal-finalization factory.
    """

    receipt: LayerFinalizationReceipt
    completion_authorization: AuthorizedUnitCompletionSet
    lineage_authorization: LayerFinalizationLineageAuthorization | None

    def __post_init__(self) -> None:
        receipt = self.receipt
        completions = self.completion_authorization
        lineage = self.lineage_authorization
        if not isinstance(receipt, LayerFinalizationReceipt):
            raise LayerFinalizationMutationAuthorizationError(
                "finalization mutation authorization requires a typed terminal receipt"
            )
        if not isinstance(completions, AuthorizedUnitCompletionSet):
            raise LayerFinalizationMutationAuthorizationError(
                "finalization mutation authorization requires typed unit completions"
            )
        expected_receipts = tuple(
            sorted(
                (row.unit_id, row.completion_receipt_digest)
                for row in receipt.claim.unit_inputs
            )
        )
        if (
            completions.layer_id != receipt.claim.layer_id
            or completions.layer_generation_digest != receipt.claim.plan_hash
            or completions.receipts != expected_receipts
        ):
            raise LayerFinalizationMutationAuthorizationError(
                "finalization mutation unit authorization does not match the exact "
                "terminal claim inputs"
            )
        executed_currently = (
            receipt.claim.selection_token == completions.selection_token
        )
        if executed_currently:
            if lineage is not None:
                raise LayerFinalizationMutationAuthorizationError(
                    "current-selection finalization mutation must not carry historical "
                    "lineage authorization"
                )
            return
        if not isinstance(lineage, LayerFinalizationLineageAuthorization):
            raise LayerFinalizationMutationAuthorizationError(
                "historical finalization mutation requires typed contiguous lineage "
                "authorization"
            )
        if (
            lineage.layer_id != receipt.claim.layer_id
            or lineage.receipt_digest != receipt.receipt_digest
            or lineage.execution_selection_token != receipt.claim.selection_token
            or lineage.current_selection_token != completions.selection_token
            or lineage.current_head_ref != completions.authority_state_head_ref
            or lineage.layer_generation_digest != receipt.claim.plan_hash
            or lineage.completion_receipt_digests != expected_receipts
            or not lineage.traversed_head_digests
        ):
            raise LayerFinalizationMutationAuthorizationError(
                "historical finalization mutation lineage does not preserve the exact "
                "terminal receipt and unit inputs through the current coordinator head"
            )


__all__ = [
    "AuthorizedLayerFinalizationMutation",
    "LayerFinalizationMutationAuthorizationError",
]
