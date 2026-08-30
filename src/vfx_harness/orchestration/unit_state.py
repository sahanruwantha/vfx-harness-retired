"""Durable work-unit state, checkpoints, and transactional replanning.

This is intentionally independent of Blender and the model SDK.  It is the small vertical
slice that makes plan/state semantics testable before the expensive builder is taught to
execute every work unit directly.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from vfx_harness.domain.work_units import (
    UNIT_STATES,
    WorkUnit,
    geometry_vis_protection_ids,
    validate_unit_dag,
)
from vfx_harness.observability.provenance import atomic_write

SCHEMA = 1
# Bump whenever WorkUnit gains or loses a field that is always present in `unit_digest`:
# the hash covers asdict(unit) except empty publishes/consumes, which are omitted so
# schema-4 durable hashes stay comparable. Non-empty interface rows participate
# (HIR-0084). Bump WHENEVER a new always-present field lands in that payload —
# validate_current only knows to route cross-shape comparison through the replan
# closure when the schema numbers differ. 3: EvidenceBinding gained optional
# per-moment bindings (8ab8f5d shipped the field without the bump and bricked every
# layer's durable state until the replan). The golden-digest test pins this pairing.
# 4: MutationScope gained `dresses` (ADR-0007 appearance-assignment authority).
DIGEST_SCHEMA = 4
STATE_DIR = "state/work-units"

_TRANSITIONS = {
    "pending": {"planning", "blocked", "superseded"},
    "planning": {"building", "blocked", "failed", "retryable", "superseded"},
    "building": {"frozen", "blocked", "failed", "hypothesis_falsified", "retryable", "superseded"},
    "frozen": {"evaluating", "building", "blocked", "failed", "retryable", "superseded"},
    "evaluating": {"passed", "repairing", "blocked", "failed", "hypothesis_falsified", "retryable", "superseded"},
    "repairing": {"building", "frozen", "blocked", "failed", "hypothesis_falsified", "retryable", "superseded"},
    "retryable": {"planning", "building", "blocked", "failed", "superseded"},
    "blocked": {"pending", "planning", "superseded"},
    "failed": {"retryable", "superseded"},
    "hypothesis_falsified": {"superseded"},
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
    """Identity hash of a WorkUnit.

    Empty ``publishes`` / ``consumes`` are omitted so schema-4 durable hashes stay
    comparable for units that never declared interfaces. Non-empty values participate,
    so changing an interface id, kind, or export invalidates the producer and its
    ``apply_replan`` closure (HIR-0084). Adding a field that is always present in
    this payload still requires a DIGEST_SCHEMA bump.
    """
    payload = asdict(unit)
    if not payload.get("publishes"):
        payload.pop("publishes", None)
    if not payload.get("consumes"):
        payload.pop("consumes", None)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def digest_matched_passed(
    state: Mapping[str, Any] | dict,
    units: tuple[WorkUnit, ...],
) -> set[str]:
    """Passed units whose stored digest matches the current WorkUnit.

    Cross-schema stored hashes are not comparable; those rows stay in the passed
    set because apply_replan is the invalidation closure (HIR-0084).
    """
    rows = (state or {}).get("units") or {}
    passed = {
        uid for uid, row in rows.items()
        if isinstance(row, dict) and row.get("status") == "passed"
    }
    if not state or int(state.get("digest_schema", 1)) != DIGEST_SCHEMA:
        return passed
    by_id = {unit.id: unit for unit in units}
    sealed: set[str] = set()
    for uid in passed:
        unit = by_id.get(uid)
        row = rows.get(uid) or {}
        if unit is not None and row.get("unit_hash") == unit_digest(unit):
            sealed.add(uid)
    return sealed


def ready_from_durable_state(
    folder: str | Path,
    layer_id: str,
    units: tuple[WorkUnit, ...],
    *,
    eligible_passed: set[str] | None = None,
) -> tuple[WorkUnit, ...]:
    """Resolve the ready set from one fresh durable-state snapshot.

    A multi-unit build mutates unit state after every accepted checkpoint. Holding the
    snapshot that existed before a producer ran makes its newly passed digest invisible
    to the next scheduling decision. Read, validate, derive passed ids, and verify
    producer digests together so readiness cannot mix lifecycle generations.

    ``eligible_passed`` may narrow passed rows whose replay artifacts are locally
    available to a caller; it can never broaden durable acceptance.
    """
    from vfx_harness.domain.work_units import ready_units

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
    if int(value.get("digest_schema", 1)) != DIGEST_SCHEMA:
        # `unit_digest` hashes the whole WorkUnit, so ADDING a field changes every
        # stored digest and would brick durable state on any schema growth (adding
        # `look_capabilities` did exactly that). Digests from another schema are not
        # comparable, so identity is verified here and the replan closure — which
        # recomputes both sides under the current schema — decides what is preserved.
        return
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
        "updated": now,
    }
    _write(_path(folder, layer_id), value)
    return value


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
    layer_active_vis_ids: Iterable[str] = (),
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

    affected = _downstream({unit_id}, units)
    now = _now()
    archived: dict[str, dict] = {}
    for uid in sorted(affected):
        slot = value["units"][uid]
        before = slot.get("status")
        if before == "superseded":
            raise ValueError(f"cannot invalidate superseded work unit {uid}")
        checkpoint = slot.pop("checkpoint", None)
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
) -> dict:
    """Seal a plan finding and stop the unit without granting it mutation authority.

    The authoritative copy lives in the work-unit state transaction.  A content-identical
    JSON artifact is also written for the public replan command and external review.
    """
    from vfx_harness.domain.unit_outcomes import (
        HYPOTHESIS_FALSIFICATION_SCHEMA,
        HypothesisFalsification,
    )

    validate_unit_dag(units, f"layer {layer_id} work units")
    value = load(folder, layer_id)
    if not value:
        raise ValueError("work-unit state is not initialized")
    validate_current(value, layer_id, units)
    slot = value["units"].get(unit.id)
    if slot is None:
        raise KeyError(f"unknown work unit {unit.id!r}")
    before = slot.get("status")
    preserve_accepted = bool(preserve_accepted_source and before == "passed")
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
    affected = sorted(_downstream(local_seeds, units))
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

    artifact = Path(folder) / STATE_DIR / "hypothesis-falsifications" / f"{payload['record_id']}.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(artifact, json.dumps(payload, indent=2, sort_keys=True) + "\n")

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
    return payload


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


def replan_effects(
    old_units: tuple[WorkUnit, ...], new_units: tuple[WorkUnit, ...]
) -> dict[str, list[str]]:
    """Return the deterministic supersession closure without mutating durable state."""
    validate_unit_dag(old_units, "old work-unit DAG")
    validate_unit_dag(new_units, "new work-unit DAG")
    old = {unit.id: unit for unit in old_units}
    new = {unit.id: unit for unit in new_units}
    added = set(new) - set(old)
    removed = set(old) - set(new)
    changed = {uid for uid in set(old) & set(new) if unit_digest(old[uid]) != unit_digest(new[uid])}
    invalidated = _downstream(added | changed, new_units)
    preserved = set(old) & set(new) - invalidated
    return {
        "added": sorted(added),
        "removed": sorted(removed),
        "changed": sorted(changed),
        "invalidated": sorted(invalidated),
        "preserved": sorted(preserved),
    }


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
        invalidated_ids = _downstream(added_ids | changed_ids, new_units)
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

    next_slots: dict[str, dict] = {}
    superseded = list(value.get("superseded") or [])
    for uid in retiring:
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
