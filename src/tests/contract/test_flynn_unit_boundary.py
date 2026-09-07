"""First offline VFX/Flynn gate: scoped evidence is not unit acceptance.

These checks use VFX's actual WorkUnit and scope compiler. They do not establish
Blender replay, scene mutation confinement, or production publication readiness.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from vfx_harness.agents.unit_scope import compile_unit_scope
from vfx_harness.domain.work_units import WorkUnit
from vfx_harness.orchestration.unit_state import unit_digest

flynn = pytest.importorskip("flynn_agents_sdk", reason="Install the private .[flynn] migration extra")


@pytest.fixture(params=[("structure", "geometry"), ("lighting", "illumination")])
def scope(request):
    uid, role = request.param
    unit = WorkUnit.parse(
        {
            "id": uid,
            "title": uid,
            "plan": f"plans/{uid}.md",
            "depends_on": [],
            "mutates": {
                "mode": "scoped",
                "roles": [role],
                "controls": [],
                "script_spans": [f"build/units/01/{uid}.py"],
            },
            "protects": {"selector": "all_active_upstream_interfaces", "resolve_to_explicit_ids_at": "freeze"},
            "evaluation": {
                "primary_judge": 1,
                "judge": [{"frame": 1, "ref": "refs/example.png"}],
                "temporal_evidence": "none",
                "claims": [
                    {
                        "id": f"claim.{uid}",
                        "proposition": "Declared host exists",
                        "axis": "form",
                        "property": f"state.{uid}",
                        "subject_roles": [role],
                        "subject_controls": [],
                        "moments": [1],
                        "kind": "atomic",
                        "required": True,
                        "authority": "executable_required",
                        "repair_owner": uid,
                        "asserts": "scene",
                        "evidence": [{"kind": "scene_contract", "id": "host-exists"}],
                    }
                ],
            },
            "completion": "all_required_claims_and_protected_contracts_pass",
            "provides": [],
        },
        "flynn-gate",
    )
    return compile_unit_scope(
        unit=unit,
        layer_id="01",
        unit_digest=unit_digest(unit),
        helpers=(),
        contracts=[{"id": "host-exists", "kind": "object_count", "roles": [role], "frame": 1}],
    )


class ScopeEvaluation:
    async def evaluate(self, candidate):
        card = json.loads(candidate.output)
        assert card["producer_unit_digest"]
        return flynn.Evaluation(
            candidate,
            "vfx-scope-transport/v1",
            "scope transport only",
            flynn.Verdict.SATISFIED,
            "Exact compiled scope returned; no scene evaluated",
        )


def make_runtime(run, scope, calls, *, max_characters=12000):
    frozen = json.dumps(scope, sort_keys=True)

    def validate(arguments):
        if json.loads(arguments) != {}:
            raise ValueError("unit_scope accepts no scope substitution")

    async def inspect(arguments):
        return frozen

    def prepare(request):
        packet = flynn.ContextCompiler(max_characters=max_characters).compile(
            (
                flynn.ContextItem("unit-scope", frozen, required=True, evidence_ids=(scope["producer_unit_digest"],)),
                flynn.ContextItem("optional-history", "x" * 12000),
            )
        )
        return replace(request, objective=packet.text)

    return flynn.Runtime(
        inference=flynn.ScriptedAdapter(calls),
        tools=flynn.ToolBroker(
            [
                flynn.Tool("unit_scope", validate, inspect, observation=True),
            ]
        ),
        evaluator=ScopeEvaluation(),
        run=run,
        grants=("unit_scope",),
        prepare_request=prepare,
    )


def test_vfx_scope_observation_never_accepts_a_unit(tmp_path, scope):
    path = tmp_path / "execution.sqlite"
    with flynn.SQLiteRun.create(
        path, run_id="scope-gate", initial_state="unaccepted", limits=flynn.RunLimits(2, 2, 0)
    ) as run:
        runtime = make_runtime(run, scope, [flynn.ToolCall("unit_scope", "{}"), flynn.ToolCall("run_bpy", "{}")])
        result = asyncio.run(runtime.step("inspect current scope"))
        assert result.evaluation.verdict is flynn.Verdict.SATISFIED
        assert not result.committed
        assert run.read().value == "unaccepted"
        assert run.outcome() is None
        assert json.loads(run.latest_observation()) == scope
        request = json.loads(run.records()["operations"][0]["request"])
        assert scope["producer_unit_digest"] in request["objective"]
        assert "optional-history" not in request["objective"]
        with pytest.raises(flynn.ContractError, match="not granted"):
            asyncio.run(runtime.step("unauthorized mutation"))
        assert run.remaining() == {"inference": 0, "tool": 1, "external": 0}
    with flynn.SQLiteRun.open(path) as run:
        assert run.read().revision == 0
        assert run.records()["commits"] == []
        assert json.loads(run.latest_observation()) == scope


def test_required_vfx_scope_cannot_be_dropped_to_fit(tmp_path, scope):
    with flynn.SQLiteRun.create(
        tmp_path / "execution.sqlite",
        run_id="scope-overflow",
        initial_state="unaccepted",
        limits=flynn.RunLimits(1, 1, 0),
    ) as run:
        runtime = make_runtime(run, scope, [flynn.ToolCall("unit_scope", "{}")], max_characters=10)
        with pytest.raises(ValueError, match="Required context"):
            asyncio.run(runtime.step("inspect"))
        assert run.records()["operations"] == []
        assert run.remaining()["inference"] == 1


def test_native_structured_scope_result_is_observation_only(tmp_path, scope):
    expected = flynn.ToolResult(
        (flynn.TextContent("Exact active-unit scope"),),
        data_json=json.dumps(scope, sort_keys=True),
    )

    def validate(arguments):
        if arguments:
            raise ValueError("scope substitution is forbidden")

    async def inspect(arguments):
        return expected

    class Evaluation:
        async def evaluate(self, candidate):
            result = flynn.ToolResult.from_json(candidate.output)
            assert json.loads(result.data_json) == scope
            return flynn.Evaluation(candidate, "scope-transport", "observation", flynn.Verdict.SATISFIED,
                                    "Transport proven, not unit acceptance")

    tool = flynn.Tool.structured(
        "unit_scope", description="Read exact scope", parameters_json='{"type":"object"}',
        validate=validate, execute=inspect,
    )
    path = tmp_path / "structured.sqlite"
    with flynn.SQLiteRun.create(path, run_id="scope", initial_state="unaccepted",
                               limits=flynn.RunLimits(1, 1, 0)) as run:
        steps = []

        def policy(view):
            if view.last_step is None:
                return flynn.SessionStep("Inspect declared scope", ("unit_scope",))
            assert flynn.ToolResult.from_json(view.observation) == expected
            return flynn.SessionStop("scope observed; VFX acceptance remains unproven")

        session = flynn.Session(
            inference=flynn.ScriptedAdapter([flynn.ToolCall("unit_scope", "{}")]),
            tools=flynn.ToolBroker([tool]), evaluator=Evaluation(), run=run, grants=("unit_scope",),
            policy=policy, on_step=steps.append,
        )
        terminal = asyncio.run(session.execute())
        assert terminal.kind == "stopped"
        assert terminal.completed_steps == 1
        assert len(steps) == 1 and not steps[0].committed
        assert run.read().value == "unaccepted"
    with flynn.SQLiteRun.open(path) as run:
        assert flynn.ToolResult.from_json(run.latest_observation()) == expected
        assert flynn.SessionTermination.from_json(run.outcome()) == terminal
        assert run.read().revision == 0
