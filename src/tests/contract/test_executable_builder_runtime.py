"""Production executable routing keeps exact claims, explicit budgets and no fallback."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import flynn_agents_sdk as flynn
import pytest
from flynn_agents_sdk import deepseek

from tests.integration.test_flynn_unit_lifecycle import _authority
from vfx_harness.agents import builder
from vfx_harness.agents.builder import unit_dispatch
from vfx_harness.infrastructure.config import Settings
from vfx_harness.orchestration.builder_execution_fence import builder_execution_fence
from vfx_harness.orchestration.ledger import Milestone


class Adapter:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


@pytest.fixture
def bound(tmp_path, monkeypatch):
    state = _authority(tmp_path, monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-key")
    monkeypatch.setenv("VFXH_EXECUTABLE_BUILDER_MODEL", deepseek.VISION_MODEL)
    monkeypatch.delenv("VFXH_RUN_MAX_USD", raising=False)
    monkeypatch.setenv("VFXH_EXECUTABLE_BUILDER_SECONDS", "91")
    monkeypatch.setenv("VFXH_EXECUTABLE_BUILDER_OUTPUT_TOKENS", "12345")
    monkeypatch.setenv("VFXH_EXECUTABLE_BUILDER_MAX_STEPS", "7")
    return state


def invoke(bound, *, guard=None, resume=False):
    shot, layer, unit, selected, original, _ = bound
    with builder_execution_fence(shot.folder) as lease:
        return asyncio.run(builder.build_unit(
            shot, Milestone("1@lock", 240, "refs/a.png", "control exists"),
            unit.mutates.script_spans[0], [], object(), layer=layer, active_unit=unit,
            selected_authority=selected, attempt_guard=guard or original,
            fence_lease=lease, resume_ok=resume,
        ))


def test_native_route_receives_exact_arguments_and_budgets(bound, monkeypatch):
    adapters = []

    def factory(**kwargs):
        adapters.append(kwargs)
        return Adapter()

    monkeypatch.setattr(deepseek, "DeepSeekAdapter", factory)
    monkeypatch.setattr(unit_dispatch.unit_loop, "build_unit", lambda *a, **kw: pytest.fail("Claude fallback"))

    async def native(*args, **kwargs):
        assert kwargs["attempt_guard"] is bound[4]
        assert kwargs["selected_authority"] is bound[3]
        limits = kwargs["limits"]
        assert limits == flynn.RunLimits(7, 7, 7, 91, output_tokens=12345)
        assert isinstance(kwargs["inference"], Adapter)
        return "native fixture"

    monkeypatch.setattr(unit_dispatch.flynn_unit, "build_unit", native)
    assert invoke(bound) == "native fixture"
    assert adapters == [{"api_key": "fixture-key", "model": deepseek.VISION_MODEL,
                         "max_tokens": 8192, "timeout_seconds": 91}]


@pytest.mark.parametrize("setting", ["key", "model", "usd", "steps", "resume", "phase", "run"])
def test_invalid_configuration_refuses_before_provider(bound, monkeypatch, setting):
    monkeypatch.setattr(deepseek, "DeepSeekAdapter", lambda **kw: pytest.fail("unexpected provider"))
    monkeypatch.setattr(unit_dispatch.unit_loop, "build_unit", lambda *a, **kw: pytest.fail("Claude fallback"))
    guard = bound[4]
    if setting == "key":
        monkeypatch.delenv("DEEPSEEK_API_KEY")
    elif setting == "model":
        monkeypatch.setenv("VFXH_EXECUTABLE_BUILDER_MODEL", "claude")
    elif setting == "usd":
        monkeypatch.setenv("VFXH_RUN_MAX_USD", "1")
    elif setting == "steps":
        monkeypatch.setenv("VFXH_EXECUTABLE_BUILDER_MAX_STEPS", "3")
    elif setting == "phase":
        guard = replace(guard, claim=replace(guard.claim, phase="planning"))
    elif setting == "run":
        monkeypatch.setattr(unit_dispatch.run_artifacts, "active", lambda _: None)
    with pytest.raises(ValueError):
        invoke(bound, guard=guard, resume=setting == "resume")


def test_native_failure_propagates_without_retry_or_other_engine(bound, monkeypatch):
    monkeypatch.setattr(deepseek, "DeepSeekAdapter", lambda **kw: Adapter())
    monkeypatch.setattr(unit_dispatch.unit_loop, "build_unit", lambda *a, **kw: pytest.fail("Claude fallback"))
    calls = []

    async def fail(*args, **kwargs):
        calls.append(args)
        raise flynn.BudgetExhausted("fixture budget exhausted")

    monkeypatch.setattr(unit_dispatch.flynn_unit, "build_unit", fail)
    with pytest.raises(flynn.BudgetExhausted):
        invoke(bound)
    assert len(calls) == 1


def test_raster_route_stays_explicit_and_does_not_construct_native_provider(bound, monkeypatch):
    monkeypatch.setattr(unit_dispatch.evidence, "_unit_requires_raster", lambda *a, **kw: True)
    monkeypatch.setattr(deepseek, "DeepSeekAdapter", lambda **kw: pytest.fail("raster reached native provider"))

    async def raster(*args, **kwargs):
        assert kwargs["attempt_guard"] is bound[4]
        return "raster fixture"

    monkeypatch.setattr(unit_dispatch.unit_loop, "build_unit", raster)
    assert invoke(bound) == "raster fixture"


def test_native_configuration_is_independent_of_remaining_builder_model(monkeypatch):
    monkeypatch.setenv("VFXH_EXECUTABLE_BUILDER_MODEL", "native")
    monkeypatch.setenv("VFXH_BUILDER_MODEL", "raster")
    settings = Settings.from_environment(load_dotenv_file=False)
    assert settings.executable_builder_model == "native" and settings.builder_model == "raster"
