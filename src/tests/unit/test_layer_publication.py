from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

import vfx_harness.agents.builder.layer as builder_layer
import vfx_harness.orchestration.layer_publication as layer_publication
import vfx_harness.orchestration.revalidation as revalidation
from tests.unit.test_layer_finalization_state import (
    _finalize,
    _finalized_independent_states,
    _independent_two_layer_capsules,
    _layer,
)
from tests.unit.test_layer_finalization_state import (
    _lower_boundary_receipt_authority as _lower_boundary_receipt_authority,
)
from tests.unit_attempt_fixtures import ABSENT_SELECTION_TOKEN
from vfx_harness.domain.layer_finalizations import LayerFinalizationReceipt
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionToken,
)
from vfx_harness.orchestration.layer_outcome_paths import layer_outcome_path
from vfx_harness.orchestration.layer_publication import (
    LayerPublicationConflict,
    require_current_layer_publication,
)

pytestmark = pytest.mark.usefixtures("_lower_boundary_receipt_authority")


@pytest.fixture(autouse=True)
def _selected_prefix_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep legacy single-layer fixtures below selected-DAG resolution."""

    monkeypatch.setattr(
        "vfx_harness.orchestration.layer_publication."
        "selected_finalization_predecessor_layers",
        lambda *_args, **_kwargs: (),
    )


def _selected(token=ABSENT_SELECTION_TOKEN):
    return SimpleNamespace(selection_token=token, plan=object())


def _outcome(receipt: LayerFinalizationReceipt, *, schema: int = 3) -> dict:
    projected = dict(receipt.projection["outcome"])
    projected.pop("schema")
    return {
        "schema": schema,
        "at": receipt.completed_at,
        **projected,
        "finalization_receipt": receipt.as_dict(),
    }


def _write_projections(folder, receipt: LayerFinalizationReceipt) -> None:
    outcome_path = layer_outcome_path(folder, receipt.claim.layer_id)
    outcome_path.parent.mkdir(parents=True, exist_ok=True)
    outcome_path.write_text(
        json.dumps(_outcome(receipt), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    ledger_path = folder / "shot.json"
    ledger = (
        json.loads(ledger_path.read_text(encoding="utf-8"))
        if ledger_path.exists()
        else {"shot": "fixture", "milestones": {}}
    )
    ledger["milestones"][receipt.claim.layer_id] = {
        "status": "passed",
        "script": receipt.layer_script_path,
        "script_sha256": receipt.layer_script_sha256,
        "finalization_receipt_digest": receipt.receipt_digest,
    }
    ledger_path.write_text(
        json.dumps(ledger, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _published_independent_prefix(tmp_path, monkeypatch: pytest.MonkeyPatch):
    _global, _effective, capsules = _independent_two_layer_capsules()
    first, later, states, _bindings = _finalized_independent_states(
        tmp_path,
        capsules,
    )
    receipts = {
        layer_id: LayerFinalizationReceipt.parse(
            state["layer_finalization"]["terminal_receipt"]
        )
        for layer_id, state in states.items()
    }
    _write_projections(tmp_path, receipts[first.id])
    _write_projections(tmp_path, receipts[later.id])
    monkeypatch.setattr(
        "vfx_harness.orchestration.layer_publication."
        "selected_finalization_predecessor_layers",
        lambda _folder, layer_id, **_kwargs: (
            () if str(layer_id) == first.id else (first,)
        ),
    )
    return first, later, receipts


def test_current_layer_publication_requires_one_exact_receipt_across_all_surfaces(
    tmp_path,
) -> None:
    layer, _claim_guard, _replay, _stored, receipt = _finalize(tmp_path)
    _write_projections(tmp_path, receipt)

    publication = require_current_layer_publication(
        tmp_path,
        layer,
        _selected(),
    )

    assert publication.receipt == receipt
    assert publication.outcome.receipt_digest == receipt.receipt_digest
    assert json.loads(publication.outcome_bytes)["finalization_receipt"] == receipt.as_dict()
    assert publication.outcome_locator == "plans/outcomes/layer-31.json"
    assert publication.ledger_status == "passed"
    assert publication.ledger_receipt_digest == receipt.receipt_digest
    assert publication.ledger_script_path == receipt.layer_script_path
    assert publication.ledger_script_sha256 == receipt.layer_script_sha256
    assert len(publication.outcome_sha256) == 64


def test_direct_current_publication_source_closes_heterogeneous_prefix(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, later, receipts = _published_independent_prefix(tmp_path, monkeypatch)

    publication = require_current_layer_publication(tmp_path, later, _selected())

    assert publication.receipt == receipts[later.id]
    assert publication.receipt.claim.predecessor_inputs[0].layer_id == first.id


@pytest.mark.parametrize("corruption", ["missing_outcome", "ledger"])
def test_direct_current_publication_rejects_broken_predecessor_projection(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    corruption: str,
) -> None:
    first, later, _receipts = _published_independent_prefix(tmp_path, monkeypatch)
    if corruption == "missing_outcome":
        layer_outcome_path(tmp_path, first.id).unlink()
        message = "sealed outcome is missing"
    else:
        ledger_path = tmp_path / "shot.json"
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        ledger["milestones"][first.id]["status"] = "pending"
        ledger_path.write_text(
            json.dumps(ledger, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        message = "ledger status must be 'passed'"

    with pytest.raises(LayerPublicationConflict, match=message):
        require_current_layer_publication(tmp_path, later, _selected())


@pytest.mark.parametrize("corruption", ["missing_outcome", "ledger"])
def test_current_publication_rejects_predecessor_projection_race(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    corruption: str,
) -> None:
    first, later, _receipts = _published_independent_prefix(tmp_path, monkeypatch)
    capture = layer_publication.capture_layer_publication_projections

    def racing_capture(*args, **kwargs):
        if kwargs["layer_id"] == later.id:
            if corruption == "missing_outcome":
                layer_outcome_path(tmp_path, first.id).unlink()
            else:
                ledger_path = tmp_path / "shot.json"
                ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
                ledger["milestones"][first.id]["status"] = "pending"
                ledger_path.write_text(
                    json.dumps(ledger, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
        return capture(*args, **kwargs)

    monkeypatch.setattr(
        layer_publication,
        "capture_layer_publication_projections",
        racing_capture,
    )

    with pytest.raises(LayerPublicationConflict):
        require_current_layer_publication(tmp_path, later, _selected())


def test_current_publication_tolerates_unrelated_ledger_slot_race(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _first, later, receipts = _published_independent_prefix(tmp_path, monkeypatch)
    capture = layer_publication.capture_layer_publication_projections

    def racing_capture(*args, **kwargs):
        if kwargs["layer_id"] == later.id:
            ledger_path = tmp_path / "shot.json"
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            ledger["milestones"]["unrelated"] = {"status": "pending"}
            ledger_path.write_text(
                json.dumps(ledger, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        return capture(*args, **kwargs)

    monkeypatch.setattr(
        layer_publication,
        "capture_layer_publication_projections",
        racing_capture,
    )

    publication = require_current_layer_publication(tmp_path, later, _selected())

    assert publication.receipt == receipts[later.id]


def test_layer_publication_refuses_missing_terminal_receipt(tmp_path) -> None:
    layer = _layer()

    with pytest.raises(
        LayerPublicationConflict,
        match="no current terminal finalization receipt",
    ):
        require_current_layer_publication(tmp_path, layer, _selected())


def test_layer_publication_refuses_terminal_receipt_without_outcome(tmp_path) -> None:
    layer, _claim_guard, _replay, _stored, _receipt = _finalize(tmp_path)

    with pytest.raises(LayerPublicationConflict, match="sealed outcome is missing"):
        require_current_layer_publication(tmp_path, layer, _selected())


def test_revalidation_prior_digest_refuses_receipt_without_public_projections(
    tmp_path,
) -> None:
    layer, _claim_guard, _replay, _stored, receipt = _finalize(tmp_path)

    with pytest.raises(
        ValueError,
        match=r"revalidation cannot consume prior layer 1.*sealed outcome is missing",
    ):
        revalidation._prior_outcome_digests(
            tmp_path,
            (layer,),
            _selected(),
        )

    _write_projections(tmp_path, receipt)
    outcome_path = layer_outcome_path(tmp_path, layer.id)
    assert revalidation._prior_outcome_digests(
        tmp_path,
        (layer,),
        _selected(),
    ) == {layer.id: hashlib.sha256(outcome_path.read_bytes()).hexdigest()}


def test_downstream_finalization_refuses_predecessor_without_public_projections(
    tmp_path,
) -> None:
    layer, _claim_guard, _replay, _stored, receipt = _finalize(tmp_path)
    shot = SimpleNamespace(folder=tmp_path)

    with pytest.raises(
        ValueError,
        match=(
            r"layer finalization cannot consume predecessor 1.*"
            r"sealed outcome is missing"
        ),
    ):
        builder_layer._finalization_predecessor_inputs(
            shot,
            [tmp_path / layer.script],
            {layer.id: layer},
            _selected(),
        )

    _write_projections(tmp_path, receipt)
    assert builder_layer._finalization_predecessor_inputs(
        shot,
        [tmp_path / layer.script],
        {layer.id: layer},
        _selected(),
    ) == (
        builder_layer.LayerFinalizationPredecessorInput.mint(
            layer_id=layer.id,
            finalization_receipt_digest=receipt.receipt_digest,
            script_path=receipt.layer_script_path,
            script_sha256=receipt.layer_script_sha256,
        ),
    )


def test_layer_publication_strictly_rejects_schema_two_outcome(tmp_path) -> None:
    layer, _claim_guard, _replay, _stored, receipt = _finalize(tmp_path)
    _write_projections(tmp_path, receipt)
    path = layer_outcome_path(tmp_path, layer.id)
    path.write_text(
        json.dumps(_outcome(receipt, schema=2), sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        LayerPublicationConflict,
        match="sealed layer outcome schema must be 3",
    ):
        require_current_layer_publication(tmp_path, layer, _selected())


def test_layer_publication_refuses_another_valid_embedded_receipt(tmp_path) -> None:
    layer, _claim_guard, _replay, _stored, receipt = _finalize(tmp_path)
    _write_projections(tmp_path, receipt)
    other_projection = json.loads(json.dumps(receipt.projection))
    other_projection["best"]["mean"] = 2.0
    other_projection["outcome"]["best"]["mean"] = 2.0
    other = LayerFinalizationReceipt.mint(
        evaluation_receipt=receipt.evaluation_receipt,
        evaluation_receipt_locator=receipt.evaluation_receipt_locator,
        evaluation_receipt_sha256=receipt.evaluation_receipt_sha256,
        projection=other_projection,
        completed_at="2026-09-01T10:04:00+00:00",
    )
    path = layer_outcome_path(tmp_path, layer.id)
    path.write_text(
        json.dumps(_outcome(other), sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        LayerPublicationConflict,
        match="does not embed the exact current terminal finalization receipt",
    ):
        require_current_layer_publication(tmp_path, layer, _selected())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("best", {"round": 9, "mean": 0.0, "render": None}),
        ("canonical", []),
        (
            "interfaces",
            [
                {
                    "id": "invented-interface",
                    "metric": "object_property",
                    "value": 1,
                    "target": ">= 1",
                    "pass": True,
                    "owner_layer": "1",
                    "fault_owner": "1",
                    "activates_at": "1",
                    "lifecycle": "layer",
                }
            ],
        ),
        ("authoritative_total", 2),
        ("authoritative_passed", 2),
        ("failed_contracts", ["invented-interface"]),
        ("revalidation_manifest", {"tampered": True}),
    ],
)
def test_layer_publication_refuses_receipt_bound_projection_tamper(
    tmp_path,
    field: str,
    value: object,
) -> None:
    layer, _claim_guard, _replay, _stored, receipt = _finalize(tmp_path)
    _write_projections(tmp_path, receipt)
    path = layer_outcome_path(tmp_path, layer.id)
    record = json.loads(path.read_text(encoding="utf-8"))
    record[field] = value
    path.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(
        LayerPublicationConflict,
        match=(
            rf"sealed layer outcome {field} does not match its receipt-bound "
            r"proposed projection"
        ),
    ):
        require_current_layer_publication(tmp_path, layer, _selected())


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("status", "in_progress", "ledger status must be 'passed'"),
        (
            "finalization_receipt_digest",
            "f" * 64,
            "ledger finalization receipt digest does not match",
        ),
        ("script", "build/other.py", "ledger script locator does not match"),
        ("script_sha256", "e" * 64, "ledger script SHA-256 does not match"),
    ],
)
def test_layer_publication_refuses_ledger_projection_drift(
    tmp_path,
    field: str,
    value: str,
    message: str,
) -> None:
    layer, _claim_guard, _replay, _stored, receipt = _finalize(tmp_path)
    _write_projections(tmp_path, receipt)
    path = tmp_path / "shot.json"
    ledger = json.loads(path.read_text(encoding="utf-8"))
    ledger["milestones"][layer.id][field] = value
    path.write_text(json.dumps(ledger, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(LayerPublicationConflict, match=message):
        require_current_layer_publication(tmp_path, layer, _selected())


def test_layer_publication_refuses_legacy_truncated_script_digest(tmp_path) -> None:
    layer, _claim_guard, _replay, _stored, receipt = _finalize(tmp_path)
    _write_projections(tmp_path, receipt)
    path = tmp_path / "shot.json"
    ledger = json.loads(path.read_text(encoding="utf-8"))
    slot = ledger["milestones"][layer.id]
    slot["script_sha"] = slot.pop("script_sha256")[:16]
    path.write_text(json.dumps(ledger, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(
        LayerPublicationConflict,
        match="ledger script SHA-256 does not match",
    ):
        require_current_layer_publication(tmp_path, layer, _selected())


def test_layer_publication_source_verifies_terminal_receipt_under_guard(
    tmp_path,
) -> None:
    layer, _claim_guard, _replay, _stored, receipt = _finalize(tmp_path)
    _write_projections(tmp_path, receipt)
    (tmp_path / receipt.layer_script_path).write_text(
        "# substituted composed script\n",
        encoding="utf-8",
    )

    with pytest.raises(
        LayerPublicationConflict,
        match=r"terminal layer replay group 0 input 0 bytes changed",
    ):
        require_current_layer_publication(tmp_path, layer, _selected())


def test_layer_publication_refuses_another_selected_authority(tmp_path) -> None:
    layer, _claim_guard, _replay, _stored, receipt = _finalize(tmp_path)
    _write_projections(tmp_path, receipt)
    selected = _selected(
        AuthoritySelectionToken(
            plan_revision=1,
            plan_pointer_sha256="d" * 64,
            jit_revision=0,
            jit_pointer_sha256=None,
        )
    )

    with pytest.raises(
        LayerPublicationConflict,
        match="authority selection changed",
    ):
        require_current_layer_publication(tmp_path, layer, selected)


def test_sealed_outcome_capture_binds_the_expected_terminal_status(tmp_path) -> None:
    """The capture binds the status its caller names; a passed outcome cannot bind a
    failed receipt's chain row (HIR-0172 accepted-build index)."""

    layer, _claim_guard, _replay, _stored, receipt = _finalize(tmp_path)
    _write_projections(tmp_path, receipt)

    outcome, _snapshot = layer_publication.capture_sealed_outcome_projection(
        tmp_path,
        layer_id=layer.id,
        receipt=receipt,
    )
    assert outcome.status == "passed"

    with pytest.raises(
        LayerPublicationConflict,
        match="outcome is 'passed' although its terminal receipt is",
    ):
        layer_publication.capture_sealed_outcome_projection(
            tmp_path,
            layer_id=layer.id,
            receipt=receipt,
            expect_passed=False,
        )
