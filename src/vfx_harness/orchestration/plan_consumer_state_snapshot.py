"""Exact mutable-authority snapshot for a run-scoped plan consumer view."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from vfx_harness.infrastructure.trusted_files import (
    TrustedFileError,
    TrustedFileNotFound,
    read_trusted_file,
)
from vfx_harness.observability.run_artifacts import RunLayout
from vfx_harness.orchestration import (
    plan_bundle_integrity,
    plan_consumer_view_projection,
    unit_state_lock,
    unit_state_storage,
)
from vfx_harness.orchestration.layer_outcome_paths import layer_outcome_locator
from vfx_harness.orchestration.plan_consumer_view_mutation import (
    PlanConsumerViewMutationCapability,
    PlanConsumerViewMutationConflict,
)

PlanPublicationError = plan_bundle_integrity.PlanPublicationError


def _snapshot_ledger(
    layout: RunLayout,
    capability: PlanConsumerViewMutationCapability,
) -> None:
    # Ledger imports the plan-authority facade, so this dependency remains at the
    # transaction boundary instead of creating a module initialization cycle.
    from vfx_harness.orchestration.ledger import ledger_lock  # noqa: PLC0415

    source = layout.shot / "shot.json"
    with ledger_lock(source, exclusive=False):
        try:
            snapshot = read_trusted_file(
                layout.shot,
                source,
                "plan consumer ledger snapshot",
            )
        except TrustedFileNotFound:
            plan_consumer_view_projection.require_member_absent(
                capability,
                "shot.json",
            )
            return
        except TrustedFileError as exc:
            raise PlanPublicationError(str(exc)) from exc
    try:
        plan_consumer_view_projection.create_construction_ledger_snapshot(
            capability,
            snapshot.payload,
        )
    except PlanConsumerViewMutationConflict as exc:
        raise PlanPublicationError(str(exc)) from exc


def _snapshot_cross_run_state(
    layout: RunLayout,
    capability: PlanConsumerViewMutationCapability,
) -> None:
    source = layout.shot / "state"
    plan_consumer_view_projection.ensure_directory(capability, "state")
    if source.is_dir():
        for child in source.iterdir():
            if child.name in {
                "jit-layers",
                "plan-resolutions.jsonl",
                "publication-locks",
                "work-units",
            }:
                continue
            plan_consumer_view_projection.create_verified_state_symlink(
                capability,
                Path("state") / child.name,
                child,
            )


def _snapshot_work_units(
    layout: RunLayout,
    capability: PlanConsumerViewMutationCapability,
    layers: Sequence[Mapping[str, object]],
) -> None:
    plan_consumer_view_projection.ensure_directory(
        capability,
        Path("state") / "work-units",
    )
    for layer in layers:
        layer_id = str(layer.get("id") or "").strip()
        state_path = unit_state_lock.unit_state_path(layout.shot, layer_id)
        state_bytes = unit_state_storage.read(state_path)
        if state_bytes is not None:
            plan_consumer_view_projection.create_regular_file(
                capability,
                Path("state") / "work-units" / state_path.name,
                state_bytes,
            )


def _snapshot_layer_outcomes(
    layout: RunLayout,
    capability: PlanConsumerViewMutationCapability,
    layers: Sequence[Mapping[str, object]],
) -> None:
    plan_consumer_view_projection.ensure_directory(
        capability,
        Path("plans") / "outcomes",
    )
    for layer in layers:
        layer_id = str(layer.get("id") or "").strip()
        locator = layer_outcome_locator(layer_id)
        source = layout.shot / locator
        try:
            snapshot = read_trusted_file(
                layout.shot,
                source,
                f"plan consumer sealed outcome for layer {layer_id}",
            )
        except TrustedFileNotFound:
            continue
        except TrustedFileError as exc:
            raise PlanPublicationError(str(exc)) from exc
        plan_consumer_view_projection.create_regular_file(
            capability,
            locator,
            snapshot.payload,
        )


def snapshot_consumer_execution_authority(
    layout: RunLayout,
    capability: PlanConsumerViewMutationCapability,
    layers: Sequence[Mapping[str, object]],
) -> None:
    """Capture one exact ledger, unit-state, and layer-outcome generation."""

    try:
        _snapshot_ledger(layout, capability)
        _snapshot_cross_run_state(layout, capability)
        _snapshot_work_units(layout, capability, layers)
        _snapshot_layer_outcomes(layout, capability, layers)
    except PlanConsumerViewMutationConflict as exc:
        raise PlanPublicationError(str(exc)) from exc
