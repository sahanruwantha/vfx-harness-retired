"""Exactly-once terminal commit of an owned run (HIR-0172).

The root owner, still holding its exclusive run-owner fence, captures the authority
observation under the shared shot-authority fence, publishes the receipt and its
sources, lets the independent evaluator reopen the archive, and only then publishes
the evaluation, an interruption summary, the artifact inventory, the terminal
``interrupted`` status, and the latest-run projection.  Terminal status is the commit
point: it replaces the exact ``running`` bytes observed at entry, an existing receipt
or a non-running status refuses, and an unsatisfied evaluation leaves the run running
with interruption authority unavailable.  Nothing here mutates shot authority.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vfx_harness.domain.run_authority_source_records import decode_strict_json_object
from vfx_harness.domain.run_interruption_archive import INTERRUPTION_ARCHIVE_MANIFEST_LOCATOR
from vfx_harness.domain.run_interruption_records import (
    INTERRUPTION_AUTHORITY_OBSERVATION_LOCATOR,
    INTERRUPTION_RECEIPT_LOCATOR,
    RunInterruptionReceipt,
    transcript_frontier_record_locator,
)
from vfx_harness.domain.run_owner_claims import RUN_OWNER_CLAIM_LOCATOR, RunOwnerClaim
from vfx_harness.domain.run_owner_loss import RunOwnerLossObservation
from vfx_harness.domain.run_record_refs import RunRecordRef
from vfx_harness.domain.run_status import (
    INTERRUPTION_RECEIPT_EVALUATION_LOCATOR,
    RUN_SUMMARY_LOCATOR,
    STOP_ENVELOPE_LOCATOR,
    InterruptionReceiptEvaluation,
    RunStatusV2,
)
from vfx_harness.domain.stop_envelopes import StopEnvelope
from vfx_harness.evaluation import run_interruption as interruption_evaluation
from vfx_harness.observability import run_artifacts
from vfx_harness.observability.run_interruption_archive import (
    RunInterruptionArchiveError,
    publish_run_record,
    read_run_record_bytes,
    record_bytes,
)
from vfx_harness.observability.run_owner_fence import RunOwnerFenceError, RunOwnerFenceLease
from vfx_harness.orchestration import run_interruption_capture
from vfx_harness.orchestration.shot_authority_capture import shot_authority_writer_fence

RUN_STATUS_LOCATOR = "status.json"
INTERRUPTION_SUMMARY_SCHEMA = "vfx-harness.interruption-summary/v1"
OWNED_INTERRUPTION_KINDS = frozenset({"operator_interrupt", "termination_request"})
_KIND_SIGNAL = {"operator_interrupt": 2, "termination_request": 15}


class RunTerminalizationConflict(ValueError):
    """The terminal commit could not be made exactly once from verified evidence."""


def _terminal_write_boundary(event: str) -> None:
    """Stable no-op observation seam at each terminal-visible write boundary."""


@dataclass(frozen=True, slots=True)
class TerminalizedInterruption:
    """The exact records one terminal commit selected and read back."""

    receipt: RunInterruptionReceipt
    evaluation: InterruptionReceiptEvaluation
    status: RunStatusV2
    status_sha256: str


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _require_lease(
    lease: RunOwnerFenceLease,
    run_root: Path,
    *,
    kinds: frozenset[str] = frozenset({"owner"}),
) -> RunOwnerClaim:
    if not isinstance(lease, RunOwnerFenceLease) or lease.acquisition_kind not in kinds:
        raise RunTerminalizationConflict(
            f"terminal commit requires a live {' or '.join(sorted(kinds))} fence lease"
        )
    try:
        lease.require_current_identity()
    except RunOwnerFenceError as exc:
        raise RunTerminalizationConflict(f"root-owner lease is not live: {exc}") from exc
    if Path(lease.run_root).resolve() != run_root.resolve():
        raise RunTerminalizationConflict("root-owner lease belongs to another run root")
    return lease.claim


def _atomic_replace(target: Path, payload: bytes) -> None:
    staging = target.with_name(f".{target.name}.tmp.{os.getpid()}.{secrets.token_hex(8)}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(staging, flags, 0o644)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:  # pragma: no cover - POSIX write either advances or raises
                raise OSError("status write made no progress")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(staging, target)
    directory = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _replace_status(run_root: Path, payload: bytes, *, expected_current: bytes | None) -> None:
    target = run_root / RUN_STATUS_LOCATOR
    if target.is_symlink():
        raise RunTerminalizationConflict("run status is a symlink; refusing to publish through an alias")
    if expected_current is not None:
        try:
            current = read_run_record_bytes(run_root, RUN_STATUS_LOCATOR)
        except RunInterruptionArchiveError as exc:
            raise RunTerminalizationConflict(f"run status disappeared before terminal selection: {exc}") from exc
        if current != expected_current:
            raise RunTerminalizationConflict(
                "run status changed under the terminalizer; terminal selection is exactly once"
            )
    try:
        _atomic_replace(target, payload)
    except OSError as exc:
        raise RunTerminalizationConflict(f"could not publish run status: {exc}") from exc


def publish_running_status(
    run_root: str | Path,
    *,
    lease: RunOwnerFenceLease,
    updated_at: str,
) -> RunStatusV2:
    """Publish the v2 ``running`` status that selects the held owner claim."""

    run = Path(run_root).expanduser().absolute()
    claim = _require_lease(lease, run)
    status = RunStatusV2.mint(
        run_id=claim.run_id,
        state="running",
        updated_at=updated_at,
        record_locator=RUN_OWNER_CLAIM_LOCATOR,
        selected_record=claim,
    )
    _replace_status(run, record_bytes(status), expected_current=None)
    run_artifacts.write_latest_projection(
        run_artifacts.RunLayout(shot=run.parent.parent, run_id=claim.run_id, root=run),
        state="running",
    )
    return status


def _commit_terminal_status(
    run_root: Path,
    *,
    lease: RunOwnerFenceLease,
    status: RunStatusV2,
    running_bytes: bytes,
) -> str:
    """Replace the exact observed running bytes with one terminal status and project it."""

    _require_lease(lease, run_root, kinds=frozenset({"owner", "reconciler"}))
    payload = record_bytes(status)
    _replace_status(run_root, payload, expected_current=running_bytes)
    run_artifacts.write_latest_projection(
        run_artifacts.RunLayout(shot=run_root.parent.parent, run_id=status.run_id, root=run_root),
        state=status.state,
    )
    return _sha256(payload)


def publish_passed_status(
    run_root: str | Path,
    *,
    lease: RunOwnerFenceLease,
    summary: Mapping[str, Any],
    updated_at: str,
    state: str = "passed",
    detail: str | None = None,
) -> RunStatusV2:
    """Select the exact run summary as ``passed`` or ``dry-run`` terminal authority."""

    run = Path(run_root).expanduser().absolute()
    claim = _require_lease(lease, run)
    _running, running_bytes = _read_running_status(run, claim)
    status = RunStatusV2.mint(
        run_id=claim.run_id,
        state=state,
        updated_at=updated_at,
        record_locator=RUN_SUMMARY_LOCATOR,
        selected_record=summary,
        detail=detail,
        owner=claim,
        owner_locator=RUN_OWNER_CLAIM_LOCATOR,
    )
    _commit_terminal_status(run, lease=lease, status=status, running_bytes=running_bytes)
    return status


def publish_failed_status(
    run_root: str | Path,
    *,
    lease: RunOwnerFenceLease,
    envelope: StopEnvelope,
    exit_code: int,
    updated_at: str,
    detail: str | None = None,
) -> RunStatusV2:
    """Select one typed stop envelope as ``failed`` terminal authority."""

    run = Path(run_root).expanduser().absolute()
    claim = _require_lease(lease, run)
    _running, running_bytes = _read_running_status(run, claim)
    status = RunStatusV2.mint(
        run_id=claim.run_id,
        state="failed",
        updated_at=updated_at,
        record_locator=STOP_ENVELOPE_LOCATOR,
        selected_record=envelope,
        exit_code=exit_code,
        detail=detail,
        owner=claim,
        owner_locator=RUN_OWNER_CLAIM_LOCATOR,
    )
    _commit_terminal_status(run, lease=lease, status=status, running_bytes=running_bytes)
    return status


def _read_running_status(run_root: Path, claim: RunOwnerClaim) -> tuple[RunStatusV2, bytes]:
    try:
        payload = read_run_record_bytes(run_root, RUN_STATUS_LOCATOR)
    except RunInterruptionArchiveError as exc:
        raise RunTerminalizationConflict(f"run has no readable status to terminalize: {exc}") from exc
    document, reason = decode_strict_json_object(payload)
    if document is None:
        raise RunTerminalizationConflict(f"run status is not one JSON object ({reason})")
    state = document.get("state")
    if state != "running":
        raise RunTerminalizationConflict(
            f"terminal commit already exists ({state!r}); terminal selection is exactly once"
        )
    try:
        status = RunStatusV2.from_dict(document, selected_record=claim, where="run status")
    except ValueError as exc:
        raise RunTerminalizationConflict(
            f"terminal commit requires a v2 running status selecting the held owner claim: {exc}"
        ) from exc
    return status, payload


def _summary(receipt: RunInterruptionReceipt, evaluation: InterruptionReceiptEvaluation) -> dict[str, Any]:
    return {
        "schema": INTERRUPTION_SUMMARY_SCHEMA,
        "run_id": receipt.run_id,
        "command": receipt.owner.command,
        "state": "interrupted",
        "interruption_kind": receipt.interruption_kind,
        "signal_number": receipt.signal_number,
        "exit_code": receipt.exit_code,
        "owner_claim": RUN_OWNER_CLAIM_LOCATOR,
        "owner_claim_digest": receipt.owner.digest,
        "interruption_receipt": INTERRUPTION_RECEIPT_LOCATOR,
        "interruption_receipt_digest": receipt.digest,
        "interruption_evaluation": INTERRUPTION_RECEIPT_EVALUATION_LOCATOR,
        "interruption_evaluation_digest": evaluation.digest,
        "authority_digest": receipt.authority.before.authority_digest,
        "transcripts": [
            {
                "locator": row.locator,
                "state": row.state,
                "last_complete_sequence": row.last_complete_sequence,
                "truncated_tail": row.truncated_tail,
            }
            for row in receipt.transcript_frontiers
        ],
        "legal_transactions": [],
        "retryable": False,
        "resume_authorized": False,
        "dispatch_authority": False,
    }


def _capture_and_publish_sources(
    shot: Path,
    run: Path,
    *,
    claim: RunOwnerClaim,
    clock: Callable[[], str],
) -> tuple[Any, RunRecordRef, tuple[RunRecordRef, ...], RunRecordRef, RunRecordRef]:
    try:
        with shot_authority_writer_fence(shot) as capability:
            captured = run_interruption_capture.capture_interruption_observation(
                shot,
                run,
                run_id=claim.run_id,
                writer_capability=capability,
                clock=clock,
            )
    except run_interruption_capture.RunInterruptionCaptureError as exc:
        raise RunTerminalizationConflict(f"authority capture refused: {exc}") from exc
    _terminal_write_boundary("after_capture")
    try:
        authority_ref = publish_run_record(run, INTERRUPTION_AUTHORITY_OBSERVATION_LOCATOR, captured.observation)
        frontier_refs = tuple(
            publish_run_record(run, transcript_frontier_record_locator(frontier), frontier)
            for frontier in captured.frontiers
        )
        archive_ref = publish_run_record(run, INTERRUPTION_ARCHIVE_MANIFEST_LOCATOR, captured.archive)
        owner_payload = read_run_record_bytes(run, RUN_OWNER_CLAIM_LOCATOR)
    except RunInterruptionArchiveError as exc:
        raise RunTerminalizationConflict(f"interruption source publication failed: {exc}") from exc
    owner_ref = RunRecordRef(
        locator=RUN_OWNER_CLAIM_LOCATOR,
        sha256=_sha256(owner_payload),
        record_schema=claim.SCHEMA,
        record_digest=claim.digest,
    )
    return captured, authority_ref, frontier_refs, archive_ref, owner_ref


def _select_interrupted(
    shot: Path,
    run: Path,
    *,
    lease: RunOwnerFenceLease,
    claim: RunOwnerClaim,
    receipt: RunInterruptionReceipt,
    running_bytes: bytes,
    clock: Callable[[], str],
    detail: str | None,
) -> TerminalizedInterruption:
    """Evaluate a published receipt and select it exactly once as the terminal status."""

    evaluation = interruption_evaluation.evaluate_interruption_receipt(
        shot,
        claim.run_id,
        evaluated_at=clock(),
    )
    if evaluation.status != "satisfied":
        raise RunTerminalizationConflict(
            "independent evaluation did not satisfy the receipt closure "
            f"({', '.join(evaluation.issue_ids)}); the run keeps its running status and "
            "its interruption authority is unavailable"
        )
    try:
        publish_run_record(run, INTERRUPTION_RECEIPT_EVALUATION_LOCATOR, evaluation)
    except RunInterruptionArchiveError as exc:
        raise RunTerminalizationConflict(f"interruption evaluation publication failed: {exc}") from exc
    _terminal_write_boundary("after_evaluation_publication")
    layout = run_artifacts.RunLayout(shot=shot, run_id=claim.run_id, root=run)
    layout.write_summary(_summary(receipt, evaluation))
    layout.write_inventory()
    _terminal_write_boundary("before_status_selection")
    status = RunStatusV2.mint(
        run_id=claim.run_id,
        state="interrupted",
        updated_at=clock(),
        record_locator=INTERRUPTION_RECEIPT_LOCATOR,
        selected_record=receipt,
        exit_code=receipt.exit_code,
        detail=detail,
        owner=claim,
        owner_locator=RUN_OWNER_CLAIM_LOCATOR,
        interruption_evaluation_locator=INTERRUPTION_RECEIPT_EVALUATION_LOCATOR,
        interruption_evaluation=evaluation,
    )
    status_sha256 = _commit_terminal_status(run, lease=lease, status=status, running_bytes=running_bytes)
    _terminal_write_boundary("after_status_selection")
    _terminal_write_boundary("after_latest_projection")
    try:
        verified = interruption_evaluation.read_interrupted_run(shot, claim.run_id, verified_at=clock())
    except interruption_evaluation.InterruptionAuthorityUnavailable as exc:
        raise RunTerminalizationConflict(f"terminal status read-back failed: {exc}") from exc
    if verified.status != status:
        raise RunTerminalizationConflict("terminal status read-back differs from the selected status")
    return TerminalizedInterruption(
        receipt=receipt,
        evaluation=evaluation,
        status=status,
        status_sha256=status_sha256,
    )


def terminalize_interruption(
    shot_root: str | Path,
    run_root: str | Path,
    *,
    lease: RunOwnerFenceLease,
    interruption_kind: str,
    clock: Callable[[], str],
    detail: str | None = None,
) -> TerminalizedInterruption:
    """Commit one owned interruption exactly once from source-verified evidence."""

    shot = Path(shot_root).expanduser().absolute()
    run = Path(run_root).expanduser().absolute()
    claim = _require_lease(lease, run)
    if interruption_kind not in OWNED_INTERRUPTION_KINDS:
        raise RunTerminalizationConflict(
            f"the root owner may terminalize only {sorted(OWNED_INTERRUPTION_KINDS)}; "
            "owner loss belongs to the reconciler"
        )
    _running, running_bytes = _read_running_status(run, claim)
    if os.path.lexists(run / INTERRUPTION_RECEIPT_LOCATOR):
        raise RunTerminalizationConflict(
            "an interruption receipt already exists for this run; terminal selection is exactly once"
        )
    captured, authority_ref, frontier_refs, archive_ref, owner_ref = _capture_and_publish_sources(
        shot,
        run,
        claim=claim,
        clock=clock,
    )
    signal = _KIND_SIGNAL[interruption_kind]
    receipt = RunInterruptionReceipt(
        run_id=claim.run_id,
        interruption_kind=interruption_kind,
        terminalizer_kind="owner",
        owner=claim,
        owner_ref=owner_ref,
        owner_loss=None,
        owner_loss_ref=None,
        authority=captured.observation,
        authority_ref=authority_ref,
        archive=captured.archive,
        archive_ref=archive_ref,
        transcript_frontiers=captured.frontiers,
        transcript_frontier_refs=frontier_refs,
        signal_number=signal,
        exit_code=128 + signal,
        interrupted_at=clock(),
    )
    _require_lease(lease, run)
    try:
        publish_run_record(run, INTERRUPTION_RECEIPT_LOCATOR, receipt)
    except RunInterruptionArchiveError as exc:
        raise RunTerminalizationConflict(f"interruption receipt publication failed: {exc}") from exc
    _terminal_write_boundary("after_receipt_publication")
    return _select_interrupted(
        shot,
        run,
        lease=lease,
        claim=claim,
        receipt=receipt,
        running_bytes=running_bytes,
        clock=clock,
        detail=detail,
    )


def terminalize_owner_loss(
    shot_root: str | Path,
    run_root: str | Path,
    *,
    lease: RunOwnerFenceLease,
    owner_loss: RunOwnerLossObservation,
    owner_loss_ref: RunRecordRef,
    running_bytes: bytes,
    clock: Callable[[], str],
) -> TerminalizedInterruption:
    """Commit one ``owner_lost`` interruption from the reconciler's acquired fence."""

    shot = Path(shot_root).expanduser().absolute()
    run = Path(run_root).expanduser().absolute()
    claim = _require_lease(lease, run, kinds=frozenset({"reconciler"}))
    if os.path.lexists(run / INTERRUPTION_RECEIPT_LOCATOR):
        raise RunTerminalizationConflict(
            "an interruption receipt already exists for this run; complete it instead of minting another"
        )
    captured, authority_ref, frontier_refs, archive_ref, owner_ref = _capture_and_publish_sources(
        shot,
        run,
        claim=claim,
        clock=clock,
    )
    receipt = RunInterruptionReceipt(
        run_id=claim.run_id,
        interruption_kind="owner_lost",
        terminalizer_kind="reconciler",
        owner=claim,
        owner_ref=owner_ref,
        owner_loss=owner_loss,
        owner_loss_ref=owner_loss_ref,
        authority=captured.observation,
        authority_ref=authority_ref,
        archive=captured.archive,
        archive_ref=archive_ref,
        transcript_frontiers=captured.frontiers,
        transcript_frontier_refs=frontier_refs,
        signal_number=None,
        exit_code=None,
        interrupted_at=clock(),
    )
    _require_lease(lease, run, kinds=frozenset({"reconciler"}))
    try:
        publish_run_record(run, INTERRUPTION_RECEIPT_LOCATOR, receipt)
    except RunInterruptionArchiveError as exc:
        raise RunTerminalizationConflict(f"interruption receipt publication failed: {exc}") from exc
    _terminal_write_boundary("after_receipt_publication")
    return _select_interrupted(
        shot,
        run,
        lease=lease,
        claim=claim,
        receipt=receipt,
        running_bytes=running_bytes,
        clock=clock,
        detail="owner lost; reconciled from the released fence",
    )


def complete_prepared_interruption(
    shot_root: str | Path,
    run_root: str | Path,
    *,
    lease: RunOwnerFenceLease,
    running_bytes: bytes,
    clock: Callable[[], str],
) -> TerminalizedInterruption:
    """Select an already prepared, source-valid receipt without changing its bytes."""

    shot = Path(shot_root).expanduser().absolute()
    run = Path(run_root).expanduser().absolute()
    claim = _require_lease(lease, run, kinds=frozenset({"reconciler"}))
    try:
        receipt = interruption_evaluation.reopen_interruption_receipt(shot, claim.run_id)
    except interruption_evaluation.InterruptionEvaluationUnavailable as exc:
        raise RunTerminalizationConflict(f"prepared interruption receipt cannot be reopened: {exc}") from exc
    if receipt.owner != claim:
        raise RunTerminalizationConflict("prepared interruption receipt binds another owner claim")
    return _select_interrupted(
        shot,
        run,
        lease=lease,
        claim=claim,
        receipt=receipt,
        running_bytes=running_bytes,
        clock=clock,
        detail="prepared interruption completed by the reconciler",
    )


def read_running_status_bytes(run_root: str | Path, claim: RunOwnerClaim) -> bytes:
    """Return the exact v2 running bytes that select ``claim``; anything else refuses."""

    _status, payload = _read_running_status(Path(run_root).expanduser().absolute(), claim)
    return payload


__all__ = [
    "INTERRUPTION_SUMMARY_SCHEMA",
    "OWNED_INTERRUPTION_KINDS",
    "RUN_STATUS_LOCATOR",
    "RunTerminalizationConflict",
    "TerminalizedInterruption",
    "complete_prepared_interruption",
    "publish_failed_status",
    "publish_passed_status",
    "publish_running_status",
    "read_running_status_bytes",
    "terminalize_interruption",
    "terminalize_owner_loss",
]
