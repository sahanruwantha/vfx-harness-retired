from __future__ import annotations

import pytest

from tests.architecture.test_staged_architecture import _unit
from tests.unit.test_unit_state_claims import _PLAN_A, _pass_unclaimed
from vfx_harness.orchestration import unit_state
from vfx_harness.orchestration.authority_unit_completion_sources import (
    PreservedUnitCompletionSourceConflict,
    prepare_preserved_unit_completion_sources,
    require_preserved_unit_completion_sources,
)

_UNIT_GENERATION = "f" * 64


def _prepared_closure(tmp_path):
    unit = _unit("form")
    units = (unit,)
    unit_state.initialize(tmp_path, "1", units, plan_hash=_PLAN_A)
    receipt = _pass_unclaimed(tmp_path, unit, units)
    checkpoint = unit_state.load(tmp_path, "1")["units"][unit.id]["checkpoint"]
    prepared = prepare_preserved_unit_completion_sources(
        tmp_path,
        layer_id="1",
        unit=unit,
        unit_generation_digest=_UNIT_GENERATION,
        receipt=receipt,
        checkpoint=checkpoint,
    )
    return unit, receipt, checkpoint, prepared


def test_preserved_completion_sources_bind_receipt_evaluator_and_checkpoint(
    tmp_path,
) -> None:
    _unit_row, receipt, _checkpoint, prepared = _prepared_closure(tmp_path)

    assert prepared.completion_receipt_digest == receipt.receipt_digest
    assert require_preserved_unit_completion_sources(prepared) == receipt


def test_preserved_completion_sources_refuse_checkpoint_outside_receipt(
    tmp_path,
) -> None:
    unit, receipt, checkpoint, _prepared = _prepared_closure(tmp_path)
    changed = dict(checkpoint)
    changed["candidate_hash"] = "0" * 64

    with pytest.raises(
        PreservedUnitCompletionSourceConflict,
        match=r"checkpoint for 1\.form does not match its exact receipt",
    ):
        prepare_preserved_unit_completion_sources(
            tmp_path,
            layer_id="1",
            unit=unit,
            unit_generation_digest=_UNIT_GENERATION,
            receipt=receipt,
            checkpoint=changed,
        )


def test_preserved_completion_source_recheck_refuses_script_mutation(tmp_path) -> None:
    _unit_row, receipt, _checkpoint, prepared = _prepared_closure(tmp_path)
    (tmp_path / receipt.script_path).write_text(
        "# changed after transition preparation\n",
        encoding="utf-8",
    )

    with pytest.raises(
        PreservedUnitCompletionSourceConflict,
        match=r"source closure for 1\.form is invalid",
    ):
        require_preserved_unit_completion_sources(prepared)
