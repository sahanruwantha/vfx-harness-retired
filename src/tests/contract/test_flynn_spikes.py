"""Native spikes preserve VFX scope and never turn stdout into acceptance."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys

import flynn_agents_sdk as flynn
import pytest

from tests.contract.test_flynn_materialization_tools import bound as bound
from tests.contract.test_flynn_plan_publication import ObservationOnly
from vfx_harness.agents import flynn_spikes, spike_policy
from vfx_harness.blender import spike_execution


def arguments(**overrides):
    return {"script": "import bpy\nprint('probe')", "contracts": [], "render_frame": None, "timeout": 30,
            **overrides}


def execute(bound, steps, check=lambda: None):
    results = []

    class Adapter:
        index = 0

        async def generate(self, request):
            args = steps[self.index]
            self.index += 1
            return flynn.InferenceResult.scripted(flynn.ToolCall("spike", json.dumps(args)))

    async def run():
        tools, guard = flynn_spikes.planning_spike_tools(layout=bound[0], layer_id="2", check_current=check)
        with flynn.SQLiteRun.create(bound[0].checkpoints / "spikes.sqlite", run_id="spikes",
                                   initial_state="unaccepted", limits=flynn.RunLimits(10, 10, 10)) as journal:
            await flynn.Session(
                inference=Adapter(), tools=flynn.ToolBroker(tools), evaluator=ObservationOnly(), run=journal,
                guards=(guard,), grants=("spike",),
                on_step=lambda step: results.append(flynn.ToolResult.from_json(step.candidate.output)),
                policy=lambda view: flynn.SessionStop("done") if view.completed_steps == len(steps)
                else flynn.SessionStep("Test technique", ("spike",)),
            ).execute()
    asyncio.run(run())
    return results


def fake_runner(**kwargs):
    kwargs["check_current"]()
    kwargs["directory"].mkdir(parents=True)
    (kwargs["directory"] / "script.py").write_text(kwargs["script"])
    return {"status": "measured", "passed": False, "results": [], "executed": True}


def test_native_spike_records_report_and_bounds_attempts(bound, monkeypatch):
    monkeypatch.setattr(spike_execution, "execute_spike", fake_runner)
    results = execute(bound, [arguments()] * 5)
    assert all(result.status == "ok" for result in results[:4])
    assert results[-1].status == "refused"
    data = json.loads(results[0].data_json)
    report = json.loads((bound[0].root / data["report"]).read_text())
    assert report["layer"] == "2" and report["artifacts"]
    assert not report["planning_evidence_published"] and not report["plan_authority_changed"]
    with flynn.SQLiteRun.open(bound[0].checkpoints / "spikes.sqlite") as journal:
        assert journal.records()["commits"] == []
        assert journal.remaining()["external"] == 5


def test_timeout_exhausts_hypothesis_after_two_failures(bound, monkeypatch):
    def timeout(**kwargs):
        result = fake_runner(**kwargs)
        return {**result, "status": "timeout"}
    monkeypatch.setattr(spike_execution, "execute_spike", timeout)
    results = execute(bound, [arguments()] * 3)
    assert all(result.status == "refused" for result in results)
    assert "already failed 2" in results[-1].content[0].text


@pytest.mark.parametrize("args", [arguments(script="import os"), arguments(timeout=181),
                                   arguments(contracts=[{"kind": "invented"}]), arguments(extra=True)])
def test_invalid_spike_refuses_before_tool_reservation(bound, args):
    with pytest.raises(flynn.ProposalRejected):
        execute(bound, [args])
    with flynn.SQLiteRun.open(bound[0].checkpoints / "spikes.sqlite") as journal:
        assert journal.remaining()["external"] == journal.remaining()["tool"] == 10


def test_owner_loss_during_spike_publishes_no_success(bound, monkeypatch):
    live = True

    def check():
        if not live:
            raise ValueError("owner lost")

    def run(**kwargs):
        nonlocal live
        result = fake_runner(**kwargs)
        live = False
        return result

    monkeypatch.setattr(spike_execution, "execute_spike", run)
    with pytest.raises(ValueError, match="owner lost"):
        execute(bound, [arguments()], check)
    assert not list(bound[0].reports.glob("native-spike-*.json"))


def test_hypothesis_reordering_does_not_reset_failure_budget():
    rows = [{"id": "a", "kind": "bbox_width", "frame": 1}, {"id": "b", "kind": "bbox_height", "frame": 1}]
    assert spike_policy._SpikeBudget.key(rows) == spike_policy._SpikeBudget.key(list(reversed(rows)))


def test_native_spikes_import_without_claude():
    result = subprocess.run([sys.executable, "-c", "import sys; sys.modules['claude_agent_sdk']=None; "
                             "from vfx_harness.agents.flynn_spikes import planning_spike_tools"],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
