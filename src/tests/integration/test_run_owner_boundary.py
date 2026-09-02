"""The root-owner boundary owns one v2 run per invocation and terminalizes it (HIR-0172)."""

from __future__ import annotations

import json
import os
import signal
from pathlib import Path

import pytest

from tests.integration.test_judgment_debt_public_pipeline import _public_fixture_root
from vfx_harness.domain.run_interruption_records import INTERRUPTION_RECEIPT_LOCATOR
from vfx_harness.domain.run_status import RUN_STATUS_SCHEMA
from vfx_harness.evaluation import run_interruption as evaluator
from vfx_harness.observability import run_artifacts
from vfx_harness.observability.run_owner_fence import read_run_owner_claim
from vfx_harness.orchestration import run_interruption_capture, run_owner_boundary


def _document(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def shot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = _public_fixture_root(tmp_path)
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    monkeypatch.setenv("VFXH_RUN_ID", "boundary-001")
    return root


def test_direct_invocation_owns_a_v2_run_and_selects_its_summary(shot: Path) -> None:
    with run_owner_boundary.invocation(shot, "plan", shot_id=shot.name) as layout:
        assert _document(layout.manifest)["schema"] == "vfx-harness.run/v2"
        running = _document(layout.status)
        assert (running["schema"], running["state"]) == (RUN_STATUS_SCHEMA, "running")
        claim = read_run_owner_claim(layout.root, run_id=layout.run_id)
        assert running["owner_claim_digest"] == claim.digest
        assert _document(shot / "runs" / "latest.json")["state"] == "running"
        layout.terminal_metadata["outcome"] = "clean"

    status = _document(layout.status)
    summary = _document(layout.reports / "summary.json")
    assert (status["state"], status["exit_code"]) == ("passed", 0)
    assert status["summary"] == "reports/summary.json"
    assert (summary["state"], summary["command"], summary["outcome"]) == ("passed", "plan", "clean")
    assert _document(shot / "runs" / "latest.json")["state"] == "passed"
    assert layout.inventory.is_file()


def test_typed_stop_selects_a_failed_status_and_reads_back(shot: Path) -> None:
    class _Boom(RuntimeError):
        pass

    with pytest.raises(_Boom), run_owner_boundary.invocation(shot, "build", shot_id=shot.name) as layout:
        raise _Boom("worker died")

    status = _document(layout.status)
    assert (status["state"], status["exit_code"]) == ("failed", 1)
    assert status["stop_envelope"] == "reports/stop-envelope.json"
    envelope = layout.read_terminal_stop()
    assert envelope.digest == status["stop_envelope_digest"]
    summary = _document(layout.reports / "summary.json")
    assert (summary["state"], summary["stop_class"]) == ("failed", envelope.stop_class)
    assert _document(shot / "runs" / "latest.json")["state"] == "failed"


def test_sigint_records_intent_and_terminalizes_an_interruption(shot: Path) -> None:
    with (
        pytest.raises(run_owner_boundary.RunInterrupted) as raised,
        run_owner_boundary.invocation(shot, "plan", shot_id=shot.name) as layout,
    ):
        (layout.logs / "transcripts" / "plan").mkdir(parents=True)
        (layout.logs / "transcripts" / "plan" / "session.jsonl").write_bytes(
            b'{"seq": 1, "dt": 0.0, "kind": "open"}\n'
        )
        os.kill(os.getpid(), signal.SIGINT)
        raise AssertionError("the signal handler must interrupt the body")  # pragma: no cover

    assert raised.value.code == 130
    status = _document(layout.status)
    assert (status["state"], status["exit_code"]) == ("interrupted", 130)
    assert (layout.root / INTERRUPTION_RECEIPT_LOCATOR).is_file()
    verified = evaluator.read_interrupted_run(shot, layout.run_id, verified_at="2036-01-01T00:00:00+00:00")
    assert verified.receipt.interruption_kind == "operator_interrupt"
    assert verified.receipt.transcript_frontiers[0].state == "incomplete"
    assert verified.legal_transactions == ()
    assert _document(shot / "runs" / "latest.json")["state"] == "interrupted"
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


def test_repeated_signals_converge_on_one_receipt(shot: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = run_interruption_capture.capture_transcript_frontiers
    deliveries: list[int] = []

    def deliver_again(*args, **kwargs):
        os.kill(os.getpid(), signal.SIGTERM)
        deliveries.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(run_interruption_capture, "capture_transcript_frontiers", deliver_again)
    with (
        pytest.raises(run_owner_boundary.RunInterrupted) as raised,
        run_owner_boundary.invocation(shot, "plan", shot_id=shot.name) as layout,
    ):
        os.kill(os.getpid(), signal.SIGTERM)

    assert raised.value.code == 143
    assert deliveries == [1]
    status = _document(layout.status)
    assert (status["state"], status["exit_code"]) == ("interrupted", 143)
    verified = evaluator.read_interrupted_run(shot, layout.run_id, verified_at="2036-01-01T00:00:00+00:00")
    assert verified.receipt.interruption_kind == "termination_request"


def test_cancellation_without_recorded_intent_is_a_failure(shot: Path) -> None:
    with pytest.raises(KeyboardInterrupt), run_owner_boundary.invocation(shot, "plan", shot_id=shot.name) as layout:
        raise KeyboardInterrupt

    status = _document(layout.status)
    assert (status["state"], status["exit_code"]) == ("failed", 130)
    assert _document(layout.reports / "summary.json")["terminal_cause"] == "cancelled_without_intent"
    assert not (layout.root / INTERRUPTION_RECEIPT_LOCATOR).exists()


def test_inherited_stage_publishes_only_its_typed_stop(shot: Path) -> None:
    class _StageFailure(RuntimeError):
        pass

    with run_owner_boundary.invocation(shot, "plan", shot_id=shot.name) as root_layout:
        running_bytes = root_layout.status.read_bytes()
        with pytest.raises(_StageFailure), run_owner_boundary.invocation(shot, "build") as inherited:
            raise _StageFailure("stage failed inside the driver")
        assert inherited.root == root_layout.root
        assert inherited.stop_envelope.is_file()
        assert root_layout.status.read_bytes() == running_bytes
    assert _document(root_layout.status)["state"] == "passed"


def test_prepared_run_has_no_status_until_owned(shot: Path) -> None:
    layout = run_artifacts.create(shot, "prepared-001", command="build")
    assert not layout.status.exists()
    assert run_artifacts.latest(shot) is None
    with pytest.raises(ValueError, match="owner manifest"):
        run_artifacts.create(shot, "prepared-002", command="mystery")
