from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.unit.test_layer_finalizations import _terminal
from vfx_harness.agents.builder import layer_finalization_reconcile as reconciliation


def test_terminal_reconciliation_projects_without_execution_or_judgment(
    tmp_path,
    monkeypatch,
) -> None:
    receipt = _terminal()
    layer = SimpleNamespace(
        id=receipt.claim.layer_id,
        script=receipt.layer_script_path,
        stages=(SimpleNamespace(id="camera"),),
        as_milestone=lambda _strips: SimpleNamespace(id=receipt.claim.layer_id),
    )
    shot = SimpleNamespace(folder=tmp_path)
    selected = SimpleNamespace(selection_token=object())
    events: list[str] = []

    class Guard:
        def __init__(self, terminal_receipt) -> None:
            self.receipt = terminal_receipt
            self.authority_binding = {
                "receipt": terminal_receipt.receipt_digest
            }

        def publish(self, operation, mutation):
            events.append(f"guard:{operation}")
            return mutation()

    monkeypatch.setattr(
        reconciliation.LayerFinalizationReceiptGuard,
        "bind",
        lambda *_args, **_kwargs: Guard(receipt),
    )
    monkeypatch.setattr(
        reconciliation,
        "reconcile_layer_revalidation_projection",
        lambda *_args, **_kwargs: events.append("revalidation")
        or {"kept": 0, "dropped": []},
    )
    monkeypatch.setattr(
        reconciliation,
        "record_prepared_accepted_hypothesis_falsification",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("receipt has no finding")
        ),
    )
    monkeypatch.setattr(
        reconciliation,
        "reconcile_judgment_payment_attempt_failures",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("receipt has no judgment debt")
        ),
    )
    outcome = tmp_path / "plans/outcomes/layer-33.json"
    monkeypatch.setattr(
        reconciliation,
        "publish_finalized_layer_outcome",
        lambda *_args, **_kwargs: events.append("outcome") or outcome,
    )

    class FakeLedger:
        def __init__(self, *_args, execution_guard, **_kwargs) -> None:
            assert isinstance(execution_guard, Guard)
            self.slot: dict = {}
            self.data: dict = {}

        def _slot(self, _milestone):
            return self.slot

        def save(self, *, derived_index=None) -> None:
            assert derived_index is None
            events.append("ledger")

    monkeypatch.setattr(reconciliation, "AuthorityBoundLedger", FakeLedger)
    # This fixture sits below the coordinator boundary; the accepted-build index
    # needs real selected authority and is covered by the public pipeline tests.
    monkeypatch.setattr(
        reconciliation.shot_ledger_v2_derivation,
        "derive_shot_ledger_index",
        lambda *_args, **_kwargs: None,
    )

    result = reconciliation.reconcile_layer_finalization(
        shot,
        layer,
        receipt,
        selected_authority=selected,
        strips={},
    )

    assert result.outcome == outcome
    assert result.finding is None
    assert result.revalidation == {"kept": 0, "dropped": []}
    assert result.ledger.slot == {
        "status": "passed",
        "updated": receipt.completed_at,
        "script": receipt.layer_script_path,
        "script_sha256": receipt.layer_script_sha256,
        "script_sha": receipt.layer_script_sha256[:16],
        "finalization_receipt_digest": receipt.receipt_digest,
        "layer_finalization_claim": receipt.claim.claim_id,
        "best": {"round": 0, "mean": 1.0, "render": None},
        "ablation": {
            "ok": True,
            "note": "fixture ablation",
            "at": receipt.completed_at,
        },
    }
    assert events == [
        f"guard:reconcile layer {layer.id} image checks",
        "revalidation",
        "outcome",
        "ledger",
    ]


def test_terminal_reconciliation_refuses_conflicting_same_receipt_ledger(
    tmp_path,
    monkeypatch,
) -> None:
    receipt = _terminal()
    layer = SimpleNamespace(
        id=receipt.claim.layer_id,
        script=receipt.layer_script_path,
        stages=(SimpleNamespace(id="camera"),),
        as_milestone=lambda _strips: SimpleNamespace(id=receipt.claim.layer_id),
    )
    selected = SimpleNamespace(selection_token=object())

    class Guard:
        def __init__(self, terminal_receipt) -> None:
            self.receipt = terminal_receipt
            self.authority_binding = {
                "receipt": terminal_receipt.receipt_digest
            }

        def publish(self, _operation, mutation):
            return mutation()

    monkeypatch.setattr(
        reconciliation.LayerFinalizationReceiptGuard,
        "bind",
        lambda *_args, **_kwargs: Guard(receipt),
    )
    monkeypatch.setattr(
        reconciliation,
        "reconcile_layer_revalidation_projection",
        lambda *_args, **_kwargs: {"kept": 0, "dropped": []},
    )
    monkeypatch.setattr(
        reconciliation,
        "publish_finalized_layer_outcome",
        lambda *_args, **_kwargs: tmp_path / "plans/outcomes/layer-33.json",
    )

    class ConflictingLedger:
        def __init__(self, *_args, **_kwargs) -> None:
            self.slot = {
                "finalization_receipt_digest": receipt.receipt_digest,
                "status": "failed",
            }

        def _slot(self, _milestone):
            return self.slot

        def save(self) -> None:
            raise AssertionError("conflicting ledger must not publish")

    monkeypatch.setattr(
        reconciliation,
        "AuthorityBoundLedger",
        ConflictingLedger,
    )

    with pytest.raises(ValueError, match="ledger conflicts"):
        reconciliation.reconcile_layer_finalization(
            SimpleNamespace(folder=tmp_path),
            layer,
            receipt,
            selected_authority=selected,
            strips={},
        )
