"""Selected-snapshot lifecycle filtering for builder scene evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from vfx_harness.domain.brief import Shot
from vfx_harness.domain.contracts import active_for, load_document
from vfx_harness.orchestration.plan_authority import selected_artifact_path

if TYPE_CHECKING:
    from vfx_harness.orchestration.authority_selection import ResolvedSelectedAuthority


def _scene_contract_path(shot: Shot, selected_authority) -> Path:
    if selected_authority is None:
        return selected_artifact_path(shot.folder, "scene_checks.json")
    if selected_authority.plan is None:
        return shot.folder / "scene_checks.json"
    return selected_authority.artifact_paths["scene_checks.json"]


def _scene_ids_active_on_layer(
    shot: Shot,
    layer_id: str,
    ids: set[str],
    frames: tuple[int, ...] | list[int],
    *,
    selected_authority: ResolvedSelectedAuthority | None = None,
) -> set[str]:
    path = _scene_contract_path(shot, selected_authority)
    if not path.is_file():
        return set(ids)
    try:
        rows = {
            str(row.get("id")): row
            for row in load_document(path, "contracts")
            if isinstance(row, dict) and row.get("id")
        }
    except (OSError, ValueError, json.JSONDecodeError):
        return set(ids)
    due: set[str] = set()
    frame_list = tuple(int(frame) for frame in frames)
    for contract_id in ids:
        row = rows.get(contract_id)
        if row is None:
            due.add(contract_id)
        elif row.get("frame") is None:
            if active_for(row, layer_id):
                due.add(contract_id)
        elif any(active_for(row, layer_id, frame) for frame in frame_list):
            due.add(contract_id)
    return due


def _scene_ids_active_at_declared_frames(
    shot: Shot,
    layer_id: str,
    ids: set[str],
    fallback_frames: tuple[int, ...] | list[int],
    *,
    selected_authority: ResolvedSelectedAuthority | None = None,
) -> set[str]:
    path = _scene_contract_path(shot, selected_authority)
    if not path.is_file():
        return set(ids)
    try:
        rows = {
            str(row.get("id")): row
            for row in load_document(path, "contracts")
            if isinstance(row, dict) and row.get("id")
        }
    except (OSError, ValueError, json.JSONDecodeError):
        return set(ids)
    fallbacks = tuple(int(frame) for frame in fallback_frames)
    due: set[str] = set()
    for contract_id in ids:
        row = rows.get(contract_id)
        if row is None:
            due.add(contract_id)
            continue
        declared = row.get("frames")
        if not isinstance(declared, (list, tuple)) or not declared:
            declared = [row.get("frame")] if row.get("frame") is not None else fallbacks
        if any(active_for(row, layer_id, int(frame)) for frame in declared):
            due.add(contract_id)
    return due
