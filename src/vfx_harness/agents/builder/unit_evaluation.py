"""Independent evaluator receipt staging for one exact builder attempt."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from vfx_harness.agents.builder.attempt_guard import UnitAttemptGuard
from vfx_harness.domain.work_units import WorkUnit
from vfx_harness.orchestration import unit_evaluation_receipts
from vfx_harness.orchestration.unit_evaluation_receipts import (
    ExecutedReplayInput,
    StoredUnitEvaluationReceipt,
)


def publish_unit_evaluation_outcome(
    folder,
    layer_id: str,
    unit: WorkUnit,
    attempt_guard: UnitAttemptGuard,
    *,
    result: str,
    canonical_verdicts: Sequence[tuple[tuple[int, str], Mapping[str, Any]]],
    ledger_slot: Mapping[str, Any],
    replay_inputs: Sequence[ExecutedReplayInput],
    candidate_path: str | None,
) -> StoredUnitEvaluationReceipt:
    """Stage evaluator bytes unlocked, then bind them in one short guarded commit."""

    attempt_guard.check(
        f"start unit {unit.id} independent evaluation receipt staging"
    )
    prepared = unit_evaluation_receipts.prepare_unit_evaluation_receipt(
        folder,
        str(layer_id),
        unit,
        attempt_guard.claim,
        result=result,
        canonical_verdicts=canonical_verdicts,
        ledger_slot=ledger_slot,
        replay_inputs=replay_inputs,
        candidate_path=candidate_path,
    )
    try:
        committed = attempt_guard.publish(
            f"publish unit {unit.id} independent evaluation receipt",
            lambda: unit_evaluation_receipts.commit_unit_evaluation_receipt(prepared),
        )
        return unit_evaluation_receipts.finalize_committed_unit_evaluation_receipt(
            prepared,
            committed,
        )
    finally:
        unit_evaluation_receipts.discard_prepared_unit_evaluation_receipt(prepared)
