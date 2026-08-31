"""Typed terminal handling for one unaccepted builder work unit."""

from __future__ import annotations

from collections.abc import Callable
from functools import partial

from vfx_harness.agents.builder.falsify import (
    _record_bound_contract_falsification,
    _record_contract_gap_falsification,
    _record_unsatisfiable_pair_falsification,
)
from vfx_harness.agents.builder.models import BuildAuthorityDefect
from vfx_harness.observability.log import log
from vfx_harness.orchestration.unit_state import block_dependents, transition
from vfx_harness.orchestration.unit_state import load as load_unit_state


def handle_unpassed_unit(
    shot,
    layer,
    unit,
    milestone,
    ledger,
    unit_status: str,
    *,
    selected_authority,
    publish: Callable,
) -> None:
    """Publish only the typed failure state authorized by the terminal unit outcome."""

    def mark_failed(reason: str, metadata: dict | None = None) -> None:
        publish(
            f"mark unit {layer.id}.{unit.id} failed",
            partial(
                transition,
                shot.folder,
                str(layer.id),
                unit.id,
                "failed",
                reason=reason,
                metadata=metadata,
            ),
        )

    finding = None
    if unit_status == "contract_gap":
        try:
            finding = publish(
                f"record unit {layer.id}.{unit.id} contract falsification",
                partial(
                    _record_contract_gap_falsification,
                    shot,
                    layer,
                    unit,
                    selected_authority=selected_authority,
                ),
            )
            log(
                "plan hypothesis falsified by executable evidence → "
                f"{finding['record_id']} (transactional replan required)",
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
            finding = publish(
                f"record unit {layer.id}.{unit.id} executable falsification",
                lambda: _record_unsatisfiable_pair_falsification(
                    shot,
                    layer,
                    unit,
                    milestone,
                    ledger,
                    selected_authority=selected_authority,
                )
                or _record_bound_contract_falsification(
                    shot,
                    layer,
                    unit,
                    milestone,
                    ledger,
                    selected_authority=selected_authority,
                ),
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
                    f"{finding['record_id']} (transactional replan required)",
                    1,
                )
    else:
        mark_failed(unit_status)

    publish(
        f"block dependants of unit {layer.id}.{unit.id}",
        partial(
            block_dependents,
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
