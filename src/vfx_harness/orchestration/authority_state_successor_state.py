"""Successor work-unit state projection for one authority-state transition.

The effects compiler decides *what* each layer's transition means; these writers derive
the exact successor bytes: fresh pending state, retired slots, archived attempts and
finalizations, and the digest-generation migration (HIR-0171, HIR-0182).
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from vfx_harness.domain import unit_attempts, unit_completion_receipts
from vfx_harness.domain.authority_capsules import LayerAuthorityCapsule
from vfx_harness.domain.work_units import WorkUnit
from vfx_harness.orchestration import layer_finalization_lifecycle
from vfx_harness.orchestration.unit_state_identity import (
    DIGEST_GENERATION_RULE,
    DIGEST_SCHEMA,
    PRIOR_DIGEST_SCHEMAS,
    unit_digest,
)


class AuthorityStateEffectsError(ValueError):
    """Current state cannot be moved mechanically to the proposed capsules."""



def _state_revision(value: Mapping[str, Any], layer_id: str) -> int:
    revision = value.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision <= 0:
        raise AuthorityStateEffectsError(
            f"work-unit state for layer {layer_id!r} has no positive revision"
        )
    return revision


def _new_state(
    layer_id: str,
    layer_digest: str,
    units: tuple[WorkUnit, ...],
    at: str,
) -> dict[str, Any]:
    return {
        "schema": 1,
        "digest_schema": DIGEST_SCHEMA,
        "layer": layer_id,
        "plan_hash": layer_digest,
        "revision": 1,
        "units": {
            unit.id: {
                "status": "pending",
                "unit_hash": unit_digest(unit),
                "updated": at,
                "history": [],
            }
            for unit in units
        },
        "superseded": [],
        "replans": [],
        "attempt_lineage": {},
        "updated": at,
    }


def _retire_slot(
    slot: Mapping[str, Any],
    unit_id: str,
    *,
    layer_digest: str,
    at: str,
    reason: str | None = None,
) -> dict[str, Any]:
    retired = copy.deepcopy(dict(slot))
    reason = reason or f"authority-state transition invalidated {unit_id}"
    unit_attempts.archive_active_attempt(
        retired,
        disposition="revoked",
        reason=reason,
        evidence=["authority-state-transition"],
        at=at,
    )
    unit_completion_receipts.archive_completion_receipt(
        retired,
        disposition="superseded",
        reason=reason,
        evidence=["authority-state-transition"],
        at=at,
    )
    retired.update(
        {
            "id": unit_id,
            "status": "superseded",
            "superseded_at": at,
            "superseded_by_plan": layer_digest,
            "superseded_reason": reason,
        }
    )
    return retired


def _prior_generation_layers(states: Mapping[str, Mapping[str, Any]]) -> frozenset[str]:
    """Layers whose durable state binds a prior digest generation; mixing is refused."""

    generations = {
        layer_id: int(state.get("digest_schema", 0)) for layer_id, state in states.items()
    }
    prior = {layer_id for layer_id, value in generations.items() if value != DIGEST_SCHEMA}
    unknown = sorted(
        layer_id for layer_id in prior if generations[layer_id] not in PRIOR_DIGEST_SCHEMAS
    )
    if unknown:
        raise AuthorityStateEffectsError(
            "work-unit state for layer(s) "
            + ", ".join(unknown)
            + " binds an unknown digest generation; only "
            f"{sorted(PRIOR_DIGEST_SCHEMAS)} migrate to {DIGEST_SCHEMA}"
        )
    if prior and len(prior) != len(generations):
        raise AuthorityStateEffectsError(
            "digest generations are mixed across layers: current "
            f"{sorted(set(generations) - prior)}, prior {sorted(prior)}; "
            + DIGEST_GENERATION_RULE
        )
    return frozenset(prior)


def _migration_reason(state: Mapping[str, Any]) -> str:
    return (
        f"digest generation {state.get('digest_schema')} migrated to {DIGEST_SCHEMA} "
        "during atomic authority-state transition"
    )


def _after_state(
    *,
    layer_id: str,
    state: Mapping[str, Any] | None,
    after_layer: LayerAuthorityCapsule,
    after_units: tuple[WorkUnit, ...],
    preserved_ids: set[str],
    preserve_finalization: bool,
    at: str,
    reason: str = "authority selection changed during atomic authority-state transition",
) -> dict[str, Any]:
    if state is None:
        return _new_state(layer_id, after_layer.capsule_digest, after_units, at)
    value = copy.deepcopy(dict(state))
    before_serialized = copy.deepcopy(value)
    for slot in value["units"].values():
        unit_attempts.revoke_active_attempt(
            slot,
            reason=reason,
            evidence=["authority-state-transition"],
            at=at,
            next_status="retryable",
        )
    finalization_slot = value.get("layer_finalization")
    if (
        isinstance(finalization_slot, Mapping)
        and finalization_slot.get("active_claim") is not None
    ) or not preserve_finalization:
        layer_finalization_lifecycle.archive_layer_finalization(
            value,
            disposition="superseded",
            reason=reason,
            evidence=["authority-state-transition"],
            at=at,
        )
    old_slots = value["units"]
    retiring = set(old_slots) - preserved_ids
    superseded = list(value.get("superseded") or [])
    superseded.extend(
        _retire_slot(
            old_slots[unit_id],
            unit_id,
            layer_digest=after_layer.capsule_digest,
            at=at,
            reason=reason,
        )
        for unit_id in sorted(retiring)
    )
    next_slots: dict[str, dict[str, Any]] = {}
    for unit in after_units:
        if unit.id in preserved_ids:
            slot = copy.deepcopy(old_slots[unit.id])
            slot["unit_hash"] = unit_digest(unit)
        else:
            slot = {
                "status": "pending",
                "unit_hash": unit_digest(unit),
                "updated": at,
                "history": [
                    {
                        "at": at,
                        "from": "superseded" if unit.id in old_slots else None,
                        "to": "pending",
                        "reason": "atomic authority-state transition",
                    }
                ],
            }
        next_slots[unit.id] = slot
    value["digest_schema"] = DIGEST_SCHEMA
    value["plan_hash"] = after_layer.capsule_digest
    value["units"] = next_slots
    value["superseded"] = superseded
    if value != before_serialized:
        value["revision"] = _state_revision(state, layer_id) + 1
        value["updated"] = at
    return value
