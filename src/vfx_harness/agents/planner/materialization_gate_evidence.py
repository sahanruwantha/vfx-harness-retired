"""Strict parser and semantic projection for materialization plan-gate evidence."""

from __future__ import annotations

import json
from collections import Counter
from typing import TYPE_CHECKING, Any

import vfx_harness.orchestration.jit_materialization.gate_evidence as gate_evidence
from vfx_harness.agents.planner.materialization_stop_files import (
    content_record,
    read_file,
)
from vfx_harness.domain.stop_envelope_primitives import canonical_digest

if TYPE_CHECKING:
    from vfx_harness.observability.run_artifacts import RunLayout


_GATE_SCHEMA = "vfx-harness.plan-gate/v1"
_GATE_FIELDS = {
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
_GATE_FINDING_FIELDS = {"check", "severity", "where", "what"}
_GATE_RECORD_FIELDS = {
    "schema",
    "bundle_digest",
    "layer_id",
    "candidate",
    "phase",
    "local_findings",
    "gate_result",
    "gate_issue",
    "finalization",
    "record_digest",
}


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value == value.strip()


def _integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _parse_gate_result(
    value: object,
    *,
    shot_id: str,
) -> tuple[tuple[dict[str, Any], ...], tuple[str, ...], bool]:
    issues: list[str] = []
    if not isinstance(value, dict):
        return (), ("gate_result_not_object",), False
    if set(value) != _GATE_FIELDS:
        return (), ("gate_result_fields_mismatch",), False
    if value.get("schema") != _GATE_SCHEMA:
        issues.append("gate_result_schema_unsupported")
    if value.get("policy") != "structural-authority/runtime-falsification-v1":
        issues.append("gate_result_policy_unsupported")
    if value.get("validation_scope") != "structural_authority":
        issues.append("gate_result_scope_not_structural")
    if value.get("runtime_contracts_confirmed") is not False:
        issues.append("gate_result_claims_runtime_confirmation")
    if value.get("shot") != shot_id:
        issues.append("gate_result_shot_mismatch")
    for field in ("generated_at", "outcome"):
        if not _text(value.get(field)):
            issues.append(f"gate_result_{field}_invalid")
    if not isinstance(value.get("signature"), str):
        issues.append("gate_result_signature_invalid")
    if not isinstance(value.get("clean"), bool):
        issues.append("gate_result_clean_invalid")
    for field in ("blocking_count", "warning_count"):
        if not _integer(value.get(field)) or int(value.get(field, -1)) < 0:
            issues.append(f"gate_result_{field}_invalid")
    if not isinstance(value.get("stats"), dict):
        issues.append("gate_result_stats_invalid")

    findings = value.get("findings")
    if not isinstance(findings, list):
        return (), tuple(sorted({*issues, "gate_result_findings_invalid"})), False
    parsed: list[dict[str, Any]] = []
    for row in findings:
        if not isinstance(row, dict) or not (
            _GATE_FINDING_FIELDS <= set(row) <= _GATE_FINDING_FIELDS | {"fix"}
        ):
            issues.append("gate_result_finding_shape_invalid")
            continue
        if row.get("severity") not in {"blocking", "warning"}:
            issues.append("gate_result_finding_severity_invalid")
            continue
        if any(not _text(row.get(field)) for field in ("check", "where", "what")):
            issues.append("gate_result_finding_text_invalid")
            continue
        if "fix" in row and not _text(row["fix"]):
            issues.append("gate_result_finding_fix_invalid")
            continue
        parsed.append(dict(row))
    blocking = tuple(row for row in parsed if row["severity"] == "blocking")
    warnings = tuple(row for row in parsed if row["severity"] == "warning")
    clean = not blocking
    if value.get("clean") is not clean:
        issues.append("gate_result_clean_mismatch")
    if value.get("blocking_count") != len(blocking):
        issues.append("gate_result_blocking_count_mismatch")
    if value.get("warning_count") != len(warnings):
        issues.append("gate_result_warning_count_mismatch")
    signature = "|".join(
        sorted(f"{row['check']}:{row['where']}:{row['what'][:60]}" for row in parsed)
    )
    if value.get("signature") != signature:
        issues.append("gate_result_signature_mismatch")
    expected_outcome = (
        "dirty"
        if blocking
        else (
            "clean_with_assumptions"
            if value.get("stats", {}).get("open_assumptions", 0)
            else (
                "clean_with_deferred"
                if value.get("stats", {}).get("open_obligations", 0)
                else "clean"
            )
        )
    )
    if value.get("outcome") != expected_outcome:
        issues.append("gate_result_outcome_mismatch")
    return blocking, tuple(sorted(set(issues))), clean


def gate_finding_records(
    blocking: tuple[dict[str, Any], ...],
) -> tuple[dict[str, Any], ...]:
    normalized = [
        (
            row,
            {
                "check": str(row["check"]),
                **(
                    {"pointer": pointer}
                    if (
                        pointer := gate_evidence.semantic_materialization_pointer(
                            str(row["where"])
                        )
                    )
                    is not None
                    else {
                        "scope": gate_evidence.normalized_materialization_message(
                            str(row["where"])
                        )
                    }
                ),
                "what": gate_evidence.normalized_materialization_message(
                    str(row["what"])
                ),
            },
        )
        for row in blocking
    ]
    normalized.sort(
        key=lambda pair: canonical_digest(
            {
                "schema": "vfx-harness.materialization-gate-causal-fact/v1",
                "fact": pair[1],
            }
        )
    )
    counters: Counter[str] = Counter()
    records: list[dict[str, Any]] = []
    for row, causal_fact in normalized:
        fact_digest = canonical_digest(
            {
                "schema": "vfx-harness.materialization-gate-causal-fact/v1",
                "fact": causal_fact,
            }
        )
        ordinal = counters[fact_digest]
        counters[fact_digest] += 1
        records.append(
            {
                "finding_id": "materialization-gate:"
                + canonical_digest(
                    {
                        "schema": "vfx-harness.materialization-gate-finding-id/v1",
                        "causal_fact_digest": fact_digest,
                        "ordinal": ordinal,
                    }
                ),
                "causal_fact": causal_fact,
                "fact": row,
            }
        )
    return tuple(records)


def _normalize_gate_value(value: Any) -> Any:
    """Remove run-local strings from otherwise semantic gate measurements."""

    if isinstance(value, str):
        return gate_evidence.normalized_materialization_message(value)
    if isinstance(value, list):
        return [_normalize_gate_value(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _normalize_gate_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    return value


def _semantic_gate_result(value: dict[str, Any]) -> dict[str, Any]:
    findings = []
    for row in value["findings"]:
        pointer = gate_evidence.semantic_materialization_pointer(row["where"])
        normalized = {
            "check": row["check"],
            "severity": row["severity"],
            **(
                {"pointer": pointer}
                if pointer is not None
                else {
                    "scope": gate_evidence.normalized_materialization_message(
                        row["where"]
                    )
                }
            ),
            "what": gate_evidence.normalized_materialization_message(row["what"]),
        }
        if "fix" in row:
            normalized["fix"] = gate_evidence.normalized_materialization_message(
                row["fix"]
            )
        findings.append(normalized)
    findings.sort(key=canonical_digest)
    return {
        "schema": value["schema"],
        "policy": value["policy"],
        "validation_scope": value["validation_scope"],
        "runtime_contracts_confirmed": value["runtime_contracts_confirmed"],
        "clean": value["clean"],
        "outcome": value["outcome"],
        "blocking_count": value["blocking_count"],
        "warning_count": value["warning_count"],
        "stats": _normalize_gate_value(value["stats"]),
        "findings": findings,
    }


def gate_evidence_state(
    layout: RunLayout,
    *,
    candidate_record: dict[str, Any],
    bundle_digest: str,
    layer_id: str,
    current_finalization: dict[str, Any],
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    tuple[dict[str, Any], ...],
    bool,
    tuple[str, ...],
]:
    path = layout.reports / f"{gate_evidence.MATERIALIZATION_GATE_REPORT}.json"
    record, payload = read_file(
        path,
        locator=f"reports/{gate_evidence.MATERIALIZATION_GATE_REPORT}.json",
    )
    audit: dict[str, Any] = {"record": record}
    if payload is None:
        return (
            content_record(record),
            audit,
            (),
            False,
            (f"gate_evidence_{record['state']}",),
        )
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return (
            {**content_record(record), "validation": "invalid"},
            audit,
            (),
            False,
            ("gate_evidence_malformed",),
        )
    if not isinstance(value, dict) or set(value) != _GATE_RECORD_FIELDS:
        return (
            {**content_record(record), "validation": "invalid"},
            audit,
            (),
            False,
            ("gate_evidence_shape_invalid",),
        )
    audit["document"] = value
    unsigned = {key: item for key, item in value.items() if key != "record_digest"}
    issues: list[str] = []
    if value.get("schema") != gate_evidence.MATERIALIZATION_GATE_EVIDENCE_SCHEMA:
        issues.append("gate_evidence_schema_unsupported")
    if value.get("record_digest") != canonical_digest(unsigned):
        issues.append("gate_evidence_digest_mismatch")
    if value.get("bundle_digest") != bundle_digest:
        issues.append("gate_evidence_bundle_mismatch")
    if value.get("layer_id") != layer_id:
        issues.append("gate_evidence_layer_mismatch")
    pinned_candidate = value.get("candidate")
    if not isinstance(pinned_candidate, dict):
        issues.append("gate_evidence_candidate_invalid")
    elif (
        pinned_candidate.get("state") != "present"
        or pinned_candidate.get("sha256") != candidate_record.get("sha256")
        or pinned_candidate.get("bytes") != candidate_record.get("bytes")
    ):
        issues.append("gate_evidence_candidate_mismatch")
    pinned_finalization = value.get("finalization")
    if not isinstance(pinned_finalization, dict):
        issues.append("gate_evidence_finalization_invalid")
    elif content_record(pinned_finalization) != current_finalization:
        issues.append("gate_evidence_finalization_mismatch")
    if value.get("phase") != "terminal_gate":
        issues.append("gate_evidence_phase_not_terminal")
    if value.get("local_findings") != []:
        issues.append("gate_evidence_local_findings_unexpected")
    if value.get("gate_issue") is not None:
        issues.append("gate_evidence_gate_issue_present")
    blocking, gate_issues, clean = _parse_gate_result(
        value.get("gate_result"),
        shot_id=layout.shot.name,
    )
    issues.extend(gate_issues)
    unique_issues = tuple(sorted(set(issues)))
    if unique_issues:
        return (
            {**content_record(record), "validation": "invalid"},
            audit,
            blocking,
            clean,
            unique_issues,
        )

    gate_result = value["gate_result"]
    assert isinstance(gate_result, dict)
    semantic_document = {
        "schema": value["schema"],
        "bundle_digest": value["bundle_digest"],
        "layer_id": value["layer_id"],
        "candidate": content_record(value["candidate"]),
        "phase": value["phase"],
        "local_findings": [],
        "gate_result": _semantic_gate_result(gate_result),
        "gate_issue": None,
        "finalization": content_record(value["finalization"]),
    }
    semantic_record = {
        "state": "present",
        "validation": "verified",
        "record_schema": gate_evidence.MATERIALIZATION_GATE_EVIDENCE_SCHEMA,
        "record_digest": canonical_digest(semantic_document),
        "document": semantic_document,
    }
    return semantic_record, audit, blocking, clean, ()
