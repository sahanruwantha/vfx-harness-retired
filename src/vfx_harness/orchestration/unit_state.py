"""Durable work-unit state and checkpoint lifecycle operations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from vfx_harness.domain import unit_attempts, unit_completion_receipts
from vfx_harness.domain.authority_head_records import parse_authority_selection_token
from vfx_harness.domain.layer_finalizations import LayerFinalizationReceipt
from vfx_harness.domain.stop_envelope_primitives import require_digest
from vfx_harness.domain.unit_outcomes import HYPOTHESIS_FALSIFICATION_SCHEMA, HypothesisFalsification
from vfx_harness.domain.work_units import (
    UNIT_STATES,
    WorkUnit,
    geometry_vis_protection_ids,
    validate_unit_dag,
)
from vfx_harness.orchestration import (
    hypothesis_falsification_projection,
    layer_finalization_lifecycle,
)
from vfx_harness.orchestration.authority_selection_transaction import AuthoritySelectionToken
from vfx_harness.orchestration.authority_state_context import (
    resolve_current_authority_state,
)
from vfx_harness.orchestration.layer_finalization_authorizations import (
    AuthorizedLayerFinalizationMutation,
)
from vfx_harness.orchestration.unit_state_identity import (
    DIGEST_SCHEMA,
    downstream,
    unit_digest,
)
from vfx_harness.orchestration.unit_state_identity import (
    authorized_passed_unit_ids as authorized_passed_unit_ids,
)
from vfx_harness.orchestration.unit_state_identity import (
    replan_effects as replan_effects,
)
from vfx_harness.orchestration.unit_state_lifecycle import TRANSITIONS as _TRANSITIONS
from vfx_harness.orchestration.unit_state_lock import (
    serialized_state_mutation,
    unit_state_lock,
    unit_state_path,
)
from vfx_harness.orchestration.unit_state_queries import SCHEMA as SCHEMA
from vfx_harness.orchestration.unit_state_queries import load as load
from vfx_harness.orchestration.unit_state_queries import load_snapshot as load_snapshot
from vfx_harness.orchestration.unit_state_queries import (
    ready_from_durable_state as ready_from_durable_state,
)
from vfx_harness.orchestration.unit_state_queries import (
    unresolved_falsification as unresolved_falsification,
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
            raise ValueError(f"work-unit state belongs to layer {current.get('layer')}, not {layer_id}")
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
        raise ValueError(
            "work-unit state authority capsule does not match the selected layer "
            "capsule; publish authority through the atomic authority-state coordinator "
            "before initializing"
        )
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

    This is explicit orphan retirement only. If an authority-state transition cannot
    compare the old DAG — for example because its digests predate the current WorkUnit
    schema — no preservation base can be reconstructed. The operator records the exact
    owner, trigger, and evidence instead of hand-editing or silently reseeding state.
    """
    if not owner.strip() or not trigger.strip() or not evidence:
        raise ValueError("superseding layer units requires owner, trigger, and evidence")
    value = load(folder, layer_id)
    if not value:
        return {}
    accepted = sorted(uid for uid, row in value["units"].items() if row.get("status") == "passed")
    if accepted and not allow_accepted:
        raise ValueError(
            f"layer {layer_id} has accepted unit(s) {', '.join(accepted)}; "
            "move that state with a replan transaction instead"
        )
    now = _now()
    layer_finalization_lifecycle.archive_layer_finalization(
        value,
        disposition="superseded",
        reason=trigger,
        evidence=list(evidence),
        at=now,
    )
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
        raise ValueError("checkpoint freeze requires the exact active work-unit attempt")
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
            slot.setdefault("history", []).append({"at": now, "from": before, "to": "blocked", "reason": str(reason)})
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
    layer_finalization_lifecycle.archive_layer_finalization(
        value,
        disposition="revoked",
        reason=reason,
        evidence=list(evidence),
        at=now,
    )
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


def _hypothesis_falsification_payload(
    value: Mapping[str, Any],
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
    affected_seed_ids: set[str] | tuple[str, ...] | list[str] | None,
    recorded_at: str,
) -> dict:
    if not isinstance(recorded_at, str) or not recorded_at.strip():
        raise ValueError("hypothesis falsification recorded_at must be non-empty")
    known_ids = {candidate.id for candidate in units}
    seeds = {str(uid) for uid in (affected_seed_ids or {unit.id}) if str(uid)}
    seeds.add(unit.id)
    upstream_owners = sorted(seeds - known_ids)
    local_seeds = (seeds & known_ids) | {unit.id}
    affected = sorted(downstream(local_seeds, units))
    payload = {
        "schema": HYPOTHESIS_FALSIFICATION_SCHEMA,
        "record_id": "pending",
        "recorded_at": recorded_at,
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
    return payload


def prepare_accepted_hypothesis_falsification(
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
    recorded_at: str,
) -> dict:
    """Prepare the exact finding later projected from a terminal layer receipt."""

    validate_unit_dag(units, f"layer {layer_id} work units")
    with unit_state_lock(folder, layer_id, exclusive=False):
        value = load(folder, layer_id)
        if not value:
            raise ValueError("work-unit state is not initialized")
        validate_current(value, layer_id, units)
        slot = value["units"].get(unit.id)
        if not isinstance(slot, Mapping) or slot.get("status") != "passed":
            raise ValueError(
                "accepted hypothesis-falsification preparation requires a passed "
                f"source unit, found {None if slot is None else slot.get('status')!r}"
            )
        return _hypothesis_falsification_payload(
            value,
            layer_id,
            unit,
            units,
            bundle_hash=bundle_hash,
            unit_plan_hash=unit_plan_hash,
            candidate_hash=candidate_hash,
            settings_hash=settings_hash,
            contract_ids=contract_ids,
            observations=observations,
            decisions=decisions,
            conflict=conflict,
            evidence=evidence,
            affected_seed_ids=affected_seed_ids,
            recorded_at=recorded_at,
        )


def record_prepared_accepted_hypothesis_falsification(
    folder: str | Path,
    layer_id: str,
    units: tuple[WorkUnit, ...],
    payload: Mapping[str, Any],
    *,
    selection_token: AuthoritySelectionToken,
    required_layer_finalization_receipt_digest: str,
    finalization_authorization: AuthorizedLayerFinalizationMutation,
) -> dict:
    """Reconcile one exact receipt-carried finding without rediscovering evidence."""

    if not isinstance(payload, Mapping):
        raise ValueError("prepared hypothesis falsification must be an object")
    raw = dict(payload)
    parsed = HypothesisFalsification.parse(
        raw,
        "prepared hypothesis falsification",
    )
    if parsed.layer != str(layer_id):
        raise ValueError("prepared hypothesis falsification belongs to another layer")
    try:
        source = next(unit for unit in units if unit.id == parsed.unit)
    except StopIteration as exc:
        raise ValueError("prepared hypothesis falsification source unit is not in the current DAG") from exc
    identities = raw.get("identities")
    conflict = raw.get("conflict")
    if not isinstance(identities, Mapping) or not isinstance(conflict, Mapping):
        raise ValueError("prepared hypothesis falsification identities and conflict must be objects")
    seeds = {
        *(str(unit_id) for unit_id in parsed.affected),
        *(str(unit_id) for unit_id in parsed.fault_owner_units),
    }
    return record_hypothesis_falsification(
        folder,
        str(layer_id),
        source,
        units,
        bundle_hash=str(identities.get("bundle_hash")),
        unit_plan_hash=str(identities.get("unit_plan_hash")),
        candidate_hash=str(identities.get("candidate_hash")),
        settings_hash=str(identities.get("settings_hash")),
        contract_ids=list(parsed.contract_ids),
        observations=[dict(row) for row in parsed.observations],
        decisions=[{"id": row.id, "strength": row.strength} for row in parsed.decisions],
        conflict=dict(conflict),
        evidence=list(parsed.evidence),
        affected_seed_ids=seeds,
        preserve_accepted_source=True,
        selection_token=selection_token,
        recorded_at=parsed.recorded_at,
        expected_payload=raw,
        required_layer_finalization_receipt_digest=(required_layer_finalization_receipt_digest),
        finalization_authorization=finalization_authorization,
    )


def _require_finalization_mutation_authorization(
    folder: str | Path,
    value: Mapping[str, Any],
    units: tuple[WorkUnit, ...],
    *,
    selection_token: AuthoritySelectionToken,
    required_receipt_digest: str,
    authorization: AuthorizedLayerFinalizationMutation | None,
) -> LayerFinalizationReceipt:
    """Recheck a terminal authorization under selection-SH -> unit-state-EX."""

    if not isinstance(authorization, AuthorizedLayerFinalizationMutation):
        raise ValueError(
            "accepted hypothesis falsification requires typed terminal-finalization "
            "mutation authorization"
        )
    required_receipt_digest = require_digest(
        required_receipt_digest,
        "accepted hypothesis falsification terminal receipt",
    )
    receipt = authorization.receipt
    completions = authorization.completion_authorization
    if receipt.receipt_digest != required_receipt_digest:
        raise ValueError(
            "hypothesis falsification terminal authorization names another receipt"
        )
    if not isinstance(selection_token, AuthoritySelectionToken):
        raise ValueError(
            "accepted hypothesis falsification requires an exact selection token"
        )
    current_projection = parse_authority_selection_token(
        selection_token.to_dict(),
        "accepted hypothesis falsification selection token",
    )
    if completions.selection_token != current_projection:
        raise ValueError(
            "hypothesis falsification terminal authorization belongs to another "
            "selection"
        )
    context = resolve_current_authority_state(folder)
    if context is None or context.head_ref != completions.authority_state_head_ref:
        raise ValueError(
            "hypothesis falsification terminal authorization belongs to another "
            "authority-state head"
        )
    sealed = authorized_passed_unit_ids(
        value,
        units,
        completion_authorization=completions,
    )
    expected_units = {unit.id for unit in units}
    if sealed != expected_units:
        raise ValueError(
            "hypothesis falsification terminal authorization does not cover every "
            "current finalization unit"
        )
    raw_terminal = (
        (value.get("layer_finalization") or {}).get("terminal_receipt")
        if isinstance(value.get("layer_finalization"), Mapping)
        else None
    )
    current = LayerFinalizationReceipt.parse(
        raw_terminal,
        "accepted hypothesis falsification current terminal receipt",
    )
    if current != receipt:
        raise ValueError(
            "hypothesis falsification terminal authorization does not name the exact "
            "current receipt"
        )
    return current


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
    recorded_at: str | None = None,
    expected_payload: Mapping[str, Any] | None = None,
    required_layer_finalization_receipt_digest: str | None = None,
    finalization_authorization: AuthorizedLayerFinalizationMutation | None = None,
) -> dict:
    """Seal a plan finding and stop the unit without granting it mutation authority.

    The authoritative copy lives in the work-unit state transaction. A content-identical
    JSON artifact is also written for review and a future typed authority-replacement
    consumer; it does not itself authorize state movement.
    """

    validate_unit_dag(units, f"layer {layer_id} work units")
    value = load(folder, layer_id)
    if not value:
        raise ValueError("work-unit state is not initialized")
    validate_current(value, layer_id, units)
    authorized_terminal_receipt = None
    if required_layer_finalization_receipt_digest is not None:
        authorized_terminal_receipt = _require_finalization_mutation_authorization(
            folder,
            value,
            units,
            selection_token=selection_token,
            required_receipt_digest=required_layer_finalization_receipt_digest,
            authorization=finalization_authorization,
        )
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
    if preserve_accepted_source and required_layer_finalization_receipt_digest is None:
        raise ValueError(
            "preserving an accepted falsification source requires typed terminal "
            "finalization authority"
        )
    if not preserve_accepted and source_attempt is None:
        raise ValueError(
            "hypothesis falsification requires the exact active work-unit attempt; "
            "review an unclaimed historical row into retryable state first"
        )
    if not preserve_accepted and "hypothesis_falsified" not in _TRANSITIONS.get(before, set()):
        raise ValueError(f"cannot falsify plan hypothesis for {unit.id} from state {before}")
    if preserve_accepted_source and not preserve_accepted:
        raise ValueError(f"preserve_accepted_source requires passed state for {unit.id}, found {before}")
    now = recorded_at or _now()
    payload = _hypothesis_falsification_payload(
        value,
        layer_id,
        unit,
        units,
        bundle_hash=bundle_hash,
        unit_plan_hash=unit_plan_hash,
        candidate_hash=candidate_hash,
        settings_hash=settings_hash,
        contract_ids=contract_ids,
        observations=observations,
        decisions=decisions,
        conflict=conflict,
        evidence=evidence,
        affected_seed_ids=affected_seed_ids,
        recorded_at=now,
    )
    if expected_payload is not None and dict(expected_payload) != payload:
        raise ValueError("prepared hypothesis falsification no longer matches current durable state")
    if required_layer_finalization_receipt_digest is not None:
        assert authorized_terminal_receipt is not None
        bound_finding = authorized_terminal_receipt.projection.get("finding")
        if bound_finding != payload:
            raise ValueError(
                "hypothesis falsification payload is not the exact finding bound "
                "by the current terminal layer-finalization receipt"
            )
    affected = list(payload["affected"])
    same_id = [
        row
        for row in value.get("falsifications", [])
        if isinstance(row, Mapping) and row.get("record_id") == payload["record_id"]
    ]
    if same_id:
        if len(same_id) != 1 or dict(same_id[0]) != payload or slot.get("falsification") != payload:
            raise ValueError("hypothesis falsification record id conflicts with durable state")
        hypothesis_falsification_projection.publish_projection(folder, payload)
        return payload

    event = {
        "at": now,
        "from": before,
        "to": "passed" if preserve_accepted else "hypothesis_falsified",
        "reason": (
            "composed canonical evidence falsified authority after unit acceptance; "
            "checkpoint preserved until a validated authority amendment is selected"
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
            # authority. Preserve the checkpoint until reviewed replacement authority
            # publishes the complete invalidation transaction.
            continue
        if dependent_before == "superseded":
            raise ValueError(
                f"cannot record falsification while downstream unit {affected_unit} is "
                f"{dependent_before}; invalidate its accepted checkpoint first"
            )
        if dependent_before != "blocked":
            if "blocked" not in _TRANSITIONS.get(dependent_before, set()):
                raise ValueError(f"cannot block downstream unit {affected_unit} from {dependent_before}")
            unit_attempts.archive_active_attempt(
                dependent,
                disposition="revoked",
                reason=f"upstream hypothesis {unit.id} was falsified",
                evidence=list(evidence),
                at=now,
            )
            dependent.setdefault("history", []).append(
                {
                    "at": now,
                    "from": dependent_before,
                    "to": "blocked",
                    "reason": f"upstream hypothesis {unit.id} was falsified",
                    "metadata": {"record_id": payload["record_id"]},
                }
            )
            dependent["status"] = "blocked"
            dependent["updated"] = now
    value.setdefault("falsifications", []).append(payload)
    value["updated"] = now
    _write(_path(folder, layer_id), value)
    # State is the commit receipt; JSON is only its derived projection.
    try:
        hypothesis_falsification_projection.publish_projection(folder, payload)
    except OSError as exc:
        raise hypothesis_falsification_projection.FalsificationProjectionPending(payload) from exc
    return payload
