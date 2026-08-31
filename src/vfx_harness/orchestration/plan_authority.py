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

from vfx_harness.domain.authority_head_records import canonical_json_bytes
from vfx_harness.observability import run_artifacts
from vfx_harness.observability.run_artifacts import RunLayout
from vfx_harness.orchestration import plan_bundle_integrity
from vfx_harness.orchestration.authority_selection_transaction import (
    authority_selection_lock,
    durable_remove_pointer,
    durable_replace_pointer_bytes,
    durable_replace_pointer_json,
    durably_ensure_real_directory,
    require_matching_authority_selection_token,
)
from vfx_harness.orchestration.plan_consumer_view import (
    OVERLAY_ARTIFACTS,
    PlanConsumerViewMarker,
)
from vfx_harness.orchestration.plan_inputs import (
    PROVENANCE_SCHEMA,
    workspace_base_selection,
)
from vfx_harness.orchestration.plan_inputs import (
    authored_input_bytes as _authored_input_bytes,
)
from vfx_harness.orchestration.plan_inputs import (
    authored_inputs as _authored_inputs,
)
from vfx_harness.orchestration.plan_inputs import (
    decision_input_bytes as _decision_input_bytes,
)
from vfx_harness.orchestration.plan_inputs import (
    decision_inputs as _decision_inputs,
)
from vfx_harness.orchestration.plan_inputs import (
    exact_planning_input_identity as _exact_planning_input_identity,
)
from vfx_harness.orchestration.plan_inputs import (
    prepare_staging as prepare_staging,
)
from vfx_harness.orchestration.plan_inputs import (
    read_workspace_marker as _read_workspace_marker,
)
from vfx_harness.orchestration.plan_inputs import (
    verify_decision_inputs as _verify_decision_inputs,
)
from vfx_harness.orchestration.plan_pointer import (
    PLAN_POINTER_PATH,
    PLAN_POINTER_SCHEMA,
    PUBLISHABLE_OUTCOMES,
    PlanPointer,
)

POINTER_SCHEMA = PLAN_POINTER_SCHEMA
BUNDLE_SCHEMA = "vfx-harness.plan-bundle/v1"
POINTER = PLAN_POINTER_PATH

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
_PUBLISHABLE_OUTCOMES = PUBLISHABLE_OUTCOMES
_PROVENANCE_FIELDS = frozenset({"schema", "authored_inputs", "decision_inputs"})
PlanPublicationError = plan_bundle_integrity.PlanPublicationError


class PlanSelectionConflict(PlanPublicationError):
    """Global plan publication lost its exact plan/JIT base selection."""


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
        artifacts[mapping.relative_to(source_root).as_posix()] = mapping.relative_to(source_root)
    evidence = plans / "evidence"
    if evidence.is_dir():
        artifacts.update(
            {
                path.relative_to(source_root).as_posix(): path.relative_to(source_root)
                for path in sorted(evidence.rglob("*"))
                if path.is_file() and path.suffix in {".md", ".png"}
            }
        )
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


@dataclass(frozen=True, slots=True)
class SelectedPlanAuthority:
    """One verified plan pointer revision and its immutable bundle."""

    revision: int
    bundle: PlanBundle


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


def _validate_current_pointer(
    pointer: dict[str, Any],
) -> tuple[int, str, Path, str, str]:
    selected = PlanPointer.from_dict(pointer)
    return (
        selected.revision,
        selected.run_id,
        selected.bundle,
        selected.content_hash,
        selected.outcome,
    )


def _payloads(source_root: Path, plan_path: Path | None) -> dict[str, bytes]:
    sources = dict(_SOURCES)
    if plan_path is not None:
        sources["global.md"] = plan_path.resolve().relative_to(source_root)
    sources.update(_supplemental_plan_artifacts(source_root))
    missing = [name for name, rel in sources.items() if not (source_root / rel).is_file()]
    if missing:
        raise PlanPublicationError("cannot publish incomplete plan authority; missing " + ", ".join(sorted(missing)))
    # Publication and resolution must agree on membership: sealing an artifact the
    # resolver refuses publishes authority no consumer can read (writer/reader
    # asymmetry, the HIR-0016 class). Fail at the transaction boundary instead.
    unsupported = sorted(name for name in sources if not _is_supported_artifact(name))
    if unsupported:
        raise PlanPublicationError(
            "cannot publish artifacts current authority resolution does not support: " + ", ".join(unsupported)
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
    artifact_hashes = {name: plan_bundle_integrity.digest(data) for name, data in sorted(payloads.items())}
    manifest = {
        "schema": BUNDLE_SCHEMA,
        "run_id": layout.run_id,
        "content_hash": content_hash,
        "outcome": outcome,
        "artifacts": artifact_hashes,
    }

    if root.exists() or root.is_symlink():
        _verify_bundle_root(layout.shot, root, expected_manifest=manifest)
        plan_bundle_integrity.durably_flush_bundle_directory(layout.shot, root)
    else:
        members = dict(payloads)
        members["bundle.json"] = (
            json.dumps(manifest, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        plan_bundle_integrity.durably_install_bundle_directory(
            layout.shot,
            root,
            members,
        )
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
            raise PlanPublicationError("run-scoped plan publication must use the producing run's plan workspace")
        # Revalidate the marker even when a caller retained the path from an earlier step.
        # A renamed or replaced directory must not be accepted merely because its path fits.
        prepare_staging(layout)
        workspace = _read_workspace_marker(source, source / ".plan-workspace.json")
        base_selection = workspace_base_selection(workspace)
    else:
        # Import at the transaction boundary to avoid the resolver's intentional
        # plan-authority dependency becoming a module cycle.
        from vfx_harness.orchestration.authority_selection import (  # noqa: PLC0415
            resolve_selected_authority,
        )

        base_selection = resolve_selected_authority(shot).selection_token
    candidate_path = Path(plan_path).expanduser().resolve() if plan_path is not None else None
    if candidate_path is not None:
        try:
            candidate_path.relative_to(source)
        except ValueError as exc:
            raise PlanPublicationError("plan path escapes the publication source") from exc
    payloads = _payloads(source, candidate_path)
    bundle = _write_bundle(layout, payloads, outcome=outcome)
    pointer_path = shot / POINTER
    try:
        with authority_selection_lock(shot, exclusive=True):
            # The JIT package imports the plan authority facade; defer the shared
            # two-head parser until this module is fully initialized.
            from vfx_harness.orchestration.authority_selection_heads import (  # noqa: PLC0415
                read_authority_selection_heads,
            )

            heads = read_authority_selection_heads(shot)
            require_matching_authority_selection_token(base_selection, heads.token)
            # The workspace proves what the planner read, but publication must also
            # prove those inputs are still the live authored/decision authority. A
            # concurrent brief or reference edit must leave the old head untouched,
            # never select a bundle every reader will immediately reject.
            _resolve_bundle(
                shot,
                run_id=bundle.run_id,
                bundle_relative=bundle.root.relative_to(shot),
                content_hash=bundle.content_hash,
                pointer_outcome=outcome,
            )
            durably_ensure_real_directory(shot, pointer_path.parent)
            if heads.plan is not None:
                current = _resolve_pointer(shot, heads.plan.as_dict())
                if current.bundle.content_hash == bundle.content_hash and current.bundle.outcome == outcome:
                    return current.bundle
            pointer = PlanPointer(
                revision=heads.token.plan_revision + 1,
                run_id=bundle.run_id,
                bundle=bundle.root.relative_to(shot),
                content_hash=bundle.content_hash,
                outcome=outcome,
                published_at=_now(),
            )
            durable_replace_pointer_json(shot, pointer_path, pointer.as_dict())
            try:
                selected = read_authority_selection_heads(shot)
                if (
                    selected.plan != pointer
                    or selected.jit_pointer_bytes != heads.jit_pointer_bytes
                ):
                    raise PlanPublicationError(
                        "plan publication postcondition did not select the exact new plan "
                        "head while preserving the observed JIT head"
                    )
                verified = _resolve_pointer(shot, pointer.as_dict())
                if verified.bundle != bundle:
                    raise PlanPublicationError(
                        "plan publication postcondition selected another verified bundle"
                    )
            except BaseException as publication_error:
                # Readers cannot observe the tentative pointer while this exclusive
                # lock is held. If authored inputs changed during the final rename,
                # restore the exact predecessor (including absence) before releasing.
                if heads.plan_pointer_bytes is None:
                    durable_remove_pointer(shot, pointer_path)
                else:
                    durable_replace_pointer_bytes(
                        shot,
                        pointer_path,
                        heads.plan_pointer_bytes,
                    )
                restored = read_authority_selection_heads(shot)
                if (
                    restored.plan_pointer_bytes != heads.plan_pointer_bytes
                    or restored.jit_pointer_bytes != heads.jit_pointer_bytes
                ):
                    raise PlanPublicationError(
                        "plan publication rollback did not restore the exact predecessor heads"
                    ) from publication_error
                raise
    except ValueError as exc:
        raise PlanSelectionConflict(f"plan publication selection conflict: {exc}") from exc
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
        "clean",
        "clean_with_assumptions",
        "clean_with_deferred",
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
            "retained candidate does not pass the current deterministic gate:\n" + plan_gate.feedback(result)
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
        raise PlanPublicationError("plan bundle artifact set is incomplete: missing " + ", ".join(missing))
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


def _resolve_pointer(shot: Path, pointer: dict[str, Any]) -> SelectedPlanAuthority:
    """Verify one selected bundle using the complete current-pointer contract."""

    revision, run_id, bundle_relative, content_hash, outcome = _validate_current_pointer(pointer)
    return SelectedPlanAuthority(
        revision=revision,
        bundle=_resolve_bundle(
            shot,
            run_id=run_id,
            bundle_relative=bundle_relative,
            content_hash=content_hash,
            pointer_outcome=outcome,
        ),
    )


def resolve_current_selection(shot_folder: str | Path) -> SelectedPlanAuthority:
    """Resolve one verified versioned plan head under the shared selection lock."""

    shot = Path(shot_folder).expanduser().resolve()
    try:
        with authority_selection_lock(shot, exclusive=False):
            from vfx_harness.orchestration.authority_selection_heads import (  # noqa: PLC0415
                read_authority_selection_heads,
            )

            heads = read_authority_selection_heads(shot)
            if heads.plan is None:
                raise PlanPublicationError(f"selected plan pointer is missing: {shot / POINTER}")
            return _resolve_pointer(shot, heads.plan.as_dict())
    except ValueError as exc:
        if isinstance(exc, PlanPublicationError):
            raise
        raise PlanPublicationError(f"selected authority heads are invalid: {exc}") from exc


def resolve_current(shot_folder: str | Path) -> PlanBundle:
    """Resolve and verify the complete plan generation selected by ``plans/current.json``."""

    return resolve_current_selection(shot_folder).bundle


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
    if not safe_run or safe_run in {".", ".."} or "/" in safe_run or "\\" in safe_run:
        raise PlanPublicationError(f"invalid plan bundle run id: {run_id!r}")
    if len(safe_hash) != 64 or any(char not in "0123456789abcdef" for char in safe_hash):
        raise PlanPublicationError("plan bundle content hash must be a lowercase SHA-256 digest")
    bundle_relative = Path("runs") / safe_run / "checkpoints" / "plans" / "bundles" / safe_hash
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
    return pointer_path.exists() or pointer_path.is_symlink() or pointer_path.parent.is_symlink()


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
    return hashlib.sha256(selected_artifact_path(shot, "layers.json").read_bytes()).hexdigest()


def selected_artifact_path(shot_folder: str | Path, name: str) -> Path:
    """Resolve selected authority, preserving pointer-less legacy fixtures temporarily.

    Once a pointer exists there is deliberately no fallback: malformed, stale, or
    incomplete published authority fails closed. Pointer-less archived fixtures retain a
    bounded migration window until they are republished.
    """
    shot = Path(shot_folder).expanduser().resolve()
    if _has_selected_pointer(shot):
        if name not in _SOURCES and name not in _GENERATED_ARTIFACTS:
            raise PlanPublicationError(f"unsupported plan artifact: {name}")
        # The shared resolver imports this facade to verify plan bundles, so selection
        # resolution remains a function-bound dependency rather than a module cycle.
        from vfx_harness.orchestration.authority_selection import (  # noqa: PLC0415
            SelectedAuthorityResolutionError,
            resolve_selected_authority,
        )

        try:
            selected = resolve_selected_authority(shot)
        except SelectedAuthorityResolutionError as exc:
            raise PlanPublicationError(str(exc)) from exc
        if selected.plan is None:
            raise PlanPublicationError("selected plan authority is absent")
        try:
            return selected.artifact_paths[name]
        except KeyError as exc:
            raise PlanPublicationError(
                f"current selected authority does not contain required artifact: {name}"
            ) from exc
    if name not in _SOURCES:
        raise PlanPublicationError(f"unsupported legacy plan artifact: {name}")
    return shot / _SOURCES[name]


def prepare_consumer_view(
    layout: RunLayout,
    *,
    selected_authority: Any | None = None,
) -> Path:
    """Materialize a run-scoped read view of one verified bundle plus authored inputs.

    The deterministic gate still expects a folder-shaped candidate. This view gives it
    that interface without copying published bytes back to the shot root or allowing a
    mixture of plan generations.
    """
    # Selection resolution verifies both heads and every effective artifact while it
    # holds the shared authority lock. The resulting paths all belong to that one token;
    # no later per-artifact pointer lookup can mix plan or JIT generations.
    from vfx_harness.orchestration.authority_selection import (  # noqa: PLC0415
        SelectedAuthorityResolutionError,
        resolve_selected_authority,
    )

    if selected_authority is None:
        try:
            selected = resolve_selected_authority(layout.shot)
        except SelectedAuthorityResolutionError as exc:
            raise PlanPublicationError(str(exc)) from exc
    else:
        selected = selected_authority
    if selected.plan is None or selected.assertion.effective_view is None:
        raise PlanPublicationError("a plan consumer view requires selected plan authority")
    bundle = selected.plan.bundle
    effective_view = selected.assertion.effective_view
    try:
        bundle.root.relative_to(layout.shot)
    except ValueError as exc:
        raise PlanPublicationError("plan consumer snapshot belongs to another shot") from exc
    view = layout.scratch / "plan-consumer-view"
    temp = Path(tempfile.mkdtemp(prefix=".plan-consumer-view.tmp-", dir=layout.scratch))
    try:
        provenance = plan_bundle_integrity.read_schema_object(
            layout.shot,
            selected.artifact_paths["plan.provenance.json"],
            "selected plan bundle provenance",
            schema=PROVENANCE_SCHEMA,
            fields=_PROVENANCE_FIELDS,
        )
        authored_bytes = _authored_input_bytes(layout.shot)
        decision_bytes = _decision_input_bytes(layout.shot)
        (temp / "refs").mkdir()
        for name, payload in authored_bytes.items():
            target = temp / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        for name, payload in decision_bytes.items():
            target = temp / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        captured_authored, captured_decisions = _exact_planning_input_identity(temp)
        if provenance.get("authored_inputs") != captured_authored:
            raise PlanPublicationError(
                "plan consumer snapshot was captured from different authored inputs"
            )
        _verify_decision_inputs(temp, provenance.get("decision_inputs"))

        (temp / "plans").mkdir()
        for name in bundle.artifacts:
            try:
                source = selected.artifact_paths[name]
            except KeyError as exc:
                raise PlanPublicationError(f"selected authority omits bundle artifact {name!r}") from exc
            target = temp / "plans" / "global.md" if name == "global.md" else temp / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(source)
        artifact_hashes = {
            name: hashlib.sha256(selected.artifact_paths[name].read_bytes()).hexdigest() for name in OVERLAY_ARTIFACTS
        }
        marker = PlanConsumerViewMarker(
            shot=layout.shot,
            bundle=bundle.root,
            content_hash=bundle.content_hash,
            base_selection=selected.selection_token,
            view_source=effective_view.source,
            view_digest=effective_view.digest,
            artifact_hashes=artifact_hashes,
            authored_inputs=captured_authored,
            decision_inputs=captured_decisions,
        )
        (temp / ".plan-consumer-view.json").write_bytes(
            canonical_json_bytes(marker.to_dict()),
        )
        source = layout.shot / "shot.json"
        if source.is_file():
            (temp / "shot.json").symlink_to(source)
        state = layout.shot / "state"
        if state.is_dir():
            target_state = temp / "state"
            target_state.mkdir(exist_ok=True)
            for child in state.iterdir():
                if child.name in {"jit-layers", "plan-resolutions.jsonl"}:
                    continue
                (target_state / child.name).symlink_to(
                    child,
                    target_is_directory=child.is_dir(),
                )
        outcomes = layout.shot / "plans" / "outcomes"
        if outcomes.is_dir():
            (temp / "plans" / "outcomes").symlink_to(outcomes, target_is_directory=True)
        try:
            layers = json.loads(selected.artifact_paths["layers.json"].read_text(encoding="utf-8"))["layers"]
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
                        validate_work_unit_plan_authority(
                            layout.shot,
                            source,
                            require_gate=False,
                            selected_authority=selected,
                        )
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
                        raise PlanPublicationError(f"JIT unit plan authority sidecar is missing: {source_authority}")
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
