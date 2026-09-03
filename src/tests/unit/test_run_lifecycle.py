"""Pure HIR-0172 ownership, interruption, and status contracts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError, replace

import pytest

from tests.unit.test_stop_envelopes import _local_stop
from vfx_harness.domain.run_lifecycle import (
    INTERRUPTION_ARCHIVE_MANIFEST_LOCATOR,
    INTERRUPTION_AUTHORITY_OBSERVATION_LOCATOR,
    INTERRUPTION_OWNER_LOSS_OBSERVATION_LOCATOR,
    INTERRUPTION_RECEIPT_EVALUATION_LOCATOR,
    INTERRUPTION_RECEIPT_LOCATOR,
    RUN_OWNER_FENCE_IMPLEMENTATION,
    RUN_OWNER_LOSS_RECONCILER_MANIFEST_LOCATOR,
    RUN_OWNER_LOSS_RECONCILER_MANIFEST_SCHEMA,
    AcceptedStateSourceClosure,
    ArchivedSourceObject,
    AuthoredInputsSourceClosure,
    AuthoritySourceIdentity,
    DurableStateSourceClosure,
    InterruptionArchiveManifest,
    InterruptionAuthorityObservation,
    InterruptionAuthoritySourceClosure,
    InterruptionReceiptEvaluation,
    InterruptionTranscriptFrontier,
    PriorRunningStatusEvidence,
    RunAuthoritySnapshot,
    RunInterruptionReceipt,
    RunOwnerClaim,
    RunOwnerLossObservation,
    RunRecordRef,
    RunStatusV2,
    SelectedPlanSourceClosure,
    interruption_evaluation_receipt_binding,
    iter_authority_sources,
    transcript_frontier_record_locator,
)

_RUN = "run-lifecycle-001"
_T0 = "2026-09-01T00:00:00+00:00"
_T1 = "2026-09-01T00:01:00+00:00"
_T2 = "2026-09-01T00:02:00+00:00"
_T3 = "2026-09-01T00:03:00+00:00"


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _owner(*, run_id: str = _RUN, claimed_at: str = _T0) -> RunOwnerClaim:
    return RunOwnerClaim(
        run_id=run_id,
        command="plan",
        invocation_digest=_digest("invocation"),
        owner_kind="direct",
        owner_id=_digest("unpredictable owner id"),
        process_id=31415,
        process_start_token=_digest("process birth"),
        shot_root_device=41,
        shot_root_inode=4101,
        runs_directory_device=42,
        runs_directory_inode=4201,
        run_root_device=43,
        run_root_inode=4301,
        owner_directory_device=44,
        owner_directory_inode=4401,
        claim_device=45,
        claim_inode=4501,
        fence_locator="owner/fence.lock",
        fence_implementation=RUN_OWNER_FENCE_IMPLEMENTATION,
        fence_device=51,
        fence_inode=8675309,
        descriptor_inheritable=False,
        manifest_locator="manifest.json",
        manifest_sha256=_digest("manifest bytes"),
        claimed_at=claimed_at,
    )


def _authority_value(*, accepted: str = "accepted-a") -> InterruptionAuthoritySourceClosure:
    ledger = AuthoritySourceIdentity.valid(
        source_kind="accepted_ledger",
        locator="shot.json",
        byte_count=len(accepted),
        sha256=_digest(f"ledger bytes:{accepted}"),
        record_schema="vfx-harness.shot-ledger/v1",
        record_digest=_digest(f"ledger record:{accepted}"),
    )
    return InterruptionAuthoritySourceClosure(
        SelectedPlanSourceClosure.absent(),
        AcceptedStateSourceClosure.mint_present(ledger=ledger),
        DurableStateSourceClosure.absent(),
        AuthoredInputsSourceClosure.absent(),
    )


def _authority_observation(
    *,
    run_id: str = _RUN,
    after_value: InterruptionAuthoritySourceClosure | None = None,
) -> InterruptionAuthorityObservation:
    value = _authority_value()
    return InterruptionAuthorityObservation(
        run_id=run_id,
        before=RunAuthoritySnapshot.mint(
            run_id=run_id,
            source_closure=value,
            captured_at=_T1,
        ),
        after=RunAuthoritySnapshot.mint(
            run_id=run_id,
            source_closure=value if after_value is None else after_value,
            captured_at=_T2,
        ),
    )


def _frontier(
    *,
    locator: str = "logs/transcripts/plan/session.jsonl",
    state: str = "incomplete",
    kind: str | None = "prompt",
    truncated_tail: bool = False,
) -> InterruptionTranscriptFrontier:
    return InterruptionTranscriptFrontier(
        run_id=_RUN,
        locator=locator,
        sha256=_digest(f"bytes:{locator}"),
        byte_count=381,
        truncated_tail=truncated_tail,
        last_complete_sequence=None if kind is None else 2,
        last_complete_kind=kind,
        state=state,
        captured_at=_T2,
    )


def _ref(locator: str, record) -> RunRecordRef:
    return RunRecordRef(
        locator=locator,
        sha256=_digest(f"record bytes:{locator}"),
        record_schema=record.SCHEMA,
        record_digest=record.digest,
    )


def _prior_running_status(owner: RunOwnerClaim) -> PriorRunningStatusEvidence:
    status = RunStatusV2.mint(
        run_id=owner.run_id,
        state="running",
        updated_at=_T1,
        record_locator="owner/claim.json",
        selected_record=owner,
    )
    status_bytes = (json.dumps(status.as_dict(), indent=2, sort_keys=True) + "\n").encode()
    return PriorRunningStatusEvidence.mint(
        status_bytes=status_bytes,
        owner=owner,
        captured_at=_T1,
    )


def _archive(
    owner: RunOwnerClaim,
    authority: InterruptionAuthorityObservation,
    frontiers: tuple[InterruptionTranscriptFrontier, ...],
) -> InterruptionArchiveManifest:
    return InterruptionArchiveManifest.mint(
        run_id=owner.run_id,
        captured_at=_T1,
        authority_digest=authority.before.authority_digest,
        objects=[
            *(
                ArchivedSourceObject("shot", source.locator, source.byte_count, source.sha256)
                for source in iter_authority_sources(authority.before.source_closure)
            ),
            *(
                ArchivedSourceObject("run", row.locator, row.byte_count, row.sha256)
                for row in frontiers
            ),
        ],
    )


def _receipt(
    *,
    interruption_kind: str = "operator_interrupt",
    owner_loss: RunOwnerLossObservation | None = None,
) -> RunInterruptionReceipt:
    owner = _owner()
    authority = _authority_observation()
    frontier = _frontier()
    archive = _archive(owner, authority, (frontier,))
    signal_number = {
        "operator_interrupt": 2,
        "termination_request": 15,
    }.get(interruption_kind)
    return RunInterruptionReceipt(
        run_id=owner.run_id,
        interruption_kind=interruption_kind,
        terminalizer_kind=("reconciler" if interruption_kind == "owner_lost" else "owner"),
        owner=owner,
        owner_ref=_ref("owner/claim.json", owner),
        owner_loss=owner_loss,
        owner_loss_ref=(None if owner_loss is None else _ref(INTERRUPTION_OWNER_LOSS_OBSERVATION_LOCATOR, owner_loss)),
        authority=authority,
        authority_ref=_ref(INTERRUPTION_AUTHORITY_OBSERVATION_LOCATOR, authority),
        archive=archive,
        archive_ref=_ref(INTERRUPTION_ARCHIVE_MANIFEST_LOCATOR, archive),
        transcript_frontiers=(frontier,),
        transcript_frontier_refs=(_ref(transcript_frontier_record_locator(frontier), frontier),),
        signal_number=signal_number,
        exit_code=(None if signal_number is None else 128 + signal_number),
        interrupted_at=_T3,
    )


def _owner_loss(
    owner: RunOwnerClaim,
    *,
    observed_at: str = _T1,
    supervisor_wait_status: int | None = None,
) -> RunOwnerLossObservation:
    return RunOwnerLossObservation(
        run_id=owner.run_id,
        prior_owner_digest=owner.digest,
        reconciler_run_id="run-reconciler-001",
        reconciler_manifest_ref=RunRecordRef(
            locator=RUN_OWNER_LOSS_RECONCILER_MANIFEST_LOCATOR,
            sha256=_digest("reconciler manifest bytes"),
            record_schema=RUN_OWNER_LOSS_RECONCILER_MANIFEST_SCHEMA,
            record_digest=_digest("reconciler manifest record"),
        ),
        fence_locator=owner.fence_locator,
        fence_device=owner.fence_device,
        fence_inode=owner.fence_inode,
        prior_running_status=_prior_running_status(owner),
        exclusive_fence_acquired=True,
        exclusive_acquisition_observed_at=observed_at,
        supervisor_wait_status=supervisor_wait_status,
    )


def _evaluation(
    receipt: RunInterruptionReceipt,
    *,
    status: str = "satisfied",
    evaluated_at: str = _T3,
) -> InterruptionReceiptEvaluation:
    return InterruptionReceiptEvaluation(
        **interruption_evaluation_receipt_binding(receipt),
        status=status,
        issue_ids=() if status == "satisfied" else ("authority_changed",),
        evaluated_at=evaluated_at,
    )


def _structural_interrupted_status(
    receipt: RunInterruptionReceipt,
    evaluation: InterruptionReceiptEvaluation,
) -> RunStatusV2:
    """Build non-authoritative wire structure for parser/fence tests only."""

    return RunStatusV2(
        run_id=receipt.run_id,
        state="interrupted",
        updated_at=_T3,
        exit_code=receipt.exit_code,
        detail=None,
        owner_claim=receipt.owner_ref.locator,
        owner_claim_digest=receipt.owner.digest,
        summary=None,
        summary_digest=None,
        stop_envelope=None,
        stop_envelope_digest=None,
        interruption_receipt=INTERRUPTION_RECEIPT_LOCATOR,
        interruption_receipt_digest=receipt.digest,
        interruption_evaluation=INTERRUPTION_RECEIPT_EVALUATION_LOCATOR,
        interruption_evaluation_digest=evaluation.digest,
    )


def test_owner_claim_round_trip_binds_fence_manifest_and_non_inheritance() -> None:
    owner = _owner()

    assert RunOwnerClaim.from_dict(owner.as_dict()) == owner
    assert owner.as_dict()["fence_implementation"] == "posix-flock-inode/v1"
    assert owner.as_dict()["descriptor_inheritable"] is False
    changed_audit_time = replace(owner, claimed_at=_T1)
    assert changed_audit_time.semantic_identity_digest == owner.semantic_identity_digest
    assert changed_audit_time.digest != owner.digest

    with pytest.raises(FrozenInstanceError):
        owner.process_id = 7  # type: ignore[misc]
    with pytest.raises(ValueError, match="must be false"):
        replace(owner, descriptor_inheritable=True)
    with pytest.raises(ValueError, match="owner_kind"):
        replace(owner, owner_kind="child")


def test_owner_claim_rejects_stale_or_incomplete_wire_identity() -> None:
    owner = _owner()
    stale = owner.as_dict()
    stale["fence_inode"] += 1
    with pytest.raises(ValueError, match="is stale"):
        RunOwnerClaim.from_dict(stale)

    incomplete = owner.as_dict()
    incomplete.pop("fence_device")
    with pytest.raises(ValueError, match="fields mismatch"):
        RunOwnerClaim.from_dict(incomplete)

    changed_time = owner.as_dict()
    changed_time["claimed_at"] = _T1
    with pytest.raises(ValueError, match="claim_digest is stale"):
        RunOwnerClaim.from_dict(changed_time)


def test_authority_snapshot_requires_and_round_trips_the_closed_source_closure() -> None:
    closure = _authority_value()
    snapshot = RunAuthoritySnapshot.mint(
        run_id=_RUN,
        source_closure=closure,
        captured_at=_T1,
    )

    assert snapshot.source_closure == closure
    assert snapshot.authority_digest == closure.digest
    assert RunAuthoritySnapshot.from_dict(snapshot.as_dict()) == snapshot

    with pytest.raises(ValueError, match="typed authority source closure"):
        replace(snapshot, source_closure={})  # type: ignore[arg-type]


def test_authority_observation_requires_exact_pre_post_equality() -> None:
    observation = _authority_observation()
    assert InterruptionAuthorityObservation.from_dict(observation.as_dict()) == observation

    with pytest.raises(ValueError, match="authority changed"):
        _authority_observation(after_value=_authority_value(accepted="accepted-b"))


def test_transcript_frontier_never_invents_or_hides_close() -> None:
    closed = _frontier(state="closed", kind="close")
    assert InterruptionTranscriptFrontier.from_dict(closed.as_dict()) == closed

    with pytest.raises(ValueError, match="must end at a real close"):
        _frontier(state="closed", kind="prompt")
    with pytest.raises(ValueError, match="cannot relabel a close"):
        _frontier(state="incomplete", kind="close")
    truncated = _frontier(truncated_tail=True)
    assert truncated.state == "incomplete"
    with pytest.raises(ValueError, match="cannot have a truncated tail"):
        _frontier(state="closed", kind="close", truncated_tail=True)


@pytest.mark.parametrize(
    ("kind", "signal_number", "exit_code"),
    [
        ("operator_interrupt", 2, 130),
        ("termination_request", 15, 143),
    ],
)
def test_signal_interruption_round_trip_derives_closed_exit_mapping(
    kind: str,
    signal_number: int,
    exit_code: int,
) -> None:
    receipt = _receipt(interruption_kind=kind)
    assert receipt.signal_number == signal_number
    assert receipt.exit_code == exit_code
    assert receipt.terminalizer_kind == "owner"
    assert RunInterruptionReceipt.from_dict(receipt.as_dict()) == receipt

    with pytest.raises(ValueError, match="must be derived"):
        replace(receipt, exit_code=1)
    with pytest.raises(ValueError, match="terminalizer_kind"):
        replace(receipt, terminalizer_kind="reconciler")


def test_owner_loss_requires_exact_reacquired_fence_and_unknown_exit_without_wait_status() -> None:
    owner = _owner()
    loss = _owner_loss(owner)
    authority = _authority_observation()
    frontier = _frontier()
    archive = _archive(owner, authority, (frontier,))
    receipt = RunInterruptionReceipt(
        run_id=owner.run_id,
        interruption_kind="owner_lost",
        terminalizer_kind="reconciler",
        owner=owner,
        owner_ref=_ref("owner/claim.json", owner),
        owner_loss=loss,
        owner_loss_ref=_ref(INTERRUPTION_OWNER_LOSS_OBSERVATION_LOCATOR, loss),
        authority=authority,
        authority_ref=_ref(INTERRUPTION_AUTHORITY_OBSERVATION_LOCATOR, authority),
        archive=archive,
        archive_ref=_ref(INTERRUPTION_ARCHIVE_MANIFEST_LOCATOR, archive),
        transcript_frontiers=(frontier,),
        transcript_frontier_refs=(_ref(transcript_frontier_record_locator(frontier), frontier),),
        signal_number=None,
        exit_code=None,
        interrupted_at=_T3,
    )
    assert receipt.signal_number is None
    assert receipt.exit_code is None
    assert receipt.terminalizer_kind == "reconciler"
    assert loss.exclusive_fence_acquired is True
    assert RunOwnerLossObservation.from_dict(loss.as_dict(), prior_owner=owner) == loss

    with pytest.raises(ValueError, match="exact prior owner fence"):
        replace(loss, fence_inode=loss.fence_inode + 1).require_matches_owner(owner)
    with pytest.raises(ValueError, match="must be true"):
        replace(loss, exclusive_fence_acquired=False)
    with pytest.raises(ValueError, match=r"exact .* record digest"):
        replace(
            receipt,
            owner_loss=replace(
                loss,
                reconciler_manifest_ref=replace(
                    loss.reconciler_manifest_ref,
                    sha256=_digest("substituted reconciliation manifest"),
                ),
            ),
        )

    with pytest.raises(ValueError, match="canonical archive locator"):
        replace(
            loss,
            reconciler_manifest_ref=replace(
                loss.reconciler_manifest_ref,
                locator="scratch/copied-reconciler-manifest.json",
            ),
        )
    with pytest.raises(ValueError, match="distinct root run"):
        replace(loss, reconciler_run_id=loss.run_id)


def test_owner_loss_wait_status_is_audit_only_and_capture_starts_after_fence_acquisition() -> None:
    owner = _owner()
    loss = _owner_loss(owner, supervisor_wait_status=9)
    receipt = _receipt(interruption_kind="owner_lost", owner_loss=loss)

    assert loss.signal_number == 9
    assert loss.exit_code == 137
    assert receipt.signal_number is None
    assert receipt.exit_code is None

    with pytest.raises(ValueError, match="owner-loss fence/prior-status capture"):
        replace(loss, exclusive_acquisition_observed_at=_T2)

    authority_captured_too_early = replace(
        loss,
        exclusive_acquisition_observed_at="2026-09-01T00:01:30+00:00",
        prior_running_status=replace(
            loss.prior_running_status,
            captured_at="2026-09-01T00:01:30+00:00",
        ),
    )
    with pytest.raises(ValueError, match="owner-loss fence/authority capture"):
        _receipt(interruption_kind="owner_lost", owner_loss=authority_captured_too_early)

    prior_status_captured_too_late = replace(
        loss,
        prior_running_status=replace(
            loss.prior_running_status,
            captured_at="2026-09-01T00:01:30+00:00",
        ),
    )
    with pytest.raises(ValueError, match="prior-status/authority capture"):
        _receipt(interruption_kind="owner_lost", owner_loss=prior_status_captured_too_late)


def test_interruption_receipt_rejects_stop_action_and_dispatch_fields() -> None:
    receipt = _receipt()
    for illegal in ("actions", "stop_envelope", "dispatch_mode", "retryable"):
        wire = receipt.as_dict()
        wire[illegal] = [] if illegal == "actions" else None
        with pytest.raises(ValueError, match="fields mismatch"):
            RunInterruptionReceipt.from_dict(wire)


def test_interruption_receipt_source_refs_reject_stale_or_substituted_records() -> None:
    receipt = _receipt()
    with pytest.raises(ValueError, match=r"exact .* record digest"):
        replace(
            receipt,
            owner_ref=replace(receipt.owner_ref, record_digest=_digest("another owner")),
        )

    with pytest.raises(ValueError, match="canonical owner claim"):
        replace(receipt, owner_ref=replace(receipt.owner_ref, locator="scratch/copied-claim.json"))
    with pytest.raises(ValueError, match="canonical report locator"):
        replace(
            receipt,
            authority_ref=replace(
                receipt.authority_ref,
                locator="reports/substituted-authority.json",
            ),
        )
    with pytest.raises(ValueError, match="canonical record locator"):
        replace(
            receipt,
            transcript_frontier_refs=(
                replace(
                    receipt.transcript_frontier_refs[0],
                    locator="reports/substituted-frontier.json",
                ),
            ),
        )


def test_evaluation_structurally_binds_receipt_without_an_outcome_factory() -> None:
    receipt = _receipt()
    satisfied = _evaluation(receipt)
    assert (
        InterruptionReceiptEvaluation.from_dict(
            satisfied.as_dict(),
            receipt=receipt,
        )
        == satisfied
    )
    assert _evaluation(receipt, status="failed").status == "failed"
    assert not hasattr(InterruptionReceiptEvaluation, "mint")

    with pytest.raises(ValueError, match="cannot contain issues"):
        replace(satisfied, issue_ids=("authority_changed",))
    with pytest.raises(ValueError, match="must name at least one"):
        replace(satisfied, status="failed")

    with pytest.raises(ValueError, match="receipt/evaluation"):
        _evaluation(receipt, evaluated_at=_T2)


def test_receipt_schema_rejects_unregistered_ambient_context_refs() -> None:
    receipt = _receipt()
    for name in (
        "last_checkpoint_ref",
        "last_journal_ref",
        "candidate_ref",
        "active_boundary_ref",
    ):
        wire = receipt.as_dict()
        wire[name] = None
        with pytest.raises(ValueError, match="fields mismatch"):
            RunInterruptionReceipt.from_dict(wire)


@pytest.mark.parametrize("state", ["passed", "dry-run"])
def test_success_status_selects_only_exact_summary(state: str) -> None:
    owner = _owner()
    summary = {
        "schema": "vfx-harness.run-summary/v1",
        "run_id": _RUN,
        "command": owner.command,
        "state": state,
        "exit_code": 0,
    }
    status = RunStatusV2.mint(
        run_id=_RUN,
        state=state,
        updated_at=_T3,
        record_locator="reports/summary.json",
        selected_record=summary,
        owner=owner,
        owner_locator="owner/claim.json",
    )
    assert status.exit_code == 0
    assert status.summary == "reports/summary.json"
    assert status.owner_claim_digest == owner.digest
    assert RunStatusV2.from_dict(status.as_dict(), selected_record=summary, owner=owner) == status

    with pytest.raises(ValueError, match="canonical run summary locator"):
        RunStatusV2.mint(
            run_id=_RUN,
            state=state,
            updated_at=_T3,
            record_locator="scratch/copied-summary.json",
            selected_record=summary,
            owner=owner,
            owner_locator="owner/claim.json",
        )


@pytest.mark.parametrize("summary_exit_code", [9, True, None])
def test_success_status_rejects_summary_exit_code_that_is_not_exact_zero(
    summary_exit_code: object,
) -> None:
    owner = _owner()
    summary = {
        "schema": "vfx-harness.run-summary/v1",
        "run_id": _RUN,
        "command": owner.command,
        "state": "passed",
    }
    if summary_exit_code is not None:
        summary["exit_code"] = summary_exit_code

    with pytest.raises(ValueError, match="run summary exit_code"):
        RunStatusV2.mint(
            run_id=_RUN,
            state="passed",
            updated_at=_T3,
            record_locator="reports/summary.json",
            selected_record=summary,
            owner=owner,
            owner_locator="owner/claim.json",
        )


def test_status_mint_refuses_caller_exit_code_that_conflicts_with_selected_state() -> None:
    owner = _owner()
    summary = {
        "schema": "vfx-harness.run-summary/v1",
        "run_id": _RUN,
        "command": owner.command,
        "state": "passed",
        "exit_code": 0,
    }

    with pytest.raises(ValueError, match="caller-provided exit code"):
        RunStatusV2.mint(
            run_id=_RUN,
            state="running",
            updated_at=_T0,
            record_locator="owner/claim.json",
            selected_record=owner,
            exit_code=9,
        )
    with pytest.raises(ValueError, match="caller-provided nonzero exit code"):
        RunStatusV2.mint(
            run_id=_RUN,
            state="passed",
            updated_at=_T3,
            record_locator="reports/summary.json",
            selected_record=summary,
            exit_code=9,
            owner=owner,
            owner_locator="owner/claim.json",
        )


def test_success_summary_must_bind_the_root_owner_command() -> None:
    owner = _owner()
    summary = {
        "schema": "vfx-harness.run-summary/v1",
        "run_id": _RUN,
        "command": "build",
        "state": "passed",
        "exit_code": 0,
    }

    with pytest.raises(ValueError, match="root owner claim"):
        RunStatusV2.mint(
            run_id=_RUN,
            state="passed",
            updated_at=_T3,
            record_locator="reports/summary.json",
            selected_record=summary,
            owner=owner,
            owner_locator="owner/claim.json",
        )


def test_running_and_failed_statuses_both_retain_the_exact_root_owner() -> None:
    owner = _owner()
    running = RunStatusV2.mint(
        run_id=_RUN,
        state="running",
        updated_at=_T0,
        record_locator="owner/claim.json",
        selected_record=owner,
    )
    assert running.owner_claim_digest == owner.digest
    assert running.exit_code is None

    stop = _local_stop(run_id=_RUN)
    failed = RunStatusV2.mint(
        run_id=_RUN,
        state="failed",
        updated_at=_T3,
        record_locator="reports/stop-envelope.json",
        selected_record=stop,
        exit_code=9,
        owner=owner,
        owner_locator="owner/claim.json",
    )
    assert failed.stop_envelope_digest == stop.digest
    assert failed.interruption_receipt is None
    assert failed.owner_claim_digest == owner.digest
    assert RunStatusV2.from_dict(failed.as_dict(), selected_record=stop, owner=owner) == failed

    with pytest.raises(ValueError, match="canonical stop-envelope locator"):
        RunStatusV2.mint(
            run_id=_RUN,
            state="failed",
            updated_at=_T3,
            record_locator="scratch/copied-stop-envelope.json",
            selected_record=stop,
            exit_code=9,
            owner=owner,
            owner_locator="owner/claim.json",
        )


def _mint_interrupted(receipt, evaluation, **overrides) -> RunStatusV2:
    arguments = {
        "run_id": _RUN,
        "state": "interrupted",
        "updated_at": _T3,
        "record_locator": INTERRUPTION_RECEIPT_LOCATOR,
        "selected_record": receipt,
        "owner": receipt.owner,
        "owner_locator": "owner/claim.json",
        "interruption_evaluation_locator": INTERRUPTION_RECEIPT_EVALUATION_LOCATOR,
        "interruption_evaluation": evaluation,
        **overrides,
    }
    return RunStatusV2.mint(**arguments)


def test_interrupted_status_selects_only_a_satisfied_evaluation_of_its_receipt() -> None:
    receipt = _receipt()
    evaluation = _evaluation(receipt)
    status = _mint_interrupted(receipt, evaluation)
    assert status == _structural_interrupted_status(receipt, evaluation)
    assert status.exit_code == 130
    assert status.stop_envelope is None
    assert status.interruption_receipt_digest == receipt.digest
    assert status.interruption_evaluation_digest == evaluation.digest
    assert (
        RunStatusV2.from_dict(
            status.as_dict(),
            selected_record=receipt,
            owner=receipt.owner,
            interruption_evaluation=evaluation,
        )
        == status
    )

    with pytest.raises(ValueError, match="satisfied independent evaluation"):
        _mint_interrupted(receipt, _evaluation(receipt, status="failed"))
    with pytest.raises(ValueError, match="requires the independent evaluation"):
        _mint_interrupted(receipt, None)
    other = _evaluation(_receipt(interruption_kind="termination_request"))
    with pytest.raises(ValueError, match="does not bind the exact receipt closure"):
        _mint_interrupted(receipt, other)
    with pytest.raises(ValueError, match="interruption evaluation/status selection"):
        _mint_interrupted(receipt, evaluation, updated_at=_T2)
    with pytest.raises(ValueError, match="derived from its receipt"):
        _mint_interrupted(receipt, evaluation, exit_code=1)
    with pytest.raises(ValueError, match="exact owner claim"):
        _mint_interrupted(receipt, evaluation, owner=_owner(claimed_at=_T1))
    with pytest.raises(ValueError, match="canonical interruption evaluation locator"):
        _mint_interrupted(receipt, evaluation, interruption_evaluation_locator="reports/other.json")


def test_interrupted_status_structure_requires_canonical_record_locators() -> None:
    receipt = _receipt()
    evaluation = _evaluation(receipt)
    status = _structural_interrupted_status(receipt, evaluation)

    with pytest.raises(ValueError, match="canonical interruption receipt locator"):
        replace(status, interruption_receipt="reports/another-receipt.json")
    with pytest.raises(ValueError, match="canonical interruption evaluation locator"):
        replace(
            status,
            interruption_evaluation="reports/another-evaluation.json",
        )


def test_status_matrix_rejects_unrelated_stop_action_and_dispatch_fields() -> None:
    receipt = _receipt()
    evaluation = _evaluation(receipt)
    status = _structural_interrupted_status(receipt, evaluation)
    for illegal in ("stop_class", "cause_fingerprint", "actions", "dispatch_mode"):
        wire = status.as_dict()
        wire[illegal] = None
        with pytest.raises(ValueError, match="fields mismatch"):
            RunStatusV2.from_dict(
                wire,
                selected_record=receipt,
                interruption_evaluation=evaluation,
            )

    hybrid = status.as_dict()
    hybrid["stop_envelope"] = "reports/stop-envelope.json"
    hybrid["stop_envelope_digest"] = _digest("stop")
    with pytest.raises(ValueError, match="does not match its selected record and state matrix"):
        RunStatusV2.from_dict(
            hybrid,
            selected_record=receipt,
            owner=receipt.owner,
            interruption_evaluation=evaluation,
        )


def test_evaluation_issue_ids_are_a_closed_vocabulary() -> None:
    receipt = _receipt()
    with pytest.raises(ValueError, match="closed evaluator vocabulary"):
        InterruptionReceiptEvaluation(
            **interruption_evaluation_receipt_binding(receipt),
            status="failed",
            issue_ids=("invented_issue",),
            evaluated_at=_T3,
        )
