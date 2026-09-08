"""Native retrieval replaces feedback, respects scope and preserves canonical work."""

import asyncio
import hashlib
import json

import flynn_agents_sdk as flynn
import pytest

from tests.integration.test_flynn_unit_lifecycle import _PROGRAM, _authority
from vfx_harness.agents.builder import flynn_unit
from vfx_harness.blender.session import BlenderSession
from vfx_harness.knowledge import recipes
from vfx_harness.orchestration import unit_state
from vfx_harness.orchestration.builder_execution_fence import builder_execution_fence
from vfx_harness.orchestration.ledger import Milestone


@pytest.mark.parametrize("first_status", ["ok", "context_refused", "out_of_scope"])
def test_native_recipe_feedback_and_journal_precede_real_replay(tmp_path, monkeypatch, first_status):
    shot, layer, unit, selected, guard, layout = _authority(tmp_path, monkeypatch)
    marker = "FIRST_RECIPE_ONLY"
    monkeypatch.setattr(recipes, "_all", lambda: [
        {"name": "first", "when": "fixture", "verified": True, "tags": ["api"],
         "requires_roles": ["light.*"] if first_status == "out_of_scope" else [],
         "body": marker + ("x" * 11000 if first_status == "context_refused" else "")},
        {"name": "second", "when": "fixture", "verified": True, "tags": ["api"],
         "body": "SECOND_RECIPE_ONLY"},
    ])
    source_digest = hashlib.sha256(_PROGRAM.encode()).hexdigest()
    calls = [
        flynn.ToolCall("find_recipe", json.dumps({"query": "first#full"})),
        flynn.ToolCall("find_recipe", json.dumps({"query": "second#full"})),
        flynn.ToolCall("write_candidate", json.dumps({"source": _PROGRAM})),
        flynn.ToolCall("probe_candidate", "{}"),
        flynn.ToolCall("freeze_candidate", json.dumps({"sha256": source_digest})),
    ]

    class Adapter:
        index = 0

        async def generate(self, request):
            if self.index in {1, 2}:
                observation = json.loads(request.observation)
                data = observation["data"]
                assert data["claim_id"] == guard.claim.claim_id
                assert data["unit_digest"] == guard.claim.unit_digest
                if "mutation_roles" in data:
                    assert data["mutation_roles"] == list(unit.mutates.roles)
                assert not data["mutation_authorized"]
                if self.index == 1:
                    assert data["status"] == ("found" if first_status == "ok" else first_status)
                    if first_status != "ok":
                        assert marker not in request.observation
                else:
                    assert "SECOND_RECIPE_ONLY" in request.observation
                    assert marker not in request.observation
                    assert "find_recipe" not in request.allowed_tools
            if self.index == 3:
                assert "SECOND_RECIPE_ONLY" not in request.observation
            call = calls[self.index]
            self.index += 1
            return flynn.InferenceResult.scripted(call)

    milestone = Milestone("1@lock", 240, "refs/a.png", "control exists")
    with builder_execution_fence(tmp_path) as lease, BlenderSession(
        artifacts_dir=layout.scratch / "worker", cwd=tmp_path,
    ) as session:
        ledger = asyncio.run(flynn_unit.build_unit(
            shot, milestone, unit.mutates.script_spans[0], [], session,
            inference=Adapter(), limits=flynn.RunLimits(6, 6, 3, 180),
            layer=layer, active_unit=unit, selected_authority=selected,
            attempt_guard=guard, fence_lease=lease,
        ))
        assert ledger.status(milestone) == "passed"
        with flynn.SQLiteRun.open(layout.checkpoints / "flynn" / f"{guard.claim.claim_id}.sqlite") as run:
            operations = run.records()["operations"]
            assert len(operations) == 6
            assert not run.records()["commits"]
            result = flynn.ToolResult.from_json(operations[1]["output"])
            data = json.loads(result.data_json)
            assert data["used"] == ["second"]
            assert data["response_sha256"] == hashlib.sha256(result.content[0].text.encode()).hexdigest()
            assert data["remaining_reads"] == 4
            assert run.remaining() == {"inference": 0, "tool": 0, "external": 0}
        assert unit_state.load(tmp_path, "1")["units"][unit.id]["status"] == "building"
