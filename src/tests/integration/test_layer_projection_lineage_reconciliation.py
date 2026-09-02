from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest

from tests.integration.test_authority_receipt_lineage import (
    _commit_same_capsule_successor,
    _published_finalized_root,
)
from vfx_harness.agents.builder import layer as builder_layer
from vfx_harness.agents.builder.layer_finalization_guard import (
    LayerFinalizationReceiptGuard,
)
from vfx_harness.domain.brief import load_shot
from vfx_harness.orchestration.authority_selection import resolve_selected_authority
from vfx_harness.orchestration.layer_finalization_state import (
    authorize_terminal_layer_finalization_mutation,
)
from vfx_harness.orchestration.layer_outcome_paths import layer_outcome_path
from vfx_harness.orchestration.layer_plans import (
    finalization_layer_outcome_authority,
    prepare_layer_outcome,
)
from vfx_harness.orchestration.unit_state import load as load_unit_state


def test_preserved_terminal_receipt_restores_exact_outcome_under_successor(
    tmp_path,
    monkeypatch,
) -> None:
    layer, receipt, execution_authority = _published_finalized_root(tmp_path)
    outcome = layer_outcome_path(tmp_path, layer.id)

    async def refuse_new_finalization(*_args, **_kwargs):
        raise AssertionError("projection reconciliation scheduled new paid finalization")

    monkeypatch.setattr(
        builder_layer,
        "finalize_composed_layer",
        refuse_new_finalization,
    )
    # Replace the fixture writer's equivalent JSON with the production projection
    # encoding before that exact source is preserved into the successor generation.
    outcome.unlink()
    asyncio.run(
        builder_layer.build_layer(
            load_shot(tmp_path),
            layer,
            object(),
            selected_authority=execution_authority,
            verbose=False,
        )
    )
    expected_outcome = outcome.read_bytes()
    accepted_unit = deepcopy(
        load_unit_state(tmp_path, layer.id)["units"][layer.stages[0].id]
    )

    _commit_same_capsule_successor(tmp_path)
    selected = resolve_selected_authority(tmp_path)
    assert selected.selection_token != execution_authority.selection_token
    outcome.unlink()

    asyncio.run(
        builder_layer.build_layer(
            load_shot(tmp_path),
            layer,
            object(),
            selected_authority=selected,
            verbose=False,
        )
    )

    assert outcome.read_bytes() == expected_outcome
    state = load_unit_state(tmp_path, layer.id)
    assert state["units"][layer.stages[0].id] == accepted_unit
    assert (
        state["layer_finalization"]["terminal_receipt"]
        == receipt.as_dict()
    )


@pytest.mark.parametrize("successor", [False, True], ids=["current", "successor"])
@pytest.mark.parametrize("source_change", ["mutated", "deleted"])
def test_outcome_reconciliation_refuses_preparation_source_drift(
    tmp_path,
    *,
    successor: bool,
    source_change: str,
) -> None:
    layer, receipt, execution_authority = _published_finalized_root(tmp_path)
    outcome = layer_outcome_path(tmp_path, layer.id)
    original_outcome = outcome.read_bytes()
    replay_point = (
        receipt.evaluation_receipt.replay_receipts[0]
        .receipt.observation.points[0]
    )
    reference = tmp_path / replay_point.ref
    assert reference.is_file()

    selected = execution_authority
    if successor:
        _commit_same_capsule_successor(tmp_path)
        selected = resolve_selected_authority(tmp_path)
        assert selected.selection_token != execution_authority.selection_token
    finalization_authorization = authorize_terminal_layer_finalization_mutation(
        tmp_path,
        receipt,
        layer.stages,
        selected,
    )
    authority = finalization_layer_outcome_authority(
        layer,
        selected,
        finalization_receipt=receipt,
        finalization_authorization=finalization_authorization,
    )
    guard = LayerFinalizationReceiptGuard(
        tmp_path,
        receipt,
        layer.stages,
        selected,
    )

    if source_change == "mutated":
        reference.write_bytes(b"changed after terminal finalization")
    else:
        reference.unlink()

    with pytest.raises(
        ValueError,
        match=(
            r"(?:manifest file .*|canonical\[0\] reference.*)"
            r"bytes changed or are missing"
        ),
    ):
        prepare_layer_outcome(
            tmp_path,
            layer,
            best=dict(receipt.projection["best"]),
            canonical=[
                ((int(row["frame"]), str(row["ref"])), dict(row["verdict"]))
                for row in receipt.canonical
            ],
            finalization_receipt=receipt,
            blender_version=str(receipt.projection["blender_version"]),
            selected_authority=selected,
            authority=authority,
            guard=guard,
        )

    assert outcome.read_bytes() == original_outcome
    assert list(outcome.parent.glob(f".{outcome.name}.prepared.*")) == []
