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
from vfx_harness.orchestration import plan_bundle_integrity, unit_state_lock, unit_state_storage
from vfx_harness.orchestration.layer_outcome_paths import layer_outcome_locator

PlanPublicationError = plan_bundle_integrity.PlanPublicationError


def _snapshot_ledger(layout: RunLayout, destination: Path) -> None:
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
            return
        except TrustedFileError as exc:
            raise PlanPublicationError(str(exc)) from exc
    (destination / "shot.json").write_bytes(snapshot.payload)


def _snapshot_cross_run_state(layout: RunLayout, destination: Path) -> Path:
    source = layout.shot / "state"
    target = destination / "state"
    target.mkdir(exist_ok=True)
    if source.is_dir():
        for child in source.iterdir():
            if child.name in {
                "jit-layers",
                "plan-resolutions.jsonl",
                "work-units",
            }:
                continue
            (target / child.name).symlink_to(
                child,
                target_is_directory=child.is_dir(),
            )
    return target


def _snapshot_work_units(
    layout: RunLayout,
    target_state: Path,
    layers: Sequence[Mapping[str, object]],
) -> None:
    target = target_state / "work-units"
    target.mkdir()
    for layer in layers:
        layer_id = str(layer.get("id") or "").strip()
        state_path = unit_state_lock.unit_state_path(layout.shot, layer_id)
        state_bytes = unit_state_storage.read(state_path)
        if state_bytes is not None:
            (target / state_path.name).write_bytes(state_bytes)


def _snapshot_layer_outcomes(
    layout: RunLayout,
    destination: Path,
    layers: Sequence[Mapping[str, object]],
) -> None:
    target = destination / "plans" / "outcomes"
    target.mkdir()
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
        outcome_target = destination / locator
        outcome_target.parent.mkdir(parents=True, exist_ok=True)
        outcome_target.write_bytes(snapshot.payload)


def snapshot_consumer_execution_authority(
    layout: RunLayout,
    destination: Path,
    layers: Sequence[Mapping[str, object]],
) -> None:
    """Capture one exact ledger, unit-state, and layer-outcome generation."""

    _snapshot_ledger(layout, destination)
    target_state = _snapshot_cross_run_state(layout, destination)
    _snapshot_work_units(layout, target_state, layers)
    _snapshot_layer_outcomes(layout, destination, layers)
