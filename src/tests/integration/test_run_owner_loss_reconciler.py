"""Owner loss is proven by acquiring the exact recorded fence, never inferred (HIR-0172)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.integration.test_run_interruption_evaluator import _bare_shot
from tests.run_owner_support import MonotonicClock, owned_run, pass_run
from vfx_harness.domain.run_interruption_records import INTERRUPTION_RECEIPT_LOCATOR
from vfx_harness.evaluation import run_interruption as evaluator
from vfx_harness.observability import run_artifacts
from vfx_harness.observability.run_owner_fence import acquire_run_owner_fence
from vfx_harness.orchestration import run_owner_loss_reconciler as reconciler
from vfx_harness.orchestration import run_terminalizer

_TARGET = "orphan-001"


def _document(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _orphan_running_run(root: Path, run_id: str = _TARGET) -> run_artifacts.RunLayout:
    """Own a run, publish running, and release the fence without any terminal status."""

    layout = run_artifacts.create(root, run_id, command="build")
    with acquire_run_owner_fence(layout.root, run_id=run_id, command="build", owner_kind="direct") as lease:
        run_terminalizer.publish_running_status(layout.root, lease=lease, updated_at=MonotonicClock()())
    return layout


def _reconcile(root: Path, run_id: str = _TARGET, reconciler_id: str = "reconciler-001"):
    with owned_run(root, reconciler_id, command="reconcile") as (layout, lease):
        result = reconciler.reconcile_lost_run(root, run_id, reconciler=layout, clock=MonotonicClock())
        pass_run(layout, lease, command="reconcile", extra={"reconciliation": result.as_dict()})
    return result


def test_live_owner_is_left_untouched(tmp_path: Path) -> None:
    root = _bare_shot(tmp_path)
    layout = run_artifacts.create(root, _TARGET, command="build")
    with acquire_run_owner_fence(layout.root, run_id=_TARGET, command="build", owner_kind="direct") as lease:
        run_terminalizer.publish_running_status(layout.root, lease=lease, updated_at=MonotonicClock()())
        running_bytes = layout.status.read_bytes()
        result = _reconcile(root)
        assert result.disposition == "owner_live"
        assert result.receipt is None
        assert layout.status.read_bytes() == running_bytes
        assert not (layout.root / INTERRUPTION_RECEIPT_LOCATOR).exists()


def test_released_fence_proves_owner_loss_and_reconciles_exactly_once(tmp_path: Path) -> None:
    root = _bare_shot(tmp_path)
    layout = _orphan_running_run(root)
    assert _document(layout.status)["state"] == "running"

    result = _reconcile(root)

    assert result.disposition == "owner_lost"
    assert result.receipt is not None
    assert result.receipt.interruption_kind == "owner_lost"
    assert result.receipt.terminalizer_kind == "reconciler"
    assert result.receipt.owner_loss is not None
    assert result.receipt.owner_loss.reconciler_run_id == "reconciler-001"
    status = _document(layout.status)
    assert (status["state"], status["exit_code"]) == ("interrupted", None)
    assert status["interruption_receipt_digest"] == result.receipt.digest
    verified = evaluator.read_interrupted_run(root, _TARGET, verified_at="2036-01-01T00:00:00+00:00")
    assert verified.receipt == result.receipt
    assert verified.legal_transactions == ()
    assert (root / "runs" / _TARGET / "reports" / "reconciler-manifest.json").is_file()
    assert _document(root / "runs" / "latest.json")["run_id"] == "reconciler-001"

    status_bytes = layout.status.read_bytes()
    repeated = _reconcile(root, reconciler_id="reconciler-002")
    assert repeated.disposition == "already_terminal"
    assert repeated.receipt == result.receipt
    assert layout.status.read_bytes() == status_bytes


def test_prepared_receipt_is_completed_without_changing_its_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _bare_shot(tmp_path)
    layout = run_artifacts.create(root, _TARGET, command="build")

    class _Death(RuntimeError):
        pass

    def die_before_selection(event: str) -> None:
        if event == "before_status_selection":
            raise _Death(event)

    monkeypatch.setattr(run_terminalizer, "_terminal_write_boundary", die_before_selection)
    with acquire_run_owner_fence(layout.root, run_id=_TARGET, command="build", owner_kind="direct") as lease:
        run_terminalizer.publish_running_status(layout.root, lease=lease, updated_at=MonotonicClock()())
        with pytest.raises(_Death):
            run_terminalizer.terminalize_interruption(
                root,
                layout.root,
                lease=lease,
                interruption_kind="operator_interrupt",
                clock=MonotonicClock(),
            )
    monkeypatch.undo()
    receipt_bytes = (layout.root / INTERRUPTION_RECEIPT_LOCATOR).read_bytes()
    assert _document(layout.status)["state"] == "running"

    result = _reconcile(root)

    assert result.disposition == "completed_prepared"
    assert result.receipt is not None
    assert result.receipt.interruption_kind == "operator_interrupt"
    assert (layout.root / INTERRUPTION_RECEIPT_LOCATOR).read_bytes() == receipt_bytes
    status = _document(layout.status)
    assert (status["state"], status["exit_code"]) == ("interrupted", 130)
    verified = evaluator.read_interrupted_run(root, _TARGET, verified_at="2036-01-01T00:00:00+00:00")
    assert verified.receipt == result.receipt


def test_terminal_and_ownerless_runs_are_not_reconciled(tmp_path: Path) -> None:
    root = _bare_shot(tmp_path)
    with owned_run(root, _TARGET, command="build") as (layout, lease):
        pass_run(layout, lease, command="build")
    passed_bytes = layout.status.read_bytes()
    result = _reconcile(root)
    assert (result.disposition, result.receipt) == ("already_terminal", None)
    assert layout.status.read_bytes() == passed_bytes

    legacy = run_artifacts.create(root, "legacy-001", command="build")
    (legacy.root / "status.json").write_text(
        json.dumps({"schema": "vfx-harness.run/v1", "run_id": "legacy-001", "state": "running"}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(reconciler.RunOwnerLossReconciliationError, match="not reconcilable"):
        _reconcile(root, run_id="legacy-001", reconciler_id="reconciler-003")
