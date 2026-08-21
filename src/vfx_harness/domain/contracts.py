"""Strict lifecycle and document schema shared by executable contracts.

Contracts are execution authority, so ambiguity is a schema error.  In particular there
is no compatibility path for the former ``layer`` field: every record names who created
the state, who repairs it, when it becomes testable, and how long it remains active.
"""

from __future__ import annotations

import json
from pathlib import Path

SCHEMA = 2
LIFECYCLES = {"layer", "window", "persistent"}


def load_document(path: str | Path, key: str) -> list[dict]:
    """Load ``{"schema": 2, <key>: [...]}`` or fail closed."""
    path = Path(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
        raise ValueError(f"{path.name} must be an object with schema={SCHEMA}")
    rows = raw.get(key)
    if not isinstance(rows, list):
        raise ValueError(f"{path.name}.{key} must be a list")
    return rows


def dump_document(path: str | Path, key: str, rows: list[dict]) -> None:
    Path(path).write_text(json.dumps({"schema": SCHEMA, key: rows}, indent=1) + "\n", encoding="utf-8")


def _layer(value, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a positive integer") from exc
    if result < 1 or str(value).strip() not in {str(result), f"{result}.0"}:
        raise ValueError(f"{field} must be a positive integer")
    return result


def validate_lifecycle(row: dict) -> str | None:
    if "layer" in row:
        return "legacy 'layer' is removed; use owner_layer/fault_owner/activates_at/lifecycle"
    try:
        owner = _layer(row.get("owner_layer"), "owner_layer")
        fault = _layer(row.get("fault_owner"), "fault_owner")
        active = _layer(row.get("activates_at"), "activates_at")
    except ValueError as exc:
        return str(exc)
    lifecycle = str(row.get("lifecycle") or "")
    if lifecycle not in LIFECYCLES:
        return f"lifecycle must be one of {sorted(LIFECYCLES)}"
    if active < owner:
        return "activates_at cannot precede owner_layer"
    valid = row.get("valid_through")
    if lifecycle == "layer":
        if valid is not None and str(valid) != str(active):
            return "layer lifecycle valid_through must equal activates_at or be omitted"
    elif lifecycle == "persistent":
        if valid is not None:
            return "persistent lifecycle must omit valid_through"
    else:
        try:
            end = _layer(valid, "valid_through")
        except ValueError as exc:
            return str(exc)
        if end < active:
            return "valid_through cannot precede activates_at"
    if fault > active and lifecycle == "layer":
        return "fault_owner cannot be later than a one-layer contract's activation"
    return None


def bounds(row: dict) -> tuple[int, int | None]:
    """Return the inclusive active layer interval for a validated row."""
    start = _layer(row["activates_at"], "activates_at")
    lifecycle = row["lifecycle"]
    if lifecycle == "persistent":
        return start, None
    if lifecycle == "layer":
        return start, start
    return start, _layer(row["valid_through"], "valid_through")


def active_for(row: dict, layer_id: str | int, frame: int | None = None) -> bool:
    if validate_lifecycle(row):
        return False
    current = _layer(layer_id, "current layer")
    start, end = bounds(row)
    if current < start or (end is not None and current > end):
        return False
    if frame is not None and row.get("frame") is not None:
        try:
            return int(row["frame"]) == int(frame)
        except (TypeError, ValueError):
            return False
    return True


def lifecycle_fields(
    owner_layer: str | int,
    *,
    lifecycle: str = "layer",
    activates_at: str | int | None = None,
    valid_through: str | int | None = None,
    fault_owner: str | int | None = None,
) -> dict:
    owner = int(owner_layer)
    active = int(activates_at if activates_at is not None else owner)
    out = {
        "owner_layer": str(owner),
        "fault_owner": str(fault_owner if fault_owner is not None else owner),
        "activates_at": str(active),
        "lifecycle": lifecycle,
    }
    if lifecycle == "window":
        out["valid_through"] = str(valid_through)
    return out
