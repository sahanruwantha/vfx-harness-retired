"""Projection of unpublished materialization state into an isolated gate view."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from vfx_harness.observability.provenance import atomic_write
from vfx_harness.orchestration.jit_materialization.schema import MaterializedLayer
from vfx_harness.orchestration.layer_outcome_paths import layer_outcome_locator
from vfx_harness.orchestration.plan_bundle_integrity import read_real_file_snapshot
from vfx_harness.orchestration.unit_state import apply_replan, load_snapshot


def _prepare_preview_state_directory(view: Path) -> Path:
    """Replace a live-state projection with one isolated, real scratch directory."""

    preview_dir = view / "state" / "work-units"
    if preview_dir.is_symlink():
        preview_dir.unlink()
    elif preview_dir.exists():
        if not preview_dir.is_dir():
            raise ValueError(
                "candidate preview work-unit state must be a directory or the exact "
                "read-only source projection"
            )
        for child in preview_dir.iterdir():
            if child.is_symlink() or not child.is_file():
                raise ValueError(
                    "candidate preview work-unit state must contain only real snapshot "
                    "files: "
                    f"{child.name}"
                )
    preview_dir.mkdir(parents=True, exist_ok=True)
    return preview_dir


def _projected_statuses(state: dict[str, Any]) -> dict[str, str]:
    units = state.get("units")
    if not isinstance(units, dict):
        raise ValueError("candidate preview unit state must contain a units object")
    statuses: dict[str, str] = {}
    for unit_id, slot in units.items():
        if not isinstance(slot, dict) or not isinstance(slot.get("status"), str):
            raise ValueError(
                f"candidate preview unit state has no typed status for {unit_id}"
            )
        statuses[str(unit_id)] = str(slot["status"])
    return statuses


def _record_ids(record: dict[str, Any], field: str) -> frozenset[str]:
    value = record.get(field, [])
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"candidate preview replan {field} must be a string list")
    return frozenset(value)


def _project_candidate_ledger(
    view: Path,
    layer_id: str,
    record: dict[str, Any],
    state: dict[str, Any],
) -> bool:
    """Reopen only ledger slots whose authority the preview replan replaced.

    Returns whether the projected layer is no longer fully passed.  The aggregate
    layer slot is a scheduling projection, while ``layer@unit`` slots mirror the
    exact surviving unit status (or ``superseded`` for retired identities).
    """

    statuses = _projected_statuses(state)
    reopened = not statuses or any(status != "passed" for status in statuses.values())
    if not reopened:
        return False

    path = view / "shot.json"
    if not path.exists() and not path.is_symlink():
        return True
    snapshot = read_real_file_snapshot(view, path, "candidate preview ledger")
    try:
        ledger = json.loads(snapshot.payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("candidate preview shot.json is not readable JSON") from exc
    if not isinstance(ledger, dict) or not isinstance(ledger.get("milestones"), dict):
        raise ValueError("candidate preview shot.json must contain a milestones object")

    added = _record_ids(record, "added")
    removed = _record_ids(record, "removed")
    changed = _record_ids(record, "changed")
    invalidated = _record_ids(record, "invalidated")
    preserved = _record_ids(record, "preserved")
    orphaned = _record_ids(record, "orphaned") if "orphaned" in record else frozenset()
    affected = added | removed | changed | invalidated | preserved | orphaned
    milestones = ledger["milestones"]

    aggregate = milestones.get(layer_id)
    if aggregate is not None:
        if not isinstance(aggregate, dict):
            raise ValueError(
                f"candidate preview ledger milestone {layer_id} must be an object"
            )
        aggregate["status"] = "pending"

    unit_prefix = f"{layer_id}@"
    for milestone_id, slot in milestones.items():
        if not isinstance(milestone_id, str) or not milestone_id.startswith(unit_prefix):
            continue
        unit_id = milestone_id[len(unit_prefix) :]
        if unit_id not in affected:
            continue
        if not isinstance(slot, dict):
            raise ValueError(
                f"candidate preview ledger milestone {milestone_id} must be an object"
            )
        slot["status"] = statuses.get(unit_id, "superseded")

    atomic_write(path, json.dumps(ledger, indent=2) + "\n")
    return True


def _remove_reopened_outcome(view: Path, layer_id: str) -> None:
    """Remove one affected sealed outcome from scratch, never from live authority."""

    outcomes = view / "plans" / "outcomes"
    if outcomes.is_symlink() or not outcomes.is_dir():
        raise ValueError(
            "candidate preview outcomes must be an isolated real directory"
        )
    path = view / layer_outcome_locator(layer_id)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(
            f"candidate preview outcome must be absent or a real regular file: {path}"
        )
    path.unlink(missing_ok=True)


def project_candidate_unit_state(
    shot: Path,
    view: Path,
    materialized: MaterializedLayer,
) -> None:
    """Apply the exact state-backed replan to preview-local durable state.

    A replacement candidate names a different unit DAG before publication while the
    shot must retain its accepted predecessor. The isolated view therefore receives
    the same transactional replan without mutating selected state.
    """

    layer_id = str(materialized.layer.id)
    state, state_bytes = load_snapshot(shot, layer_id)
    preview_dir = _prepare_preview_state_directory(view)
    if not state:
        if state_bytes is not None:
            raise ValueError(
                f"layer {layer_id} candidate preview read an empty parsed state from "
                "present bytes"
            )
        return
    old_plan_hash = str(state.get("plan_hash") or "")
    if not old_plan_hash:
        raise ValueError(
            f"layer {layer_id} candidate preview cannot project unit state without "
            "the durable predecessor plan_hash"
        )

    assert state_bytes is not None
    target = preview_dir / f"layer_{layer_id}.json"
    try:
        snapshot_text = state_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:  # load_snapshot normally rejects this first.
        raise ValueError(
            f"layer {layer_id} candidate preview state is not UTF-8"
        ) from exc
    atomic_write(target, snapshot_text)

    record = apply_replan(
        view,
        layer_id,
        (),
        materialized.layer.stages,
        old_plan_hash=old_plan_hash,
        new_plan_hash=hashlib.sha256((view / "layers.json").read_bytes()).hexdigest(),
        owner="vfx-harness.candidate-preview",
        trigger="project unpublished materialization through the transactional replan",
        evidence=["scratch/candidate-materialization"],
        state_backed_base=True,
    )
    projected, _projected_bytes = load_snapshot(view, layer_id)
    if _project_candidate_ledger(view, layer_id, record, projected):
        _remove_reopened_outcome(view, layer_id)
