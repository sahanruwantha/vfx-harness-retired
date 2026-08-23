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

from vfx_harness.observability.run_artifacts import RunLayout

POINTER_SCHEMA = "vfx-harness.plan-pointer/v1"
BUNDLE_SCHEMA = "vfx-harness.plan-bundle/v1"
WORKSPACE_SCHEMA = "vfx-harness.plan-workspace/v1"
PROVENANCE_SCHEMA = "vfx-harness.plan-provenance/v1"
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
_INPUT_VERIFICATIONS: set[tuple[str, str, tuple[tuple[str, int, int], ...]]] = set()
CONSUMER_VIEW_SCHEMA = "vfx-harness.plan-consumer-view/v1"


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


class PlanPublicationError(RuntimeError):
    """The candidate plan set cannot become authoritative."""


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


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _bundle_hash(payloads: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name in sorted(payloads):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_digest(payloads[name]).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _authored_paths(root: Path) -> list[Path]:
    paths = [root / "brief.md"]
    paths.extend(sorted(path for path in (root / "refs").rglob("*") if path.is_file()))
    return paths


def _authored_inputs(root: Path) -> dict[str, str]:
    """Content identity that selected authority must continue to match."""
    paths = _authored_paths(root)
    return {
        path.relative_to(root).as_posix(): _digest(path.read_bytes())
        for path in paths
    }


def _decision_inputs(root: Path) -> dict[str, str]:
    """Append-only cross-run decisions folded into a new planning transaction."""
    paths = [
        path
        for path in (root / "plan_amendments.jsonl", root / "state/plan-resolutions.jsonl")
        if path.is_file()
    ]
    return {path.relative_to(root).as_posix(): _digest(path.read_bytes()) for path in paths}


def _input_signature(root: Path) -> tuple[tuple[str, int, int], ...]:
    paths = _authored_paths(root)
    return tuple(
        (path.relative_to(root).as_posix(), path.stat().st_size, path.stat().st_mtime_ns)
        for path in paths
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
    if workspace.exists():
        try:
            record = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PlanPublicationError(f"existing plan workspace is unowned: {workspace}") from exc
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

    brief = layout.shot / "brief.md"
    refs = layout.shot / "refs"
    if not brief.is_file() or not refs.is_dir():
        raise PlanPublicationError("plan staging requires authored brief.md and refs/")
    workspace.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix=".plan-workspace.tmp-", dir=workspace.parent))
    try:
        shutil.copy2(brief, temp / "brief.md")
        target_refs = temp / "refs"
        target_refs.mkdir()
        for source in sorted(path for path in refs.rglob("*") if path.is_file()):
            relative = source.relative_to(refs)
            target = target_refs / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            # A hard link would make a staged Edit mutate the authored reference inode.
            # Freshness requires byte isolation as well as path isolation.
            shutil.copy2(source, target)
        for source in (layout.shot / "plan_amendments.jsonl", layout.shot / "state/plan-resolutions.jsonl"):
            if not source.is_file():
                continue
            target = temp / source.relative_to(layout.shot)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
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
    payloads = {name: (source_root / rel).read_bytes() for name, rel in sources.items()}
    marker = source_root / ".plan-workspace.json"
    if marker.is_file():
        try:
            workspace = json.loads(marker.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise PlanPublicationError("plan workspace marker is unreadable") from exc
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
    content_hash = _bundle_hash(payloads)
    parent = layout.checkpoints / "plans" / "bundles"
    parent.mkdir(parents=True, exist_ok=True)
    root = _bundle_root(layout, content_hash)
    artifact_hashes = {name: _digest(data) for name, data in sorted(payloads.items())}
    manifest = {
        "schema": BUNDLE_SCHEMA,
        "run_id": layout.run_id,
        "content_hash": content_hash,
        "outcome": outcome,
        "artifacts": artifact_hashes,
    }

    if root.exists():
        try:
            existing = json.loads((root / "bundle.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PlanPublicationError(f"existing plan bundle is unreadable: {root}") from exc
        if existing != manifest:
            raise PlanPublicationError(f"immutable plan bundle conflicts with candidate: {root}")
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

    return PlanBundle(
        shot=layout.shot,
        run_id=layout.run_id,
        root=root.resolve(),
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
    if outcome not in {"clean", "clean_with_assumptions", "clean_with_deferred"}:
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
    _atomic_json(shot / POINTER, pointer)
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
    from vfx_harness.evaluation import plan_gate
    from vfx_harness.observability import run_artifacts

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


def _resolve_pointer(shot: Path, pointer: dict[str, Any]) -> PlanBundle:
    """Verify one explicitly identified bundle using the same rules as current authority."""
    if pointer.get("schema") != POINTER_SCHEMA:
        raise PlanPublicationError(f"unsupported plan pointer schema: {pointer.get('schema')!r}")
    root = (shot / str(pointer.get("bundle") or "")).resolve()
    try:
        rel = root.relative_to(shot / "runs")
    except ValueError as exc:
        raise PlanPublicationError("plan pointer escapes the shot run store") from exc
    if len(rel.parts) != 5 or rel.parts[1:4] != ("checkpoints", "plans", "bundles"):
        raise PlanPublicationError(f"plan pointer targets a non-bundle path: {root}")
    try:
        manifest = json.loads((root / "bundle.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanPublicationError(f"plan bundle is missing or unreadable: {root}") from exc
    if manifest.get("schema") != BUNDLE_SCHEMA:
        raise PlanPublicationError(f"unsupported plan bundle schema: {manifest.get('schema')!r}")
    if manifest.get("content_hash") != pointer.get("content_hash"):
        raise PlanPublicationError("plan pointer and bundle content hashes disagree")
    if manifest.get("run_id") != pointer.get("run_id") or rel.parts[0] != pointer.get("run_id"):
        raise PlanPublicationError("plan pointer and bundle run ids disagree")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts:
        raise PlanPublicationError("plan bundle carries no artifact manifest")
    expected_artifacts = set(_SOURCES) | _GENERATED_ARTIFACTS
    missing = sorted(expected_artifacts - set(artifacts))
    unsupported = sorted(name for name in artifacts if not _is_supported_artifact(name))
    if missing or unsupported:
        detail = []
        if missing:
            detail.append("missing " + ", ".join(missing))
        if unsupported:
            detail.append("unsupported " + ", ".join(unsupported))
        raise PlanPublicationError("plan bundle artifact set is incomplete: " + "; ".join(detail))
    payloads: dict[str, bytes] = {}
    for name, expected in artifacts.items():
        if not _is_supported_artifact(name):
            raise PlanPublicationError(f"plan bundle declares unsupported artifact: {name}")
        path = root / name
        if not path.is_file():
            raise PlanPublicationError(f"plan bundle artifact is missing: {name}")
        data = path.read_bytes()
        if _digest(data) != expected:
            raise PlanPublicationError(f"plan bundle artifact hash mismatch: {name}")
        payloads[name] = data
    if _bundle_hash(payloads) != pointer.get("content_hash"):
        raise PlanPublicationError("plan bundle aggregate hash mismatch")
    try:
        provenance = json.loads((root / "plan.provenance.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanPublicationError("plan bundle provenance is missing or unreadable") from exc
    if provenance.get("schema") != PROVENANCE_SCHEMA:
        raise PlanPublicationError("plan bundle provenance schema is unsupported")
    authored = provenance.get("authored_inputs")
    signature = _input_signature(shot)
    verification = (str(shot), str(pointer["content_hash"]), signature)
    if verification not in _INPUT_VERIFICATIONS:
        if not isinstance(authored, dict) or authored != _authored_inputs(shot):
            raise PlanPublicationError("published plan was derived from different authored inputs")
        _INPUT_VERIFICATIONS.add(verification)
    return PlanBundle(
        shot=shot,
        run_id=str(pointer["run_id"]),
        root=root,
        content_hash=str(pointer["content_hash"]),
        artifacts=tuple(sorted(artifacts)),
        outcome=str(pointer.get("outcome") or manifest.get("outcome") or ""),
    )


def resolve_current(shot_folder: str | Path) -> PlanBundle:
    """Resolve and verify the complete plan generation selected by ``plans/current.json``."""
    shot = Path(shot_folder).expanduser().resolve()
    pointer_path = shot / POINTER
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanPublicationError(f"plan pointer is missing or unreadable: {pointer_path}") from exc
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
    root = shot / "runs" / safe_run / "checkpoints" / "plans" / "bundles" / safe_hash
    try:
        manifest = json.loads((root / "bundle.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanPublicationError(f"plan bundle is missing or unreadable: {root}") from exc
    return _resolve_pointer(
        shot,
        {
            "schema": POINTER_SCHEMA,
            "run_id": safe_run,
            "bundle": root.relative_to(shot).as_posix(),
            "content_hash": safe_hash,
            "outcome": manifest.get("outcome"),
        },
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


def selected_artifact_path(shot_folder: str | Path, name: str) -> Path:
    """Resolve selected authority, preserving pointer-less legacy fixtures temporarily.

    Once a pointer exists there is deliberately no fallback: malformed, stale, or
    incomplete published authority fails closed. Pointer-less archived fixtures retain a
    bounded migration window until they are republished.
    """
    shot = Path(shot_folder).expanduser().resolve()
    if (shot / POINTER).exists():
        if name in {"layers.json", "scene_checks.json", "checks.json", "requirements.json"}:
            from vfx_harness.orchestration.jit_materialization import selected_view_artifact

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
                if name in {"layers.json", "scene_checks.json", "checks.json", "requirements.json"}
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
        for layer in layers:
            for unit in layer.get("stages") or []:
                rel = Path(str(unit.get("plan") or ""))
                source = layout.shot / rel
                if source.is_file():
                    from vfx_harness.orchestration.layer_plans import (
                        validate_work_unit_plan_authority,
                        work_unit_plan_authority_path,
                    )

                    try:
                        validate_work_unit_plan_authority(layout.shot, source)
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
