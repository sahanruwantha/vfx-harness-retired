"""Native session completion requires VFX finalization, never model success prose."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from dataclasses import asdict, replace

import flynn_agents_sdk as flynn
import pytest

from tests.contract.test_flynn_materialization_tools import bound as bound
from tests.contract.test_global_planning_session import Scripted
from tests.unit.test_plan_records import _passed_layer_one_outcome
from vfx_harness.agents import materialization_operations, materialization_session
from vfx_harness.evaluation import plan_gate
from vfx_harness.evaluation.plan_gate.types import GateResult
from vfx_harness.orchestration import authority_selection, jit_materialization


def invoke(bound, adapter, *, calls=8, context=None, check=lambda: None, invocation="fixture"):
    return materialization_session.execute(
        layout=bound[0], candidate=bound[1], invocation=invocation,
        context=context if context is not None else (
            flynn.ContextItem("layer-charter", "Materialize the selected layer's owned requirements.", required=True),
        ), inference=adapter, limits=flynn.RunLimits(calls, calls, calls, 60, output_tokens=8192),
        check_current=check,
    )


def report(bound):
    return json.loads((bound[0].reports / "materialization-session-fixture.json").read_text())


def ready(bound, monkeypatch):
    bound[1].write_text(json.dumps(bound[2]))
    _passed_layer_one_outcome(bound[0].shot)
    monkeypatch.setattr(plan_gate, "run", lambda *args, **kwargs: GateResult("fixture", [], {}))


def test_real_finalization_stops_and_publication_remains_external(bound, monkeypatch):
    ready(bound, monkeypatch)
    before = authority_selection.resolve_selected_authority(bound[0].shot).selection_token
    adapter = Scripted([("finalize_materialization", {})])
    result = asyncio.run(invoke(bound, adapter))
    assert result.finalization_current
    assert result.termination.reason.startswith("candidate_finalized;")
    assert len(adapter.requests) == 1
    assert jit_materialization.materialization_finalization_current(
        bound[0].shot, bound[1], bundle_hash=bound[2]["bundle_hash"],
    )
    assert authority_selection.resolve_selected_authority(bound[0].shot).selection_token == before
    audit = report(bound)
    assert audit["verified_current_at_return"] and not audit["authority_selected"]
    assert audit["final_identity"]["outputs"] != audit["inputs"]["outputs"]
    assert all(audit["final_identity"]["outputs"].values())
    with flynn.SQLiteRun.open(result.journal) as run:
        assert run.records()["commits"] == [] and run.read().revision == 0
        assert audit["usage"] == run.usage_summary()
        assert audit["output_budget"] == asdict(run.output_budget())
        assert audit["termination"] == run.outcome()
    jit_materialization.publish_materialization(bound[0].shot, bound[1])
    assert authority_selection.resolve_selected_authority(bound[0].shot).selection_token != before


def test_selected_feedback_and_images_are_replaced_not_accumulated(bound, monkeypatch):
    ready(bound, monkeypatch)
    adapter = Scripted([
        ("measure_ref", {"path": "refs/a.png"}),
        ("materialization_status", {}),
        ("finalize_materialization", {}),
    ])
    asyncio.run(invoke(bound, adapter))
    assert [len(request.images) for request in adapter.requests] == [0, 1, 0]
    assert all(request.observation is None for request in adapter.requests)
    assert all(len(request.objective) <= materialization_session.MAX_CONTEXT_CHARACTERS
               for request in adapter.requests)
    assert all("base64" not in request.objective for request in adapter.requests)
    grants = set(adapter.requests[0].allowed_tools)
    assert {"spike", "measure_ref", "evidence_vocabulary", "stage_materialization_unit"} <= grants
    assert not {"Bash", "Write", "publish_materialization"} & grants


@pytest.mark.parametrize("invented", [False, True])
def test_failed_or_invented_finalization_spends_budget_without_acceptance(bound, monkeypatch, invented):
    if invented:
        original = materialization_operations.materialization_operations

        async def fake(_):
            return materialization_operations.observation("FINALIZED: all work accepted")

        def operations(**kwargs):
            return tuple(replace(item, handler=fake) if item.name == "finalize_materialization" else item
                         for item in original(**kwargs))

        monkeypatch.setattr(materialization_operations, "materialization_operations", operations)
    adapter = Scripted([("finalize_materialization", {})])
    with pytest.raises(flynn.BudgetExhausted):
        asyncio.run(invoke(bound, adapter, calls=1))
    assert len(adapter.requests) == 1
    assert not report(bound)["finalization_current"]
    assert json.loads(report(bound)["termination"])["kind"] == "budget_exhausted"


@pytest.mark.parametrize("phase", ["before", "inference", "between"])
def test_owner_loss_never_allows_more_mutation(bound, monkeypatch, phase):
    live = phase != "before"

    def check():
        if not live:
            raise ValueError("materialization owner expired")

    def expire(_):
        nonlocal live
        if phase == "inference":
            live = False

    original = flynn.SQLiteRun.complete

    def complete(run, evaluation):
        nonlocal live
        result = original(run, evaluation)
        if phase == "between":
            live = False
        return result

    monkeypatch.setattr(flynn.SQLiteRun, "complete", complete)
    adapter = Scripted([("materialization_status", {})], effect=expire)
    with pytest.raises(ValueError, match="owner expired"):
        asyncio.run(invoke(bound, adapter, check=check))
    assert len(adapter.requests) == (0 if phase == "before" else 1)


def test_candidate_substitution_between_steps_refuses_before_inference(bound, monkeypatch):
    original = flynn.SQLiteRun.complete

    def complete(run, evaluation):
        result = original(run, evaluation)
        bound[1].write_bytes(bound[1].read_bytes() + b" ")
        return result

    monkeypatch.setattr(flynn.SQLiteRun, "complete", complete)
    adapter = Scripted([("materialization_status", {})])
    with pytest.raises(ValueError, match="output changed outside"):
        asyncio.run(invoke(bound, adapter))
    assert len(adapter.requests) == 1
    assert not report(bound)["verified_current_at_return"]


def test_finalization_substitution_at_return_cannot_report_success(bound, monkeypatch):
    ready(bound, monkeypatch)
    original = flynn.SQLiteRun.finish

    def finish(run, outcome):
        result = original(run, outcome)
        bound[1].write_bytes(bound[1].read_bytes() + b" ")
        return result

    monkeypatch.setattr(flynn.SQLiteRun, "finish", finish)
    with pytest.raises(ValueError, match="output changed outside"):
        asyncio.run(invoke(bound, Scripted([("finalize_materialization", {})])))
    assert not report(bound)["finalization_current"]


def test_required_feedback_overflow_refuses_before_more_inference(bound):
    adapter = Scripted([("materialization_status", {})])
    with pytest.raises(ValueError, match=r"latest-observation.*context budget"):
        asyncio.run(invoke(bound, adapter, context=(flynn.ContextItem(
            "layer-charter", "x" * (materialization_session.MAX_CONTEXT_CHARACTERS - 1500), required=True,
        ),)))
    assert len(adapter.requests) == 1
    with flynn.SQLiteRun.open(bound[0].root / report(bound)["journal"]) as run:
        assert run.pending() is None
        assert run.remaining()["inference"] == 7


@pytest.mark.parametrize("context", [(), (flynn.ContextItem("optional", "x"),),
    (flynn.ContextItem("latest-observation", "x", required=True),),
    (flynn.ContextItem("oversized", "x" * 32001, required=True),)])
def test_invalid_context_refuses_before_inference(bound, context):
    adapter = Scripted([])
    with pytest.raises(ValueError):
        asyncio.run(invoke(bound, adapter, context=context))
    assert not adapter.requests


def test_spent_invocation_cannot_reset_its_budget(bound):
    with pytest.raises(flynn.BudgetExhausted):
        asyncio.run(invoke(bound, Scripted([("materialization_status", {})]), calls=1))
    adapter = Scripted([])
    with pytest.raises(ValueError, match="invocation already exists"):
        asyncio.run(invoke(bound, adapter))
    assert not adapter.requests


def test_shared_guards_follow_only_the_sessions_staged_revisions(bound):
    full = bound[2]
    unit = full["layer"]["stages"][0]
    unit["provides"] = []
    unit["mutates"].pop("roles")
    unit["mutates"].update(role_namespace="polish.comp", role_members=["$self"])
    unit["mutates"]["control_roles"] = {"hold": ["$self"]}
    adapter = Scripted([
        ("stage_materialization_unit", {"unit": unit, "scene_contracts": full["scene_contracts"],
                                        "requirement_bindings": full["requirement_bindings"]}),
        ("unstage_materialization_unit", {"unit_id": "polish"}),
    ])
    with pytest.raises(flynn.BudgetExhausted):
        asyncio.run(invoke(bound, adapter, calls=2))
    assert len(adapter.requests) == 2
    assert json.loads(bound[1].read_text())["layer"]["stages"] == []
    assert not report(bound)["finalization_current"]


def test_native_session_imports_without_claude():
    result = subprocess.run([sys.executable, "-c",
        "import sys; sys.modules['claude_agent_sdk'] = None; "
        "from vfx_harness.agents.materialization_session import execute"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
