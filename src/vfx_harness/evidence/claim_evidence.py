"""Claim/evidence closure and typed critic-observation reconciliation.

The important distinction in this module is between a contradiction and an absence of
coverage.  A passing contract disproves a critic only when that contract is explicitly
bound to the claim/property being discussed.  No contract at all is a plan defect, not
permission to edit the scene and not evidence that the critic is wrong.
"""

from __future__ import annotations

import fnmatch
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from vfx_harness.domain.work_units import EXTRA_FRAME_BINDING_RULE
from vfx_harness.observability import run_artifacts

OBSERVATION_KINDS = {"measurable", "qualitative"}
RECONCILIATION_STATES = {
    "actionable",
    "contradicted",
    "contract_gap",
    "unverified_qualitative",
    "protocol_error",
}
_CIRCULAR_PROPERTIES = {"authoritative_contracts", "all_contracts", "contracts_pass"}
_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")


def _strings(value: Any, where: str, *, allow_empty: bool = True) -> tuple[str, ...]:
    if not isinstance(value, list) or (not allow_empty and not value):
        qualifier = "non-empty " if not allow_empty else ""
        raise ValueError(f"{where} must be a {qualifier}list")
    out = tuple(str(item).strip() for item in value)
    if any(not item for item in out):
        raise ValueError(f"{where} entries must be non-empty strings")
    if len(set(out)) != len(out):
        raise ValueError(f"{where} contains duplicates")
    return out


@dataclass(frozen=True, slots=True)
class Observation:
    """One typed visual observation emitted by a claim judge or coverage auditor."""

    id: str
    kind: str
    axis: str
    property: str
    observation: str
    action: str
    moment: int
    roles: tuple[str, ...]
    claim_id: str | None = None
    check_ids: tuple[str, ...] = ()
    panel_ids: tuple[str, ...] = ()

    @classmethod
    def parse(cls, value: Any, where: str = "observation") -> Observation:
        if not isinstance(value, dict):
            raise ValueError(f"{where} must be an object")
        oid = str(value.get("id") or "").strip()
        kind = str(value.get("kind") or "").strip()
        axis = str(value.get("axis") or "").strip()
        prop = str(value.get("property") or "").strip()
        text = " ".join(str(value.get("observation") or "").split())
        action = " ".join(str(value.get("action") or "").split())
        moment = value.get("moment")
        if not oid or not axis or not prop or not text or not action:
            raise ValueError(f"{where} requires id, axis, property, observation, and action")
        for field, item in (("id", oid), ("axis", axis), ("property", prop)):
            if not _ID.fullmatch(item):
                raise ValueError(f"{where}.{field} has invalid id {item!r}")
        if kind not in OBSERVATION_KINDS:
            raise ValueError(f"{where}.kind must be one of {sorted(OBSERVATION_KINDS)}")
        if isinstance(moment, bool) or not isinstance(moment, int) or moment < 1:
            raise ValueError(f"{where}.moment must be a positive integer")
        claim_id = value.get("claim_id")
        if claim_id is not None:
            claim_id = str(claim_id).strip() or None
        return cls(
            oid,
            kind,
            axis,
            prop,
            text,
            action,
            moment,
            _strings(value.get("roles", []), f"{where}.roles"),
            claim_id,
            _strings(value.get("check_ids", []), f"{where}.check_ids"),
            _strings(value.get("panel_ids", []), f"{where}.panel_ids"),
        )


def reconcile_observation(
    observation: Observation,
    evidence: Iterable[Mapping[str, Any]],
    *,
    claim_bindings: Mapping[str, set[str] | frozenset[str] | tuple[str, ...]] | None = None,
    qualified_claims: set[str] | frozenset[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    """Classify an observation without granting the critic execution authority.

    ``claim_bindings`` is the closure produced by the plan gate.  A check cited for a
    claim but absent from that closure is a protocol error even if a check with that id
    exists elsewhere; evidence about another property cannot settle this one.
    """

    facts = {str(row.get("id")): dict(row) for row in evidence if row.get("id")}
    cited = list(observation.check_ids)
    if observation.claim_id and claim_bindings is not None:
        if observation.claim_id not in claim_bindings:
            return {
                "state": "protocol_error",
                "observation": asdict(observation),
                "reason": f"names unknown claim {observation.claim_id}",
                "check_ids": cited,
            }
        allowed = set(claim_bindings.get(observation.claim_id, ()))
        outside = sorted(set(cited) - allowed)
        if outside:
            return {
                "state": "protocol_error",
                "observation": asdict(observation),
                "reason": "cites evidence not bound to the claim: " + ", ".join(outside),
                "check_ids": cited,
            }
    # A qualification binding is evidence of *judgment authority*, not a scene fact
    # with PASS/FAIL.  Critics commonly cite the exact bound qualification id in
    # check_ids.  Treat that id as known when it belongs to this qualified claim so it
    # reaches the qualified-qualitative branch below; accepting an arbitrary unknown id
    # would still be a protocol error.
    qualification_ids = (
        set(claim_bindings.get(observation.claim_id, ()))
        if observation.kind == "qualitative"
        and observation.claim_id in set(qualified_claims)
        and claim_bindings is not None
        else set()
    )
    missing = sorted(
        cid for cid in cited if cid not in facts and cid not in qualification_ids
    )
    if missing:
        return {
            "state": "protocol_error",
            "observation": asdict(observation),
            "reason": "cites unknown evidence: " + ", ".join(missing),
            "check_ids": cited,
        }
    failed = [
        cid
        for cid in cited
        if cid in facts and facts[cid].get("authoritative") and not facts[cid].get("pass")
    ]
    if failed:
        return {
            "state": "actionable",
            "observation": asdict(observation),
            "reason": "supported by failed authoritative evidence",
            "check_ids": failed,
        }
    passing = [
        cid
        for cid in cited
        if cid in facts and facts[cid].get("authoritative") and facts[cid].get("pass")
    ]
    if passing:
        return {
            "state": "contradicted",
            "observation": asdict(observation),
            "reason": "directly bound authoritative evidence passes",
            "check_ids": passing,
        }
    if observation.kind == "qualitative" and observation.claim_id in set(qualified_claims):
        return {
            "state": "actionable",
            "observation": asdict(observation),
            "reason": "claim has qualified qualitative blocking authority",
            "check_ids": cited,
        }
    state = "contract_gap" if observation.kind == "measurable" else "unverified_qualitative"
    return {
        "state": state,
        "observation": asdict(observation),
        "reason": (
            "no authoritative evidence is bound to this measurable property"
            if state == "contract_gap"
            else "qualitative observation has no qualified blocking authority"
        ),
        "check_ids": cited,
    }


def reconcile_observations(
    observations: Iterable[Observation],
    evidence: Iterable[Mapping[str, Any]],
    *,
    claim_bindings: Mapping[str, set[str] | frozenset[str] | tuple[str, ...]] | None = None,
    qualified_claims: set[str] | frozenset[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    rows = [
        reconcile_observation(
            item,
            evidence,
            claim_bindings=claim_bindings,
            qualified_claims=qualified_claims,
        )
        for item in observations
    ]
    by_state = {
        state: [row for row in rows if row["state"] == state]
        for state in sorted(RECONCILIATION_STATES)
    }
    return {
        "observations": rows,
        "by_state": by_state,
        "has_actionable": bool(by_state["actionable"]),
        "has_contract_gap": bool(by_state["contract_gap"]),
        "has_protocol_error": bool(by_state["protocol_error"]),
    }


def append_gap_record(
    folder: str | Path,
    *,
    layer: str,
    unit: str,
    candidate_hash: str,
    settings_hash: str,
    rows: list[dict[str, Any]],
) -> Path:
    """Persist verified gaps as planner input, never as live-builder authority."""

    gaps = [row for row in rows if row.get("state") == "contract_gap"]
    if not gaps:
        raise ValueError("gap record requires at least one contract_gap observation")
    path = run_artifacts.shot_state_dir(folder) / "contract-gaps.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "schema": 1,
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "classification": "plan_defect",
        "layer": str(layer),
        "unit": str(unit),
        "candidate_hash": str(candidate_hash),
        "settings_hash": str(settings_hash),
        "gaps": gaps,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    return path


@dataclass(frozen=True, slots=True)
class ClosureFinding:
    where: str
    what: str


@dataclass(frozen=True, slots=True)
class ClosureResult:
    findings: tuple[ClosureFinding, ...]
    claim_bindings: dict[str, frozenset[str]]
    bound_contract_ids: frozenset[str]
    required_contract_ids: frozenset[str]

    @property
    def clean(self) -> bool:
        return not self.findings


def validate_claim_closure(folder: str | Path, layers: Iterable[Any]) -> ClosureResult:
    """Prove that schema-4 required claims close over the plan's concrete contracts."""

    from vfx_harness.domain.contracts import load_document

    root = Path(folder)
    from vfx_harness.orchestration.plan_authority import selected_artifact_path

    scene_rows = load_document(selected_artifact_path(root, "scene_checks.json"), "contracts")
    image_rows = load_document(selected_artifact_path(root, "checks.json"), "checks")
    catalogs = {
        "scene_contract": {str(row.get("id")): row for row in scene_rows if row.get("id")},
        "image_contract": {str(row.get("id")): row for row in image_rows if row.get("id")},
    }
    from vfx_harness.domain.image_debts import (
        metric_matches_property,
        normalize_evidence_id,
    )
    from vfx_harness.evidence.checks import load_image_contract_payment_rows

    runtime_by_id = {
        normalize_evidence_id(row.get("id")): row
        for row in load_image_contract_payment_rows(root)
        if normalize_evidence_id(row.get("id"))
        and str(row.get("origin") or "") == "builder"
    }
    findings: list[ClosureFinding] = []
    claim_bindings: dict[str, frozenset[str]] = {}
    bound: set[str] = set()
    required: set[str] = set()

    for layer in layers:
        layer_id = str(getattr(layer, "id", ""))
        layer_axes = set(getattr(layer, "owns", ()))
        for _kind, rows in catalogs.items():
            for cid, row in rows.items():
                if str(row.get("owner_layer")) == layer_id:
                    required.add(cid)
        for unit in getattr(layer, "stages", ()):
            unit_frames = {int(point.frame) for point in unit.evaluation.judges}
            required_claims = [claim for claim in unit.evaluation.claims if claim.required]
            subject_roles = {
                role
                for claim in required_claims
                for role in claim.subject_roles
            }
            for mutation_role in unit.mutates.roles:
                if not any(
                    fnmatch.fnmatchcase(role, mutation_role) or fnmatch.fnmatchcase(mutation_role, role)
                    for role in subject_roles
                ):
                    findings.append(
                        ClosureFinding(
                            f"layer {layer_id} unit {unit.id}",
                            f"mutation role {mutation_role!r} has no required claim",
                        )
                    )
            for claim in unit.evaluation.claims:
                where = f"layer {layer_id} unit {unit.id} claim {claim.id}"
                if claim.axis not in layer_axes:
                    findings.append(ClosureFinding(where, f"axis {claim.axis!r} is not owned by the layer"))
                outside = sorted(set(claim.moments) - unit_frames)
                if outside:
                    findings.append(
                        ClosureFinding(
                            where,
                            f"moments outside the unit judge set: {outside}. "
                            f"{EXTRA_FRAME_BINDING_RULE}",
                        )
                    )
                if claim.property in _CIRCULAR_PROPERTIES:
                    findings.append(
                        ClosureFinding(
                            where,
                            "circular aggregate property cannot stand in for atomic claim coverage",
                        )
                    )
                ids: set[str] = set()
                for binding in claim.evidence:
                    if binding.kind in catalogs:
                        record = catalogs[binding.kind].get(binding.id)
                        if record is None and binding.kind == "image_contract":
                            record = runtime_by_id.get(normalize_evidence_id(binding.id))
                        if record is None:
                            if binding.kind == "image_contract":
                                # Materialization forbids candidate-sensitive checks.json
                                # rows. HIR-0046 requires the look claim to bind the id
                                # now; HIR-0047 counts that id as a producer so the unit
                                # plan is not retracted for a debt the builder still owes.
                                # A runtime_checks.json row that matches id/axis/frame is
                                # a paid debt (HIR-0048); absence here is still a debt.
                                ids.add(binding.id)
                                bound.add(binding.id)
                                continue
                            findings.append(
                                ClosureFinding(
                                    where,
                                    f"{binding.kind} binding {binding.id!r} does not exist",
                                )
                            )
                            continue
                        if str(record.get("axis") or "") != claim.axis:
                            findings.append(
                                ClosureFinding(
                                    where,
                                    f"binding {binding.id!r} belongs to axis "
                                    f"{record.get('axis')!r}, not {claim.axis!r}",
                                )
                            )
                        if record.get("frame") is not None and int(record["frame"]) not in claim.moments:
                            findings.append(
                                ClosureFinding(
                                    where,
                                    f"binding {binding.id!r} targets f{record['frame']} "
                                    f"outside claim moments. {EXTRA_FRAME_BINDING_RULE}",
                                )
                            )
                        if (
                            binding.kind == "image_contract"
                            and record.get("metric")
                            and not metric_matches_property(
                                str(record.get("metric") or ""),
                                str(claim.property or ""),
                            )
                        ):
                            findings.append(
                                ClosureFinding(
                                    where,
                                    f"binding {binding.id!r} metric {record.get('metric')!r} "
                                    f"does not certify property {claim.property!r}",
                                )
                            )
                        ids.add(binding.id)
                        bound.add(binding.id)
                    elif binding.kind == "qualification":
                        suite = (claim.qualification or {}).get("suite")
                        if binding.id != suite:
                            findings.append(
                                ClosureFinding(
                                    where,
                                    f"qualification binding {binding.id!r} does not match suite {suite!r}",
                                )
                            )
                    # semantic_diff, human_decision, and unbound image_contract ids are
                    # declared evidence paths whose runtime result cannot exist at plan
                    # time. Their typed ids are still part of the claim closure and are
                    # resolved at evaluation (HIR-0047).
                if claim.id in claim_bindings:
                    findings.append(ClosureFinding(where, "claim id is duplicated across work units"))
                claim_bindings[claim.id] = frozenset(ids)
            context = unit.evaluation.composition_context
            if context is not None:
                context_where = f"layer {layer_id} unit {unit.id} composition_context"
                for cid in context.contract_ids:
                    record = catalogs["scene_contract"].get(cid)
                    if record is None:
                        findings.append(
                            ClosureFinding(
                                context_where,
                                f"scene_contract binding {cid!r} does not exist",
                            )
                        )
                        continue
                    bound.add(cid)

    missing = sorted(required - bound)
    for cid in missing:
        findings.append(ClosureFinding(cid, "owned planner contract is not bound to any claim"))
    return ClosureResult(tuple(findings), claim_bindings, frozenset(bound), frozenset(required))
