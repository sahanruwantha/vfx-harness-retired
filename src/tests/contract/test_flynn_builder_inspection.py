"""Inspection never reports a clean observation from missing or changed replay inputs."""

from __future__ import annotations

import asyncio

import flynn_agents_sdk as flynn
import pytest

from tests.integration.test_flynn_unit_lifecycle import _authority
from vfx_harness.agents.builder import flynn_unit, prior
from vfx_harness.blender.session import BlenderError
from vfx_harness.observability.run_artifacts import RunLayout
from vfx_harness.orchestration.builder_execution_fence import builder_execution_fence
from vfx_harness.orchestration.ledger import Milestone


@pytest.mark.parametrize("failure", ["missing", "replay", "changed_during_replay", "changed_during_report"])
def test_inspection_failure_never_reaches_model_feedback(tmp_path, monkeypatch, failure):
    shot, layer, unit, selected, guard, layout = _authority(tmp_path, monkeypatch)
    source = tmp_path / "build" / "inspection-prior.py"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("# bound prior source\n")
    calls = []

    class Session:
        def run(self, code, **kwargs):
            calls.append(code)
            return {"result": {}}

    def replay(session, paths, prepared):
        assert paths == [source]
        assert prepared[0].payload == b"# bound prior source\n"
        if failure == "replay":
            raise BlenderError("injected prior replay failure")
        if failure == "changed_during_replay":
            source.write_text("# substituted prior\n")

    monkeypatch.setattr(prior, "_run_prior_paths", replay)
    if failure == "missing":
        source.unlink()
    if failure == "changed_during_report":
        write_report = RunLayout.write_report

        def change_source(self, name, payload):
            result = write_report(self, name, payload)
            if name.startswith("flynn-unit-inspection-"):
                source.write_text("# substituted while recording\n")
            return result

        monkeypatch.setattr(RunLayout, "write_report", change_source)

    with builder_execution_fence(tmp_path) as lease, pytest.raises(BlenderError):
        asyncio.run(flynn_unit.build_unit(
            shot, Milestone("1@lock", 240, "refs/a.png", "control exists"),
            unit.mutates.script_spans[0], [source], Session(),
            inference=flynn.ScriptedAdapter([flynn.ToolCall("inspect_unit", "{}")]),
            limits=flynn.RunLimits(5, 5, 4, 180), layer=layer, active_unit=unit,
            selected_authority=selected, attempt_guard=guard, fence_lease=lease, verbose=False,
        ))
    with flynn.SQLiteRun.open(layout.checkpoints / "flynn" / f"{guard.claim.claim_id}.sqlite") as run:
        assert run.latest_observation() is None
        assert run.pending().stage == "dispatched"
        assert run.remaining() == {"inference": 4, "tool": 4, "external": 3}
        assert run.records()["commits"] == []
    reports = list(layout.reports.glob("flynn-unit-inspection-*.json"))
    assert len(reports) == (1 if failure == "changed_during_report" else 0)
    if failure == "missing":
        assert calls == []
    assert not (tmp_path / unit.mutates.script_spans[0]).exists()
