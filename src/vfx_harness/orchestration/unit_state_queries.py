"""Read-only parsing, identity validation, and scheduling queries for unit state."""

from __future__ import annotations

import json
from pathlib import Path

from vfx_harness.domain import (
    layer_finalizations,
    unit_attempts,
    unit_completion_receipts,
)
from vfx_harness.domain.work_units import WorkUnit, ready_units
from vfx_harness.orchestration.authority_selection_transaction import (
    authority_selection_lock,
)
from vfx_harness.orchestration.unit_completion_authority_guard import (
    require_current_unit_completion_authorization,
)
from vfx_harness.orchestration.unit_completion_authorizations import (
    AuthorizedUnitCompletionSet,
)
from vfx_harness.orchestration.unit_state_identity import (
    DIGEST_GENERATION_RULE,
    DIGEST_SCHEMA,
    authorized_passed_unit_ids,
    unit_digest,
)
from vfx_harness.orchestration.unit_state_lifecycle import (
    PLANNING_CLAIMABLE_STATES,
    TRANSITIONS,
)
from vfx_harness.orchestration.unit_state_lock import unit_state_lock, unit_state_path
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
    layer_finalizations.validate_state_layer_finalization_contracts(value)
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
            "work-unit state IDs do not match the active layer DAG; publish a "
            "validated authority replacement or amendment"
        )
    if int(value.get("digest_schema", 1)) != DIGEST_SCHEMA:
        raise ValueError(
            f"work-unit state for layer {layer_id} is digest generation "
            f"{value.get('digest_schema')}; the current generation is {DIGEST_SCHEMA}. "
            + DIGEST_GENERATION_RULE
        )
    changed = sorted(uid for uid in expected if actual.get(uid) != expected[uid])
    if changed:
        raise ValueError(
            "work-unit state hashes do not match the active layer DAG for "
            + ", ".join(changed)
            + "; publish a validated authority replacement or amendment"
        )


def ready_from_durable_state(
    folder: str | Path,
    layer_id: str,
    units: tuple[WorkUnit, ...],
    *,
    eligible_passed: set[str] | None = None,
    completion_authorization: AuthorizedUnitCompletionSet | None,
) -> tuple[WorkUnit, ...]:
    """Resolve the ready set from one fresh, digest-validated state snapshot.

    ``eligible_passed`` may narrow passed rows whose replay artifacts are locally
    available to a caller; it can never broaden durable acceptance.
    """

    if completion_authorization is None:
        state = load(folder, layer_id)
    else:
        with authority_selection_lock(folder, exclusive=False):
            require_current_unit_completion_authorization(
                folder,
                completion_authorization,
            )
            with unit_state_lock(folder, layer_id, exclusive=False):
                state = load(folder, layer_id)
    validate_current(state, layer_id, units)
    passed = {
        str(uid)
        for uid, row in ((state or {}).get("units") or {}).items()
        if isinstance(row, dict) and row.get("status") == "passed"
    }
    if eligible_passed is not None:
        passed &= {str(uid) for uid in eligible_passed}
    sealed = authorized_passed_unit_ids(
        state,
        units,
        completion_authorization=completion_authorization,
    ) & passed
    return ready_units(units, passed, sealed_producers=sealed)


def unresolved_falsification(
    folder: str | Path,
    layer_id: str,
    unit_id: str,
) -> dict | None:
    """The durable finding on a unit no builder may claim, or ``None``.

    ``hypothesis_falsified`` has exactly one legal successor, ``superseded``, so a unit
    in it is not waiting for a retry — it is waiting for a reviewed authority
    transaction. The ready set is computed from passed rows and does not exclude it, so
    the driver used to select it and the claim raised ``UnitAttemptConflict`` with a
    traceback, from a boundary holding the finding all along (HIR-0214).
    """

    state = load(folder, layer_id)
    slot = ((state or {}).get("units") or {}).get(str(unit_id))
    if not isinstance(slot, dict) or slot.get("status") != "hypothesis_falsified":
        return None
    finding = slot.get("falsification")
    return dict(finding) if isinstance(finding, dict) else None


def unclaimable_state(
    folder: str | Path,
    layer_id: str,
    unit_id: str,
) -> tuple[str, str] | None:
    """The durable status no builder may claim, with the transaction that clears it.

    Returns ``(status, next_action)`` or ``None`` when the unit is claimable.

    An operator interrupt — including one taken to honour a budget ceiling — leaves the
    active unit in ``building``.  The claim below correctly refuses it, but refusing with
    a traceback from a boundary that holds the status, the legal states, and the closed
    lifecycle wastes every one of them: the run terminalized as an unclassified
    ``harness_defect`` routed to engineering, while ``vfx units retry`` cleared it in
    seconds and nothing said so (HIR-0214 fixed the sibling case for
    ``hypothesis_falsified`` and this path never received it).

    The next action is DERIVED from ``TRANSITIONS`` rather than listed here, so a
    lifecycle edge cannot be added without this sentence following it.
    """

    state = load(folder, layer_id)
    slot = ((state or {}).get("units") or {}).get(str(unit_id))
    if not isinstance(slot, dict):
        return None
    status = str(slot.get("status") or "")
    if not status or status in PLANNING_CLAIMABLE_STATES:
        return None
    if status == "hypothesis_falsified":
        # Its own typed stop already names the reviewed transaction (HIR-0214).
        return None
    if "retryable" in TRANSITIONS.get(status, frozenset()):
        return status, (
            f"`{status}` reaches `retryable`, so a reviewed retry reopens it: "
            f"vfx units retry <shot> --layer {layer_id} --unit {unit_id} "
            "--reason <why it is retryable now> --evidence <locator>. "
            "Then rerun the build."
        )
    reachable = sorted(TRANSITIONS.get(status, frozenset()))
    return status, (
        f"`{status}` does not reach `retryable`, so no retry exists for it; its only "
        f"legal successors are {reachable or ['(none)']}. Reopening it requires a "
        "reviewed authority transaction, not a builder retry."
    )
