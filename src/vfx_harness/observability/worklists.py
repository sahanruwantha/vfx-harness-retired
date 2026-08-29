"""Digest-bound durable builder worklists.

A layer is a scheduling boundary, not a worklist identity. Rematerialization may replace
every unit while retaining the layer id, so layer-only files can never authorize a new
unit's completion state.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from vfx_harness.observability import run_artifacts
from vfx_harness.observability.provenance import atomic_write

SCHEMA = "vfx-harness.builder-worklist/v2"
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def _safe_id(value: str, field: str) -> str:
    token = str(value).strip()
    if not _SAFE_ID.fullmatch(token):
        raise ValueError(f"invalid worklist {field}: {value!r}")
    return token


def unit_worklist_path(
    shot_folder: str | Path,
    *,
    layer_id: str,
    unit_id: str,
    unit_hash: str,
) -> Path:
    """Return the only durable worklist path authorized for one unit generation."""
    layer = _safe_id(layer_id, "layer_id")
    unit = _safe_id(unit_id, "unit_id")
    digest = str(unit_hash).strip().lower()
    if not _DIGEST.fullmatch(digest):
        raise ValueError(f"invalid worklist unit_hash: {unit_hash!r}")
    return (
        run_artifacts.shot_state_dir(shot_folder)
        / "worklists"
        / f"layer-{layer}"
        / unit
        / f"{digest}.json"
    )


def empty_worklist(*, layer_id: str, unit_id: str, unit_hash: str) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "layer_id": str(layer_id),
        "unit_id": str(unit_id),
        "unit_hash": str(unit_hash),
        "items": [],
        "done": [],
        "notes": [],
    }


def load_unit_worklist(
    shot_folder: str | Path,
    *,
    layer_id: str,
    unit_id: str,
    unit_hash: str,
) -> tuple[Path, dict[str, Any]]:
    """Load and identity-check one exact unit generation; absent means empty."""
    path = unit_worklist_path(
        shot_folder, layer_id=layer_id, unit_id=unit_id, unit_hash=unit_hash
    )
    if not path.is_file():
        return path, empty_worklist(
            layer_id=layer_id, unit_id=unit_id, unit_hash=unit_hash
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        raise ValueError(f"unsupported builder worklist schema at {path}")
    expected = {
        "layer_id": str(layer_id),
        "unit_id": str(unit_id),
        "unit_hash": str(unit_hash),
    }
    mismatches = [
        f"{key}={payload.get(key)!r} (expected {value!r})"
        for key, value in expected.items()
        if payload.get(key) != value
    ]
    if mismatches:
        raise ValueError("builder worklist identity mismatch: " + "; ".join(mismatches))
    for field in ("items", "done", "notes"):
        if not isinstance(payload.get(field), list):
            raise ValueError(f"builder worklist {field} must be an array")
    return path, payload


def write_unit_worklist(path: Path, payload: dict[str, Any]) -> None:
    """Publish durable worklist JSON atomically."""
    atomic_write(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
