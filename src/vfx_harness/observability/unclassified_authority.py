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
from pathlib import Path, PurePosixPath
from typing import Any

from vfx_harness.domain.stop_envelopes import STOP_CLASSES

_PLAN_POINTER = Path("plans/current.json")
_PLAN_POINTER_SCHEMA = "vfx-harness.plan-pointer/v2"
_PLAN_BUNDLE_SCHEMA = "vfx-harness.plan-bundle/v1"
_JIT_POINTER = Path("state/jit-layers/current.json")
_JIT_VIEW_SCHEMA = "vfx-harness.jit-layer-view/v2"
_PLAN_POINTER_FIELDS = frozenset(
    {
        "schema",
        "revision",
        "run_id",
        "bundle",
        "content_hash",
        "outcome",
        "published_at",
    }
)
_JIT_POINTER_FIELDS = frozenset(
    {
        "schema",
        "revision",
        "plan_revision",
        "bundle_hash",
        "view_hash",
        "materialized_layers",
        "artifacts",
        "hashes",
    }
)
_OVERLAY_ARTIFACTS = frozenset(
    {
        "layers.json",
        "scene_checks.json",
        "checks.json",
        "requirements.json",
        "acceptance.json",
    }
)
_PUBLISHABLE_OUTCOMES = frozenset(
    {"clean", "clean_with_assumptions", "clean_with_deferred"}
)
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
#: Why a run ended.  This is a different question from ``StopEnvelope.stop_class``,
#: which says who owns the stop -- and the two vocabularies are disjoint, so a stop
#: class is never a legal value here.  Three members exist because typed stops arrived
#: after this set did and had no honest cause to name; the sites that needed them
#: reached for the stop class instead (HIR-0227).
TERMINAL_CAUSES = frozenset(
    {
        "acceptance_rejected",
        "authority_amendment_required",
        "build_truncated",
        "cancelled_without_intent",
        "gate_rejected",
        "gate_stalled",
        "interrupted",
        "materialization_failed",
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
        "stop_envelope_publication_failure",
        "terminal_service_error",
        "typed_stop_selected",
        "usage_limit",
    }
)
_TERMINAL_CAUSES = TERMINAL_CAUSES

#: Named here only so the most common miswrite is legible in its own rejection. The
#: values are ``StopEnvelope``'s and are imported rather than restated: this module
#: exists to stop one closed vocabulary being mistaken for another, and a second copy
#: of one of them is that same defect one level down. The first draft of HIR-0227
#: restated them, which the duplicate scanner then found in HIR-0227 itself.
_STOP_CLASSES = STOP_CLASSES


class _DuplicateJsonKey(ValueError):
    """A JSON object contains two values for one authority field."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateJsonKey(key)
        value[key] = item
    return value


#: Causes an operator produced deliberately. The boundary still violated the invariant
#: -- a recorded signal should have become an interruption receipt, not an unclassified
#: stop -- so these remain harness defects. They are named so an operator action is at
#: least distinguishable from a code fault in the identity, rather than sharing one
#: finding id with four of them (HIR-0214).
OPERATOR_CAUSES = frozenset({"interrupted", "requested_exit"})


def closed_terminal_cause(value: str) -> str:
    """Normalise arbitrary *observed* metadata for identity.

    Lenient on purpose: the input may be exception metadata from any historical
    record, and unknown prose must not enter action identity.  It is the wrong
    function for a value this repository authors -- there a typo would degrade
    silently into a plausible-looking record.  Author with
    :func:`require_terminal_cause` instead (HIR-0227).
    """

    return value if value in TERMINAL_CAUSES else "unclassified_terminal_cause"


def plan_outcome_terminal_cause(outcome: str) -> str:
    """The one derivation of a plan loop outcome's terminal cause.

    `PlanGateFailure` and `terminal_record` both need it.  When they each had their
    own, the exception's answer was a stop class and the boundary's was a cause, and
    the exception's is the one that reached the operator (HIR-0227).
    """

    return {
        "stalled": "gate_stalled",
        "budget": "plan_budget_exhausted",
    }.get(str(outcome), "gate_rejected")


def require_terminal_cause(value: object, field: str) -> str:
    """Accept only a member of the closed vocabulary, naming the accepted set.

    A ``StopEnvelope.stop_class`` is the value this most often receives by mistake:
    it answers "who owns this stop", not "why did the run end", and the two
    vocabularies share no members (HIR-0227).
    """

    text = str(value or "").strip()
    if text in TERMINAL_CAUSES:
        return text
    hint = (
        " -- that is a StopEnvelope.stop_class, which says who owns the stop, "
        "not why the run ended"
        if text in _STOP_CLASSES
        else ""
    )
    raise ValueError(
        f"{field} must be one of {sorted(TERMINAL_CAUSES)}; found {text!r}{hint}"
    )


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
        value = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except _DuplicateJsonKey:
        audit["raw_text"] = text
        return audit, None, "duplicate_json_key"
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


def _selected_bundle(
    shot: Path,
) -> tuple[dict[str, Any], dict[str, Any], int | None]:
    pointer_audit, raw_pointer, read_issue = _read_json(shot, shot / _PLAN_POINTER)
    audit: dict[str, Any] = {"pointer": pointer_audit}
    if read_issue == "missing":
        return {"selection": "absent"}, audit, None
    if read_issue is not None:
        return (
            {"selection": "invalid", "issues": [f"pointer_{read_issue}"]},
            audit,
            None,
        )
    if not isinstance(raw_pointer, Mapping):
        return {"selection": "invalid", "issues": ["pointer_not_object"]}, audit, None

    issues: list[str] = []
    if set(raw_pointer) != _PLAN_POINTER_FIELDS:
        issues.append("pointer_fields_mismatch")
    if raw_pointer.get("schema") != _PLAN_POINTER_SCHEMA:
        issues.append("pointer_schema_unsupported")
    revision = raw_pointer.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision <= 0:
        issues.append("pointer_revision_invalid")
        revision = None
    bundle_digest = raw_pointer.get("content_hash")
    if not _is_digest(bundle_digest):
        issues.append("pointer_bundle_digest_invalid")
    outcome = raw_pointer.get("outcome")
    if outcome not in _PUBLISHABLE_OUTCOMES:
        issues.append("pointer_outcome_invalid")
    run_id = raw_pointer.get("run_id")
    if (
        not isinstance(run_id, str)
        or not run_id
        or run_id in {".", ".."}
        or "/" in run_id
        or "\\" in run_id
    ):
        issues.append("pointer_publisher_identity_invalid")
    published_at = raw_pointer.get("published_at")
    if not isinstance(published_at, str) or not published_at.strip():
        issues.append("pointer_published_at_invalid")

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
                or relative.parts[0] != run_id
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
        if set(raw_manifest) != {
            "schema",
            "run_id",
            "content_hash",
            "outcome",
            "artifacts",
        }:
            issues.append("manifest_fields_mismatch")
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
        "outcome": outcome if outcome in _PUBLISHABLE_OUTCOMES else None,
    }
    if issues:
        semantic["issues"] = sorted(set(issues))
    else:
        semantic["artifacts"] = artifact_digests
    return semantic, audit, revision


def _selected_view(
    shot: Path,
    selected_bundle: Mapping[str, Any],
    plan_revision: int | None,
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
    issues: list[str] = []
    if set(raw_pointer) != _JIT_POINTER_FIELDS:
        issues.append("pointer_fields_mismatch")
    if raw_pointer.get("schema") != _JIT_VIEW_SCHEMA:
        issues.append("pointer_schema_unsupported")
    revision = raw_pointer.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision <= 0:
        issues.append("pointer_revision_invalid")
    observed_plan_revision = raw_pointer.get("plan_revision")
    if (
        not isinstance(observed_plan_revision, int)
        or isinstance(observed_plan_revision, bool)
        or observed_plan_revision <= 0
    ):
        issues.append("pointer_plan_revision_invalid")
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
        or set(raw_artifacts) != _OVERLAY_ARTIFACTS
        or not isinstance(raw_hashes, Mapping)
        or set(raw_hashes) != _OVERLAY_ARTIFACTS
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
        if (
            not isinstance(relative, str)
            or not relative
            or relative != relative.strip()
            or PurePosixPath(relative).is_absolute()
            or ".." in PurePosixPath(relative).parts
            or PurePosixPath(relative).as_posix() != relative
        ):
            issues.append("view_artifact_locator_invalid")
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

    layers_document = documents.get("layers.json")
    derived_layers: list[str] = []
    if isinstance(layers_document, Mapping) and layers_document.get("schema") in {4, 5}:
        rows = layers_document.get("layers")
        if isinstance(rows, list) and all(isinstance(row, Mapping) for row in rows):
            for row in rows:
                layer_id = row.get("id")
                if not isinstance(layer_id, str) or not layer_id or layer_id != layer_id.strip():
                    issues.append("view_layers_document_invalid")
                    break
                if row.get("execution") != "jit_deferred":
                    derived_layers.append(layer_id)
            if len(derived_layers) != len(set(derived_layers)):
                issues.append("view_layers_document_invalid")
        else:
            issues.append("view_layers_document_invalid")
    else:
        issues.append("view_layers_document_invalid")
    if not issues and materialized != sorted(derived_layers):
        issues.append("pointer_materialized_layers_mismatch")

    if selected_bundle.get("selection") != "verified" or plan_revision is None:
        issues.append("selected_plan_unavailable")

    semantic = {
        "selection": "invalid" if issues else "verified",
        "bundle_digest": observed_bundle if _is_digest(observed_bundle) else None,
        "view_digest": observed_view if _is_digest(observed_view) else None,
    }
    if issues:
        semantic["issues"] = sorted(set(issues))
    elif observed_bundle != bundle_digest or observed_plan_revision != plan_revision:
        semantic = {
            "selection": "superseded",
            "observed_bundle_digest": observed_bundle,
            "observed_view_digest": observed_view,
        }
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
    selected_bundle, bundle_audit, plan_revision = _selected_bundle(shot)
    selected_view, view_audit = _selected_view(
        shot,
        selected_bundle,
        plan_revision,
    )
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
