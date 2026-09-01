"""Atomic replan and supersession transaction for durable work-unit state."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from vfx_harness.domain.work_units import WorkUnit, validate_unit_dag
from vfx_harness.orchestration import (
    layer_finalization_lifecycle,
    unit_state_replan,
)
from vfx_harness.orchestration.unit_state_identity import (
    DIGEST_SCHEMA,
    downstream,
    replan_effects,
    unit_digest,
)
from vfx_harness.orchestration.unit_state_lock import (
    serialized_state_mutation,
    unit_state_path,
)
from vfx_harness.orchestration.unit_state_queries import (
    load,
    validate_current,
)
from vfx_harness.orchestration.unit_state_storage import now as _now


def _path(folder: str | Path, layer_id: str) -> Path:
    return unit_state_path(folder, layer_id)


@serialized_state_mutation(_path)
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
    falsification_id: str | None = None,
    falsification_payload: Mapping[str, Any] | None = None,
    hard_constraint_approval: str | None = None,
    discard_accepted: bool = False,
    reopen: frozenset[str] | set[str] | tuple[str, ...] | None = None,
    state_backed_base: bool = False,
) -> dict:
    """Atomically publish state effects and an audit record for a validated DAG amendment."""

    if not owner.strip() or not trigger.strip() or not evidence:
        raise ValueError("replan requires owner, trigger, and non-empty evidence")
    validate_unit_dag(old_units, "old work-unit DAG")
    validate_unit_dag(new_units, "new work-unit DAG")
    value = load(folder, layer_id)
    if not value:
        raise ValueError("work-unit state is not initialized")
    state_unit_ids = set(value.get("units") or {})
    # Under unit-first authority a generation's bundle carries no unit DAG: the layer's
    # units exist only in its materialized view and this durable state. Superseding such
    # a generation therefore arrives with an EMPTY base DAG while state holds the real
    # units; the state itself is the only truthful old identity (layer + its recorded
    # plan hash), and every state unit absent from the new DAG must be retired WITH an
    # audit trail — the bundle-level diff alone would have dropped them silently.
    deferred_base = not old_units and bool(state_unit_ids)
    if deferred_base:
        if str(value.get("layer")) != str(layer_id):
            raise ValueError(f"work-unit state belongs to layer {value.get('layer')}, not {layer_id}")
        if value.get("plan_hash") != old_plan_hash:
            raise ValueError("replan base layer/plan hash does not match active state")
    else:
        validate_current(value, layer_id, old_units)
        if str(value.get("layer")) != str(layer_id) or value.get("plan_hash") != old_plan_hash:
            raise ValueError("replan base layer/plan hash does not match active state")

    old = {unit.id: unit for unit in old_units}
    new_ids = {unit.id for unit in new_units}
    if deferred_base and state_backed_base:
        if int(value.get("digest_schema", 1)) != DIGEST_SCHEMA:
            raise ValueError("digest-bound replan base uses an incompatible work-unit digest schema")
        stored_hashes: dict[str, str] = {}
        for uid, row in value["units"].items():
            digest = (row or {}).get("unit_hash")
            if not isinstance(digest, str) or not digest:
                raise ValueError(f"digest-bound replan base is missing unit_hash for {uid}")
            stored_hashes[str(uid)] = digest
        new_by_id = {unit.id: unit for unit in new_units}
        added_ids = new_ids - state_unit_ids
        removed_ids = state_unit_ids - new_ids
        changed_ids = {uid for uid in state_unit_ids & new_ids if stored_hashes[uid] != unit_digest(new_by_id[uid])}
        invalidated_ids = downstream(added_ids | changed_ids, new_units)
        effects = {
            "added": sorted(added_ids),
            "removed": sorted(removed_ids),
            "changed": sorted(changed_ids),
            "invalidated": sorted(invalidated_ids),
            "preserved": sorted(state_unit_ids & new_ids - invalidated_ids),
        }
    else:
        effects = replan_effects(old_units, new_units)
    added = set(effects["added"])
    removed = set(effects["removed"])
    changed = set(effects["changed"])
    reopen_ids = {str(uid) for uid in (reopen or ()) if str(uid)}
    invalidated = set(effects["invalidated"]) | (reopen_ids & new_ids)
    preserved = set(effects["preserved"]) - invalidated
    orphaned = (
        set()
        if state_backed_base
        else state_unit_ids - set(old) - {unit.id for unit in new_units}
        if deferred_base
        else set()
    )
    now = _now()

    layer_finalization_lifecycle.archive_layer_finalization(
        value,
        disposition="superseded",
        reason=trigger,
        evidence=list(evidence),
        at=now,
    )

    old_identity_ids = state_unit_ids if deferred_base and state_backed_base else set(old)
    retiring = sorted(removed | (invalidated & old_identity_ids) | orphaned)
    # A published DAG amendment is itself the recorded authority for the units it
    # removes or invalidates — but ORPHANS are invisible to the amendment diff (they
    # exist only in materialization-era state), so retiring an accepted orphan needs
    # its own explicit decision, exactly like --discard-accepted at rematerialization.
    accepted_orphans = sorted(
        uid for uid in orphaned if (value.get("units", {}).get(uid) or {}).get("status") == "passed"
    )
    if accepted_orphans and not discard_accepted and falsification_id is None:
        raise ValueError(
            f"replan would retire accepted unit(s) {', '.join(accepted_orphans)}; "
            "discarding proven work requires --discard-accepted or a typed "
            "falsification record"
        )

    if falsification_id is not None:
        unit_state_replan.require_unconsumed_falsification(
            value,
            falsification_id,
            falsification_payload,
        )
    elif falsification_payload is not None:
        raise ValueError("replan falsification payload requires its exact record id")

    unit_state_replan.revoke_active_authority(
        value["units"],
        plan_changed=old_plan_hash != new_plan_hash,
        evidence=list(evidence),
        at=now,
    )

    next_slots: dict[str, dict] = {}
    superseded = list(value.get("superseded") or [])
    for uid in retiring:
        superseded.append(
            unit_state_replan.retire_slot(
                value["units"].get(uid) or {},
                uid,
                new_plan_hash=new_plan_hash,
                evidence=list(evidence),
                at=now,
            )
        )
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
                        "from": "superseded" if unit.id in old_identity_ids else None,
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
    if orphaned:
        record["orphaned"] = sorted(orphaned)
    if discard_accepted:
        record["discard_accepted"] = True
    if falsification_id is not None:
        record["falsification_id"] = str(falsification_id)
    if hard_constraint_approval is not None:
        record["hard_constraint_approval"] = str(hard_constraint_approval)
    value.update(
        plan_hash=new_plan_hash,
        revision=int(value.get("revision") or 1) + 1,
        units=next_slots,
        superseded=superseded,
        updated=now,
    )
    value.setdefault("replans", []).append(record)
    # Plan effects and their audit record share one rename boundary: readers see the old
    # plan state or the complete amended state, never one without the other. Resolve the
    # facade at commit time so established tests and runtime instrumentation replacing
    # ``unit_state._write`` continue to observe this mutation.
    from vfx_harness.orchestration import unit_state as unit_state_facade  # noqa: PLC0415

    unit_state_facade._write(unit_state_path(folder, layer_id), value)
    return record


__all__ = ["apply_replan"]
