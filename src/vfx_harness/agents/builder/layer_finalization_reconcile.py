"""Deterministic post-terminal projection for one finalized layer.

The terminal receipt is the decision.  This module only reconciles its mutable
read models; it never invokes Blender, renders, calls a critic, or derives a new
judgment.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from vfx_harness.agents.builder.authority import AuthorityBoundLedger
from vfx_harness.agents.builder.judgment_payment import (
    reconcile_judgment_payment_attempt_failures,
)
from vfx_harness.agents.builder.layer_finalization_guard import (
    LayerFinalizationReceiptGuard,
)
from vfx_harness.agents.builder.layer_outcome import (
    publish_finalized_layer_outcome,
)
from vfx_harness.domain.brief import Shot
from vfx_harness.domain.layer_finalizations import (
    LayerFinalizationReceipt,
    LayerReplayReceipt,
)
from vfx_harness.evidence.layer_revalidation_projection import (
    reconcile_layer_revalidation_projection,
)
from vfx_harness.orchestration.authority_selection import (
    ResolvedSelectedAuthority,
)
from vfx_harness.orchestration.judgment_debt_state import (
    mark_judgment_debt_due,
    resolve_current_judgment_debt,
)
from vfx_harness.orchestration.layer_finalization_state import (
    authorize_terminal_layer_finalization_mutation,
)
from vfx_harness.orchestration.ledger import Ledger
from vfx_harness.orchestration.unit_state import (
    record_prepared_accepted_hypothesis_falsification,
)


@dataclass(frozen=True, slots=True)
class LayerFinalizationReconciliation:
    """Result of replaying every receipt-carried mutable projection."""

    ledger: Ledger
    finding: dict | None
    revalidation: dict
    outcome: Path


def canonical_from_finalization_receipt(
    receipt: LayerFinalizationReceipt,
) -> list:
    return [
        ((int(row["frame"]), str(row["ref"])), dict(row["verdict"]))
        for row in receipt.canonical
    ]


def _judgment_replay_receipt(
    receipt: LayerFinalizationReceipt,
    decision: Mapping,
) -> LayerReplayReceipt:
    """Resolve the sole sealed group that actually evaluated one debt."""

    matches = tuple(
        binding.receipt
        for binding in receipt.evaluation_receipt.replay_receipts
        if binding.receipt.observation.plan.debt_id == decision.get("debt_id")
        and binding.receipt.observation.plan.definition_digest
        == decision.get("definition_digest")
        and binding.receipt.observation.plan.activation_digest
        == decision.get("activation_digest")
    )
    if len(matches) != 1:
        raise ValueError(
            "terminal judgment-debt projection must bind exactly one sealed "
            "replay group"
        )
    return matches[0]


def _reconcile_finding(
    shot: Shot,
    layer,
    receipt: LayerFinalizationReceipt,
    selected_authority: ResolvedSelectedAuthority,
) -> dict | None:
    finding = receipt.projection["finding"]
    if finding is None:
        return None
    if not isinstance(finding, Mapping):
        raise ValueError("terminal layer finding projection must be an object or null")
    authorization = authorize_terminal_layer_finalization_mutation(
        shot.folder,
        receipt,
        tuple(layer.stages),
        selected_authority,
    )
    return record_prepared_accepted_hypothesis_falsification(
        shot.folder,
        str(layer.id),
        tuple(layer.stages),
        finding,
        selection_token=selected_authority.selection_token,
        required_layer_finalization_receipt_digest=receipt.receipt_digest,
        finalization_authorization=authorization,
    )


def _require_existing_receipt_slot_consistent(
    slot: Mapping,
    expected: Mapping,
    *,
    layer_id: str,
    receipt_digest: str,
) -> None:
    if slot.get("finalization_receipt_digest") != receipt_digest:
        return
    conflicts = sorted(
        key
        for key, value in expected.items()
        if key in slot and slot.get(key) != value
    )
    if conflicts:
        raise ValueError(
            f"layer {layer_id} ledger conflicts with its exact terminal receipt: "
            + ", ".join(conflicts)
        )


def _reconcile_ledger(
    shot: Shot,
    layer,
    receipt: LayerFinalizationReceipt,
    selected_authority: ResolvedSelectedAuthority,
    guard: LayerFinalizationReceiptGuard,
    strips: Mapping,
) -> Ledger:
    ledger = AuthorityBoundLedger(
        shot,
        selected_authority,
        execution_guard=guard,
    )
    milestone = layer.as_milestone(strips)
    slot = ledger._slot(milestone)
    best = dict(receipt.projection["best"])
    ablation = dict(receipt.projection["ablation"])
    expected = {
        "status": receipt.final_status,
        "updated": receipt.completed_at,
        "script": receipt.layer_script_path,
        "script_sha256": receipt.layer_script_sha256,
        "script_sha": receipt.layer_script_sha256[:16],
        "finalization_receipt_digest": receipt.receipt_digest,
        "layer_finalization_claim": receipt.claim.claim_id,
        "best": {key: best.get(key) for key in ("round", "mean", "render")},
        "ablation": {**ablation, "at": receipt.completed_at},
    }
    _require_existing_receipt_slot_consistent(
        slot,
        expected,
        layer_id=str(layer.id),
        receipt_digest=receipt.receipt_digest,
    )
    slot.update(expected)
    ledger.save()
    return ledger


def reconcile_layer_finalization(
    shot: Shot,
    layer,
    receipt: LayerFinalizationReceipt,
    *,
    selected_authority: ResolvedSelectedAuthority,
    strips: Mapping,
) -> LayerFinalizationReconciliation:
    """Idempotently reproduce every projection from one current terminal receipt."""

    if not isinstance(receipt, LayerFinalizationReceipt):
        raise TypeError("layer finalization reconciliation requires a typed receipt")
    if (
        receipt.claim.layer_id != str(layer.id)
        or receipt.layer_script_path != str(layer.script)
    ):
        raise ValueError("terminal finalization receipt belongs to another layer")
    guard = LayerFinalizationReceiptGuard.bind(
        shot.folder,
        receipt,
        tuple(layer.stages),
        selected_authority,
    )
    projection = receipt.projection
    revalidation = guard.publish(
        f"reconcile layer {layer.id} image checks",
        lambda: reconcile_layer_revalidation_projection(
            shot.folder,
            str(layer.id),
            projection["revalidation"],
        ),
    )

    debts = projection["judgment_debts"]
    for debt in debts:
        decision = debt["decision"]
        replay_receipt = _judgment_replay_receipt(receipt, decision)
        guard.publish(
            f"activate judgment debt {decision['debt_id']}",
            lambda decision=decision, replay_receipt=replay_receipt: (
                mark_judgment_debt_due(
                    shot.folder,
                    decision["definition_digest"],
                    layer_id=str(layer.id),
                    replay_receipt=replay_receipt.observation.replay_prefix,
                    selected_authority=selected_authority,
                )
            ),
        )

    finding = _reconcile_finding(
        shot,
        layer,
        receipt,
        selected_authority,
    )

    for debt in debts:
        decision = debt["decision"]
        guard.publish(
            f"publish judgment payment failures for {decision['debt_id']}",
            lambda debt=debt: reconcile_judgment_payment_attempt_failures(
                shot.folder,
                debt["payment_failures"],
            ),
        )
        resolution = debt["resolution"]
        if resolution is None:
            continue
        guard.publish(
            f"resolve judgment debt {decision['debt_id']}",
            lambda decision=decision, resolution=resolution: (
                resolve_current_judgment_debt(
                    shot.folder,
                    decision["definition_digest"],
                    outcome=resolution["outcome"],
                    evidence_digest=resolution["evidence_digest"],
                    selected_authority=selected_authority,
                )
            ),
        )

    canonical = canonical_from_finalization_receipt(receipt)
    outcome = publish_finalized_layer_outcome(
        shot.folder,
        layer,
        best=dict(projection["best"]),
        canonical=canonical,
        blender_version=str(projection["blender_version"]),
        selected_authority=selected_authority,
        finalization_guard=guard,
    )
    ledger = _reconcile_ledger(
        shot,
        layer,
        receipt,
        selected_authority,
        guard,
        strips,
    )
    return LayerFinalizationReconciliation(
        ledger=ledger,
        finding=finding,
        revalidation=revalidation,
        outcome=outcome,
    )


__all__ = [
    "LayerFinalizationReconciliation",
    "canonical_from_finalization_receipt",
    "reconcile_layer_finalization",
]
