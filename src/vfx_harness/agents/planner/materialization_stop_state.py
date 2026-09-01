"""Semantic projections of durable inputs to a materialization stop.

The source files are retained in the uncited audit sidecar.  These projections contain
only state that the materializer or its deterministic validators consume, so clocks,
run ids, histories, and file locators cannot create a new recovery attempt.
"""

from __future__ import annotations

import json
from typing import Any

from vfx_harness.domain.layer_outcomes import (
    LayerOutcomeContractError,
    parse_sealed_layer_outcome,
)
from vfx_harness.domain.plan_records import (
    ResolutionLedgerError,
    active_structured_decisions_from_text,
    roles_match_reserved,
)
from vfx_harness.domain.unit_outcomes import HypothesisFalsification
from vfx_harness.domain.work_units import UNIT_STATES
from vfx_harness.orchestration.jit_materialization.gate_evidence import (
    normalized_materialization_message,
)
from vfx_harness.orchestration.unit_state import DIGEST_SCHEMA

_CHECKPOINT_FIELDS = {
    "at",
    "candidate_hash",
    "settings_hash",
    "script_hash",
    "input_hash",
    "protected_contract_ids",
    "unit_hash",
}


def _digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _falsification_identity(
    value: object,
    *,
    layer_id: str,
    unit_id: str,
    plan_hash: str,
    unit_hash: str,
) -> dict[str, Any]:
    parsed = HypothesisFalsification.parse(value, f"unit {unit_id} falsification")
    if (
        parsed.layer != layer_id
        or parsed.unit != unit_id
        or parsed.plan_hash != plan_hash
        or parsed.unit_hash != unit_hash
    ):
        raise ValueError("falsification identities do not match durable unit state")
    return {
        "schema": value["schema"],
        "layer": parsed.layer,
        "unit": parsed.unit,
        "identities": {
            "bundle_hash": parsed.bundle_hash,
            "plan_hash": parsed.plan_hash,
            "unit_hash": parsed.unit_hash,
            "unit_plan_hash": parsed.unit_plan_hash,
            "candidate_hash": parsed.candidate_hash,
            "settings_hash": parsed.settings_hash,
        },
        "contract_ids": list(parsed.contract_ids),
        "observations": [dict(item) for item in parsed.observations],
        "decisions": [
            {"id": item.id, "strength": item.strength}
            for item in parsed.decisions
        ],
        "conflict": {
            "kind": parsed.conflict.kind,
            "required_authority": parsed.conflict.required_authority,
            "roles": list(parsed.conflict.roles),
            "controls": list(parsed.conflict.controls),
        },
        "affected": list(parsed.affected),
        "fault_owner_units": list(parsed.fault_owner_units),
    }


def _checkpoint_identity(
    value: object,
    *,
    unit_hash: str,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _CHECKPOINT_FIELDS:
        raise ValueError("checkpoint fields do not match the durable producer schema")
    for field in (
        "candidate_hash",
        "settings_hash",
        "script_hash",
        "input_hash",
        "unit_hash",
    ):
        if not _digest(value[field]):
            raise ValueError(f"checkpoint {field} is not a SHA-256 digest")
    if value["unit_hash"] != unit_hash:
        raise ValueError("checkpoint unit_hash does not match its durable unit")
    protected = value["protected_contract_ids"]
    if (
        not isinstance(protected, list)
        or any(
            not isinstance(item, str) or not item or item != item.strip()
            for item in protected
        )
        or protected != sorted(set(protected))
    ):
        raise ValueError("checkpoint protected_contract_ids must be sorted unique ids")
    return {key: item for key, item in value.items() if key != "at"}


def unit_state(
    payload: bytes | None,
    *,
    layer_id: str,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    if payload is None:
        return {"state": "absent"}, ()
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"state": "invalid", "reason": "malformed"}, ("layer_state_malformed",)
    units = value.get("units") if isinstance(value, dict) else None
    plan_hash = value.get("plan_hash") if isinstance(value, dict) else None
    revision = value.get("revision") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or value.get("schema") != 1
        or value.get("digest_schema") != DIGEST_SCHEMA
        or str(value.get("layer") or "") != layer_id
        or not _digest(plan_hash)
        or not isinstance(revision, int)
        or isinstance(revision, bool)
        or revision < 1
        or not isinstance(units, dict)
    ):
        return {"state": "invalid", "reason": "schema_or_layer"}, ("layer_state_invalid",)
    semantic_units: dict[str, Any] = {}
    for unit_id, row in sorted(units.items(), key=lambda pair: str(pair[0])):
        if (
            not isinstance(unit_id, str)
            or not unit_id
            or unit_id != unit_id.strip()
            or not isinstance(row, dict)
            or row.get("status") not in UNIT_STATES
            or not _digest(row.get("unit_hash"))
        ):
            return {"state": "invalid", "reason": "unit_shape"}, ("layer_state_invalid",)
        semantic = {
            "status": row["status"],
            "unit_hash": row["unit_hash"],
        }
        try:
            if "checkpoint" in row:
                semantic["checkpoint"] = _checkpoint_identity(
                    row["checkpoint"],
                    unit_hash=row["unit_hash"],
                )
            if "falsification" in row:
                semantic["falsification"] = _falsification_identity(
                    row["falsification"],
                    layer_id=layer_id,
                    unit_id=unit_id,
                    plan_hash=plan_hash,
                    unit_hash=row["unit_hash"],
                )
            if row["status"] in {"frozen", "evaluating", "repairing", "passed"} and (
                "checkpoint" not in semantic
            ):
                raise ValueError(f"unit {unit_id} status requires an accepted checkpoint")
            if row["status"] == "hypothesis_falsified" and "falsification" not in semantic:
                raise ValueError(f"unit {unit_id} hypothesis_falsified status requires its finding")
            if "falsification" in semantic and row["status"] not in {
                "hypothesis_falsified",
                "passed",
            }:
                raise ValueError(
                    f"unit {unit_id} falsification is illegal for status {row['status']}"
                )
        except ValueError:
            return {"state": "invalid", "reason": "unit_shape"}, ("layer_state_invalid",)
        semantic_units[str(unit_id)] = semantic
    return {
        "state": "present",
        "schema": value["schema"],
        "digest_schema": value["digest_schema"],
        "layer": layer_id,
        "plan_hash": plan_hash,
        "revision": revision,
        "units": semantic_units,
    }, ()


def active_resolution_state(
    payload: bytes | None,
    *,
    bundle_digest: str,
    reserved_roles: tuple[str, ...],
) -> tuple[dict[str, Any], tuple[str, ...]]:
    try:
        text = "" if payload is None else payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return {"state": "invalid", "reason": "encoding"}, (
            "plan_resolutions_malformed",
        )
    try:
        parsed = active_structured_decisions_from_text(
            text,
            bundle_hash=bundle_digest,
        )
    except ResolutionLedgerError as exc:
        issue = (
            "plan_resolutions_malformed"
            if exc.code == "malformed_row"
            else "plan_resolutions_invalid"
        )
        return {"state": "invalid", "reason": exc.code}, (issue,)
    active = {
        decision_id: {
            "id": decision.id,
            "decision": decision.decision,
            "contract": decision.contract,
        }
        for decision_id, decision in parsed.items()
    }
    binding = [
        row
        for row in active.values()
        if roles_match_reserved(
            [str(role) for role in (row["contract"].get("roles") or [])],
            reserved_roles,
        )
    ]
    binding.sort(key=lambda row: str(row["id"]))
    return {
        "schema": "vfx-harness.materialization-binding-decisions/v1",
        "bundle_digest": bundle_digest,
        "decisions": binding,
    }, ()


def dependency_outcome_state(
    payload: bytes | None,
    *,
    dependency: str,
    required_outcomes: frozenset[tuple[str, str]],
    script_state: dict[str, Any] | None,
    current_publication_receipt_digest: str | None,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    if payload is None:
        return {"layer": dependency, "status": "missing"}, ()
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"layer": dependency, "status": "invalid", "reason": "malformed"}, (
            f"dependency_outcome_{dependency}_malformed",
        )
    try:
        sealed = parse_sealed_layer_outcome(
            value,
            expected_layer_id=dependency,
        )
    except LayerOutcomeContractError as exc:
        return {"layer": dependency, "status": "invalid", "reason": exc.code}, (
            f"dependency_outcome_{dependency}_invalid",
        )
    if sealed.status == "passed":
        if current_publication_receipt_digest is None:
            return {
                "layer": dependency,
                "status": "invalid",
                "reason": "publication_unverified",
            }, (f"dependency_outcome_{dependency}_publication_unverified",)
        if current_publication_receipt_digest != sealed.receipt_digest:
            return {
                "layer": dependency,
                "status": "invalid",
                "reason": "publication_receipt_mismatch",
            }, (f"dependency_outcome_{dependency}_publication_receipt_mismatch",)
    matched = [
        {
            key: (
                normalized_materialization_message(item)
                if isinstance(item, str) and key in {"error", "note", "source"}
                else item
            )
            for key, item in row.items()
        }
        for row in sealed.required_evidence(required_outcomes)
    ]
    state = {
        "layer": sealed.layer_id,
        "status": sealed.status,
        "script": script_state,
        "required_evidence": matched,
    }
    if sealed.status == "passed":
        state["finalization_receipt_digest"] = sealed.receipt_digest
    return state, ()
