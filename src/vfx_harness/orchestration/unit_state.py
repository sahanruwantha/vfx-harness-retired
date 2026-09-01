"""Durable work-unit state, checkpoints, and transactional replanning."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from vfx_harness.domain import unit_attempts, unit_completion_receipts
from vfx_harness.domain.unit_outcomes import HYPOTHESIS_FALSIFICATION_SCHEMA, HypothesisFalsification
from vfx_harness.domain.work_units import (
    UNIT_STATES,
    WorkUnit,
    geometry_vis_protection_ids,
    validate_unit_dag,
)
from vfx_harness.orchestration import hypothesis_falsification_projection, unit_state_replan
from vfx_harness.orchestration.authority_selection_transaction import AuthoritySelectionToken
from vfx_harness.orchestration.unit_state_identity import (
    DIGEST_SCHEMA,
    downstream,
    replan_effects,
    unit_digest,
)
from vfx_harness.orchestration.unit_state_identity import (
    digest_matched_passed as digest_matched_passed,
)
from vfx_harness.orchestration.unit_state_lifecycle import TRANSITIONS as _TRANSITIONS
from vfx_harness.orchestration.unit_state_lock import (
    serialized_state_mutation,
    unit_state_path,
)
from vfx_harness.orchestration.unit_state_queries import SCHEMA as SCHEMA
from vfx_harness.orchestration.unit_state_queries import load as load
from vfx_harness.orchestration.unit_state_queries import load_snapshot as load_snapshot
from vfx_harness.orchestration.unit_state_queries import (
    ready_from_durable_state as ready_from_durable_state,
)
from vfx_harness.orchestration.unit_state_queries import validate_current as validate_current
from vfx_harness.orchestration.unit_state_selection import selected_attempt_state_mutation
from vfx_harness.orchestration.unit_state_storage import now as _now
from vfx_harness.orchestration.unit_state_storage import write as _write


def _path(folder: str | Path, layer_id: str) -> Path:
    return unit_state_path(folder, layer_id)


@serialized_state_mutation(_path)
def initialize(folder: str | Path, layer_id: str, units: tuple[WorkUnit, ...], *, plan_hash: str) -> dict:
    validate_unit_dag(units, f"layer {layer_id} work units")
    current = load(folder, layer_id)
    if current and not current["units"]:
        # A replan transaction legally empties a layer's unit set, and under unit-first
        # authority that is the NORMAL pre-materialization condition: the global DAG
        # carries no units, so they exist only once the layer materializes. Seeding
        # pending units into empty state destroys nothing and preserves the full
        # supersession history — the forbidden act is reinitializing OVER existing
        # units, which still fails closed below.
        if str(current.get("layer")) != str(layer_id):
            raise ValueError(
                f"work-unit state belongs to layer {current.get('layer')}, not {layer_id}"
            )
        now = _now()
        value = {
            **current,
            "digest_schema": DIGEST_SCHEMA,
            "plan_hash": plan_hash,
            "revision": int(current.get("revision", 0)) + 1,
            "units": {
                unit.id: {
                    "status": "pending",
                    "unit_hash": unit_digest(unit),
                    "updated": now,
                    "history": [],
                }
                for unit in units
            },
            "attempt_lineage": dict(current.get("attempt_lineage") or {}),
            "updated": now,
        }
        _write(_path(folder, layer_id), value)
        return value
    if current:
        validate_current(current, layer_id, units)
        if current.get("plan_hash") == plan_hash:
            return current
        # `plan_hash` is sha256 of the selected layers.json, which names every
        # materialized layer. A sibling rematerialization changes that file while
        # this layer's unit DAG can be byte-identical. That is a plan-identity
        # adoption, not a DAG change: empty-base `vfx units replan` would treat
        # every unit as added and reset passed checkpoints (HIR-0040).
        if int(current.get("digest_schema", 1)) != DIGEST_SCHEMA:
            raise ValueError(
                "work-unit plan changed; apply a transactional replan instead of reinitializing"
            )
        apply_replan(
            folder,
            layer_id,
            units,
            units,
            old_plan_hash=str(current["plan_hash"]),
            new_plan_hash=plan_hash,
            owner="vfx-harness.initialize",
            trigger=(
                "selected layers.json identity changed while this layer's unit DAG is unchanged"
            ),
            evidence=[f"layers.json sha256 {plan_hash}"],
        )
        adopted = load(folder, layer_id)
        if not adopted:
            raise ValueError("work-unit state disappeared during plan-identity adoption")
        return adopted
    now = _now()
    value = {
        "schema": SCHEMA,
        "digest_schema": DIGEST_SCHEMA,
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
        "attempt_lineage": {},
        "updated": now,
    }
    _write(_path(folder, layer_id), value)
    return value


@serialized_state_mutation(_path)
def supersede_layer_units(
    folder: str | Path,
    layer_id: str,
    *,
    owner: str,
    trigger: str,
    evidence: list[str],
    plan_hash: str,
    allow_accepted: bool = False,
) -> dict:
    """Retire every unit of a layer whose authority was replaced, under an audited
    transaction. Refuses when any unit has been accepted.

    Re-materialization publishes a new view and then moves state; if the move cannot
    validate — the old DAG is gone, or its digests predate a WorkUnit schema change —
    the state is orphaned and no `apply_replan` base can be reconstructed. This retires
    it explicitly, with the operator's owner/trigger/evidence recorded, rather than
    leaving it to be hand-edited or silently reseeded.
    """
    if not owner.strip() or not trigger.strip() or not evidence:
        raise ValueError("superseding layer units requires owner, trigger, and evidence")
    value = load(folder, layer_id)
    if not value:
        return {}
    accepted = sorted(
        uid for uid, row in value["units"].items() if row.get("status") == "passed"
    )
    if accepted and not allow_accepted:
        raise ValueError(
            f"layer {layer_id} has accepted unit(s) {', '.join(accepted)}; "
            "move that state with a replan transaction instead"
        )
    now = _now()
    superseded = list(value.get("superseded") or [])
    for uid in sorted(value["units"]):
        prior = dict(value["units"][uid])
        unit_attempts.archive_active_attempt(
            prior,
            disposition="revoked",
            reason=trigger,
            evidence=list(evidence),
            at=now,
        )
        unit_completion_receipts.archive_completion_receipt(
            prior,
            disposition="superseded",
            reason=trigger,
            evidence=list(evidence),
            at=now,
        )
        prior.update(
            {
                "id": uid,
                "status": "superseded",
                "superseded_at": now,
                "superseded_by_plan": plan_hash,
                "owner": owner,
                "trigger": trigger,
                "evidence": list(evidence),
            }
        )
        superseded.append(prior)
    value = {
        **value,
        "digest_schema": DIGEST_SCHEMA,
        "units": {},
        "superseded": superseded,
        "plan_hash": plan_hash,
        "revision": int(value.get("revision", 0)) + 1,
        "updated": now,
    }
    _write(_path(folder, layer_id), value)
    return value


@selected_attempt_state_mutation
@serialized_state_mutation(_path)
def transition(
    folder: str | Path,
    layer_id: str,
    unit_id: str,
    status: str,
    *,
    reason: str,
    metadata: dict | None = None,
    attempt: unit_attempts.UnitAttemptClaim | Mapping[str, Any] | None = None,
    selection_token: AuthoritySelectionToken | None = None,
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
    current_attempt = unit_attempts.require_attempt_matches_slot(
        slot,
        attempt,
        layer_id=str(layer_id),
        unit_id=unit_id,
        unit_digest=slot.get("unit_hash"),
        plan_hash=value.get("plan_hash"),
    )
    if current_attempt is None:
        allowed = {("pending", "blocked"), ("blocked", "pending")}
        if before != status and (before, status) not in allowed:
            raise ValueError(
                f"generic unclaimed transition refuses {unit_id}: {before} -> {status}; "
                "only pending/blocked non-spend migration is legal"
            )
    elif status not in {"evaluating", "repairing"}:
        raise ValueError(
            f"generic claimed transition cannot enter {status!r}; use the typed "
            "checkpoint, completion, release, falsification, or revocation transaction"
        )
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


@selected_attempt_state_mutation
@serialized_state_mutation(_path)
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
    layer_active_vis_ids: Iterable[str] = (),
    attempt: unit_attempts.UnitAttemptClaim | Mapping[str, Any] | None = None,
    selection_token: AuthoritySelectionToken | None = None,
) -> dict:
    """Resolve wildcards once and persist the immutable candidate boundary."""
    if attempt is None:
        raise ValueError(
            "checkpoint freeze requires the exact active work-unit attempt"
        )
    value = load(folder, layer_id)
    if not value:
        raise ValueError("work-unit state is not initialized")
    slot = value.get("units", {}).get(unit.id)
    if not slot:
        raise KeyError(f"unknown work unit {unit.id!r}")
    unit_attempts.require_attempt_matches_slot(
        slot,
        attempt,
        layer_id=str(layer_id),
        unit_id=unit.id,
        unit_digest=unit_digest(unit),
        plan_hash=value.get("plan_hash"),
    )
    if slot.get("status") not in {"building", "repairing"}:
        raise ValueError(f"cannot freeze {unit.id} from state {slot.get('status')}")
    protected = set(unit.protects.resolve(active_contract_ids))
    protected.update(geometry_vis_protection_ids(unit.provides, layer_active_vis_ids))
    checkpoint = {
        "at": _now(),
        "candidate_hash": str(candidate_hash),
        "settings_hash": str(settings_hash),
        "script_hash": str(script_hash),
        "input_hash": str(input_hash),
        "protected_contract_ids": sorted(protected),
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


@serialized_state_mutation(_path)
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
    closure = downstream({failed_unit}, units) - {failed_unit}
    now = _now()
    for uid in sorted(closure):
        slot = value["units"][uid]
        before = slot.get("status")
        if before in {"passed", "superseded"}:
            continue
        if "blocked" not in _TRANSITIONS.get(before, set()) and before != "blocked":
            raise ValueError(f"cannot block dependent {uid} from state {before}")
        if before != "blocked":
            unit_attempts.archive_active_attempt(
                slot,
                disposition="revoked",
                reason=str(reason),
                evidence=[f"upstream:{failed_unit}"],
                at=now,
            )
            slot.setdefault("history", []).append(
                {"at": now, "from": before, "to": "blocked", "reason": str(reason)}
            )
            slot["status"] = "blocked"
            slot["updated"] = now
    value["updated"] = now
    _write(_path(folder, layer_id), value)
    return value


@serialized_state_mutation(_path)
def invalidate_checkpoint(
    folder: str | Path,
    layer_id: str,
    unit_id: str,
    units: tuple[WorkUnit, ...],
    *,
    reason: str,
    evidence: list[str],
) -> dict:
    """Revoke accepted authority and block every consumer in one audit transaction.

    This is deliberately separate from the ordinary state machine: discovering that an
    accepted checkpoint violated an authoritative invariant is not a retry transition.
    The old checkpoint remains in the audit record, but it no longer grants build authority.
    """
    if not reason.strip() or not evidence:
        raise ValueError("checkpoint invalidation requires a reason and non-empty evidence")
    validate_unit_dag(units, f"layer {layer_id} work units")
    value = load(folder, layer_id)
    if not value:
        raise ValueError("work-unit state is not initialized")
    validate_current(value, layer_id, units)
    if unit_id not in value["units"]:
        raise KeyError(f"unknown work unit {unit_id!r}")

    affected = downstream({unit_id}, units)
    now = _now()
    archived: dict[str, dict] = {}
    for uid in sorted(affected):
        slot = value["units"][uid]
        before = slot.get("status")
        if before == "superseded":
            raise ValueError(f"cannot invalidate superseded work unit {uid}")
        unit_attempts.archive_active_attempt(
            slot,
            disposition="revoked",
            reason=reason if uid == unit_id else f"upstream checkpoint {unit_id} invalidated",
            evidence=list(evidence),
            at=now,
            archive_checkpoint=False,
        )
        checkpoint = slot.pop("checkpoint", None)
        unit_completion_receipts.archive_completion_receipt(
            slot,
            disposition="revoked",
            reason=reason if uid == unit_id else f"upstream checkpoint {unit_id} invalidated",
            evidence=list(evidence),
            at=now,
        )
        if checkpoint:
            archived[uid] = checkpoint
            slot.setdefault("invalidated_checkpoints", []).append(
                {
                    "at": now,
                    "reason": reason,
                    "evidence": list(evidence),
                    "checkpoint": checkpoint,
                }
            )
        after = "retryable" if uid == unit_id else "blocked"
        slot.setdefault("history", []).append(
            {
                "at": now,
                "from": before,
                "to": after,
                "reason": reason if uid == unit_id else f"upstream checkpoint {unit_id} invalidated",
                "metadata": {"evidence": list(evidence), "invalidated_unit": unit_id},
            }
        )
        slot["status"] = after
        slot["updated"] = now

    record = {
        "schema": 1,
        "at": now,
        "layer": str(layer_id),
        "unit": unit_id,
        "reason": reason,
        "evidence": list(evidence),
        "affected": sorted(affected),
        "archived_checkpoints": archived,
    }
    value.setdefault("invalidations", []).append(record)
    value["updated"] = now
    _write(_path(folder, layer_id), value)
    return record


@selected_attempt_state_mutation
@serialized_state_mutation(_path)
def record_hypothesis_falsification(
    folder: str | Path,
    layer_id: str,
    unit: WorkUnit,
    units: tuple[WorkUnit, ...],
    *,
    bundle_hash: str,
    unit_plan_hash: str,
    candidate_hash: str,
    settings_hash: str,
    contract_ids: list[str],
    observations: list[dict],
    decisions: list[dict],
    conflict: dict,
    evidence: list[str],
    affected_seed_ids: set[str] | tuple[str, ...] | list[str] | None = None,
    preserve_accepted_source: bool = False,
    attempt: unit_attempts.UnitAttemptClaim | Mapping[str, Any] | None = None,
    selection_token: AuthoritySelectionToken | None = None,
) -> dict:
    """Seal a plan finding and stop the unit without granting it mutation authority.

    The authoritative copy lives in the work-unit state transaction.  A content-identical
    JSON artifact is also written for the public replan command and external review.
    """

    validate_unit_dag(units, f"layer {layer_id} work units")
    value = load(folder, layer_id)
    if not value:
        raise ValueError("work-unit state is not initialized")
    validate_current(value, layer_id, units)
    slot = value["units"].get(unit.id)
    if slot is None:
        raise KeyError(f"unknown work unit {unit.id!r}")
    before = slot.get("status")
    source_attempt = unit_attempts.require_attempt_matches_slot(
        slot,
        attempt,
        layer_id=str(layer_id),
        unit_id=unit.id,
        unit_digest=unit_digest(unit),
        plan_hash=value.get("plan_hash"),
    )
    preserve_accepted = bool(preserve_accepted_source and before == "passed")
    if not preserve_accepted and source_attempt is None:
        raise ValueError(
            "hypothesis falsification requires the exact active work-unit attempt; "
            "review an unclaimed historical row into retryable state first"
        )
    if not preserve_accepted and "hypothesis_falsified" not in _TRANSITIONS.get(before, set()):
        raise ValueError(f"cannot falsify plan hypothesis for {unit.id} from state {before}")
    if preserve_accepted_source and not preserve_accepted:
        raise ValueError(
            f"preserve_accepted_source requires passed state for {unit.id}, found {before}"
        )
    known_ids = {candidate.id for candidate in units}
    seeds = {str(uid) for uid in (affected_seed_ids or {unit.id}) if str(uid)}
    seeds.add(unit.id)
    upstream_owners = sorted(seeds - known_ids)
    local_seeds = (seeds & known_ids) | {unit.id}
    affected = sorted(downstream(local_seeds, units))
    now = _now()
    payload = {
        "schema": HYPOTHESIS_FALSIFICATION_SCHEMA,
        "record_id": "pending",
        "recorded_at": now,
        "layer": str(layer_id),
        "unit": unit.id,
        "identities": {
            "bundle_hash": str(bundle_hash),
            "plan_hash": str(value.get("plan_hash")),
            "unit_hash": unit_digest(unit),
            "unit_plan_hash": str(unit_plan_hash),
            "candidate_hash": str(candidate_hash),
            "settings_hash": str(settings_hash),
        },
        "contract_ids": list(contract_ids),
        "observations": list(observations),
        "decisions": list(decisions),
        "conflict": dict(conflict),
        "evidence": list(evidence),
        "affected": affected,
        "fault_owner_units": list(upstream_owners),
    }
    identity_payload = dict(payload)
    identity_payload.pop("record_id")
    digest = hashlib.sha256(
        json.dumps(identity_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    payload["record_id"] = f"hf-{digest[:20]}"
    HypothesisFalsification.parse(payload)

    event = {
        "at": now,
        "from": before,
        "to": "passed" if preserve_accepted else "hypothesis_falsified",
        "reason": (
            "composed canonical evidence falsified authority after unit acceptance; "
            "checkpoint preserved until transactional replan"
            if preserve_accepted
            else "executable evidence requires authority outside the active unit plan"
        ),
        "metadata": {"record_id": payload["record_id"], "affected": affected},
    }
    slot.setdefault("history", []).append(event)
    if not preserve_accepted:
        if source_attempt is not None:
            unit_attempts.archive_active_attempt(
                slot,
                disposition="completed",
                reason=event["reason"],
                evidence=list(evidence),
                at=now,
            )
            unit_attempts.archive_attempt_checkpoint(
                slot,
                claim_id=source_attempt.claim_id,
                disposition="revoked",
                reason=event["reason"],
                evidence=list(evidence),
                at=now,
            )
        slot["status"] = "hypothesis_falsified"
    slot["falsification"] = payload
    slot["updated"] = now
    for affected_unit in affected:
        if affected_unit == unit.id:
            continue
        dependent = value["units"][affected_unit]
        dependent_before = dependent.get("status")
        if dependent_before == "passed":
            # A typed upstream fault-owner finding must not silently revoke accepted
            # authority. Preserve the checkpoint until vfx units replan consumes this
            # immutable finding and publishes the complete invalidation transaction.
            continue
        if dependent_before == "superseded":
            raise ValueError(
                f"cannot record falsification while downstream unit {affected_unit} is "
                f"{dependent_before}; invalidate its accepted checkpoint first"
            )
        if dependent_before != "blocked":
            if "blocked" not in _TRANSITIONS.get(dependent_before, set()):
                raise ValueError(
                    f"cannot block downstream unit {affected_unit} from {dependent_before}"
                )
            unit_attempts.archive_active_attempt(
                dependent,
                disposition="revoked",
                reason=f"upstream hypothesis {unit.id} was falsified",
                evidence=list(evidence),
                at=now,
            )
            dependent.setdefault("history", []).append({
                "at": now,
                "from": dependent_before,
                "to": "blocked",
                "reason": f"upstream hypothesis {unit.id} was falsified",
                "metadata": {"record_id": payload["record_id"]},
            })
            dependent["status"] = "blocked"
            dependent["updated"] = now
    value.setdefault("falsifications", []).append(payload)
    value["updated"] = now
    _write(_path(folder, layer_id), value)
    # State is the commit receipt; JSON is only its derived projection.
    try:
        hypothesis_falsification_projection.publish_projection(folder, payload)
    except OSError as exc:
        raise hypothesis_falsification_projection.FalsificationProjectionPending(
            payload
        ) from exc
    return payload


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
            raise ValueError(
                f"work-unit state belongs to layer {value.get('layer')}, not {layer_id}"
            )
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
            raise ValueError(
                "digest-bound replan base uses an incompatible work-unit digest schema"
            )
        stored_hashes: dict[str, str] = {}
        for uid, row in value["units"].items():
            digest = (row or {}).get("unit_hash")
            if not isinstance(digest, str) or not digest:
                raise ValueError(
                    f"digest-bound replan base is missing unit_hash for {uid}"
                )
            stored_hashes[str(uid)] = digest
        new_by_id = {unit.id: unit for unit in new_units}
        added_ids = new_ids - state_unit_ids
        removed_ids = state_unit_ids - new_ids
        changed_ids = {
            uid
            for uid in state_unit_ids & new_ids
            if stored_hashes[uid] != unit_digest(new_by_id[uid])
        }
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
            "discarding proven work requires --discard-accepted or a typed falsification record"
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
    # plan state or the complete amended state, never one without the other.
    _write(_path(folder, layer_id), value)
    return record
