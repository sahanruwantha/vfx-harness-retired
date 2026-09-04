"""`vfx run` drafts the global plan when no plan authority is selected (HIR-0186)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from vfx_harness.application import run_shot
from vfx_harness.observability import run_artifacts


def _ok_preflight() -> dict:
    return {
        "ok": True,
        "auth": {"ok": True, "using": "api", "problems": [], "notes": [], "present": [], "decoys": []},
        "configuration": {"ok": True, "problems": []},
        "blender": {"ok": True, "requested": "blender", "resolved": "/usr/bin/blender", "problems": []},
        "blender_confinement": {
            "ok": True,
            "bwrap": "/usr/bin/bwrap",
            "libseccomp": "libseccomp.so.2",
            "worker_blender": "5.2.1 LTS",
            "problems": [],
        },
        "builder_execution_fence": {"ok": True, "mechanism": "sysv-sem-undo+descriptor-flock", "problems": []},
        "plan_consumer_directory": {"ok": True, "mechanism": "fanotify-target-fid+openat2", "problems": []},
    }


def _driver(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, plan_selected: bool):
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    shot = SimpleNamespace(folder=tmp_path, id="global-plan-shot")
    monkeypatch.setattr(run_shot, "load_shot", lambda _folder: shot)
    monkeypatch.setattr(run_shot, "preflight_probe", lambda _blender: _ok_preflight())
    state = {"plan_selected": plan_selected, "planner_rc": 0}
    chain = [SimpleNamespace(id="1", title="Camera")]
    commands: list[list[str]] = []

    def fake_run(command, *, dry=False, tee=None):
        commands.append([str(item) for item in command])
        text = " ".join(str(item) for item in command)
        if "vfx_harness.agents.planner" in text and "--until-clean" in text:
            if state["planner_rc"] == 0:
                state["plan_selected"] = True
            return state["planner_rc"]
        return 0

    def selected_layers(_shot):
        if not state["plan_selected"]:
            raise AssertionError("layers were resolved before any plan authority existed")
        return ("authority", chain, {layer.id: layer for layer in chain})

    monkeypatch.setattr(run_shot, "_needs_global_plan", lambda _shot: not state["plan_selected"])
    monkeypatch.setattr(run_shot, "_selected_run_layers", selected_layers)
    monkeypatch.setattr(run_shot, "_run", fake_run)
    # Sequencing test: the deterministic gate has its own coverage in
    # test_run_shot_plan_gate_stop.py and test_run_shot_orphaned_owner.py.
    monkeypatch.setattr(
        run_shot,
        "_gate_selected_authority",
        lambda _layout, _shot: (None, run_shot.GateResult("shot", [], {})),
    )
    monkeypatch.setattr(run_shot, "_receipt_backed_passed_layers", lambda *_args: {"1"})
    monkeypatch.setattr(sys, "argv", ["vfx run", str(tmp_path), "--skip-accept", "--skip-render", "--single-pass"])
    return state, commands


def test_run_drafts_the_global_plan_before_its_first_layer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _state, commands = _driver(tmp_path, monkeypatch, plan_selected=False)

    try:
        run_shot.main()
    except SystemExit as raised:
        assert raised.code in (0, None), raised.code

    planner = commands[0]
    assert "vfx_harness.agents.planner" in planner and "--until-clean" in planner
    assert "--layer" not in planner
    assert commands[0][commands[0].index("--blender") + 1] == "/usr/bin/blender"
    layout = run_artifacts.latest(tmp_path)
    assert json.loads(layout.status.read_text())["state"] == "passed"


def test_run_with_selected_plan_never_redrafts_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _state, commands = _driver(tmp_path, monkeypatch, plan_selected=True)

    try:
        run_shot.main()
    except SystemExit as raised:
        assert raised.code in (0, None), raised.code

    assert not any("--until-clean" in command for command in commands)


def test_failed_global_plan_is_a_typed_stop_before_any_layer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state, commands = _driver(tmp_path, monkeypatch, plan_selected=False)
    state["planner_rc"] = 2

    with pytest.raises(SystemExit) as raised:
        run_shot.main()

    assert raised.value.code == 2
    assert len(commands) == 1 and "--until-clean" in commands[0]
    layout = run_artifacts.latest(tmp_path)
    status = json.loads(layout.status.read_text())
    assert status["state"] == "failed"
    summary = json.loads((layout.reports / "summary.json").read_text())
    assert summary["exit_code"] == 2
    assert summary["stop_class"] == "harness_defect", "a bare child exit is never inferred authority"


def test_dry_run_without_a_plan_explains_instead_of_drafting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _state, commands = _driver(tmp_path, monkeypatch, plan_selected=False)
    monkeypatch.setattr(
        sys, "argv", ["vfx run", str(tmp_path), "--dry-run", "--skip-accept", "--skip-render"]
    )

    with pytest.raises(SystemExit) as raised:
        run_shot.main()

    assert "drafts the global plan first" in str(raised.value.code)
    assert commands == []
