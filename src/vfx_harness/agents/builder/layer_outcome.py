"""Builder adapters for exact, short-guard layer-outcome publication."""

from __future__ import annotations

from pathlib import Path

from vfx_harness.agents.builder.attempt_guard import UnitAttemptGuard
from vfx_harness.agents.builder.authority import (
    commit_selected_authority,
    require_selected_authority_unchanged,
)
from vfx_harness.orchestration.authority_selection import ResolvedSelectedAuthority
from vfx_harness.orchestration.layer_plans import (
    commit_layer_outcome,
    composition_layer_outcome_authority,
    discard_layer_outcome,
    prepare_layer_outcome,
    unit_attempt_layer_outcome_authority,
)


def publish_unit_layer_outcome(
    shot_folder: str | Path,
    layer,
    *,
    status: str,
    best: dict,
    canonical: list,
    run_id: str,
    ledger_attempt: int,
    blender_version: str,
    selected_authority: ResolvedSelectedAuthority,
    attempt_guard: UnitAttemptGuard,
) -> Path:
    """Stage all outcome bytes unlocked, then publish under the exact unit claim."""

    authority = unit_attempt_layer_outcome_authority(
        layer,
        selected_authority,
        attempt_guard.claim,
        run_id=run_id,
        ledger_attempt=ledger_attempt,
    )
    attempt_guard.check(f"start layer {layer.id} outcome preparation")
    prepared = prepare_layer_outcome(
        shot_folder,
        layer,
        status=status,
        best=best,
        canonical=canonical,
        run_id=run_id,
        attempt=ledger_attempt,
        blender_version=blender_version,
        selected_authority=selected_authority,
        authority=authority,
    )
    try:
        return attempt_guard.publish(
            f"publish layer {layer.id} outcome",
            lambda: commit_layer_outcome(prepared, authority=authority),
        )
    finally:
        discard_layer_outcome(prepared)


def publish_composed_layer_outcome(
    shot_folder: str | Path,
    layer,
    *,
    status: str,
    best: dict,
    canonical: list,
    run_id: str,
    ledger_attempt: int,
    blender_version: str,
    selected_authority: ResolvedSelectedAuthority,
) -> Path:
    """Stage composed evidence unlocked, then commit under exact selected authority."""

    authority = composition_layer_outcome_authority(
        layer,
        selected_authority,
        run_id=run_id,
        ledger_attempt=ledger_attempt,
    )
    require_selected_authority_unchanged(
        shot_folder,
        selected_authority,
        operation=f"start composed layer {layer.id} outcome preparation",
    )
    prepared = prepare_layer_outcome(
        shot_folder,
        layer,
        status=status,
        best=best,
        canonical=canonical,
        run_id=run_id,
        attempt=ledger_attempt,
        blender_version=blender_version,
        selected_authority=selected_authority,
        authority=authority,
    )
    try:
        return commit_selected_authority(
            shot_folder,
            selected_authority,
            operation=f"publish composed layer {layer.id} outcome",
            mutation=lambda: commit_layer_outcome(prepared, authority=authority),
        )
    finally:
        discard_layer_outcome(prepared)
