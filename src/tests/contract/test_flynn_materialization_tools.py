"""Native materialization records effects without selecting VFX authority."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from dataclasses import replace

import flynn_agents_sdk as flynn
import pytest
from PIL import Image

from tests.contract.test_flynn_plan_publication import ObservationOnly
from tests.unit.test_plan_records import (
    _add_deferred_layer,
    _candidate,
    _jit_payload,
    _passed_layer_one_outcome,
    publish_current,
)
from vfx_harness.agents import flynn_materialization_tools, materialization_operations
from vfx_harness.evaluation import plan_gate
from vfx_harness.evaluation.plan_gate.types import Finding, GateResult
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration import authority_selection, jit_materialization
from vfx_harness.orchestration.plan_bundle_integrity import PlanPublicationError


@pytest.fixture
def bound(tmp_path, monkeypatch):
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    Image.new("RGB", (64, 64), "gray").save(tmp_path / "refs/a.png")
    layout = run_artifacts.create(tmp_path, "native-materialize")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    full = json.loads(_jit_payload(tmp_path, bundle.content_hash).read_text())
    candidate = layout.scratch / "candidate.json"
    jit_materialization.seed_materialization_candidate(
        bundle.root, candidate, layer_id="2", bundle_hash=bundle.content_hash,
        base_selection=authority_selection.resolve_selected_authority(tmp_path).selection_token,
    )
    return layout, candidate, full


def execute(bound, steps, *, after_inference=lambda _: None, check=lambda: None):
    layout, candidate, _ = bound
    results = []

    class Adapter:
        index = 0

        async def generate(self, request):
            name, arguments = steps[self.index]
            after_inference(self.index)
            self.index += 1
            return flynn.InferenceResult.scripted(flynn.ToolCall(name, json.dumps(arguments)))

    async def run():
        capabilities = flynn_materialization_tools.materialization_tools(
            layout=layout, candidate=candidate, check_current=check,
        )
        tools, guard = capabilities.tools, capabilities.guard
        grants = tuple(tool.name for tool in tools)
        with flynn.SQLiteRun.create(layout.checkpoints / "materialization.sqlite", run_id="materialize",
                                   initial_state="unaccepted", limits=flynn.RunLimits(10, 10, 10)) as journal:
            session = flynn.Session(
                inference=Adapter(), tools=flynn.ToolBroker(tools), evaluator=ObservationOnly(), run=journal,
                grants=grants, guards=(guard,),
                on_step=lambda step: results.append(flynn.ToolResult.from_json(step.candidate.output)),
                policy=lambda view: (flynn.SessionStop("fixture complete") if view.completed_steps == len(steps)
                                     else flynn.SessionStep("Materialize one layer", grants)),
            )
            await session.execute()
    asyncio.run(run())
    return results


def test_status_and_failed_finalization_are_honest_durable_observations(bound):
    layout, candidate, _ = bound
    before = candidate.read_bytes()
    selection = authority_selection.resolve_selected_authority(layout.shot).selection_token
    status, final = execute(bound, [("materialization_status", {}), ("finalize_materialization", {})])
    assert status.status == "ok"
    assert final.status == "refused"
    record = json.loads(final.data_json)
    assert record["finalization_current"] is False
    assert record["authority_selected"] is False
    report = json.loads((layout.root / record["report"]).read_text())
    assert "VALIDATION" in report["text"]
    assert candidate.read_bytes() == before
    assert authority_selection.resolve_selected_authority(layout.shot).selection_token == selection
    with flynn.SQLiteRun.open(layout.checkpoints / "materialization.sqlite") as journal:
        assert journal.records()["commits"] == []
        assert journal.read().value == "unaccepted"
        assert journal.remaining() == {"inference": 8, "tool": 8, "external": 8}


@pytest.mark.parametrize("damage", ["candidate", "attestation", "brief", "owner", "symlink"])
def test_stale_inputs_refuse_after_inference_before_external_reservation(bound, damage):
    layout, candidate, _ = bound
    live = True

    def check():
        if not live:
            raise ValueError("owner released")

    def interfere(_):
        nonlocal live
        if damage == "candidate":
            candidate.write_bytes(candidate.read_bytes() + b" ")
        elif damage == "attestation":
            jit_materialization.materialization_finalization_path(candidate).write_text("{}")
        elif damage == "brief":
            with (layout.shot / "brief.md").open("a") as stream:
                stream.write("Changed intent")
        elif damage == "owner":
            live = False
        else:
            other = candidate.with_name("other.json")
            candidate.rename(other)
            candidate.symlink_to(other)

    with pytest.raises((ValueError, OSError, PlanPublicationError)):
        execute(bound, [("materialization_status", {})], after_inference=interfere, check=check)
    with flynn.SQLiteRun.open(layout.checkpoints / "materialization.sqlite") as journal:
        assert journal.remaining()["inference"] == 9
        assert journal.remaining()["tool"] == journal.remaining()["external"] == 10
        assert journal.records()["commits"] == []


@pytest.mark.parametrize("name,args", [
    ("materialization_status", {"path": "../shot.json"}),
    ("mint_refobs", {"source": "../brief.md", "box": [0.1, 0.1, 0.5, 0.5]}),
    ("stage_materialization_unit", {"unit": {}, "scene_contracts": [], "requirement_bindings": []}),
    ("unstage_materialization_unit", {"unit_id": "polish", "path": "other.json"}),
    ("patch_materialization", {"pointer": "/layer/title", "value": "true", "patches": []}),
])
def test_closed_schemas_refuse_before_tool_spend(bound, name, args):
    with pytest.raises(flynn.ProposalRejected):
        execute(bound, [(name, args)])
    with flynn.SQLiteRun.open(bound[0].checkpoints / "materialization.sqlite") as journal:
        assert journal.remaining()["tool"] == journal.remaining()["external"] == 10


def test_output_is_bounded_but_complete_report_survives(bound, monkeypatch):
    original = materialization_operations.materialization_operations

    async def large(_):
        return materialization_operations.observation("finding " * 2000, is_error=True)

    def operations(**kwargs):
        return tuple(replace(item, handler=large) if item.name == "finalize_materialization" else item
                     for item in original(**kwargs))

    monkeypatch.setattr(materialization_operations, "materialization_operations", operations)
    result = execute(bound, [("finalize_materialization", {})])[0]
    record = json.loads(result.data_json)
    assert len(result.content[0].text) == flynn_materialization_tools.FEEDBACK_CHARACTERS
    assert record["omitted_characters"] == 8000
    assert len(json.loads((bound[0].root / record["report"]).read_text())["text"]) == 16000
    assert result.status == "refused" and not record["finalization_current"]


def test_native_module_imports_without_claude():
    result = subprocess.run([sys.executable, "-c",
        "import sys; sys.modules['claude_agent_sdk'] = None; "
        "from vfx_harness.agents.flynn_materialization_tools import materialization_tools"],
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_stage_patch_unstage_keep_one_revision_chain(bound):
    _layout, candidate, full = bound
    unit = full["layer"]["stages"][0]
    unit["provides"] = []
    unit["mutates"].pop("roles")
    unit["mutates"].update(role_namespace="polish.comp", role_members=["$self"])
    unit["mutates"]["control_roles"] = {"hold": ["$self"]}
    results = execute(bound, [
        ("stage_materialization_unit", {"unit": unit, "scene_contracts": full["scene_contracts"],
                                        "requirement_bindings": full["requirement_bindings"]}),
        ("patch_materialization", {"pointer": "/layer/stages/id=polish/title", "value": '"Revised polish"'}),
        ("unstage_materialization_unit", {"unit_id": "polish"}),
    ])
    for result in results:
        assert result.status == "ok", result
    rows = [json.loads(result.data_json) for result in results]
    assert rows[0]["after"] == rows[1]["before"]
    assert rows[1]["after"] == rows[2]["before"]
    assert rows[0]["before"] != rows[0]["after"]
    assert json.loads(candidate.read_text())["layer"]["stages"] == []
    assert not any(row["finalization_current"] or row["authority_selected"] for row in rows)


def test_handler_failure_after_write_remains_uncertain(bound, monkeypatch):
    layout, candidate, _ = bound
    original = materialization_operations.materialization_operations

    async def broken(_):
        candidate.write_bytes(candidate.read_bytes() + b" ")
        raise RuntimeError("injected death after candidate write")

    def operations(**kwargs):
        return tuple(replace(item, handler=broken) if item.name == "materialization_status" else item
                     for item in original(**kwargs))

    monkeypatch.setattr(materialization_operations, "materialization_operations", operations)
    with pytest.raises(RuntimeError, match="injected death"):
        execute(bound, [("materialization_status", {})])
    with flynn.SQLiteRun.open(layout.checkpoints / "materialization.sqlite") as journal:
        assert journal.remaining() == {"inference": 9, "tool": 9, "external": 9}
        assert journal.pending() is not None
        assert journal.records()["commits"] == []


def test_success_prose_cannot_invent_finalization(bound, monkeypatch):
    original = materialization_operations.materialization_operations

    async def invented(_):
        return materialization_operations.observation("FINALIZATION ATTESTED AND TERMINAL GATE CLEAN")

    def operations(**kwargs):
        return tuple(replace(item, handler=invented) if item.name == "finalize_materialization" else item
                     for item in original(**kwargs))

    monkeypatch.setattr(materialization_operations, "materialization_operations", operations)
    result = execute(bound, [("finalize_materialization", {})])[0]
    assert not json.loads(result.data_json)["finalization_current"]


@pytest.mark.parametrize("dirty", [False, True])
def test_finalization_uses_real_attestation_and_keeps_publication_external(bound, monkeypatch, dirty):
    layout, candidate, full = bound
    candidate.write_text(json.dumps(full))
    _passed_layer_one_outcome(layout.shot)
    original_selection = authority_selection.resolve_selected_authority(layout.shot).selection_token
    calls = []

    def controlled_gate(view, **kwargs):
        calls.append(view)
        return GateResult("fixture", [Finding.in_layer("injected", True, "2", "unit", "repair required")]
                          if dirty else [], {})

    monkeypatch.setattr(plan_gate, "run", controlled_gate)
    result = execute(bound, [("finalize_materialization", {})])[0]
    assert calls, result
    assert json.loads(result.data_json)["finalization_current"] is (not dirty), result
    assert result.status == ("refused" if dirty else "ok")
    assert authority_selection.resolve_selected_authority(layout.shot).selection_token == original_selection


def test_reference_crop_returns_a_real_witness_handle(bound):
    layout, _candidate_path, full = bound
    source = full["layer"]["judge"][0]["ref"]
    result = execute(bound, [("mint_refobs", {"source": source, "box": [0.1, 0.1, 0.8, 0.8]})])[0]
    assert result.status == "ok", result
    record = json.loads(result.data_json)
    token = record["result"]["witness_id"]
    assert token.startswith("refobs-")
    assert (layout.shot / "state/refobs" / f"{token}.json").is_file()
    assert (layout.shot / "state/refobs" / f"{token}.png").is_file()
    assert record["before"] == record["after"]
    assert not record["authority_selected"]


@pytest.mark.parametrize("location", ["shot", "other_run", "hardlink"])
def test_candidate_requires_current_run_and_unshared_file(bound, location):
    layout, candidate, _ = bound
    if location == "hardlink":
        os.link(candidate, candidate.with_name("alias.json"))
        target = candidate
    else:
        target = (layout.shot / "candidate.json" if location == "shot"
                  else layout.root.parent / "other-run" / "scratch" / "candidate.json")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(candidate.read_bytes())
    with pytest.raises(ValueError):
        flynn_materialization_tools.materialization_tools(layout=layout, candidate=target, check_current=lambda: None)
