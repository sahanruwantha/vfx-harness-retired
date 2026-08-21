from __future__ import annotations

import json
from pathlib import Path

import pytest

from vfx_harness.agents.planner import PlanGateFailure, PlanLoopResult
from vfx_harness.observability import run_artifacts, transcript


def test_structured_run_has_one_machine_readable_entrypoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shot = tmp_path / "shot-a"
    shot.mkdir()
    monkeypatch.delenv(run_artifacts.ENV, raising=False)

    layout = run_artifacts.create(
        shot,
        "20260821T120000Z-a1b2c3",
        shot_id="shot-a",
        argv=["vfx", "run", "shot-a"],
        parameters={"rounds": 2},
    )

    manifest = json.loads(layout.manifest.read_text(encoding="utf-8"))
    latest = json.loads((shot / "runs" / "latest.json").read_text(encoding="utf-8"))
    assert manifest["schema"] == "vfx-harness.run/v1"
    assert manifest["reader_entrypoint"] == "manifest.json"
    assert manifest["layout"]["evidence"] == "evidence/"
    assert latest["run_id"] == layout.run_id
    assert run_artifacts.active(shot) == layout
    assert run_artifacts.latest(shot) == layout


def test_inventory_classifies_outputs_without_scanning_the_shot_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shot = tmp_path / "shot-b"
    shot.mkdir()
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    layout = run_artifacts.create(shot, "run-001")
    render = layout.evidence / "renders" / "layer-1-round-1.png"
    render.write_bytes(b"png")
    report = layout.reports / "layers" / "layer-1.json"
    report.write_text("{}\n", encoding="utf-8")

    layout.write_inventory()
    rows = json.loads(layout.inventory.read_text(encoding="utf-8"))["artifacts"]
    by_path = {row["path"]: row for row in rows}
    assert by_path["evidence/renders/layer-1-round-1.png"]["category"] == "evidence"
    assert by_path["reports/layers/layer-1.json"]["media_type"] == "application/json"


def test_transcripts_are_grouped_by_run_stage_and_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shot = tmp_path / "shot-c"
    shot.mkdir()
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    monkeypatch.delenv("VFXH_NO_TRANSCRIPT", raising=False)
    layout = run_artifacts.create(shot, "run-002")

    path = transcript.bind(shot, "build", label="layer1", run_id=layout.run_id)
    assert path == layout.logs / "transcripts" / "build" / "layer1.jsonl"
    transcript.event("test_event", value=1)
    transcript.unbind()

    assert transcript.find(shot, stage="build", run_id=layout.run_id) == [path]
    assert transcript.run_id_for(path, shot) == layout.run_id
    assert [row["kind"] for row in transcript.read(path)] == ["open", "test_event", "close"]


def test_direct_output_writer_creates_a_structured_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shot = tmp_path / "direct-shot"
    shot.mkdir()
    monkeypatch.delenv(run_artifacts.ENV, raising=False)

    logs = run_artifacts.logs_dir(shot)
    layout = run_artifacts.active(shot)
    assert layout is not None
    assert logs == layout.logs
    assert run_artifacts.renders_dir(shot) == layout.evidence / "renders"
    assert not (shot / "logs").exists()
    assert not (shot / "renders").exists()


def test_direct_cli_invocation_publishes_terminal_status_and_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shot = tmp_path / "direct-command"
    shot.mkdir()
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    monkeypatch.setenv("VFXH_RUN_ID", "direct-001")

    with run_artifacts.invocation(shot, "plan", shot_id="direct-command") as layout:
        (layout.logs / "command.log").write_text("ok\n", encoding="utf-8")

    assert json.loads(layout.status.read_text(encoding="utf-8"))["state"] == "passed"
    summary = json.loads((layout.reports / "summary.json").read_text(encoding="utf-8"))
    assert summary["command"] == "plan"
    assert summary["state"] == "passed"
    assert layout.inventory.is_file()


def test_dirty_plan_exit_publishes_failed_status_and_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shot = tmp_path / "dirty-plan"
    shot.mkdir()
    plan = shot / "plans" / "global.md"
    plan.parent.mkdir()
    plan.write_text("# dirty but preserved\n", encoding="utf-8")
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    monkeypatch.setenv("VFXH_RUN_ID", "direct-dirty")
    result = PlanLoopResult(plan, "budget", 2)

    with (
        pytest.raises(PlanGateFailure, match="2 blocking"),
        run_artifacts.invocation(shot, "plan", shot_id="dirty-plan") as layout,
    ):
        raise PlanGateFailure(result)

    status = json.loads(layout.status.read_text(encoding="utf-8"))
    summary = json.loads((layout.reports / "summary.json").read_text(encoding="utf-8"))
    assert status["state"] == "failed"
    assert status["exit_code"] == 3
    assert "2 blocking" in status["detail"]
    assert summary["state"] == "failed"
    assert summary["exit_code"] == 3


def test_reader_refuses_shot_root_legacy_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shot = tmp_path / "unsupported-output"
    (shot / "renders").mkdir(parents=True)
    (shot / "renders" / "old.png").write_bytes(b"old")
    monkeypatch.delenv(run_artifacts.ENV, raising=False)

    with pytest.raises(FileNotFoundError, match="no structured run"):
        run_artifacts.readable_renders_dir(shot)
