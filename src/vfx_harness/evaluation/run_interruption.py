"""Independent archive-reopening evaluation of one interruption receipt (HIR-0172).

The evaluator derives ``satisfied`` or ``failed`` solely by reopening the target run's
own records and content-addressed archive: the receipt, owner claim and manifest,
authority observation, transcript frontiers, archive manifest, every archived source
byte stream, and any owner-loss evidence.  It never reads the live shot tree, so a
later valid authority publication cannot falsify a committed interruption, and it
mutates nothing.  A run without a readable receipt has no evaluation at all.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vfx_harness.domain.run_authority_source_identity import canonical_digest
from vfx_harness.domain.run_authority_source_records import (
    classify_authority_source,
    decode_strict_json_object,
)
from vfx_harness.domain.run_interruption_archive import (
    InterruptionArchiveManifest,
    iter_authority_sources,
)
from vfx_harness.domain.run_interruption_records import (
    INTERRUPTION_RECEIPT_LOCATOR,
    InterruptionAuthorityObservation,
    InterruptionTranscriptFrontier,
    RunInterruptionReceipt,
    derive_transcript_frontier,
)
from vfx_harness.domain.run_owner_claims import RunOwnerClaim
from vfx_harness.domain.run_owner_loss import (
    RUN_OWNER_LOSS_RECONCILER_MANIFEST_SCHEMA,
    RunOwnerLossObservation,
)
from vfx_harness.domain.run_record_refs import RunRecordRef
from vfx_harness.domain.run_status import (
    INTERRUPTION_RECEIPT_EVALUATION_LOCATOR,
    InterruptionReceiptEvaluation,
    RunStatusV2,
    interruption_evaluation_receipt_binding,
)
from vfx_harness.observability.run_interruption_archive import (
    ArchiveObjectMismatch,
    RunInterruptionArchiveError,
    RunRecordAbsent,
    read_archive_object,
    read_run_record_bytes,
)
from vfx_harness.observability.run_owner_fence import (
    RunOwnerFenceError,
    read_run_owner_claim,
)


class InterruptionEvaluationUnavailable(ValueError):
    """No exact receipt exists to bind, so no evaluation can be produced."""


class InterruptionAuthorityUnavailable(ValueError):
    """The run's interrupted status cannot be source-verified and authorizes nothing."""


RUN_STATUS_LOCATOR = "status.json"


@dataclass(frozen=True, slots=True)
class VerifiedInterruptedRun:
    """One interrupted terminal status whose closure was re-verified from the archive."""

    status: RunStatusV2
    receipt: RunInterruptionReceipt
    evaluation: InterruptionReceiptEvaluation
    verification: InterruptionReceiptEvaluation

    @property
    def legal_transactions(self) -> tuple[()]:
        return ()

    @property
    def retryable(self) -> bool:
        return False

    @property
    def resume_authorized(self) -> bool:
        return False

    @property
    def dispatch_authority(self) -> bool:
        return False


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class _Reopener:
    def __init__(self, run_root: Path, receipt: RunInterruptionReceipt) -> None:
        self.run_root = run_root
        self.receipt = receipt
        self.issues: set[str] = set()

    def verify_record_ref(
        self,
        reference: RunRecordRef,
        *,
        expected: Any,
        parser: Callable[[dict[str, Any]], Any],
        unreadable: str,
        mismatch: str,
        ref_issue: str,
    ) -> None:
        """Reopen one referenced run record and require exact bytes and content."""

        try:
            payload = read_run_record_bytes(self.run_root, reference.locator)
        except RunInterruptionArchiveError:
            self.issues.add(unreadable)
            return
        if _sha256(payload) != reference.sha256:
            self.issues.add(ref_issue)
        document, _reason = decode_strict_json_object(payload)
        if document is None:
            self.issues.add(mismatch)
            return
        try:
            parsed = parser(document)
        except ValueError:
            self.issues.add(mismatch)
            return
        if parsed != expected:
            self.issues.add(mismatch)
        if (reference.record_schema, reference.record_digest) != (parsed.SCHEMA, parsed.digest):
            self.issues.add(ref_issue)

    def verify_owner(self) -> None:
        receipt = self.receipt
        try:
            claim = read_run_owner_claim(
                self.run_root,
                run_id=receipt.run_id,
                expected_digest=receipt.owner.digest,
            )
        except RunOwnerFenceError:
            self.issues.add("owner_claim_unverified")
        else:
            if claim != receipt.owner:
                self.issues.add("owner_claim_mismatch")
        self.verify_record_ref(
            receipt.owner_ref,
            expected=receipt.owner,
            parser=lambda document: RunOwnerClaim.from_dict(document, "run owner claim"),
            unreadable="owner_claim_unverified",
            mismatch="owner_claim_mismatch",
            ref_issue="owner_ref_mismatch",
        )

    def verify_authority(self) -> None:
        receipt = self.receipt
        self.verify_record_ref(
            receipt.authority_ref,
            expected=receipt.authority,
            parser=lambda document: InterruptionAuthorityObservation.from_dict(
                document,
                "interruption authority observation",
            ),
            unreadable="authority_observation_unreadable",
            mismatch="authority_observation_mismatch",
            ref_issue="authority_ref_mismatch",
        )
        if receipt.authority.before.source_closure != receipt.authority.after.source_closure:
            self.issues.add("authority_changed")

    def verify_frontiers(self) -> None:
        receipt = self.receipt
        for frontier, reference in zip(
            receipt.transcript_frontiers,
            receipt.transcript_frontier_refs,
            strict=True,
        ):
            self.verify_record_ref(
                reference,
                expected=frontier,
                parser=lambda document: InterruptionTranscriptFrontier.from_dict(
                    document,
                    "interruption transcript frontier",
                ),
                unreadable="transcript_frontier_unreadable",
                mismatch="transcript_frontier_mismatch",
                ref_issue="transcript_frontier_ref_mismatch",
            )
            archived = receipt.archive.object_for("run", frontier.locator)
            payload = self._archived_bytes(archived)
            if payload is None:
                continue
            derived = derive_transcript_frontier(
                run_id=receipt.run_id,
                locator=frontier.locator,
                payload=payload,
                captured_at=frontier.captured_at,
            )
            if derived != frontier:
                self.issues.add("transcript_bytes_mismatch")

    def verify_archive(self) -> None:
        receipt = self.receipt
        self.verify_record_ref(
            receipt.archive_ref,
            expected=receipt.archive,
            parser=lambda document: InterruptionArchiveManifest.from_dict(
                document,
                "interruption archive manifest",
            ),
            unreadable="archive_manifest_unreadable",
            mismatch="archive_manifest_mismatch",
            ref_issue="archive_ref_mismatch",
        )
        for source in iter_authority_sources(receipt.authority.before.source_closure):
            archived = receipt.archive.object_for("shot", source.locator)
            payload = self._archived_bytes(archived)
            if payload is None:
                continue
            reclassified = classify_authority_source(source.source_kind, source.locator, payload)
            if reclassified != source:
                self.issues.add("source_identity_mismatch")

    def _archived_bytes(self, archived: Any) -> bytes | None:
        if archived is None:
            self.issues.add("archive_object_missing")
            return None
        try:
            return read_archive_object(self.run_root, archived)
        except RunRecordAbsent:
            self.issues.add("archive_object_missing")
        except ArchiveObjectMismatch:
            self.issues.add("archive_object_mismatch")
        except RunInterruptionArchiveError:
            self.issues.add("archive_object_mismatch")
        return None

    def verify_owner_loss(self) -> None:
        receipt = self.receipt
        loss = receipt.owner_loss
        if loss is None or receipt.owner_loss_ref is None:
            return
        self.verify_record_ref(
            receipt.owner_loss_ref,
            expected=loss,
            parser=lambda document: RunOwnerLossObservation.from_dict(
                document,
                "run owner-loss observation",
                prior_owner=receipt.owner,
            ),
            unreadable="owner_loss_unreadable",
            mismatch="owner_loss_mismatch",
            ref_issue="owner_loss_ref_mismatch",
        )
        manifest_ref = loss.reconciler_manifest_ref
        try:
            manifest_bytes = read_run_record_bytes(self.run_root, manifest_ref.locator)
        except RunInterruptionArchiveError:
            self.issues.add("reconciler_manifest_mismatch")
        else:
            document, _reason = decode_strict_json_object(manifest_bytes)
            if (
                _sha256(manifest_bytes) != manifest_ref.sha256
                or document is None
                or document.get("schema") != RUN_OWNER_LOSS_RECONCILER_MANIFEST_SCHEMA
                or canonical_digest(document) != manifest_ref.record_digest
            ):
                self.issues.add("reconciler_manifest_mismatch")
        prior = loss.prior_running_status
        try:
            status_bytes = read_run_record_bytes(self.run_root, prior.status_snapshot_ref.locator)
        except RunInterruptionArchiveError:
            self.issues.add("prior_status_snapshot_unreadable")
            return
        if _sha256(status_bytes) != prior.status_snapshot_ref.sha256:
            self.issues.add("prior_status_snapshot_mismatch")
            return
        try:
            prior.require_source_bytes(status_bytes, owner=receipt.owner)
        except ValueError:
            self.issues.add("prior_status_snapshot_mismatch")


def reopen_interruption_receipt(shot_root: str | Path, run_id: str) -> RunInterruptionReceipt:
    """Parse the exact selected receipt of one run, or report that none can bind."""

    run_root = Path(shot_root).expanduser().absolute() / "runs" / run_id
    try:
        payload = read_run_record_bytes(run_root, INTERRUPTION_RECEIPT_LOCATOR)
    except RunInterruptionArchiveError as exc:
        raise InterruptionEvaluationUnavailable(f"run {run_id} has no readable interruption receipt: {exc}") from exc
    document, reason = decode_strict_json_object(payload)
    if document is None:
        raise InterruptionEvaluationUnavailable(f"run {run_id} interruption receipt is not a JSON object: {reason}")
    try:
        receipt = RunInterruptionReceipt.from_dict(document, "interruption receipt")
    except ValueError as exc:
        raise InterruptionEvaluationUnavailable(f"run {run_id} interruption receipt is invalid: {exc}") from exc
    if receipt.run_id != run_id:
        raise InterruptionEvaluationUnavailable(f"run {run_id} interruption receipt names run {receipt.run_id!r}")
    return receipt


def evaluate_interruption_receipt(
    shot_root: str | Path,
    run_id: str,
    *,
    evaluated_at: str,
) -> InterruptionReceiptEvaluation:
    """Derive one evaluation by reopening the run's archive graph; never mutate."""

    run_root = Path(shot_root).expanduser().absolute() / "runs" / run_id
    receipt = reopen_interruption_receipt(shot_root, run_id)
    reopener = _Reopener(run_root, receipt)
    reopener.verify_owner()
    reopener.verify_authority()
    reopener.verify_archive()
    reopener.verify_frontiers()
    reopener.verify_owner_loss()
    issues = tuple(sorted(reopener.issues))
    return InterruptionReceiptEvaluation(
        **interruption_evaluation_receipt_binding(receipt),
        status="failed" if issues else "satisfied",
        issue_ids=issues,
        evaluated_at=evaluated_at,
    )


def read_interrupted_run(
    shot_root: str | Path,
    run_id: str,
    *,
    verified_at: str,
) -> VerifiedInterruptedRun:
    """Authoritatively read one interrupted run by re-verifying its closure.

    The selected status must bind the exact receipt and a satisfied evaluation by
    digest, and a fresh evaluation from the archive must still be satisfied; any other
    outcome leaves the run's interruption authority unavailable.  The reader derives an
    empty legal-transaction set and no retry, resume, or dispatch authority.
    """

    run_root = Path(shot_root).expanduser().absolute() / "runs" / run_id
    try:
        payload = read_run_record_bytes(run_root, RUN_STATUS_LOCATOR)
    except RunInterruptionArchiveError as exc:
        raise InterruptionAuthorityUnavailable(f"run {run_id} has no readable status: {exc}") from exc
    document, reason = decode_strict_json_object(payload)
    if document is None:
        raise InterruptionAuthorityUnavailable(f"run {run_id} status is not one JSON object: {reason}")
    if document.get("state") != "interrupted":
        raise InterruptionAuthorityUnavailable(
            f"run {run_id} status is {document.get('state')!r}, not an interrupted terminal status"
        )
    try:
        receipt = reopen_interruption_receipt(shot_root, run_id)
    except InterruptionEvaluationUnavailable as exc:
        raise InterruptionAuthorityUnavailable(str(exc)) from exc
    try:
        evaluation_payload = read_run_record_bytes(run_root, INTERRUPTION_RECEIPT_EVALUATION_LOCATOR)
    except RunInterruptionArchiveError as exc:
        raise InterruptionAuthorityUnavailable(f"run {run_id} has no readable interruption evaluation: {exc}") from exc
    evaluation_document, reason = decode_strict_json_object(evaluation_payload)
    if evaluation_document is None:
        raise InterruptionAuthorityUnavailable(f"run {run_id} interruption evaluation is not a JSON object: {reason}")
    try:
        evaluation = InterruptionReceiptEvaluation.from_dict(
            evaluation_document,
            "interruption receipt evaluation",
            receipt=receipt,
        )
    except ValueError as exc:
        raise InterruptionAuthorityUnavailable(f"run {run_id} interruption evaluation is invalid: {exc}") from exc
    if evaluation.status != "satisfied":
        raise InterruptionAuthorityUnavailable(f"run {run_id} selected an unsatisfied interruption evaluation")
    try:
        status = RunStatusV2.from_dict(
            document,
            selected_record=receipt,
            owner=receipt.owner,
            interruption_evaluation=evaluation,
            where="run status",
        )
    except ValueError as exc:
        raise InterruptionAuthorityUnavailable(
            f"run {run_id} status does not select its exact receipt and evaluation: {exc}"
        ) from exc
    verification = evaluate_interruption_receipt(shot_root, run_id, evaluated_at=verified_at)
    if verification.status != "satisfied":
        raise InterruptionAuthorityUnavailable(
            f"run {run_id} interruption closure no longer verifies: {', '.join(verification.issue_ids)}"
        )
    return VerifiedInterruptedRun(
        status=status,
        receipt=receipt,
        evaluation=evaluation,
        verification=verification,
    )


__all__ = [
    "InterruptionAuthorityUnavailable",
    "InterruptionEvaluationUnavailable",
    "VerifiedInterruptedRun",
    "evaluate_interruption_receipt",
    "read_interrupted_run",
    "reopen_interruption_receipt",
]
