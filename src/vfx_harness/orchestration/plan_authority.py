"""Transactional publication primitives for global plan authority.

Planner agents author in an authored-input-only workspace owned by their current run. A clean
gate may publish that complete surface as one immutable, run-owned bundle. Readers that opt
into the new contract resolve a single atomic pointer and therefore never observe files from
different plan generations.

This module deliberately does not fall back from a malformed pointer to shot-root files.  A
pointer is authority once present; corrupt or incomplete authority fails closed.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from vfx_harness.observability import run_artifacts
from vfx_harness.observability.run_artifacts import RunLayout
from vfx_harness.orchestration import plan_bundle_integrity

POINTER_SCHEMA = "vfx-harness.plan-pointer/v1"
BUNDLE_SCHEMA = "vfx-harness.plan-bundle/v1"
WORKSPACE_SCHEMA = "vfx-harness.plan-workspace/v2"
PROVENANCE_SCHEMA = "vfx-harness.plan-provenance/v2"
POINTER = Path("plans/current.json")

_SOURCES = {
    "global.md": Path("plans/global.md"),
    "layers.json": Path("layers.json"),
    "acceptance.json": Path("acceptance.json"),
    "critic_axes.json": Path("critic_axes.json"),
    "checks.json": Path("checks.json"),
    "scene_checks.json": Path("scene_checks.json"),
    "requirements.json": Path("requirements.json"),
    "obligations.json": Path("obligations.json"),
    "assumptions.json": Path("assumptions.json"),
}
_GENERATED_ARTIFACTS = {"plan.provenance.json"}
CONSUMER_VIEW_SCHEMA = "vfx-harness.plan-consumer-view/v1"
_PUBLISHABLE_OUTCOMES = frozenset({"clean", "clean_with_assumptions", "clean_with_deferred"})
_POINTER_FIELDS = frozenset(
    {"schema", "run_id", "bundle", "content_hash", "outcome", "published_at"}
)
_PROVENANCE_FIELDS = frozenset({"schema", "authored_inputs", "decision_inputs"})
_WORKSPACE_FIELDS = frozenset(
    {"schema", "run_id", "shot", "authored_inputs", "decision_inputs"}
)
_DECISION_INPUT_FIELDS = frozenset({"size", "sha256"})
_DECISION_INPUT_PATHS = (
    Path("plan_amendments.jsonl"),
    Path("state/plan-resolutions.jsonl"),
)
PlanPublicationError = plan_bundle_integrity.PlanPublicationError


def _supplemental_plan_artifacts(source_root: Path) -> dict[str, Path]:
    """Return nested plans and harness-deposited plan evidence.

    ``global.md`` remains the canonical top-level member for compatibility. Everything
    else below ``plans/`` is preserved at its workspace-relative path so citations and
    pre-authored ready-unit plans resolve identically before and after publication.
    """
    plans = source_root / "plans"
    if not plans.is_dir():
        return {}
    artifacts = {
        path.relative_to(source_root).as_posix(): path.relative_to(source_root)
        for path in sorted(plans.rglob("*.md"))
        if path.is_file() and path != plans / "global.md"
    }
    # The compact mapping is the source document the whole surface was expanded from;
    # bundles carry it for provenance (the *.md glob predates it).
    mapping = plans / "ownership_mapping.json"
    if mapping.is_file():
        artifacts[mapping.relative_to(source_root).as_posix()] = mapping.relative_to(
            source_root
        )
    evidence = plans / "evidence"
    if evidence.is_dir():
        artifacts.update({
            path.relative_to(source_root).as_posix(): path.relative_to(source_root)
            for path in sorted(evidence.rglob("*"))
            if path.is_file() and path.suffix in {".md", ".png"}
        })
    return artifacts


def _is_supported_artifact(name: str) -> bool:
    if name in _SOURCES or name in _GENERATED_ARTIFACTS:
        return True
    # the compact ownership mapping is the source document the plan surface is expanded
    # from; the publisher seals it for provenance, so the resolver must read it back
    # (run 20260824T150358Z-3bc39c published the first mapping-carrying bundle and every
    # consumer failed closed on "unsupported plans/ownership_mapping.json")
    if name == "plans/ownership_mapping.json":
        return True
    path = Path(name)
    nested_markdown = (
        not path.is_absolute()
        and ".." not in path.parts
        and len(path.parts) >= 2
        and path.parts[0] == "plans"
        and path.suffix == ".md"
        and name != "plans/global.md"
    )
    plan_evidence = (
        not path.is_absolute()
        and ".." not in path.parts
        and len(path.parts) >= 3
        and path.parts[:2] == ("plans", "evidence")
        and path.suffix in {".md", ".png"}
    )
    return nested_markdown or plan_evidence


@dataclass(frozen=True, slots=True)
class PlanBundle:
    shot: Path
    run_id: str
    root: Path
    content_hash: str
    artifacts: tuple[str, ...]
    outcome: str


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _verify_bundle_root(
    shot: Path,
    root: Path,
    *,
    expected_manifest: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Verify an immutable bundle completely before it can be returned or selected."""

    return plan_bundle_integrity.verify_bundle_root(
        shot,
        root,
        schema=BUNDLE_SCHEMA,
        publishable_outcomes=_PUBLISHABLE_OUTCOMES,
        supports_artifact=_is_supported_artifact,
        expected_manifest=expected_manifest,
    )


def _validate_current_pointer(pointer: dict[str, Any]) -> tuple[str, Path, str, str]:
    found = set(pointer)
    if found != _POINTER_FIELDS:
        raise PlanPublicationError(
            f"plan pointer fields mismatch; missing={sorted(_POINTER_FIELDS - found)}; "
            f"unexpected={sorted(found - _POINTER_FIELDS)}"
        )
    if pointer["schema"] != POINTER_SCHEMA:
        raise PlanPublicationError(f"unsupported plan pointer schema: {pointer['schema']!r}")
    run_id = pointer["run_id"]
    if (
        not isinstance(run_id, str)
        or not run_id
        or run_id in {".", ".."}
        or "/" in run_id
        or "\\" in run_id
    ):
        raise PlanPublicationError(f"plan pointer run id is invalid: {run_id!r}")
    content_hash = pointer["content_hash"]
    if not plan_bundle_integrity.is_digest(content_hash):
        raise PlanPublicationError("plan pointer content hash must be a lowercase SHA-256 digest")
    outcome = pointer["outcome"]
    if outcome not in _PUBLISHABLE_OUTCOMES:
        raise PlanPublicationError(f"plan pointer outcome is not publishable: {outcome!r}")
    if not isinstance(pointer["published_at"], str) or not pointer["published_at"].strip():
        raise PlanPublicationError("plan pointer published_at must be a non-empty string")
    bundle_value = pointer["bundle"]
    if not isinstance(bundle_value, str) or not bundle_value:
        raise PlanPublicationError("plan pointer bundle must be a non-empty relative path")
    bundle = Path(bundle_value)
    expected = Path("runs") / run_id / "checkpoints" / "plans" / "bundles" / content_hash
    if bundle.is_absolute() or bundle.as_posix() != bundle_value or bundle != expected:
        raise PlanPublicationError(
            "plan pointer bundle must name its exact run-owned content-addressed root"
        )
    return run_id, bundle, content_hash, outcome


def _authored_input_bytes(root: Path) -> dict[str, bytes]:
    brief = root / "brief.md"
    inputs = {
        "brief.md": plan_bundle_integrity.read_real_file(
            root,
            brief,
            "authored brief input",
        )
    }
    for path in plan_bundle_integrity.regular_files_under(
        root,
        root / "refs",
        "authored refs input",
    ):
        inputs[path.relative_to(root).as_posix()] = plan_bundle_integrity.read_real_file(
            root,
            path,
            "authored reference input",
        )
    return inputs


def _authored_inputs(root: Path) -> dict[str, str]:
    """Content identity that selected authority must continue to match."""

    return {
        name: plan_bundle_integrity.digest(data)
        for name, data in _authored_input_bytes(root).items()
    }


def _decision_input_bytes(root: Path) -> dict[str, bytes]:
    inputs: dict[str, bytes] = {}
    for relative in _DECISION_INPUT_PATHS:
        path = root / relative
        if path.parent != root and (
            path.parent.is_symlink()
            or (path.parent.exists() and not path.parent.is_dir())
        ):
            plan_bundle_integrity.require_real_directory(
                root,
                path.parent,
                "planning decision input parent",
            )
        if not path.exists() and not path.is_symlink():
            continue
        inputs[relative.as_posix()] = plan_bundle_integrity.read_real_file(
            root,
            path,
            "planning decision input",
        )
    return inputs


def _decision_inputs(root: Path) -> dict[str, dict[str, str | int]]:
    """Append-only cross-run decisions folded into a new planning transaction."""

    return {
        name: {"size": len(data), "sha256": plan_bundle_integrity.digest(data)}
        for name, data in _decision_input_bytes(root).items()
    }


def _verify_decision_inputs(root: Path, expected: Any) -> None:
    if not isinstance(expected, dict):
        raise PlanPublicationError("plan bundle decision-input provenance must be an object")
    allowed = {path.as_posix() for path in _DECISION_INPUT_PATHS}
    unsupported = sorted(name for name in expected if name not in allowed)
    if unsupported:
        raise PlanPublicationError(
            "plan bundle decision-input provenance names unsupported paths: "
            + ", ".join(unsupported)
        )
    live = _decision_input_bytes(root)
    for name, assertion in expected.items():
        if not isinstance(assertion, dict) or set(assertion) != _DECISION_INPUT_FIELDS:
            raise PlanPublicationError(
                f"plan bundle decision-input provenance fields are invalid: {name}"
            )
        size = assertion["size"]
        expected_hash = assertion["sha256"]
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise PlanPublicationError(
                f"plan bundle decision-input provenance size is invalid: {name}"
            )
        if not plan_bundle_integrity.is_digest(expected_hash):
            raise PlanPublicationError(
                f"plan bundle decision-input provenance hash is invalid: {name}"
            )
        data = live.get(name)
        if data is None or len(data) < size:
            raise PlanPublicationError(
                "published plan was derived from missing or truncated planning decisions"
            )
        if plan_bundle_integrity.digest(data[:size]) != expected_hash:
            raise PlanPublicationError("published plan was derived from different planning decisions")


def _read_workspace_marker(anchor: Path, marker: Path) -> dict[str, Any]:
    return plan_bundle_integrity.read_schema_object(
        anchor,
        marker,
        "plan workspace marker",
        schema=WORKSPACE_SCHEMA,
        fields=_WORKSPACE_FIELDS,
    )


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def prepare_staging(layout: RunLayout) -> Path:
    """Create one run-scoped planner workspace from explicit planning inputs only.

    Prior plans, contracts, questions, builds, and run reports are deliberately absent.
    Append-only amendments and plan resolutions are explicit cross-run inputs: unlike directory
    proximity, their hashes become part of the transaction's provenance identity.
    """
    workspace = layout.scratch / "plan-workspace"
    marker = workspace / ".plan-workspace.json"
    if workspace.exists() or workspace.is_symlink():
        record = _read_workspace_marker(layout.scratch, marker)
        identity = {
            "schema": WORKSPACE_SCHEMA,
            "run_id": layout.run_id,
            "shot": str(layout.shot),
            "authored_inputs": _authored_inputs(workspace),
            "decision_inputs": _decision_inputs(workspace),
        }
        if record != identity:
            raise PlanPublicationError(f"plan workspace ownership mismatch: {workspace}")
        return workspace

    authored = _authored_input_bytes(layout.shot)
    decisions = _decision_input_bytes(layout.shot)
    workspace.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix=".plan-workspace.tmp-", dir=workspace.parent))
    try:
        (temp / "brief.md").write_bytes(authored["brief.md"])
        target_refs = temp / "refs"
        target_refs.mkdir()
        for name, data in authored.items():
            if name == "brief.md":
                continue
            target = temp / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        for name, data in decisions.items():
            target = temp / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        (temp / ".plan-workspace.json").write_text(
            json.dumps(
                {
                    "schema": WORKSPACE_SCHEMA,
                    "run_id": layout.run_id,
                    "shot": str(layout.shot),
                    "authored_inputs": _authored_inputs(temp),
                    "decision_inputs": _decision_inputs(temp),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        os.replace(temp, workspace)
    except BaseException:
        shutil.rmtree(temp, ignore_errors=True)
        raise
    return workspace


def _payloads(source_root: Path, plan_path: Path | None) -> dict[str, bytes]:
    sources = dict(_SOURCES)
    if plan_path is not None:
        sources["global.md"] = plan_path.resolve().relative_to(source_root)
    sources.update(_supplemental_plan_artifacts(source_root))
    missing = [name for name, rel in sources.items() if not (source_root / rel).is_file()]
    if missing:
        raise PlanPublicationError(
            "cannot publish incomplete plan authority; missing " + ", ".join(sorted(missing))
        )
    # Publication and resolution must agree on membership: sealing an artifact the
    # resolver refuses publishes authority no consumer can read (writer/reader
    # asymmetry, the HIR-0016 class). Fail at the transaction boundary instead.
    unsupported = sorted(name for name in sources if not _is_supported_artifact(name))
    if unsupported:
        raise PlanPublicationError(
            "cannot publish artifacts current authority resolution does not support: "
            + ", ".join(unsupported)
        )
    payloads = {name: (source_root / rel).read_bytes() for name, rel in sources.items()}
    marker = source_root / ".plan-workspace.json"
    if marker.exists() or marker.is_symlink():
        workspace = _read_workspace_marker(source_root, marker)
        inputs = workspace.get("authored_inputs")
        if not isinstance(inputs, dict) or inputs != _authored_inputs(source_root):
            raise PlanPublicationError("authored planning inputs changed during the transaction")
        decisions = workspace.get("decision_inputs")
        if not isinstance(decisions, dict) or decisions != _decision_inputs(source_root):
            raise PlanPublicationError("planning decision inputs changed during the transaction")
    else:
        inputs = _authored_inputs(source_root)
        decisions = _decision_inputs(source_root)
    payloads["plan.provenance.json"] = (
        json.dumps(
            {
                "schema": PROVENANCE_SCHEMA,
                "authored_inputs": inputs,
                "decision_inputs": decisions,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    return payloads


def _bundle_root(layout: RunLayout, content_hash: str) -> Path:
    return layout.checkpoints / "plans" / "bundles" / content_hash


def _write_bundle(layout: RunLayout, payloads: dict[str, bytes], *, outcome: str) -> PlanBundle:
    content_hash = plan_bundle_integrity.bundle_hash(payloads)
    parent = layout.checkpoints / "plans" / "bundles"
    plan_bundle_integrity.ensure_real_directories(layout.shot, parent, "plan bundle store")
    root = _bundle_root(layout, content_hash)
    artifact_hashes = {
        name: plan_bundle_integrity.digest(data) for name, data in sorted(payloads.items())
    }
    manifest = {
        "schema": BUNDLE_SCHEMA,
        "run_id": layout.run_id,
        "content_hash": content_hash,
        "outcome": outcome,
        "artifacts": artifact_hashes,
    }

    if root.exists() or root.is_symlink():
        _verify_bundle_root(layout.shot, root, expected_manifest=manifest)
    else:
        temp = Path(tempfile.mkdtemp(prefix=f".{content_hash}.tmp-", dir=parent))
        try:
            for name, data in payloads.items():
                target = temp / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            (temp / "bundle.json").write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            os.replace(temp, root)
        except BaseException:
            shutil.rmtree(temp, ignore_errors=True)
            raise
        _verify_bundle_root(layout.shot, root, expected_manifest=manifest)

    return PlanBundle(
        shot=layout.shot,
        run_id=layout.run_id,
        root=root,
        content_hash=content_hash,
        artifacts=tuple(sorted(payloads)),
        outcome=outcome,
    )


def publish_current(
    shot_folder: str | Path,
    layout: RunLayout,
    *,
    outcome: str,
    plan_path: str | Path | None = None,
    source_root: str | Path | None = None,
) -> PlanBundle:
    """Freeze a complete candidate and atomically select it as current authority.

    All candidate bytes are read before any bundle or pointer mutation.  A missing artifact
    therefore leaves the previously published pointer byte-for-byte unchanged.
    """
    shot = Path(shot_folder).expanduser().resolve()
    if layout.shot != shot:
        raise PlanPublicationError("run layout belongs to a different shot")
    if outcome not in _PUBLISHABLE_OUTCOMES:
        raise PlanPublicationError(f"non-publishable plan outcome: {outcome!r}")
    source = Path(source_root).expanduser().resolve() if source_root is not None else shot
    if source != shot:
        expected = (layout.scratch / "plan-workspace").resolve()
        if source != expected:
            raise PlanPublicationError(
                "run-scoped plan publication must use the producing run's plan workspace"
            )
        # Revalidate the marker even when a caller retained the path from an earlier step.
        # A renamed or replaced directory must not be accepted merely because its path fits.
        prepare_staging(layout)
    candidate_path = Path(plan_path).expanduser().resolve() if plan_path is not None else None
    if candidate_path is not None:
        try:
            candidate_path.relative_to(source)
        except ValueError as exc:
            raise PlanPublicationError("plan path escapes the publication source") from exc
    payloads = _payloads(source, candidate_path)
    bundle = _write_bundle(layout, payloads, outcome=outcome)
    pointer = {
        "schema": POINTER_SCHEMA,
        "run_id": bundle.run_id,
        "bundle": bundle.root.relative_to(shot).as_posix(),
        "content_hash": bundle.content_hash,
        "outcome": outcome,
        "published_at": _now(),
    }
    pointer_path = shot / POINTER
    plan_bundle_integrity.ensure_real_directories(
        shot,
        pointer_path.parent,
        "plan pointer parent",
    )
    if pointer_path.is_symlink():
        raise PlanPublicationError(f"plan pointer must not be a symlink: {pointer_path}")
    if pointer_path.exists() and not pointer_path.is_file():
        raise PlanPublicationError(f"plan pointer must be a regular file: {pointer_path}")
    _atomic_json(pointer_path, pointer)
    return bundle


def promote_candidate(
    shot_folder: str | Path,
    source_run_id: str,
    layout: RunLayout,
) -> tuple[PlanBundle, Any, Path]:
    """Revalidate and publish a retained candidate without invoking a model.

    Promotion is a new run-owned transaction, never a mutation of the source run. It is
    allowed only when the source workspace still matches current authored inputs, all
    candidate authority can be copied into this run's isolated workspace, and the current
    deterministic gate clears that copied surface.
    """
    # The gate imports authority readers; promotion invokes it only after publication setup.
    from vfx_harness.evaluation import plan_gate  # noqa: PLC0415

    shot = Path(shot_folder).expanduser().resolve()
    source_layout = run_artifacts.select(shot, source_run_id)
    if source_layout is None:
        raise PlanPublicationError(f"source planning run does not exist: {source_run_id}")
    try:
        source_status = json.loads(source_layout.status.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanPublicationError("source planning run has no readable terminal status") from exc
    if source_status.get("state") != "passed" or source_status.get("outcome") not in {
        "clean", "clean_with_assumptions", "clean_with_deferred",
    }:
        raise PlanPublicationError("only a terminal gate-clean planning run can be promoted")

    source = prepare_staging(source_layout)
    marker = json.loads((source / ".plan-workspace.json").read_text(encoding="utf-8"))
    if marker.get("authored_inputs") != _authored_inputs(shot):
        raise PlanPublicationError("source candidate was derived from different authored inputs")
    if marker.get("decision_inputs") != _decision_inputs(shot):
        raise PlanPublicationError("source candidate was derived from different planning decisions")

    target = prepare_staging(layout)
    sources = dict(_SOURCES)
    sources.update(_supplemental_plan_artifacts(source))
    missing = [name for name, rel in sources.items() if not (source / rel).is_file()]
    if missing:
        raise PlanPublicationError(
            "cannot promote incomplete candidate authority; missing " + ", ".join(sorted(missing))
        )
    for rel in sources.values():
        destination = target / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / rel, destination)

    result = plan_gate.run(target, require_scene_checks=True)
    result.shot = shot.name
    outcome = result.publishable_outcome if result.clean else "blocked"
    layout.write_report("plan_gate", result.to_dict(outcome=outcome))
    if not result.clean:
        raise PlanPublicationError(
            "retained candidate does not pass the current deterministic gate:\n"
            + plan_gate.feedback(result)
        )
    bundle = publish_current(
        shot,
        layout,
        outcome=result.publishable_outcome,
        source_root=target,
    )
    return bundle, result, target


def _resolve_bundle(
    shot: Path,
    *,
    run_id: str,
    bundle_relative: Path,
    content_hash: str,
    pointer_outcome: str | None,
) -> PlanBundle:
    root = shot / bundle_relative
    manifest, payloads = _verify_bundle_root(shot, root)
    if manifest["content_hash"] != content_hash:
        raise PlanPublicationError("plan pointer and bundle content hashes disagree")
    if manifest["run_id"] != run_id:
        raise PlanPublicationError("plan pointer and bundle run ids disagree")
    if pointer_outcome is not None and manifest["outcome"] != pointer_outcome:
        raise PlanPublicationError("plan pointer and bundle outcomes disagree")
    artifacts = manifest["artifacts"]
    expected_artifacts = set(_SOURCES) | _GENERATED_ARTIFACTS
    missing = sorted(expected_artifacts - set(artifacts))
    if missing:
        raise PlanPublicationError(
            "plan bundle artifact set is incomplete: missing " + ", ".join(missing)
        )
    provenance = plan_bundle_integrity.decode_json_object(
        payloads["plan.provenance.json"],
        "plan bundle provenance",
    )
    found = set(provenance)
    if found != _PROVENANCE_FIELDS:
        raise PlanPublicationError(
            "plan bundle provenance fields mismatch; "
            f"missing={sorted(_PROVENANCE_FIELDS - found)}; "
            f"unexpected={sorted(found - _PROVENANCE_FIELDS)}"
        )
    if provenance["schema"] != PROVENANCE_SCHEMA:
        raise PlanPublicationError("plan bundle provenance schema is unsupported")
    authored = provenance["authored_inputs"]
    if not isinstance(authored, dict) or authored != _authored_inputs(shot):
        raise PlanPublicationError("published plan was derived from different authored inputs")
    _verify_decision_inputs(shot, provenance["decision_inputs"])
    return PlanBundle(
        shot=shot,
        run_id=run_id,
        root=root,
        content_hash=content_hash,
        artifacts=tuple(sorted(artifacts)),
        outcome=str(manifest["outcome"]),
    )


def _resolve_pointer(shot: Path, pointer: dict[str, Any]) -> PlanBundle:
    """Verify one selected bundle using the complete current-pointer contract."""

    run_id, bundle_relative, content_hash, outcome = _validate_current_pointer(pointer)
    return _resolve_bundle(
        shot,
        run_id=run_id,
        bundle_relative=bundle_relative,
        content_hash=content_hash,
        pointer_outcome=outcome,
    )


def resolve_current(shot_folder: str | Path) -> PlanBundle:
    """Resolve and verify the complete plan generation selected by ``plans/current.json``."""
    shot = Path(shot_folder).expanduser().resolve()
    pointer_path = shot / POINTER
    pointer, _raw = plan_bundle_integrity.read_json_object(
        shot,
        pointer_path,
        "plan pointer",
    )
    return _resolve_pointer(shot, pointer)


def resolve_published_bundle(
    shot_folder: str | Path,
    *,
    run_id: str,
    content_hash: str,
) -> PlanBundle:
    """Resolve one explicitly named immutable bundle without searching historical runs.

    Administrative transactions use this to prove the exact authority that produced
    durable state before comparing it with the singular current plan.  It deliberately
    requires both identifiers: a content hash alone may legitimately occur in more than
    one publication run.
    """
    shot = Path(shot_folder).expanduser().resolve()
    safe_run = str(run_id).strip()
    safe_hash = str(content_hash).strip()
    if (
        not safe_run
        or safe_run in {".", ".."}
        or "/" in safe_run
        or "\\" in safe_run
    ):
        raise PlanPublicationError(f"invalid plan bundle run id: {run_id!r}")
    if len(safe_hash) != 64 or any(char not in "0123456789abcdef" for char in safe_hash):
        raise PlanPublicationError("plan bundle content hash must be a lowercase SHA-256 digest")
    bundle_relative = (
        Path("runs") / safe_run / "checkpoints" / "plans" / "bundles" / safe_hash
    )
    return _resolve_bundle(
        shot,
        run_id=safe_run,
        bundle_relative=bundle_relative,
        content_hash=safe_hash,
        pointer_outcome=None,
    )


def artifact_path(shot_folder: str | Path, name: str) -> Path:
    """Resolve one verified artifact from the singular current plan generation."""
    if name not in _SOURCES and name not in _GENERATED_ARTIFACTS:
        raise PlanPublicationError(f"unsupported plan artifact: {name}")
    bundle = resolve_current(shot_folder)
    if name not in bundle.artifacts:
        raise PlanPublicationError(f"current plan bundle does not contain required artifact: {name}")
    return bundle.root / name


def authority_root(shot_folder: str | Path) -> Path:
    """Return the verified current bundle root; never fall back to shot-root authority."""
    return resolve_current(shot_folder).root


def _has_selected_pointer(shot: Path) -> bool:
    """Distinguish true absence from a symlink substitution that must fail closed."""

    pointer_path = shot / POINTER
    return (
        pointer_path.exists()
        or pointer_path.is_symlink()
        or pointer_path.parent.is_symlink()
    )


def active_plan_hash(shot_folder: str | Path, *, fallback_root: Path | None = None) -> str:
    """The ONE identity of the active layer DAG: sha256 of the RESOLVED layers.json.

    Under unit-first authority the bundle's sparse layers.json and the materialized
    view's overlay are different documents by design. units-replan hashed the bundle
    while build initialization hashed the view (run 20260825, view 381623c7): every
    rematerialize -> replan -> build sequence dead-ended on the two derivations, each
    correctly refusing the other's hash.

    `fallback_root`: a caller-resolved bundle root used only when the shot carries no
    publication pointer (explicit-bundle administrative transactions on archived
    fixtures); a pointer-carrying shot always resolves through the selected view."""
    shot = Path(shot_folder).expanduser().resolve()
    if fallback_root is not None and not _has_selected_pointer(shot):
        return hashlib.sha256((fallback_root / "layers.json").read_bytes()).hexdigest()
    return hashlib.sha256(
        selected_artifact_path(shot, "layers.json").read_bytes()
    ).hexdigest()


def selected_artifact_path(shot_folder: str | Path, name: str) -> Path:
    """Resolve selected authority, preserving pointer-less legacy fixtures temporarily.

    Once a pointer exists there is deliberately no fallback: malformed, stale, or
    incomplete published authority fails closed. Pointer-less archived fixtures retain a
    bounded migration window until they are republished.
    """
    shot = Path(shot_folder).expanduser().resolve()
    if _has_selected_pointer(shot):
        if name in {
            "layers.json",
            "scene_checks.json",
            "checks.json",
            "requirements.json",
            "acceptance.json",
        }:
            # Materialized-view publication imports authority resolution in the reverse direction.
            from vfx_harness.orchestration.jit_materialization import (  # noqa: PLC0415
                selected_view_artifact,
            )

            bundle = resolve_current(shot)
            overlay = selected_view_artifact(shot, name, bundle.content_hash)
            if overlay is not None:
                return overlay
        return artifact_path(shot, name)
    if name not in _SOURCES:
        raise PlanPublicationError(f"unsupported legacy plan artifact: {name}")
    return shot / _SOURCES[name]


def prepare_consumer_view(layout: RunLayout) -> Path:
    """Materialize a run-scoped read view of one verified bundle plus authored inputs.

    The deterministic gate still expects a folder-shaped candidate. This view gives it
    that interface without copying published bytes back to the shot root or allowing a
    mixture of plan generations.
    """
    bundle = resolve_current(layout.shot)
    view = layout.scratch / "plan-consumer-view"
    temp = Path(tempfile.mkdtemp(prefix=".plan-consumer-view.tmp-", dir=layout.scratch))
    try:
        (temp / "plans").mkdir()
        for name in bundle.artifacts:
            source = (
                selected_artifact_path(layout.shot, name)
                if name
                in {
                    "layers.json",
                    "scene_checks.json",
                    "checks.json",
                    "requirements.json",
                    "acceptance.json",
                }
                else bundle.root / name
            )
            target = temp / "plans" / "global.md" if name == "global.md" else temp / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(source)
        (temp / ".plan-consumer-view.json").write_text(
            json.dumps(
                {
                    "schema": CONSUMER_VIEW_SCHEMA,
                    "shot": str(layout.shot),
                    "bundle": str(bundle.root),
                    "content_hash": bundle.content_hash,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        (temp / "brief.md").symlink_to(layout.shot / "brief.md")
        (temp / "refs").symlink_to(layout.shot / "refs", target_is_directory=True)
        for name in ("plan_amendments.jsonl", "shot.json"):
            source = layout.shot / name
            if source.is_file():
                (temp / name).symlink_to(source)
        state = layout.shot / "state"
        if state.is_dir():
            (temp / "state").symlink_to(state, target_is_directory=True)
        outcomes = layout.shot / "plans" / "outcomes"
        if outcomes.is_dir():
            (temp / "plans" / "outcomes").symlink_to(outcomes, target_is_directory=True)
        try:
            layers = json.loads(
                selected_artifact_path(layout.shot, "layers.json").read_text(encoding="utf-8")
            )["layers"]
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise PlanPublicationError("published layers.json is unreadable") from exc
        # This import is delayed to avoid the plan-authority/layer-plans module cycle,
        # but resolved once per consumer view rather than once per staged unit.
        from vfx_harness.orchestration.layer_plans import (  # noqa: PLC0415
            validate_work_unit_plan_authority,
            work_unit_plan_authority_path,
        )

        for layer in layers:
            for unit in layer.get("stages") or []:
                rel = Path(str(unit.get("plan") or ""))
                source = layout.shot / rel
                if source.is_file():
                    try:
                        # staging feeds the gate, which runs before attestation exists
                        validate_work_unit_plan_authority(layout.shot, source, require_gate=False)
                    except ValueError:
                        continue
                    target = temp / rel
                    if target.exists() or target.is_symlink():
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.symlink_to(source)
                    source_authority = work_unit_plan_authority_path(source)
                    target_authority = work_unit_plan_authority_path(target)
                    if not source_authority.is_file():
                        raise PlanPublicationError(
                            f"JIT unit plan authority sidecar is missing: {source_authority}"
                        )
                    target_authority.symlink_to(source_authority)
        if view.exists():
            previous = view.with_name(view.name + f".old.{os.getpid()}")
            os.replace(view, previous)
            os.replace(temp, view)
            shutil.rmtree(previous)
        else:
            os.replace(temp, view)
    except BaseException:
        shutil.rmtree(temp, ignore_errors=True)
        raise
    return view


def snapshot_repair_input(layout: RunLayout, round_number: int, source: str | Path) -> Path:
    """Preserve one immutable repair input under its producing run."""
    if round_number < 1:
        raise ValueError("repair round must be positive")
    source_path = Path(source)
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    target = layout.checkpoints / "plans" / "snapshots" / f"global.round{round_number}.md"
    data = source_path.read_bytes()
    if target.exists():
        if target.read_bytes() != data:
            raise PlanPublicationError(f"immutable repair snapshot already exists: {target}")
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + f".tmp.{os.getpid()}")
    temp.write_bytes(data)
    os.replace(temp, target)
    return target
