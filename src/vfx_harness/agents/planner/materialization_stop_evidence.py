"""Strict evidence readers for the JIT materialization stop boundary."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import vfx_harness.agents.planner.materialization_stop_state as stop_state
import vfx_harness.orchestration.layer_publication as layer_publication
import vfx_harness.orchestration.ledger as ledger
from vfx_harness.agents.planner.materialization_gate_evidence import (
    _GATE_FIELDS as _GATE_FIELDS,
)
from vfx_harness.agents.planner.materialization_gate_evidence import (
    _GATE_FINDING_FIELDS as _GATE_FINDING_FIELDS,
)
from vfx_harness.agents.planner.materialization_gate_evidence import (
    _GATE_RECORD_FIELDS as _GATE_RECORD_FIELDS,
)
from vfx_harness.agents.planner.materialization_gate_evidence import (
    _GATE_SCHEMA as _GATE_SCHEMA,
)
from vfx_harness.agents.planner.materialization_gate_evidence import (
    _integer as _integer,
)
from vfx_harness.agents.planner.materialization_gate_evidence import (
    _normalize_gate_value as _normalize_gate_value,
)
from vfx_harness.agents.planner.materialization_gate_evidence import (
    _parse_gate_result as _parse_gate_result,
)
from vfx_harness.agents.planner.materialization_gate_evidence import (
    _semantic_gate_result as _semantic_gate_result,
)
from vfx_harness.agents.planner.materialization_gate_evidence import (
    _text as _text,
)
from vfx_harness.agents.planner.materialization_gate_evidence import (
    gate_evidence as gate_evidence,
)
from vfx_harness.agents.planner.materialization_gate_evidence import (
    gate_evidence_state as gate_evidence_state,
)
from vfx_harness.agents.planner.materialization_gate_evidence import (
    gate_finding_records as gate_finding_records,
)
from vfx_harness.agents.planner.materialization_stop_files import (
    _is_digest as _is_digest,
)
from vfx_harness.agents.planner.materialization_stop_files import (
    _sha256 as _sha256,
)
from vfx_harness.agents.planner.materialization_stop_files import (
    content_record as content_record,
)
from vfx_harness.agents.planner.materialization_stop_files import read_file as read_file
from vfx_harness.domain.layer_outcomes import (
    LayerOutcomeContractError,
    parse_sealed_layer_outcome,
)
from vfx_harness.domain.stop_envelope_primitives import canonical_digest
from vfx_harness.orchestration import plan_authority
from vfx_harness.orchestration.jit_materialization.schema import (
    CURRENT,
    MATERIALIZATION_SCHEMA,
    OVERLAY_ARTIFACTS,
    materialization_finalization_attested,
    materialization_finalization_path,
    read_materialization_finalization,
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
    from vfx_harness.orchestration.authority_selection import ResolvedSelectedAuthority
    from vfx_harness.orchestration.plan_authority import PlanBundle


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
    selected_authority: ResolvedSelectedAuthority | None = None,
) -> tuple[dict[str, Any], str | None, tuple[str, ...], dict[str, Any]]:
    pointer, payload = read_file(
        layout.shot / CURRENT,
        locator=CURRENT.as_posix(),
    )
    audit: dict[str, Any] = {"pointer_record": pointer}
    if payload is None:
        if (
            selected_authority is not None
            and selected_authority.selection_token.jit_revision != 0
        ):
            return {
                "selection": "invalid",
                "pointer": _presence_record(pointer),
                "reason": "changed_after_snapshot",
            }, None, ("selected_view_pointer_changed_after_snapshot",), audit
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
    if (
        selected_authority is not None
        and pointer.get("sha256")
        != selected_authority.selection_token.jit_pointer_sha256
    ):
        return {
            "selection": "invalid",
            "pointer": _presence_record(pointer),
            "reason": "changed_after_snapshot",
        }, None, ("selected_view_pointer_changed_after_snapshot",), audit
    try:
        selected = parse_jit_view_pointer(value)
    except JitViewPointerError as exc:
        return {
            "selection": "invalid",
            "pointer": _presence_record(pointer),
            "reason": exc.code,
        }, None, (f"selected_view_pointer_{exc.code}_invalid",), audit
    audit["pointer_document"] = value

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
        state = {
            "selection": "invalid",
            "pointer": _presence_record(pointer),
            "bundle_digest": bundle_digest,
            "view_digest": selected.view_hash,
            "materialized_layers": list(selected.materialized_layers),
            "reason_codes": sorted(set(issues)),
        }
        return state, None, tuple(sorted(set(issues))), audit
    expected_plan_revision = (
        selected_authority.plan.revision
        if selected_authority is not None and selected_authority.plan is not None
        else selected.plan_revision
    )
    if (
        selected.bundle_hash != bundle_digest
        or selected.plan_revision != expected_plan_revision
    ):
        state = {
            "selection": "superseded",
            "pointer": _presence_record(pointer),
            "observed_bundle_digest": selected.bundle_hash,
            "observed_view_digest": selected.view_hash,
        }
        return state, None, (), audit
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
    selected_authority: ResolvedSelectedAuthority | None,
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
    current = selected_authority.plan.bundle if (
        selected_authority is not None and selected_authority.plan is not None
    ) else None
    if selected_authority is None:
        issues.append("selected_bundle_unresolvable")
    elif pointer.get("sha256") != selected_authority.selection_token.plan_pointer_sha256:
        current = None
        issues.append("selected_bundle_changed_after_snapshot")
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
        selected_authority=selected_authority,
    )
    issues.extend(view_issues)
    dependency_layers: dict[str, Any] = {}
    if dependencies:
        try:
            if selected_authority is None:
                raise ValueError("selected authority snapshot is unavailable")
            dependency_layers = ledger.load_layers_from_path(
                selected_authority.artifact_paths["layers.json"]
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
        current_publication_receipt_digest: str | None = None
        publication_error: str | None = None
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
        dependency_layer = dependency_layers.get(dependency)
        if selected_authority is None:
            publication_error = "selected authority snapshot is unavailable"
        elif dependency_layer is None:
            publication_error = "selected dependency layer is unavailable"
        else:
            try:
                publication = layer_publication.require_current_layer_publication(
                    layout.shot,
                    dependency_layer,
                    selected_authority,
                )
            except layer_publication.LayerPublicationConflict as exc:
                publication_error = str(exc)
            else:
                if outcome_record.get("sha256") != publication.outcome_sha256:
                    publication_error = (
                        "dependency outcome changed across receipt-backed publication verification"
                    )
                elif (
                    script_state is None
                    or script_state.get("sha256") != publication.ledger_script_sha256
                ):
                    publication_error = (
                        "dependency script changed across receipt-backed publication verification"
                    )
                else:
                    current_publication_receipt_digest = (
                        publication.receipt.receipt_digest
                    )
        semantic_outcome, outcome_issues = stop_state.dependency_outcome_state(
            outcome_bytes,
            dependency=dependency,
            required_outcomes=required_outcomes,
            script_state=script_state,
            current_publication_receipt_digest=current_publication_receipt_digest,
        )
        outcome_state[dependency] = semantic_outcome
        outcome_audit[dependency] = {
            "record": outcome_record,
            "script_record": script_audit,
            "current_publication": (
                {
                    "receipt_digest": current_publication_receipt_digest,
                    "error": publication_error,
                }
                if current_publication_receipt_digest is not None
                or publication_error is not None
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
        finalization = read_materialization_finalization(candidate)
    except (OSError, ValueError):
        return content_record(record), False, ("finalization_invalid",)
    if finalization.bundle_hash != bundle_digest:
        issues.append("finalization_bundle_mismatch")
    current = (
        not issues
        and candidate_digest is not None
        and finalization.candidate_revision == candidate_digest
        and materialization_finalization_attested(
            candidate,
            bundle_hash=bundle_digest,
        )
    )
    return content_record(record), current, tuple(sorted(set(issues)))
