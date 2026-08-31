"""Semantic before-state for a boundary that failed to classify its own stop.

This reader is deliberately independent of orchestration modules: plan publication
imports :mod:`run_artifacts`, so importing the planner back from that terminal fallback
would create a dependency cycle.  The fallback cannot grant execution authority.  It
only needs a stable, fail-closed identity that changes when selected plan/view or
accepted build content changes and remains stable across audit-only republishes.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_PLAN_POINTER = Path("plans/current.json")
_PLAN_POINTER_SCHEMA = "vfx-harness.plan-pointer/v1"
_PLAN_BUNDLE_SCHEMA = "vfx-harness.plan-bundle/v1"
_JIT_POINTER = Path("state/jit-layers/current.json")
_JIT_VIEW_SCHEMA = "vfx-harness.jit-layer-view/v1"
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_TERMINAL_CAUSES = {
    "build_truncated",
    "gate_rejected",
    "gate_stalled",
    "interrupted",
    "max_turns_exhausted",
    "missing_stop_envelope",
    "model_budget_exhausted",
    "model_session_failure",
    "model_session_idle_timeout",
    "plan_budget_exhausted",
    "process_error",
    "requested_exit",
    "service_unavailable",
    "session_stalled",
    "terminal_service_error",
    "usage_limit",
}


def closed_terminal_cause(value: str) -> str:
    """Keep arbitrary legacy exception metadata out of action identity."""

    return value if value in _TERMINAL_CAUSES else "unclassified_terminal_cause"


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and bool(_DIGEST.fullmatch(value))


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _locator(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return path.name


def _read_json(root: Path, path: Path) -> tuple[dict[str, Any], object | None, str | None]:
    """Return raw audit data, parsed JSON, and a closed read issue."""

    audit: dict[str, Any] = {"locator": _locator(root, path)}
    try:
        payload = path.read_bytes()
    except FileNotFoundError:
        return {**audit, "state": "missing"}, None, "missing"
    except OSError:
        return {**audit, "state": "unreadable"}, None, "unreadable"
    audit.update(state="present", sha256=_sha256(payload), bytes=len(payload))
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return audit, None, "non_utf8"
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        audit["raw_text"] = text
        return audit, None, "malformed_json"
    audit["document"] = value
    return audit, value, None


def _inside(root: Path, relative: object) -> Path | None:
    if not isinstance(relative, str) or not relative.strip():
        return None
    try:
        candidate = (root / relative).resolve()
        candidate.relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    return candidate


def _bundle_hash(artifacts: Mapping[str, str]) -> str:
    digest = hashlib.sha256()
    for name in sorted(artifacts):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(artifacts[name].encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _selected_bundle(shot: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    pointer_audit, raw_pointer, read_issue = _read_json(shot, shot / _PLAN_POINTER)
    audit: dict[str, Any] = {"pointer": pointer_audit}
    if read_issue == "missing":
        return {"selection": "absent"}, audit
    if read_issue is not None:
        return {"selection": "invalid", "issues": [f"pointer_{read_issue}"]}, audit
    if not isinstance(raw_pointer, Mapping):
        return {"selection": "invalid", "issues": ["pointer_not_object"]}, audit

    issues: list[str] = []
    if raw_pointer.get("schema") != _PLAN_POINTER_SCHEMA:
        issues.append("pointer_schema_unsupported")
    bundle_digest = raw_pointer.get("content_hash")
    if not _is_digest(bundle_digest):
        issues.append("pointer_bundle_digest_invalid")
    outcome = raw_pointer.get("outcome")
    if not isinstance(outcome, str) or not outcome.strip():
        issues.append("pointer_outcome_invalid")

    bundle_root = _inside(shot, raw_pointer.get("bundle"))
    if bundle_root is None:
        issues.append("pointer_bundle_locator_invalid")
    else:
        try:
            relative = bundle_root.relative_to((shot / "runs").resolve())
        except ValueError:
            issues.append("pointer_bundle_outside_run_store")
        else:
            if (
                len(relative.parts) != 5
                or relative.parts[1:4] != ("checkpoints", "plans", "bundles")
                or (_is_digest(bundle_digest) and relative.parts[4] != bundle_digest)
            ):
                issues.append("pointer_bundle_shape_invalid")

    manifest_audit: dict[str, Any] | None = None
    raw_manifest: object | None = None
    if bundle_root is not None:
        manifest_audit, raw_manifest, manifest_issue = _read_json(
            shot,
            bundle_root / "bundle.json",
        )
        audit["manifest"] = manifest_audit
        if manifest_issue is not None:
            issues.append(f"manifest_{manifest_issue}")
    if raw_manifest is not None and not isinstance(raw_manifest, Mapping):
        issues.append("manifest_not_object")
        raw_manifest = None

    artifact_digests: dict[str, str] = {}
    if isinstance(raw_manifest, Mapping):
        if raw_manifest.get("schema") != _PLAN_BUNDLE_SCHEMA:
            issues.append("manifest_schema_unsupported")
        if raw_manifest.get("content_hash") != bundle_digest:
            issues.append("manifest_bundle_digest_mismatch")
        if raw_manifest.get("outcome") != outcome:
            issues.append("manifest_outcome_mismatch")
        if (
            raw_manifest.get("run_id") != raw_pointer.get("run_id")
            or (
                bundle_root is not None
                and bundle_root.is_relative_to((shot / "runs").resolve())
                and bundle_root.relative_to((shot / "runs").resolve()).parts[0]
                != raw_pointer.get("run_id")
            )
        ):
            issues.append("publisher_identity_mismatch")
        raw_artifacts = raw_manifest.get("artifacts")
        if not isinstance(raw_artifacts, Mapping) or not raw_artifacts:
            issues.append("manifest_artifacts_invalid")
        elif bundle_root is not None:
            for raw_name, expected in sorted(raw_artifacts.items(), key=lambda row: str(row[0])):
                if not isinstance(raw_name, str) or not _is_digest(expected):
                    issues.append("manifest_artifact_identity_invalid")
                    continue
                artifact = _inside(bundle_root, raw_name)
                if artifact is None:
                    issues.append("manifest_artifact_locator_invalid")
                    continue
                try:
                    observed = _sha256(artifact.read_bytes())
                except FileNotFoundError:
                    issues.append("manifest_artifact_missing")
                    continue
                except OSError:
                    issues.append("manifest_artifact_unreadable")
                    continue
                if observed != expected:
                    issues.append("manifest_artifact_hash_mismatch")
                    continue
                artifact_digests[raw_name] = observed
            if (
                len(artifact_digests) == len(raw_artifacts)
                and _is_digest(bundle_digest)
                and _bundle_hash(artifact_digests) != bundle_digest
            ):
                issues.append("manifest_aggregate_hash_mismatch")

    semantic: dict[str, Any] = {
        "selection": "invalid" if issues else "verified",
        "bundle_digest": bundle_digest if _is_digest(bundle_digest) else None,
        "outcome": outcome if isinstance(outcome, str) and outcome.strip() else None,
    }
    if issues:
        semantic["issues"] = sorted(set(issues))
    else:
        semantic["artifacts"] = artifact_digests
    return semantic, audit


def _selected_view(
    shot: Path,
    selected_bundle: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    pointer_audit, raw_pointer, read_issue = _read_json(shot, shot / _JIT_POINTER)
    audit: dict[str, Any] = {"pointer": pointer_audit}
    bundle_digest = (
        selected_bundle.get("bundle_digest")
        if selected_bundle.get("selection") == "verified"
        else None
    )
    if read_issue == "missing":
        if _is_digest(bundle_digest):
            return {
                "selection": "bundle",
                "bundle_digest": bundle_digest,
                "view_digest": bundle_digest,
            }, audit
        return {"selection": "absent"}, audit
    if read_issue is not None:
        return {"selection": "invalid", "issues": [f"pointer_{read_issue}"]}, audit
    if not isinstance(raw_pointer, Mapping):
        return {"selection": "invalid", "issues": ["pointer_not_object"]}, audit

    observed_bundle = raw_pointer.get("bundle_hash")
    observed_view = raw_pointer.get("view_hash")
    if _is_digest(bundle_digest) and observed_bundle != bundle_digest:
        return {
            "selection": "superseded",
            "observed_bundle_digest": observed_bundle if _is_digest(observed_bundle) else None,
            "observed_view_digest": observed_view if _is_digest(observed_view) else None,
        }, audit

    issues: list[str] = []
    if raw_pointer.get("schema") != _JIT_VIEW_SCHEMA:
        issues.append("pointer_schema_unsupported")
    if not _is_digest(observed_bundle):
        issues.append("pointer_bundle_digest_invalid")
    if not _is_digest(observed_view):
        issues.append("pointer_view_digest_invalid")
    materialized = raw_pointer.get("materialized_layers")
    if (
        not isinstance(materialized, list)
        or any(not isinstance(item, str) or not item.strip() for item in materialized)
        or materialized != sorted(set(materialized))
    ):
        issues.append("pointer_materialized_layers_invalid")
        materialized = []
    raw_artifacts = raw_pointer.get("artifacts")
    raw_hashes = raw_pointer.get("hashes")
    if (
        not isinstance(raw_artifacts, Mapping)
        or not raw_artifacts
        or not isinstance(raw_hashes, Mapping)
        or set(raw_artifacts) != set(raw_hashes)
    ):
        issues.append("pointer_artifacts_invalid")
        raw_artifacts = {}
        raw_hashes = {}

    documents: dict[str, Any] = {}
    artifact_digests: dict[str, str] = {}
    artifact_audit: dict[str, Any] = {}
    for raw_name, relative in sorted(raw_artifacts.items(), key=lambda row: str(row[0])):
        expected = raw_hashes.get(raw_name)
        if not isinstance(raw_name, str) or not _is_digest(expected):
            issues.append("view_artifact_identity_invalid")
            continue
        artifact = _inside(shot, relative)
        if artifact is None:
            issues.append("view_artifact_locator_invalid")
            continue
        record, document, artifact_issue = _read_json(shot, artifact)
        artifact_audit[raw_name] = record
        if artifact_issue is not None:
            issues.append(f"view_artifact_{artifact_issue}")
            continue
        if record.get("sha256") != expected:
            issues.append("view_artifact_hash_mismatch")
            continue
        documents[raw_name] = document
        artifact_digests[raw_name] = expected
    audit["artifacts"] = artifact_audit
    if len(documents) == len(raw_artifacts) and _is_digest(observed_view):
        aggregate = _sha256(
            json.dumps(documents, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        if aggregate != observed_view:
            issues.append("view_aggregate_hash_mismatch")

    semantic = {
        "selection": "invalid" if issues else "verified",
        "bundle_digest": observed_bundle if _is_digest(observed_bundle) else None,
        "view_digest": observed_view if _is_digest(observed_view) else None,
    }
    if issues:
        semantic["issues"] = sorted(set(issues))
    else:
        semantic["materialized_layers"] = materialized
        semantic["artifacts"] = artifact_digests
    return semantic, audit


def _artifact_state(shot: Path, relative: object) -> dict[str, Any]:
    artifact = _inside(shot, relative)
    if artifact is None:
        return {"state": "absent" if relative is None or relative == "" else "invalid"}
    try:
        payload = artifact.read_bytes()
    except FileNotFoundError:
        return {"state": "missing"}
    except OSError:
        return {"state": "unreadable"}
    return {"state": "present", "sha256": _sha256(payload)}


def _acceptance_state(raw_acceptance: object) -> dict[str, Any]:
    if raw_acceptance is None:
        return {"state": "absent"}
    if not isinstance(raw_acceptance, Mapping):
        return {"state": "invalid"}
    outcome = raw_acceptance.get("outcome")
    if outcome is None:
        return {"state": "uncommitted"}
    if not isinstance(outcome, Mapping):
        return {"state": "invalid"}
    names = (
        "schema",
        "authority_digest",
        "bundle_digest",
        "view_digest",
        "chain_digest",
        "passed",
        "total",
        "outcome_digest",
    )
    moments = outcome.get("moments")
    semantic_moments = []
    if isinstance(moments, list):
        for row in moments:
            if isinstance(row, Mapping):
                semantic_moments.append(
                    {
                        key: row.get(key)
                        for key in (
                            "schema",
                            "moment_id",
                            "passed",
                            "evidence_digest",
                            "moment_outcome_digest",
                        )
                    }
                )
    return {
        "state": "committed",
        "outcome": {**{name: outcome.get(name) for name in names}, "moments": semantic_moments},
    }


def _accepted_build(shot: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    audit, raw_ledger, read_issue = _read_json(shot, shot / "shot.json")
    if read_issue == "missing":
        return {"state": "absent"}, audit
    if read_issue is not None:
        return {"state": "invalid", "issues": [read_issue]}, audit
    if not isinstance(raw_ledger, Mapping):
        return {"state": "invalid", "issues": ["not_object"]}, audit
    raw_milestones = raw_ledger.get("milestones")
    if not isinstance(raw_milestones, Mapping):
        return {"state": "invalid", "issues": ["milestones_invalid"]}, audit

    milestones: dict[str, Any] = {}
    for raw_id, raw_row in sorted(raw_milestones.items(), key=lambda row: str(row[0])):
        layer_id = str(raw_id)
        if not isinstance(raw_row, Mapping):
            milestones[layer_id] = {"state": "invalid"}
            continue
        row: dict[str, Any] = {
            "status": str(raw_row.get("status") or "pending"),
            "script_artifact": _artifact_state(shot, raw_row.get("script")),
        }
        if isinstance(raw_row.get("frame"), int) and not isinstance(raw_row.get("frame"), bool):
            row["frame"] = raw_row["frame"]
        for name in ("unit_hash", "artifact_unit_hash", "script_sha"):
            value = raw_row.get(name)
            if isinstance(value, str) and value.strip():
                row[name] = value
        milestones[layer_id] = row
    return {
        "state": "present",
        "shot_id": raw_ledger.get("shot") if isinstance(raw_ledger.get("shot"), str) else None,
        "milestones": milestones,
        "acceptance": _acceptance_state(raw_ledger.get("acceptance")),
    }, audit


def snapshot(
    shot_folder: str | Path,
    manifest_path: str | Path,
    *,
    boundary: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return semantic authority and uncited raw source audit for one fallback stop."""

    shot = Path(shot_folder).resolve()
    manifest_audit, raw_manifest, _manifest_issue = _read_json(
        shot,
        Path(manifest_path),
    )
    manifest_shot_id = (
        raw_manifest.get("shot_id")
        if isinstance(raw_manifest, Mapping) and isinstance(raw_manifest.get("shot_id"), str)
        else shot.name
    )
    selected_bundle, bundle_audit = _selected_bundle(shot)
    selected_view, view_audit = _selected_view(shot, selected_bundle)
    accepted_build, ledger_audit = _accepted_build(shot)
    authority = {
        "schema": "vfx-harness.unclassified-boundary-authority/v2",
        "shot_id": manifest_shot_id,
        "selected_bundle": selected_bundle,
        "selected_view": selected_view,
        "accepted_build": accepted_build,
    }
    audit = {
        "schema": "vfx-harness.unclassified-boundary-authority-audit/v1",
        "boundary": boundary,
        "manifest": manifest_audit,
        "selected_bundle": bundle_audit,
        "selected_view": view_audit,
        "accepted_build": ledger_audit,
    }
    return authority, audit
