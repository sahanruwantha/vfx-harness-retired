"""Receipt-backed re-verification after an operator repairs the environment."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vfx_harness.application import preflight
from vfx_harness.domain.environment_recovery import EnvironmentRecoveryCommit
from vfx_harness.domain.environment_results import EnvironmentResult
from vfx_harness.domain.stop_envelopes import StopEnvelope
from vfx_harness.domain.stop_transaction_state import StopEvidenceRef
from vfx_harness.domain.stop_transactions import (
    EnvironmentReverified,
    PostconditionEvaluation,
    RecoverEnvironmentTarget,
    StopAction,
    action_idempotency_key,
)
from vfx_harness.domain.transaction_receipts import TransactionReceipt
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration import environment_recovery_state, transaction_receipts

RECEIPT_CAPABLE_TRANSACTION_IDS = frozenset({"recover_environment"})
ADAPTER_ID = "external_operator"


@dataclass(frozen=True, slots=True)
class _RecoveryAttempt:
    shot: Path
    envelope: StopEnvelope
    action: StopAction
    target: RecoverEnvironmentTarget
    postcondition: EnvironmentReverified
    before_result: EnvironmentResult
    before_result_ref: StopEvidenceRef
    prepared: TransactionReceipt


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"environment stop evidence contains duplicate JSON key {key!r}")
        value[key] = item
    return value


def _read_before_result(
    shot: Path,
    target: RecoverEnvironmentTarget,
) -> tuple[EnvironmentResult, StopEvidenceRef]:
    candidates = tuple(
        evidence
        for evidence in target.evidence
        if evidence.kind == "environment_result"
        and evidence.record_schema == EnvironmentResult.SCHEMA
        and evidence.record_digest == target.environment.result_digest
    )
    if len(candidates) != 1:
        raise ValueError(
            "recover_environment target must cite exactly one matching EnvironmentResult"
        )
    evidence = candidates[0]
    shot_root = shot.resolve()
    path = _real_file_under_shot(
        shot_root,
        evidence.locator,
        "failed environment-result evidence",
    )
    if not path.is_file():
        raise ValueError("failed environment-result evidence must be an immutable regular file")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != evidence.sha256:
        raise ValueError("failed environment-result evidence SHA-256 mismatch")
    try:
        value = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("failed environment-result evidence is malformed JSON") from exc
    result = EnvironmentResult.from_dict(value, "failed environment result")
    if result.digest != evidence.record_digest:
        raise ValueError("failed environment-result evidence record digest mismatch")
    return result, evidence


def _real_file_under_shot(shot: Path, locator: str, where: str) -> Path:
    """Resolve a validated locator without accepting a symlink in its path."""

    path = shot / locator
    current = shot
    for part in Path(locator).parts:
        current /= part
        if current.is_symlink():
            raise ValueError(f"{where} must not use symlink path components")
    try:
        path.resolve().relative_to(shot)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(f"{where} escapes the shot root") from exc
    if not path.is_file():
        raise ValueError(f"{where} must be an immutable regular file")
    return path


def _resolve_attempt(
    shot_folder: str | Path,
    *,
    run_id: str,
    idempotency_key: str,
) -> _RecoveryAttempt:
    shot = Path(shot_folder).expanduser().resolve()
    layout = run_artifacts.select(shot, run_id)
    if layout is None:
        raise ValueError(f"environment recovery source run {run_id!r} does not exist")
    _real_file_under_shot(
        shot,
        layout.status.relative_to(shot).as_posix(),
        "environment recovery source status",
    )
    _real_file_under_shot(
        shot,
        layout.stop_envelope.relative_to(shot).as_posix(),
        "environment recovery source stop envelope",
    )
    envelope = layout.read_terminal_stop()
    action = envelope.actions[0]
    if (
        envelope.stop_class != "infrastructure_failure"
        or action.transaction_id != "recover_environment"
        or action.dispatch_mode != "external_recovery"
        or not isinstance(action.target, RecoverEnvironmentTarget)
        or not isinstance(action.postcondition, EnvironmentReverified)
    ):
        raise ValueError("source run does not authorize recover_environment")
    target = action.target
    postcondition = action.postcondition
    if target.recovery_adapter_id != ADAPTER_ID:
        raise ValueError(
            f"recover_environment adapter must be {ADAPTER_ID!r}, "
            f"found {target.recovery_adapter_id!r}"
        )
    before_result, before_result_ref = _read_before_result(shot, target)
    before_failed_ids = tuple(
        check.check_id for check in before_result.checks if not check.passed
    )
    if (
        before_result.ok
        or before_result.probe_spec is None
        or before_result.probe_id != target.environment.probe_id
        or before_result.probe_spec.digest != target.environment.probe_spec_digest
        or before_result.digest != target.environment.result_digest
        or before_failed_ids != target.failed_check_ids
        or envelope.authoritative_before_digest != before_result.environment_digest
        or envelope.attempt_evidence_digest != before_result.digest
    ):
        raise ValueError("source environment stop does not bind its exact failed probe result")
    expected_key = action_idempotency_key(
        action,
        authoritative_before_digest=envelope.authoritative_before_digest,
        attempt_evidence_digest=envelope.attempt_evidence_digest,
    )
    if idempotency_key != expected_key:
        raise ValueError(
            "environment recovery idempotency key does not match the source stop action"
        )
    prepared = TransactionReceipt.prepare(
        action,
        adapter_id=target.recovery_adapter_id,
        authoritative_before_digest=envelope.authoritative_before_digest,
        attempt_evidence_digest=envelope.attempt_evidence_digest,
        recovery_disposition="repeatable_read_only",
    )
    return _RecoveryAttempt(
        shot=shot,
        envelope=envelope,
        action=action,
        target=target,
        postcondition=postcondition,
        before_result=before_result,
        before_result_ref=before_result_ref,
        prepared=prepared,
    )


def _assert_attempt_receipt(
    attempt: _RecoveryAttempt,
    receipt: TransactionReceipt,
) -> None:
    if not receipt.same_attempt_as(attempt.prepared):
        raise ValueError(
            "stored receipt does not bind the semantically exact environment recovery attempt"
        )
    if receipt.adapter_id != attempt.target.recovery_adapter_id:
        raise ValueError("stored receipt names another environment recovery adapter")


def _validate_observed_result(
    attempt: _RecoveryAttempt,
    result: EnvironmentResult,
) -> None:
    if (
        result.probe_spec is None
        or result.probe_id != attempt.postcondition.probe_id
        or result.probe_spec.digest != attempt.postcondition.probe_spec_digest
    ):
        raise ValueError(
            "strict preflight probe identity changed during environment recovery"
        )


def _running_results(
    attempt: _RecoveryAttempt,
    receipt: TransactionReceipt,
) -> tuple[tuple[EnvironmentResult, StopEvidenceRef], ...]:
    results: list[tuple[EnvironmentResult, StopEvidenceRef]] = []
    for evidence in receipt.operation_refs:
        if (
            evidence.kind != "environment_result"
            or evidence.record_schema != EnvironmentResult.SCHEMA
        ):
            raise ValueError(
                "external_operator receipt contains an unsupported operation reference"
            )
        result = environment_recovery_state.read_environment_result(
            attempt.shot, evidence
        )
        _validate_observed_result(attempt, result)
        results.append((result, evidence))
    passing = tuple(item for item in results if item[0].ok)
    if len(passing) > 1:
        raise ValueError("environment recovery receipt contains ambiguous passing results")
    return tuple(results)


def _commit_from_running(
    attempt: _RecoveryAttempt,
    running: TransactionReceipt,
    after_result: EnvironmentResult,
    after_result_ref: StopEvidenceRef,
) -> TransactionReceipt:
    commit = EnvironmentRecoveryCommit.create(
        action=attempt.action,
        running_receipt=running,
        before_result=attempt.before_result,
        before_result_ref=attempt.before_result_ref,
        after_result=after_result,
        after_result_ref=after_result_ref,
    )
    commit_ref = environment_recovery_state.publish_environment_commit(
        attempt.shot, commit
    )
    terminal = running.terminal_successor(
        terminal_outcome="committed",
        authoritative_after_digest=after_result.environment_digest,
        result_evidence=(after_result_ref,),
        commit_marker=commit_ref,
        recovery_disposition="reconcile_commit_only",
    )
    return transaction_receipts.publish_transaction_receipt(attempt.shot, terminal)


def _reconcile_commit(
    attempt: _RecoveryAttempt,
    running: TransactionReceipt,
    commit: EnvironmentRecoveryCommit,
) -> TransactionReceipt:
    if (
        commit.running_receipt_revision != running.revision
        or commit.running_receipt_digest != running.digest
    ):
        raise ValueError("environment recovery commit does not bind the selected running receipt")
    after_result = environment_recovery_state.read_environment_result(
        attempt.shot, commit.after_result
    )
    commit.assert_matches(
        action=attempt.action,
        running_receipt=running,
        before_result=attempt.before_result,
        before_result_ref=attempt.before_result_ref,
        after_result=after_result,
        after_result_ref=commit.after_result,
    )
    commit_ref = environment_recovery_state.environment_commit_evidence_ref(
        attempt.shot, commit
    )
    terminal = running.terminal_successor(
        terminal_outcome="committed",
        authoritative_after_digest=commit.after_environment_digest,
        result_evidence=(commit.after_result,),
        commit_marker=commit_ref,
        recovery_disposition="reconcile_commit_only",
    )
    return transaction_receipts.publish_transaction_receipt(attempt.shot, terminal)


def _verify_committed_terminal(
    attempt: _RecoveryAttempt,
    chain: tuple[TransactionReceipt, ...],
) -> tuple[EnvironmentRecoveryCommit, EnvironmentResult, StopEvidenceRef]:
    terminal = chain[-1]
    if terminal.phase != "terminal" or terminal.terminal_outcome != "committed":
        raise ValueError("environment recovery terminal receipt is not committed")
    commit = environment_recovery_state.read_environment_commit(
        attempt.shot, terminal.idempotency_key
    )
    if commit is None:
        raise ValueError("committed environment recovery receipt has no commit marker record")
    try:
        running = next(
            receipt for receipt in chain if receipt.digest == commit.running_receipt_digest
        )
    except StopIteration as exc:
        raise ValueError("environment recovery commit names a receipt outside its chain") from exc
    if running.phase != "running" or running.revision != commit.running_receipt_revision:
        raise ValueError("environment recovery commit does not name an exact running receipt")
    after_result = environment_recovery_state.read_environment_result(
        attempt.shot, commit.after_result
    )
    commit.assert_matches(
        action=attempt.action,
        running_receipt=running,
        before_result=attempt.before_result,
        before_result_ref=attempt.before_result_ref,
        after_result=after_result,
        after_result_ref=commit.after_result,
    )
    commit_ref = environment_recovery_state.environment_commit_evidence_ref(
        attempt.shot, commit
    )
    if (
        terminal.predecessor_receipt_digest != running.digest
        or terminal.commit_marker != commit_ref
        or terminal.authoritative_after_digest != after_result.environment_digest
        or terminal.result_evidence != (commit.after_result,)
        or commit.after_result not in terminal.operation_refs
    ):
        raise ValueError("terminal environment recovery receipt does not bind its exact commit")
    return commit, after_result, commit_ref


def execute_environment_recovery(
    shot_folder: str | Path,
    *,
    run_id: str,
    idempotency_key: str,
    blender: str | None = None,
) -> TransactionReceipt:
    """Reverify external recovery idempotently; never edit the environment itself."""

    attempt = _resolve_attempt(
        shot_folder,
        run_id=run_id,
        idempotency_key=idempotency_key,
    )
    with environment_recovery_state.environment_recovery_lock(
        attempt.shot, idempotency_key
    ):
        transaction_receipts.reconcile_transaction_receipt_crash_orphan(
            attempt.shot, idempotency_key
        )
        chain = transaction_receipts.transaction_receipt_chain(
            attempt.shot, idempotency_key
        )
        if not chain:
            if environment_recovery_state.read_environment_commit(
                attempt.shot, idempotency_key
            ) is not None:
                raise ValueError(
                    "environment recovery commit exists without a prepared receipt chain"
                )
            current = transaction_receipts.publish_transaction_receipt(
                attempt.shot, attempt.prepared
            )
        else:
            current = chain[-1]
            _assert_attempt_receipt(attempt, current)
        if current.phase == "terminal":
            chain = transaction_receipts.transaction_receipt_chain(
                attempt.shot, idempotency_key
            )
            _verify_committed_terminal(attempt, chain)
            return current
        if current.phase == "prepared":
            if environment_recovery_state.read_environment_commit(
                attempt.shot, idempotency_key
            ) is not None:
                raise ValueError(
                    "environment recovery commit exists before a running receipt"
                )
            current = transaction_receipts.publish_transaction_receipt(
                attempt.shot, current.running_successor()
            )
        if current.phase != "running":
            raise ValueError("environment recovery receipt has an unsupported phase")
        if current.recovery_disposition not in {
            "repeatable_read_only",
            "reconcile_commit_only",
        }:
            raise ValueError("environment recovery receipt does not authorize re-verification")

        commit = environment_recovery_state.read_environment_commit(
            attempt.shot, idempotency_key
        )
        if commit is not None:
            return _reconcile_commit(attempt, current, commit)
        if current.recovery_disposition == "reconcile_commit_only":
            raise ValueError("environment recovery requires a commit that is missing")

        prior_results = _running_results(attempt, current)
        passing = tuple(item for item in prior_results if item[0].ok)
        if passing:
            after_result, after_result_ref = passing[0]
        else:
            after_result = preflight.environment_result(preflight.probe(blender))
            after_result_ref = environment_recovery_state.publish_environment_result(
                attempt.shot, after_result
            )
            try:
                _validate_observed_result(attempt, after_result)
            except ValueError:
                current = transaction_receipts.publish_transaction_receipt(
                    attempt.shot,
                    current.running_successor(
                        additional_operation_refs=(after_result_ref,),
                        recovery_disposition="halt_on_uncertainty",
                    ),
                )
                raise
            if after_result_ref not in current.operation_refs:
                current = transaction_receipts.publish_transaction_receipt(
                    attempt.shot,
                    current.running_successor(
                        additional_operation_refs=(after_result_ref,),
                    ),
                )
        if not after_result.ok:
            return current
        return _commit_from_running(
            attempt,
            current,
            after_result,
            after_result_ref,
        )


def evaluate_environment_recovery(
    shot_folder: str | Path,
    *,
    run_id: str,
    idempotency_key: str,
) -> PostconditionEvaluation | None:
    """Independently verify terminal bytes before declaring the postcondition satisfied."""

    attempt = _resolve_attempt(
        shot_folder,
        run_id=run_id,
        idempotency_key=idempotency_key,
    )
    chain = transaction_receipts.transaction_receipt_chain(
        attempt.shot, idempotency_key
    )
    if not chain or chain[-1].phase != "terminal":
        return None
    terminal = chain[-1]
    _assert_attempt_receipt(attempt, terminal)
    _commit, after_result, commit_ref = _verify_committed_terminal(attempt, chain)
    receipt_ref = transaction_receipts.transaction_receipt_evidence_ref(
        attempt.shot, terminal
    )
    evaluation = PostconditionEvaluation(
        action_digest=attempt.action.digest,
        postcondition_digest=attempt.postcondition.digest,
        idempotency_key=idempotency_key,
        evaluator_id=attempt.action.evaluator_id,
        authoritative_before_digest=attempt.envelope.authoritative_before_digest,
        authoritative_after_digest=after_result.environment_digest,
        observed_records=(receipt_ref, commit_ref, _commit.after_result),
        result="satisfied",
    )
    evaluation.assert_matches(
        attempt.action,
        authoritative_before_digest=attempt.envelope.authoritative_before_digest,
        attempt_evidence_digest=attempt.envelope.attempt_evidence_digest,
    )
    current = environment_recovery_state.read_postcondition_evaluation(
        attempt.shot, idempotency_key
    )
    if current is not None and current != evaluation:
        raise ValueError("stored environment postcondition evaluation conflicts with current proof")
    environment_recovery_state.publish_postcondition_evaluation(
        attempt.shot, evaluation
    )
    return evaluation


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vfx recover-environment",
        description="Reverify a typed environment stop after external operator recovery.",
    )
    parser.add_argument("shot", type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--idempotency-key", required=True)
    parser.add_argument("--blender")
    args = parser.parse_args(argv)
    receipt = execute_environment_recovery(
        args.shot,
        run_id=args.run_id,
        idempotency_key=args.idempotency_key,
        blender=args.blender,
    )
    evaluation = evaluate_environment_recovery(
        args.shot,
        run_id=args.run_id,
        idempotency_key=args.idempotency_key,
    )
    print(
        json.dumps(
            {
                "receipt": receipt.as_dict(),
                "evaluation": None if evaluation is None else evaluation.as_dict(),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if evaluation is not None and evaluation.satisfied else 2


if __name__ == "__main__":
    raise SystemExit(main())
