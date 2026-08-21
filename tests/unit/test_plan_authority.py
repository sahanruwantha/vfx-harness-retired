from __future__ import annotations

import json
from pathlib import Path

import anyio
import pytest

from vfx_harness.agents.plan_guardrails import planner_path_scope
from vfx_harness.agents.planner import plan_role_capabilities
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration.plan_authority import (
    PlanPublicationError,
    prepare_staging,
    publish_current,
    resolve_current,
    snapshot_repair_input,
)


def _write_plan(folder: Path, *, marker: str = "one") -> None:
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


def test_clean_plan_publishes_one_immutable_bundle_behind_atomic_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _write_plan(tmp_path)
    layout = run_artifacts.create(tmp_path, "plan-run-1")

    published = publish_current(tmp_path, layout, outcome="clean")
    pointer = json.loads((tmp_path / "plans" / "current.json").read_text(encoding="utf-8"))
    resolved = resolve_current(tmp_path)

    assert pointer["schema"] == "vfx-harness.plan-pointer/v1"
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
    }

    _write_plan(tmp_path, marker="two")
    assert (published.root / "global.md").read_text(encoding="utf-8") == "# plan one\n"
    assert resolve_current(tmp_path) == published


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
    assert not (workspace / "plans" / "global.md").exists()
    assert not (workspace / "layers.json").exists()
    assert not (workspace / "questions.jsonl").exists()
    assert not (workspace / "runs").exists()


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
        assert {"author", "patch", "measure", "gate", "escalate"} <= capabilities.verbs
        assert capabilities.include_gate is True
        assert "Edit" in capabilities.allowed_tools
        assert "Edit" not in capabilities.denied_tools
        assert "Bash" in capabilities.denied_tools

    assert {"Task", "Agent"} <= plan_role_capabilities("repair").denied_tools


def test_unknown_plan_role_fails_closed() -> None:
    with pytest.raises(ValueError, match="unknown global plan role"):
        plan_role_capabilities("invented")
