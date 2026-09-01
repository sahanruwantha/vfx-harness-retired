"""Read-only parsing, identity validation, and scheduling queries for unit state."""

from __future__ import annotations

import json
from pathlib import Path

from vfx_harness.domain import unit_attempts, unit_completion_receipts
from vfx_harness.domain.work_units import WorkUnit, ready_units
from vfx_harness.orchestration.unit_state_identity import (
    DIGEST_SCHEMA,
    digest_matched_passed,
    unit_digest,
)
from vfx_harness.orchestration.unit_state_lock import unit_state_path
from vfx_harness.orchestration.unit_state_storage import read

SCHEMA = 1


def _decode_snapshot(path: Path, payload: bytes | None) -> dict:
    """Parse one descriptor-locked state snapshot without reopening its path."""

    if payload is None:
        return {}
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path} is invalid JSON: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise ValueError(f"{path} must be an object with schema={SCHEMA}")
    if not isinstance(value.get("units"), dict):
        raise ValueError(f"{path}.units must be an object")
    unit_attempts.validate_state_attempt_contracts(value)
    unit_completion_receipts.validate_state_completion_contracts(value)
    return value


def load_snapshot(
    folder: str | Path,
    layer_id: str,
) -> tuple[dict, bytes | None]:
    """Return parsed state and the exact bytes read under the same state lock."""

    path = unit_state_path(folder, layer_id)
    payload = read(path)
    return _decode_snapshot(path, payload), payload


def load(folder: str | Path, layer_id: str) -> dict:
    value, _payload = load_snapshot(folder, layer_id)
    return value


def validate_current(value: dict, layer_id: str, units: tuple[WorkUnit, ...]) -> None:
    """Reject stale state before it grants planning or dependency authority."""

    if not value:
        return
    if str(value.get("layer")) != str(layer_id):
        raise ValueError(
            f"work-unit state belongs to layer {value.get('layer')}, not {layer_id}"
        )
    expected = {unit.id: unit_digest(unit) for unit in units}
    actual = {uid: row.get("unit_hash") for uid, row in value["units"].items()}
    if set(actual) != set(expected):
        raise ValueError(
            "work-unit state IDs do not match the active layer DAG; apply a "
            "transactional replan"
        )
    if int(value.get("digest_schema", 1)) != DIGEST_SCHEMA:
        # Digests from another schema are not comparable. Replan closure recomputes
        # both sides under the current schema before deciding what can be preserved.
        return
    changed = sorted(uid for uid in expected if actual.get(uid) != expected[uid])
    if changed:
        raise ValueError(
            "work-unit state hashes do not match the active layer DAG for "
            + ", ".join(changed)
            + "; apply a transactional replan"
        )


def ready_from_durable_state(
    folder: str | Path,
    layer_id: str,
    units: tuple[WorkUnit, ...],
    *,
    eligible_passed: set[str] | None = None,
) -> tuple[WorkUnit, ...]:
    """Resolve the ready set from one fresh, digest-validated state snapshot.

    ``eligible_passed`` may narrow passed rows whose replay artifacts are locally
    available to a caller; it can never broaden durable acceptance.
    """

    state = load(folder, layer_id)
    validate_current(state, layer_id, units)
    passed = {
        str(uid)
        for uid, row in ((state or {}).get("units") or {}).items()
        if isinstance(row, dict) and row.get("status") == "passed"
    }
    if eligible_passed is not None:
        passed &= {str(uid) for uid in eligible_passed}
    sealed = digest_matched_passed(state, units) & passed
    return ready_units(units, passed, sealed_producers=sealed)
