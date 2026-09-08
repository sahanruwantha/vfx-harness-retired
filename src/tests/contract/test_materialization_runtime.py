"""Production materialization uses Flynn while VFX retains its publication fence."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from types import SimpleNamespace

import flynn_agents_sdk as flynn
import pytest
from flynn_agents_sdk import deepseek

from tests.contract.test_flynn_materialization_tools import bound as bound
from tests.contract.test_global_planning_session import Scripted
from tests.unit.test_plan_records import _passed_layer_one_outcome
from vfx_harness.agents import materialization_runtime
from vfx_harness.agents.planner import generate, rematerialize
from vfx_harness.evaluation import plan_gate
from vfx_harness.evaluation.plan_gate.types import GateResult
from vfx_harness.infrastructure.config import Settings
from vfx_harness.orchestration import authority_selection, ledger
from vfx_harness.orchestration.builder_execution_fence import (
    BuilderExecutionFenceActive,
    BuilderExecutionFenceError,
    builder_execution_fence,
    require_builder_execution_lease,
)


class Adapter(Scripted):
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def configure(monkeypatch, adapter):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-key")
    monkeypatch.delenv("VFXH_RUN_MAX_USD", raising=False)
    monkeypatch.setenv("VFXH_MATERIALIZATION_SECONDS", "90")
    monkeypatch.setenv("VFXH_MATERIALIZATION_MODEL", deepseek.VISION_MODEL)
    monkeypatch.setenv("VFXH_MATERIALIZATION_OUTPUT_TOKENS", "16384")
    calls = []

    def factory(**kwargs):
        calls.append(kwargs)
        return adapter

    monkeypatch.setattr(materialization_runtime.deepseek, "DeepSeekAdapter", factory)
    return calls


@pytest.mark.parametrize("public", [False, True])
def test_production_materializer_stages_finalizes_and_publishes_through_flynn(bound, monkeypatch, public):
    layout, _candidate, full = bound
    _passed_layer_one_outcome(layout.shot)
    selected = authority_selection.resolve_selected_authority(layout.shot)
    layer = ledger.load_layers_from_path(selected.artifact_paths["layers.json"])["2"]
    unit = full["layer"]["stages"][0]
    unit["provides"] = []
    unit["mutates"].pop("roles")
    unit["mutates"].update(role_namespace="polish.comp", role_members=["$self"])
    unit["mutates"]["control_roles"] = {"hold": ["$self"]}
    adapter = Adapter([
        ("stage_materialization_unit", {"unit": unit, "scene_contracts": full["scene_contracts"],
                                        "requirement_bindings": full["requirement_bindings"]}),
        ("finalize_materialization", {}),
    ])
    calls = configure(monkeypatch, adapter)
    monkeypatch.setattr(plan_gate, "run", lambda *args, **kwargs: GateResult("fixture", [], {}))
    monkeypatch.setattr(rematerialize.run_artifacts, "active", lambda _: layout)
    shot = SimpleNamespace(folder=layout.shot)
    monkeypatch.setattr(generate.planner_package(), "load_shot", lambda _: shot)

    async def invoke():
        if public:
            return await generate.generate_layer_plan(layout.shot, "2", materialize_only=True)
        with builder_execution_fence(layout.shot) as lease:
            await rematerialize._materialize_deferred_layer(
                shot, layer, model=deepseek.VISION_MODEL,
                blender="blender", max_turns=8, fence_lease=lease,
            )

    asyncio.run(invoke())
    assert len(adapter.requests) == 2
    assert calls[0]["model"] == deepseek.VISION_MODEL and calls[0]["timeout_seconds"] == 90
    assert authority_selection.resolve_selected_authority(layout.shot).selection_token != selected.selection_token
    audits = list(layout.reports.glob("materialization-session-*.json"))
    assert len(audits) == 1
    audit = json.loads(audits[0].read_text())
    assert audit["finalization_current"] and not audit["authority_selected"]
    assert (layout.root / audit["journal"]).is_file()
    with flynn.SQLiteRun.open(layout.root / audit["journal"]) as run:
        operations = run.records()["operations"]
        assert len(operations) == 2
        assert all(row["request"] and row["call"] and row["output"] for row in operations)
        assert audit["usage"] == run.usage_summary()


@pytest.mark.parametrize("problem", ["key", "model", "usd", "released_fence"])
def test_invalid_runtime_refuses_before_adapter_creation(bound, monkeypatch, problem):
    calls = configure(monkeypatch, Adapter([]))
    if problem == "key":
        monkeypatch.delenv("DEEPSEEK_API_KEY")
    elif problem == "usd":
        monkeypatch.setenv("VFXH_RUN_MAX_USD", "1")
    with builder_execution_fence(bound[0].shot) as lease:
        async def invoke():
            return await materialization_runtime.execute(
                layout=bound[0], candidate=bound[1], charter="charter", kickoff="kickoff",
                model="unsupported" if problem == "model" else deepseek.VISION_MODEL,
                blender="blender", max_turns=4, fence_lease=lease,
            )
        if problem != "released_fence":
            with pytest.raises(ValueError):
                asyncio.run(invoke())
    if problem == "released_fence":
        with pytest.raises(BuilderExecutionFenceError):
            asyncio.run(invoke())
    assert not calls


def test_materialization_settings_do_not_change_legacy_unit_model(monkeypatch):
    monkeypatch.setenv("VFXH_MATERIALIZATION_MODEL", "native-model")
    monkeypatch.setenv("VFXH_PLANNER_MODEL", "unit-model")
    monkeypatch.setenv("VFXH_MATERIALIZATION_SECONDS", "123")
    monkeypatch.setenv("VFXH_MATERIALIZATION_OUTPUT_TOKENS", "4567")
    settings = Settings.from_environment(load_dotenv_file=False)
    assert settings.materialization_model == "native-model" and settings.planner_model == "unit-model"
    assert settings.materialization_seconds == 123 and settings.materialization_output_tokens == 4567


def test_native_provider_boundary_imports_without_claude():
    result = subprocess.run([sys.executable, "-c",
        "import sys; sys.modules['claude_agent_sdk'] = None; "
        "from vfx_harness.agents.materialization_runtime import execute"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("existing", [False, True])
def test_public_materialization_retains_a_live_shot_fence(bound, monkeypatch, existing):
    captured = []

    async def inner(folder, layer_id, **kwargs):
        require_builder_execution_lease(kwargs["fence_lease"], folder)
        captured.append(kwargs["fence_lease"])
        return bound[1]

    monkeypatch.setattr(generate, "_generate_layer_plan", inner)
    if existing:
        with builder_execution_fence(bound[0].shot) as lease:
            asyncio.run(generate.generate_layer_plan(bound[0].shot, "2", materialize_only=True, fence_lease=lease))
            assert captured == [lease]
    else:
        asyncio.run(generate.generate_layer_plan(bound[0].shot, "2", materialize_only=True))
    with pytest.raises(BuilderExecutionFenceError):
        require_builder_execution_lease(captured[0], bound[0].shot)


def test_materialization_cannot_race_an_owned_builder(bound):
    with builder_execution_fence(bound[0].shot), pytest.raises(BuilderExecutionFenceActive):
        asyncio.run(generate.generate_layer_plan(bound[0].shot, "2", materialize_only=True))
