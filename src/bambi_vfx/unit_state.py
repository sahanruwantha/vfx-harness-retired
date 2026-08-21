"""Durable work-unit state, checkpoints, and transactional replanning.

This is intentionally independent of Blender and the model SDK.  It is the small vertical
slice that makes plan/state semantics testable before the expensive builder is taught to
execute every work unit directly.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from .provenance import atomic_write
from .work_units import UNIT_STATES, WorkUnit, validate_unit_dag

SCHEMA = 1
STATE_DIR = "logs/work_units"

_TRANSITIONS = {
    "pending": {"planning", "blocked", "superseded"},
    "planning": {"building", "blocked", "failed", "retryable", "superseded"},
    "building": {"frozen", "blocked", "failed", "retryable", "superseded"},
    "frozen": {"evaluating", "building", "blocked", "failed", "retryable", "superseded"},
    "evaluating": {"passed", "repairing", "blocked", "failed", "retryable", "superseded"},
    "repairing": {"building", "frozen", "blocked", "failed", "retryable", "superseded"},
    "retryable": {"planning", "building", "blocked", "failed", "superseded"},
    "blocked": {"pending", "planning", "superseded"},
    "failed": {"retryable", "superseded"},
    "passed": {"superseded"},
    "superseded": set(),
}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _path(folder: str | Path, layer_id: str) -> Path:
    safe_layer = str(layer_id).strip()
    if not safe_layer or "/" in safe_layer or "\\" in safe_layer or safe_layer in {".", ".."}:
        raise ValueError(f"invalid layer id for work-unit state: {layer_id!r}")
    return Path(folder) / STATE_DIR / f"layer_{safe_layer}.json"


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def load(folder: str | Path, layer_id: str) -> dict:
    path = _path(folder, layer_id)
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is invalid JSON: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise ValueError(f"{path} must be an object with schema={SCHEMA}")
    if not isinstance(value.get("units"), dict):
        raise ValueError(f"{path}.units must be an object")
    return value


def unit_digest(unit: WorkUnit) -> str:
    payload = json.dumps(asdict(unit), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def validate_current(value: dict, layer_id: str, units: tuple[WorkUnit, ...]) -> None:
    """Reject stale state before it grants planning or dependency authority."""
    if not value:
        return
    if str(value.get("layer")) != str(layer_id):
        raise ValueError(f"work-unit state belongs to layer {value.get('layer')}, not {layer_id}")
    expected = {unit.id: unit_digest(unit) for unit in units}
    actual = {uid: row.get("unit_hash") for uid, row in value["units"].items()}
    if set(actual) != set(expected):
        raise ValueError("work-unit state IDs do not match the active layer DAG; apply a transactional replan")
    changed = sorted(uid for uid in expected if actual.get(uid) != expected[uid])
    if changed:
        raise ValueError(
            "work-unit state hashes do not match the active layer DAG for "
            + ", ".join(changed)
            + "; apply a transactional replan"
        )


def initialize(folder: str | Path, layer_id: str, units: tuple[WorkUnit, ...], *, plan_hash: str) -> dict:
    validate_unit_dag(units, f"layer {layer_id} work units")
    current = load(folder, layer_id)
    if current:
        validate_current(current, layer_id, units)
        if current.get("plan_hash") == plan_hash:
            return current
        raise ValueError("work-unit plan changed; apply a transactional replan instead of reinitializing")
    now = _now()
    value = {
        "schema": SCHEMA,
        "layer": str(layer_id),
        "plan_hash": plan_hash,
        "revision": 1,
        "units": {
            unit.id: {
                "status": "pending",
                "unit_hash": unit_digest(unit),
                "updated": now,
                "history": [],
            }
            for unit in units
        },
        "superseded": [],
        "replans": [],
        "updated": now,
    }
    _write(_path(folder, layer_id), value)
    return value


def transition(
    folder: str | Path,
    layer_id: str,
    unit_id: str,
    status: str,
    *,
    reason: str,
    metadata: dict | None = None,
) -> dict:
    if status not in UNIT_STATES:
        raise ValueError(f"unknown work-unit state {status!r}")
    value = load(folder, layer_id)
    if not value:
        raise ValueError("work-unit state is not initialized")
    try:
        slot = value["units"][unit_id]
    except KeyError as exc:
        raise KeyError(f"unknown work unit {unit_id!r}") from exc
    before = slot.get("status")
    if before == status:
        return value
    if status not in _TRANSITIONS.get(before, set()):
        raise ValueError(f"illegal work-unit transition {unit_id}: {before} -> {status}")
    event = {"at": _now(), "from": before, "to": status, "reason": str(reason)}
    if metadata:
        event["metadata"] = metadata
    slot.setdefault("history", []).append(event)
    slot["status"] = status
    slot["updated"] = event["at"]
    value["updated"] = event["at"]
    _write(_path(folder, layer_id), value)
    return value


def freeze_checkpoint(
    folder: str | Path,
    layer_id: str,
    unit: WorkUnit,
    *,
    active_contract_ids: list[str] | tuple[str, ...] | set[str],
    candidate_hash: str,
    settings_hash: str,
    script_hash: str,
    input_hash: str,
) -> dict:
    """Resolve wildcards once and persist the immutable candidate boundary."""
    value = load(folder, layer_id)
    if not value:
        raise ValueError("work-unit state is not initialized")
    slot = value.get("units", {}).get(unit.id)
    if not slot:
        raise KeyError(f"unknown work unit {unit.id!r}")
    if slot.get("status") not in {"building", "repairing"}:
        raise ValueError(f"cannot freeze {unit.id} from state {slot.get('status')}")
    protected = unit.protects.resolve(active_contract_ids)
    checkpoint = {
        "at": _now(),
        "candidate_hash": str(candidate_hash),
        "settings_hash": str(settings_hash),
        "script_hash": str(script_hash),
        "input_hash": str(input_hash),
        "protected_contract_ids": list(protected),
        "unit_hash": unit_digest(unit),
    }
    before = slot["status"]
    slot.setdefault("history", []).append(
        {"at": checkpoint["at"], "from": before, "to": "frozen", "reason": "candidate frozen"}
    )
    slot["status"] = "frozen"
    slot["checkpoint"] = checkpoint
    slot["updated"] = checkpoint["at"]
    value["updated"] = checkpoint["at"]
    _write(_path(folder, layer_id), value)
    return value


def block_dependents(
    folder: str | Path,
    layer_id: str,
    failed_unit: str,
    units: tuple[WorkUnit, ...],
    *,
    reason: str,
) -> dict:
    """Mark the transitive dependency closure blocked without calling it a scene failure."""
    value = load(folder, layer_id)
    if not value:
        raise ValueError("work-unit state is not initialized")
    closure = _downstream({failed_unit}, units) - {failed_unit}
    now = _now()
    for uid in sorted(closure):
        slot = value["units"][uid]
        before = slot.get("status")
        if before in {"passed", "superseded"}:
            continue
        if "blocked" not in _TRANSITIONS.get(before, set()) and before != "blocked":
            raise ValueError(f"cannot block dependent {uid} from state {before}")
        if before != "blocked":
            slot.setdefault("history", []).append(
                {"at": now, "from": before, "to": "blocked", "reason": str(reason)}
            )
            slot["status"] = "blocked"
            slot["updated"] = now
    value["updated"] = now
    _write(_path(folder, layer_id), value)
    return value


def _downstream(seeds: set[str], units: tuple[WorkUnit, ...]) -> set[str]:
    reverse: dict[str, set[str]] = {unit.id: set() for unit in units}
    for unit in units:
        for dep in unit.depends_on:
            reverse.setdefault(dep, set()).add(unit.id)
    out = set(seeds)
    frontier = list(seeds)
    while frontier:
        current = frontier.pop()
        for child in reverse.get(current, set()):
            if child not in out:
                out.add(child)
                frontier.append(child)
    return out


def apply_replan(
    folder: str | Path,
    layer_id: str,
    old_units: tuple[WorkUnit, ...],
    new_units: tuple[WorkUnit, ...],
    *,
    old_plan_hash: str,
    new_plan_hash: str,
    owner: str,
    trigger: str,
    evidence: list[str],
) -> dict:
    """Atomically publish state effects and an audit record for a validated DAG amendment."""
    if not owner.strip() or not trigger.strip() or not evidence:
        raise ValueError("replan requires owner, trigger, and non-empty evidence")
    validate_unit_dag(old_units, "old work-unit DAG")
    validate_unit_dag(new_units, "new work-unit DAG")
    value = load(folder, layer_id)
    if not value:
        raise ValueError("work-unit state is not initialized")
    validate_current(value, layer_id, old_units)
    if str(value.get("layer")) != str(layer_id) or value.get("plan_hash") != old_plan_hash:
        raise ValueError("replan base layer/plan hash does not match active state")

    old = {unit.id: unit for unit in old_units}
    new = {unit.id: unit for unit in new_units}
    added = set(new) - set(old)
    removed = set(old) - set(new)
    changed = {uid for uid in set(old) & set(new) if unit_digest(old[uid]) != unit_digest(new[uid])}
    invalidated = _downstream(added | changed, new_units)
    preserved = set(old) & set(new) - invalidated
    now = _now()

    next_slots: dict[str, dict] = {}
    superseded = list(value.get("superseded") or [])
    for uid in sorted(removed | (invalidated & set(old))):
        prior = dict(value["units"].get(uid) or {})
        prior.update(
            {
                "id": uid,
                "status": "superseded",
                "superseded_at": now,
                "superseded_by_plan": new_plan_hash,
            }
        )
        superseded.append(prior)
    for unit in new_units:
        if unit.id in preserved:
            slot = dict(value["units"][unit.id])
        else:
            slot = {
                "status": "pending",
                "updated": now,
                "history": [
                    {
                        "at": now,
                        "from": "superseded" if unit.id in old else None,
                        "to": "pending",
                        "reason": "transactional plan amendment",
                    }
                ],
            }
        slot["unit_hash"] = unit_digest(unit)
        next_slots[unit.id] = slot

    record = {
        "schema": 1,
        "at": now,
        "layer": str(layer_id),
        "owner": owner,
        "trigger": trigger,
        "evidence": list(evidence),
        "old_plan_hash": old_plan_hash,
        "new_plan_hash": new_plan_hash,
        "added": sorted(added),
        "removed": sorted(removed),
        "changed": sorted(changed),
        "invalidated": sorted(invalidated),
        "preserved": sorted(preserved),
    }
    value.update(
        plan_hash=new_plan_hash,
        revision=int(value.get("revision") or 1) + 1,
        units=next_slots,
        superseded=superseded,
        updated=now,
    )
    value.setdefault("replans", []).append(record)
    # Plan effects and their audit record share one rename boundary: readers see the old
    # plan state or the complete amended state, never one without the other.
    _write(_path(folder, layer_id), value)
    return record
