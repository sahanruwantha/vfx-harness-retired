from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from bambi_vfx.agents.builder import builder_model, critic_model, script_model
from bambi_vfx.config import DEFAULT_CRITIC_MODEL, DEFAULT_EXECUTION_MODEL, Settings
from bambi_vfx.revalidation import input_manifest

MODEL_VARIABLES = (
    "BVFX_EXECUTION_MODEL",
    "BVFX_PLANNER_MODEL",
    "BVFX_BUILDER_MODEL",
    "BVFX_SCRIPT_MODEL",
    "BVFX_REVIEWER_MODEL",
    "BVFX_ASSET_MODEL",
    "BVFX_DISTILLER_MODEL",
    "BVFX_CRITIC_MODEL",
)


def _clear(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in MODEL_VARIABLES:
        monkeypatch.delenv(name, raising=False)


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
    monkeypatch.setenv("BVFX_EXECUTION_MODEL", "execution-control")
    monkeypatch.setenv("BVFX_SCRIPT_MODEL", "script-control")
    monkeypatch.setenv("BVFX_CRITIC_MODEL", "judge-control")
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
    monkeypatch.setenv("BVFX_BUILDER_MODEL", "   ")
    with pytest.raises(ValueError, match="BVFX_BUILDER_MODEL must not be empty"):
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
    monkeypatch.setenv("BVFX_BUILDER_MODEL", "builder-experiment")
    changed = input_manifest(tmp_path, layer, blender_version="5.2")

    assert baseline["models"]["builder"] == "claude-sonnet-5"
    assert changed["models"]["builder"] == "builder-experiment"
    assert baseline != changed
