"""Production Flynn planning uses the live root owner and exact phase evidence."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from dataclasses import replace

import httpx
import pytest
from flynn_agents_sdk import deepseek

from tests.contract.test_flynn_mapping_publication import bound as bound
from vfx_harness.agents import global_planner, planner
from vfx_harness.agents.planner import generate as jit_generate
from vfx_harness.application import global_plan_stage
from vfx_harness.infrastructure.config import Settings
from vfx_harness.observability import run_artifacts
from vfx_harness.observability.run_owner_fence import RunOwnerFenceError
from vfx_harness.orchestration import authority_selection, run_owner_boundary


@pytest.fixture
def configured(bound, monkeypatch):
    settings = Settings(plan_max_turns=4)
    monkeypatch.setattr(Settings, "from_environment", lambda **_: settings)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-fixture")
    monkeypatch.setattr(jit_generate, "query", lambda **_: pytest.fail("global planner invoked Claude"))
    requests = []
    adapter_class = deepseek.DeepSeekAdapter

    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        names = [row["function"]["name"] for row in payload["tools"]]
        name = "run_gate" if "run_gate" in names else "publish_ownership_mapping"
        args = {} if name == "run_gate" else bound[2]
        return httpx.Response(200, json={
            "id": "offline-planning", "model": deepseek.VISION_MODEL,
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            "choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [{
                "type": "function", "function": {"name": name, "arguments": json.dumps(args)},
            }]}}],
        })

    monkeypatch.setattr(deepseek, "DeepSeekAdapter", lambda **kw: adapter_class(
        **kw, transport=httpx.MockTransport(respond),
    ))
    return bound, requests


def owner(layout):
    return run_owner_boundary.owned_root_run(layout, command="plan", owner_kind="direct")


def test_production_draft_verify_repair_preserve_exact_phase_inputs(configured):
    bound, requests = configured
    layout, workspace, _ = bound
    with owner(layout):
        path = asyncio.run(global_planner.generate_plan(layout.shot, workspace=workspace))
        snapshot = global_planner.snapshot_candidate(layout)
        original = snapshot.read_bytes()
        for role in ("verify", "repair"):
            result = asyncio.run(global_planner.generate_plan(
                layout.shot, workspace=workspace, role=role, phase_input=snapshot,
                repair_feedback="Preserve settled ownership." if role == "repair" else None,
            ))
            assert result == path and snapshot.read_bytes() == original
    assert len(requests) == 6
    assert not (workspace / "plans/global.draft.md").exists()
    assert snapshot.parent == layout.reports
    for request in requests:
        text = json.dumps(request)
        assert "clause-registry" in text and "authored-brief" in text and "Frames are 1-based" in text
        assert "claude" not in request["model"]
    assert "baseline-mapping" in json.dumps(requests[2])
    assert "repair-findings" in json.dumps(requests[4])
    assert authority_selection.resolve_selected_authority(layout.shot).plan is None


def test_driver_stage_publishes_only_after_independent_gate(configured):
    bound, requests = configured
    layout = bound[0]
    with owner(layout):
        assert global_plan_stage.run(layout) == 0
    selected = authority_selection.resolve_selected_authority(layout.shot)
    assert selected.plan is not None
    assert len(requests) == 4
    assert json.loads((layout.reports / "plan_gate.json").read_text())["outcome"].startswith("clean")


def test_dirty_driver_stage_preserves_typed_stop_without_selecting_authority(configured):
    bound, requests = configured
    layout = bound[0]
    bound[2]["blockers"] = ["Client must resolve the contradictory delivery constraint."]
    with owner(layout):
        assert global_plan_stage.run(layout) == 3
        envelope = layout.read_prepared_stop()
        assert envelope.identity.run_id == layout.run_id
        assert json.loads(layout.status.read_text())["state"] == "running"
    assert requests
    assert authority_selection.resolve_selected_authority(layout.shot).plan is None


def test_public_plan_command_uses_flynn_and_terminalizes(configured, monkeypatch):
    bound, requests = configured
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    monkeypatch.setattr(sys, "argv", ["vfx plan", str(bound[0].shot), "--until-clean"])
    planner.main()
    layout = run_artifacts.latest(bound[0].shot)
    assert json.loads(layout.status.read_text())["state"] == "passed"
    assert authority_selection.resolve_selected_authority(layout.shot).plan is not None
    assert len(requests) == 4


def test_inherited_environment_is_not_a_live_owner(configured):
    bound, requests = configured
    with pytest.raises(RunOwnerFenceError, match="live root-owner process"):
        asyncio.run(global_planner.generate_plan(bound[0].shot, workspace=bound[1]))
    assert not requests


def test_owner_scope_does_not_leak_and_refuses_other_run(configured):
    bound, _ = configured
    layout = bound[0]
    with owner(layout) as lease:
        assert run_owner_boundary.require_current_owner(layout) is lease
        with pytest.raises(RunOwnerFenceError, match="does not own"):
            run_owner_boundary.require_current_owner(replace(layout, run_id="other"))
    with pytest.raises(RunOwnerFenceError, match="live root-owner process"):
        run_owner_boundary.require_current_owner(layout)


def test_fork_cannot_use_inherited_owner_capability(configured):
    bound, _ = configured
    layout = bound[0]
    with owner(layout):
        child = os.fork()
        if child == 0:
            try:
                run_owner_boundary.require_current_owner(layout)
            except RunOwnerFenceError:
                os._exit(0)
            os._exit(1)
        _, status = os.waitpid(child, 0)
        assert os.waitstatus_to_exitcode(status) == 0
        run_owner_boundary.require_current_owner(layout)


def test_released_owner_refuses_even_inside_its_original_scope(configured):
    bound, _ = configured
    with owner(bound[0]) as lease:
        lease.release()
        with pytest.raises(RunOwnerFenceError, match="released"):
            run_owner_boundary.require_current_owner(bound[0])


def test_unknown_role_refuses_before_spend(configured):
    bound, requests = configured
    with owner(bound[0]), pytest.raises(ValueError, match="role must"):
        asyncio.run(global_planner.generate_plan(bound[0].shot, workspace=bound[1], role="invented"))
    assert not requests


@pytest.mark.parametrize("damage", ["snapshot", "draft"])
def test_substituted_phase_evidence_refuses_before_model(configured, damage):
    bound, requests = configured
    layout, workspace, _ = bound
    with owner(layout):
        asyncio.run(global_planner.generate_plan(layout.shot, workspace=workspace))
        snapshot = global_planner.snapshot_candidate(layout)
        if damage == "snapshot":
            data = json.loads(snapshot.read_text())
            data["sources"]["ownership_mapping.json"] = "substituted"
            snapshot.write_text(json.dumps(data))
        else:
            (workspace / "plans/global.md").write_text("different writer")
        with pytest.raises(ValueError, match="snapshot"):
            asyncio.run(global_planner.generate_plan(
                layout.shot, workspace=workspace, role="verify", phase_input=snapshot,
            ))
    assert len(requests) == 2


@pytest.mark.parametrize("setting", ["key", "model", "price"])
def test_bad_configuration_refuses_before_spend(configured, monkeypatch, setting):
    bound, requests = configured
    if setting == "key":
        monkeypatch.delenv("DEEPSEEK_API_KEY")
    else:
        settings = Settings(global_planner_model="claude" if setting == "model" else deepseek.VISION_MODEL,
                            run_max_usd=1 if setting == "price" else None)
        monkeypatch.setattr(Settings, "from_environment", lambda **_: settings)
    with owner(bound[0]), pytest.raises(ValueError):
        asyncio.run(global_planner.generate_plan(bound[0].shot, workspace=bound[1]))
    assert not requests


def test_global_model_configuration_is_independent_of_jit(monkeypatch):
    monkeypatch.setenv("VFXH_PLANNER_MODEL", "jit-model")
    monkeypatch.setenv("VFXH_GLOBAL_PLANNER_MODEL", deepseek.VISION_MODEL)
    settings = Settings.from_environment(load_dotenv_file=False)
    assert settings.planner_model == "jit-model"
    assert settings.global_planner_model == deepseek.VISION_MODEL


def test_production_global_module_imports_without_claude():
    result = subprocess.run([sys.executable, "-c", "import sys; sys.modules['claude_agent_sdk']=None; "
        "from vfx_harness.agents.global_planner import generate_plan; assert callable(generate_plan)"],
        capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
