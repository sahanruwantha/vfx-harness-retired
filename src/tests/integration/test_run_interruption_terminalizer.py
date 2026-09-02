"""The terminal commit path selects an interrupted status exactly once (HIR-0172)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from tests.integration.test_run_interruption_evaluator import (
    _RUN,
    _bare_shot,
    _Clock,
    _owned_run,
)
from tests.run_owner_support import now
from vfx_harness.domain.run_interruption_records import INTERRUPTION_RECEIPT_LOCATOR
from vfx_harness.domain.run_signal_intent import RecordedSignalIntent
from vfx_harness.domain.run_status import INTERRUPTION_RECEIPT_EVALUATION_LOCATOR
from vfx_harness.evaluation import run_interruption as evaluator
from vfx_harness.observability.run_owner_fence import acquire_run_owner_fence
from vfx_harness.orchestration import run_terminalizer as terminalizer


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _document(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _lease(run_root: Path):
    return acquire_run_owner_fence(run_root, run_id=_RUN, command="plan", owner_kind="direct")


def _intent(kind: str) -> RecordedSignalIntent:
    """A test-minted intent; production mints one only inside the signal handler."""
    return RecordedSignalIntent(
        kind, {"operator_interrupt": 2, "termination_request": 15}[kind], now()
    )


def test_terminalizer_commits_an_interrupted_status_exactly_once(tmp_path: Path) -> None:
    root = _bare_shot(tmp_path)
    run_root = _owned_run(root)
    clock = _Clock()
    with _lease(run_root) as lease:
        running = terminalizer.publish_running_status(run_root, lease=lease, updated_at=clock())
        assert _document(run_root / "status.json")["state"] == "running"
        assert _document(root / "runs" / "latest.json")["state"] == "running"

        result = terminalizer.terminalize_interruption(
            root,
            run_root,
            lease=lease,
            intent=_intent("operator_interrupt"),
            clock=clock,
        )

        status_bytes = (run_root / "status.json").read_bytes()
        assert _sha(status_bytes) == result.status_sha256
        status = _document(run_root / "status.json")
        assert status["state"] == "interrupted"
        assert status["exit_code"] == 130
        assert status["owner_claim_digest"] == lease.claim.digest
        assert status["interruption_receipt_digest"] == result.receipt.digest
        assert status["interruption_evaluation_digest"] == result.evaluation.digest
        assert status["stop_envelope"] is None
        assert status["updated_at"] >= running.updated_at
        latest = _document(root / "runs" / "latest.json")
        assert (latest["run_id"], latest["state"]) == (_RUN, "interrupted")
        summary = _document(run_root / "reports" / "summary.json")
        assert summary["schema"] == terminalizer.INTERRUPTION_SUMMARY_SCHEMA
        assert summary["legal_transactions"] == []
        assert summary["dispatch_authority"] is False
        inventory = _document(run_root / "artifacts.json")
        assert INTERRUPTION_RECEIPT_LOCATOR in {row["path"] for row in inventory["artifacts"]}
        assert INTERRUPTION_RECEIPT_EVALUATION_LOCATOR in {row["path"] for row in inventory["artifacts"]}

        with pytest.raises(terminalizer.RunTerminalizationConflict, match="exactly once"):
            terminalizer.terminalize_interruption(
                root,
                run_root,
                lease=lease,
                intent=_intent("operator_interrupt"),
                clock=clock,
            )
        assert (run_root / "status.json").read_bytes() == status_bytes

    verified = evaluator.read_interrupted_run(root, _RUN, verified_at=clock())
    assert verified.status == result.status
    assert verified.receipt == result.receipt
    assert verified.evaluation == result.evaluation
    assert verified.verification.status == "satisfied"
    assert verified.legal_transactions == ()
    assert (verified.retryable, verified.resume_authorized, verified.dispatch_authority) == (False, False, False)


def test_unsatisfied_evaluation_leaves_the_run_running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _bare_shot(tmp_path)
    run_root = _owned_run(root)
    clock = _Clock()
    real = evaluator.evaluate_interruption_receipt

    def failed(shot_root, run_id, *, evaluated_at):
        evaluation = real(shot_root, run_id, evaluated_at=evaluated_at)
        return replace(evaluation, status="failed", issue_ids=("authority_changed",))

    monkeypatch.setattr(terminalizer.interruption_evaluation, "evaluate_interruption_receipt", failed)
    with _lease(run_root) as lease:
        terminalizer.publish_running_status(run_root, lease=lease, updated_at=clock())
        running_bytes = (run_root / "status.json").read_bytes()
        with pytest.raises(terminalizer.RunTerminalizationConflict, match="authority_changed"):
            terminalizer.terminalize_interruption(
                root,
                run_root,
                lease=lease,
                intent=_intent("termination_request"),
                clock=clock,
            )
    assert (run_root / "status.json").read_bytes() == running_bytes
    assert (run_root / INTERRUPTION_RECEIPT_LOCATOR).is_file()
    assert not (run_root / INTERRUPTION_RECEIPT_EVALUATION_LOCATOR).exists()
    assert _document(root / "runs" / "latest.json")["state"] == "running"
    with pytest.raises(evaluator.InterruptionAuthorityUnavailable, match="not an interrupted terminal status"):
        evaluator.read_interrupted_run(root, _RUN, verified_at=clock())


def test_death_before_status_selection_keeps_running_and_refuses_a_second_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _bare_shot(tmp_path)
    run_root = _owned_run(root)
    clock = _Clock()

    class _Death(RuntimeError):
        pass

    def die_before_selection(event: str) -> None:
        if event == "before_status_selection":
            raise _Death(event)

    monkeypatch.setattr(terminalizer, "_terminal_write_boundary", die_before_selection)
    with _lease(run_root) as lease:
        terminalizer.publish_running_status(run_root, lease=lease, updated_at=clock())
        running_bytes = (run_root / "status.json").read_bytes()
        with pytest.raises(_Death):
            terminalizer.terminalize_interruption(
                root,
                run_root,
                lease=lease,
                intent=_intent("operator_interrupt"),
                clock=clock,
            )
        assert (run_root / "status.json").read_bytes() == running_bytes
        assert (run_root / INTERRUPTION_RECEIPT_LOCATOR).is_file()
        assert (run_root / INTERRUPTION_RECEIPT_EVALUATION_LOCATOR).is_file()
        monkeypatch.undo()
        with pytest.raises(terminalizer.RunTerminalizationConflict, match="already exists"):
            terminalizer.terminalize_interruption(
                root,
                run_root,
                lease=lease,
                intent=_intent("operator_interrupt"),
                clock=clock,
            )
    assert (run_root / "status.json").read_bytes() == running_bytes


def test_reader_refuses_a_committed_run_whose_archive_no_longer_verifies(tmp_path: Path) -> None:
    root = _bare_shot(tmp_path)
    run_root = _owned_run(root)
    clock = _Clock()
    with _lease(run_root) as lease:
        terminalizer.publish_running_status(run_root, lease=lease, updated_at=clock())
        result = terminalizer.terminalize_interruption(
            root,
            run_root,
            lease=lease,
            intent=_intent("operator_interrupt"),
            clock=clock,
        )
    archived = result.receipt.archive.object_for("shot", "plans/current.json")
    assert archived is not None
    (run_root / archived.archive_locator).write_bytes(b"{}\n")
    with pytest.raises(evaluator.InterruptionAuthorityUnavailable, match="no longer verifies"):
        evaluator.read_interrupted_run(root, _RUN, verified_at=clock())


def test_terminalizer_requires_a_v2_running_status_and_an_owned_kind(tmp_path: Path) -> None:
    root = _bare_shot(tmp_path)
    run_root = _owned_run(root)
    clock = _Clock()
    with _lease(run_root) as lease:
        with pytest.raises(terminalizer.RunTerminalizationConflict, match="no readable status"):
            terminalizer.terminalize_interruption(
                root,
                run_root,
                lease=lease,
                intent=_intent("operator_interrupt"),
                clock=clock,
            )
        (run_root / "status.json").write_text(
            json.dumps({"schema": "vfx-harness.run/v1", "run_id": _RUN, "state": "running"}) + "\n",
            encoding="utf-8",
        )
        with pytest.raises(terminalizer.RunTerminalizationConflict, match="v2 running status"):
            terminalizer.terminalize_interruption(
                root,
                run_root,
                lease=lease,
                intent=_intent("operator_interrupt"),
                clock=clock,
            )
        terminalizer.publish_running_status(run_root, lease=lease, updated_at=clock())
        with pytest.raises(terminalizer.RunTerminalizationConflict, match="reconciler"):
            terminalizer.terminalize_interruption(
                root,
                run_root,
                lease=lease,
                intent="owner_lost",
                clock=clock,
            )
    assert not (run_root / INTERRUPTION_RECEIPT_LOCATOR).exists()
