"""Typed terminal handling for one unaccepted builder work unit."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vfx_harness.agents.builder.falsify import (
    _record_bound_contract_falsification,
    _record_contract_gap_falsification,
    _record_unsatisfiable_pair_falsification,
)
from vfx_harness.agents.builder.models import BuildAuthorityDefect
from vfx_harness.observability.log import log
from vfx_harness.orchestration.hypothesis_falsification_projection import (
    FalsificationProjectionPending,
)
from vfx_harness.orchestration.unit_state import block_dependents
from vfx_harness.orchestration.unit_state import load as load_unit_state
from vfx_harness.orchestration.unit_state_claims import fail_unit_attempt

if TYPE_CHECKING:
    from vfx_harness.domain.unit_attempts import UnitAttemptClaim


def handle_unpassed_unit(
    shot,
    layer,
    unit,
    milestone,
    ledger,
    unit_status: str,
    *,
    attempt: UnitAttemptClaim,
    selected_authority,
    publish_external,
) -> None:
    """Publish only the typed failure state authorized by the terminal unit outcome."""

    def mark_failed(reason: str, metadata: dict | None = None) -> None:
        # Exact-attempt state transactions acquire selection SH -> unit state EX
        # themselves.  Wrapping one in UnitAttemptGuard.publish would hold state SH
        # and attempt an illegal SH -> EX upgrade.
        fail_unit_attempt(
            shot.folder,
            str(layer.id),
            unit.id,
            layer.stages,
            attempt,
            expected_plan_hash=attempt.plan_hash,
            selection_token=selected_authority.selection_token,
            reason=reason,
            evidence=[f"builder-outcome:{unit_status}"],
            metadata=metadata,
        )

    finding = None
    if unit_status == "contract_gap":
        try:
            finding = _record_contract_gap_falsification(
                shot,
                layer,
                unit,
                attempt=attempt,
                selected_authority=selected_authority,
            )
            log(
                "plan hypothesis falsified by executable evidence → "
                f"{finding['record_id']} (validated authority amendment required)",
                1,
            )
        except FalsificationProjectionPending as pending:
            finding = pending.payload
            log(
                "plan hypothesis falsification committed; its optional JSON "
                f"projection will be reconciled from state → {pending.record_id}",
                1,
            )
        except (OSError, ValueError, KeyError) as exc:
            mark_failed(
                "contract gap could not produce typed falsification evidence",
                {"error": str(exc)},
            )
            unit_status = "failed_unrecorded_plan_finding"
    elif unit_status == "failed":
        try:
            finding = (
                _record_unsatisfiable_pair_falsification(
                    shot,
                    layer,
                    unit,
                    milestone,
                    ledger,
                    attempt=attempt,
                    selected_authority=selected_authority,
                )
                or _record_bound_contract_falsification(
                    shot,
                    layer,
                    unit,
                    milestone,
                    ledger,
                    attempt=attempt,
                    selected_authority=selected_authority,
                )
            )
        except FalsificationProjectionPending as pending:
            finding = pending.payload
            log(
                "plan hypothesis falsification committed; its optional JSON "
                f"projection will be reconciled from state → {pending.record_id}",
                1,
            )
        except (OSError, ValueError, KeyError) as exc:
            mark_failed(
                "falsified bound contracts could not produce typed evidence",
                {"error": str(exc)},
            )
            unit_status = "failed_unrecorded_plan_finding"
        else:
            if finding is None:
                mark_failed(unit_status)
            else:
                log(
                    "plan hypothesis falsified by executable evidence → "
                    f"{finding['record_id']} (validated authority amendment required)",
                    1,
                )
    else:
        mark_failed(unit_status)

    publish_external(
        f"block dependants of unit {layer.id}.{unit.id}",
        lambda: block_dependents(
            shot.folder,
            str(layer.id),
            unit.id,
            layer.stages,
            reason=f"dependency {unit.id} ended {unit_status}",
        ),
    )
    if finding is None:
        return
    state_after = load_unit_state(shot.folder, str(layer.id))
    unpassed = [
        f"{uid}={row.get('status')}"
        for uid, row in (state_after.get("units") or {}).items()
        if row.get("status") != "passed"
    ]
    raise BuildAuthorityDefect(
        finding,
        stage="builder",
        exit_code=7,
        legacy_detail=(
            f"layer {layer.id} did not accept every work unit: " + ", ".join(unpassed)
        ),
    )
