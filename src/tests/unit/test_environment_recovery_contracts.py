"""Pure environment probe-spec and recovery-commit contract tests."""

from __future__ import annotations

import hashlib
from copy import deepcopy

import pytest

from vfx_harness.domain.environment_recovery import EnvironmentRecoveryCommit
from vfx_harness.domain.environment_results import (
    EnvironmentCheck,
    EnvironmentProbeSpec,
    EnvironmentResult,
)
from vfx_harness.domain.stop_transaction_state import (
    EnvironmentResultAssertion,
    StopEvidenceRef,
)
from vfx_harness.domain.stop_transactions import (
    EnvironmentReverified,
    RecoverEnvironmentTarget,
    StopAction,
)
from vfx_harness.domain.transaction_receipts import TransactionReceipt


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _result() -> EnvironmentResult:
    return EnvironmentResult(
        probe_id="preflight",
        checks=(
            EnvironmentCheck(
                check_id="credential_configuration",
                passed=True,
                observed_digest=_digest("credential observation"),
                expected="A valid credential is selected.",
                found="The credential selection is valid.",
                next_action="No credential recovery is required.",
            ),
            EnvironmentCheck(
                check_id="blender_executable",
                passed=True,
                observed_digest=_digest("Blender observation"),
                expected="Blender resolves.",
                found="Blender resolves to /usr/bin/blender.",
                next_action="No Blender recovery is required.",
            ),
        ),
    )


def _after_ref(result: EnvironmentResult) -> StopEvidenceRef:
    return StopEvidenceRef(
        kind="environment_result",
        locator=f"state/recovery/environment/results/{result.digest}.json",
        sha256=_digest("result bytes"),
        record_schema=result.SCHEMA,
        record_digest=result.digest,
    )


def _failed_result() -> EnvironmentResult:
    passing = _result()
    return EnvironmentResult(
        probe_id=passing.probe_id,
        probe_spec=passing.probe_spec,
        checks=tuple(
            EnvironmentCheck(
                check_id=check.check_id,
                passed=False if check.check_id == "credential_configuration" else check.passed,
                observed_digest=_digest(f"failed:{check.check_id}"),
                expected=check.expected,
                found=(
                    "No credential is selected."
                    if check.check_id == "credential_configuration"
                    else check.found
                ),
                next_action=check.next_action,
            )
            for check in passing.checks
        ),
    )


def _running_attempt() -> tuple[StopAction, TransactionReceipt, EnvironmentResult, StopEvidenceRef]:
    before = _failed_result()
    before_ref = StopEvidenceRef(
        kind="environment_result",
        locator="runs/failed/reports/preflight-environment-result.json",
        sha256=_digest("before bytes"),
        record_schema=before.SCHEMA,
        record_digest=before.digest,
    )
    assert before.probe_spec is not None
    assertion = EnvironmentResultAssertion(
        probe_id=before.probe_id,
        probe_spec_digest=before.probe_spec.digest,
        result_digest=before.digest,
    )
    target = RecoverEnvironmentTarget(
        environment=assertion,
        failed_check_ids=("credential_configuration",),
        recovery_adapter_id="external_operator",
        evidence=(before_ref,),
    )
    action = StopAction(
        target=target,
        postcondition=EnvironmentReverified(
            probe_id=assertion.probe_id,
            probe_spec_digest=assertion.probe_spec_digest,
            before_result_digest=assertion.result_digest,
            failed_check_ids=target.failed_check_ids,
        ),
    )
    after_ref = _after_ref(_result())
    running = TransactionReceipt.prepare(
        action,
        adapter_id="external_operator",
        authoritative_before_digest=before.environment_digest,
        attempt_evidence_digest=before.digest,
        recovery_disposition="repeatable_read_only",
    ).running_successor(additional_operation_refs=(after_ref,))
    return action, running, before, before_ref


def test_probe_spec_is_exact_sorted_and_strict() -> None:
    result = _result()
    spec = result.probe_spec

    assert spec.check_ids == ("blender_executable", "credential_configuration")
    assert EnvironmentProbeSpec.from_dict(spec.as_dict(), "probe spec") == spec

    stale = deepcopy(spec.as_dict())
    stale["check_ids"].append("runtime_configuration")
    with pytest.raises(ValueError, match="probe_spec_digest is stale"):
        EnvironmentProbeSpec.from_dict(stale, "probe spec")


def test_environment_recovery_commit_round_trips_and_binds_changed_state() -> None:
    result = _result()
    commit = EnvironmentRecoveryCommit(
        idempotency_key=_digest("key"),
        action_digest=_digest("action"),
        postcondition_digest=_digest("postcondition"),
        running_receipt_revision=2,
        running_receipt_digest=_digest("running receipt"),
        probe_id=result.probe_id,
        probe_spec_digest=result.probe_spec.digest,
        before_result_digest=_digest("failed result"),
        before_environment_digest=_digest("failed environment"),
        failed_check_ids=("credential_configuration",),
        after_result=_after_ref(result),
        after_environment_digest=result.environment_digest,
    )

    assert EnvironmentRecoveryCommit.from_dict(commit.as_dict(), "commit") == commit

    stale = deepcopy(commit.as_dict())
    stale["after_environment_digest"] = _digest("substituted environment")
    with pytest.raises(ValueError, match="commit_digest is stale"):
        EnvironmentRecoveryCommit.from_dict(stale, "commit")

    with pytest.raises(ValueError, match="must change authoritative environment state"):
        EnvironmentRecoveryCommit(
            idempotency_key=commit.idempotency_key,
            action_digest=commit.action_digest,
            postcondition_digest=commit.postcondition_digest,
            running_receipt_revision=commit.running_receipt_revision,
            running_receipt_digest=commit.running_receipt_digest,
            probe_id=commit.probe_id,
            probe_spec_digest=commit.probe_spec_digest,
            before_result_digest=commit.before_result_digest,
            before_environment_digest=commit.before_environment_digest,
            failed_check_ids=commit.failed_check_ids,
            after_result=commit.after_result,
            after_environment_digest=commit.before_environment_digest,
        )


def test_environment_recovery_commit_rejects_non_environment_result_evidence() -> None:
    result = _result()
    wrong = StopEvidenceRef(
        kind="authority_record",
        locator="state/recovery/environment/results/wrong.json",
        sha256=_digest("wrong bytes"),
        record_schema="vfx-harness.wrong/v1",
        record_digest=_digest("wrong record"),
    )

    with pytest.raises(ValueError, match="must cite an exact environment result"):
        EnvironmentRecoveryCommit(
            idempotency_key=_digest("key"),
            action_digest=_digest("action"),
            postcondition_digest=_digest("postcondition"),
            running_receipt_revision=2,
            running_receipt_digest=_digest("running receipt"),
            probe_id=result.probe_id,
            probe_spec_digest=result.probe_spec.digest,
            before_result_digest=_digest("failed result"),
            before_environment_digest=_digest("failed environment"),
            failed_check_ids=("credential_configuration",),
            after_result=wrong,
            after_environment_digest=result.environment_digest,
        )


def test_environment_recovery_commit_factory_joins_action_receipt_and_results() -> None:
    action, running, before, before_ref = _running_attempt()
    after = _result()
    after_ref = _after_ref(after)

    commit = EnvironmentRecoveryCommit.create(
        action=action,
        running_receipt=running,
        before_result=before,
        before_result_ref=before_ref,
        after_result=after,
        after_result_ref=after_ref,
    )
    commit.assert_matches(
        action=action,
        running_receipt=running,
        before_result=before,
        before_result_ref=before_ref,
        after_result=after,
        after_result_ref=after_ref,
    )
    assert commit.running_receipt_digest == running.digest
    assert commit.after_environment_digest == after.environment_digest

    with pytest.raises(ValueError, match="fully passing"):
        EnvironmentRecoveryCommit.create(
            action=action,
            running_receipt=running,
            before_result=before,
            before_result_ref=before_ref,
            after_result=before,
            after_result_ref=before_ref,
        )


def test_environment_recovery_commit_factory_rejects_unsealed_receipt_joins() -> None:
    action, _running, before, before_ref = _running_attempt()
    after = _result()
    after_ref = _after_ref(after)

    wrong_adapter = TransactionReceipt.prepare(
        action,
        adapter_id="another_adapter",
        authoritative_before_digest=before.environment_digest,
        attempt_evidence_digest=before.digest,
        recovery_disposition="repeatable_read_only",
    ).running_successor(additional_operation_refs=(after_ref,))
    with pytest.raises(ValueError, match="another recovery adapter"):
        EnvironmentRecoveryCommit.create(
            action=action,
            running_receipt=wrong_adapter,
            before_result=before,
            before_result_ref=before_ref,
            after_result=after,
            after_result_ref=after_ref,
        )

    wrong_attempt = TransactionReceipt.prepare(
        action,
        adapter_id="external_operator",
        authoritative_before_digest=before.environment_digest,
        attempt_evidence_digest=_digest("another failed observation"),
        recovery_disposition="repeatable_read_only",
    ).running_successor(additional_operation_refs=(after_ref,))
    with pytest.raises(ValueError, match="exact recovery target"):
        EnvironmentRecoveryCommit.create(
            action=action,
            running_receipt=wrong_attempt,
            before_result=before,
            before_result_ref=before_ref,
            after_result=after,
            after_result_ref=after_ref,
        )

    unrecorded = TransactionReceipt.prepare(
        action,
        adapter_id="external_operator",
        authoritative_before_digest=before.environment_digest,
        attempt_evidence_digest=before.digest,
        recovery_disposition="repeatable_read_only",
    ).running_successor()
    with pytest.raises(ValueError, match="durably recorded receipt observation"):
        EnvironmentRecoveryCommit.create(
            action=action,
            running_receipt=unrecorded,
            before_result=before,
            before_result_ref=before_ref,
            after_result=after,
            after_result_ref=after_ref,
        )
