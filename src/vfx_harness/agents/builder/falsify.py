"""Stage 3 — the build + critic loop for one PLAN LAYER."""

from __future__ import annotations

import fnmatch
import hashlib
import json
from typing import TYPE_CHECKING

from vfx_harness.agents.builder.authority import commit_selected_authority
from vfx_harness.agents.builder.critic_focus import _unsatisfiable_pair_findings
from vfx_harness.domain import plan_records
from vfx_harness.domain.brief import Shot
from vfx_harness.domain.image_debts import conflict_authority
from vfx_harness.domain.unit_outcomes import falsifying_decisions
from vfx_harness.domain.work_units import dependency_ordered_units
from vfx_harness.evidence.claim_evidence import append_gap_record
from vfx_harness.observability.log import (
    log,
)
from vfx_harness.observability.run_artifacts import shot_state_dir
from vfx_harness.orchestration import layer_plans, plan_authority
from vfx_harness.orchestration.ledger import Milestone
from vfx_harness.orchestration.unit_state import record_hypothesis_falsification

if TYPE_CHECKING:
    from vfx_harness.domain.unit_attempts import UnitAttemptClaim
    from vfx_harness.orchestration.authority_selection import ResolvedSelectedAuthority


def _selected_bundle(shot: Shot, selected_authority: ResolvedSelectedAuthority | None):
    if selected_authority is None:
        return plan_authority.resolve_current(shot.folder)
    if selected_authority.plan is None:
        raise ValueError("hypothesis falsification requires selected plan authority")
    return selected_authority.plan.bundle


def _persist_contract_gaps(
    shot: Shot,
    layer,
    m: Milestone,
    render_rel: str,
    verdict: dict,
    *,
    mode: str = "eevee",
    selected_authority: ResolvedSelectedAuthority | None = None,
) -> None:
    """Pin a coverage defect to the exact canonical pixels and comparison boundary."""
    rows = list(verdict.get("observation_reconciliation") or [])
    if not verdict.get("contract_gap") or not any(row.get("state") == "contract_gap" for row in rows):
        return
    candidate = shot.folder / render_rel
    if not candidate.is_file():
        return
    unit_id = "acceptance"
    if layer is not None:
        active = [
            unit.id
            for unit in layer.stages
            if any(int(m.frame) in claim.moments for claim in unit.evaluation.claims)
        ]
        unit_id = active[0] if len(active) == 1 else "+".join(active) or "coverage_audit"
    settings = {
        "mode": str(mode),
        "scale": 0.5,
        "frame": int(m.frame),
        "reference": str(m.ref),
        "reference_sha256": hashlib.sha256((shot.folder / m.ref).read_bytes()).hexdigest(),
    }
    def mutation():
        return append_gap_record(
            shot.folder,
            layer=str(getattr(layer, "id", m.id)),
            unit=unit_id,
            candidate_hash=hashlib.sha256(candidate.read_bytes()).hexdigest(),
            settings_hash=hashlib.sha256(
                json.dumps(settings, sort_keys=True, separators=(",", ":")).encode(
                    "utf-8"
                )
            ).hexdigest(),
            rows=rows,
        )
    if selected_authority is None:
        mutation()
    else:
        commit_selected_authority(
            shot.folder,
            selected_authority,
            operation=f"record contract gap for {getattr(layer, 'id', m.id)}.{unit_id}",
            mutation=mutation,
        )


def _record_unsatisfiable_pair_falsification(
    shot: Shot,
    layer,
    unit,
    milestone,
    ledger,
    *,
    attempt: UnitAttemptClaim | None = None,
    selected_authority: ResolvedSelectedAuthority | None = None,
) -> dict | None:
    """Escalate a published schedule/smoothness pair (or cannot_express) as a plan defect.

    Classification is arithmetic plus an explicit in-scope abstention, not a named
    decision path: consecutive ``keyframe_schedule`` samples already exceed
    ``curve_derivative_max.hi``, so no in-scope interpolation can pass. Run
    ``20260826T170413Z-ba2b4c`` spent two repairs proving that floor.
    """

    slot = ledger._slot(milestone)
    declared = slot.get("cannot_express") if isinstance(slot.get("cannot_express"), dict) else {}
    failing = {
        str(item["id"])
        for row in (slot.get("rounds") or [])
        if row.get("kind") == "canonical"
        for item in (row.get("evidence") or [])
        if isinstance(item, dict) and item.get("id") and not item.get("pass")
    }
    pairs = _unsatisfiable_pair_findings(
        shot,
        failing,
        selected_authority=selected_authority,
    )
    contract_ids = list(declared.get("contract_ids") or [])
    reason = str(declared.get("reason") or "")
    if pairs:
        contract_ids = sorted({
            *contract_ids,
            *(pair["schedule_id"] for pair in pairs),
            *(pair["smoothness_id"] for pair in pairs),
        })
        reason = reason or pairs[0]["message"]
    if not contract_ids or not reason:
        return None
    bundle = _selected_bundle(shot, selected_authority)
    script_rel = slot.get("script")
    if not script_rel:
        raise ValueError("terminal canonical verdict has no recorded build script")
    candidate_hash = hashlib.sha256((shot.folder / script_rel).read_bytes()).hexdigest()
    settings_hash = hashlib.sha256(
        json.dumps(
            {
                "script": str(script_rel),
                "contract_ids": contract_ids,
                "run_id": (slot.get("rounds") or [{}])[-1].get("run_id"),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    unit_plan = layer_plans.work_unit_plan_path(
        shot.folder,
        unit,
        selected_authority=selected_authority,
    )
    classification = str(declared.get("classification") or "unsatisfiable_in_scope")

    return record_hypothesis_falsification(
        shot.folder,
        str(layer.id),
        unit,
        layer.stages,
        bundle_hash=bundle.content_hash,
        unit_plan_hash=hashlib.sha256(unit_plan.read_bytes()).hexdigest(),
        candidate_hash=candidate_hash,
        settings_hash=settings_hash,
        contract_ids=contract_ids,
        observations=[{
            "contract_ids": contract_ids,
            "reason": reason,
            "classification": classification,
        }],
        decisions=[],
        conflict={
            "kind": "contract",
            "required_authority": conflict_authority(classification),
            "roles": list(unit.mutates.roles),
            "controls": list(unit.mutates.controls),
        },
        evidence=[str(script_rel)],
        affected_seed_ids={unit.id, *declared.get("fault_owner_units", [])},
        attempt=attempt,
        selection_token=(
            selected_authority.selection_token
            if attempt is not None and selected_authority is not None
            else None
        ),
    )


def _record_contract_gap_falsification(
    shot: Shot,
    layer,
    unit,
    *,
    attempt: UnitAttemptClaim | None = None,
    selected_authority: ResolvedSelectedAuthority | None = None,
) -> dict:
    """Promote the latest verified coverage gap into typed replan authority.

    ``contract_gap`` is narrower than a failed contract: it means a measurable observation
    has no authoritative contract binding, so repairing the scene would require authority
    the active unit does not have. Ordinary executable misses never enter this path.
    """

    gaps_path = shot_state_dir(shot.folder) / "contract-gaps.jsonl"
    records = []
    if gaps_path.is_file():
        for line_no, line in enumerate(gaps_path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{gaps_path}:{line_no} is invalid JSON: {exc}") from exc
            if (
                str(row.get("layer")) == str(layer.id)
                and str(row.get("unit")) in {unit.id, "coverage_audit"}
                and row.get("classification") == "plan_defect"
            ):
                records.append(row)
    if not records:
        raise ValueError(
            f"contract gap for {layer.id}.{unit.id} has no hash-pinned gap record; "
            "refusing to invent replanning evidence"
        )
    gap = records[-1]
    observations = list(gap.get("gaps") or [])
    if not observations:
        raise ValueError("latest contract-gap record has no observations")
    bundle = _selected_bundle(shot, selected_authority)
    cited_contracts = sorted({
        str(contract_id)
        for finding in observations
        for contract_id in finding.get("check_ids") or []
        if str(contract_id).strip()
    })
    owner = f"{layer.id}.{unit.id}"
    decisions = [
        {"id": assumption.id, "strength": assumption.decision_strength}
        for assumption in plan_records.load_assumptions(bundle.root)
        if assumption.falsification_owner == owner
        or set(assumption.falsification_contract_ids) & set(cited_contracts)
    ]
    unit_plan = layer_plans.work_unit_plan_path(
        shot.folder,
        unit,
        selected_authority=selected_authority,
    )
    return record_hypothesis_falsification(
        shot.folder,
        str(layer.id),
        unit,
        layer.stages,
        bundle_hash=bundle.content_hash,
        unit_plan_hash=hashlib.sha256(unit_plan.read_bytes()).hexdigest(),
        candidate_hash=str(gap.get("candidate_hash")),
        settings_hash=str(gap.get("settings_hash")),
        contract_ids=cited_contracts,
        observations=observations,
        decisions=decisions,
        conflict={
            "kind": "contract",
            "required_authority": (
                "add an independently measurable claim/contract binding before any scene repair"
            ),
            "roles": list(unit.mutates.roles),
            "controls": list(unit.mutates.controls),
        },
        evidence=["state/contract-gaps.jsonl"],
        attempt=attempt,
        selection_token=(
            selected_authority.selection_token
            if attempt is not None and selected_authority is not None
            else None
        ),
    )


def _record_composed_contract_gap_falsification(
    shot: Shot,
    layer,
    composition_unit,
    *,
    selected_authority: ResolvedSelectedAuthority | None = None,
) -> dict:
    """Publish a replan-consumable finding after all producer units have passed.

    Composition has no script identity of its own.  Bind the finding to the earliest
    exact producer implicated by the critic's role evidence, preserve every accepted
    checkpoint, and name the full role-derived producer/downstream closure for the
    transaction that consumes it.
    """

    if selected_authority is None:
        raise ValueError(
            "composition falsification requires the exact selected authority snapshot"
        )

    gaps_path = shot_state_dir(shot.folder) / "contract-gaps.jsonl"
    records = []
    if gaps_path.is_file():
        for line_no, line in enumerate(
            gaps_path.read_text(encoding="utf-8").splitlines(), 1
        ):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{gaps_path}:{line_no} is invalid JSON: {exc}") from exc
            if (
                str(row.get("layer")) == str(layer.id)
                and row.get("classification") == "plan_defect"
            ):
                records.append(row)
    if not records:
        raise ValueError(
            f"composed contract gap for layer {layer.id} has no hash-pinned gap record"
        )
    gap = records[-1]
    observations = list(gap.get("gaps") or [])
    if not observations:
        raise ValueError("latest composed contract-gap record has no observations")

    observed_roles = {
        str(role)
        for finding in observations
        for role in (finding.get("observation") or {}).get("roles") or []
        if str(role)
    }
    ordered = dependency_ordered_units(layer.stages)
    implicated = [
        unit
        for unit in ordered
        if any(
            fnmatch.fnmatchcase(role, selector)
            for role in observed_roles
            for selector in unit.mutates.roles
        )
    ]
    if not implicated:
        implicated = list(ordered)
    source = implicated[0]
    cited = sorted(
        {
            str(value)
            for finding in observations
            for value in (
                *(finding.get("check_ids") or []),
                str((finding.get("observation") or {}).get("claim_id") or ""),
            )
            if str(value).strip()
        }
    )
    decisions = [
        {"id": str(row["id"]), "strength": str(row["decision_strength"])}
        for row in tuple(getattr(composition_unit, "provisional_decisions", ()) or ())
    ]
    bundle = _selected_bundle(shot, selected_authority)
    unit_plan = layer_plans.work_unit_plan_path(
        shot.folder,
        source,
        selected_authority=selected_authority,
    )
    return record_hypothesis_falsification(
        shot.folder,
        str(layer.id),
        source,
        ordered,
        bundle_hash=bundle.content_hash,
        unit_plan_hash=hashlib.sha256(unit_plan.read_bytes()).hexdigest(),
        candidate_hash=str(gap.get("candidate_hash")),
        settings_hash=str(gap.get("settings_hash")),
        contract_ids=cited,
        observations=observations,
        decisions=decisions,
        conflict={
            "kind": "decision",
            "required_authority": (
                "amend the bounded producer claim/contract graph and transactionally "
                "reopen the role-derived producer closure"
            ),
            "roles": sorted(observed_roles),
            "controls": sorted(
                {
                    control
                    for unit in implicated
                    for control in unit.mutates.controls
                }
            ),
        },
        evidence=["state/contract-gaps.jsonl"],
        affected_seed_ids={unit.id for unit in implicated},
        preserve_accepted_source=True,
        selection_token=selected_authority.selection_token,
    )


def _record_bound_contract_falsification(
    shot: Shot,
    layer,
    unit,
    milestone,
    ledger,
    *,
    attempt: UnitAttemptClaim | None = None,
    selected_authority: ResolvedSelectedAuthority | None = None,
) -> dict | None:
    """Escalate terminal failing contracts that sit on a declared decision falsification path.

    Classification is by declared authority: a decision names the exact contracts that can
    falsify it, so a terminal miss on one of them can only pass by amending that decision.
    A failing contract that no decision names returns None and stays an ordinary failure —
    uncertainty does not gain plan-defect authority.
    """

    slot = ledger._slot(milestone)
    rounds = [row for row in slot.get("rounds") or [] if row.get("kind") == "canonical"]
    if not rounds:
        return None
    final = rounds[-1]
    failing = [
        dict(row)
        for row in final.get("evidence") or []
        if isinstance(row, dict) and row.get("id") and not row.get("pass")
    ]
    if not failing:
        return None
    try:
        bundle = _selected_bundle(shot, selected_authority)
        assumptions = plan_records.load_assumptions(bundle.root)
    except (OSError, ValueError, KeyError) as exc:
        log(f"! falsification classification skipped (unreadable selected authority): {str(exc)[:90]}", 1)
        return None
    decisions = falsifying_decisions((str(row["id"]) for row in failing), assumptions)
    if not decisions:
        return None
    listed = {cid for record in decisions for cid in record.falsification_contract_ids}
    observations = [row for row in failing if str(row["id"]) in listed]
    cited = sorted({str(row["id"]) for row in observations})
    script_rel = slot.get("script")
    if not script_rel:
        raise ValueError("terminal canonical verdict has no recorded build script")
    candidate_hash = hashlib.sha256((shot.folder / script_rel).read_bytes()).hexdigest()
    settings = {
        "frame": int(milestone.frame),
        "ref": str(milestone.ref),
        "script": str(script_rel),
        "round": final.get("round"),
        "attempt": slot.get("attempt"),
        "run_id": final.get("run_id"),
    }
    settings_hash = hashlib.sha256(
        json.dumps(settings, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    unit_plan = layer_plans.work_unit_plan_path(
        shot.folder,
        unit,
        selected_authority=selected_authority,
    )
    evidence = [f"artifact:{script_rel}#sha256={candidate_hash}"]
    render_rel = final.get("render")
    if render_rel and (shot.folder / str(render_rel)).is_file():
        evidence.append(f"render:{render_rel}")
    return record_hypothesis_falsification(
        shot.folder,
        str(layer.id),
        unit,
        layer.stages,
        bundle_hash=bundle.content_hash,
        unit_plan_hash=hashlib.sha256(unit_plan.read_bytes()).hexdigest(),
        candidate_hash=candidate_hash,
        settings_hash=settings_hash,
        contract_ids=cited,
        observations=observations,
        decisions=[
            {"id": record.id, "strength": record.decision_strength} for record in decisions
        ],
        conflict={
            "kind": "decision",
            "required_authority": (
                "amend decision(s) "
                + ", ".join(record.id for record in decisions)
                + " through vfx units replan --falsification; the failing contracts are their "
                "declared falsification path, so passing requires authority outside this unit"
            ),
            "roles": list(unit.mutates.roles),
            "controls": list(unit.mutates.controls),
        },
        evidence=evidence,
        attempt=attempt,
        selection_token=(
            selected_authority.selection_token
            if attempt is not None and selected_authority is not None
            else None
        ),
    )
