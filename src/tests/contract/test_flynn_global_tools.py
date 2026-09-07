"""Bounded planner reads and gate observations share the publisher's exact draft."""

from __future__ import annotations

import asyncio
import json

import flynn_agents_sdk as flynn
import pytest

from tests.contract.test_flynn_mapping_publication import bound as bound
from tests.contract.test_flynn_plan_publication import ObservationOnly
from vfx_harness.agents import flynn_global_tools
from vfx_harness.evaluation import plan_gate
from vfx_harness.evaluation.plan_gate.types import Finding, GateResult
from vfx_harness.orchestration import authority_selection
from vfx_harness.orchestration.plan_bundle_integrity import digest


def execute(bound, steps, *, after_inference=lambda _: None):
    layout, _, _ = bound
    results = []

    class Adapter:
        def __init__(self):
            self.index = 0

        async def generate(self, request):
            name, arguments = steps[self.index]
            after_inference(self.index)
            self.index += 1
            return flynn.InferenceResult.scripted(flynn.ToolCall(name, json.dumps(arguments)))

    async def run():
        tools, guard = flynn_global_tools.global_planning_tools(layout=layout, check_current=lambda: None)
        grants = tuple(tool.name for tool in tools)
        limits = flynn.RunLimits(len(steps), len(steps), len(steps))
        with flynn.SQLiteRun.create(layout.checkpoints / "global.sqlite", run_id="global",
                                   initial_state="unaccepted", limits=limits) as journal:
            session = flynn.Session(
                inference=Adapter(), tools=flynn.ToolBroker(tools), evaluator=ObservationOnly(), run=journal,
                grants=grants, guards=(guard,),
                on_step=lambda _: results.append(flynn.ToolResult.from_json(journal.latest_observation())),
                policy=lambda view: (flynn.SessionStop("fixture complete") if view.completed_steps == len(steps)
                                     else flynn.SessionStep("Bounded planning", grants)),
            )
            await session.execute()
    asyncio.run(run())
    return results


def publish(bound):
    return "publish_ownership_mapping", bound[2]


def test_publish_read_gate_share_the_same_generation(bound):
    layout, workspace, _ = bound
    results = execute(bound, [
        publish(bound), ("read_plan_input", {"name": "layers.json", "offset": 0}), ("run_gate", {}),
    ])
    publication, read, gate = [json.loads(result.data_json) for result in results]
    assert publication["artifacts"] == read["artifacts"] == gate["artifacts"]
    assert read["sha256"] == digest((workspace / "layers.json").read_bytes())
    assert results[1].content[0].text == (workspace / "layers.json").read_text()[:flynn_global_tools.READ_CHARS]
    assert gate["clean"] is True and gate["blocking_count"] == 0
    report = layout.root / gate["report"]
    assert digest(report.read_bytes()) == gate["report_sha256"]
    record = json.loads(report.read_text())
    assert record["artifacts"] == gate["artifacts"]
    assert record["evaluation"]["shot"] != "plan-workspace"
    assert record["gate_attested"] is False
    assert authority_selection.resolve_selected_authority(layout.shot).plan is None
    with flynn.SQLiteRun.open(layout.checkpoints / "global.sqlite") as journal:
        assert journal.records()["commits"] == []
        assert journal.remaining()["external"] == 1  # reads charge no external action


def test_text_paging_is_exact_bounded_and_hash_bound(bound):
    _, workspace, mapping = bound
    mapping["axes"][0]["desc"] = "é" * 4500
    results = execute(bound, [publish(bound),
        ("read_plan_input", {"name": "ownership_mapping.json", "offset": 0}),
        ("read_plan_input", {"name": "ownership_mapping.json", "offset": 4000}),
    ])
    payload = (workspace / "ownership_mapping.json").read_bytes()
    for index, offset in [(1, 0), (2, 4000)]:
        data = json.loads(results[index].data_json)
        assert results[index].content[0].text == payload.decode()[offset:offset + 4000]
        assert data["sha256"] == digest(payload)
        assert data["offset"] == offset
    assert json.loads(results[1].data_json)["next_offset"] == 4000


@pytest.mark.parametrize("arguments", [
    {"name": "../brief.md", "offset": 0}, {"name": "/etc/passwd", "offset": 0},
    {"name": "refs/f001.png", "offset": 0}, {"name": "brief.md", "offset": True},
    {"name": "brief.md", "offset": -1}, {"name": "brief.md", "offset": 0, "limit": 100000},
])
def test_read_refuses_ambient_paths_and_unbounded_parameters(bound, arguments):
    with pytest.raises(ValueError):
        execute(bound, [("read_plan_input", arguments)])
    with flynn.SQLiteRun.open(bound[0].checkpoints / "global.sqlite") as journal:
        assert journal.remaining()["tool"] == 1


def test_unwritten_document_is_explicit_refusal(bound):
    result = execute(bound, [("read_plan_input", {"name": "layers.json", "offset": 0})])[0]
    assert result.status == "refused"
    assert json.loads(result.data_json)["available"] is False


def test_offset_past_end_is_refused_without_unresolved_operation(bound):
    length = len((bound[1] / "brief.md").read_text())
    result = execute(bound, [("read_plan_input", {"name": "brief.md", "offset": length + 1})])[0]
    assert result.status == "refused"
    data = json.loads(result.data_json)
    assert data["error"] == "offset_out_of_range" and data["total_chars"] == length
    with flynn.SQLiteRun.open(bound[0].checkpoints / "global.sqlite") as journal:
        assert journal.pending() is None
        assert journal.remaining()["external"] == 1


def test_read_refuses_draft_replaced_during_inference(bound):
    def replace(index):
        if index == 1:
            (bound[1] / "layers.json").write_text("new owner")

    with pytest.raises(ValueError, match="changed outside"):
        execute(bound, [publish(bound), ("read_plan_input", {"name": "layers.json", "offset": 0})],
                after_inference=replace)
    assert (bound[1] / "layers.json").read_text() == "new owner"
    with flynn.SQLiteRun.open(bound[0].checkpoints / "global.sqlite") as journal:
        assert journal.remaining()["tool"] == 1


def test_gate_without_complete_draft_refuses_before_external_spend(bound):
    with pytest.raises(ValueError, match="complete published draft"):
        execute(bound, [("run_gate", {})])
    with flynn.SQLiteRun.open(bound[0].checkpoints / "global.sqlite") as journal:
        assert journal.remaining()["tool"] == journal.remaining()["external"] == 1


def test_four_call_gate_cap_is_enforced_before_fifth_dispatch(bound):
    with pytest.raises(ValueError, match="call cap"):
        execute(bound, [publish(bound), *[("run_gate", {})] * 5])
    assert len(list(bound[0].reports.glob("native-plan-gate-*.json"))) == 4
    with flynn.SQLiteRun.open(bound[0].checkpoints / "global.sqlite") as journal:
        assert journal.remaining()["tool"] == journal.remaining()["external"] == 1


def test_unchanged_dirty_gate_reports_plateau_and_refuses_further_gate(bound, monkeypatch):
    finding = Finding.in_layer("fixture", True, "2", "contract", "unresolved", "repair the owning layer")
    monkeypatch.setattr(plan_gate, "run", lambda *a, **k: GateResult("fixture", [finding]))
    with pytest.raises(ValueError, match="plateau"):
        execute(bound, [publish(bound), ("run_gate", {}), ("run_gate", {}), ("run_gate", {})])
    with flynn.SQLiteRun.open(bound[0].checkpoints / "global.sqlite") as journal:
        data = json.loads(flynn.ToolResult.from_json(journal.latest_observation()).data_json)
        assert data["halt_reason"] == "plateau"
        assert data["findings"][0]["layer"] == "2"
        assert journal.remaining()["tool"] == 1


def test_oversized_findings_are_archived_without_hiding_dirty_status(bound, monkeypatch):
    findings = [Finding("fixture", False, "draft", "warning"), Finding("fixture", True, "draft", "x" * 9000)]
    monkeypatch.setattr(plan_gate, "run", lambda *a, **k: GateResult("fixture", findings))
    data = json.loads(execute(bound, [publish(bound), ("run_gate", {})])[-1].data_json)
    assert data["clean"] is False and data["blocking_count"] == 1
    assert data["findings"] == [] and data["omitted_findings"] == 2
    assert data["halt_reason"] == "feedback_overflow"
    record = json.loads((bound[0].root / data["report"]).read_text())
    assert len(record["evaluation"]["findings"]) == 2


def test_gate_draft_change_during_evaluation_cannot_produce_report(bound, monkeypatch):
    original = plan_gate.run

    def change(root, **kwargs):
        result = original(root, **kwargs)
        (root / "layers.json").write_text("substituted after evaluation")
        return result

    monkeypatch.setattr(plan_gate, "run", change)
    with pytest.raises(ValueError, match="changed outside"):
        execute(bound, [publish(bound), ("run_gate", {})])
    assert not list(bound[0].reports.glob("native-plan-gate-*.json"))
    with flynn.SQLiteRun.open(bound[0].checkpoints / "global.sqlite") as journal:
        assert journal.pending().stage == "dispatched"


def test_report_write_failure_remains_pending_without_retry(bound, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("injected report publication failure")

    monkeypatch.setattr(type(bound[0]), "write_report", fail)
    with pytest.raises(OSError, match="injected"):
        execute(bound, [publish(bound), ("run_gate", {})])
    with flynn.SQLiteRun.open(bound[0].checkpoints / "global.sqlite") as journal:
        assert journal.pending().stage == "dispatched"
        assert journal.remaining()["external"] == 0


def test_unbound_auxiliary_file_refuses_gate_before_dispatch(bound):
    def inject(index):
        if index == 1:
            (bound[1] / ".plan-consumer-view.json").write_text("unexpected gate authority")

    with pytest.raises(ValueError, match="unbound inputs"):
        execute(bound, [publish(bound), ("run_gate", {})], after_inference=inject)
    with flynn.SQLiteRun.open(bound[0].checkpoints / "global.sqlite") as journal:
        assert journal.remaining()["external"] == 1


@pytest.mark.parametrize("citation", ["../outside.md", "/tmp/outside.md"])
def test_gate_refuses_citations_outside_its_bound_inputs(bound, citation):
    bound[2]["layers"][0]["title"] = f"A title citing `{citation}`"
    with pytest.raises(ValueError, match="citation leaves"):
        execute(bound, [publish(bound), ("run_gate", {})])
    with flynn.SQLiteRun.open(bound[0].checkpoints / "global.sqlite") as journal:
        assert journal.remaining()["external"] == 1
