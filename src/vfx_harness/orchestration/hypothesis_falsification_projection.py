"""Derived public projection of authoritative durable falsification state."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from vfx_harness.orchestration.authority_selection_transaction import (
    durable_replace_file_bytes,
    durably_ensure_real_directory,
)
from vfx_harness.orchestration.unit_state_lock import STATE_DIR

# The projection is derived review output, not live work-unit state: it lives beside the
# strict `state/work-units/` namespace, whose enumerator refuses every unrecognised
# member (HIR-0171). Placing it inside that namespace failed the terminal materialization
# gate of every rematerialization that followed a real falsification (HIR-0175).
PROJECTION_DIR = Path(STATE_DIR).parent / "hypothesis-falsifications"


class FalsificationProjectionPending(OSError):
    """Authoritative state committed but its review projection needs reconciliation."""

    def __init__(self, payload: Mapping[str, Any]):
        self.payload = dict(payload)
        self.record_id = str(payload.get("record_id") or "")
        super().__init__(
            "hypothesis falsification committed to durable state but its derived "
            f"projection is pending reconciliation: {self.record_id}"
        )


def projection_path(folder: str | Path, record_id: str) -> Path:
    safe_id = str(record_id).strip()
    if (
        not safe_id
        or safe_id in {".", ".."}
        or "/" in safe_id
        or "\\" in safe_id
    ):
        raise ValueError(f"invalid hypothesis falsification record id: {record_id!r}")
    return Path(folder) / PROJECTION_DIR / f"{safe_id}.json"


def publish_projection(
    folder: str | Path,
    payload: Mapping[str, Any],
) -> Path:
    """Durably replace the optional projection from one state-validated payload."""

    record_id = str(payload.get("record_id") or "")
    path = projection_path(folder, record_id)
    durably_ensure_real_directory(folder, PROJECTION_DIR)
    data = (json.dumps(dict(payload), indent=2, sort_keys=True) + "\n").encode()
    durable_replace_file_bytes(folder, path.relative_to(Path(folder)), data)
    return path
