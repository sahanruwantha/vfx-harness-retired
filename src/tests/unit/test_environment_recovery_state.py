"""Durable environment recovery result, commit, and evaluation state."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from vfx_harness.domain.environment_recovery import EnvironmentRecoveryCommit
from vfx_harness.domain.environment_results import EnvironmentCheck, EnvironmentResult
from vfx_harness.domain.stop_transaction_state import StopEvidenceRef
from vfx_harness.domain.stop_transactions import PostconditionEvaluation
from vfx_harness.orchestration import environment_recovery_state


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _result() -> EnvironmentResult:
    return EnvironmentResult(
        probe_id="preflight",
        checks=(
            EnvironmentCheck(
                check_id="credential_configuration",
                passed=True,
                observed_digest=_digest("credential"),
                expected="A selected credential.",
                found="The credential is selected.",
                next_action="No recovery is required.",
            ),
            EnvironmentCheck(
                check_id="runtime_configuration",
                passed=True,
                observed_digest=_digest("configuration"),
                expected="Valid runtime configuration.",
                found="The runtime configuration is valid.",
                next_action="No recovery is required.",
            ),
        ),
    )


def _receipt_ref(key: str) -> StopEvidenceRef:
    return StopEvidenceRef(
        kind="transaction_receipt",
        locator=f"state/transactions/{key}/receipts/{_digest('receipt')}.json",
        sha256=_digest("receipt bytes"),
        record_schema="vfx-harness.transaction-receipt/v1",
        record_digest=_digest("receipt"),
    )


def test_environment_recovery_state_is_immutable_and_round_trips(tmp_path: Path) -> None:
    result = _result()
    result_ref = environment_recovery_state.publish_environment_result(tmp_path, result)
    assert environment_recovery_state.read_environment_result(tmp_path, result_ref) == result

    key = _digest("transaction key")
    commit = EnvironmentRecoveryCommit(
        idempotency_key=key,
        action_digest=_digest("action"),
        postcondition_digest=_digest("postcondition"),
        running_receipt_revision=2,
        running_receipt_digest=_digest("running receipt"),
        probe_id=result.probe_id,
        probe_spec_digest=result.probe_spec.digest,
        before_result_digest=_digest("failed result"),
        before_environment_digest=_digest("failed environment"),
        failed_check_ids=("credential_configuration",),
        after_result=result_ref,
        after_environment_digest=result.environment_digest,
    )
    commit_ref = environment_recovery_state.publish_environment_commit(tmp_path, commit)
    assert commit_ref.kind == "authority_record"
    assert environment_recovery_state.read_environment_commit(tmp_path, key) == commit
    assert environment_recovery_state.publish_environment_commit(tmp_path, commit) == commit_ref

    evaluation = PostconditionEvaluation(
        action_digest=commit.action_digest,
        postcondition_digest=commit.postcondition_digest,
        idempotency_key=key,
        evaluator_id="environment_reverification",
        authoritative_before_digest=commit.before_environment_digest,
        authoritative_after_digest=commit.after_environment_digest,
        observed_records=(_receipt_ref(key), commit_ref, result_ref),
        result="satisfied",
    )
    evaluation_ref = environment_recovery_state.publish_postcondition_evaluation(
        tmp_path, evaluation
    )
    assert evaluation_ref.kind == "authority_record"
    assert environment_recovery_state.read_postcondition_evaluation(tmp_path, key) == evaluation


def test_environment_recovery_queries_do_not_create_state(tmp_path: Path) -> None:
    key = _digest("absent key")

    assert environment_recovery_state.read_environment_commit(tmp_path, key) is None
    assert environment_recovery_state.read_postcondition_evaluation(tmp_path, key) is None
    assert not (tmp_path / "state").exists()


def test_environment_recovery_state_rejects_tampering_and_symlink_roots(
    tmp_path: Path,
) -> None:
    result = _result()
    result_ref = environment_recovery_state.publish_environment_result(tmp_path, result)
    result_path = tmp_path / result_ref.locator
    value = json.loads(result_path.read_text(encoding="utf-8"))
    value["ok"] = False
    result_path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        environment_recovery_state.read_environment_result(tmp_path, result_ref)

    other = tmp_path / "other-state"
    other.mkdir()
    shot = tmp_path / "symlink-shot"
    shot.mkdir()
    (shot / "state").symlink_to(other, target_is_directory=True)
    with pytest.raises(ValueError, match="must not be a symlink"):
        environment_recovery_state.read_environment_commit(shot, _digest("key"))


def test_environment_result_read_refuses_a_symlinked_content_addressed_record(
    tmp_path: Path,
) -> None:
    result = _result()
    result_ref = environment_recovery_state.publish_environment_result(tmp_path, result)
    result_path = tmp_path / result_ref.locator
    outside = tmp_path / "outside-result.json"
    outside.write_bytes(result_path.read_bytes())
    result_path.unlink()
    result_path.symlink_to(outside)

    with pytest.raises(ValueError, match="immutable regular file"):
        environment_recovery_state.read_environment_result(tmp_path, result_ref)


def test_environment_recovery_state_accepts_a_relative_shot_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path.parent)
    shot = Path(tmp_path.name)
    result = _result()

    result_ref = environment_recovery_state.publish_environment_result(shot, result)

    assert environment_recovery_state.read_environment_result(shot, result_ref) == result
