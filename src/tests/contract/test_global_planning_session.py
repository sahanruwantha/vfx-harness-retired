"""The native VFX sweep owns bounded feedback and independent gate stopping."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from dataclasses import asdict

import flynn_agents_sdk as flynn
import httpx
import pytest
from flynn_agents_sdk import deepseek

from tests.contract.test_flynn_mapping_publication import bound as bound
from tests.contract.test_flynn_mapping_publication import execute as seed
from tests.contract.test_flynn_planning_inputs import reference_bound as reference_bound
from vfx_harness.agents import global_planning_session as planning
from vfx_harness.evaluation import plan_gate
from vfx_harness.evaluation.plan_gate.types import Finding, GateResult
from vfx_harness.orchestration import authority_selection, plan_authoring
from vfx_harness.orchestration.plan_bundle_integrity import PlanPublicationError


class Scripted:
    def __init__(self, steps, effect=lambda _: None):
        self.steps = iter(steps)
        self.requests = []
        self.effect = effect

    def plan_output(self, request, available):
        return flynn.OutputReservation("scripted", 0)

    async def generate(self, request):
        self.requests.append(request)
        self.effect(len(self.requests))
        name, args = next(self.steps)
        return flynn.InferenceResult.scripted(flynn.ToolCall(name, json.dumps(args)))


def invoke(bound, adapter, *, role="draft", context=None, check=lambda: None, calls=12, invocation="fixture"):
    return planning.execute(
        layout=bound[0], invocation=invocation, role=role,
        context=context if context is not None else (flynn.ContextItem(
            "authored-brief", (bound[1] / "brief.md").read_text(), required=True,
        ),), inference=adapter, limits=flynn.RunLimits(calls, calls, calls, 60, output_tokens=8192),
        check_current=check,
    )


def audit(bound):
    return json.loads((bound[0].reports / "global-planning-fixture.json").read_text())


def test_real_clean_gate_stops_without_more_inference_or_acceptance(bound):
    adapter = Scripted([("publish_ownership_mapping", bound[2]), ("run_gate", {})])
    result = asyncio.run(invoke(bound, adapter))
    assert result.termination.kind == "stopped" and result.termination.reason.startswith("gate_clean;")
    assert len(adapter.requests) == 2
    assert "run_gate" not in adapter.requests[0].allowed_tools
    assert "run_gate" in adapter.requests[1].allowed_tools
    assert json.loads(result.gate_feedback_json)["clean"] is True
    assert plan_gate.run(bound[1], require_scene_checks=True).clean
    assert authority_selection.resolve_selected_authority(bound[0].shot).plan is None
    report = audit(bound)
    assert report["verified_current_at_return"] is True and report["plan_authority_changed"] is False
    with flynn.SQLiteRun.open(result.journal) as run:
        assert run.read().revision == 0 and run.records()["commits"] == []
        assert report["usage"] == run.usage_summary()
        assert report["output_budget"] == asdict(run.output_budget())
        assert report["termination"] == run.outcome()


@pytest.mark.parametrize("role", ["verify", "repair"])
def test_existing_draft_must_be_audited_before_gate(bound, role):
    seed(bound)
    adapter = Scripted([("publish_ownership_mapping", bound[2]), ("run_gate", {})])
    result = asyncio.run(invoke(bound, adapter, role=role))
    assert result.termination.kind == "stopped"
    assert "run_gate" not in adapter.requests[0].allowed_tools


@pytest.mark.parametrize("role", ["verify", "repair"])
def test_missing_draft_refuses_before_inference(bound, role):
    adapter = Scripted([])
    with pytest.raises(ValueError, match="complete existing draft"):
        asyncio.run(invoke(bound, adapter, role=role))
    assert not adapter.requests


def test_model_cannot_gate_before_publication(bound):
    adapter = Scripted([("run_gate", {})])
    with pytest.raises(flynn.ContractError):
        asyncio.run(invoke(bound, adapter))
    assert json.loads(audit(bound)["termination"])["kind"] == "failed"
    assert not (bound[1] / "ownership_mapping.json").exists()


def test_only_latest_text_observation_is_selected(bound):
    adapter = Scripted([
        ("publish_ownership_mapping", bound[2]),
        ("read_plan_input", {"name": "ownership_mapping.json", "offset": 0}),
        ("read_plan_input", {"name": "layers.json", "offset": 0}),
        ("run_gate", {}),
    ])
    asyncio.run(invoke(bound, adapter))
    assert all(request.observation is None for request in adapter.requests)
    assert all(len(request.objective) <= planning.MAX_CONTEXT_CHARACTERS for request in adapter.requests)
    assert '"name": "ownership_mapping.json"' in adapter.requests[2].objective
    assert '"name": "ownership_mapping.json"' not in adapter.requests[3].objective
    assert '"name": "layers.json"' in adapter.requests[3].objective


def test_image_is_selected_for_one_request_only(reference_bound):
    bound = reference_bound()
    adapter = Scripted([
        ("read_reference", {"name": "refs/f001.png"}),
        ("read_plan_input", {"name": "brief.md", "offset": 0}),
        ("publish_ownership_mapping", bound[2]), ("run_gate", {}),
    ])
    asyncio.run(invoke(bound, adapter))
    assert [len(request.images) for request in adapter.requests] == [0, 1, 0, 0]
    assert adapter.requests[1].images[0].url.startswith("data:image/png;base64,")
    assert all("base64" not in request.objective for request in adapter.requests)


@pytest.mark.parametrize("halt", ["plateau", "gate_call_cap", "feedback_overflow"])
def test_gate_halt_stops_inference_and_returns_dirty_evidence(bound, monkeypatch, halt):
    calls = 0

    def dirty(*args, **kwargs):
        nonlocal calls
        calls += 1
        message = "x" * 9000 if halt == "feedback_overflow" else f"finding {calls if halt == 'gate_call_cap' else 1}"
        return GateResult(shot="fixture", findings=[Finding("fixture", True, "draft", message)])

    monkeypatch.setattr(plan_gate, "run", dirty)
    expected = {"plateau": 2, "gate_call_cap": 4, "feedback_overflow": 1}[halt]
    adapter = Scripted([("publish_ownership_mapping", bound[2]), *[("run_gate", {})] * expected])
    result = asyncio.run(invoke(bound, adapter))
    assert result.termination.reason.startswith(f"gate_{halt};")
    assert json.loads(result.gate_feedback_json)["clean"] is False
    assert len(adapter.requests) == expected + 1


def test_context_overflow_refuses_before_inference(bound):
    adapter = Scripted([])
    with pytest.raises(ValueError, match="context budget"):
        asyncio.run(invoke(bound, adapter, context=(flynn.ContextItem(
            "oversized", "x" * planning.MAX_CONTEXT_CHARACTERS, required=True,
        ),)))
    assert not adapter.requests


def test_required_feedback_overflow_stops_before_another_inference(bound):
    adapter = Scripted([("publish_ownership_mapping", bound[2])])
    with pytest.raises(ValueError, match=r"latest-observation.*context budget"):
        asyncio.run(invoke(bound, adapter, context=(flynn.ContextItem(
            "large-authority", "x" * (planning.MAX_CONTEXT_CHARACTERS - 1600), required=True,
        ),)))
    assert len(adapter.requests) == 1
    with flynn.SQLiteRun.open(bound[0].root / audit(bound)["journal"]) as run:
        assert run.pending() is None  # the preceding publication completed and is retained
        assert run.remaining()["inference"] == 11


def test_changed_inputs_between_steps_refuse_before_more_spend(bound, monkeypatch):
    original = flynn.SQLiteRun.complete

    def change(run, evaluation):
        result = original(run, evaluation)
        (bound[1] / "brief.md").write_text("substituted brief")
        return result

    monkeypatch.setattr(flynn.SQLiteRun, "complete", change)
    adapter = Scripted([("publish_ownership_mapping", bound[2])])
    with pytest.raises(PlanPublicationError, match="authored inputs changed"):
        asyncio.run(invoke(bound, adapter))
    assert len(adapter.requests) == 1
    assert audit(bound)["verified_current_at_return"] is False


def test_budget_exhaustion_is_recorded_without_retry(bound):
    adapter = Scripted([("publish_ownership_mapping", bound[2])])
    with pytest.raises(flynn.BudgetExhausted):
        asyncio.run(invoke(bound, adapter, calls=1))
    report = audit(bound)
    assert json.loads(report["termination"])["kind"] == "budget_exhausted"
    assert report["verified_current_at_return"] is False
    assert len(adapter.requests) == 1


@pytest.mark.parametrize("phase", ["before", "inference", "next_inference"])
def test_expired_owner_refuses_at_each_boundary(bound, phase):
    current = phase != "before"

    def check():
        if not current:
            raise ValueError("run owner expired")

    def expire(count):
        nonlocal current
        if count == (2 if phase == "next_inference" else 1):
            current = False

    adapter = Scripted([("publish_ownership_mapping", bound[2]), ("run_gate", {})], effect=expire)
    with pytest.raises(ValueError, match="owner expired"):
        asyncio.run(invoke(bound, adapter, check=check))
    assert len(adapter.requests) == {"before": 0, "inference": 1, "next_inference": 2}[phase]


def test_partial_publication_remains_pending_and_invocation_cannot_restart(bound, monkeypatch):
    def partial(root, mapping):
        raise OSError("injected expansion failure")

    monkeypatch.setattr(plan_authoring, "expand_mapping", partial)
    adapter = Scripted([("publish_ownership_mapping", bound[2])])
    with pytest.raises(OSError, match="expansion"):
        asyncio.run(invoke(bound, adapter))
    report = audit(bound)
    with flynn.SQLiteRun.open(bound[0].root / report["journal"]) as run:
        assert run.pending().stage == "dispatched"
    with pytest.raises(ValueError, match="already exists"):
        asyncio.run(invoke(bound, adapter))
    assert len(adapter.requests) == 1


@pytest.mark.parametrize("failure", ["timeout", "cancel"])
def test_timeout_and_cancellation_are_durable(bound, failure):
    class Interrupted(Scripted):
        async def generate(self, request):
            raise TimeoutError() if failure == "timeout" else asyncio.CancelledError()

    with pytest.raises(TimeoutError if failure == "timeout" else asyncio.CancelledError):
        asyncio.run(invoke(bound, Interrupted([])))
    assert json.loads(audit(bound)["termination"])["kind"] == ("timed_out" if failure == "timeout" else "cancelled")


def test_real_adapter_records_usage_and_prepared_tools(bound):
    requests = []
    steps = iter([("publish_ownership_mapping", bound[2]), ("run_gate", {})])

    def respond(request):
        requests.append(json.loads(request.content))
        name, args = next(steps)
        return httpx.Response(200, json={
            "id": "offline", "model": deepseek.VISION_MODEL,
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            "choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [{
                "type": "function", "function": {"name": name, "arguments": json.dumps(args)},
            }]}}],
        })

    async def run():
        async with deepseek.DeepSeekAdapter(
            api_key="offline", model=deepseek.VISION_MODEL, max_tokens=2048,
            transport=httpx.MockTransport(respond),
        ) as adapter:
            return await invoke(bound, adapter)

    asyncio.run(run())
    report = audit(bound)
    assert report["usage"]["known_output_tokens"] == 40
    assert report["pricing_status"] == "unpriced"
    assert requests[0]["max_tokens"] == 2048
    assert "run_gate" not in [row["function"]["name"] for row in requests[0]["tools"]]


def test_native_session_imports_without_claude():
    completed = subprocess.run([sys.executable, "-c", "import sys; sys.modules['claude_agent_sdk']=None; "
        "from vfx_harness.agents.global_planning_session import execute; assert callable(execute)"],
        capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr
