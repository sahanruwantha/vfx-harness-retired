"""Strict evidence readers for the JIT materialization stop boundary."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any

import vfx_harness.agents.planner.materialization_stop_state as stop_state
import vfx_harness.orchestration.jit_materialization.gate_evidence as gate_evidence
import vfx_harness.orchestration.ledger as ledger
import vfx_harness.orchestration.revalidation as revalidation
from vfx_harness.domain.layer_outcomes import (
    LayerOutcomeContractError,
    parse_sealed_layer_outcome,
)
from vfx_harness.domain.stop_envelope_primitives import canonical_digest
from vfx_harness.orchestration import plan_authority
from vfx_harness.orchestration.jit_materialization.schema import (
    CURRENT,
    FINALIZATION_SCHEMA,
    MATERIALIZATION_SCHEMA,
    OVERLAY_ARTIFACTS,
    materialization_finalization_path,
)
from vfx_harness.orchestration.jit_materialization.view_pointer import (
    JitViewPointerError,
    canonical_view_hash,
    parse_jit_view_pointer,
    require_materialized_layers_match,
)
from vfx_harness.orchestration.layer_outcome_paths import (
    layer_outcome_locator,
    layer_outcome_path,
)

if TYPE_CHECKING:
    from vfx_harness.observability.run_artifacts import RunLayout
    from vfx_harness.orchestration.plan_authority import PlanBundle


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


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value == value.strip()


def _integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def read_file(path: Path, *, locator: str) -> tuple[dict[str, Any], bytes | None]:
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


def content_record(record: dict[str, Any]) -> dict[str, Any]:
    """Content identity only; the locator remains audit metadata."""

    return {
        key: record[key]
        for key in ("state", "sha256", "bytes")
        if key in record
    }


def _presence_record(record: dict[str, Any]) -> dict[str, Any]:
    """Retain availability without making valid audit bytes semantic authority."""

    return {"state": record["state"]}


def _relative_locator(layout: RunLayout, path: Path, fallback: str) -> str:
    try:
        return path.resolve().relative_to(layout.shot).as_posix()
    except ValueError:
        return fallback


def candidate_state(
    layout: RunLayout,
    candidate: Path,
    *,
    bundle_digest: str,
    layer_id: str,
) -> tuple[dict[str, Any], bytes | None, dict[str, Any] | None, tuple[str, ...]]:
    path = candidate.expanduser().resolve()
    locator = _relative_locator(layout, path, "candidate")
    record, payload = read_file(path, locator=locator)
    issues: list[str] = []
    document: dict[str, Any] | None = None
    if not path.is_relative_to(layout.shot):
        issues.append("candidate_outside_shot")
    if payload is None:
        issues.append(f"candidate_{record['state']}")
        return record, payload, document, tuple(issues)
    try:
        parsed = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        issues.append("candidate_malformed_json")
        return record, payload, document, tuple(issues)
    if not isinstance(parsed, dict):
        issues.append("candidate_not_object")
        return record, payload, document, tuple(issues)
    document = parsed
    if parsed.get("schema") != MATERIALIZATION_SCHEMA:
        issues.append("candidate_schema_unsupported")
    if parsed.get("bundle_hash") != bundle_digest:
        issues.append("candidate_bundle_mismatch")
    raw_layer = parsed.get("layer")
    if not isinstance(raw_layer, dict):
        issues.append("candidate_layer_invalid")
    elif str(raw_layer.get("id") or "") != layer_id:
        issues.append("candidate_layer_mismatch")
    return record, payload, document, tuple(issues)


def _selected_view_state(
    layout: RunLayout,
    *,
    bundle_digest: str,
) -> tuple[dict[str, Any], str | None, tuple[str, ...], dict[str, Any]]:
    pointer, payload = read_file(
        layout.shot / CURRENT,
        locator=CURRENT.as_posix(),
    )
    audit: dict[str, Any] = {"pointer_record": pointer}
    if payload is None:
        if pointer["state"] == "missing":
            return {
                "selection": "absent",
                "pointer": _presence_record(pointer),
            }, None, (), audit
        return {
            "selection": "invalid",
            "pointer": _presence_record(pointer),
            "reason": "unreadable",
        }, None, ("selected_view_pointer_unreadable",), audit
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {
            "selection": "invalid",
            "pointer": _presence_record(pointer),
            "reason": "malformed",
        }, None, ("selected_view_pointer_malformed",), audit
    try:
        selected = parse_jit_view_pointer(value)
    except JitViewPointerError as exc:
        return {
            "selection": "invalid",
            "pointer": _presence_record(pointer),
            "reason": exc.code,
        }, None, (f"selected_view_pointer_{exc.code}_invalid",), audit
    audit["pointer_document"] = value

    if selected.bundle_hash != bundle_digest:
        state = {
            "selection": "superseded",
            "pointer": _presence_record(pointer),
            "observed_bundle_digest": selected.bundle_hash,
            "observed_view_digest": selected.view_hash,
        }
        return state, None, (), audit

    documents: dict[str, Any] = {}
    artifact_state: dict[str, Any] = {}
    artifact_audit: dict[str, Any] = {}
    issues: list[str] = []
    shot_root = layout.shot.resolve()
    for name in OVERLAY_ARTIFACTS:
        relative = selected.artifacts[name]
        expected = selected.hashes[name]
        path = (layout.shot / relative).resolve()
        if not path.is_relative_to(shot_root):
            issues.append(f"selected_view_{name}_outside_shot")
            continue
        record, artifact_bytes = read_file(path, locator=relative)
        artifact_state[name] = content_record(record)
        artifact_audit[name] = record
        if artifact_bytes is None:
            issues.append(f"selected_view_{name}_{record['state']}")
            continue
        if record["sha256"] != expected:
            issues.append(f"selected_view_{name}_hash_mismatch")
            continue
        try:
            documents[name] = json.loads(artifact_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError):
            issues.append(f"selected_view_{name}_malformed")
    if not issues:
        try:
            require_materialized_layers_match(selected, documents["layers.json"])
        except JitViewPointerError:
            issues.append("selected_view_materialized_layers_mismatch")
    if not issues and canonical_view_hash(documents) != selected.view_hash:
        issues.append("selected_view_aggregate_mismatch")
    audit["artifact_records"] = artifact_audit
    if issues:
        # The selected pointer fields are semantic authority, while bytes that fail
        # their declared content identities are merely observations of a broken
        # harness input.  Keep those raw records in the audit sidecar so run-local
        # metadata cannot manufacture a new recovery attempt.
        state = {
            "selection": "invalid",
            "pointer": _presence_record(pointer),
            "bundle_digest": bundle_digest,
            "view_digest": selected.view_hash,
            "materialized_layers": list(selected.materialized_layers),
            "reason_codes": sorted(set(issues)),
        }
        return state, None, tuple(sorted(set(issues))), audit
    state = {
        "selection": "verified",
        "pointer": _presence_record(pointer),
        "bundle_digest": bundle_digest,
        "view_digest": selected.view_hash,
        "materialized_layers": list(selected.materialized_layers),
        "artifacts": artifact_state,
    }
    return state, selected.view_hash, (), audit


def authority_before(
    layout: RunLayout,
    bundle: PlanBundle,
    *,
    layer_id: str,
    overlay_root: Path | None,
) -> tuple[
    dict[str, Any],
    str,
    str | None,
    tuple[str, ...],
    dict[str, Any],
    dict[str, Any],
]:
    issues: list[str] = []
    pointer, pointer_bytes = read_file(
        layout.shot / plan_authority.POINTER,
        locator=plan_authority.POINTER.as_posix(),
    )
    manifest, manifest_bytes = read_file(
        bundle.root / "bundle.json",
        locator="selected-bundle/bundle.json",
    )
    try:
        current = plan_authority.resolve_current(layout.shot)
    except (OSError, TypeError, ValueError, plan_authority.PlanPublicationError):
        current = None
        issues.append("selected_bundle_unresolvable")
    if pointer_bytes is None:
        issues.append(f"selected_bundle_pointer_{pointer['state']}")
    if manifest_bytes is None:
        issues.append(f"selected_bundle_manifest_{manifest['state']}")

    expected_selection = {
        "digest": bundle.content_hash,
        "outcome": bundle.outcome,
        "artifacts": list(bundle.artifacts),
    }
    observed_selection = None
    if current is not None:
        observed_selection = {
            "digest": current.content_hash,
            "outcome": current.outcome,
            "artifacts": list(current.artifacts),
        }
        if observed_selection != expected_selection:
            issues.append("selected_bundle_changed_during_materialization")
    if current is None or pointer_bytes is None or manifest_bytes is None:
        selected_bundle_state = {
            "selection": "invalid",
            "expected": expected_selection,
            "observed": observed_selection,
            "pointer_state": pointer["state"],
            "manifest_state": manifest["state"],
        }
    elif observed_selection != expected_selection:
        selected_bundle_state = {
            "selection": "changed",
            "expected": expected_selection,
            "observed": observed_selection,
        }
    else:
        selected_bundle_state = {
            "selection": "verified",
            **expected_selection,
            "pointer": _presence_record(pointer),
            "manifest": _presence_record(manifest),
        }

    layer_row_digest: str | None = None
    dependencies: tuple[str, ...] = ()
    required_outcomes: frozenset[tuple[str, str]] = frozenset()
    reserved_roles: tuple[str, ...] = ()
    try:
        layers = json.loads((bundle.root / "layers.json").read_text(encoding="utf-8"))
        rows = layers.get("layers") if isinstance(layers, dict) else None
        row = next(
            item
            for item in rows
            if isinstance(item, dict) and str(item.get("id") or "") == layer_id
        )
        layer_row_digest = canonical_digest(row)
        dependencies = tuple(
            sorted(str(item) for item in ((row.get("jit") or {}).get("depends_on_layers") or []))
        )
        required_outcomes = frozenset(
            (str(item.get("kind")), str(item.get("id")))
            for item in ((row.get("jit") or {}).get("required_outcomes") or [])
            if isinstance(item, dict) and item.get("kind") and item.get("id")
        )
        reserved_roles = tuple(
            str(item) for item in ((row.get("jit") or {}).get("reserved_roles") or [])
        )
    except (OSError, StopIteration, TypeError, ValueError, json.JSONDecodeError):
        issues.append("selected_layer_authority_unreadable")

    view_state, view_digest, view_issues, view_audit = _selected_view_state(
        layout,
        bundle_digest=bundle.content_hash,
    )
    issues.extend(view_issues)
    dependency_layers: dict[str, Any] = {}
    if dependencies:
        try:
            dependency_layers = ledger.load_layers_from_path(
                plan_authority.selected_artifact_path(layout.shot, "layers.json")
            )
        except (
            OSError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
            plan_authority.PlanPublicationError,
        ):
            dependency_layers = {}
    outcome_state: dict[str, Any] = {}
    outcome_audit: dict[str, Any] = {}
    for dependency in dependencies:
        outcome_path = layer_outcome_path(layout.shot, dependency)
        outcome_locator = layer_outcome_locator(dependency)
        outcome_record, outcome_bytes = read_file(
            outcome_path,
            locator=outcome_locator,
        )
        script_state = None
        script_audit = None
        sealed_outcome = None
        current_eligibility: tuple[bool, tuple[str, ...]] | None = None
        if outcome_bytes is not None:
            try:
                outcome_document = json.loads(outcome_bytes)
            except (UnicodeDecodeError, json.JSONDecodeError):
                outcome_document = None
            try:
                sealed_outcome = parse_sealed_layer_outcome(
                    outcome_document,
                    expected_layer_id=dependency,
                )
            except LayerOutcomeContractError:
                sealed_outcome = None
            raw_script = sealed_outcome.script if sealed_outcome is not None else None
            if isinstance(raw_script, str) and raw_script:
                script_path = (layout.shot / raw_script).resolve()
                if script_path.is_relative_to(layout.shot):
                    script_record, _script_bytes = read_file(
                        script_path,
                        locator=raw_script,
                    )
                    script_state = content_record(script_record)
                    script_audit = script_record
                else:
                    script_state = {"state": "invalid"}
                    issues.append(f"dependency_outcome_{dependency}_script_outside_shot")
            if sealed_outcome is not None and sealed_outcome.status == "passed":
                dependency_layer = dependency_layers.get(dependency)
                current_eligibility = (
                    revalidation.current_outcome_eligibility(
                        layout.shot,
                        dependency_layer,
                        outcome_document,
                    )
                    if dependency_layer is not None
                    else (False, ("selected dependency layer is unavailable",))
                )
        semantic_outcome, outcome_issues = stop_state.dependency_outcome_state(
            outcome_bytes,
            dependency=dependency,
            required_outcomes=required_outcomes,
            script_state=script_state,
            current_eligibility=current_eligibility,
        )
        outcome_state[dependency] = semantic_outcome
        outcome_audit[dependency] = {
            "record": outcome_record,
            "script_record": script_audit,
            "current_eligibility": (
                {
                    "eligible": current_eligibility[0],
                    "reasons": list(current_eligibility[1]),
                }
                if current_eligibility is not None
                else None
            ),
        }
        issues.extend(outcome_issues)

    layer_state_record, layer_state_bytes = read_file(
        layout.shot / "state" / "work-units" / f"layer_{layer_id}.json",
        locator=f"state/work-units/layer_{layer_id}.json",
    )
    layer_state, layer_state_issues = stop_state.unit_state(
        layer_state_bytes,
        layer_id=layer_id,
    )
    issues.extend(layer_state_issues)
    resolutions_record, resolutions_bytes = read_file(
        layout.shot / "state" / "plan-resolutions.jsonl",
        locator="state/plan-resolutions.jsonl",
    )
    resolution_state, resolution_issues = stop_state.active_resolution_state(
        resolutions_bytes,
        bundle_digest=bundle.content_hash,
        reserved_roles=reserved_roles,
    )
    issues.extend(resolution_issues)
    overlay_state: dict[str, Any] | None = None
    overlay_audit: dict[str, Any] | None = None
    if overlay_root is not None:
        overlay_state = {}
        overlay_audit = {}
        for name in OVERLAY_ARTIFACTS:
            record, _payload = read_file(overlay_root / name, locator=name)
            overlay_state[name] = content_record(record)
            overlay_audit[name] = record
        if any(item.get("state") != "present" for item in overlay_state.values()):
            issues.append("rematerialization_overlay_incomplete")
    state = {
        "schema": "vfx-harness.materialization-authority-before/v1",
        "selected_bundle": selected_bundle_state,
        "selected_view": view_state,
        "layer_id": layer_id,
        "layer_row_digest": layer_row_digest,
        "layer_state": layer_state,
        "plan_resolutions": resolution_state,
        "dependency_outcomes": outcome_state,
    }
    audit = {
        "selected_bundle": {
            "pointer_record": pointer,
            "manifest_record": manifest,
            "expected_run_id": bundle.run_id,
            "expected_root": str(bundle.root),
            "observed_run_id": current.run_id if current is not None else None,
            "observed_root": str(current.root) if current is not None else None,
        },
        "selected_view": view_audit,
        "layer_state_record": layer_state_record,
        "plan_resolutions_record": resolutions_record,
        "dependency_outcomes": outcome_audit,
        "rematerialization_overlay": overlay_audit,
    }
    attempt_inputs = {"rematerialization_overlay": overlay_state}
    return (
        state,
        canonical_digest(state),
        view_digest,
        tuple(sorted(set(issues))),
        audit,
        attempt_inputs,
    )


def current_finalization(
    candidate: Path,
    *,
    bundle_digest: str,
    candidate_digest: str | None,
) -> tuple[dict[str, Any], bool, tuple[str, ...]]:
    path = materialization_finalization_path(candidate)
    record, payload = read_file(path, locator=path.name)
    if payload is None:
        return content_record(record), False, ()
    issues: list[str] = []
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return content_record(record), False, ("finalization_malformed",)
    if not isinstance(value, dict) or set(value) != {
        "schema",
        "bundle_hash",
        "candidate_revision",
    }:
        issues.append("finalization_shape_invalid")
        value = {}
    if value.get("schema") != FINALIZATION_SCHEMA:
        issues.append("finalization_schema_unsupported")
    if value.get("bundle_hash") != bundle_digest:
        issues.append("finalization_bundle_mismatch")
    revision = value.get("candidate_revision")
    if not _is_digest(revision):
        issues.append("finalization_revision_invalid")
    current = not issues and candidate_digest is not None and revision == candidate_digest
    return content_record(record), current, tuple(sorted(set(issues)))


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
    expected_outcome = "dirty" if blocking else (
        "clean_with_assumptions"
        if value.get("stats", {}).get("open_assumptions", 0)
        else (
            "clean_with_deferred"
            if value.get("stats", {}).get("open_obligations", 0)
            else "clean"
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
                "what": gate_evidence.normalized_materialization_message(str(row["what"])),
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
