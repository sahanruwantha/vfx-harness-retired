"""Pure immutable transaction-receipt phase and idempotency contracts."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import replace

import pytest

from vfx_harness.domain.stop_transaction_state import (
    EnvironmentResultAssertion,
    StopEvidenceRef,
)
from vfx_harness.domain.stop_transactions import (
    EnvironmentReverified,
    RecoverEnvironmentTarget,
    StopAction,
    action_idempotency_key,
)
from vfx_harness.domain.transaction_receipts import (
    TransactionReceipt,
    TransactionSpend,
    validate_receipt_chain,
)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _evidence(label: str, *, kind: str = "environment_result") -> StopEvidenceRef:
    return StopEvidenceRef(
        kind=kind,
        locator=f"state/transactions/evidence/{label}.json",
        sha256=_digest(f"{label}:bytes"),
        record_schema=f"vfx-harness.{label}/v1",
        record_digest=_digest(f"{label}:record"),
    )


def _action() -> StopAction:
    environment = EnvironmentResultAssertion(
        probe_id="preflight",
        probe_spec_digest=_digest("probe-spec"),
        result_digest=_digest("failed-result"),
    )
    evidence = _evidence("failed-environment")
    target = RecoverEnvironmentTarget(
        environment=environment,
        failed_check_ids=("credential_configuration",),
        recovery_adapter_id="external_operator",
        evidence=(evidence,),
    )
    return StopAction(
        target=target,
        postcondition=EnvironmentReverified(
            probe_id=environment.probe_id,
            probe_spec_digest=environment.probe_spec_digest,
            before_result_digest=environment.result_digest,
            failed_check_ids=target.failed_check_ids,
        ),
    )


def _prepared() -> TransactionReceipt:
    return TransactionReceipt.prepare(
        _action(),
        adapter_id="strict_preflight_reverification",
        authoritative_before_digest=_digest("failed-environment-state"),
        attempt_evidence_digest=_digest("attempt-evidence"),
        recovery_disposition="repeatable_read_only",
    )


def _running() -> TransactionReceipt:
    return _prepared().running_successor()


def _commit() -> StopEvidenceRef:
    return _evidence("environment-recovery-commit", kind="authority_record")


def _result() -> StopEvidenceRef:
    return _evidence("passing-environment")


def test_prepared_receipt_embeds_action_recomputes_key_and_round_trips() -> None:
    receipt = _prepared()
    action = receipt.action

    assert receipt.revision == 1
    assert receipt.phase == "prepared"
    assert receipt.predecessor_receipt_digest is None
    assert receipt.idempotency_key == action_idempotency_key(
        action,
        authoritative_before_digest=receipt.authoritative_before_digest,
        attempt_evidence_digest=receipt.attempt_evidence_digest,
    )
    assert receipt.transaction_id == "recover_environment"
    assert receipt.action_digest == action.digest
    assert receipt.precondition_digest == action.precondition_digest
    assert receipt.postcondition_digest == action.postcondition.digest
    assert receipt.evaluator_id == action.evaluator_id
    assert TransactionReceipt.from_dict(receipt.as_dict(), "receipt") == receipt

    ref = receipt.evidence_ref(
        locator=f"state/transactions/{receipt.idempotency_key}/receipts/{receipt.digest}.json",
        sha256=_digest("receipt bytes"),
    )
    assert ref.kind == "transaction_receipt"
    assert ref.record_schema == TransactionReceipt.SCHEMA
    assert ref.record_digest == receipt.digest


def test_prepared_receipt_rejects_substituted_key_and_execution_claims() -> None:
    receipt = _prepared()
    with pytest.raises(ValueError, match="idempotency_key does not match"):
        replace(receipt, idempotency_key=_digest("another-key"))
    with pytest.raises(ValueError, match="revision 1"):
        replace(receipt, revision=2)
    with pytest.raises(ValueError, match="cannot claim execution"):
        replace(receipt, operation_refs=(_evidence("premature-operation"),))
    with pytest.raises(ValueError, match="cannot claim terminal"):
        replace(receipt, terminal_outcome="failed")


def test_running_revisions_append_operation_identity_and_cumulative_spend() -> None:
    prepared = _prepared()
    running = prepared.running_successor()
    operation = _evidence("preflight-operation", kind="journal")
    spend = TransactionSpend(10, 4, 1, 0, 3_500, 25)
    advanced = running.running_successor(
        additional_operation_refs=(operation,),
        cumulative_spend=spend,
        recovery_disposition="reconcile_commit_only",
    )

    assert running.revision == 2
    assert advanced.revision == 3
    assert advanced.predecessor_receipt_digest == running.digest
    assert advanced.operation_refs == (operation,)
    assert advanced.spend == spend
    assert validate_receipt_chain((prepared, running, advanced)) is advanced

    with pytest.raises(ValueError, match="must add execution identity"):
        running.running_successor()
    with pytest.raises(ValueError, match="recovery_disposition must be one of"):
        running.running_successor(recovery_disposition="")


def test_successor_cannot_remove_refs_decrease_spend_or_claim_safer_recovery() -> None:
    with pytest.raises(ValueError, match="safer or unrelated"):
        _prepared().running_successor(recovery_disposition="query_external")

    prepared = TransactionReceipt.prepare(
        _action(),
        adapter_id="external_request_recovery",
        authoritative_before_digest=_digest("failed-environment-state"),
        attempt_evidence_digest=_digest("attempt-evidence"),
        recovery_disposition="query_external",
    )
    running = prepared.running_successor(
        additional_operation_refs=(_evidence("operation", kind="journal"),),
        cumulative_spend=TransactionSpend(10, 4, 1, 1, 3_500, 25),
        recovery_disposition="query_external",
    )

    removed = replace(
        running,
        revision=running.revision + 1,
        predecessor_receipt_digest=running.digest,
        operation_refs=(),
    )
    with pytest.raises(ValueError, match="cannot remove"):
        removed.assert_successor(running)

    decreased = replace(
        running,
        revision=running.revision + 1,
        predecessor_receipt_digest=running.digest,
        spend=TransactionSpend(9, 4, 1, 1, 3_500, 25),
    )
    with pytest.raises(ValueError, match="cannot decrease"):
        decreased.assert_successor(running)

    safer = replace(
        running,
        revision=running.revision + 1,
        predecessor_receipt_digest=running.digest,
        recovery_disposition="repeatable_read_only",
    )
    with pytest.raises(ValueError, match="safer or unrelated"):
        safer.assert_successor(running)


def test_committed_terminal_requires_changed_authority_and_exact_commit_marker() -> None:
    running = _running()
    terminal = running.terminal_successor(
        terminal_outcome="committed",
        authoritative_after_digest=_digest("passing-environment-state"),
        commit_marker=_commit(),
        result_evidence=(_result(),),
    )

    assert terminal.phase == "terminal"
    assert terminal.terminal_outcome == "committed"
    assert terminal.commit_marker == _commit()
    assert TransactionReceipt.from_dict(terminal.as_dict(), "terminal") == terminal

    with pytest.raises(ValueError, match="must change authoritative"):
        running.terminal_successor(
            terminal_outcome="committed",
            authoritative_after_digest=running.authoritative_before_digest,
            commit_marker=_commit(),
            result_evidence=(_result(),),
        )
    with pytest.raises(ValueError, match="authority-record"):
        running.terminal_successor(
            terminal_outcome="committed",
            authoritative_after_digest=_digest("after"),
            commit_marker=_evidence("wrong-marker"),
            result_evidence=(_result(),),
        )
    with pytest.raises(ValueError, match="requires typed result evidence"):
        running.terminal_successor(
            terminal_outcome="committed",
            authoritative_after_digest=_digest("after"),
            commit_marker=_commit(),
            result_evidence=(),
        )


@pytest.mark.parametrize("outcome", ["failed", "indeterminate"])
def test_noncommitted_terminal_has_no_commit_and_cannot_be_succeeded(outcome: str) -> None:
    running = _running()
    terminal = running.terminal_successor(
        terminal_outcome=outcome,
        authoritative_after_digest=running.authoritative_before_digest,
        result_evidence=(_result(),),
        recovery_disposition="halt_on_uncertainty",
    )
    assert terminal.commit_marker is None

    with pytest.raises(ValueError, match="cannot claim a commit marker"):
        replace(terminal, commit_marker=_commit())
    with pytest.raises(ValueError, match="terminal transaction receipt is immutable"):
        terminal.running_successor(
            additional_operation_refs=(_evidence("late-operation", kind="journal"),)
        )


def test_receipt_cannot_skip_running_or_create_evidence_cycles() -> None:
    prepared = _prepared()
    with pytest.raises(ValueError, match="revision >= 3"):
        prepared.terminal_successor(
            terminal_outcome="failed",
            authoritative_after_digest=prepared.authoritative_before_digest,
            result_evidence=(_result(),),
        )

    circular = prepared.evidence_ref(
        locator="state/transactions/circular.json",
        sha256=_digest("circular bytes"),
    )
    with pytest.raises(ValueError, match="receipt cycle"):
        prepared.running_successor(additional_operation_refs=(circular,))


def test_parser_rejects_stale_derived_action_fields_and_unknown_fields() -> None:
    row = _prepared().as_dict()
    stale = deepcopy(row)
    stale["evaluator_id"] = "another_evaluator"
    with pytest.raises(ValueError, match="does not match the embedded action"):
        TransactionReceipt.from_dict(stale, "receipt")

    unknown = deepcopy(row)
    unknown["command"] = "anything"
    with pytest.raises(ValueError, match="fields mismatch"):
        TransactionReceipt.from_dict(unknown, "receipt")

    stale_digest = deepcopy(row)
    stale_digest["adapter_id"] = "another_adapter"
    with pytest.raises(ValueError, match="receipt_digest is stale"):
        TransactionReceipt.from_dict(stale_digest, "receipt")


def test_chain_requires_prepared_first_and_contiguous_exact_attempt() -> None:
    prepared = _prepared()
    running = prepared.running_successor()
    other = TransactionReceipt.prepare(
        _action(),
        adapter_id="strict_preflight_reverification",
        authoritative_before_digest=_digest("another-before"),
        attempt_evidence_digest=_digest("attempt-evidence"),
        recovery_disposition="repeatable_read_only",
    )

    with pytest.raises(ValueError, match="begin with prepared"):
        validate_receipt_chain((running,))
    with pytest.raises(ValueError, match="same exact transaction attempt"):
        replace(
            other,
            revision=2,
            phase="running",
            predecessor_receipt_digest=prepared.digest,
        ).assert_successor(prepared)


def test_spend_is_integer_only_nonnegative_and_componentwise_monotone() -> None:
    assert TransactionSpend.from_dict(TransactionSpend.zero().as_dict(), "spend") == TransactionSpend.zero()
    with pytest.raises(ValueError, match="non-negative integer"):
        TransactionSpend(0, 0, 0, 0, -1, 0)
    with pytest.raises(ValueError, match="non-negative integer"):
        TransactionSpend(0, 0, 0, 0, 0, 1.5)  # type: ignore[arg-type]
