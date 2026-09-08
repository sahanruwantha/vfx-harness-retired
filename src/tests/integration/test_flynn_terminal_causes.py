"""Native failures retain their cause through direct and inherited run ownership."""

import asyncio
import json

import flynn_agents_sdk as flynn
import pytest

from tests.integration.test_judgment_debt_public_pipeline import _public_fixture_root
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration import run_owner_boundary


@pytest.mark.parametrize("command", ["plan", "build", "accept"])
@pytest.mark.parametrize("failure_type,cause,code", [
    (flynn.BudgetExhausted, "model_budget_exhausted", 3),
    (flynn.InferenceFailure, "model_session_failure", 3),
    (flynn.InferenceRejected, "model_session_failure", 3),
    (asyncio.CancelledError, "cancelled_without_intent", 130),
    (flynn.ContractError, "process_error", 1),
    (flynn.ProposalRejected, "process_error", 1),
])
def test_native_failure_retains_cause_without_granting_recovery(
    tmp_path, monkeypatch, command, failure_type, cause, code,
):
    shot = _public_fixture_root(tmp_path)
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    monkeypatch.setenv("VFXH_RUN_ID", "native-failure")
    failure = failure_type("injected native failure")
    with pytest.raises(failure_type) as raised, run_owner_boundary.invocation(shot, command) as layout:
        raise failure

    assert raised.value is failure
    summary = json.loads((layout.reports / "summary.json").read_text())
    status = json.loads(layout.status.read_text())
    assert (summary["state"], summary["terminal_cause"], summary["exit_code"]) == (
        "failed", cause, code,
    )
    assert (status["state"], status["exit_code"]) == ("failed", code)
    envelope = layout.read_terminal_stop()
    # An exception alone still lacks the phase receipt required for recovery.
    assert envelope.stop_class == "harness_defect"
    assert envelope.retryable is False
    assert not (layout.reports / "interruption-receipt.json").exists()
    audit = json.loads((layout.reports / "unclassified-boundary-audit.json").read_text())
    assert audit["exception_message"] == str(failure)


def test_inherited_native_failure_keeps_original_usage_and_root_terminalization(tmp_path, monkeypatch):
    shot = _public_fixture_root(tmp_path)
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    monkeypatch.setenv("VFXH_RUN_ID", "native-inherited-failure")
    usage = flynn.InferenceUsage(
        kind="model", status=flynn.UsageStatus.UNKNOWN, request_started=True,
        provider="fixture", model="fixture-model", input_tokens=11,
    )
    failure = flynn.InferenceFailure("response lost", usage=usage)
    with pytest.raises(flynn.InferenceFailure) as raised, run_owner_boundary.invocation(shot, "plan") as root:
        running = root.status.read_bytes()
        with pytest.raises(flynn.InferenceFailure), run_owner_boundary.invocation(shot, "build") as child:
            raise failure
        assert child.root == root.root
        assert root.status.read_bytes() == running
        assert root.stop_envelope.is_file()
        published_stop = root.stop_envelope.read_bytes()
        published_audit = (root.reports / "unclassified-boundary-audit.json").read_bytes()
        assert failure.stop_envelope.digest == root.read_stop_envelope().digest
        raise failure

    assert raised.value is failure
    assert raised.value.usage is usage
    summary = json.loads((root.reports / "summary.json").read_text())
    assert summary["terminal_cause"] == "model_session_failure"
    assert json.loads(root.status.read_text())["state"] == "failed"
    assert root.stop_envelope.read_bytes() == published_stop
    assert (root.reports / "unclassified-boundary-audit.json").read_bytes() == published_audit
    assert root.read_terminal_stop().digest == failure.stop_envelope.digest


def test_explicit_phase_budget_cause_remains_authoritative():
    class PlanningBudget(flynn.BudgetExhausted):
        terminal_cause = "plan_budget_exhausted"

    assert run_artifacts.terminal_record(PlanningBudget("planning spent"))[2] == "plan_budget_exhausted"


def test_native_cancellation_does_not_invent_signal_intent_or_drop_usage():
    usage = flynn.InferenceUsage(
        kind="model", status=flynn.UsageStatus.KNOWN, request_started=True,
        provider="fixture", model="fixture-model", input_tokens=7, output_tokens=3,
    )
    failure = flynn.InferenceCancelled(usage)
    assert run_artifacts.terminal_record(failure)[:3] == (
        "failed", 130, "cancelled_without_intent",
    )
    assert failure.usage is usage
