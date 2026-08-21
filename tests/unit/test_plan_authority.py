from __future__ import annotations

import json
from pathlib import Path

import pytest

from vfx_harness.agents.planner import plan_role_capabilities
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration.plan_authority import (
    PlanPublicationError,
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
