"""Closed envelope parsing for one unpublished JIT materialization candidate."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from vfx_harness.orchestration.jit_materialization.schema import (
    MATERIALIZATION_FIELDS,
    MATERIALIZATION_SCHEMA,
    _document,
    materialization_base_selection,
)


def load_materialization_candidate(
    path: str | Path,
    *,
    expected_bundle_hash: str,
) -> dict[str, Any]:
    """Parse the exact v3 envelope and require its selected global bundle."""

    source = Path(path)
    payload = _document(source)
    if payload.get("schema") != MATERIALIZATION_SCHEMA:
        raise ValueError(f"{source} has unsupported JIT materialization schema")
    if set(payload) != MATERIALIZATION_FIELDS:
        raise ValueError(f"{source} fields do not match the v3 materialization schema")
    materialization_base_selection(payload)
    if payload.get("bundle_hash") != expected_bundle_hash:
        raise ValueError("JIT materialization is pinned to another global bundle")
    return payload
