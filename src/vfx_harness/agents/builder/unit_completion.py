"""Receipt-backed work-unit completion and debt reconciliation."""

from __future__ import annotations

from pathlib import Path

from vfx_harness.domain.unit_attempts import UnitAttemptClaim
from vfx_harness.domain.unit_completion_receipts import UnitCompletionReceipt
from vfx_harness.domain.work_units import WorkUnit
from vfx_harness.orchestration.authority_selection import ResolvedSelectedAuthority
from vfx_harness.orchestration.plan_due import require_due_clear, resolve_unit_completion
from vfx_harness.orchestration.unit_completion_state import (
    completed_unit_attempt_guard,
)
from vfx_harness.orchestration.unit_state import load as load_unit_state
from vfx_harness.orchestration.unit_state_claims import complete_unit_attempt


def resolve_completed_unit(
    folder: str | Path,
    layer_id: str,
    unit: WorkUnit,
    units: tuple[WorkUnit, ...],
    receipt: UnitCompletionReceipt,
    *,
    expected_plan_hash: str,
    selected_authority: ResolvedSelectedAuthority,
) -> tuple[str, ...]:
    """Resolve debt from a current receipt without holding state locks over ledger I/O.

    The first guard establishes the exact receipt/checkpoint boundary.  Resolution rows
    are immutable evidence projections bound to that receipt digest, so they may be
    appended after the guard is released: a concurrent invalidation makes the row inert
    instead of making it authoritative for a different generation.  The final due read
    independently snapshots current receipts and refuses if the unit was invalidated
    while the resolution ledger was being published.
    """

    with completed_unit_attempt_guard(
        folder,
        layer_id,
        unit.id,
        units,
        receipt,
        expected_plan_hash=expected_plan_hash,
        selection_token=selected_authority.selection_token,
    ) as current:
        state = load_unit_state(folder, layer_id)
        checkpoint_hash = str(
            state["units"][unit.id]["checkpoint"]["candidate_hash"]
        )
    resolved = resolve_unit_completion(
        folder,
        layer=layer_id,
        unit=unit.id,
        completion_receipt=current,
        checkpoint_hash=checkpoint_hash,
        selected_authority=selected_authority,
    )
    require_due_clear(
        folder,
        layer=layer_id,
        unit=unit.id,
        completion=True,
        selected_authority=selected_authority,
    )
    return resolved


def complete_and_resolve_unit(
    folder: str | Path,
    layer_id: str,
    unit: WorkUnit,
    units: tuple[WorkUnit, ...],
    attempt: UnitAttemptClaim,
    *,
    expected_plan_hash: str,
    selected_authority: ResolvedSelectedAuthority,
    checkpoint_hash: str,
) -> UnitCompletionReceipt:
    """Publish executable acceptance first, then reconcile its separate debts."""

    receipt = complete_unit_attempt(
        folder,
        layer_id,
        unit.id,
        units,
        attempt,
        expected_plan_hash=expected_plan_hash,
        selection_token=selected_authority.selection_token,
        reason="all required unit claims passed",
        evidence=[
            "canonical-replay:passed",
            f"checkpoint:{checkpoint_hash}",
        ],
    )
    resolve_completed_unit(
        folder,
        layer_id,
        unit,
        units,
        receipt,
        expected_plan_hash=expected_plan_hash,
        selected_authority=selected_authority,
    )
    return receipt
