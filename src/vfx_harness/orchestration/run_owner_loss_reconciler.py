"""Model-free owner-loss reconciliation for one exact orphaned run (HIR-0172).

A run whose root owner died after publishing ``running`` keeps that status forever
unless a reconciler proves the loss: it acquires the exact recorded owner fence
non-blocking (a held fence means the owner is live and nothing is written), snapshots
the exact running status bytes as prior-status evidence, completes an already prepared
source-valid receipt if one exists, or otherwise captures the current authority and
transcript frontiers and mints one ``owner_lost`` receipt, then publishes evaluation,
summary, inventory, terminal status, and the latest projection through the terminalizer.
PID absence, status age, and transcript silence have no authority here.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from vfx_harness.domain.prior_running_status import (
    PriorRunningStatusEvidence,
    running_status_snapshot_locator,
)
from vfx_harness.domain.run_authority_source_identity import canonical_digest
from vfx_harness.domain.run_authority_source_records import decode_strict_json_object
from vfx_harness.domain.run_interruption_records import (
    INTERRUPTION_OWNER_LOSS_OBSERVATION_LOCATOR,
    INTERRUPTION_RECEIPT_LOCATOR,
    RunInterruptionReceipt,
)
from vfx_harness.domain.run_owner_loss import (
    RUN_OWNER_LOSS_RECONCILER_MANIFEST_LOCATOR,
    RUN_OWNER_LOSS_RECONCILER_MANIFEST_SCHEMA,
    RunOwnerLossObservation,
)
from vfx_harness.domain.run_record_refs import RunRecordRef
from vfx_harness.evaluation import run_interruption as interruption_evaluation
from vfx_harness.observability import run_artifacts
from vfx_harness.observability.run_interruption_archive import (
    RunInterruptionArchiveError,
    RunRecordAbsent,
    publish_run_record,
    read_run_record_bytes,
)
from vfx_harness.observability.run_owner_fence import (
    RunOwnerFenceActive,
    RunOwnerFenceError,
    acquire_run_reconciler_fence,
    read_run_owner_claim,
)
from vfx_harness.orchestration import run_terminalizer

RECONCILIATION_DISPOSITIONS = frozenset({"owner_live", "already_terminal", "completed_prepared", "owner_lost"})


class RunOwnerLossReconciliationError(ValueError):
    """The target run cannot be reconciled from its exact claim and fence."""


@dataclass(frozen=True, slots=True)
class OwnerLossReconciliation:
    """Typed outcome of one reconciliation attempt against one exact run."""

    run_id: str
    disposition: str
    owner_claim_digest: str
    receipt: RunInterruptionReceipt | None
    status_sha256: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "vfx-harness.run-owner-loss-reconciliation/v1",
            "run_id": self.run_id,
            "disposition": self.disposition,
            "owner_claim_digest": self.owner_claim_digest,
            "interruption_receipt_digest": None if self.receipt is None else self.receipt.digest,
            "status_sha256": self.status_sha256,
        }


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _write_create_only(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise RunOwnerLossReconciliationError(f"{path.name} already holds different bytes")
        return
    staging = path.with_name(f".{path.name}.tmp")
    staging.write_bytes(payload)
    staging.replace(path)


def _archive_reconciler_manifest(target_root: Path, reconciler: run_artifacts.RunLayout) -> RunRecordRef:
    """Copy the reconciler's exact manifest bytes into the target run and reference them."""

    payload = reconciler.manifest.read_bytes()
    document, reason = decode_strict_json_object(payload)
    if document is None or document.get("schema") != RUN_OWNER_LOSS_RECONCILER_MANIFEST_SCHEMA:
        raise RunOwnerLossReconciliationError(
            f"reconciler run manifest is not a {RUN_OWNER_LOSS_RECONCILER_MANIFEST_SCHEMA} record ({reason})"
        )
    _write_create_only(target_root / RUN_OWNER_LOSS_RECONCILER_MANIFEST_LOCATOR, payload)
    return RunRecordRef(
        locator=RUN_OWNER_LOSS_RECONCILER_MANIFEST_LOCATOR,
        sha256=_sha256(payload),
        record_schema=RUN_OWNER_LOSS_RECONCILER_MANIFEST_SCHEMA,
        record_digest=canonical_digest(document),
    )


def _already_terminal(shot: Path, run_id: str, claim_digest: str) -> OwnerLossReconciliation | None:
    """Return the already selected receipt when the run is no longer running."""

    run_root = shot / "runs" / run_id
    try:
        payload = read_run_record_bytes(run_root, run_terminalizer.RUN_STATUS_LOCATOR)
    except RunRecordAbsent as exc:
        raise RunOwnerLossReconciliationError(f"run {run_id} has no status to reconcile") from exc
    document, _reason = decode_strict_json_object(payload)
    state = document.get("state") if document is not None else None
    if state == "running":
        return None
    if state == "interrupted":
        verified = interruption_evaluation.read_interrupted_run(
            shot,
            run_id,
            verified_at=_now_iso(),
        )
        return OwnerLossReconciliation(
            run_id=run_id,
            disposition="already_terminal",
            owner_claim_digest=claim_digest,
            receipt=verified.receipt,
            status_sha256=_sha256(payload),
        )
    return OwnerLossReconciliation(
        run_id=run_id,
        disposition="already_terminal",
        owner_claim_digest=claim_digest,
        receipt=None,
        status_sha256=_sha256(payload),
    )


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def reconcile_lost_run(
    shot_root: str | Path,
    run_id: str,
    *,
    reconciler: run_artifacts.RunLayout,
    clock: Callable[[], str],
    supervisor_wait_status: int | None = None,
) -> OwnerLossReconciliation:
    """Reconcile one exact run whose owner may be gone; never write while it is live."""

    shot = Path(shot_root).expanduser().absolute()
    run_root = shot / "runs" / run_id
    if reconciler.run_id == run_id:
        raise RunOwnerLossReconciliationError("a run cannot reconcile itself")
    try:
        claim = read_run_owner_claim(run_root, run_id=run_id)
    except RunOwnerFenceError as exc:
        raise RunOwnerLossReconciliationError(
            f"run {run_id} has no source-verifiable owner claim; a legacy ownerless run is not reconcilable: {exc}"
        ) from exc
    terminal = _already_terminal(shot, run_id, claim.digest)
    if terminal is not None:
        return terminal
    try:
        lease = acquire_run_reconciler_fence(run_root, prior_owner=claim)
    except RunOwnerFenceActive:
        return OwnerLossReconciliation(
            run_id=run_id,
            disposition="owner_live",
            owner_claim_digest=claim.digest,
            receipt=None,
            status_sha256=None,
        )
    except RunOwnerFenceError as exc:
        raise RunOwnerLossReconciliationError(f"reconciler could not acquire the exact owner fence: {exc}") from exc
    with lease:
        acquisition_at = clock()
        running_bytes = run_terminalizer.read_running_status_bytes(run_root, claim)
        prior = PriorRunningStatusEvidence.mint(
            status_bytes=running_bytes,
            owner=claim,
            captured_at=clock(),
        )
        _write_create_only(run_root / running_status_snapshot_locator(prior.status_snapshot_ref.sha256), running_bytes)
        if (run_root / INTERRUPTION_RECEIPT_LOCATOR).exists():
            result = run_terminalizer.complete_prepared_interruption(
                shot,
                run_root,
                lease=lease,
                running_bytes=running_bytes,
                clock=clock,
            )
            return OwnerLossReconciliation(
                run_id=run_id,
                disposition="completed_prepared",
                owner_claim_digest=claim.digest,
                receipt=result.receipt,
                status_sha256=result.status_sha256,
            )
        manifest_ref = _archive_reconciler_manifest(run_root, reconciler)
        loss = RunOwnerLossObservation(
            run_id=run_id,
            prior_owner_digest=claim.digest,
            reconciler_run_id=reconciler.run_id,
            reconciler_manifest_ref=manifest_ref,
            fence_locator=claim.fence_locator,
            fence_device=claim.fence_device,
            fence_inode=claim.fence_inode,
            prior_running_status=prior,
            exclusive_fence_acquired=True,
            exclusive_acquisition_observed_at=acquisition_at,
            supervisor_wait_status=supervisor_wait_status,
        )
        try:
            loss_ref = publish_run_record(run_root, INTERRUPTION_OWNER_LOSS_OBSERVATION_LOCATOR, loss)
        except RunInterruptionArchiveError as exc:
            raise RunOwnerLossReconciliationError(f"owner-loss observation publication failed: {exc}") from exc
        result = run_terminalizer.terminalize_owner_loss(
            shot,
            run_root,
            lease=lease,
            owner_loss=loss,
            owner_loss_ref=loss_ref,
            running_bytes=running_bytes,
            clock=clock,
        )
    return OwnerLossReconciliation(
        run_id=run_id,
        disposition="owner_lost",
        owner_claim_digest=claim.digest,
        receipt=result.receipt,
        status_sha256=result.status_sha256,
    )


def reconciliation_json(result: OwnerLossReconciliation) -> str:
    return json.dumps(result.as_dict(), indent=2, sort_keys=True)


__all__ = [
    "RECONCILIATION_DISPOSITIONS",
    "OwnerLossReconciliation",
    "RunOwnerLossReconciliationError",
    "reconcile_lost_run",
    "reconciliation_json",
]
