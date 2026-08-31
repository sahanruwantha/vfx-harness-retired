from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from vfx_harness.agents.builder import builder_model, critic_model, script_model
from vfx_harness.infrastructure import config
from vfx_harness.infrastructure.config import (
    DEFAULT_CRITIC_MODEL,
    DEFAULT_EXECUTION_MODEL,
    PROJECT_ROOT,
    Settings,
)
from vfx_harness.orchestration import revalidation
from vfx_harness.orchestration.revalidation import input_manifest

MODEL_VARIABLES = (
    "VFXH_EXECUTION_MODEL",
    "VFXH_PLANNER_MODEL",
    "VFXH_BUILDER_MODEL",
    "VFXH_SCRIPT_MODEL",
    "VFXH_REVIEWER_MODEL",
    "VFXH_ASSET_MODEL",
    "VFXH_DISTILLER_MODEL",
    "VFXH_CRITIC_MODEL",
)


def _clear(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in MODEL_VARIABLES:
        monkeypatch.delenv(name, raising=False)


def test_project_root_is_checkout_root_not_src_directory() -> None:
    checkout = Path(__file__).resolve().parents[3]

    assert checkout == PROJECT_ROOT
    assert (PROJECT_ROOT / "pyproject.toml").is_file()


def test_non_checkout_layout_requires_explicit_env_file(tmp_path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("ANTHROPIC_API_KEY=secret\n", encoding="utf-8")
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)

    assert config.environment_file() is None
    assert config.environment_file(env_file) == env_file


def test_default_lane_uses_sonnet_execution_and_opus_critic(monkeypatch):
    _clear(monkeypatch)
    settings = Settings.from_environment(load_dotenv_file=False)

    assert settings.execution_model == DEFAULT_EXECUTION_MODEL == "claude-sonnet-5"
    assert settings.critic_model == DEFAULT_CRITIC_MODEL == "claude-opus-5"
    assert {
        settings.planner_model,
        settings.builder_model,
        settings.script_model,
        settings.reviewer_model,
        settings.asset_model,
        settings.distiller_model,
    } == {"claude-sonnet-5"}


def test_global_execution_lane_and_role_overrides_are_independent(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("VFXH_EXECUTION_MODEL", "execution-control")
    monkeypatch.setenv("VFXH_SCRIPT_MODEL", "script-control")
    monkeypatch.setenv("VFXH_CRITIC_MODEL", "judge-control")
    settings = Settings.from_environment(load_dotenv_file=False)

    assert settings.planner_model == "execution-control"
    assert settings.builder_model == "execution-control"
    assert settings.script_model == "script-control"
    assert settings.critic_model == "judge-control"
    assert builder_model() == "execution-control"
    assert script_model() == "script-control"
    assert critic_model() == "judge-control"


def test_empty_model_override_fails_closed(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("VFXH_BUILDER_MODEL", "   ")
    with pytest.raises(ValueError, match="VFXH_BUILDER_MODEL must not be empty"):
        Settings.from_environment(load_dotenv_file=False)


def test_model_lane_is_part_of_revalidation_boundary(monkeypatch, tmp_path):
    _clear(monkeypatch)
    unit = SimpleNamespace(plan="plans/01_layout.md")
    layer = SimpleNamespace(
        id="1",
        script="build/01_layout.py",
        judges=((1, "refs/f001.png"),),
        stages=(unit,),
    )
    (tmp_path / "plans").mkdir()
    (tmp_path / "plans" / "01_layout.md").write_text("unit plan\n", encoding="utf-8")
    (tmp_path / "layers.json").write_text(
        json.dumps({"schema": 4, "layers": [{"id": "1", "script": layer.script}]}),
        encoding="utf-8",
    )
    baseline = input_manifest(tmp_path, layer, blender_version="5.2")
    monkeypatch.setenv("VFXH_BUILDER_MODEL", "builder-experiment")
    changed = input_manifest(tmp_path, layer, blender_version="5.2")

    assert baseline["models"]["builder"] == "claude-sonnet-5"
    assert changed["models"]["builder"] == "builder-experiment"
    assert baseline != changed


def test_input_manifest_uses_one_selected_snapshot_without_hybrid_reads(
    monkeypatch,
    tmp_path,
) -> None:
    bundle_root = tmp_path / "runs" / "bundle-a" / "plan_gate"
    effective_root = tmp_path / "state" / "jit" / "view-a"
    bundle_plan = bundle_root / "plans" / "01_layout.md"
    effective_layers = effective_root / "layers.json"
    bundle_plan.parent.mkdir(parents=True)
    effective_root.mkdir(parents=True)
    (tmp_path / "build").mkdir()
    bundle_plan.write_text("bundle A unit plan\n", encoding="utf-8")
    (bundle_root / "global.md").write_text("bundle A global plan\n", encoding="utf-8")
    (bundle_root / "layers.json").write_text(
        json.dumps(
            {
                "schema": 5,
                "layers": [
                    {
                        "id": "1",
                        "script": "build/sparse-must-not-run.py",
                        "depends_on": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    effective_layers.write_text(
        json.dumps(
            {
                "schema": 4,
                "layers": [
                    {
                        "id": "1",
                        "script": "build/effective-a.py",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "build" / "effective-a.py").write_text(
        "# accepted effective script A\n",
        encoding="utf-8",
    )

    effective_names = (
        "acceptance.json",
        "critic_axes.json",
        "checks.json",
        "scene_checks.json",
    )
    for name in effective_names:
        (effective_root / name).write_text(f"effective A {name}\n", encoding="utf-8")
    bundle_names = (
        "requirements.json",
        "obligations.json",
        "assumptions.json",
        "plan.provenance.json",
    )
    for name in bundle_names:
        (bundle_root / name).write_text(f"bundle A {name}\n", encoding="utf-8")

    artifacts = (
        "global.md",
        "layers.json",
        *effective_names,
        *bundle_names,
        "plans/01_layout.md",
    )
    artifact_paths = {name: bundle_root / name for name in artifacts}
    artifact_paths.update(
        {name: effective_root / name for name in ("layers.json", *effective_names)}
    )
    snapshot = SimpleNamespace(
        plan=SimpleNamespace(
            bundle=SimpleNamespace(
                root=bundle_root,
                artifacts=artifacts,
                content_hash="a" * 64,
            )
        ),
        artifact_paths=artifact_paths,
    )
    resolver_calls = []

    def selected_once(folder):
        resolver_calls.append(Path(folder))
        if len(resolver_calls) != 1:
            raise AssertionError("input manifest re-resolved selected authority")
        return snapshot

    def stale_resolver(*args, **kwargs):
        raise AssertionError("input manifest used a per-artifact selection resolver")

    monkeypatch.setattr(revalidation, "resolve_selected_authority", selected_once)
    monkeypatch.setattr(revalidation, "selected_artifact_path", stale_resolver)
    unit = SimpleNamespace(plan="plans/01_layout.md")
    layer = SimpleNamespace(
        id="1",
        script="build/effective-a.py",
        judges=(),
        stages=(unit,),
    )

    manifest = input_manifest(tmp_path, layer, blender_version="5.2")

    assert resolver_calls == [tmp_path]
    assert manifest["files"]["state/jit/view-a/layers.json"] == hashlib.sha256(
        effective_layers.read_bytes()
    ).hexdigest()
    assert manifest["files"]["runs/bundle-a/plan_gate/global.md"] == hashlib.sha256(
        (bundle_root / "global.md").read_bytes()
    ).hexdigest()
    assert manifest["files"][
        "runs/bundle-a/plan_gate/plans/01_layout.md"
    ] == hashlib.sha256(bundle_plan.read_bytes()).hexdigest()
    assert "build/effective-a.py" in manifest["files"]
    assert "build/sparse-must-not-run.py" not in manifest["files"]
