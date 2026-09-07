"""Seeded planning authority, real Flynn units and native layer composition.

Planning/gate attestations are fixture setup, not evidence of planner quality.
No execution, verdict, checkpoint, completion or finalization publisher is replaced.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from contextlib import nullcontext

import pytest

from tests.unit.test_plan_records import _candidate, _write, publish_current
from vfx_harness.agents.builder import layer as layer_runtime
from vfx_harness.agents.builder import unit_completion
from vfx_harness.blender.session import BlenderSession
from vfx_harness.domain.brief import Shot
from vfx_harness.domain.unit_completion_receipts import UnitCompletionReceipt
from vfx_harness.observability import run_artifacts
from vfx_harness.observability.runid import RUN_ID
from vfx_harness.orchestration import layer_plans, unit_completion_state, unit_state
from vfx_harness.orchestration.authority_capsule_resolution import selected_layer_capsule_digest
from vfx_harness.orchestration.authority_selection import resolve_selected_authority
from vfx_harness.orchestration.layer_publication import require_current_layer_publication
from vfx_harness.orchestration.ledger import load_layers

flynn = pytest.importorskip("flynn_agents_sdk")
flynn_unit = pytest.importorskip("vfx_harness.agents.builder.flynn_unit")


def _fixture(root, monkeypatch):
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _candidate(root)
    document = json.loads((root / "layers.json").read_text())
    row = document["layers"][0]
    template = row["stages"][0]
    row["evidence_domains"] = ["scene"]
    row["stages"] = []
    contracts = []
    for name in ("consumer", "independent", "producer"):
        stage = copy.deepcopy(template)
        stage.update(id=name, title=name, plan=f"plans/01_finish/{name}.md",
                     depends_on=["producer"] if name == "consumer" else [])
        stage["mutates"] = {"mode": "scoped", "roles": [f"comp.{name}"],
                            "controls": [], "control_roles": {},
                            "script_spans": [f"build/units/01/{name}.py"]}
        if name == "producer":
            stage["publishes"] = [{"id": "producer-control", "kind": "placement_control",
                                   "exports": {"role": "comp.producer"}}]
        if name == "consumer":
            stage["consumes"] = [{"producer": "producer", "interface_id": "producer-control",
                                 "kind": "placement_control"}]
        evaluation = stage["evaluation"]
        evaluation.pop("composition_context")
        evaluation["temporal_evidence"] = "none"
        evaluation["claims"][0].update(
            id=f"claim.{name}", proposition=f"{name} control exists", property="object_count",
            subject_roles=[f"comp.{name}"], subject_controls=[], repair_owner=name, asserts="scene",
            evidence=[{"kind": "scene_contract", "id": f"count.{name}"}],
        )
        row["stages"].append(stage)
        contracts.append({"id": f"count.{name}", "kind": "object_count", "roles": [f"comp.{name}"],
                          "owner_layer": "1", "fault_owner": "1", "activates_at": "1", "lifecycle": "layer",
                          "axis": "final_lock", "op": "eq", "value": 1})
    _write(root / "layers.json", document)
    _write(root / "scene_checks.json", {"schema": 2, "contracts": contracts})
    requirements = json.loads((root / "requirements.json").read_text())
    requirements["requirements"][0]["resolution"] = {
        "kind": "deferred_owner", "ids": [], "owner_layer": "1",
        "due": {"kind": "before_layer", "layer": "1"}, "evidence_domains": ["scene"],
    }
    _write(root / "requirements.json", requirements)
    _write(root / "obligations.json", {"schema": "vfx-harness.obligations/v1", "obligations": []})
    layout = run_artifacts.create(root, RUN_ID)
    publish_current(root, layout, outcome="clean_with_deferred")
    monkeypatch.setenv(run_artifacts.ENV, str(layout.root))
    selected = resolve_selected_authority(root)
    shot = Shot(root, {"frames": 240, "fps": 24, "resolution": [64, 64]}, "DAG fixture")
    layer = load_layers(shot, selected_authority=selected)["1"]
    for unit in layer.stages:
        path = root / unit.plan
        path.parent.mkdir(parents=True, exist_ok=True)
        plan = (
            f"# Unit {unit.id}\n\nCreate exactly one Blender Empty named {unit.id}, "
            f"tagged bvfx_role=comp.{unit.id}. "
            + ("Read the accepted producer Empty named producer using bpy.data.objects.get; "
               "set this consumer's location.x to producer.location.x + 1. Do not modify the producer. "
               if unit.id == "consumer" else "Set its location.x to 2. ")
            + "Write a self-contained script. The harness resets the scene and replays accepted priors. "
            "Probe measured evidence, then freeze the observed SHA-256.\n"
        )
        path.write_text(plan + f"UNIT_PLAN_SENTINEL_{unit.id}\n")
        # Same explicit test-only planning attestation as the existing lifecycle fixture.
        layer_plans.stamp_work_unit_plan(
            root, path, gate={"clean": True, "blocking": 0, "run_id": layout.run_id},
            selected_authority=selected,
        )
    unit_state.initialize(root, "1", layer.stages,
                          plan_hash=selected_layer_capsule_digest(root, "1", selected))
    return shot, layer, selected, layout


def _program(name):
    prefix = "import bpy\n"
    if name == "consumer":
        prefix += (
            "source = bpy.data.objects.get('producer')\n"
            "assert source.location.x == 2\n"
        )
    return prefix + (
        f"host = bpy.data.objects.new('{name}', None)\n"
        "bpy.context.scene.collection.objects.link(host)\n"
        f"host['bvfx_role'] = 'comp.{name}'\n"
        + ("host.location.x = source.location.x + 1\n" if name == "consumer" else "host.location.x = 2\n")
    )


@pytest.mark.parametrize("fault", [None, "consumer_failure", "stale_producer"])
def test_flynn_dependency_order_and_native_composition(tmp_path, monkeypatch, fault):
    shot, layer, selected, layout = _fixture(tmp_path, monkeypatch)
    dispatched = []
    preserved = {}
    consumer_calls = []

    async def execute(*args, **kwargs):
        unit = kwargs["active_unit"]
        dispatched.append(unit.id)
        assert [p.name for p in args[3]] == [f"{name}.py" for name in dispatched[:-1]]
        if unit.id == "consumer":
            state = unit_state.load(tmp_path, "1")
            for name in ("independent", "producer"):
                preserved[name] = copy.deepcopy(state["units"][name])
            if fault == "stale_producer":
                with (tmp_path / "build/units/01/producer.py").open("a") as handle:
                    handle.write("\n# injected source drift\n")
        program = (
            "raise RuntimeError('injected consumer replay failure')\n"
            if unit.id == "consumer" and fault == "consumer_failure" else _program(unit.id)
        )
        scripted = flynn.ScriptedAdapter([
            flynn.ToolCall("write_candidate", json.dumps({"source": program})),
            flynn.ToolCall("probe_candidate", "{}"),
            flynn.ToolCall("freeze_candidate", json.dumps({"sha256": hashlib.sha256(program.encode()).hexdigest()})),
        ])

        class Adapter:
            async def generate(self, request):
                if unit.id == "consumer":
                    consumer_calls.append(request)
                card = json.loads(request.objective.split("[unit-scope]\n", 1)[1].split("\n\n[unit-plan]", 1)[0])
                predecessors = card["predecessor_interfaces"]
                assert [row["unit_id"] for row in predecessors] == list(unit.depends_on)
                assert all(row["dependency_status"] == "passed" for row in predecessors)
                if unit.id == "consumer":
                    assert [row["id"] for row in card["predecessor_publish_interfaces"]] == ["producer-control"]
                assert len(request.objective) + len(request.observation or "") <= 12000
                assert f"UNIT_PLAN_SENTINEL_{unit.id}" in request.objective
                for other in layer.stages:
                    if other.id != unit.id:
                        assert f"UNIT_PLAN_SENTINEL_{other.id}" not in request.objective
                return await scripted.generate(request)

        return await flynn_unit.build_unit(*args, **kwargs, inference=Adapter(), limits=flynn.RunLimits(4, 4, 3, 180))

    with BlenderSession(artifacts_dir=layout.scratch / "worker", cwd=tmp_path) as session:
        expectation = (
            pytest.raises(unit_completion_state.UnitCompletionConflict, match=r"producer\.py")
            if fault == "stale_producer" else nullcontext()
        )
        with expectation:
            ledger = asyncio.run(layer_runtime.build_layer(
                shot, layer, session, unit_builder=execute, selected_authority=selected, verbose=False,
            ))
        if fault is None:
            session.run(
                "import bpy\n"
                "bpy.context.view_layer.update()\n"
                "assert bpy.data.objects.get('producer').location.x == 2\n"
                "assert bpy.data.objects.get('consumer').location.x == 3\n",
                journal=False,
            )
    assert dispatched == ["independent", "producer", "consumer"]
    state = unit_state.load(tmp_path, "1")
    for name, previous in preserved.items():
        assert state["units"][name] == previous
    independent = next(unit for unit in layer.stages if unit.id == "independent")
    independent_receipt = UnitCompletionReceipt.parse(
        preserved["independent"]["completion_receipt"], "preserved independent",
    )
    with unit_completion_state.completed_unit_attempt_guard(
        tmp_path, "1", independent.id, layer.stages, independent_receipt,
        expected_plan_hash=selected_layer_capsule_digest(tmp_path, "1", selected),
        selection_token=selected.selection_token,
    ) as verified:
        assert verified == independent_receipt
    if fault == "stale_producer":
        with pytest.raises(unit_completion_state.UnitCompletionConflict, match=r"producer\.py"):
            unit_completion.resolve_completed_unit(
                tmp_path, "1", independent, layer.stages, independent_receipt,
                expected_plan_hash=selected_layer_capsule_digest(tmp_path, "1", selected), selected_authority=selected,
            )
    if fault:
        assert state["units"]["consumer"]["status"] == ("building" if fault == "stale_producer" else "failed")
        assert not state["units"]["consumer"].get("completion_receipt")
        assert not (tmp_path / layer.script).exists()
        if fault == "stale_producer":
            assert consumer_calls == []
            assert len(list((layout.checkpoints / "flynn").glob("*.sqlite"))) == 2
        else:
            assert len(consumer_calls) == 3
        return
    assert ledger.status(layer.as_milestone({})) == "passed"
    publication = require_current_layer_publication(tmp_path, layer, selected)
    assert publication.receipt.final_status == "passed"
    assert [row.unit_id for row in publication.receipt.evaluation_receipt.claim.unit_inputs] == dispatched
    state = unit_state.load(tmp_path, "1")
    assert all(row["status"] == "passed" for row in state["units"].values())
