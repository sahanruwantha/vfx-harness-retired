"""Global drafts remain VFX-owned observations across native Flynn dispatch."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from dataclasses import replace

import flynn_agents_sdk as flynn
import pytest

from tests.contract.test_flynn_plan_publication import ObservationOnly
from tests.unit.test_plan_authoring import MOTION_BRIEF, PRODUCT_BRIEF, _motion_mapping, _product_mapping, _shot
from vfx_harness.agents import flynn_global_tools
from vfx_harness.evaluation import plan_gate
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration import authority_selection, plan_authoring, plan_inputs
from vfx_harness.orchestration.plan_bundle_integrity import PlanPublicationError, digest


@pytest.fixture(params=["product", "motion"])
def bound(tmp_path, monkeypatch, request):
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    product = request.param == "product"
    root = _shot(tmp_path, PRODUCT_BRIEF if product else MOTION_BRIEF,
                 ["f001.png"] if product else ["f024.png", "f048.png"])
    layout = run_artifacts.create(root, "native-mapping")
    workspace = plan_inputs.prepare_staging(layout)
    mapping = (_product_mapping if product else _motion_mapping)(plan_authoring.clause_registry(root / "brief.md"))
    return layout, workspace, mapping


def execute(bound, *, after_inference=lambda: None, check=lambda: None, count=1):
    layout, _, mapping = bound

    class Adapter:
        async def generate(self, request):
            after_inference()
            return flynn.InferenceResult.scripted(flynn.ToolCall("publish_ownership_mapping", json.dumps(mapping)))

    async def run():
        tools, guard = flynn_global_tools.global_planning_tools(layout=layout, check_current=check)
        tool = tools[0]
        with flynn.SQLiteRun.create(layout.checkpoints / "mapping.sqlite", run_id="mapping",
                                   initial_state="unaccepted", limits=flynn.RunLimits(count, count, count)) as journal:
            session = flynn.Session(
                inference=Adapter(), tools=flynn.ToolBroker([tool]), evaluator=ObservationOnly(),
                run=journal, grants=(tool.name,), guards=(guard,),
                policy=lambda view: (flynn.SessionStop("draft only") if view.completed_steps == count
                                     else flynn.SessionStep("Draft ownership", (tool.name,))),
            )
            await session.execute()
            return flynn.ToolResult.from_json(journal.latest_observation())
    return asyncio.run(run())


def test_draft_expansion_passes_independent_gate_without_selecting_authority(bound):
    layout, workspace, mapping = bound
    result = execute(bound, count=2)
    observation = json.loads(result.data_json)
    assert observation["gate_attested"] is False
    assert set(observation["artifacts"]) == {"ownership_mapping.json", *plan_authoring.MAPPING_ARTIFACTS}
    for name, expected in observation["artifacts"].items():
        assert digest((workspace / name).read_bytes()) == expected
    assert json.loads((workspace / "ownership_mapping.json").read_text()) == mapping
    gate = plan_gate.run(workspace, "plans/global.md", require_scene_checks=True)
    assert gate.clean, [finding.what for finding in gate.blocking]
    assert authority_selection.resolve_selected_authority(layout.shot).plan is None
    assert not (layout.shot / "plans").exists()
    with flynn.SQLiteRun.open(layout.checkpoints / "mapping.sqlite") as journal:
        assert journal.read().value == "unaccepted"
        assert journal.records()["commits"] == []
        assert journal.remaining() == {"inference": 0, "tool": 0, "external": 0}


@pytest.mark.parametrize("damage", ["path", "layer_field", "bool_frame", "missing_owner", "oversize"])
def test_invalid_mapping_refuses_without_writing_or_external_spend(bound, damage):
    layout, workspace, mapping = bound
    if damage == "path":
        mapping["path"] = "../shot.json"
    elif damage == "layer_field":
        mapping["layers"][0]["extra"] = "not an owned field"
    elif damage == "bool_frame":
        mapping["layers"][0]["judge"][0]["frame"] = True
    elif damage == "missing_owner":
        mapping["resolutions"].pop(next(iter(mapping["resolutions"])))
    else:
        mapping["blockers"] = ["x" * 64_001]
    with pytest.raises(ValueError, match="mapping"):
        execute(bound)
    assert not (workspace / "ownership_mapping.json").exists()
    with flynn.SQLiteRun.open(layout.checkpoints / "mapping.sqlite") as journal:
        assert journal.remaining()["tool"] == journal.remaining()["external"] == 1


@pytest.mark.parametrize("damage", ["live_brief", "workspace_brief", "marker", "decision", "new_writer"])
def test_changed_inputs_or_outputs_during_inference_refuse(bound, damage):
    layout, workspace, _ = bound

    def change():
        paths = {
            "live_brief": layout.shot / "brief.md", "workspace_brief": workspace / "brief.md",
            "marker": workspace / ".plan-workspace.json", "decision": layout.shot / "plan_amendments.jsonl",
            "new_writer": workspace / "ownership_mapping.json",
        }
        paths[damage].write_text("new writer's bytes")

    with pytest.raises((ValueError, PlanPublicationError), match="changed"):
        execute(bound, after_inference=change)
    if damage == "new_writer":
        assert (workspace / "ownership_mapping.json").read_text() == "new writer's bytes"
    with flynn.SQLiteRun.open(layout.checkpoints / "mapping.sqlite") as journal:
        assert journal.remaining()["tool"] == journal.remaining()["external"] == 1


def test_expired_owner_refuses_before_dispatch(bound):
    current = True

    def expire():
        nonlocal current
        current = False

    def check():
        if not current:
            raise ValueError("run owner expired")

    with pytest.raises(ValueError, match="owner expired"):
        execute(bound, after_inference=expire, check=check)


def test_changed_selection_refuses_before_dispatch(bound, monkeypatch):
    layout, _, _ = bound
    prior = authority_selection.resolve_selected_authority(layout.shot)
    successor = replace(prior, selection_token=replace(
        prior.selection_token, plan_revision=1, plan_pointer_sha256="a" * 64,
    ))

    def change():
        monkeypatch.setattr(authority_selection, "resolve_selected_authority", lambda _: successor)

    with pytest.raises(ValueError, match="selection"):
        execute(bound, after_inference=change)


def test_partial_expansion_stays_pending_without_retry(bound, monkeypatch):
    layout, workspace, _ = bound
    calls = []

    def fail(root, mapping):
        calls.append(root)
        (root / "requirements.json").write_text("partial external effect")
        raise OSError("injected expansion failure")

    monkeypatch.setattr(plan_authoring, "expand_mapping", fail)
    with pytest.raises(OSError, match="injected"):
        execute(bound)
    assert calls == [workspace]
    assert (workspace / "requirements.json").read_text() == "partial external effect"
    with flynn.SQLiteRun.open(layout.checkpoints / "mapping.sqlite") as journal:
        assert journal.pending().stage == "dispatched"
        assert journal.remaining()["external"] == 0
        assert journal.records()["commits"] == []


@pytest.mark.parametrize("name", ["ownership_mapping.json", "plans", "requirements.json"])
def test_symlink_output_refuses_without_touching_target(bound, name):
    layout, workspace, _ = bound
    outside = layout.shot / "do-not-touch"
    outside.write_text("preserve")
    (workspace / name).symlink_to(outside)
    with pytest.raises(PlanPublicationError):
        execute(bound)
    assert outside.read_text() == "preserve"


def test_hardlinked_output_cannot_mutate_external_bytes(bound):
    layout, workspace, _ = bound
    outside = layout.shot / "do-not-touch"
    outside.write_text("preserve")
    os.link(outside, workspace / "requirements.json")
    with pytest.raises(ValueError, match="hard-linked"):
        execute(bound)
    assert outside.read_text() == "preserve"


def test_workspace_for_another_run_refuses_before_inference(bound):
    layout, workspace, _ = bound
    marker = workspace / ".plan-workspace.json"
    record = json.loads(marker.read_text())
    record["run_id"] = "different-run"
    marker.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="exact current shot and run"):
        execute(bound)
    assert not (layout.checkpoints / "mapping.sqlite").exists()


def test_changed_inputs_after_expansion_leave_unresolved_effect(bound, monkeypatch):
    layout, _, _ = bound
    expand = plan_authoring.expand_mapping

    def change_after_write(root, mapping):
        result = expand(root, mapping)
        (layout.shot / "brief.md").write_text("changed during external execution")
        return result

    monkeypatch.setattr(plan_authoring, "expand_mapping", change_after_write)
    with pytest.raises(PlanPublicationError, match="authored inputs changed"):
        execute(bound)
    with flynn.SQLiteRun.open(layout.checkpoints / "mapping.sqlite") as journal:
        assert journal.pending().stage == "dispatched"
        assert journal.records()["commits"] == []


def test_expander_manifest_cannot_self_certify_missing_outputs(bound, monkeypatch):
    layout, _, _ = bound
    monkeypatch.setattr(plan_authoring, "expand_mapping", lambda root, _: {
        name: root / name for name in plan_authoring.MAPPING_ARTIFACTS
    })
    with pytest.raises(ValueError, match="missing artifacts"):
        execute(bound)
    with flynn.SQLiteRun.open(layout.checkpoints / "mapping.sqlite") as journal:
        assert journal.pending().stage == "dispatched"


def test_native_mapping_module_imports_without_claude():
    result = subprocess.run([sys.executable, "-c", """
import sys
sys.modules['claude_agent_sdk'] = None
from vfx_harness.agents.flynn_global_tools import global_planning_tools
assert callable(global_planning_tools)
"""], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
