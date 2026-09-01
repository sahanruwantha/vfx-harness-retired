"""State-native loading and reconciliation for typed hypothesis falsifications."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from vfx_harness.domain.unit_outcomes import HypothesisFalsification
from vfx_harness.orchestration import hypothesis_falsification_projection, unit_state
from vfx_harness.orchestration.unit_state_lock import unit_state_lock


def load_state_backed_falsification(
    folder: str | Path,
    layer_id: str,
    record_id: str,
) -> dict[str, Any]:
    """Load one exact finding from its authoritative layer state, never its projection."""

    record_id = str(record_id).strip()
    if not record_id:
        raise ValueError("hypothesis falsification record id must be non-empty")
    with unit_state_lock(folder, layer_id, exclusive=False):
        value = unit_state.load(folder, layer_id)
        if not value:
            raise ValueError(f"work-unit state for layer {layer_id} is not initialized")
        rows = [
            row
            for row in value.get("falsifications", [])
            if isinstance(row, Mapping) and row.get("record_id") == record_id
        ]
        if len(rows) != 1:
            raise ValueError(
                f"hypothesis falsification {record_id!r} must have exactly one "
                "authoritative state row"
            )
        payload = dict(rows[0])
        finding = HypothesisFalsification.parse(payload)
        if finding.layer != str(layer_id):
            raise ValueError(
                f"hypothesis falsification {record_id!r} belongs to layer "
                f"{finding.layer}, not {layer_id}"
            )
        slot = value.get("units", {}).get(finding.unit)
        if not isinstance(slot, Mapping) or slot.get("falsification") != payload:
            raise ValueError(
                f"hypothesis falsification {record_id!r} is not bound identically "
                "to its durable unit slot"
            )
        return payload


def reconcile_falsification_projection(
    folder: str | Path,
    layer_id: str,
    record_id: str,
) -> Path:
    """Recreate one missing/corrupt derived JSON projection from durable state."""

    # Keep the source row stable through publication. The nested loader reacquires this
    # same shared lock through the safe same-thread reentrancy contract.
    with unit_state_lock(folder, layer_id, exclusive=False):
        payload = load_state_backed_falsification(folder, layer_id, record_id)
        published = hypothesis_falsification_projection.publish_projection(
            folder, payload
        )
        if published != hypothesis_falsification_projection.projection_path(
            folder, record_id
        ):  # pragma: no cover
            raise RuntimeError("falsification projection resolved to an unexpected path")
        return published
