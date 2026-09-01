"""Read guards for exact active work-unit attempt claims."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from vfx_harness.domain.authority_head_records import (
    AuthoritySelectionTokenProjection,
    parse_authority_selection_token,
)
from vfx_harness.domain.stop_envelope_primitives import require_digest
from vfx_harness.domain.unit_attempts import (
    UnitAttemptClaim,
    require_attempt_matches_slot,
    validate_state_attempt_contracts,
)
from vfx_harness.domain.work_units import WorkUnit
from vfx_harness.orchestration import unit_state
from vfx_harness.orchestration.authority_selection_heads import (
    read_authority_selection_heads,
)
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionToken,
    authority_selection_lock,
    require_matching_authority_selection_token,
)
from vfx_harness.orchestration.unit_state_lock import unit_state_lock


class UnitAttemptConflict(ValueError):
    """Current durable state cannot grant or mutate the requested attempt."""


def _selection_projection(
    selection_token: AuthoritySelectionToken,
) -> AuthoritySelectionTokenProjection:
    if not isinstance(selection_token, AuthoritySelectionToken):
        raise UnitAttemptConflict(
            "work-unit claim requires an exact typed authority selection token"
        )
    return parse_authority_selection_token(
        selection_token.to_dict(),
        "work-unit claim authority selection token",
    )


def _slot(value: Mapping[str, Any], unit_id: str) -> dict[str, Any]:
    try:
        slot = value["units"][unit_id]
    except KeyError as exc:
        raise UnitAttemptConflict(f"unknown work unit {unit_id!r}") from exc
    if not isinstance(slot, dict):
        raise UnitAttemptConflict(
            f"work-unit state slot {unit_id!r} must be an object"
        )
    return slot


def require_active_unit_attempt_in_state(
    value: Mapping[str, Any],
    layer_id: str,
    unit_id: str,
    units: tuple[WorkUnit, ...],
    claim: UnitAttemptClaim,
    *,
    expected_plan_hash: str,
    selection_token: AuthoritySelectionToken,
) -> UnitAttemptClaim:
    """Validate one exact claim in state while the caller owns both shared locks."""

    validate_state_attempt_contracts(value)
    unit_state.validate_current(dict(value), layer_id, units)
    if int(value.get("digest_schema", 0)) != unit_state.DIGEST_SCHEMA:
        raise UnitAttemptConflict(
            "active work-unit attempt requires current digest schema"
        )
    expected_plan_hash = require_digest(
        expected_plan_hash,
        "active work-unit attempt expected_plan_hash",
    )
    if value.get("plan_hash") != expected_plan_hash:
        raise UnitAttemptConflict("active work-unit attempt plan identity changed")
    unit = next((candidate for candidate in units if candidate.id == unit_id), None)
    if unit is None:
        raise UnitAttemptConflict(f"unknown work unit {unit_id!r}")
    slot = _slot(value, unit_id)
    try:
        current = require_attempt_matches_slot(
            slot,
            claim,
            layer_id=str(layer_id),
            unit_id=unit.id,
            unit_digest=unit_state.unit_digest(unit),
            plan_hash=expected_plan_hash,
        )
    except ValueError as exc:
        raise UnitAttemptConflict(str(exc)) from exc
    if current is None:
        raise UnitAttemptConflict(f"work unit {unit_id} has no active attempt")
    if current.selection_token != _selection_projection(selection_token):
        raise UnitAttemptConflict(
            "active work-unit attempt belongs to another authority selection"
        )
    return current


@contextmanager
def active_unit_attempt_guard(
    folder: str | Path,
    layer_id: str,
    unit_id: str,
    units: tuple[WorkUnit, ...],
    claim: UnitAttemptClaim,
    *,
    expected_plan_hash: str,
    selection_token: AuthoritySelectionToken,
) -> Iterator[UnitAttemptClaim]:
    """Hold selection-SH then state-SH around exact claimed external work."""

    _selection_projection(selection_token)
    with authority_selection_lock(folder, exclusive=False):
        observed = read_authority_selection_heads(folder).token
        require_matching_authority_selection_token(selection_token, observed)
        with unit_state_lock(folder, layer_id, exclusive=False):
            value = unit_state.load(folder, layer_id)
            if not value:
                raise UnitAttemptConflict("work-unit state is not initialized")
            current = require_active_unit_attempt_in_state(
                value,
                layer_id,
                unit_id,
                units,
                claim,
                expected_plan_hash=expected_plan_hash,
                selection_token=selection_token,
            )
            yield current


def require_active_unit_attempt(
    folder: str | Path,
    layer_id: str,
    unit_id: str,
    units: tuple[WorkUnit, ...],
    claim: UnitAttemptClaim,
    *,
    expected_plan_hash: str,
    selection_token: AuthoritySelectionToken,
) -> UnitAttemptClaim:
    """Return the exact parsed live claim after a selection/state shared proof."""

    with active_unit_attempt_guard(
        folder,
        layer_id,
        unit_id,
        units,
        claim,
        expected_plan_hash=expected_plan_hash,
        selection_token=selection_token,
    ) as current:
        return current


__all__ = [
    "UnitAttemptConflict",
    "active_unit_attempt_guard",
    "require_active_unit_attempt",
    "require_active_unit_attempt_in_state",
]
