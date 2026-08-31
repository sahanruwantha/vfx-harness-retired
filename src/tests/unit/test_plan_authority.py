from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest

from vfx_harness.agents.guardrails import selected_plan_read_guard
from vfx_harness.agents.plan_guardrails import (
    format_staged_relative_reads,
    planner_hooks,
    planner_path_scope,
    staged_relative_reads,
)
from vfx_harness.agents.planner import _phase_tools, plan_role_capabilities
from vfx_harness.agents.prompts import verifier_user_prompt
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration import plan_authority, plan_bundle_integrity
from vfx_harness.orchestration.jit_materialization.schema import OVERLAY_ARTIFACTS
from vfx_harness.orchestration.jit_materialization.view_pointer import canonical_view_hash
from vfx_harness.orchestration.layer_plans import (
    is_selected_bundle_member,
    read_work_unit_plan,
    stamp_work_unit_plan,
    validate_work_unit_plan_authority,
    work_unit_plan_path,
)
from vfx_harness.orchestration.plan_authority import (
    PlanPublicationError,
    PlanSelectionConflict,
    prepare_consumer_view,
    prepare_staging,
    promote_candidate,
    publish_current,
    resolve_current,
    resolve_published_bundle,
    selected_artifact_path,
    snapshot_repair_input,
)


def _write_plan(folder: Path, *, marker: str = "one") -> None:
    if not (folder / "brief.md").is_file():
        (folder / "brief.md").parent.mkdir(parents=True, exist_ok=True)
        (folder / "brief.md").write_text("# brief\n", encoding="utf-8")
    (folder / "refs").mkdir(parents=True, exist_ok=True)
    (folder / "plans").mkdir(parents=True, exist_ok=True)
    (folder / "plans" / "global.md").write_text(f"# plan {marker}\n", encoding="utf-8")
    for name, value in {
        "layers.json": {"schema": 4, "layers": []},
        "acceptance.json": [],
        "critic_axes.json": [],
        "checks.json": {"schema": 2, "checks": []},
        "scene_checks.json": {"schema": 2, "contracts": []},
    }.items():
        (folder / name).write_text(json.dumps(value) + "\n", encoding="utf-8")
    brief_hash = hashlib.sha256((folder / "brief.md").read_bytes()).hexdigest()
    (folder / "requirements.json").write_text(json.dumps({
        "schema": "vfx-harness.requirements/v2",
        "judgment_debt_definitions": [],
        "judgment_debt_activations": [],
        "requirements": [{
            "id": "R1",
            "statement": "fixture brief",
            "citation": {"source": "brief.md", "sha256": brief_hash, "line_start": 1, "line_end": 1},
            "resolution": {"kind": "decision", "ids": [], "decision": "fixture authority"},
        }],
    }) + "\n", encoding="utf-8")
    (folder / "obligations.json").write_text(
        json.dumps({"schema": "vfx-harness.obligations/v1", "obligations": []}) + "\n",
        encoding="utf-8",
    )
    (folder / "assumptions.json").write_text(
        json.dumps({"schema": "vfx-harness.assumptions/v1", "assumptions": []}) + "\n",
        encoding="utf-8",
    )


def _preexisting_candidate_bundle(
    shot: Path,
) -> tuple[Path, run_artifacts.RunLayout, bytes]:
    _write_plan(shot, marker="selected")
    selected_layout = run_artifacts.create(shot, "selected-plan")
    publish_current(shot, selected_layout, outcome="clean")
    pointer = shot / "plans" / "current.json"
    selected_pointer = pointer.read_bytes()

    _write_plan(shot, marker="candidate")
    candidate_layout = run_artifacts.create(shot, "candidate-plan")
    candidate = publish_current(shot, candidate_layout, outcome="clean")
    pointer.write_bytes(selected_pointer)
    return candidate.root, candidate_layout, selected_pointer


def test_clean_plan_publishes_one_immutable_bundle_behind_atomic_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _write_plan(tmp_path)
    layout = run_artifacts.create(tmp_path, "plan-run-1")

    published = publish_current(tmp_path, layout, outcome="clean")
    pointer = json.loads((tmp_path / "plans" / "current.json").read_text(encoding="utf-8"))
    resolved = resolve_current(tmp_path)

    assert pointer["schema"] == "vfx-harness.plan-pointer/v2"
    assert pointer["revision"] == 1
    assert pointer["run_id"] == layout.run_id
    assert pointer["content_hash"] == published.content_hash
    assert resolved == published
    assert published.root.is_relative_to(layout.checkpoints / "plans" / "bundles")
    assert (published.root / "global.md").read_text(encoding="utf-8") == "# plan one\n"
    assert set(published.artifacts) == {
        "global.md",
        "layers.json",
        "acceptance.json",
        "critic_axes.json",
        "checks.json",
        "scene_checks.json",
        "requirements.json",
        "obligations.json",
        "assumptions.json",
        "plan.provenance.json",
    }

    _write_plan(tmp_path, marker="two")
    assert (published.root / "global.md").read_text(encoding="utf-8") == "# plan one\n"
    assert selected_artifact_path(tmp_path, "global.md") == published.root / "global.md"
    assert resolve_current(tmp_path) == published


def test_bundle_durability_barrier_has_deterministic_pre_pointer_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _write_plan(tmp_path)
    evidence = tmp_path / "plans" / "evidence" / "durability.md"
    evidence.parent.mkdir()
    evidence.write_text("# durable bundle\n", encoding="utf-8")
    layout = run_artifacts.create(tmp_path, "durable-plan")
    bundle_parent = layout.checkpoints / "plans" / "bundles"
    events: list[str] = []
    original_file_fsync = plan_bundle_integrity._fsync_regular_file
    original_directory_fsync = plan_bundle_integrity._fsync_directory
    original_replace = plan_bundle_integrity.os.replace
    original_pointer_parent = plan_authority.durably_ensure_real_directory
    original_pointer_write = plan_authority.durable_replace_pointer_json

    def temp_root(path: Path) -> Path:
        relative = path.relative_to(bundle_parent)
        return bundle_parent / relative.parts[0]

    def recording_file_fsync(path: Path) -> None:
        events.append(f"file:{path.relative_to(temp_root(path)).as_posix()}")
        original_file_fsync(path)

    def recording_directory_fsync(path: Path) -> None:
        try:
            temporary = temp_root(path)
        except (ValueError, IndexError):
            relative = path.relative_to(tmp_path)
            label = "." if relative == Path(".") else relative.as_posix()
            events.append(f"ancestor:{label}")
        else:
            relative = path.relative_to(temporary)
            label = "." if relative == Path(".") else relative.as_posix()
            events.append(f"tree:{label}")
        original_directory_fsync(path)

    def recording_replace(
        source: str | Path,
        target: str | Path,
        **kwargs: object,
    ) -> None:
        if not kwargs and Path(target).parent == bundle_parent:
            events.append("bundle-rename")
        original_replace(source, target, **kwargs)  # type: ignore[arg-type]

    def recording_pointer_write(*args: object, **kwargs: object) -> bytes:
        events.append("pointer-write")
        return original_pointer_write(*args, **kwargs)  # type: ignore[arg-type]

    def recording_pointer_parent(*args: object, **kwargs: object) -> Path:
        result = original_pointer_parent(*args, **kwargs)  # type: ignore[arg-type]
        events.append("pointer-parent-durable")
        return result

    monkeypatch.setattr(
        plan_bundle_integrity,
        "_fsync_regular_file",
        recording_file_fsync,
    )
    monkeypatch.setattr(
        plan_bundle_integrity,
        "_fsync_directory",
        recording_directory_fsync,
    )
    monkeypatch.setattr(plan_bundle_integrity.os, "replace", recording_replace)
    monkeypatch.setattr(
        plan_authority,
        "durably_ensure_real_directory",
        recording_pointer_parent,
    )
    monkeypatch.setattr(
        plan_authority,
        "durable_replace_pointer_json",
        recording_pointer_write,
    )

    publish_current(tmp_path, layout, outcome="clean")

    assert events == [
        "file:acceptance.json",
        "file:assumptions.json",
        "file:bundle.json",
        "file:checks.json",
        "file:critic_axes.json",
        "file:global.md",
        "file:layers.json",
        "file:obligations.json",
        "file:plan.provenance.json",
        "file:plans/evidence/durability.md",
        "file:requirements.json",
        "file:scene_checks.json",
        "tree:plans/evidence",
        "tree:plans",
        "tree:.",
        "bundle-rename",
        "ancestor:runs/durable-plan/checkpoints/plans/bundles",
        "ancestor:runs/durable-plan/checkpoints/plans",
        "ancestor:runs/durable-plan/checkpoints",
        "ancestor:runs/durable-plan",
        "ancestor:runs",
        "ancestor:.",
        "pointer-parent-durable",
        "pointer-write",
    ]


def test_first_plan_pointer_parent_is_durable_before_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _write_plan(tmp_path)
    layout = run_artifacts.create(tmp_path, "first-pointer-parent")
    original_write_bundle = plan_authority._write_bundle
    original_pointer_parent = plan_authority.durably_ensure_real_directory
    original_pointer_write = plan_authority.durable_replace_pointer_json
    events: list[str] = []

    def remove_source_parent_after_freeze(*args: object, **kwargs: object):
        bundle = original_write_bundle(*args, **kwargs)  # type: ignore[arg-type]
        source = tmp_path / "plans" / "global.md"
        source.unlink()
        source.parent.rmdir()
        return bundle

    def recording_pointer_parent(*args: object, **kwargs: object) -> Path:
        result = original_pointer_parent(*args, **kwargs)  # type: ignore[arg-type]
        events.append("pointer-parent-durable")
        assert result == tmp_path / "plans"
        assert result.is_dir()
        return result

    def recording_pointer(*args: object, **kwargs: object) -> bytes:
        events.append("pointer-write")
        return original_pointer_write(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(plan_authority, "_write_bundle", remove_source_parent_after_freeze)
    monkeypatch.setattr(
        plan_authority,
        "durably_ensure_real_directory",
        recording_pointer_parent,
    )
    monkeypatch.setattr(
        plan_authority,
        "durable_replace_pointer_json",
        recording_pointer,
    )

    publish_current(tmp_path, layout, outcome="clean")

    assert events == ["pointer-parent-durable", "pointer-write"]
    assert (tmp_path / "plans" / "current.json").is_file()


def test_crash_after_bundle_rename_cannot_select_until_orphan_is_reflushed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _write_plan(tmp_path)
    layout = run_artifacts.create(tmp_path, "durability-retry")
    bundle_parent = layout.checkpoints / "plans" / "bundles"
    pointer = tmp_path / "plans" / "current.json"
    original_directory_fsync = plan_bundle_integrity._fsync_directory
    original_pointer_write = plan_authority.durable_replace_pointer_json

    def crash_at_rename_parent(path: Path) -> None:
        if path == bundle_parent:
            raise OSError("injected crash after bundle rename")
        original_directory_fsync(path)

    def forbidden_pointer_write(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("pointer write crossed an incomplete bundle durability barrier")

    monkeypatch.setattr(
        plan_bundle_integrity,
        "_fsync_directory",
        crash_at_rename_parent,
    )
    monkeypatch.setattr(
        plan_authority,
        "durable_replace_pointer_json",
        forbidden_pointer_write,
    )

    with pytest.raises(PlanPublicationError, match="durably install"):
        publish_current(tmp_path, layout, outcome="clean")

    assert not pointer.exists()
    orphan_roots = tuple(
        path
        for path in bundle_parent.iterdir()
        if path.is_dir() and not path.name.startswith(".")
    )
    assert len(orphan_roots) == 1

    reflushed: list[Path] = []
    original_file_fsync = plan_bundle_integrity._fsync_regular_file

    def recording_file_fsync(path: Path) -> None:
        reflushed.append(path)
        original_file_fsync(path)

    monkeypatch.setattr(
        plan_bundle_integrity,
        "_fsync_directory",
        original_directory_fsync,
    )
    monkeypatch.setattr(
        plan_bundle_integrity,
        "_fsync_regular_file",
        recording_file_fsync,
    )
    monkeypatch.setattr(
        plan_authority,
        "durable_replace_pointer_json",
        original_pointer_write,
    )

    published = publish_current(tmp_path, layout, outcome="clean")

    assert pointer.is_file()
    assert published.root == orphan_roots[0]
    assert {path.relative_to(published.root).as_posix() for path in reflushed} == {
        *published.artifacts,
        "bundle.json",
    }


def test_semantically_identical_publication_is_a_true_pointer_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _write_plan(tmp_path)
    first = run_artifacts.create(tmp_path, "plan-run-1")
    selected = publish_current(tmp_path, first, outcome="clean")
    pointer = tmp_path / "plans" / "current.json"
    before = pointer.read_bytes()

    second = run_artifacts.create(tmp_path, "plan-run-2")
    replay = publish_current(tmp_path, second, outcome="clean")

    assert replay == selected
    assert pointer.read_bytes() == before
    assert json.loads(before)["revision"] == 1


def test_only_one_plan_workspace_can_publish_from_the_same_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    (tmp_path / "brief.md").write_text("# brief\n", encoding="utf-8")
    (tmp_path / "refs").mkdir()
    first_layout = run_artifacts.create(tmp_path, "plan-run-1")
    second_layout = run_artifacts.create(tmp_path, "plan-run-2")
    first_workspace = prepare_staging(first_layout)
    second_workspace = prepare_staging(second_layout)
    _write_plan(first_workspace, marker="first")
    _write_plan(second_workspace, marker="second")

    selected = publish_current(
        tmp_path,
        first_layout,
        outcome="clean",
        source_root=first_workspace,
    )
    pointer = tmp_path / "plans" / "current.json"
    after_first = pointer.read_bytes()

    with pytest.raises(PlanSelectionConflict, match="selection conflict"):
        publish_current(
            tmp_path,
            second_layout,
            outcome="clean",
            source_root=second_workspace,
        )

    assert pointer.read_bytes() == after_first
    assert resolve_current(tmp_path) == selected


@pytest.mark.parametrize("damage", ["tamper", "missing", "symlink", "undeclared"])
def test_publication_refuses_a_damaged_preexisting_content_addressed_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    damage: str,
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    root, layout, selected_pointer = _preexisting_candidate_bundle(tmp_path)
    member = root / "global.md"
    if damage == "tamper":
        member.write_text("# different bytes\n", encoding="utf-8")
    elif damage == "missing":
        member.unlink()
    elif damage == "symlink":
        outside = tmp_path / "substituted-global.md"
        outside.write_bytes(member.read_bytes())
        member.unlink()
        member.symlink_to(outside)
    else:
        (root / "undeclared.txt").write_text("not in the manifest\n", encoding="utf-8")

    with pytest.raises(PlanPublicationError, match="plan bundle"):
        publish_current(tmp_path, layout, outcome="clean")

    pointer = tmp_path / "plans" / "current.json"
    assert pointer.read_bytes() == selected_pointer
    assert resolve_current(tmp_path).run_id == "selected-plan"


@pytest.mark.parametrize("substitution", ["pointer", "bundle", "manifest", "artifact"])
def test_selected_authority_refuses_symlink_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    substitution: str,
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _write_plan(tmp_path)
    layout = run_artifacts.create(tmp_path, "plan-run-symlink")
    bundle = publish_current(tmp_path, layout, outcome="clean")
    pointer = tmp_path / "plans" / "current.json"

    if substitution == "pointer":
        outside = tmp_path / "substituted-pointer.json"
        outside.write_bytes(pointer.read_bytes())
        pointer.unlink()
        pointer.symlink_to(outside)
    elif substitution == "bundle":
        outside = tmp_path / "substituted-bundle"
        bundle.root.rename(outside)
        bundle.root.symlink_to(outside, target_is_directory=True)
    else:
        member = bundle.root / ("bundle.json" if substitution == "manifest" else "global.md")
        outside = tmp_path / f"substituted-{member.name}"
        outside.write_bytes(member.read_bytes())
        member.unlink()
        member.symlink_to(outside)

    with pytest.raises(PlanPublicationError, match="symlink"):
        resolve_current(tmp_path)


def test_selected_authority_refuses_manifest_tampering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _write_plan(tmp_path)
    layout = run_artifacts.create(tmp_path, "plan-run-manifest")
    bundle = publish_current(tmp_path, layout, outcome="clean")
    manifest_path = bundle.root / "bundle.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["outcome"] = "clean_with_deferred"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(PlanPublicationError, match="outcomes disagree"):
        resolve_current(tmp_path)


def test_publication_preserves_the_compact_mapping_for_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run 886632 published the first diet bundle without its source mapping because the
    supplemental filter globbed *.md only."""
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _write_plan(tmp_path)
    (tmp_path / "plans" / "ownership_mapping.json").write_text(
        '{"schema": "vfx-harness.ownership-mapping/v1"}', encoding="utf-8"
    )
    layout = run_artifacts.create(tmp_path, "plan-run-mapping")

    published = publish_current(tmp_path, layout, outcome="clean")

    assert "plans/ownership_mapping.json" in published.artifacts
    assert (published.root / "plans" / "ownership_mapping.json").is_file()


def test_explicit_published_bundle_resolution_requires_exact_run_and_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _write_plan(tmp_path)
    layout = run_artifacts.create(tmp_path, "plan-run-1")
    published = publish_current(tmp_path, layout, outcome="clean")

    resolved = resolve_published_bundle(
        tmp_path,
        run_id=layout.run_id,
        content_hash=published.content_hash,
    )

    assert resolved == published
    with pytest.raises(PlanPublicationError, match="missing or unreadable"):
        resolve_published_bundle(
            tmp_path,
            run_id="another-run",
            content_hash=published.content_hash,
        )
    with pytest.raises(PlanPublicationError, match="SHA-256"):
        resolve_published_bundle(tmp_path, run_id=layout.run_id, content_hash="not-a-hash")


def test_plan_staging_contains_authored_inputs_but_no_prior_authority_or_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    (tmp_path / "brief.md").write_text("# fresh brief\n", encoding="utf-8")
    refs = tmp_path / "refs"
    refs.mkdir()
    (refs / "one.png").write_bytes(b"reference")
    _write_plan(tmp_path, marker="old")
    (tmp_path / "questions.jsonl").write_text('{"id": 1}\n', encoding="utf-8")
    (tmp_path / "plan_amendments.jsonl").write_text('{"id":"amendment"}\n', encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir()
    (state / "plan-resolutions.jsonl").write_text('{"id":"resolution"}\n', encoding="utf-8")
    layout = run_artifacts.create(tmp_path, "fresh-run")

    workspace = prepare_staging(layout)
    manifest = json.loads(layout.manifest.read_text(encoding="utf-8"))

    assert workspace == layout.scratch / "plan-workspace"
    assert manifest["authority"]["plan_authoring_workspace"] == (
        "scratch/plan-workspace/ for global plan invocations"
    )
    assert (workspace / "brief.md").read_text(encoding="utf-8") == "# fresh brief\n"
    assert (workspace / "refs" / "one.png").read_bytes() == b"reference"
    assert (workspace / "refs" / "one.png").stat().st_ino != (refs / "one.png").stat().st_ino
    assert (workspace / "plan_amendments.jsonl").read_text(encoding="utf-8") == (
        '{"id":"amendment"}\n'
    )
    assert (workspace / "state/plan-resolutions.jsonl").read_text(encoding="utf-8") == (
        '{"id":"resolution"}\n'
    )
    assert (workspace / "plan_amendments.jsonl").stat().st_ino != (
        tmp_path / "plan_amendments.jsonl"
    ).stat().st_ino
    assert not (workspace / "plans" / "global.md").exists()
    assert not (workspace / "layers.json").exists()
    assert not (workspace / "questions.jsonl").exists()
    assert not (workspace / "runs").exists()


@pytest.mark.parametrize(
    "damage",
    [
        "missing_brief",
        "missing_refs",
        "brief_symlink",
        "refs_symlink",
        "nested_ref_symlink",
        "special_ref",
    ],
)
def test_first_time_staging_refuses_invalid_authored_input_structure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    damage: str,
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _write_plan(tmp_path)
    brief = tmp_path / "brief.md"
    refs = tmp_path / "refs"
    reference = refs / "one.png"
    reference.write_bytes(b"reference")
    layout = run_artifacts.create(tmp_path, f"staging-{damage}")

    if damage == "missing_brief":
        brief.unlink()
    elif damage == "missing_refs":
        reference.unlink()
        refs.rmdir()
    elif damage == "brief_symlink":
        outside = tmp_path / "outside-source-brief.md"
        outside.write_bytes(brief.read_bytes())
        brief.unlink()
        brief.symlink_to(outside)
    elif damage == "refs_symlink":
        outside = tmp_path / "outside-source-refs"
        refs.rename(outside)
        refs.symlink_to(outside, target_is_directory=True)
    elif damage == "nested_ref_symlink":
        outside = tmp_path / "outside-source-reference.png"
        outside.write_bytes(reference.read_bytes())
        reference.unlink()
        reference.symlink_to(outside)
    else:
        reference.unlink()
        os.mkfifo(reference)

    with pytest.raises(PlanPublicationError, match="authored"):
        prepare_staging(layout)
    assert not (layout.scratch / "plan-workspace").exists()


def test_first_time_staging_wraps_an_unreadable_reference_as_publication_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _write_plan(tmp_path)
    reference = tmp_path / "refs" / "one.png"
    reference.write_bytes(b"reference")
    layout = run_artifacts.create(tmp_path, "staging-unreadable-reference")
    original_read_bytes = Path.read_bytes

    def deny_reference(path: Path) -> bytes:
        if path == reference:
            raise PermissionError("injected unreadable reference")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", deny_reference)
    with pytest.raises(PlanPublicationError, match="authored reference input is unreadable"):
        prepare_staging(layout)
    assert not (layout.scratch / "plan-workspace").exists()


def test_plan_bundle_can_publish_from_run_scoped_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    (tmp_path / "brief.md").write_text("# brief\n", encoding="utf-8")
    (tmp_path / "refs").mkdir()
    _write_plan(tmp_path, marker="old-shot-root")
    layout = run_artifacts.create(tmp_path, "fresh-run")
    workspace = prepare_staging(layout)
    _write_plan(workspace, marker="fresh-run-plan")

    published = publish_current(tmp_path, layout, outcome="clean", source_root=workspace)

    assert (published.root / "global.md").read_text(encoding="utf-8") == "# plan fresh-run-plan\n"
    assert (tmp_path / "plans" / "global.md").read_text(encoding="utf-8") == "# plan old-shot-root\n"
    assert resolve_current(tmp_path) == published


def test_workspace_publication_refuses_live_authored_input_change_before_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    (tmp_path / "brief.md").write_text("# old brief\n", encoding="utf-8")
    (tmp_path / "refs").mkdir()
    layout = run_artifacts.create(tmp_path, "stale-workspace")
    workspace = prepare_staging(layout)
    _write_plan(workspace, marker="stale-candidate")

    (tmp_path / "brief.md").write_text("# new brief\n", encoding="utf-8")

    with pytest.raises(PlanPublicationError, match="different authored inputs"):
        publish_current(
            tmp_path,
            layout,
            outcome="clean",
            source_root=workspace,
        )

    assert not (tmp_path / "plans" / "current.json").exists()


def test_workspace_publication_rolls_back_if_authored_input_changes_during_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    brief = tmp_path / "brief.md"
    brief.write_text("# old brief\n", encoding="utf-8")
    (tmp_path / "refs").mkdir()
    layout = run_artifacts.create(tmp_path, "publication-input-race")
    workspace = prepare_staging(layout)
    _write_plan(workspace, marker="racing-candidate")
    real_replace = plan_authority.durable_replace_pointer_json

    def mutate_before_replace(*args, **kwargs):
        brief.write_text("# new brief\n", encoding="utf-8")
        return real_replace(*args, **kwargs)

    monkeypatch.setattr(
        plan_authority,
        "durable_replace_pointer_json",
        mutate_before_replace,
    )

    with pytest.raises(PlanPublicationError, match="different authored inputs"):
        publish_current(
            tmp_path,
            layout,
            outcome="clean",
            source_root=workspace,
        )

    assert not (tmp_path / "plans" / "current.json").exists()


def test_failed_live_input_postcondition_restores_exact_prior_plan_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _write_plan(tmp_path, marker="selected")
    first = run_artifacts.create(tmp_path, "selected-plan")
    publish_current(tmp_path, first, outcome="clean")
    pointer = tmp_path / "plans" / "current.json"
    predecessor = pointer.read_bytes()

    second = run_artifacts.create(tmp_path, "racing-successor")
    workspace = prepare_staging(second)
    _write_plan(workspace, marker="successor")
    real_replace = plan_authority.durable_replace_pointer_json

    def mutate_before_replace(*args, **kwargs):
        (tmp_path / "brief.md").write_text("# changed intent\n", encoding="utf-8")
        return real_replace(*args, **kwargs)

    monkeypatch.setattr(
        plan_authority,
        "durable_replace_pointer_json",
        mutate_before_replace,
    )

    with pytest.raises(PlanPublicationError, match="different authored inputs"):
        publish_current(
            tmp_path,
            second,
            outcome="clean",
            source_root=workspace,
        )

    assert pointer.read_bytes() == predecessor


def test_bundle_preserves_nested_plan_evidence_and_ready_unit_plans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    (tmp_path / "brief.md").write_text("# brief\n", encoding="utf-8")
    (tmp_path / "refs").mkdir()
    layout = run_artifacts.create(tmp_path, "fresh-run")
    workspace = prepare_staging(layout)
    _write_plan(workspace)
    unit_rel = Path("plans/01_camera/00_blockout.md")
    unit = workspace / unit_rel
    unit.parent.mkdir(parents=True)
    unit.write_text("# blockout\n" + "bounded execution step\n" * 20, encoding="utf-8")
    evidence_rel = Path("plans/03_assembly/spike_evidence.md")
    evidence = workspace / evidence_rel
    evidence.parent.mkdir(parents=True)
    evidence.write_text("# durable spike evidence\nproved\n", encoding="utf-8")
    render_rel = Path("plans/evidence/spikes/spike_evidence.png")
    render = workspace / render_rel
    render.parent.mkdir(parents=True, exist_ok=True)
    render.write_bytes(b"immutable-render")

    published = publish_current(tmp_path, layout, outcome="clean", source_root=workspace)

    assert unit_rel.as_posix() in published.artifacts
    assert evidence_rel.as_posix() in published.artifacts
    assert (published.root / unit_rel).read_bytes() == unit.read_bytes()
    assert (published.root / evidence_rel).read_bytes() == evidence.read_bytes()
    assert render_rel.as_posix() in published.artifacts
    assert (published.root / render_rel).read_bytes() == render.read_bytes()

    declared = SimpleNamespace(plan=unit_rel.as_posix())
    selected = work_unit_plan_path(tmp_path, declared)
    assert selected == published.root / unit_rel
    assert "bounded execution" in read_work_unit_plan(
        tmp_path, SimpleNamespace(id="1"), declared
    )

    view = prepare_consumer_view(layout)
    view_plan = work_unit_plan_path(view, declared)
    assert view_plan == view / unit_rel
    validate_work_unit_plan_authority(view, view_plan)
    assert view_plan.resolve() == published.root / unit_rel
    assert is_selected_bundle_member(tmp_path, selected)


def test_candidate_promotion_is_new_run_owned_and_model_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    (tmp_path / "brief.md").write_text("# brief\n", encoding="utf-8")
    (tmp_path / "refs").mkdir()
    source_layout = run_artifacts.create(tmp_path, "source-plan")
    source = prepare_staging(source_layout)
    _write_plan(source)
    nested = source / "plans" / "01_camera" / "00_blockout.md"
    nested.parent.mkdir(parents=True)
    nested.write_text("# immutable blockout\n" + "step\n" * 40, encoding="utf-8")
    source_layout.set_status(
        "passed", exit_code=0, metadata={"outcome": "clean_with_assumptions"}
    )

    class CleanGate:
        def __init__(self) -> None:
            self.clean = True
            self.publishable_outcome = "clean_with_assumptions"
            self.blocking: list[object] = []

        def to_dict(self, *, outcome=None):
            return {"clean": True, "outcome": outcome, "findings": []}

    from vfx_harness.evaluation import plan_gate

    monkeypatch.setattr(plan_gate, "run", lambda *args, **kwargs: CleanGate())
    target_layout = run_artifacts.create(tmp_path, "promoted-plan")

    bundle, result, target = promote_candidate(tmp_path, "source-plan", target_layout)

    assert result.clean
    assert target == target_layout.scratch / "plan-workspace"
    assert bundle.run_id == "promoted-plan"
    assert bundle.root.is_relative_to(target_layout.checkpoints)
    assert (bundle.root / "plans" / "01_camera" / "00_blockout.md").read_bytes() == (
        nested.read_bytes()
    )
    assert nested.read_text(encoding="utf-8").startswith("# immutable blockout")


def test_selected_bundle_fails_when_an_authored_reference_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    (tmp_path / "brief.md").write_text("# brief\n", encoding="utf-8")
    refs = tmp_path / "refs"
    refs.mkdir()
    reference = refs / "one.png"
    reference.write_bytes(b"one")
    layout = run_artifacts.create(tmp_path, "fresh-run")
    workspace = prepare_staging(layout)
    _write_plan(workspace)
    publish_current(tmp_path, layout, outcome="clean", source_root=workspace)
    assert resolve_current(tmp_path).run_id == "fresh-run"
    prior = reference.stat()

    reference.write_bytes(b"two")
    os.utime(reference, ns=(prior.st_atime_ns, prior.st_mtime_ns))

    with pytest.raises(PlanPublicationError, match="different authored inputs"):
        resolve_current(tmp_path)


def test_selected_bundle_rechecks_same_size_brief_bytes_with_restored_mtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _write_plan(tmp_path)
    layout = run_artifacts.create(tmp_path, "brief-freshness")
    publish_current(tmp_path, layout, outcome="clean")
    assert resolve_current(tmp_path).run_id == "brief-freshness"
    brief = tmp_path / "brief.md"
    prior = brief.stat()

    brief.write_text("# grief\n", encoding="utf-8")
    assert brief.stat().st_size == prior.st_size
    os.utime(brief, ns=(prior.st_atime_ns, prior.st_mtime_ns))

    with pytest.raises(PlanPublicationError, match="different authored inputs"):
        resolve_current(tmp_path)


@pytest.mark.parametrize(
    "relative",
    [Path("plan_amendments.jsonl"), Path("state/plan-resolutions.jsonl")],
)
def test_selected_bundle_rechecks_planning_decision_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative: Path,
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _write_plan(tmp_path)
    decision = tmp_path / relative
    decision.parent.mkdir(parents=True, exist_ok=True)
    decision.write_text('{"id":"one"}\n', encoding="utf-8")
    layout = run_artifacts.create(tmp_path, f"decision-{decision.name}")
    publish_current(tmp_path, layout, outcome="clean")
    assert resolve_current(tmp_path).run_id == layout.run_id

    decision.write_text('{"id":"two"}\n', encoding="utf-8")

    with pytest.raises(PlanPublicationError, match="different planning decisions"):
        resolve_current(tmp_path)


def test_selected_bundle_allows_decision_suffix_but_refuses_truncating_consumed_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _write_plan(tmp_path)
    decision = tmp_path / "plan_amendments.jsonl"
    consumed = b'{"id":"planning-input"}\n'
    decision.write_bytes(consumed)
    layout = run_artifacts.create(tmp_path, "append-only-decisions")
    bundle = publish_current(tmp_path, layout, outcome="clean")
    provenance = json.loads(
        (bundle.root / "plan.provenance.json").read_text(encoding="utf-8")
    )
    assert provenance["schema"] == "vfx-harness.plan-provenance/v2"
    assert provenance["decision_inputs"]["plan_amendments.jsonl"] == {
        "size": len(consumed),
        "sha256": hashlib.sha256(consumed).hexdigest(),
    }

    decision.write_bytes(consumed + b'{"id":"later-output"}\n')
    assert resolve_current(tmp_path).run_id == "append-only-decisions"

    decision.write_bytes(consumed[:-1])
    with pytest.raises(PlanPublicationError, match="truncated planning decisions"):
        resolve_current(tmp_path)


@pytest.mark.parametrize(
    ("relative", "expected"),
    [
        (Path("refs/one.png"), "authored refs input"),
        (Path("state/plan-resolutions.jsonl"), "planning decision input"),
    ],
)
def test_selected_bundle_rejects_symlinked_live_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative: Path,
    expected: str,
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _write_plan(tmp_path)
    selected_input = tmp_path / relative
    selected_input.parent.mkdir(parents=True, exist_ok=True)
    selected_input.write_bytes(b"same selected bytes\n")
    layout = run_artifacts.create(tmp_path, f"symlink-{selected_input.name}")
    publish_current(tmp_path, layout, outcome="clean")
    assert resolve_current(tmp_path).run_id == layout.run_id
    outside = tmp_path / f"outside-{selected_input.name}"
    outside.write_bytes(selected_input.read_bytes())
    selected_input.unlink()
    selected_input.symlink_to(outside)

    with pytest.raises(PlanPublicationError, match=rf"{expected}.*symlink"):
        resolve_current(tmp_path)


@pytest.mark.parametrize(
    "damage",
    ["missing_brief", "missing_refs", "brief_symlink", "refs_symlink", "special_ref"],
)
def test_selected_bundle_requires_real_complete_authored_input_structure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    damage: str,
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _write_plan(tmp_path)
    reference = tmp_path / "refs" / "one.png"
    reference.write_bytes(b"reference")
    layout = run_artifacts.create(tmp_path, f"authored-{damage}")
    publish_current(tmp_path, layout, outcome="clean")

    brief = tmp_path / "brief.md"
    refs = tmp_path / "refs"
    if damage == "missing_brief":
        brief.unlink()
    elif damage == "missing_refs":
        reference.unlink()
        refs.rmdir()
    elif damage == "brief_symlink":
        outside = tmp_path / "outside-brief.md"
        outside.write_bytes(brief.read_bytes())
        brief.unlink()
        brief.symlink_to(outside)
    elif damage == "refs_symlink":
        outside = tmp_path / "outside-refs"
        refs.rename(outside)
        refs.symlink_to(outside, target_is_directory=True)
    else:
        reference.unlink()
        os.mkfifo(reference)

    with pytest.raises(PlanPublicationError, match="authored"):
        resolve_current(tmp_path)


def test_selected_bundle_wraps_unreadable_authored_input_as_publication_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _write_plan(tmp_path)
    reference = tmp_path / "refs" / "one.png"
    reference.write_bytes(b"reference")
    layout = run_artifacts.create(tmp_path, "unreadable-reference")
    publish_current(tmp_path, layout, outcome="clean")
    original_read_bytes = Path.read_bytes

    def deny_reference(path: Path) -> bytes:
        if path == reference:
            raise PermissionError("injected unreadable reference")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", deny_reference)
    with pytest.raises(PlanPublicationError, match="authored reference input is unreadable"):
        resolve_current(tmp_path)


def test_v2_plan_workspace_marker_is_rejected_after_selection_token_migration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _write_plan(tmp_path)
    layout = run_artifacts.create(tmp_path, "old-workspace-schema")
    workspace = prepare_staging(layout)
    marker_path = workspace / ".plan-workspace.json"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["schema"] = "vfx-harness.plan-workspace/v2"
    marker_path.write_text(json.dumps(marker, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(PlanPublicationError, match="workspace marker schema"):
        prepare_staging(layout)


def test_v1_plan_provenance_is_rejected_after_prefix_contract_migration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _write_plan(tmp_path)
    import vfx_harness.orchestration.plan_authority as plan_authority

    monkeypatch.setattr(
        plan_authority,
        "PROVENANCE_SCHEMA",
        "vfx-harness.plan-provenance/v1",
    )
    layout = run_artifacts.create(tmp_path, "old-provenance-schema")
    publish_current(tmp_path, layout, outcome="clean")
    monkeypatch.setattr(
        plan_authority,
        "PROVENANCE_SCHEMA",
        "vfx-harness.plan-provenance/v2",
    )

    with pytest.raises(PlanPublicationError, match="provenance schema is unsupported"):
        resolve_current(tmp_path)


def test_existing_workspace_marker_rejects_duplicate_json_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _write_plan(tmp_path)
    layout = run_artifacts.create(tmp_path, "duplicate-workspace-marker")
    workspace = prepare_staging(layout)
    marker = workspace / ".plan-workspace.json"
    raw = marker.read_text(encoding="utf-8")
    schema = '"schema": "vfx-harness.plan-workspace/v3"'
    marker.write_text(raw.replace(schema, f"{schema},\n  {schema}", 1), encoding="utf-8")

    with pytest.raises(PlanPublicationError, match="duplicate JSON key 'schema'"):
        prepare_staging(layout)


def test_publication_source_marker_rejects_duplicate_json_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _write_plan(tmp_path)
    marker = tmp_path / ".plan-workspace.json"
    marker.write_text(
        """{
  "schema": "vfx-harness.plan-workspace/v3",
  "schema": "vfx-harness.plan-workspace/v3",
  "run_id": "forged",
  "shot": "forged",
  "authored_inputs": {},
  "decision_inputs": {}
}
""",
        encoding="utf-8",
    )
    layout = run_artifacts.create(tmp_path, "duplicate-source-marker")

    with pytest.raises(PlanPublicationError, match="duplicate JSON key 'schema'"):
        publish_current(tmp_path, layout, outcome="clean")


def test_published_ownership_mapping_round_trips_through_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The publisher seals plans/ownership_mapping.json (the compact source document the
    plan surface expands from); run 20260824T150358Z-3bc39c published the first
    mapping-carrying bundle and every consumer failed closed on it because the resolver
    never learned the member. Publication and resolution must agree on membership."""
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _write_plan(tmp_path)
    (tmp_path / "plans" / "ownership_mapping.json").write_text(
        json.dumps({"schema": 1, "layers": [], "axes": [], "resolutions": {}, "blockers": []}) + "\n",
        encoding="utf-8",
    )
    layout = run_artifacts.create(tmp_path, "plan-run")

    published = publish_current(tmp_path, layout, outcome="clean")

    assert "plans/ownership_mapping.json" in published.artifacts
    resolved = resolve_current(tmp_path)
    assert resolved.content_hash == published.content_hash


def test_superseded_jit_view_is_inert_after_republication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Republication used to leave every consumer — including the replan transaction
    meant to reconcile the change — raising on the PRIOR generation's materialized view.
    A view pinned to another bundle is superseded state: consumers get the new bundle's
    own deferred artifact. A malformed view still fails closed."""
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _write_plan(tmp_path)
    layout = run_artifacts.create(tmp_path, "plan-run")
    published = publish_current(tmp_path, layout, outcome="clean")

    view_pointer = tmp_path / "state" / "jit-layers" / "current.json"
    view_pointer.parent.mkdir(parents=True)
    old_documents = {
        name: json.loads((published.root / name).read_bytes())
        for name in OVERLAY_ARTIFACTS
    }
    old_view_hash = canonical_view_hash(old_documents)
    old_view = tmp_path / "state" / "jit-layers" / "views" / old_view_hash
    old_view.mkdir(parents=True)
    old_hashes = {}
    for name in OVERLAY_ARTIFACTS:
        payload = (published.root / name).read_bytes()
        (old_view / name).write_bytes(payload)
        old_hashes[name] = hashlib.sha256(payload).hexdigest()
    view_pointer.write_text(
        json.dumps(
            {
                "schema": "vfx-harness.jit-layer-view/v2",
                "revision": 1,
                "plan_revision": 1,
                "bundle_hash": "0" * 64,  # a superseded generation, not the selection
                "view_hash": old_view_hash,
                "materialized_layers": [],
                "artifacts": {
                    name: f"state/jit-layers/views/{old_view_hash}/{name}"
                    for name in OVERLAY_ARTIFACTS
                },
                "hashes": old_hashes,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    served = selected_artifact_path(tmp_path, "layers.json")
    assert served == published.root / "layers.json"

    view_pointer.write_text(json.dumps({"schema": "wrong"}) + "\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="selected JIT pointer is invalid"):
        selected_artifact_path(tmp_path, "layers.json")


def test_publication_refuses_members_resolution_cannot_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _write_plan(tmp_path)
    rogue = tmp_path / "plans" / "01_camera" / "notes.json"
    rogue.parent.mkdir(parents=True)
    rogue.write_text("{}\n", encoding="utf-8")
    layout = run_artifacts.create(tmp_path, "plan-run")

    import vfx_harness.orchestration.plan_authority as plan_authority

    monkeypatch.setattr(
        plan_authority,
        "_supplemental_plan_artifacts",
        lambda source_root: {"plans/01_camera/notes.json": Path("plans/01_camera/notes.json")},
    )
    with pytest.raises(PlanPublicationError, match="resolution does not support"):
        publish_current(tmp_path, layout, outcome="clean")


def test_jit_unit_plan_is_pinned_to_selected_bundle_and_exact_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _write_plan(tmp_path)
    layout = run_artifacts.create(tmp_path, "plan-run")
    publish_current(tmp_path, layout, outcome="clean")
    plan = tmp_path / "plans" / "01_camera" / "unit.md"
    plan.parent.mkdir()
    plan.write_text("# unit\n" + "bounded execution\n" * 20, encoding="utf-8")

    authority = stamp_work_unit_plan(
        tmp_path, plan, gate={"clean": True, "blocking": 0, "run_id": "plan-run"}
    )

    assert authority is not None and authority.is_file()
    validate_work_unit_plan_authority(tmp_path, plan)
    plan.write_text(plan.read_text(encoding="utf-8") + "edited\n", encoding="utf-8")
    with pytest.raises(ValueError, match="stale or edited"):
        validate_work_unit_plan_authority(tmp_path, plan)


def test_unit_plan_without_clean_gate_attestation_is_not_build_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run 20260824T103842Z-afec73 failed its gate and left the generated plan on the
    shot; the next build trusted the file's existence and built a unit on gate-failed
    authority. An integrity-only stamp must satisfy the gate pipeline and refuse every
    build-time consumer."""
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _write_plan(tmp_path)
    layout = run_artifacts.create(tmp_path, "plan-run")
    publish_current(tmp_path, layout, outcome="clean")
    plan = tmp_path / "plans" / "01_camera" / "unit.md"
    plan.parent.mkdir()
    plan.write_text("# unit\n" + "bounded execution\n" * 20, encoding="utf-8")

    stamp_work_unit_plan(tmp_path, plan)  # integrity only — publication never finished

    validate_work_unit_plan_authority(tmp_path, plan, require_gate=False)  # gate pipeline
    with pytest.raises(ValueError, match="no clean-gate attestation"):
        validate_work_unit_plan_authority(tmp_path, plan)  # build-time consumer

    # a hand-edited attestation cannot claim cleanliness the gate never granted
    authority = plan.with_name(plan.name + ".authority.json")
    record = json.loads(authority.read_text(encoding="utf-8"))
    record["gate_clean"] = False
    record["gate_blocking"] = 3
    record["gate_run_id"] = "forged"
    authority.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no clean-gate attestation"):
        validate_work_unit_plan_authority(tmp_path, plan)

    with pytest.raises(ValueError, match="only be stamped for a clean gate result"):
        stamp_work_unit_plan(tmp_path, plan, gate={"clean": False, "blocking": 3, "run_id": "x"})


def test_v1_authority_sidecars_predate_gate_attestation_and_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _write_plan(tmp_path)
    layout = run_artifacts.create(tmp_path, "plan-run")
    publish_current(tmp_path, layout, outcome="clean")
    plan = tmp_path / "plans" / "01_camera" / "unit.md"
    plan.parent.mkdir()
    plan.write_text("# unit\n" + "bounded execution\n" * 20, encoding="utf-8")
    stamp_work_unit_plan(tmp_path, plan, gate={"clean": True, "blocking": 0, "run_id": "r"})
    authority = plan.with_name(plan.name + ".authority.json")
    record = json.loads(authority.read_text(encoding="utf-8"))
    record["schema"] = "vfx-harness.unit-plan-authority/v1"
    for key in ("gate_clean", "gate_blocking", "gate_run_id"):
        record.pop(key)
    authority.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="stale or edited"):
        validate_work_unit_plan_authority(tmp_path, plan, require_gate=False)


def test_consumer_view_preserves_jit_plan_and_authority_sidecar_as_one_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _write_plan(tmp_path)
    rel = Path("plans/01_camera/unit.md")
    layers = {
        "schema": 4,
        "layers": [{"id": "1", "stages": [{"id": "unit", "plan": rel.as_posix()}]}],
    }
    (tmp_path / "layers.json").write_text(json.dumps(layers) + "\n", encoding="utf-8")
    layout = run_artifacts.create(tmp_path, "plan-run")
    publish_current(tmp_path, layout, outcome="clean")
    plan = tmp_path / rel
    plan.parent.mkdir(parents=True)
    plan.write_text("# unit\n" + "bounded execution\n" * 20, encoding="utf-8")
    authority = stamp_work_unit_plan(
        tmp_path, plan, gate={"clean": True, "blocking": 0, "run_id": "plan-run"}
    )
    assert authority is not None

    view = prepare_consumer_view(layout)
    view_plan = view / rel
    view_authority = view_plan.with_name(view_plan.name + ".authority.json")

    assert view_plan.resolve() == plan.resolve()
    assert view_authority.resolve() == authority.resolve()
    validate_work_unit_plan_authority(view, view_plan)
    plan.write_text(plan.read_text(encoding="utf-8") + "tampered\n", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid selected-bundle authority"):
        validate_work_unit_plan_authority(view, view_plan)


def test_planner_can_read_only_its_assigned_snapshot_outside_staging(tmp_path: Path) -> None:
    workspace = tmp_path / "runs" / "one" / "scratch" / "plan-workspace"
    workspace.mkdir(parents=True)
    snapshot = tmp_path / "runs" / "one" / "checkpoints" / "repair.md"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text("# snapshot\n", encoding="utf-8")
    hook = planner_path_scope(workspace, readable_files=(snapshot,)).hooks[0]

    def invoke(tool: str, target: Path) -> dict:
        return anyio.run(
            hook,
            {"tool_name": tool, "tool_input": {"file_path": str(target)}},
            None,
            None,
        )

    assert invoke("Read", snapshot) == {}
    assert invoke("Write", workspace / "plans" / "global.md") == {}
    for tool, target in (
        ("Edit", workspace / "brief.md"),
        ("Write", workspace / "refs" / "one.png"),
        ("Edit", workspace / ".plan-workspace.json"),
        ("Read", tmp_path / "plans" / "global.md"),
        ("Edit", tmp_path / "plans" / "global.md"),
        ("Write", snapshot),
    ):
        denied = invoke(tool, target)
        assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"

    denied_glob = anyio.run(
        hook,
        {"tool_name": "Glob", "tool_input": {"pattern": "../../../../plans/**"}},
        None,
        None,
    )
    assert denied_glob["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_planner_path_denial_enumerates_staged_relative_reads(tmp_path: Path) -> None:
    workspace = tmp_path / "plan-workspace"
    (workspace / "refs").mkdir(parents=True)
    (workspace / "plans").mkdir()
    (workspace / "brief.md").write_text("# brief\n", encoding="utf-8")
    (workspace / "ownership_mapping.json").write_text("{}\n", encoding="utf-8")
    (workspace / "plans" / "global.draft.md").write_text("# draft\n", encoding="utf-8")
    (workspace / "refs" / "frame.png").write_bytes(b"ref")
    other = tmp_path / "other-project" / "brief.md"
    other.parent.mkdir()
    other.write_text("# foreign\n", encoding="utf-8")
    hook = planner_path_scope(workspace).hooks[0]
    denied = anyio.run(
        hook,
        {"tool_name": "Read", "tool_input": {"file_path": str(other)}},
        None,
        None,
    )
    reason = denied["hookSpecificOutput"]["permissionDecisionReason"]
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert str(other) in reason
    assert "do not prefix another filesystem root" in reason
    for name in ("brief.md", "ownership_mapping.json", "plans/global.draft.md", "refs/frame.png"):
        assert name in reason
    names, total = staged_relative_reads(workspace)
    assert total == 4
    assert names == [
        "brief.md",
        "ownership_mapping.json",
        "plans/global.draft.md",
        "refs/frame.png",
    ]
    assert format_staged_relative_reads(workspace) == ", ".join(names)


def test_staged_relative_reads_cap_names_remainder(tmp_path: Path) -> None:
    for index in range(3):
        (tmp_path / f"file-{index}.txt").write_text("x\n", encoding="utf-8")
    names, total = staged_relative_reads(tmp_path, limit=1)
    assert total == 3
    assert names == ["file-0.txt"]
    assert format_staged_relative_reads(tmp_path, limit=1) == "file-0.txt, and 2 more"


def test_verifier_kickoff_compiles_relative_workspace_reads(tmp_path: Path) -> None:
    (tmp_path / "refs").mkdir()
    (tmp_path / "plans").mkdir()
    (tmp_path / "brief.md").write_text("# brief\n", encoding="utf-8")
    (tmp_path / "ownership_mapping.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "plans" / "global.draft.md").write_text("# draft\n", encoding="utf-8")
    still = tmp_path / "refs" / "frame.png"
    still.write_bytes(b"ref")
    shot = SimpleNamespace(
        id="fixture",
        frames=24,
        fps=24,
        engine="BLENDER_EEVEE_NEXT",
        folder=tmp_path,
        refs=[still],
    )
    text = verifier_user_prompt(shot, "plans/global.draft.md")
    assert "there is no source video" not in text
    assert "clips inside refs/" in text
    assert "do not prefix another project or filesystem root" in text
    assert "Open first: `brief.md`, `plans/global.draft.md`, `ownership_mapping.json`." in text
    assert "Staged relative files: brief.md, ownership_mapping.json, plans/global.draft.md, refs/frame.png." in text


def test_layer_planner_can_write_only_its_exact_jit_target_and_never_a_bundle(
    tmp_path: Path,
) -> None:
    target = tmp_path / "plans" / "01_camera" / "unit.md"
    bundle_member = (
        tmp_path / "runs" / "one" / "checkpoints" / "plans" / "bundles" / "hash"
        / "plans" / "01_camera" / "unit.md"
    )
    bundle_member.parent.mkdir(parents=True)
    hook = planner_path_scope(tmp_path, writable_files=(target,)).hooks[0]

    def invoke(path: Path) -> dict:
        return anyio.run(
            hook,
            {"tool_name": "Write", "tool_input": {"file_path": str(path)}},
            None,
            None,
        )

    assert invoke(target) == {}
    for denied_path in (tmp_path / "plans" / "other.md", bundle_member):
        denied = invoke(denied_path)
        assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_jit_planner_reads_only_authored_inputs_selected_bundle_and_target(tmp_path: Path) -> None:
    target = tmp_path / "plans" / "01_camera" / "unit.md"
    selected = tmp_path / "runs" / "selected" / "checkpoints" / "plans" / "bundles" / "hash"
    selected.mkdir(parents=True)
    (selected / "global.md").write_text("# selected\n", encoding="utf-8")
    old = tmp_path / "runs" / "old" / "scratch" / "plan-workspace" / "brief.md"
    old.parent.mkdir(parents=True)
    old.write_text("# stale\n", encoding="utf-8")
    (tmp_path / "brief.md").write_text("# authored\n", encoding="utf-8")
    refs = tmp_path / "refs"
    refs.mkdir()
    (refs / "one.png").write_bytes(b"ref")
    hook = planner_path_scope(
        tmp_path,
        readable_files=(tmp_path / "brief.md", target),
        readable_roots=(selected,),
        writable_files=(target,),
        strict_reads=True,
    ).hooks[0]

    def invoke(tool: str, arguments: dict[str, str]) -> dict:
        return anyio.run(
            hook,
            {"tool_name": tool, "tool_input": arguments},
            None,
            None,
        )

    for tool, arguments in (
        ("Read", {"file_path": str(tmp_path / "brief.md")}),
        ("Read", {"file_path": str(refs / "one.png")}),
        ("Read", {"file_path": str(selected / "global.md")}),
        ("Read", {"file_path": str(target)}),
        ("Grep", {"path": str(selected), "pattern": "camera"}),
    ):
        assert invoke(tool, arguments) == {}
    for tool, arguments in (
        ("Read", {"file_path": str(old)}),
        ("Glob", {"pattern": "**/brief.md"}),
        ("Glob", {"pattern": "runs/selected/**"}),
        ("Grep", {"pattern": "camera"}),
        ("Read", {"file_path": str(tmp_path / "build" / "old.py")}),
    ):
        denied = invoke(tool, arguments)
        assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"

    hooks = planner_hooks(
        tmp_path,
        readable_roots=(selected,),
        writable_files=(target,),
        strict_reads=True,
        completion_gate=False,
    )
    assert "Stop" not in hooks


def test_builder_can_read_selected_bundle_but_cannot_discover_historical_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _write_plan(tmp_path)
    selected_layout = run_artifacts.create(tmp_path, "selected")
    bundle = publish_current(tmp_path, selected_layout, outcome="clean")
    active_layout = run_artifacts.create(tmp_path, "active-build")
    journal = active_layout.checkpoints / "journals" / "unit.py"
    journal.parent.mkdir(parents=True)
    journal.write_text("# current journal\n", encoding="utf-8")
    old_plan = tmp_path / "runs" / "old" / "checkpoints" / "plans" / "global.md"
    old_plan.parent.mkdir(parents=True)
    old_plan.write_text("# stale\n", encoding="utf-8")
    hook = selected_plan_read_guard(tmp_path).hooks[0]

    def invoke(tool: str, arguments: dict[str, str]) -> dict:
        return anyio.run(
            hook,
            {"tool_name": tool, "tool_input": arguments},
            None,
            None,
        )

    assert invoke("Read", {"file_path": str(bundle.root / "global.md")}) == {}
    assert invoke("Read", {"file_path": str(journal)}) == {}
    assert invoke(
        "Glob",
        {"pattern": "runs/active-build/checkpoints/journals/*"},
    ) == {}
    assert invoke("Read", {"file_path": str(tmp_path / "brief.md")}) == {}
    assert invoke("Glob", {"pattern": "refs/**/*.png"}) == {}
    for tool, arguments in (
        ("Read", {"file_path": str(old_plan)}),
        ("Read", {"file_path": str(tmp_path / "plans" / "global.md")}),
        ("Read", {"file_path": str(tmp_path / "scene_checks.json")}),
        ("Read", {"file_path": str(tmp_path / "layers.json")}),
        ("Glob", {"pattern": "**/global.md"}),
        ("Glob", {"pattern": "plans/**/*.md"}),
        ("Glob", {"pattern": "runs/**/global.md"}),
        ("Grep", {"pattern": "authority"}),
    ):
        denied = invoke(tool, arguments)
        assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_publication_rejects_an_arbitrary_directory_inside_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    (tmp_path / "brief.md").write_text("# brief\n", encoding="utf-8")
    (tmp_path / "refs").mkdir()
    layout = run_artifacts.create(tmp_path, "fresh-run")
    impostor = layout.scratch / "other"
    _write_plan(impostor)

    with pytest.raises(PlanPublicationError, match="producing run's plan workspace"):
        publish_current(tmp_path, layout, outcome="clean", source_root=impostor)


def test_incomplete_publication_leaves_previous_pointer_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _write_plan(tmp_path)
    first = run_artifacts.create(tmp_path, "plan-run-1")
    published = publish_current(tmp_path, first, outcome="clean")
    pointer = tmp_path / "plans" / "current.json"
    before = pointer.read_bytes()

    (tmp_path / "scene_checks.json").unlink()
    second = run_artifacts.create(tmp_path, "plan-run-2")
    with pytest.raises(PlanPublicationError, match=r"scene_checks\.json"):
        publish_current(tmp_path, second, outcome="clean")

    assert pointer.read_bytes() == before
    assert resolve_current(tmp_path) == published


def test_repair_snapshots_are_isolated_by_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    plan = tmp_path / "plans" / "global.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("# first\n", encoding="utf-8")
    first = run_artifacts.create(tmp_path, "plan-run-1")
    first_snapshot = snapshot_repair_input(first, 1, plan)

    plan.write_text("# second\n", encoding="utf-8")
    second = run_artifacts.create(tmp_path, "plan-run-2")
    second_snapshot = snapshot_repair_input(second, 1, plan)

    assert first_snapshot != second_snapshot
    assert first_snapshot.read_text(encoding="utf-8") == "# first\n"
    assert second_snapshot.read_text(encoding="utf-8") == "# second\n"
    assert first_snapshot.is_relative_to(first.root)
    assert second_snapshot.is_relative_to(second.root)


def test_global_plan_roles_have_declared_patch_and_gate_capabilities() -> None:
    for role in ("draft", "verify", "repair"):
        capabilities = plan_role_capabilities(role)
        assert {"author", "patch", "gate", "escalate"} <= capabilities.verbs
        assert "measure" not in capabilities.verbs
        assert capabilities.include_gate is True
        assert "Edit" in capabilities.allowed_tools
        assert "Edit" not in capabilities.denied_tools
        assert "Bash" in capabilities.denied_tools

    assert {"Task", "Agent"} <= plan_role_capabilities("repair").denied_tools


def test_unknown_plan_role_fails_closed() -> None:
    with pytest.raises(ValueError, match="unknown global plan role"):
        plan_role_capabilities("invented")


def test_global_tool_routing_excludes_preproduction_capabilities() -> None:
    names = [
        "mcp__plan__measure_ref",
        "mcp__plan__measure_checks",
        "mcp__plan__spike",
        "mcp__plan__ask_supervisor",
        "mcp__plan__run_gate",
    ]

    assert _phase_tools(names, "ask_supervisor", "run_gate") == [
        "mcp__plan__ask_supervisor",
        "mcp__plan__run_gate",
    ]
