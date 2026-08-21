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
POINTER = Path("plans/current.json")

_SOURCES = {
    "global.md": Path("plans/global.md"),
    "layers.json": Path("layers.json"),
    "acceptance.json": Path("acceptance.json"),
    "critic_axes.json": Path("critic_axes.json"),
    "checks.json": Path("checks.json"),
    "scene_checks.json": Path("scene_checks.json"),
}


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


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def prepare_staging(layout: RunLayout) -> Path:
    """Create one run-scoped planner workspace from authored inputs only.

    Prior plans, contracts, questions, builds, and run reports are deliberately absent. A
    planner that needs prior authority must receive it through an explicit import operation;
    directory proximity is never enough.
    """
    workspace = layout.scratch / "plan-workspace"
    marker = workspace / ".plan-workspace.json"
    if workspace.exists():
        try:
            record = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PlanPublicationError(f"existing plan workspace is unowned: {workspace}") from exc
        if record != {
            "schema": WORKSPACE_SCHEMA,
            "run_id": layout.run_id,
            "shot": str(layout.shot),
        }:
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
        (temp / ".plan-workspace.json").write_text(
            json.dumps(
                {
                    "schema": WORKSPACE_SCHEMA,
                    "run_id": layout.run_id,
                    "shot": str(layout.shot),
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
    missing = [name for name, rel in sources.items() if not (source_root / rel).is_file()]
    if missing:
        raise PlanPublicationError(
            "cannot publish incomplete plan authority; missing " + ", ".join(sorted(missing))
        )
    return {name: (source_root / rel).read_bytes() for name, rel in sources.items()}


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
                (temp / name).write_bytes(data)
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


def resolve_current(shot_folder: str | Path) -> PlanBundle:
    """Resolve and verify the complete plan generation selected by ``plans/current.json``."""
    shot = Path(shot_folder).expanduser().resolve()
    pointer_path = shot / POINTER
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanPublicationError(f"plan pointer is missing or unreadable: {pointer_path}") from exc
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
    payloads: dict[str, bytes] = {}
    for name, expected in artifacts.items():
        if name not in _SOURCES:
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
    return PlanBundle(
        shot=shot,
        run_id=str(pointer["run_id"]),
        root=root,
        content_hash=str(pointer["content_hash"]),
        artifacts=tuple(sorted(artifacts)),
        outcome=str(pointer.get("outcome") or manifest.get("outcome") or ""),
    )


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
