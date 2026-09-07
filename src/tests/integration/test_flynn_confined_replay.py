"""Scripted Flynn dispatch into real VFX confinement and cold artifact replay.

This proves the execution/evidence seam, not production WorkUnit receipt publication.
The only registered mutation is a frozen test program; there is no arbitrary-code tool.
"""

from __future__ import annotations

import asyncio
import json
import shutil

import flynn_agents_sdk as flynn
import pytest

from vfx_harness.agents.builder.models import _RESET
from vfx_harness.agents.builder.prior import _run_artifact_script
from vfx_harness.blender.session import BlenderSession
from vfx_harness.evidence.scene_checks import _blender_probe, _evidence, validate_row

_PROGRAM = """import bpy
host = bpy.data.objects.new('test_control', None)
bpy.context.scene.collection.objects.link(host)
host['bvfx_role'] = 'fixture.control'
"""


@pytest.mark.skipif(shutil.which("blender") is None, reason="Blender is unavailable")
@pytest.mark.parametrize("expected", [1, 2])
def test_scripted_mutation_cold_replays_without_accepting_a_unit(tmp_path, expected):
    script = tmp_path / "candidate.py"
    script.write_text(_PROGRAM)
    row = {
        "id": "control-count",
        "kind": "object_count",
        "roles": ["fixture.control"],
        "op": "eq",
        "value": expected,
        "owner_layer": "1",
        "fault_owner": "1",
        "activates_at": "1",
        "lifecycle": "layer",
        "axis": "state",
    }
    assert validate_row(row) is None

    def measure(session):
        raw = session.run(_blender_probe([row], 1), journal=False)["result"]
        return list(_evidence([row], raw))

    def validate(arguments):
        if arguments != "{}":
            raise ValueError("The fixture permits only its frozen program")

    class EvidenceEvaluation:
        async def evaluate(self, candidate):
            evidence = json.loads(candidate.output)
            passed = all(item["pass"] is True for item in evidence)
            return flynn.Evaluation(
                candidate,
                "vfx-scene-check/v1",
                "fixture evidence only",
                flynn.Verdict.SATISFIED if passed else flynn.Verdict.FAILED,
                "Measured declared scene contract; no unit receipt published",
            )

    with BlenderSession(artifacts_dir=tmp_path / "live") as live:
        live.run(_RESET, journal=False)

        async def execute(arguments):
            await asyncio.to_thread(_run_artifact_script, live, script, journal=False)
            return json.dumps(await asyncio.to_thread(measure, live), sort_keys=True)

        with flynn.SQLiteRun.create(
            tmp_path / "execution.sqlite",
            run_id="confined-replay",
            initial_state="unaccepted",
            limits=flynn.RunLimits(1, 1, 1, 120),
        ) as run:
            runtime = flynn.Runtime(
                inference=flynn.ScriptedAdapter([flynn.ToolCall("build_fixture", "{}")]),
                tools=flynn.ToolBroker(
                    [flynn.Tool("build_fixture", validate, execute, observation=True, external_action=True)]
                ),
                evaluator=EvidenceEvaluation(),
                run=run,
                grants=("build_fixture",),
            )
            result = asyncio.run(runtime.step("Execute the permitted fixture and measure it"))
            assert result.evaluation.verdict is (flynn.Verdict.SATISFIED if expected == 1 else flynn.Verdict.FAILED)
            assert not result.committed and run.outcome() is None
            observed = json.loads(result.candidate.output)
            assert observed[0]["value"] == 1
            assert run.remaining()["external"] == 0

    # A different worker starts from factory state; the harness owns the replay barrier.
    with BlenderSession(artifacts_dir=tmp_path / "cold") as cold:
        cold.run(_RESET, journal=False)
        _run_artifact_script(cold, script, journal=False)
        replayed = measure(cold)
        assert replayed == observed
    with flynn.SQLiteRun.open(tmp_path / "execution.sqlite") as run:
        assert run.read().revision == 0
        assert json.loads(run.latest_observation()) == replayed
        assert len(run.records()["evaluations"]) == 1
