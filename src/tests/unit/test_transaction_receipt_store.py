"""Durable transaction receipts are atomic, immutable, and crash-reconcilable."""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier

import pytest

from vfx_harness.domain.stop_transaction_state import (
    EnvironmentResultAssertion,
    StopEvidenceRef,
)
from vfx_harness.domain.stop_transactions import (
    EnvironmentReverified,
    RecoverEnvironmentTarget,
    StopAction,
)
from vfx_harness.domain.transaction_receipts import (
    TransactionReceipt,
    TransactionSpend,
)
from vfx_harness.orchestration import transaction_receipts


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _evidence(label: str, *, kind: str = "run_report") -> StopEvidenceRef:
    return StopEvidenceRef(
        kind=kind,
        locator=f"runs/recovery-1/reports/{label}.json",
        sha256=_digest(f"{label}:bytes"),
        record_schema=f"vfx-harness.{label}/v1",
        record_digest=_digest(f"{label}:record"),
    )


def _action() -> StopAction:
    environment = EnvironmentResultAssertion(
        "preflight",
        _digest("strict-preflight-spec"),
        _digest("failed-environment"),
    )
    target = RecoverEnvironmentTarget(
        environment,
        ("blender_executable",),
        "external_operator",
        (_evidence("failed-environment", kind="environment_result"),),
    )
    return StopAction(
        target,
        EnvironmentReverified(
            environment.probe_id,
            environment.probe_spec_digest,
            environment.result_digest,
            target.failed_check_ids,
        ),
    )


def _prepared(*, adapter_id: str = "external_operator") -> TransactionReceipt:
    return TransactionReceipt.prepare(
        _action(),
        adapter_id=adapter_id,
        authoritative_before_digest=_digest("failed-environment"),
        attempt_evidence_digest=_digest("stop-envelope-attempt"),
        recovery_disposition="query_external",
    )


def _running(prepared: TransactionReceipt) -> TransactionReceipt:
    return prepared.running_successor(
        additional_operation_refs=(_evidence("strict-preflight-probe"),),
        cumulative_spend=TransactionSpend(0, 0, 0, 1, 0, 25),
    )


def _failed(running: TransactionReceipt) -> TransactionReceipt:
    prior = running.spend
    return running.terminal_successor(
        terminal_outcome="failed",
        authoritative_after_digest=running.authoritative_before_digest,
        result_evidence=(_evidence("still-failing-environment", kind="environment_result"),),
        cumulative_spend=TransactionSpend(
            prior.model_input_tokens,
            prior.model_output_tokens,
            prior.model_turns,
            prior.external_requests,
            prior.cost_microusd,
            prior.elapsed_milliseconds + 5,
        ),
        recovery_disposition="halt_on_uncertainty",
    )


def test_query_is_read_only_and_requires_an_exact_digest_key(tmp_path: Path) -> None:
    key = _digest("unused-key")

    assert transaction_receipts.current_transaction_receipt(tmp_path, key) is None
    assert transaction_receipts.transaction_receipt_chain(tmp_path, key) == ()
    assert not (tmp_path / "state").exists()

    with pytest.raises(ValueError, match="lowercase SHA-256"):
        transaction_receipts.current_transaction_receipt(tmp_path, "../escape")
    assert not (tmp_path / "state").exists()


def test_chain_query_returns_every_verified_revision_in_order(tmp_path: Path) -> None:
    prepared = _prepared()
    running = _running(prepared)
    terminal = _failed(running)
    for receipt in (prepared, running, terminal):
        transaction_receipts.publish_transaction_receipt(tmp_path, receipt)

    assert transaction_receipts.transaction_receipt_chain(
        tmp_path,
        prepared.idempotency_key,
    ) == (prepared, running, terminal)
    assert (
        transaction_receipts.current_transaction_receipt(
            tmp_path,
            prepared.idempotency_key,
        )
        == terminal
    )

def test_receipt_chain_publishes_under_shot_state_and_round_trips(tmp_path: Path) -> None:
    prepared = _prepared()
    running = _running(prepared)
    running_again = running.running_successor(
        additional_operation_refs=(_evidence("operator-status"),),
        cumulative_spend=TransactionSpend(0, 0, 0, 2, 0, 35),
    )
    terminal = _failed(running_again)

    for receipt in (prepared, running, running_again, terminal):
        assert transaction_receipts.publish_transaction_receipt(tmp_path, receipt) == receipt
        assert (
            transaction_receipts.current_transaction_receipt(
                tmp_path,
                prepared.idempotency_key,
            )
            == receipt
        )

    root = tmp_path / "state" / "transactions" / prepared.idempotency_key
    assert (root / "current.json").is_file()
    assert {
        path.name for path in (root / "receipts").glob("*.json")
    } == {
        f"{prepared.digest}.json",
        f"{running.digest}.json",
        f"{running_again.digest}.json",
        f"{terminal.digest}.json",
    }
    assert not (tmp_path / "runs").exists()


def test_exact_terminal_republication_is_idempotent_but_other_revisions_fail(
    tmp_path: Path,
) -> None:
    prepared = _prepared()
    running = _running(prepared)
    terminal = _failed(running)
    for receipt in (prepared, running, terminal):
        transaction_receipts.publish_transaction_receipt(tmp_path, receipt)

    assert transaction_receipts.publish_transaction_receipt(tmp_path, terminal) == terminal
    with pytest.raises(ValueError, match="terminal transaction receipt is immutable"):
        transaction_receipts.publish_transaction_receipt(tmp_path, running)


def test_concurrent_exact_prepared_publication_selects_one_identical_head(
    tmp_path: Path,
) -> None:
    prepared = _prepared()
    barrier = Barrier(2)

    def publish() -> TransactionReceipt:
        barrier.wait(timeout=5)
        return transaction_receipts.publish_transaction_receipt(tmp_path, prepared)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(publish) for _index in range(2)]
        results = [future.result(timeout=5) for future in futures]

    assert results == [prepared, prepared]
    assert (
        transaction_receipts.current_transaction_receipt(
            tmp_path,
            prepared.idempotency_key,
        )
        == prepared
    )
    receipts = (
        tmp_path
        / "state"
        / "transactions"
        / prepared.idempotency_key
        / "receipts"
    )
    assert [path.name for path in receipts.glob("*.json")] == [f"{prepared.digest}.json"]


def test_concurrent_competing_successors_select_exactly_one_chain(
    tmp_path: Path,
) -> None:
    prepared = _prepared()
    transaction_receipts.publish_transaction_receipt(tmp_path, prepared)
    first = _running(prepared)
    second = prepared.running_successor(
        additional_operation_refs=(_evidence("alternative-probe"),),
        cumulative_spend=TransactionSpend(0, 0, 0, 1, 0, 30),
    )
    barrier = Barrier(2)

    def publish(candidate: TransactionReceipt) -> tuple[str, TransactionReceipt | str]:
        barrier.wait(timeout=5)
        try:
            selected = transaction_receipts.publish_transaction_receipt(tmp_path, candidate)
        except transaction_receipts.TransactionReceiptStoreError as exc:
            return "conflict", str(exc)
        return "selected", selected

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(publish, candidate) for candidate in (first, second)]
        results = [future.result(timeout=5) for future in futures]

    assert sorted(status for status, _result in results) == ["conflict", "selected"]
    conflict = next(result for status, result in results if status == "conflict")
    assert "not the next legal revision" in conflict
    selected = transaction_receipts.current_transaction_receipt(
        tmp_path,
        prepared.idempotency_key,
    )
    assert selected in {first, second}
    receipts = (
        tmp_path
        / "state"
        / "transactions"
        / prepared.idempotency_key
        / "receipts"
    )
    assert {path.name for path in receipts.glob("*.json")} == {
        f"{prepared.digest}.json",
        f"{selected.digest}.json",
    }


def test_store_refuses_a_missing_initial_phase_or_noncontiguous_successor(
    tmp_path: Path,
) -> None:
    prepared = _prepared()
    running = _running(prepared)

    with pytest.raises(ValueError, match=r"first selected.*prepared revision 1"):
        transaction_receipts.publish_transaction_receipt(tmp_path, running)

    transaction_receipts.publish_transaction_receipt(tmp_path, prepared)
    unrelated = _running(_prepared(adapter_id="another_adapter"))
    with pytest.raises(ValueError, match="not the next legal revision"):
        transaction_receipts.publish_transaction_receipt(tmp_path, unrelated)


def test_query_verifies_pointer_bytes_and_the_full_predecessor_chain(tmp_path: Path) -> None:
    prepared = _prepared()
    running = _running(prepared)
    transaction_receipts.publish_transaction_receipt(tmp_path, prepared)
    transaction_receipts.publish_transaction_receipt(tmp_path, running)
    root = tmp_path / "state" / "transactions" / prepared.idempotency_key

    prepared_path = root / "receipts" / f"{prepared.digest}.json"
    prepared_path.write_bytes(prepared_path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="canonical immutable receipt encoding"):
        transaction_receipts.current_transaction_receipt(tmp_path, prepared.idempotency_key)


def test_query_refuses_pointer_tampering_and_unknown_fields(tmp_path: Path) -> None:
    prepared = _prepared()
    transaction_receipts.publish_transaction_receipt(tmp_path, prepared)
    pointer_path = (
        tmp_path
        / "state"
        / "transactions"
        / prepared.idempotency_key
        / "current.json"
    )
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    pointer["phase"] = "terminal"
    pointer_path.write_text(json.dumps(pointer, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"pointer_digest.*stale"):
        transaction_receipts.current_transaction_receipt(tmp_path, prepared.idempotency_key)

    pointer["debug"] = True
    pointer_path.write_text(json.dumps(pointer, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"unexpected=.*debug"):
        transaction_receipts.current_transaction_receipt(tmp_path, prepared.idempotency_key)


def test_crash_after_immutable_receipt_write_leaves_no_selected_phase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _prepared()
    original_publish_pointer = transaction_receipts._publish_pointer

    def crash_before_select(_path: Path, _pointer: object) -> None:
        raise RuntimeError("injected crash before pointer select")

    monkeypatch.setattr(transaction_receipts, "_publish_pointer", crash_before_select)
    with pytest.raises(RuntimeError, match="injected crash"):
        transaction_receipts.publish_transaction_receipt(tmp_path, prepared)

    assert (
        transaction_receipts.current_transaction_receipt(
            tmp_path,
            prepared.idempotency_key,
        )
        is None
    )
    orphan = (
        tmp_path
        / "state"
        / "transactions"
        / prepared.idempotency_key
        / "receipts"
        / f"{prepared.digest}.json"
    )
    assert orphan.is_file()

    monkeypatch.setattr(transaction_receipts, "_publish_pointer", original_publish_pointer)
    assert (
        transaction_receipts.reconcile_transaction_receipt_crash_orphan(
            tmp_path, prepared.idempotency_key
        )
        == prepared
    )
    assert (
        transaction_receipts.reconcile_transaction_receipt_crash_orphan(
            tmp_path, prepared.idempotency_key
        )
        is None
    )


def test_crash_orphan_reconciliation_selects_one_exact_direct_successor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _prepared()
    running = _running(prepared)
    transaction_receipts.publish_transaction_receipt(tmp_path, prepared)
    original_publish_pointer = transaction_receipts._publish_pointer

    def crash_before_select(_path: Path, _pointer: object) -> None:
        raise RuntimeError("injected crash before successor select")

    monkeypatch.setattr(transaction_receipts, "_publish_pointer", crash_before_select)
    with pytest.raises(RuntimeError, match="successor select"):
        transaction_receipts.publish_transaction_receipt(tmp_path, running)

    monkeypatch.setattr(transaction_receipts, "_publish_pointer", original_publish_pointer)
    assert (
        transaction_receipts.reconcile_transaction_receipt_crash_orphan(
            tmp_path, prepared.idempotency_key
        )
        == running
    )
    assert transaction_receipts.transaction_receipt_chain(
        tmp_path, prepared.idempotency_key
    ) == (prepared, running)


def test_conflicting_crash_orphan_cannot_be_silently_replaced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orphaned = _prepared()

    def crash_before_select(_path: Path, _pointer: object) -> None:
        raise RuntimeError("injected crash before pointer select")

    monkeypatch.setattr(transaction_receipts, "_publish_pointer", crash_before_select)
    with pytest.raises(RuntimeError, match="injected crash"):
        transaction_receipts.publish_transaction_receipt(tmp_path, orphaned)

    competing = replace(orphaned, adapter_id="another_adapter")
    with pytest.raises(ValueError, match="unselected transaction receipt"):
        transaction_receipts.publish_transaction_receipt(tmp_path, competing)


def test_far_future_orphan_also_blocks_a_new_successor(tmp_path: Path) -> None:
    prepared = _prepared()
    running = _running(prepared)
    future = running.running_successor(
        additional_operation_refs=(_evidence("future-operation"),),
        cumulative_spend=TransactionSpend(0, 0, 0, 2, 0, 35),
    )
    transaction_receipts.publish_transaction_receipt(tmp_path, prepared)
    future_path = (
        tmp_path
        / "state"
        / "transactions"
        / prepared.idempotency_key
        / "receipts"
        / f"{future.digest}.json"
    )
    future_path.write_bytes(transaction_receipts._receipt_bytes(future))

    with pytest.raises(ValueError, match="unselected transaction receipt"):
        transaction_receipts.publish_transaction_receipt(tmp_path, running)
    with pytest.raises(ValueError, match="not the exact direct successor"):
        transaction_receipts.reconcile_transaction_receipt_crash_orphan(
            tmp_path, prepared.idempotency_key
        )


def test_evidence_ref_requires_the_exact_current_durable_receipt(tmp_path: Path) -> None:
    prepared = _prepared()
    running = _running(prepared)
    transaction_receipts.publish_transaction_receipt(tmp_path, prepared)

    reference = transaction_receipts.transaction_receipt_evidence_ref(tmp_path, prepared)
    assert reference.kind == "transaction_receipt"
    assert reference.locator == (
        f"state/transactions/{prepared.idempotency_key}/receipts/{prepared.digest}.json"
    )
    assert reference.record_schema == prepared.SCHEMA
    assert reference.record_digest == prepared.digest
    assert reference.sha256 == hashlib.sha256(
        (
            tmp_path
            / reference.locator
        ).read_bytes()
    ).hexdigest()

    transaction_receipts.publish_transaction_receipt(tmp_path, running)
    with pytest.raises(ValueError, match="exact current selected receipt"):
        transaction_receipts.transaction_receipt_evidence_ref(tmp_path, prepared)


@pytest.mark.parametrize("symlink_parent", ["state", "transactions", "key"])
def test_read_only_query_refuses_symlinked_store_parents(
    tmp_path: Path,
    symlink_parent: str,
) -> None:
    key = _digest(f"symlink-key:{symlink_parent}")
    outside = tmp_path / "outside"
    outside.mkdir()
    state = tmp_path / "state"
    if symlink_parent == "state":
        state.symlink_to(outside, target_is_directory=True)
    else:
        state.mkdir()
        transactions = state / "transactions"
        if symlink_parent == "transactions":
            transactions.symlink_to(outside, target_is_directory=True)
        else:
            transactions.mkdir()
            (transactions / key).symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="must not be a symlink"):
        transaction_receipts.current_transaction_receipt(tmp_path, key)
