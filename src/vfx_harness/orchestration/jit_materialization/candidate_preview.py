"""Projection of unpublished materialization state into an isolated gate view."""

from __future__ import annotations

import hashlib
from pathlib import Path

from vfx_harness.observability.provenance import atomic_write
from vfx_harness.orchestration.jit_materialization.schema import MaterializedLayer
from vfx_harness.orchestration.unit_state import apply_replan, load


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
    state = load(shot, layer_id)
    if not state:
        return
    old_plan_hash = str(state.get("plan_hash") or "")
    if not old_plan_hash:
        raise ValueError(
            f"layer {layer_id} candidate preview cannot project unit state without "
            "the durable predecessor plan_hash"
        )

    source_dir = shot / "state" / "work-units"
    preview_dir = view / "state" / "work-units"
    if preview_dir.is_symlink():
        preview_dir.unlink()
        preview_dir.mkdir(parents=True)
        for child in source_dir.iterdir():
            if child.name.endswith(".lock"):
                continue
            (preview_dir / child.name).symlink_to(child)
    else:
        preview_dir.mkdir(parents=True, exist_ok=True)
    source = source_dir / f"layer_{layer_id}.json"
    target = preview_dir / source.name
    if target.is_symlink() or target.exists():
        target.unlink()
    atomic_write(target, source.read_text(encoding="utf-8"))

    apply_replan(
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
