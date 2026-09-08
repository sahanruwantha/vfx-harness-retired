"""Production unit planning retains real claim, fence and final-gate boundaries."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest
from flynn_agents_sdk import deepseek

from tests.contract.test_materialization_runtime import Adapter
from tests.contract.test_unit_planning_session import bound as bound
from tests.contract.test_unit_planning_session import materialization_bound as materialization_bound
from tests.contract.test_unit_planning_session import steps
from vfx_harness.agents import unit_planning_runtime
from vfx_harness.agents.builder.attempt_guard import UnitAttemptGuard
from vfx_harness.agents.planner import generate
from vfx_harness.domain.brief import Shot
from vfx_harness.evaluation import plan_gate
from vfx_harness.evaluation.plan_gate.types import Finding, GateResult
from vfx_harness.infrastructure.config import Settings
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration import ledger, unit_state, unit_state_claims
from vfx_harness.orchestration.authority_capsule_resolution import selected_layer_capsule_digest
from vfx_harness.orchestration.builder_execution_fence import builder_execution_fence
from vfx_harness.orchestration.layer_plans import validate_work_unit_plan_authority


def claim(bound):
    layout, selected, _, layer_id, unit_id = bound
    layer = ledger.load_layers_from_path(selected.artifact_paths["layers.json"])[layer_id]
    unit = next(row for row in layer.stages if row.id == unit_id)
    plan_hash = selected_layer_capsule_digest(layout.shot, layer_id, selected)
    unit_state.initialize(layout.shot, layer_id, layer.stages, plan_hash=plan_hash)
    attempt = unit_state_claims.claim_ready_unit_for_planning(
        layout.shot, layer_id, unit_id, layer.stages, expected_plan_hash=plan_hash,
        eligible_passed=set(), completion_authorization=None, run_id=layout.run_id,
        selection_token=selected.selection_token, reason="native unit planning fixture",
    )
    return UnitAttemptGuard.bind(layout.shot, layer_id, unit, layer.stages, attempt,
                                 expected_plan_hash=plan_hash, selected_authority=selected)


def configure(bound, monkeypatch, adapter):
    layout = bound[0]
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-key")
    monkeypatch.setenv("VFXH_PLANNER_MODEL", deepseek.VISION_MODEL)
    monkeypatch.setenv("VFXH_UNIT_PLAN_SECONDS", "90")
    monkeypatch.setenv("VFXH_UNIT_PLAN_OUTPUT_TOKENS", "16384")
    monkeypatch.delenv("VFXH_RUN_MAX_USD", raising=False)
    monkeypatch.setenv(run_artifacts.ENV, str(layout.root))
    shot = Shot(layout.shot, {"frames": 240, "fps": 24, "resolution": [64, 64]}, "Fixture")
    monkeypatch.setattr(generate.planner_package(), "load_shot", lambda _: shot)
    constructed = []

    def factory(**kwargs):
        constructed.append(kwargs)
        return adapter

    monkeypatch.setattr(unit_planning_runtime.deepseek, "DeepSeekAdapter", factory)
    return constructed


def run(bound, guard):
    async def execute():
        with builder_execution_fence(bound[0].shot) as lease:
            return await generate.generate_layer_plan(
                bound[0].shot, bound[3], unit_id=bound[4], attempt_guard=guard, fence_lease=lease,
            )
    return asyncio.run(execute())


@pytest.mark.parametrize("terminal_clean", [True, False])
def test_production_claimed_planner_requires_independent_terminal_gate(bound, monkeypatch, terminal_clean):
    guard = claim(bound)
    adapter = Adapter(steps())
    configured = configure(bound, monkeypatch, adapter)
    monkeypatch.setattr(plan_gate, "run", lambda *a, **kw: GateResult("preview", [], {}))
    findings = [] if terminal_clean else [Finding.in_layer("STATE", True, "1", "unit", "terminal refusal")]
    terminal_calls = []

    def terminal(*a, **kw):
        terminal_calls.append(a)
        return GateResult("terminal", findings, {})

    monkeypatch.setattr(generate, "run_plan_gate", terminal)
    if terminal_clean:
        assert run(bound, guard) == bound[2]
        validate_work_unit_plan_authority(bound[0].shot, bound[2], selected_authority=bound[1])
    else:
        with pytest.raises(RuntimeError, match="terminal refusal"):
            run(bound, guard)
        assert not bound[2].exists()
    assert len(adapter.requests) == 2 and len(terminal_calls) == 1
    assert configured[0]["model"] == deepseek.VISION_MODEL
    assert configured[0]["timeout_seconds"] == 90
    reports = list(bound[0].reports.glob("unit-planning-session-*.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text())
    assert report["preview_clean_at_return"] and not report["gate_attested"]
    guard.check("planning claim survives session completion")


@pytest.mark.parametrize("refusal", ["key", "model", "usd", "phase"])
def test_invalid_production_configuration_refuses_before_provider(bound, monkeypatch, refusal):
    guard = claim(bound)
    adapter = Adapter([])
    configured = configure(bound, monkeypatch, adapter)
    if refusal == "key":
        monkeypatch.delenv("DEEPSEEK_API_KEY")
    elif refusal == "model":
        monkeypatch.setenv("VFXH_PLANNER_MODEL", "claude")
    elif refusal == "usd":
        monkeypatch.setenv("VFXH_RUN_MAX_USD", "1")
    else:
        guard = replace(guard, claim=replace(guard.claim, phase="building"))
    with pytest.raises((ValueError, RuntimeError)):
        run(bound, guard)
    assert configured == [] and adapter.requests == []


def test_unit_budget_settings_do_not_change_materialization(monkeypatch):
    monkeypatch.setenv("VFXH_UNIT_PLAN_SECONDS", "71")
    monkeypatch.setenv("VFXH_UNIT_PLAN_OUTPUT_TOKENS", "1234")
    monkeypatch.setenv("VFXH_MATERIALIZATION_SECONDS", "92")
    settings = Settings.from_environment(load_dotenv_file=False)
    assert settings.unit_plan_seconds == 71 and settings.unit_plan_output_tokens == 1234
    assert settings.materialization_seconds == 92
