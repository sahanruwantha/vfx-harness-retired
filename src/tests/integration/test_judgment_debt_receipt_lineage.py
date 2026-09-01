"""Debt payment remains current only through contiguous receipt preservation."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from pathlib import Path

import pytest

from tests.integration.test_authority_receipt_lineage import (
    _add_layer_only_acceptance_authority,
    _commit_document_successor,
    _current_unit_completion_receipt,
    _published_finalized_root,
    _selected_documents,
)
from vfx_harness.domain.judgment_debt_replay_receipts import (
    ReplayPrefixLayerReceipt,
    ReplayPrefixReceipt,
    ReplayPrefixUnitReceipt,
)
from vfx_harness.domain.judgment_debts import (
    JudgmentDebtActivation,
    JudgmentDebtSeed,
    JudgmentPoint,
    JudgmentProvider,
    compile_judgment_debt,
)
from vfx_harness.orchestration import judgment_debt_state
from vfx_harness.orchestration.authority_capsule_resolution import (
    selected_layer_capsule_digest,
)
from vfx_harness.orchestration.authority_selection import resolve_selected_authority
from vfx_harness.orchestration.judgment_debt_state import (
    current_judgment_debt_states,
    mark_judgment_debt_due,
    resolve_current_judgment_debt,
)
from vfx_harness.orchestration.ledger import load_layers_from_path
from vfx_harness.orchestration.unit_completion_state import (
    authorize_completed_units_for_layer,
)
from vfx_harness.orchestration.unit_state import load as load_unit_state
from vfx_harness.orchestration.unit_state import unit_digest


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _debt_authority(bundle_digest: str, layer, unit):
    definition = compile_judgment_debt(
        JudgmentDebtSeed(
            requirement_id="R-lineage-subject",
            statement="The paid subject retains its approved composition.",
            decision_strength="approved_start",
            claim_kind="atomic",
            property="reference_identity",
            owner_layer=str(layer.id),
            fault_owner=str(layer.id),
            subject_roles=("fixture.subject",),
            axes=("reference_match",),
            judge_points=(JudgmentPoint(frame=1, ref="refs/subject.png"),),
            observation_medium="workbench_solid",
            lifecycle="persistent",
            bundle_digest=bundle_digest,
            carrier_families=("mesh",),
        ),
        (
            JudgmentProvider(
                id="fixture-subject-mesh",
                layer_id=str(layer.id),
                carrier_family="mesh",
                subject_roles=("fixture.subject",),
            ),
        ),
        layer_dependencies={str(layer.id): ()},
        layer_order=(str(layer.id),),
    )
    activation = JudgmentDebtActivation.for_definition(
        definition,
        payer_unit_digests=((f"{layer.id}:{unit.id}", unit_digest(unit)),),
    )
    return definition, activation


def _paid_replay_prefix(root: Path, layer, unit, terminal) -> ReplayPrefixReceipt:
    state = load_unit_state(root, str(layer.id))
    slot = state["units"][unit.id]
    completion = _current_unit_completion_receipt(root, str(layer.id), unit.id)
    checkpoint = slot["checkpoint"]
    unit_path = root / unit.mutates.script_spans[0]
    layer_path = root / layer.script
    return ReplayPrefixReceipt(
        (
            ReplayPrefixLayerReceipt(
                layer_id=str(layer.id),
                layer_generation_digest=str(state["plan_hash"]),
                predecessor_layer_digests=(),
                script_path=str(layer.script),
                script_sha256=hashlib.sha256(layer_path.read_bytes()).hexdigest(),
                dependencies=(),
                units=(
                    ReplayPrefixUnitReceipt(
                        layer_id=str(layer.id),
                        unit_id=unit.id,
                        unit_digest=unit_digest(unit),
                        checkpoint_unit_digest=str(checkpoint["unit_hash"]),
                        script_path=unit.mutates.script_spans[0],
                        script_sha256=hashlib.sha256(unit_path.read_bytes()).hexdigest(),
                        checkpoint_script_sha256=str(checkpoint["script_hash"]),
                        completion_receipt_digest=completion.receipt_digest,
                    ),
                ),
                payer_claim_id=terminal.claim.claim_id,
            ),
        )
    )


def _install_debt_catalog(monkeypatch, definition, activation) -> None:
    monkeypatch.setattr(
        judgment_debt_state,
        "_current_authority",
        lambda _shot, _selected: (
            definition.seed.bundle_digest,
            (definition,),
            (activation,),
        ),
    )
    def receipt_lineage_is_current(
        shot,
        selected,
        _definition,
        _activation,
        generation,
        _replay,
    ) -> bool:
        layers = load_layers_from_path(selected.artifact_paths["layers.json"])
        observed: dict[str, str] = {}
        replay_layer_ids = {
            row.layer_id for row in generation.replay_completions
        }
        for layer_id in replay_layer_ids:
            layer = layers[layer_id]
            authorization = authorize_completed_units_for_layer(
                shot,
                layer_id,
                layer.stages,
                expected_plan_hash=selected_layer_capsule_digest(
                    shot,
                    layer_id,
                    selected,
                ),
                selected_authority=selected,
            )
            observed.update(
                {
                    f"{layer_id}:{unit_id}": digest
                    for unit_id, digest in authorization.receipts
                }
            )
        return all(
            observed.get(row.identity) == row.completion_receipt_digest
            for row in generation.replay_completions
        )

    monkeypatch.setattr(
        judgment_debt_state,
        "payment_generation_is_current",
        receipt_lineage_is_current,
    )


def _pay_current_generation(root: Path, monkeypatch):
    layer, terminal, selected = _published_finalized_root(root)
    unit = layer.stages[0]
    assert selected.assertion.bundle is not None
    definition, activation = _debt_authority(
        selected.assertion.bundle.digest,
        layer,
        unit,
    )
    _install_debt_catalog(monkeypatch, definition, activation)
    replay = _paid_replay_prefix(root, layer, unit, terminal)
    due = mark_judgment_debt_due(
        root,
        definition.digest,
        layer_id=str(layer.id),
        replay_receipt=replay,
        selected_authority=selected,
    )
    satisfied = resolve_current_judgment_debt(
        root,
        definition.digest,
        outcome="satisfied",
        evidence_digest=_digest("qualified judgment"),
        selected_authority=selected,
    )
    assert due.payment_generation_digest == satisfied.payment_generation_digest
    return layer, unit, definition, satisfied, selected.selection_token


def test_satisfied_debt_survives_layer_change_when_paid_receipts_are_preserved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layer, unit, _definition, satisfied, execution_token = _pay_current_generation(
        tmp_path,
        monkeypatch,
    )

    successor = _selected_documents(tmp_path)
    _add_layer_only_acceptance_authority(successor)
    _commit_document_successor(tmp_path, successor)

    selected = resolve_selected_authority(tmp_path)
    [(_definition, _activation, current)] = current_judgment_debt_states(tmp_path)
    assert current == satisfied
    assert current.payment_generation_digest == satisfied.payment_generation_digest
    state = load_unit_state(tmp_path, str(layer.id))
    assert state["units"][unit.id]["status"] == "passed"
    assert state["units"][unit.id]["completion_receipt"] is not None
    assert selected.selection_token != execution_token


def test_satisfied_debt_cannot_resurrect_after_unit_changing_a_b_a(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layer, unit, definition, satisfied, _execution_token = _pay_current_generation(
        tmp_path,
        monkeypatch,
    )
    generation_a = _selected_documents(tmp_path)
    generation_b = deepcopy(generation_a)
    generation_b["layers.json"]["layers"][0]["stages"][0]["title"] = (
        "Changed paid unit"
    )

    _commit_document_successor(tmp_path, generation_b)
    [(_definition, _activation, changed)] = current_judgment_debt_states(tmp_path)
    assert changed.status == "pending_not_due"
    _commit_document_successor(tmp_path, generation_a)

    [(_definition, _activation, restored)] = current_judgment_debt_states(tmp_path)
    assert restored.status == "pending_not_due"
    assert restored.payment_generation_digest is None
    assert restored != satisfied
    state = load_unit_state(tmp_path, str(layer.id))
    assert state["units"][unit.id]["status"] != "passed"
    assert definition.digest == _definition.digest
