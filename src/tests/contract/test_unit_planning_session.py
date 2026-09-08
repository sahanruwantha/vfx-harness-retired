"""Unit planning journals evidence while the caller retains publication and rollback."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from contextlib import nullcontext
from dataclasses import asdict

import flynn_agents_sdk as flynn
import pytest

from tests.contract import test_flynn_materialization_tools
from tests.contract.test_global_planning_session import Scripted
from tests.unit.test_unit_plan_publication_stamp import _CONTENT
from vfx_harness.agents import unit_planning_session as planning
from vfx_harness.evaluation import plan_gate
from vfx_harness.evaluation.plan_gate.types import Finding, GateResult
from vfx_harness.orchestration import authority_selection, ledger
from vfx_harness.orchestration.layer_plans import (
    validate_work_unit_plan_authority,
    work_unit_plan_authority_path,
    work_unit_plan_path,
)
from vfx_harness.orchestration.work_unit_plan_transaction import (
    WorkUnitPlanTransactionConflict,
    work_unit_plan_transaction,
)

materialization_bound = test_flynn_materialization_tools.bound


@pytest.fixture
def bound(materialization_bound):
    layout = materialization_bound[0]
    root = layout.shot
    selected = authority_selection.resolve_selected_authority(root)
    layer = ledger.load_layers_from_path(selected.artifact_paths["layers.json"])["1"]
    unit = layer.stages[0]
    target = work_unit_plan_path(root, unit, selected_authority=selected)
    target.parent.mkdir(parents=True, exist_ok=True)
    return layout, selected, target, layer.id, unit.id


def invoke(bound, adapter, *, calls=8, context=None, check=lambda: None, rollback=False, **overrides):
    layout, selected, target, layer, unit = bound

    async def execute():
        async with work_unit_plan_transaction(target, work_unit_plan_authority_path(target)) as transaction:
            transaction.claim_current()
            kwargs = {
                "layout": layout, "target": target, "selected_authority": selected, "layer_id": layer, "unit_id": unit,
                "transaction": transaction, "invocation": "fixture", "inference": adapter,
                "context": context if context is not None else (
                    flynn.ContextItem("unit-authority", "Prepare this unit from its declared scope.", required=True),
                ), "limits": flynn.RunLimits(calls, calls, calls, 60, output_tokens=8192),
                "check_current": check, "hold_current": nullcontext,
            }
            kwargs.update(overrides)
            try:
                return await planning.execute(**kwargs)
            finally:
                if rollback:
                    transaction.rollback()
    return asyncio.run(execute())


def clean(monkeypatch):
    monkeypatch.setattr(plan_gate, "run", lambda *a, **kw: GateResult("fixture", [], {}))


def steps():
    return [("publish_unit_plan", {"content": _CONTENT}), ("gate_preview", {})]


def audit(bound):
    return json.loads((bound[0].reports / "unit-planning-session-fixture.json").read_text())


def test_publishes_and_previews_without_terminal_attestation_or_commit(bound, monkeypatch):
    clean(monkeypatch)
    adapter = Scripted(steps())
    result = invoke(bound, adapter)
    assert result.termination.kind == "stopped" and len(adapter.requests) == 2
    validate_work_unit_plan_authority(bound[0].shot, bound[2], require_gate=False, selected_authority=bound[1])
    with pytest.raises(ValueError, match="clean-gate attestation"):
        validate_work_unit_plan_authority(bound[0].shot, bound[2], require_gate=True, selected_authority=bound[1])
    report = audit(bound)
    assert report["preview_clean_at_return"] and not report["gate_attested"]
    with flynn.SQLiteRun.open(result.journal) as run:
        assert run.read().revision == 0 and run.records()["commits"] == []
        assert report["usage"] == run.usage_summary()
        assert report["output_budget"] == asdict(run.output_budget())
        assert report["termination"] == run.outcome()
    assert not {"Bash", "Write", "Edit", "escalate_vocabulary_gap"} & set(adapter.requests[0].allowed_tools)


def test_refusal_then_correction_keeps_only_latest_feedback(bound, monkeypatch):
    clean(monkeypatch)
    adapter = Scripted([("gate_preview", {}), *steps()])
    invoke(bound, adapter)
    assert "Publish this session's plan before" in adapter.requests[1].objective
    assert "Publish this session's plan before" not in adapter.requests[2].objective
    assert "terminal gate still required" in adapter.requests[2].objective


def test_reference_image_is_only_in_the_next_request(bound, monkeypatch):
    clean(monkeypatch)
    adapter = Scripted([("measure_ref", {"path": "refs/a.png"}), *steps()])
    invoke(bound, adapter)
    assert not adapter.requests[0].images and adapter.requests[1].images and not adapter.requests[2].images


def test_failed_preview_exhausts_budget_and_outer_transaction_rolls_back(bound, monkeypatch):
    finding = Finding.in_layer("STATE", True, "1", "unit plan", "injected blocking finding")
    monkeypatch.setattr(plan_gate, "run", lambda *a, **kw: GateResult("fixture", [finding], {}))
    before = bound[2].read_bytes() if bound[2].exists() else None
    with pytest.raises(flynn.BudgetExhausted):
        invoke(bound, Scripted(steps()), calls=2, rollback=True)
    assert (bound[2].read_bytes() if bound[2].exists() else None) == before
    assert not audit(bound)["preview_clean_at_return"]


def test_changed_target_during_inference_cannot_be_adopted_or_rolled_back(bound, monkeypatch):
    clean(monkeypatch)

    def replace_target(count):
        if count == 2:
            bound[2].write_text("newer writer")

    with pytest.raises(WorkUnitPlanTransactionConflict, match="newer writer"):
        invoke(bound, Scripted(steps(), replace_target), rollback=True)
    assert bound[2].read_text() == "newer writer"


def test_authored_change_during_gate_refuses_clean_result(bound, monkeypatch):
    def gate(*a, **kw):
        (bound[0].shot / "brief.md").write_text("changed authored input")
        return GateResult("fixture", [], {})
    monkeypatch.setattr(plan_gate, "run", gate)
    with pytest.raises(ValueError, match="authored inputs changed"):
        invoke(bound, Scripted(steps()), rollback=True)
    assert not audit(bound)["preview_clean_at_return"]


@pytest.mark.parametrize("context", [(), (flynn.ContextItem("optional", "x"),),
    (flynn.ContextItem("latest-observation", "x", required=True),),
    (flynn.ContextItem("huge", "x" * 33000, required=True),)])
def test_invalid_context_refuses_before_inference(bound, context):
    adapter = Scripted([])
    with pytest.raises(ValueError):
        invoke(bound, adapter, context=context)
    assert not adapter.requests


def test_duplicate_invocation_refuses_resume(bound, monkeypatch):
    clean(monkeypatch)
    invoke(bound, Scripted(steps()))
    adapter = Scripted([])
    with pytest.raises(ValueError, match="invocation already exists"):
        invoke(bound, adapter)
    assert not adapter.requests


def test_wrong_unit_target_refuses_before_inference(bound):
    adapter = Scripted([])
    with pytest.raises(ValueError, match="exact selected unit plan target"):
        invoke(bound, adapter, unit_id="not-selected")
    assert not adapter.requests


def test_native_unit_session_imports_without_claude():
    result = subprocess.run([sys.executable, "-c", "import sys; sys.modules['claude_agent_sdk'] = None; "
                             "import vfx_harness.agents.unit_planning_session"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_preview_cap_is_enforced_without_a_fourth_gate(bound, monkeypatch):
    calls = []

    def gate(*args, **kwargs):
        calls.append(args)
        return GateResult("fixture", [Finding.in_layer("STATE", True, "1", "unit", "blocked")], {})

    monkeypatch.setattr(plan_gate, "run", gate)
    adapter = Scripted([steps()[0], *[("gate_preview", {})] * 4])
    with pytest.raises(flynn.BudgetExhausted):
        invoke(bound, adapter, calls=5, rollback=True)
    assert len(calls) == audit(bound)["preview_calls"] == 3
    with flynn.SQLiteRun.open(bound[0].checkpoints / "flynn/unit-plan-fixture.sqlite") as run:
        result = flynn.ToolResult.from_json(run.latest_observation())
        assert result.status == "refused"


def test_owner_loss_during_inference_prevents_publication(bound):
    alive = True

    def expire(_):
        nonlocal alive
        alive = False

    def check():
        if not alive:
            raise ValueError("planning owner lost")

    with pytest.raises(ValueError, match="planning owner lost"):
        invoke(bound, Scripted(steps(), expire), check=check, rollback=True)
    assert not bound[2].exists()
    with flynn.SQLiteRun.open(bound[0].checkpoints / "flynn/unit-plan-fixture.sqlite") as run:
        assert run.remaining()["tool"] == run.remaining()["external"] == 8


def test_preview_can_repair_own_plan_before_clean_stop(bound, monkeypatch):
    results = iter([
        GateResult("fixture", [Finding.in_layer("STATE", True, "1", "unit", "repair execution ticket")], {}),
        GateResult("fixture", [], {}),
    ])
    monkeypatch.setattr(plan_gate, "run", lambda *a, **kw: next(results))
    repaired = _CONTENT + "\nRepair the owned ticket."
    adapter = Scripted([*steps(), ("publish_unit_plan", {"content": repaired}), ("gate_preview", {})])
    invoke(bound, adapter)
    assert "repair execution ticket" in adapter.requests[2].objective
    assert bound[2].read_text() == repaired.rstrip() + "\n"
    assert audit(bound)["preview_calls"] == 2


def test_budget_limits_are_required_before_inference(bound):
    adapter = Scripted([])
    with pytest.raises(ValueError, match="wall-time and output-token"):
        invoke(bound, adapter, limits=flynn.RunLimits(2, 2, 2))
    assert not adapter.requests
