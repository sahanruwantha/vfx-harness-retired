"""The default production layer dispatcher earns real native unit/layer receipts."""

from __future__ import annotations

import asyncio
import hashlib
import json

import flynn_agents_sdk as flynn
from flynn_agents_sdk import deepseek

from tests.integration.test_flynn_dependent_layer import _fixture, _program
from vfx_harness.agents.builder import layer as layer_runtime
from vfx_harness.agents.builder import unit_dispatch
from vfx_harness.blender.session import BlenderSession
from vfx_harness.orchestration import unit_state
from vfx_harness.orchestration.layer_publication import require_current_layer_publication


def test_default_layer_builder_uses_flynn_and_earns_receipts(tmp_path, monkeypatch):
    shot, layer, selected, layout = _fixture(tmp_path, monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-key")
    monkeypatch.setenv("VFXH_EXECUTABLE_BUILDER_MODEL", deepseek.VISION_MODEL)
    monkeypatch.delenv("VFXH_RUN_MAX_USD", raising=False)
    constructed = []

    class Adapter:
        def __init__(self, **kwargs):
            constructed.append(kwargs)
            self.index = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def plan_output(self, request, available):
            return flynn.OutputReservation("scripted", 0)

        async def generate(self, request):
            card = json.loads(request.objective.split("[unit-scope]\n", 1)[1].split("\n\n[unit-plan]", 1)[0])
            program = _program(card["unit_id"])
            calls = [
                flynn.ToolCall("inspect_unit", "{}"),
                flynn.ToolCall("write_candidate", json.dumps({"source": program})),
                flynn.ToolCall("probe_candidate", "{}"),
                flynn.ToolCall("freeze_candidate", json.dumps({
                    "sha256": hashlib.sha256(program.encode()).hexdigest(),
                })),
            ]
            call = calls[self.index]
            self.index += 1
            return flynn.InferenceResult.scripted(call)

    monkeypatch.setattr(deepseek, "DeepSeekAdapter", Adapter)

    async def forbidden(*args, **kwargs):
        raise AssertionError("executable production unit reached Claude")

    monkeypatch.setattr(unit_dispatch.unit_loop, "build_unit", forbidden)
    with BlenderSession(artifacts_dir=layout.scratch / "worker", cwd=tmp_path) as session:
        ledger = asyncio.run(layer_runtime.build_layer(shot, layer, session,
                            selected_authority=selected, verbose=False))
    assert ledger.status(layer.as_milestone({})) == "passed"
    state = unit_state.load(tmp_path, "1")
    assert all(state["units"][unit.id]["completion_receipt"] for unit in layer.stages)
    publication = require_current_layer_publication(tmp_path, layer, selected)
    assert publication.receipt.final_status == "passed"
    assert len(constructed) == len(layer.stages)
    journals = list((layout.checkpoints / "flynn").glob("*.sqlite"))
    assert len(journals) == len(layer.stages)
    for database in journals:
        with flynn.SQLiteRun.open(database) as run:
            assert len(run.records()["operations"]) == 5
            assert len(run.records()["guard_decisions"]) == 5
            assert run.records()["commits"] == []
            assert run.outcome().startswith("unit_evaluation_receipt:")
