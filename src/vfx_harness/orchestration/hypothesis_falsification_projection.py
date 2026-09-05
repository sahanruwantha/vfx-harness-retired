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


def open_conflict_contract_ids(
    folder: str | Path,
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Contract ids each open joint-unsatisfiability finding names, across the shot.

    The amendment a finding drives is bounded by what that finding put in question
    (HIR-0232). Only ``contract`` conflicts carry rows; a unit-scoped or capability
    conflict names none and yields nothing, so the caller's check is inert for them.

    **Deliberately not filtered by layer.** A finding's ``layer`` is where it was
    *raised*, not the layer whose authority it indicts: room's record carries
    ``layer: "2"`` and ``unit: "ground_island"`` while naming two rows owned by layer 1
    and a fault owner of ``camera_move``. Filtering on that field would mean the check
    never fired on the layer actually being amended. The caller scopes naturally instead,
    by comparing only rows present in both the base view and the candidate.

    Unreadable or malformed records are skipped rather than raised on: this reader
    informs a validation note and must not turn a review projection into a hard failure
    of an unrelated materialization.
    """

    directory = Path(folder) / PROJECTION_DIR
    if not directory.is_dir():
        return ()
    found: list[tuple[str, tuple[str, ...]]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, Mapping):
            continue
        conflict = payload.get("conflict")
        if not isinstance(conflict, Mapping) or str(conflict.get("kind") or "") != "contract":
            continue
        raw = payload.get("contract_ids")
        ids = tuple(
            str(value) for value in raw if isinstance(value, str) and value.strip()
        ) if isinstance(raw, (list, tuple)) else ()
        if ids:
            found.append((str(payload.get("record_id") or path.stem), ids))
    return tuple(found)
