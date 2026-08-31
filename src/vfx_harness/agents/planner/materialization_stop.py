"""Typed stop authority for an unaccepted JIT materialization transaction.

Classification is derived from the exact selected bundle, current candidate bytes,
deterministic local validation, and (when local validation passes) the persisted typed
terminal gate result. Model-session termination, exception strings, and exit codes are
not classification inputs.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import vfx_harness.agents.planner.materialization_stop_evidence as stop_evidence
import vfx_harness.orchestration.jit_materialization.gate_evidence as gate_evidence
import vfx_harness.orchestration.jit_materialization.publish as jit_publish
from vfx_harness.domain.stop_envelope_primitives import canonical_digest
from vfx_harness.domain.stop_envelopes import StopCause, StopEnvelope, StopIdentity
from vfx_harness.domain.stop_transaction_state import (
    EvidenceRecordAssertion,
    StopEvidenceRef,
)
from vfx_harness.domain.stop_transactions import (
    EngineeringRouteCommitted,
    PublishValidatedAmendmentTarget,
    RouteEngineeringTarget,
    SelectedAuthorityAmendmentCommitted,
    StopAction,
)
from vfx_harness.orchestration import plan_authority
from vfx_harness.orchestration.authority_selection import (
    ResolvedSelectedAuthority,
    SelectedAuthorityResolutionError,
    resolve_selected_authority,
)
from vfx_harness.orchestration.jit_materialization.schema import materialization_finalization_path
from vfx_harness.orchestration.jit_materialization.staging import inspect_materialization

if TYPE_CHECKING:
    from vfx_harness.observability.run_artifacts import RunLayout
    from vfx_harness.orchestration.plan_authority import PlanBundle


_STOP_EVIDENCE_SCHEMA = "vfx-harness.materialization-stop-evidence/v1"
_STOP_AUDIT_SCHEMA = "vfx-harness.materialization-stop-audit/v1"
_ENGINEERING_SINK = "engineering_handoff"
_MATERIALIZATION_AUTHORITY = "jit-materialization-authority"
_MATERIALIZATION_GATE_POLICY = "structural-authority/runtime-falsification-v1"
_MATERIALIZATION_GATE_SCHEMA = "vfx-harness.plan-gate/v1"


def _publish_stop_evidence(
    layout: RunLayout,
    document: dict[str, Any],
    *,
    audit: dict[str, Any],
) -> StopEvidenceRef:
    """Publish the typed causal record before constructing its envelope.

    The cited record contains content identities only.  Run ids and locators live in
    the uncited audit sidecar, so moving identical evidence between run-local paths
    does not change its record identity.  No action or envelope digest is embedded in
    either record, avoiding a report/envelope digest cycle.
    """

    record_digest = canonical_digest(document)
    published = {**document, "record_digest": record_digest}
    path = layout.write_report("materialization-stop-evidence", published)
    try:
        observed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("materialization stop evidence is unreadable") from exc
    if not isinstance(observed, dict) or observed != published:
        raise RuntimeError("materialization stop evidence failed exact read-back")
    unsigned = {key: value for key, value in observed.items() if key != "record_digest"}
    if canonical_digest(unsigned) != record_digest:
        raise RuntimeError("materialization stop evidence record digest is stale")
    payload = path.read_bytes()
    reference = StopEvidenceRef(
        kind="stop_evidence",
        locator=layout.relative(path),
        sha256=hashlib.sha256(payload).hexdigest(),
        record_schema=_STOP_EVIDENCE_SCHEMA,
        record_digest=record_digest,
    )
    layout.write_report(
        "materialization-stop-audit",
        {
            "schema": _STOP_AUDIT_SCHEMA,
            "run_id": layout.run_id,
            "evidence": reference.as_dict(),
            "audit_locators": audit,
        },
    )
    layout.terminal_metadata.update(
        {
            "materialization_stop_evidence": "reports/materialization-stop-evidence.json",
            "materialization_stop_evidence_digest": record_digest,
            "materialization_stop_audit": "reports/materialization-stop-audit.json",
        }
    )
    return reference


def _harness_defect(
    layout: RunLayout,
    *,
    bundle_digest: str,
    view_digest: str | None,
    layer_id: str,
    candidate_record: dict[str, Any],
    authority_before: dict[str, Any],
    authoritative_before_digest: str,
    artifact_state: dict[str, Any],
    issues: tuple[str, ...],
    audit: dict[str, Any],
) -> StopEnvelope:
    unique = tuple(sorted(set(issues))) or ("typed_materialization_evidence_unavailable",)
    normalized = {
        "schema": "vfx-harness.materialization-evidence-defect/v1",
        "issues": list(unique),
    }
    classification = {
        "schema": "vfx-harness.materialization-stop-classification/v1",
        "stop_class": "harness_defect",
        "issues": list(unique),
    }
    attempt = {
        "schema": "vfx-harness.materialization-stop-attempt/v1",
        "bundle_digest": bundle_digest,
        "view_digest": view_digest,
        "layer_id": layer_id,
        "candidate": stop_evidence.content_record(candidate_record),
        "artifact_state": artifact_state,
        "authoritative_before_digest": authoritative_before_digest,
    }
    cause = StopCause(
        invariant_id="materialization_requires_typed_evidence",
        finding_ids=tuple(f"materialization-evidence:{issue}" for issue in unique),
        owner_scope_ids=("jit-materialization",),
        normalized_facts_digest=canonical_digest(normalized),
    )
    attempt_digest = canonical_digest(attempt)
    classification_digest = canonical_digest(classification)
    artifact_digest = canonical_digest(artifact_state)
    evidence = _publish_stop_evidence(
        layout,
        {
            "schema": _STOP_EVIDENCE_SCHEMA,
            "evidence_kind": "harness_defect",
            "bundle_digest": bundle_digest,
            "view_digest": view_digest,
            "layer_id": layer_id,
            "candidate": stop_evidence.content_record(candidate_record),
            "authoritative_before": authority_before,
            "artifact_state": artifact_state,
            "classification": classification,
            "issues": list(unique),
            "attempt_evidence_digest": attempt_digest,
            "classification_evidence_digest": classification_digest,
            "artifact_state_digest": artifact_digest,
            "authoritative_before_digest": authoritative_before_digest,
        },
        audit={**audit, "candidate_record": candidate_record},
    )
    defect_record = EvidenceRecordAssertion(
        record_kind="defect",
        record_id="materialization-defect:" + canonical_digest(normalized),
        evidence=evidence,
    )
    target = RouteEngineeringTarget(
        cause_fingerprint=cause.fingerprint_for("harness_defect"),
        attempt_evidence_digest=attempt_digest,
        owner_scope_ids=cause.owner_scope_ids,
        defect_record=defect_record,
        evidence=(evidence,),
        sink_id=_ENGINEERING_SINK,
    )
    action = StopAction(
        target=target,
        postcondition=EngineeringRouteCommitted(
            defect_packet_digest=evidence.record_digest,
            sink_id=target.sink_id,
            owner_scope_ids=target.owner_scope_ids,
        ),
    )
    return StopEnvelope(
        stage="materialization",
        stop_class="harness_defect",
        identity=StopIdentity(
            run_id=layout.run_id,
            bundle_digest=bundle_digest,
            view_digest=view_digest,
            layer_id=layer_id,
            unit_id=None,
            unit_plan_digest=None,
            unit_digest=None,
            candidate_digest=candidate_record.get("sha256"),
            checkpoint_digest=None,
            settings_digest=None,
            debt_state_digest=None,
        ),
        cause=cause,
        attempt_evidence_digest=attempt_digest,
        classification_evidence_digest=classification_digest,
        artifact_state_digest=artifact_digest,
        authoritative_before_digest=authoritative_before_digest,
        actions=(action,),
        evidence_refs=(evidence,),
        budget_key="jit-materialization-evidence",
        expected=(
            "A JIT materialization stop must carry complete candidate and terminal "
            "gate evidence."
        ),
        found="The materialization evidence is missing, malformed, or inconsistent: "
        + ", ".join(unique)
        + ".",
        next_action="Route the exact content identities and evidence defects to engineering.",
    )


def _authority_defect(
    layout: RunLayout,
    *,
    selected_authority: ResolvedSelectedAuthority,
    bundle_digest: str,
    view_digest: str,
    layer_id: str,
    candidate_record: dict[str, Any],
    authority_before: dict[str, Any],
    authoritative_before_digest: str,
    artifact_state: dict[str, Any],
    finding_records: tuple[dict[str, Any], ...],
    source: str,
    audit: dict[str, Any],
) -> StopEnvelope:
    causal_findings = tuple(
        {
            "finding_id": row["finding_id"],
            "causal_fact": row["causal_fact"],
        }
        for row in finding_records
    )
    normalized = {
        "schema": "vfx-harness.materialization-structural-blockers/v1",
        "source": source,
        "findings": [row["causal_fact"] for row in causal_findings],
    }
    classification = {
        "schema": "vfx-harness.materialization-stop-classification/v1",
        "stop_class": "authority_defect",
        "validation_scope": "structural_authority",
        "source": source,
        "blocking_findings": list(causal_findings),
    }
    attempt = {
        "schema": "vfx-harness.materialization-stop-attempt/v1",
        "bundle_digest": bundle_digest,
        "view_digest": view_digest,
        "layer_id": layer_id,
        "candidate_sha256": candidate_record["sha256"],
        "artifact_state": artifact_state,
        "authoritative_before_digest": authoritative_before_digest,
        "finding_fact_digests": [
            canonical_digest(row["causal_fact"]) for row in causal_findings
        ],
    }
    cause = StopCause(
        invariant_id="materialization_structural_authority_blocked",
        finding_ids=tuple(row["finding_id"] for row in causal_findings),
        owner_scope_ids=(f"layer:{layer_id}",),
        normalized_facts_digest=canonical_digest(normalized),
    )
    attempt_digest = canonical_digest(attempt)
    classification_digest = canonical_digest(classification)
    artifact_digest = canonical_digest(artifact_state)
    evidence = _publish_stop_evidence(
        layout,
        {
            "schema": _STOP_EVIDENCE_SCHEMA,
            "evidence_kind": "authority_defect",
            "bundle_digest": bundle_digest,
            "view_digest": view_digest,
            "layer_id": layer_id,
            "candidate": stop_evidence.content_record(candidate_record),
            "authoritative_before": authority_before,
            "artifact_state": artifact_state,
            "classification": classification,
            "blocking_findings": list(causal_findings),
            "attempt_evidence_digest": attempt_digest,
            "classification_evidence_digest": classification_digest,
            "artifact_state_digest": artifact_digest,
            "authoritative_before_digest": authoritative_before_digest,
        },
        audit={
            **audit,
            "candidate_record": candidate_record,
            "blocking_findings": list(finding_records),
        },
    )
    findings = tuple(
        EvidenceRecordAssertion(
            record_kind="finding",
            record_id=row["finding_id"],
            evidence=evidence,
        )
        for row in causal_findings
    )
    target = PublishValidatedAmendmentTarget(
        scope="layer_view",
        base_authority=selected_authority.assertion,
        layer_id=layer_id,
        findings=findings,
        owner_authority_id=_MATERIALIZATION_AUTHORITY,
        gate_policy_id=_MATERIALIZATION_GATE_POLICY,
        gate_schema=_MATERIALIZATION_GATE_SCHEMA,
        validation_scope="structural_authority",
    )
    action = StopAction(
        target=target,
        postcondition=SelectedAuthorityAmendmentCommitted(
            scope=target.scope,
            base_authority_digest=selected_authority.assertion.digest,
            layer_id=layer_id,
            finding_ids=tuple(row.record_id for row in target.findings),
            gate_policy_id=_MATERIALIZATION_GATE_POLICY,
            gate_schema=_MATERIALIZATION_GATE_SCHEMA,
            validation_scope="structural_authority",
            owner_authority_id=_MATERIALIZATION_AUTHORITY,
            required_after_source="jit",
        ),
    )
    if resolve_selected_authority(layout.shot) != selected_authority:
        raise RuntimeError(
            "selected authority changed while its materialization stop was compiled"
        )
    return StopEnvelope(
        stage="materialization",
        stop_class="authority_defect",
        identity=StopIdentity(
            run_id=layout.run_id,
            bundle_digest=bundle_digest,
            view_digest=view_digest,
            layer_id=layer_id,
            unit_id=None,
            unit_plan_digest=None,
            unit_digest=None,
            candidate_digest=candidate_record["sha256"],
            checkpoint_digest=None,
            settings_digest=None,
            debt_state_digest=None,
        ),
        cause=cause,
        attempt_evidence_digest=attempt_digest,
        classification_evidence_digest=classification_digest,
        artifact_state_digest=artifact_digest,
        authoritative_before_digest=authoritative_before_digest,
        actions=(action,),
        evidence_refs=(evidence,),
        budget_key=f"jit-materialization:{layer_id}",
        expected=(
            "The candidate must pass local materialization validation and the terminal "
            "structural gate."
        ),
        found=(
            f"The exact candidate has {len(finding_records)} blocking structural finding(s)."
        ),
        next_action=(
            "Publish a new validated candidate revision for this exact layer; this stop "
            "does not authorize force, gate skipping, or a global replan."
        ),
    )


def publish_materialization_stop(
    layout: RunLayout,
    *,
    bundle: PlanBundle,
    layer_id: str,
    candidate: str | Path,
    overlay_root: str | Path | None = None,
) -> StopEnvelope:
    """Compile and persist the only legal stop for the current candidate bytes."""

    layer_id = str(layer_id)
    candidate_path = Path(candidate).expanduser().resolve()
    candidate_record, candidate_bytes, _document, candidate_issues = (
        stop_evidence.candidate_state(
            layout,
            candidate_path,
            bundle_digest=bundle.content_hash,
            layer_id=layer_id,
        )
    )
    overlay_path = Path(overlay_root).resolve() if overlay_root is not None else None
    (
        authority_before,
        before_digest,
        view_digest,
        authority_issues,
        authority_audit,
        attempt_inputs,
    ) = stop_evidence.authority_before(
        layout,
        bundle,
        layer_id=layer_id,
        overlay_root=overlay_path,
    )
    selected_authority: ResolvedSelectedAuthority | None = None
    try:
        selected_authority = resolve_selected_authority(layout.shot)
    except SelectedAuthorityResolutionError:
        authority_issues = (*authority_issues, "selected_authority_resolution_failed")
    else:
        selected_bundle = selected_authority.assertion.bundle
        selected_view = selected_authority.assertion.effective_view
        if (
            selected_authority.assertion.selection != "selected"
            or selected_bundle is None
            or selected_view is None
            or selected_bundle.digest != bundle.content_hash
            or (view_digest is not None and selected_view.digest != view_digest)
            or (view_digest is None and selected_view.source == "jit")
        ):
            authority_issues = (*authority_issues, "selected_authority_snapshot_disagrees")
        else:
            view_digest = selected_view.digest
            authority_before = {
                **authority_before,
                "selected_authority": selected_authority.assertion.as_dict(),
            }
    finalization, finalization_current, finalization_issues = (
        stop_evidence.current_finalization(
            candidate_path,
            bundle_digest=bundle.content_hash,
            candidate_digest=candidate_record.get("sha256"),
        )
    )
    artifact_state: dict[str, Any] = {
        "schema": "vfx-harness.materialization-stop-artifact-state/v1",
        "candidate": stop_evidence.content_record(candidate_record),
        "finalization": finalization,
        **attempt_inputs,
    }
    audit = {
        "candidate": candidate_record.get("locator"),
        "gate_evidence": f"reports/{gate_evidence.MATERIALIZATION_GATE_REPORT}.json",
        "finalization": materialization_finalization_path(candidate_path).name,
        "authority_before": authority_audit,
    }
    issues = tuple(sorted({*candidate_issues, *authority_issues, *finalization_issues}))
    if issues or candidate_bytes is None:
        return _harness_defect(
            layout,
            bundle_digest=bundle.content_hash,
            view_digest=view_digest,
            layer_id=layer_id,
            candidate_record=candidate_record,
            authority_before=authority_before,
            authoritative_before_digest=before_digest,
            artifact_state=artifact_state,
            issues=issues or ("candidate_unavailable",),
            audit=audit,
        )

    try:
        base_layers = jit_publish.selected_view_artifact(
            layout.shot,
            "layers.json",
            bundle.content_hash,
            overlay_root=overlay_path,
        ) or plan_authority.artifact_path(layout.shot, "layers.json")
        base_scene = jit_publish.selected_view_artifact(
            layout.shot,
            "scene_checks.json",
            bundle.content_hash,
            overlay_root=overlay_path,
        ) or plan_authority.artifact_path(layout.shot, "scene_checks.json")
        base_requirements = jit_publish.selected_view_artifact(
            layout.shot,
            "requirements.json",
            bundle.content_hash,
            overlay_root=overlay_path,
        ) or plan_authority.artifact_path(layout.shot, "requirements.json")
        local_findings, _materialized = inspect_materialization(
            bundle.root,
            candidate_path,
            expected_bundle_hash=bundle.content_hash,
            base_layers_path=base_layers,
            base_scene_checks_path=base_scene,
            resolutions_path=layout.shot / "state" / "plan-resolutions.jsonl",
            base_requirements_path=base_requirements,
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return _harness_defect(
            layout,
            bundle_digest=bundle.content_hash,
            view_digest=view_digest,
            layer_id=layer_id,
            candidate_record=candidate_record,
            authority_before=authority_before,
            authoritative_before_digest=before_digest,
            artifact_state=artifact_state,
            issues=("deterministic_materialization_validation_unavailable",),
            audit=audit,
        )

    if local_findings:
        assert selected_authority is not None
        finding_records = tuple(
            gate_evidence.materialization_local_finding_records(local_findings)
        )
        artifact_state["local_finding_fact_digests"] = [
            canonical_digest(row["causal_fact"]) for row in finding_records
        ]
        return _authority_defect(
            layout,
            selected_authority=selected_authority,
            bundle_digest=bundle.content_hash,
            view_digest=view_digest,
            layer_id=layer_id,
            candidate_record=candidate_record,
            authority_before=authority_before,
            authoritative_before_digest=selected_authority.assertion.digest,
            artifact_state=artifact_state,
            finding_records=finding_records,
            source="candidate_validation",
            audit=audit,
        )

    gate_state, gate_audit, blocking, gate_clean, gate_issues = (
        stop_evidence.gate_evidence_state(
            layout,
            candidate_record=candidate_record,
            bundle_digest=bundle.content_hash,
            layer_id=layer_id,
            current_finalization=finalization,
        )
    )
    audit["gate_evidence_record"] = gate_audit
    artifact_state["gate_evidence"] = gate_state
    artifact_state["gate_clean"] = gate_clean
    if gate_issues:
        return _harness_defect(
            layout,
            bundle_digest=bundle.content_hash,
            view_digest=view_digest,
            layer_id=layer_id,
            candidate_record=candidate_record,
            authority_before=authority_before,
            authoritative_before_digest=before_digest,
            artifact_state=artifact_state,
            issues=gate_issues,
            audit=audit,
        )
    if blocking:
        if finalization_current:
            return _harness_defect(
                layout,
                bundle_digest=bundle.content_hash,
                view_digest=view_digest,
                layer_id=layer_id,
                candidate_record=candidate_record,
                authority_before=authority_before,
                authoritative_before_digest=before_digest,
                artifact_state=artifact_state,
                issues=("blocking_gate_has_current_finalization",),
                audit=audit,
            )
        findings = stop_evidence.gate_finding_records(blocking)
        artifact_state["gate_blocking_fact_digests"] = [
            canonical_digest(row["causal_fact"]) for row in findings
        ]
        assert selected_authority is not None
        return _authority_defect(
            layout,
            selected_authority=selected_authority,
            bundle_digest=bundle.content_hash,
            view_digest=view_digest,
            layer_id=layer_id,
            candidate_record=candidate_record,
            authority_before=authority_before,
            authoritative_before_digest=selected_authority.assertion.digest,
            artifact_state=artifact_state,
            finding_records=findings,
            source="terminal_gate",
            audit=audit,
        )

    # A clean gate is not a terminal rejection. If the transaction nevertheless
    # reaches this compiler, either the required exact attestation is absent or the
    # attested publication failed. Neither case authorizes another planning cycle.
    issue = (
        "attested_candidate_failed_to_publish"
        if finalization_current
        else "clean_gate_without_current_finalization"
    )
    return _harness_defect(
        layout,
        bundle_digest=bundle.content_hash,
        view_digest=view_digest,
        layer_id=layer_id,
        candidate_record=candidate_record,
        authority_before=authority_before,
        authoritative_before_digest=before_digest,
        artifact_state=artifact_state,
        issues=(issue,),
        audit=audit,
    )
