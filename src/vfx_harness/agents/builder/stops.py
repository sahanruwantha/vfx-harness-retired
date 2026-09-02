"""Typed public stop authority for executable hypothesis falsification.

The builder is the earliest boundary that can prove a plan hypothesis false
against the cumulative Blender scene.  This adapter does not infer from a
``failed`` or ``contract_gap`` string.  It accepts only the strict, current
``HypothesisFalsification`` transaction already sealed by unit state, pins every
authority/evidence input, and exposes only the amendment transaction that is
legal before replacement authority exists.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from vfx_harness.domain.brief import Shot
from vfx_harness.domain.stop_envelope_primitives import canonical_digest
from vfx_harness.domain.stop_envelopes import (
    StopCause,
    StopEnvelope,
    StopIdentity,
    classify_stop,
)
from vfx_harness.domain.stop_transaction_state import (
    EvidenceRecordAssertion,
    StopEvidenceRef,
)
from vfx_harness.domain.stop_transactions import (
    EscalateQuestionTarget,
    HumanDecisionCommitted,
    PublishValidatedAmendmentTarget,
    SelectedAuthorityAmendmentCommitted,
    StopAction,
)
from vfx_harness.domain.unit_outcomes import (
    HYPOTHESIS_FALSIFICATION_SCHEMA,
    HypothesisFalsification,
)
from vfx_harness.observability.run_artifacts import RunLayout
from vfx_harness.orchestration import (
    authority_capsule_resolution,
    layer_plans,
    unit_state,
)
from vfx_harness.orchestration import ledger as ledger_runtime
from vfx_harness.orchestration.authority_selection import (
    ResolvedSelectedAuthority,
    resolve_selected_authority,
)
from vfx_harness.orchestration.hypothesis_falsification_state import (
    load_state_backed_falsification,
    reconcile_falsification_projection,
)
from vfx_harness.orchestration.unit_state_lock import STATE_DIR

_FINDING_FIELDS = {
    "schema",
    "record_id",
    "recorded_at",
    "layer",
    "unit",
    "identities",
    "contract_ids",
    "observations",
    "decisions",
    "conflict",
    "evidence",
    "affected",
    "fault_owner_units",
}
_IDENTITY_FIELDS = {
    "bundle_hash",
    "plan_hash",
    "unit_hash",
    "unit_plan_hash",
    "candidate_hash",
    "settings_hash",
}
_OWNER_AUTHORITY_ID = "layer-plan-authority"
_AMENDMENT_GATE_POLICY_ID = "structural-authority/runtime-falsification-v1"
_AMENDMENT_GATE_SCHEMA = "vfx-harness.plan-gate/v1"
_HARD_CONSTRAINT_DECISION_AUTHORITY = "human-plan-authority"
_HARD_CONSTRAINT_DECISION_SCHEMA = (
    "vfx-harness.hard-constraint-amendment-decision/v1"
)
_HARD_CONSTRAINT_ANSWER_IDS = (
    "approve-hard-constraint-amendment",
    "reject-hard-constraint-amendment",
)
_STOP_EVIDENCE_SCHEMA = "vfx-harness.builder-authority-stop-evidence/v1"
_STOP_AUDIT_SCHEMA = "vfx-harness.builder-authority-stop-audit/v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json(path: Path, where: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{where} is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{where} must be a JSON object: {path}")
    return value


def _require_current_finding_shape(payload: Mapping[str, Any]) -> None:
    if set(payload) != _FINDING_FIELDS:
        raise ValueError(
            "builder stop requires the current closed hypothesis-falsification shape; "
            f"missing={sorted(_FINDING_FIELDS - set(payload))}; "
            f"unexpected={sorted(set(payload) - _FINDING_FIELDS)}"
        )
    identities = payload.get("identities")
    if not isinstance(identities, Mapping) or set(identities) != _IDENTITY_FIELDS:
        found = set(identities) if isinstance(identities, Mapping) else set()
        raise ValueError(
            "builder stop finding identities are not closed; "
            f"missing={sorted(_IDENTITY_FIELDS - found)}; "
            f"unexpected={sorted(found - _IDENTITY_FIELDS)}"
        )
    decisions = payload.get("decisions")
    if not isinstance(decisions, list) or any(
        not isinstance(row, Mapping) or set(row) != {"id", "strength"}
        for row in decisions
    ):
        raise ValueError("builder stop finding decisions must contain only id and strength")
    conflict = payload.get("conflict")
    if not isinstance(conflict, Mapping) or set(conflict) != {
        "kind",
        "required_authority",
        "roles",
        "controls",
    }:
        raise ValueError("builder stop finding conflict has an unsupported shape")


def _inside(shot_root: Path, relative: str, where: str) -> Path:
    lexical = Path(relative)
    if lexical.is_absolute() or ".." in lexical.parts:
        raise ValueError(f"{where} must be a shot-relative path, found {relative!r}")
    candidate = (shot_root / lexical).resolve()
    try:
        candidate.relative_to(shot_root)
    except ValueError as exc:
        raise ValueError(f"{where} escapes the shot root: {relative!r}") from exc
    if not candidate.is_file():
        raise ValueError(f"{where} is missing: {relative!r}")
    return candidate


def _evidence_rows(shot_root: Path, finding: HypothesisFalsification) -> tuple[dict[str, str], ...]:
    rows = []
    for index, relative in enumerate(finding.evidence):
        path = _inside(shot_root, relative, f"hypothesis falsification evidence[{index}]")
        rows.append({"path": relative, "sha256": _sha256(path)})
    return tuple(rows)


def _finding_identity_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Causal finding content without audit timestamps, ids, or file locators."""
    return {
        "schema": "vfx-harness.builder-falsification-identity/v1",
        "layer": payload["layer"],
        "unit": payload["unit"],
        "identities": dict(payload["identities"]),
        "contract_ids": list(payload["contract_ids"]),
        "observations": list(payload["observations"]),
        "decisions": list(payload["decisions"]),
        "conflict": dict(payload["conflict"]),
        "affected": list(payload["affected"]),
        "fault_owner_units": list(payload["fault_owner_units"]),
    }


def _checkpoint_identity(checkpoint: Any) -> Any:
    if not isinstance(checkpoint, Mapping):
        return checkpoint
    return {key: value for key, value in checkpoint.items() if key != "at"}


def _attempt_identity_digest(
    *,
    authoritative_before_digest: str,
    finding_identity_digest: str,
    evidence_sha256: tuple[str, ...],
    artifact_state_digest: str,
) -> str:
    """Hash attempt content only; run ids and artifact locators are audit metadata."""
    return canonical_digest(
        {
            "schema": "vfx-harness.builder-authority-stop-attempt/v1",
            "authoritative_before_digest": authoritative_before_digest,
            "finding_identity_digest": finding_identity_digest,
            "evidence_sha256": sorted(evidence_sha256),
            "artifact_state_digest": artifact_state_digest,
        }
    )


def _publish_stop_evidence(
    layout: RunLayout,
    document: dict[str, Any],
    *,
    audit: dict[str, Any],
) -> StopEvidenceRef:
    """Publish content identity separately from run-local audit locators."""

    record_digest = canonical_digest(document)
    path = layout.write_report("builder-authority-stop-evidence", document)
    try:
        payload = path.read_bytes()
        observed = json.loads(payload)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("builder authority stop evidence is unreadable") from exc
    if observed != document or canonical_digest(observed) != record_digest:
        raise RuntimeError("builder authority stop evidence failed exact read-back")
    reference = StopEvidenceRef(
        kind="stop_evidence",
        locator=layout.relative(path),
        sha256=hashlib.sha256(payload).hexdigest(),
        record_schema=_STOP_EVIDENCE_SCHEMA,
        record_digest=record_digest,
    )
    audit_path = layout.write_report(
        "builder-authority-stop-audit",
        {
            "schema": _STOP_AUDIT_SCHEMA,
            "run_id": layout.run_id,
            "evidence_ref": reference.as_dict(),
            "audit_locators": audit,
        },
    )
    layout.terminal_metadata.update(
        {
            "builder_authority_stop_evidence": layout.relative(path),
            "builder_authority_stop_evidence_digest": record_digest,
            "builder_authority_stop_audit": layout.relative(audit_path),
        }
    )
    return reference


def _current_unit(
    shot: Shot,
    finding: HypothesisFalsification,
    selected_authority: ResolvedSelectedAuthority,
):
    layers = ledger_runtime.load_layers(
        shot,
        selected_authority=selected_authority,
    )
    layer = layers.get(finding.layer)
    if layer is None:
        raise ValueError(
            f"hypothesis falsification names non-current layer {finding.layer!r}"
        )
    matches = [candidate for candidate in layer.stages if candidate.id == finding.unit]
    if len(matches) != 1:
        raise ValueError(
            "hypothesis falsification must name exactly one current work unit; "
            f"layer={finding.layer!r}, unit={finding.unit!r}, matches={len(matches)}"
        )
    return layer, matches[0]


def compile_hypothesis_falsification_stop(
    shot: Shot,
    layout: RunLayout,
    finding_payload: Mapping[str, Any],
    *,
    stage: str = "builder",
) -> StopEnvelope:
    """Compile one exact current builder finding into an authority-defect stop.

    Generic verdict strings never enter this function.  Every read is repeated after
    the run-owned evidence report is written, so a concurrent bundle, JIT view,
    finding, unit-state, unit-plan, or cited-evidence change fails closed instead of
    publishing a mixed-generation envelope.
    """

    shot_root = shot.folder.resolve()
    if stage not in {"builder", "composition"}:
        raise ValueError("hypothesis falsification stop stage must be builder or composition")
    if layout.shot != shot_root:
        raise ValueError("builder run layout belongs to another shot")
    if not isinstance(finding_payload, Mapping):
        raise ValueError("builder authority stop requires a hypothesis-falsification object")
    raw = dict(finding_payload)
    _require_current_finding_shape(raw)
    finding = HypothesisFalsification.parse(raw, "builder terminal hypothesis falsification")
    state_payload = load_state_backed_falsification(
        shot_root,
        finding.layer,
        finding.record_id,
    )
    if state_payload != raw:
        raise ValueError(
            "builder terminal falsification differs from its authoritative state row"
        )

    selected_authority = resolve_selected_authority(shot_root)
    selected_bundle = selected_authority.assertion.bundle
    selected_view = selected_authority.assertion.effective_view
    if (
        selected_authority.assertion.selection != "selected"
        or selected_authority.plan is None
        or selected_bundle is None
        or selected_view is None
    ):
        raise ValueError(
            "hypothesis falsification does not match the shared selected-authority state"
        )
    if selected_bundle.digest != finding.bundle_hash:
        raise ValueError(
            "hypothesis falsification belongs to superseded bundle authority; "
            f"current={selected_bundle.digest}, finding={finding.bundle_hash}"
        )
    view_digest = selected_view.digest
    # The finding's plan_hash is the selected layer CAPSULE digest that durable unit
    # state records (HIR-0171), never the byte hash of the whole layers.json: comparing
    # the file hash refused the first real falsification after that change and turned
    # a typed plan defect into an unclassified harness_defect stop
    # (run 20260902T190446Z-88aeb3, HIR-0175).
    layer_capsule_digest = authority_capsule_resolution.selected_layer_capsule_digest(
        shot_root,
        finding.layer,
        selected_authority,
    )
    if layer_capsule_digest != finding.plan_hash:
        raise ValueError(
            "hypothesis falsification belongs to a superseded selected layer capsule; "
            f"current={layer_capsule_digest}, finding={finding.plan_hash}"
        )

    layer, current_unit = _current_unit(shot, finding, selected_authority)
    current_unit_digest = unit_state.unit_digest(current_unit)
    if current_unit_digest != finding.unit_hash:
        raise ValueError(
            "hypothesis falsification unit digest is stale; "
            f"current={current_unit_digest}, finding={finding.unit_hash}"
        )
    unit_plan = layer_plans.work_unit_plan_path(shot_root, current_unit)
    if not unit_plan.is_file() or _sha256(unit_plan) != finding.unit_plan_hash:
        raise ValueError("hypothesis falsification unit-plan bytes are no longer current")

    state = unit_state.load(shot_root, finding.layer)
    unit_state.validate_current(state, finding.layer, layer.stages)
    if state.get("plan_hash") != finding.plan_hash:
        raise ValueError("hypothesis falsification work-unit state names another selected layer view")
    slot = (state.get("units") or {}).get(finding.unit)
    if not isinstance(slot, dict) or slot.get("status") not in {
        "hypothesis_falsified",
        "passed",
    }:
        raise ValueError(
            "hypothesis falsification is not the current terminal unit authority; "
            f"status={None if not isinstance(slot, dict) else slot.get('status')!r}"
        )
    if slot.get("unit_hash") != finding.unit_hash or slot.get("falsification") != raw:
        raise ValueError("current unit state does not contain the exact hypothesis falsification")
    recorded = state.get("falsifications")
    if not isinstance(recorded, list) or not recorded or recorded[-1] != raw:
        raise ValueError("hypothesis falsification is not the latest selected finding")

    finding_path = reconcile_falsification_projection(
        shot_root,
        finding.layer,
        finding.record_id,
    )
    artifact_payload = _json(finding_path, "hypothesis falsification artifact")
    if artifact_payload != raw:
        raise ValueError("hypothesis falsification artifact disagrees with current unit state")
    finding_payload_digest = canonical_digest(raw)
    finding_identity = _finding_identity_payload(raw)
    finding_identity_digest = canonical_digest(finding_identity)
    finding_identity_id = f"hf-semantic-{finding_identity_digest[:20]}"
    finding_file_sha256 = _sha256(finding_path)
    evidence_rows = _evidence_rows(shot_root, finding)
    state_path = shot_root / STATE_DIR / f"layer_{finding.layer}.json"
    state_file_sha256 = _sha256(state_path)
    checkpoint_digest = canonical_digest(
        {
            "schema": "vfx-harness.builder-falsification-checkpoint/v1",
            "unit_digest": finding.unit_hash,
            "unit_plan_digest": finding.unit_plan_hash,
            "candidate_digest": finding.candidate_hash,
            "settings_digest": finding.settings_hash,
            "stored_checkpoint": _checkpoint_identity(slot.get("checkpoint")),
        }
    )
    state_identity_digest = canonical_digest(
        {
            "schema": "vfx-harness.builder-falsification-unit-state/v1",
            "layer": finding.layer,
            "plan_hash": state.get("plan_hash"),
            "revision": state.get("revision"),
            "units": {
                str(unit_id): {
                    "status": row.get("status"),
                    "unit_hash": row.get("unit_hash"),
                    "checkpoint": _checkpoint_identity(row.get("checkpoint")),
                }
                for unit_id, row in sorted((state.get("units") or {}).items())
                if isinstance(row, Mapping)
            },
            "finding_identity_digest": finding_identity_digest,
        }
    )
    artifact_state_digest = canonical_digest(
        {
            "schema": "vfx-harness.builder-falsification-artifact-state/v1",
            "layer": finding.layer,
            "unit": finding.unit,
            "unit_status": slot["status"],
            "unit_state_identity_digest": state_identity_digest,
            "finding_identity_digest": finding_identity_digest,
            "checkpoint_digest": checkpoint_digest,
            "affected": list(finding.affected),
            "fault_owner_units": list(finding.fault_owner_units),
        }
    )
    detailed_authoritative_before_digest = canonical_digest(
        {
            "schema": "vfx-harness.builder-authoritative-before/v1",
            "bundle_digest": finding.bundle_hash,
            "view_digest": view_digest,
            "layers_digest": finding.plan_hash,
            "layer": finding.layer,
            "unit": finding.unit,
            "unit_digest": finding.unit_hash,
            "unit_plan_digest": finding.unit_plan_hash,
            "candidate_digest": finding.candidate_hash,
            "checkpoint_digest": checkpoint_digest,
            "settings_digest": finding.settings_hash,
            "finding_identity_digest": finding_identity_digest,
            "unit_state_identity_digest": state_identity_digest,
        }
    )
    authoritative_before_digest = (
        detailed_authoritative_before_digest
        if finding.changes_hard_constraint
        else selected_authority.assertion.digest
    )
    evidence_sha256 = tuple(row["sha256"] for row in evidence_rows)
    attempt_evidence_digest = _attempt_identity_digest(
        authoritative_before_digest=authoritative_before_digest,
        finding_identity_digest=finding_identity_digest,
        evidence_sha256=evidence_sha256,
        artifact_state_digest=artifact_state_digest,
    )
    normalized_facts_digest = canonical_digest(
        {
            "schema": "vfx-harness.builder-authority-stop-cause/v1",
            "invariant_id": "executable_hypothesis_requires_authority_change",
            "layer": finding.layer,
            "unit": finding.unit,
            "contract_ids": sorted(finding.contract_ids),
            "decisions": sorted(
                ({"id": row.id, "strength": row.strength} for row in finding.decisions),
                key=lambda row: row["id"],
            ),
            "conflict": {
                "kind": finding.conflict.kind,
                "required_authority": finding.conflict.required_authority,
                "roles": sorted(finding.conflict.roles),
                "controls": sorted(finding.conflict.controls),
            },
            "affected": sorted(finding.affected),
            "fault_owner_units": sorted(finding.fault_owner_units),
        }
    )
    requires_human_decision = finding.changes_hard_constraint
    required_transition = (
        "escalate_question"
        if requires_human_decision
        else "publish_validated_amendment"
    )
    classification_evidence_digest = canonical_digest(
        {
            "schema": "vfx-harness.builder-authority-stop-classification/v1",
            "stage": stage,
            "finding_identity_digest": finding_identity_digest,
            "attempt_evidence_digest": attempt_evidence_digest,
            "conflict_kind": finding.conflict.kind,
            "required_transition": required_transition,
        }
    )
    question_payload = {
        "schema": "vfx-harness.hard-constraint-amendment-question/v1",
        "finding_identity_digest": finding_identity_digest,
        "layer_id": finding.layer,
        "unit_id": finding.unit,
        "decision_authority_id": _HARD_CONSTRAINT_DECISION_AUTHORITY,
        "decision_schema": _HARD_CONSTRAINT_DECISION_SCHEMA,
        "allowed_answer_ids": list(_HARD_CONSTRAINT_ANSWER_IDS),
    }
    question_digest = canonical_digest(question_payload)
    question_id = f"hard-constraint-question-{question_digest[:20]}"
    evidence_record = _publish_stop_evidence(
        layout,
        {
            "schema": _STOP_EVIDENCE_SCHEMA,
            "evidence_kind": (
                "human_decision_required"
                if requires_human_decision
                else "authority_defect"
            ),
            "authority": {
                "bundle_digest": finding.bundle_hash,
                "view_digest": view_digest,
                "layers_digest": finding.plan_hash,
                "layer_id": finding.layer,
                "unit_id": finding.unit,
                "unit_digest": finding.unit_hash,
                "unit_plan_digest": finding.unit_plan_hash,
                "candidate_digest": finding.candidate_hash,
                "checkpoint_digest": checkpoint_digest,
                "settings_digest": finding.settings_hash,
                "authoritative_before_digest": authoritative_before_digest,
                "selected_authority": selected_authority.assertion.as_dict(),
                "detailed_authoritative_before_digest": (
                    detailed_authoritative_before_digest
                ),
            },
            "finding_record": {
                "schema": finding_identity["schema"],
                "record_id": finding_identity_id,
                "record_digest": finding_identity_digest,
                "payload": finding_identity,
            },
            "source_finding_identity": {
                "wire_schema": HYPOTHESIS_FALSIFICATION_SCHEMA,
                "semantic_schema": finding_identity["schema"],
                "semantic_digest": finding_identity_digest,
            },
            "evidence_sha256": sorted(evidence_sha256),
            "attempt_evidence_digest": attempt_evidence_digest,
            "classification_evidence_digest": classification_evidence_digest,
            "artifact_state_digest": artifact_state_digest,
            **(
                {
                    "decision_question": {
                        "record_id": question_id,
                        "question_digest": question_digest,
                        "payload": question_payload,
                    }
                }
                if requires_human_decision
                else {}
            ),
        },
        audit={
            "source_finding": {
                "schema": HYPOTHESIS_FALSIFICATION_SCHEMA,
                "record_id": finding.record_id,
                "record_digest": finding_payload_digest,
                "artifact": finding_path.relative_to(shot_root).as_posix(),
                "artifact_sha256": finding_file_sha256,
                "payload": raw,
            },
            "evidence": list(evidence_rows),
            "unit_state": state_path.relative_to(shot_root).as_posix(),
            "unit_state_file_sha256": state_file_sha256,
            "unit_plan": unit_plan.relative_to(shot_root).as_posix(),
        },
    )
    finding_assertion = EvidenceRecordAssertion(
        record_kind="finding",
        record_id=finding_identity_id,
        evidence=evidence_record,
    )
    if requires_human_decision:
        question_assertion = EvidenceRecordAssertion(
            record_kind="question",
            record_id=question_id,
            evidence=evidence_record,
        )
        action = StopAction(
            target=EscalateQuestionTarget(
                question_record=question_assertion,
                question_digest=question_digest,
                decision_authority_id=_HARD_CONSTRAINT_DECISION_AUTHORITY,
                decision_schema=_HARD_CONSTRAINT_DECISION_SCHEMA,
                allowed_answer_ids=_HARD_CONSTRAINT_ANSWER_IDS,
                evidence=(evidence_record,),
            ),
            postcondition=HumanDecisionCommitted(
                question_digest=question_digest,
                decision_authority_id=_HARD_CONSTRAINT_DECISION_AUTHORITY,
                decision_schema=_HARD_CONSTRAINT_DECISION_SCHEMA,
                allowed_answer_ids=_HARD_CONSTRAINT_ANSWER_IDS,
            ),
        )
    else:
        action = StopAction(
            target=PublishValidatedAmendmentTarget(
                scope="layer_view",
                base_authority=selected_authority.assertion,
                layer_id=finding.layer,
                findings=(finding_assertion,),
                owner_authority_id=_OWNER_AUTHORITY_ID,
                gate_policy_id=_AMENDMENT_GATE_POLICY_ID,
                gate_schema=_AMENDMENT_GATE_SCHEMA,
                validation_scope="structural_authority",
            ),
            postcondition=SelectedAuthorityAmendmentCommitted(
                scope="layer_view",
                base_authority_digest=selected_authority.assertion.digest,
                layer_id=finding.layer,
                finding_ids=(finding_identity_id,),
                gate_policy_id=_AMENDMENT_GATE_POLICY_ID,
                gate_schema=_AMENDMENT_GATE_SCHEMA,
                validation_scope="structural_authority",
                owner_authority_id=_OWNER_AUTHORITY_ID,
                required_after_source="jit",
            ),
        )
    # Re-read every mutable input before granting even amendment authority.
    state_after = unit_state.load(shot_root, finding.layer)
    selected_authority_after = resolve_selected_authority(shot_root)
    if (
        selected_authority_after.selection_token
        != selected_authority.selection_token
        or selected_authority_after.assertion != selected_authority.assertion
        or authority_capsule_resolution.selected_layer_capsule_digest(
            shot_root, finding.layer, selected_authority_after
        )
        != finding.plan_hash
        or _sha256(unit_plan) != finding.unit_plan_hash
        or _sha256(state_path) != state_file_sha256
        or _sha256(finding_path) != finding_file_sha256
        or state_after != state
        or _evidence_rows(shot_root, finding) != evidence_rows
    ):
        raise ValueError("builder authority changed while its typed stop was compiled")

    owner_scope_ids = (
        f"{finding.layer}:{finding.unit}",
        *(f"fault:{owner}" for owner in finding.fault_owner_units),
    )
    candidate = StopEnvelope(
        stage=stage,
        stop_class=(
            "human_decision_required"
            if requires_human_decision
            else "authority_defect"
        ),
        identity=StopIdentity(
            run_id=layout.run_id,
            bundle_digest=finding.bundle_hash,
            view_digest=view_digest,
            layer_id=finding.layer,
            unit_id=finding.unit,
            unit_plan_digest=finding.unit_plan_hash,
            unit_digest=finding.unit_hash,
            candidate_digest=finding.candidate_hash,
            checkpoint_digest=checkpoint_digest,
            settings_digest=finding.settings_hash,
            debt_state_digest=None,
        ),
        cause=StopCause(
            invariant_id=(
                "hard_constraint_amendment_requires_human_decision"
                if requires_human_decision
                else "executable_hypothesis_requires_authority_change"
            ),
            finding_ids=(f"hf-cause-{finding_identity_digest[:20]}",),
            owner_scope_ids=owner_scope_ids,
            normalized_facts_digest=normalized_facts_digest,
        ),
        attempt_evidence_digest=attempt_evidence_digest,
        classification_evidence_digest=classification_evidence_digest,
        artifact_state_digest=artifact_state_digest,
        authoritative_before_digest=authoritative_before_digest,
        actions=(action,),
        evidence_refs=(evidence_record,),
        budget_key=(
            "builder-hard-constraint-decision"
            if requires_human_decision
            else "builder-authority-amendment"
        ),
        expected=(
            "The selected unit authority can express and satisfy its declared executable "
            "contracts without changing plan authority."
        ),
        found=(
            f"Executable finding {finding.record_id} proves a {finding.conflict.kind} "
            f"authority conflict for {finding.layer}.{finding.unit}: "
            f"{finding.conflict.required_authority}."
        ),
        next_action=(
            "Obtain the named human decision before changing the hard constraint."
            if requires_human_decision
            else (
                "Publish a validated amendment that consumes this exact finding. Only after "
                "the selected bundle or view changes may a revision-checked replan become legal."
            )
        ),
    )
    classified = classify_stop(candidate)
    if not isinstance(classified, StopEnvelope):
        raise ValueError("terminal hypothesis falsification was classified as continuation")
    return classified
