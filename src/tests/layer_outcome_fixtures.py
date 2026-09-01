"""Test-only construction of sealed-layer outcome fixtures."""

from __future__ import annotations

import json
from pathlib import Path

from vfx_harness.orchestration.layer_outcome_paths import layer_outcome_path
from vfx_harness.orchestration.layer_plans import build_layer_outcome_record


def write_test_layer_outcome(
    folder: str | Path,
    layer,
    *,
    status: str,
    best: dict,
    canonical: list,
    run_id: str,
    attempt: int | None = None,
    blender_version: str,
    selected_authority=None,
) -> Path:
    """Write fixture bytes without exposing an unguarded production publication API."""

    record = build_layer_outcome_record(
        folder,
        layer,
        status=status,
        best=best,
        canonical=canonical,
        run_id=run_id,
        attempt=attempt,
        blender_version=blender_version,
        selected_authority=selected_authority,
    )
    path = layer_outcome_path(folder, str(layer.id))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return path
