"""The independent evaluator reopens only the run-owned archive (HIR-0172)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tests.integration.test_judgment_debt_public_pipeline import _public_fixture_root
from tests.integration.test_shot_ledger_v2_derivation import _two_accepted_layers
from tests.run_owner_support import MonotonicClock
from vfx_harness.domain.prior_running_status import PriorRunningStatusEvidence
from vfx_harness.domain.run_authority_source_identity import canonical_digest
from vfx_harness.domain.run_interruption_archive import (
    INTERRUPTION_ARCHIVE_MANIFEST_LOCATOR,
    ArchivedSourceObject,
)
from vfx_harness.domain.run_interruption_records import (
    INTERRUPTION_AUTHORITY_OBSERVATION_LOCATOR,
    INTERRUPTION_OWNER_LOSS_OBSERVATION_LOCATOR,
    INTERRUPTION_RECEIPT_LOCATOR,
    RunInterruptionReceipt,
    transcript_frontier_record_locator,
)
from vfx_harness.domain.run_owner_claims import RUN_OWNER_CLAIM_LOCATOR, RunOwnerClaim
from vfx_harness.domain.run_owner_loss import (
    RUN_OWNER_LOSS_RECONCILER_MANIFEST_LOCATOR,
    RUN_OWNER_LOSS_RECONCILER_MANIFEST_SCHEMA,
    RunOwnerLossObservation,
)
from vfx_harness.domain.run_record_refs import RunRecordRef
from vfx_harness.domain.run_status import RunStatusV2
from vfx_harness.evaluation import run_interruption as evaluator
from vfx_harness.observability import run_artifacts
from vfx_harness.observability.run_interruption_archive import (
    publish_run_record,
    read_run_record_bytes,
)
from vfx_harness.observability.run_owner_fence import acquire_run_owner_fence
from vfx_harness.observability.run_owner_manifest import RUN_DISPATCH_SCHEMA, RUN_MANIFEST_SCHEMA
from vfx_harness.orchestration import run_interruption_capture as capture
from vfx_harness.orchestration.plan_authority import publish_current
from vfx_harness.orchestration.shot_authority_capture import shot_authority_writer_fence

_RUN = "run-interrupt-001"


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


# Status and receipt timestamps must follow the owner claim minted by the live fence
# acquisition; a second-resolution clock floored before that acquisition read earlier
# than the microsecond claim whenever the two straddled a second boundary.
_Clock = MonotonicClock


def _manifest(run_id: str, shot_id: str) -> dict:
    return {
        "schema": RUN_MANIFEST_SCHEMA,
        "run_id": run_id,
        "shot_id": shot_id,
        "started_at": "2026-09-01T12:00:00+00:00",
        "invocation": {
            "dispatch": {"schema": RUN_DISPATCH_SCHEMA, "kind": "direct", "command": "plan"},
            "argv": ["vfx plan", shot_id],
            "parameters": {},
        },
        "layout": {
            "status": "status.json",
            "artifact_index": "artifacts.json",
            "logs": "logs/",
            "reports": "reports/",
            "evidence": "evidence/",
            "checkpoints": "checkpoints/",
            "scratch": "scratch/",
            "deliverables": "deliverables/",
            "owner_claim": "owner/claim.json",
            "owner_fence": "owner/fence.lock",
        },
        "authority": {
            "authored_inputs": "../../brief.md and ../../refs/",
            "published_plan": "../../plans/current.json when present",
            "plan_authoring_workspace": "scratch/plan-workspace/ for global plan invocations",
            "selected_plan_consumers": "../../plans/current.json",
            "accepted_build": "../../build/ and ../../shot.json",
            "generated_output": "this directory",
        },
        "reader_entrypoint": "manifest.json",
    }


def _owned_run(root: Path, run_id: str = _RUN) -> Path:
    run_root = root / "runs" / run_id
    run_root.mkdir(parents=True)
    (run_root / "manifest.json").write_text(
        json.dumps(_manifest(run_id, root.name), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    transcript = run_root / "logs" / "transcripts" / "plan" / "session.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_bytes(
        b'{"seq": 1, "dt": 0.0, "kind": "open"}\n{"seq": 2, "dt": 0.5, "kind": "prompt"}\n'
    )
    return run_root


def _owner_ref(run_root: Path, claim: RunOwnerClaim) -> RunRecordRef:
    payload = read_run_record_bytes(run_root, RUN_OWNER_CLAIM_LOCATOR)
    return RunRecordRef(
        locator=RUN_OWNER_CLAIM_LOCATOR,
        sha256=_sha(payload),
        record_schema=claim.SCHEMA,
        record_digest=claim.digest,
    )


def _interrupt(root: Path, run_root: Path, run_id: str = _RUN) -> RunInterruptionReceipt:
    """Capture under the shared fence, publish every record, and seal one receipt."""

    clock = _Clock()
    with acquire_run_owner_fence(run_root, run_id=run_id, command="plan", owner_kind="direct") as lease:
        claim = lease.claim
        with shot_authority_writer_fence(root) as capability:
            captured = capture.capture_interruption_observation(
                root,
                run_root,
                run_id=run_id,
                writer_capability=capability,
                clock=clock,
            )
        authority_ref = publish_run_record(
            run_root, INTERRUPTION_AUTHORITY_OBSERVATION_LOCATOR, captured.observation
        )
        frontier_refs = tuple(
            publish_run_record(run_root, transcript_frontier_record_locator(frontier), frontier)
            for frontier in captured.frontiers
        )
        archive_ref = publish_run_record(run_root, INTERRUPTION_ARCHIVE_MANIFEST_LOCATOR, captured.archive)
        receipt = RunInterruptionReceipt(
            run_id=run_id,
            interruption_kind="operator_interrupt",
            terminalizer_kind="owner",
            owner=claim,
            owner_ref=_owner_ref(run_root, claim),
            owner_loss=None,
            owner_loss_ref=None,
            authority=captured.observation,
            authority_ref=authority_ref,
            archive=captured.archive,
            archive_ref=archive_ref,
            transcript_frontiers=captured.frontiers,
            transcript_frontier_refs=frontier_refs,
            signal_number=2,
            exit_code=130,
            interrupted_at=clock(),
        )
        publish_run_record(run_root, INTERRUPTION_RECEIPT_LOCATOR, receipt)
    return receipt


def _evaluate(root: Path, run_id: str = _RUN):
    return evaluator.evaluate_interruption_receipt(root, run_id, evaluated_at="2036-01-01T00:00:00+00:00")


def _bare_shot(tmp_path: Path) -> Path:
    root = _public_fixture_root(tmp_path)
    publish_current(root, run_artifacts.create(root, "interruption-fixture"), outcome="clean_with_deferred")
    return root


def test_receipt_over_rich_authority_evaluates_from_the_archive_alone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _two_accepted_layers(tmp_path, monkeypatch)
    run_root = _owned_run(root)
    receipt = _interrupt(root, run_root)
    closure = receipt.authority.before.source_closure
    assert closure.selected_plan.state == "present_valid"
    assert closure.selected_plan.effective_view_pointer is not None
    assert closure.accepted_state.state == "present_valid"
    assert [row.locator for row in closure.accepted_state.members]
    assert closure.durable_state.current_state == "present_valid"
    assert closure.durable_state.head is not None and closure.durable_state.records
    assert [row.locator for row in closure.durable_state.members]
    assert receipt.transcript_frontiers[0].state == "incomplete"

    evaluation = _evaluate(root)
    assert evaluation.status == "satisfied"
    assert evaluation.issue_ids == ()
    assert evaluation.receipt_digest == receipt.digest

    # A later valid authority change is invisible to historical verification but
    # visible to a fresh live capture.
    (root / "plan_amendments.jsonl").open("ab").write(b'{"kind": "amendment", "note": "later"}\n')
    assert _evaluate(root).status == "satisfied"
    with shot_authority_writer_fence(root) as capability:
        live = capture.capture_run_authority(
            root,
            run_root,
            run_id=_RUN,
            writer_capability=capability,
            captured_at="2036-01-01T00:00:00+00:00",
        )
    assert live.snapshot.authority_digest != receipt.authority.before.authority_digest


def test_capture_refuses_an_authority_change_between_its_snapshots(tmp_path: Path, monkeypatch) -> None:
    root = _bare_shot(tmp_path)
    run_root = _owned_run(root)
    real = capture.capture_transcript_frontiers

    def change_then_capture(*args, **kwargs):
        (root / "plan_amendments.jsonl").open("ab").write(b'{"kind": "amendment"}\n')
        return real(*args, **kwargs)

    monkeypatch.setattr(capture, "capture_transcript_frontiers", change_then_capture)
    with (
        acquire_run_owner_fence(run_root, run_id=_RUN, command="plan", owner_kind="direct"),
        shot_authority_writer_fence(root) as capability,
        pytest.raises(capture.RunInterruptionCaptureError, match="changed"),
    ):
        capture.capture_interruption_observation(
            root,
            run_root,
            run_id=_RUN,
            writer_capability=capability,
            clock=_Clock(),
        )


def test_run_without_a_receipt_has_no_evaluation(tmp_path: Path) -> None:
    root = _bare_shot(tmp_path)
    _owned_run(root)
    with pytest.raises(evaluator.InterruptionEvaluationUnavailable, match="no readable interruption receipt"):
        _evaluate(root)


def _archived(receipt: RunInterruptionReceipt, namespace: str, locator: str) -> ArchivedSourceObject:
    row = receipt.archive.object_for(namespace, locator)
    assert row is not None
    return row


def _rewrite_json(path: Path, mutate) -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    mutate(document)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


@pytest.mark.parametrize(
    ("corruption", "expected"),
    [
        ("archived_plan_pointer_bytes", {"archive_object_mismatch"}),
        ("archived_plan_pointer_removed", {"archive_object_missing"}),
        ("archived_transcript_bytes", {"archive_object_mismatch"}),
        ("archive_manifest_record", {"archive_manifest_mismatch", "archive_ref_mismatch"}),
        ("authority_observation_record", {"authority_observation_mismatch", "authority_ref_mismatch"}),
        ("transcript_frontier_record", {"transcript_frontier_mismatch", "transcript_frontier_ref_mismatch"}),
        ("owner_claim_bytes", {"owner_ref_mismatch"}),
        ("owner_claim_content", {"owner_claim_unverified", "owner_claim_mismatch", "owner_ref_mismatch"}),
    ],
)
def test_every_corrupted_source_fails_evaluation_closed(tmp_path: Path, corruption: str, expected: set[str]) -> None:
    root = _bare_shot(tmp_path)
    run_root = _owned_run(root)
    receipt = _interrupt(root, run_root)
    assert _evaluate(root).status == "satisfied"

    if corruption == "archived_plan_pointer_bytes":
        row = _archived(receipt, "shot", "plans/current.json")
        (run_root / row.archive_locator).write_bytes(b"{}\n")
    elif corruption == "archived_plan_pointer_removed":
        row = _archived(receipt, "shot", "plans/current.json")
        (run_root / row.archive_locator).unlink()
    elif corruption == "archived_transcript_bytes":
        row = _archived(receipt, "run", receipt.transcript_frontiers[0].locator)
        (run_root / row.archive_locator).write_bytes(b'{"seq": 1, "kind": "open"}\n')
    elif corruption == "archive_manifest_record":
        _rewrite_json(
            run_root / INTERRUPTION_ARCHIVE_MANIFEST_LOCATOR,
            lambda document: document.__setitem__("captured_at", "2026-09-01T00:00:00+00:00"),
        )
    elif corruption == "authority_observation_record":
        _rewrite_json(
            run_root / INTERRUPTION_AUTHORITY_OBSERVATION_LOCATOR,
            lambda document: document.__setitem__("run_id", "run-other-001"),
        )
    elif corruption == "transcript_frontier_record":
        _rewrite_json(
            run_root / transcript_frontier_record_locator(receipt.transcript_frontiers[0]),
            lambda document: document.__setitem__("byte_count", 1),
        )
    elif corruption == "owner_claim_bytes":
        # Whitespace changes the referenced bytes but not the claim's content identity.
        path = run_root / RUN_OWNER_CLAIM_LOCATOR
        path.write_bytes(path.read_bytes() + b"\n")
    elif corruption == "owner_claim_content":
        _rewrite_json(
            run_root / RUN_OWNER_CLAIM_LOCATOR,
            lambda document: document.__setitem__("command", "build"),
        )
    else:  # pragma: no cover - closed parametrization
        raise AssertionError(corruption)

    evaluation = _evaluate(root)
    assert evaluation.status == "failed"
    assert expected <= set(evaluation.issue_ids), evaluation.issue_ids


def test_tampered_receipt_has_no_evaluation(tmp_path: Path) -> None:
    root = _bare_shot(tmp_path)
    run_root = _owned_run(root)
    _interrupt(root, run_root)
    _rewrite_json(
        run_root / INTERRUPTION_RECEIPT_LOCATOR,
        lambda document: document.__setitem__("interrupted_at", "2036-01-01T00:00:00+00:00"),
    )
    with pytest.raises(evaluator.InterruptionEvaluationUnavailable, match="invalid"):
        _evaluate(root)


def test_owner_loss_receipt_verifies_prior_status_and_reconciler_manifest(tmp_path: Path) -> None:
    root = _bare_shot(tmp_path)
    run_root = _owned_run(root)
    clock = _Clock()
    with acquire_run_owner_fence(run_root, run_id=_RUN, command="plan", owner_kind="direct") as lease:
        claim = lease.claim
    running = RunStatusV2.mint(
        run_id=_RUN,
        state="running",
        updated_at=clock(),
        record_locator=RUN_OWNER_CLAIM_LOCATOR,
        selected_record=claim,
    )
    status_bytes = (json.dumps(running.as_dict(), indent=2, sort_keys=True) + "\n").encode()
    acquisition_at = clock()
    prior = PriorRunningStatusEvidence.mint(status_bytes=status_bytes, owner=claim, captured_at=clock())
    snapshot_path = run_root / prior.status_snapshot_ref.locator
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_bytes(status_bytes)
    reconciler_manifest = _manifest("run-reconciler-001", root.name)
    manifest_bytes = (json.dumps(reconciler_manifest, indent=2, sort_keys=True) + "\n").encode()
    manifest_path = run_root / RUN_OWNER_LOSS_RECONCILER_MANIFEST_LOCATOR
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_bytes(manifest_bytes)
    loss = RunOwnerLossObservation(
        run_id=_RUN,
        prior_owner_digest=claim.digest,
        reconciler_run_id="run-reconciler-001",
        reconciler_manifest_ref=RunRecordRef(
            locator=RUN_OWNER_LOSS_RECONCILER_MANIFEST_LOCATOR,
            sha256=_sha(manifest_bytes),
            record_schema=RUN_OWNER_LOSS_RECONCILER_MANIFEST_SCHEMA,
            record_digest=canonical_digest(reconciler_manifest),
        ),
        fence_locator=claim.fence_locator,
        fence_device=claim.fence_device,
        fence_inode=claim.fence_inode,
        prior_running_status=prior,
        exclusive_fence_acquired=True,
        exclusive_acquisition_observed_at=acquisition_at,
        supervisor_wait_status=None,
    )
    with shot_authority_writer_fence(root) as capability:
        captured = capture.capture_interruption_observation(
            root,
            run_root,
            run_id=_RUN,
            writer_capability=capability,
            clock=clock,
        )
    authority_ref = publish_run_record(run_root, INTERRUPTION_AUTHORITY_OBSERVATION_LOCATOR, captured.observation)
    frontier_refs = tuple(
        publish_run_record(run_root, transcript_frontier_record_locator(frontier), frontier)
        for frontier in captured.frontiers
    )
    archive_ref = publish_run_record(run_root, INTERRUPTION_ARCHIVE_MANIFEST_LOCATOR, captured.archive)
    loss_ref = publish_run_record(run_root, INTERRUPTION_OWNER_LOSS_OBSERVATION_LOCATOR, loss)
    receipt = RunInterruptionReceipt(
        run_id=_RUN,
        interruption_kind="owner_lost",
        terminalizer_kind="reconciler",
        owner=claim,
        owner_ref=_owner_ref(run_root, claim),
        owner_loss=loss,
        owner_loss_ref=loss_ref,
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
    publish_run_record(run_root, INTERRUPTION_RECEIPT_LOCATOR, receipt)

    evaluation = _evaluate(root)
    assert evaluation.status == "satisfied", evaluation.issue_ids
    assert evaluation.owner_loss_observation_digest == loss.digest

    snapshot_path.write_bytes(status_bytes.replace(b"running", b"passed"))
    tampered = _evaluate(root)
    assert tampered.status == "failed"
    assert "prior_status_snapshot_mismatch" in tampered.issue_ids
