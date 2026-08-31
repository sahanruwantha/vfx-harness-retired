"""Typed terminal authority for a rejected global planning candidate.

The deterministic plan gate owns this classification.  Exit codes, loop outcomes,
and model-session prose are diagnostics only: a plan repair becomes legal solely
from a complete, internally consistent gate report over the exact candidate bytes.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vfx_harness.domain.stop_envelope_primitives import canonical_digest
from vfx_harness.domain.stop_envelopes import StopCause, StopEnvelope, StopIdentity
from vfx_harness.domain.stop_transaction_state import (
    EvidenceRecordAssertion,
    SelectedBundleAssertion,
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

if TYPE_CHECKING:
    from vfx_harness.agents.planner.types import PlanLoopResult
    from vfx_harness.observability.run_artifacts import RunLayout


_GATE_SCHEMA = "vfx-harness.plan-gate/v1"
_GATE_POLICY = "structural-authority/runtime-falsification-v1"
_EVIDENCE_SCHEMA = "vfx-harness.plan-stop-evidence/v1"
_AUDIT_SCHEMA = "vfx-harness.plan-stop-audit/v1"
_REPORT_FIELDS = {
    "schema",
    "policy",
    "validation_scope",
    "runtime_contracts_confirmed",
    "shot",
    "generated_at",
    "clean",
    "outcome",
    "blocking_count",
    "warning_count",
    "stats",
    "signature",
    "findings",
}
_FINDING_FIELDS = {"check", "severity", "where", "what"}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_record(path: Path, *, locator: str) -> tuple[dict[str, Any], bytes | None]:
    try:
        payload = path.read_bytes()
    except FileNotFoundError:
        return {"locator": locator, "state": "missing"}, None
    except OSError:
        return {"locator": locator, "state": "unreadable"}, None
    return {
        "locator": locator,
        "state": "present",
        "sha256": _sha256(payload),
        "bytes": len(payload),
    }, payload


def _content_record(record: dict[str, Any]) -> dict[str, Any]:
    """Return exact file content identity without its regenerable locator."""

    return {key: record[key] for key in ("state", "sha256", "bytes") if key in record}


def _without_audit_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_audit_fields(item)
            for key, item in value.items()
            if key not in {"fix", "generated_at", "locator", "run_id", "shot"}
        }
    if isinstance(value, list):
        return [_without_audit_fields(item) for item in value]
    return value


def _causal_gate_state(
    report: dict[str, Any] | None,
    report_record: dict[str, Any],
) -> dict[str, Any]:
    if report is None:
        return {
            "schema": "vfx-harness.plan-gate-causal-state/v1",
            "unparsed_report": _content_record(report_record),
        }
    causal = _without_audit_fields(report)
    if not isinstance(causal, dict):
        raise ValueError("normalized plan-gate state must remain an object")
    causal.pop("signature", None)
    causal["signature"] = canonical_digest(
        {
            "schema": "vfx-harness.plan-gate-causal-signature/v1",
            "policy": causal.get("policy"),
            "findings": causal.get("findings"),
        }
    )
    return causal


def _candidate_record(layout: RunLayout, candidate: object) -> tuple[dict[str, Any], bytes | None, str | None]:
    if not isinstance(candidate, Path):
        return {"locator": "candidate", "state": "invalid_path"}, None, "candidate_path_invalid"
    path = candidate.expanduser().resolve()
    try:
        locator = path.relative_to(layout.shot).as_posix()
    except ValueError:
        return {"locator": "candidate", "state": "outside_shot"}, None, "candidate_outside_shot"
    record, payload = _file_record(path, locator=locator)
    issue = None if payload is not None else f"candidate_{record['state']}"
    return record, payload, issue


def _report_record(layout: RunLayout) -> tuple[dict[str, Any], bytes | None, str | None]:
    path = layout.reports / "plan_gate.json"
    record, payload = _file_record(
        path,
        locator=path.relative_to(layout.shot).as_posix(),
    )
    issue = None if payload is not None else f"gate_report_{record['state']}"
    return record, payload, issue


def _authority_before(layout: RunLayout) -> tuple[dict[str, Any], str, str | None]:
    """Return exact selected-before state without treating an invalid pointer as authority."""
    pointer = layout.shot / plan_authority.POINTER
    pointer_record, pointer_bytes = _file_record(
        pointer,
        locator=plan_authority.POINTER.as_posix(),
    )
    if pointer_bytes is None:
        selection = "absent" if pointer_record["state"] == "missing" else "unreadable"
        state = {
            "schema": "vfx-harness.plan-authority-before/v1",
            "selection": selection,
            "pointer": pointer_record,
            "bundle_digest": None,
            "bundle_manifest_sha256": None,
        }
        return state, canonical_digest(state), None

    try:
        pointer_value = json.loads(pointer_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError):
        state = {
            "schema": "vfx-harness.plan-authority-before/v1",
            "selection": "malformed",
            "pointer": pointer_record,
            "bundle_digest": None,
            "bundle_manifest_sha256": None,
        }
        return state, canonical_digest(state), None
    if not isinstance(pointer_value, dict):
        state = {
            "schema": "vfx-harness.plan-authority-before/v1",
            "selection": "malformed",
            "pointer": pointer_record,
            "bundle_digest": None,
            "bundle_manifest_sha256": None,
        }
        return state, canonical_digest(state), None

    try:
        bundle = plan_authority.resolve_current(layout.shot)
        manifest_bytes = (bundle.root / "bundle.json").read_bytes()
    except (OSError, TypeError, ValueError, plan_authority.PlanPublicationError):
        state = {
            "schema": "vfx-harness.plan-authority-before/v1",
            "selection": "invalid",
            "pointer": pointer_record,
            "bundle_digest": None,
            "bundle_manifest_sha256": None,
        }
        return state, canonical_digest(state), None

    state = {
        "schema": "vfx-harness.plan-authority-before/v1",
        "selection": "verified",
        "pointer": pointer_record,
        "bundle_digest": bundle.content_hash,
        "bundle_manifest_sha256": _sha256(manifest_bytes),
    }
    return state, canonical_digest(state), bundle.content_hash


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value == value.strip()


def _integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _parse_gate_report(
    payload: bytes,
    result: PlanLoopResult,
) -> tuple[dict[str, Any] | None, tuple[dict[str, Any], ...], tuple[str, ...]]:
    issues: list[str] = []
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, (), ("gate_report_malformed_json",)
    if not isinstance(value, dict):
        return None, (), ("gate_report_not_object",)
    if set(value) != _REPORT_FIELDS:
        return value, (), ("gate_report_fields_mismatch",)
    if value.get("schema") != _GATE_SCHEMA:
        issues.append("gate_report_schema_unsupported")
    if value.get("policy") != _GATE_POLICY:
        issues.append("gate_report_policy_unsupported")
    if value.get("validation_scope") != "structural_authority":
        issues.append("gate_report_scope_not_structural")
    if value.get("runtime_contracts_confirmed") is not False:
        issues.append("gate_report_claims_runtime_confirmation")
    for field in ("shot", "generated_at", "outcome", "signature"):
        if not _text(value.get(field)):
            issues.append(f"gate_report_{field}_invalid")
    if not isinstance(value.get("clean"), bool):
        issues.append("gate_report_clean_invalid")
    if not _integer(value.get("blocking_count")) or int(value.get("blocking_count", -1)) < 0:
        issues.append("gate_report_blocking_count_invalid")
    if not _integer(value.get("warning_count")) or int(value.get("warning_count", -1)) < 0:
        issues.append("gate_report_warning_count_invalid")
    if not isinstance(value.get("stats"), dict):
        issues.append("gate_report_stats_invalid")

    raw_findings = value.get("findings")
    if not isinstance(raw_findings, list):
        return value, (), tuple(sorted({*issues, "gate_report_findings_invalid"}))
    parsed: list[dict[str, Any]] = []
    for row in raw_findings:
        if not isinstance(row, dict) or not (_FINDING_FIELDS <= set(row) <= _FINDING_FIELDS | {"fix"}):
            issues.append("gate_report_finding_shape_invalid")
            continue
        if row.get("severity") not in {"blocking", "warning"}:
            issues.append("gate_report_finding_severity_invalid")
            continue
        if any(not _text(row.get(field)) for field in ("check", "where", "what")):
            issues.append("gate_report_finding_text_invalid")
            continue
        if "fix" in row and not _text(row["fix"]):
            issues.append("gate_report_finding_fix_invalid")
            continue
        parsed.append(dict(row))

    blocking = tuple(row for row in parsed if row["severity"] == "blocking")
    warnings = tuple(row for row in parsed if row["severity"] == "warning")
    if value.get("clean") is not (not blocking):
        issues.append("gate_report_clean_mismatch")
    if value.get("blocking_count") != len(blocking):
        issues.append("gate_report_blocking_count_mismatch")
    if value.get("warning_count") != len(warnings):
        issues.append("gate_report_warning_count_mismatch")
    expected_signature = "|".join(sorted(f"{row['check']}:{row['where']}:{row['what'][:60]}" for row in parsed))
    if value.get("signature") != expected_signature:
        issues.append("gate_report_signature_mismatch")
    if value.get("outcome") != result.outcome:
        issues.append("gate_report_result_outcome_mismatch")
    if value.get("blocking_count") != result.blocking_count:
        issues.append("gate_report_result_count_mismatch")
    if value.get("clean") is not False or not blocking:
        issues.append("gate_report_has_no_structural_blocker")
    return value, blocking, tuple(sorted(set(issues)))


def _finding_records(blocking: tuple[dict[str, Any], ...]) -> tuple[dict[str, Any], ...]:
    """Bind exact facts and semantic JSON-pointer scope to stable ids."""
    normalized_rows = [
        (
            row,
            {
                "check": str(row["check"]),
                "where": str(row["where"]),
                "what": str(row["what"]),
            },
        )
        for row in blocking
    ]
    sorted_rows = sorted(
        normalized_rows,
        key=lambda pair: (
            pair[1]["check"],
            canonical_digest(
                {
                    "schema": "vfx-harness.plan-gate-causal-fact/v1",
                    "fact": pair[1],
                }
            ),
        ),
    )
    counters: Counter[str] = Counter()
    records: list[dict[str, Any]] = []
    for row, causal_fact in sorted_rows:
        fact_digest = canonical_digest(
            {
                "schema": "vfx-harness.plan-gate-causal-fact/v1",
                "fact": causal_fact,
            }
        )
        ordinal = counters[fact_digest]
        counters[fact_digest] += 1
        finding_id = "plan-gate:" + canonical_digest(
            {
                "schema": "vfx-harness.plan-gate-finding-id/v1",
                "causal_fact_digest": fact_digest,
                "ordinal": ordinal,
            }
        )
        records.append(
            {
                "finding_id": finding_id,
                "causal_fact": causal_fact,
                "fact": row,
            }
        )
    return tuple(records)


def _publish_stop_evidence(
    layout: RunLayout,
    document: dict[str, Any],
    *,
    audit: dict[str, Any],
) -> StopEvidenceRef:
    """Publish stable causal identity and a separate uncited run audit."""

    record_digest = canonical_digest(document)
    published = {**document, "record_digest": record_digest}
    path = layout.write_report("plan-stop-evidence", published)
    try:
        payload = path.read_bytes()
        observed = json.loads(payload)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("plan-stop evidence did not publish as readable JSON") from exc
    if observed != published:
        raise RuntimeError("plan-stop evidence failed exact read-back")
    unsigned = {key: value for key, value in observed.items() if key != "record_digest"}
    if canonical_digest(unsigned) != record_digest:
        raise RuntimeError("plan-stop evidence record digest is stale")
    reference = StopEvidenceRef(
        kind="stop_evidence",
        locator=path.relative_to(layout.shot).as_posix(),
        sha256=_sha256(payload),
        record_schema=_EVIDENCE_SCHEMA,
        record_digest=record_digest,
    )
    audit_path = layout.write_report(
        "plan-stop-audit",
        {
            "schema": _AUDIT_SCHEMA,
            "run_id": layout.run_id,
            "evidence_ref": reference.as_dict(),
            "audit": audit,
        },
    )
    layout.terminal_metadata.update(
        {
            "plan_stop_evidence": "reports/plan-stop-evidence.json",
            "plan_stop_evidence_digest": record_digest,
            "plan_stop_audit": "reports/plan-stop-audit.json",
        }
    )
    if not audit_path.is_file():
        raise RuntimeError("plan-stop audit sidecar did not publish")
    return reference


def _harness_defect(
    layout: RunLayout,
    result: PlanLoopResult,
    *,
    issues: tuple[str, ...],
    candidate_record: dict[str, Any],
    report_record: dict[str, Any],
    report: dict[str, Any] | None,
    authority_before: dict[str, Any],
    authoritative_before_digest: str,
    selected_bundle_digest: str | None,
) -> StopEnvelope:
    issue_ids = tuple(f"plan-gate-evidence:{issue}" for issue in sorted(set(issues)))
    normalized = {
        "schema": "vfx-harness.plan-gate-evidence-defect/v1",
        "issues": sorted(set(issues)),
    }
    classification = {
        "schema": "vfx-harness.plan-stop-classification/v1",
        "stop_class": "harness_defect",
        "issues": sorted(set(issues)),
    }
    candidate_content = _content_record(candidate_record)
    gate_state = _causal_gate_state(report, report_record)
    authority_content = _without_audit_fields(authority_before)
    artifact_state = {
        "schema": "vfx-harness.plan-stop-artifact-state/v1",
        "candidate": candidate_content,
        "gate_report": gate_state,
    }
    attempt = {
        "schema": "vfx-harness.plan-stop-attempt/v1",
        "candidate": candidate_content,
        "gate_report": gate_state,
        "authoritative_before": authority_content,
        "result": {
            "outcome": result.outcome,
            "blocking_count": result.blocking_count,
        },
    }
    attempt_digest = canonical_digest(attempt)
    classification_digest = canonical_digest(classification)
    artifact_state_digest = canonical_digest(artifact_state)
    document = {
        "schema": _EVIDENCE_SCHEMA,
        "evidence_kind": "harness_defect",
        "candidate": candidate_content,
        "gate_report": gate_state,
        "authoritative_before": authority_content,
        "artifact_state": artifact_state,
        "classification": classification,
        "issues": sorted(set(issues)),
        "attempt_evidence_digest": attempt_digest,
        "classification_evidence_digest": classification_digest,
        "artifact_state_digest": artifact_state_digest,
        "authoritative_before_digest": authoritative_before_digest,
    }
    evidence = _publish_stop_evidence(
        layout,
        document,
        audit={
            "candidate_record": candidate_record,
            "gate_report_record": report_record,
            "gate_report": report,
            "authoritative_before": authority_before,
        },
    )
    cause = StopCause(
        invariant_id="planning_gate_requires_typed_evidence",
        finding_ids=issue_ids,
        owner_scope_ids=("global-plan-gate",),
        normalized_facts_digest=canonical_digest(normalized),
    )
    defect = EvidenceRecordAssertion(
        record_kind="defect",
        record_id=f"plan-gate-evidence-{classification_digest[:20]}",
        evidence=evidence,
    )
    target = RouteEngineeringTarget(
        cause_fingerprint=cause.fingerprint_for("harness_defect"),
        attempt_evidence_digest=attempt_digest,
        owner_scope_ids=cause.owner_scope_ids,
        defect_record=defect,
        evidence=(evidence,),
        sink_id="engineering_handoff",
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
        stage="planning",
        stop_class="harness_defect",
        identity=StopIdentity(
            run_id=layout.run_id,
            bundle_digest=selected_bundle_digest,
            view_digest=None,
            layer_id=None,
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
        artifact_state_digest=artifact_state_digest,
        authoritative_before_digest=authoritative_before_digest,
        actions=(action,),
        evidence_refs=(evidence,),
        budget_key="global-plan-gate-evidence",
        expected="A rejected global planning candidate must have complete typed gate evidence.",
        found="The plan-gate evidence is missing, malformed, or inconsistent: " + ", ".join(sorted(set(issues))) + ".",
        next_action="Route the exact candidate and gate-evidence identities to engineering.",
    )


def publish_global_plan_gate_stop(layout: RunLayout, result: PlanLoopResult) -> StopEnvelope:
    """Compile and persist the only legal stop for one rejected global gate result."""
    candidate_record, candidate_bytes, candidate_issue = _candidate_record(layout, result.path)
    report_record, report_bytes, report_issue = _report_record(layout)
    authority_before, before_digest, selected_bundle_digest = _authority_before(layout)
    issues = tuple(issue for issue in (candidate_issue, report_issue) if issue is not None)
    report: dict[str, Any] | None = None
    blocking: tuple[dict[str, Any], ...] = ()
    if report_bytes is not None:
        report, blocking, report_issues = _parse_gate_report(report_bytes, result)
        issues = (*issues, *report_issues)
    if candidate_bytes is None and candidate_issue is None:
        issues = (*issues, "candidate_unavailable")
    if issues:
        return _harness_defect(
            layout,
            result,
            issues=tuple(sorted(set(issues))),
            candidate_record=candidate_record,
            report_record=report_record,
            report=report,
            authority_before=authority_before,
            authoritative_before_digest=before_digest,
            selected_bundle_digest=selected_bundle_digest,
        )
    if report is None or candidate_bytes is None:
        return _harness_defect(
            layout,
            result,
            issues=("typed_gate_evidence_unavailable",),
            candidate_record=candidate_record,
            report_record=report_record,
            report=report,
            authority_before=authority_before,
            authoritative_before_digest=before_digest,
            selected_bundle_digest=selected_bundle_digest,
        )

    finding_records = _finding_records(blocking)
    causal_findings = tuple(
        {
            "finding_id": record["finding_id"],
            "causal_fact": record["causal_fact"],
        }
        for record in finding_records
    )
    finding_ids = tuple(record["finding_id"] for record in causal_findings)
    normalized = {
        "schema": "vfx-harness.structural-plan-blockers/v1",
        "findings": [record["causal_fact"] for record in causal_findings],
    }
    classification = {
        "schema": "vfx-harness.plan-stop-classification/v1",
        "stop_class": "authority_defect",
        "validation_scope": report["validation_scope"],
        "blocking_findings": list(causal_findings),
    }
    candidate_content = _content_record(candidate_record)
    gate_state = _causal_gate_state(report, report_record)
    authority_content = _without_audit_fields(authority_before)
    artifact_state = {
        "schema": "vfx-harness.plan-stop-artifact-state/v1",
        "candidate": candidate_content,
        "gate_report": gate_state,
        "blocking_count": len(blocking),
    }
    attempt = {
        "schema": "vfx-harness.plan-stop-attempt/v1",
        "candidate": candidate_content,
        "gate_policy": report["policy"],
        "gate_signature": gate_state["signature"],
        "finding_fact_digests": [canonical_digest(record["causal_fact"]) for record in causal_findings],
        "authoritative_before_digest": before_digest,
    }
    attempt_digest = canonical_digest(attempt)
    classification_digest = canonical_digest(classification)
    artifact_state_digest = canonical_digest(artifact_state)
    document = {
        "schema": _EVIDENCE_SCHEMA,
        "evidence_kind": "authority_defect",
        "candidate": candidate_content,
        "gate_report": gate_state,
        "authoritative_before": authority_content,
        "artifact_state": artifact_state,
        "blocking_findings": list(causal_findings),
        "classification": classification,
        "attempt_evidence_digest": attempt_digest,
        "classification_evidence_digest": classification_digest,
        "artifact_state_digest": artifact_state_digest,
        "authoritative_before_digest": before_digest,
    }
    evidence = _publish_stop_evidence(
        layout,
        document,
        audit={
            "candidate_record": candidate_record,
            "gate_report_record": report_record,
            "gate_report": report,
            "authoritative_before": authority_before,
            "blocking_findings": list(finding_records),
        },
    )
    findings = tuple(
        EvidenceRecordAssertion(
            record_kind="finding",
            record_id=record["finding_id"],
            evidence=evidence,
        )
        for record in causal_findings
    )
    target = PublishValidatedAmendmentTarget(
        scope="global_plan",
        base_bundle=SelectedBundleAssertion(
            bundle_digest=selected_bundle_digest,
            selection_digest=before_digest,
        ),
        base_view=None,
        layer_id=None,
        findings=findings,
        owner_authority_id="global-plan-authority",
        changes_hard_constraint=False,
    )
    action = StopAction(
        target=target,
        postcondition=SelectedAuthorityAmendmentCommitted(
            scope=target.scope,
            base_bundle_digest=target.base_bundle.bundle_digest,
            base_view_digest=None,
            layer_id=None,
            finding_ids=tuple(record["finding_id"] for record in causal_findings),
            gate_policy_id=_GATE_POLICY,
        ),
    )
    return StopEnvelope(
        stage="plan_gate",
        stop_class="authority_defect",
        identity=StopIdentity(
            run_id=layout.run_id,
            bundle_digest=selected_bundle_digest,
            view_digest=None,
            layer_id=None,
            unit_id=None,
            unit_plan_digest=None,
            unit_digest=None,
            candidate_digest=candidate_record["sha256"],
            checkpoint_digest=None,
            settings_digest=None,
            debt_state_digest=None,
        ),
        cause=StopCause(
            invariant_id="structural_plan_gate_blocked",
            finding_ids=finding_ids,
            owner_scope_ids=("global-plan-authority",),
            normalized_facts_digest=canonical_digest(normalized),
        ),
        attempt_evidence_digest=attempt_digest,
        classification_evidence_digest=classification_digest,
        artifact_state_digest=artifact_state_digest,
        authoritative_before_digest=before_digest,
        actions=(action,),
        evidence_refs=(evidence,),
        budget_key="global-plan-authority",
        expected="The global candidate must pass every deterministic structural authority check.",
        found=f"The exact candidate has {len(blocking)} blocking structural finding(s).",
        next_action="Publish a new validated authority amendment; this stop does not authorize apply-replan.",
    )
