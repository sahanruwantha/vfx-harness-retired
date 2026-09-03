"""`vfx run` continues after a receipt-backed dispatch and stops on every refusal (ADR-0010)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.run_owner_support import owned_run
from tests.unit.test_run_controller import amendment_envelope, write_finding
from vfx_harness.application import run_controller, run_shot
from vfx_harness.observability import run_artifacts

DISPATCHED = run_controller.Dispatched(
    index=1,
    transaction_id="publish_validated_amendment",
    layer_id="2",
    idempotency_key="0" * 64,
    receipt_digest="1" * 64,
    evaluation_digest="2" * 64,
    resume_layer="2",
    ledger_report="runs/r/reports/controller-dispatch-01.json",
)


def test_stop_after_stage_continues_after_a_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    with owned_run(tmp_path, "driver-dispatch", command="run", dispatch_kind="driver") as (layout, lease):
        envelope = amendment_envelope(layout, finding_path=write_finding(tmp_path))
        layout.write_stop_envelope(envelope)
        seen: list[str] = []

        def dispatch(candidate):
            seen.append(candidate.digest)
            layout.stop_envelope.unlink()
            return DISPATCHED

        controller = SimpleNamespace(dispatch=dispatch)
        with pytest.raises(run_shot._ContinueRun) as raised:
            run_shot._stop_after_stage(layout, lease, 7, "layer-2-builder", controller)

        assert raised.value.dispatched is DISPATCHED
        assert seen == [envelope.digest]
        assert not layout.status.exists() or json.loads(layout.status.read_text())["state"] == "running"


def test_stop_after_stage_records_a_refusal_and_stops(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    with owned_run(tmp_path, "driver-refusal", command="run", dispatch_kind="driver") as (layout, lease):
        envelope = amendment_envelope(layout, finding_path=write_finding(tmp_path))
        layout.write_stop_envelope(envelope)
        controller = SimpleNamespace(
            dispatch=lambda _candidate: run_controller.DispatchRefusal(
                "budget_exhausted", "run dispatch cap 1 reached"
            )
        )
        with pytest.raises(SystemExit) as raised:
            run_shot._stop_after_stage(layout, lease, 7, "layer-2-builder", controller)

    assert raised.value.code == 7
    summary = json.loads((layout.reports / "summary.json").read_text())
    assert summary["controller"]["refusal"] == {
        "reason": "budget_exhausted",
        "detail": "run dispatch cap 1 reached",
        "boundary": "layer-2-builder",
    }
    assert summary["controller"]["dispatches"] == []
    assert summary["stop_envelope_digest"] == envelope.digest
    status = json.loads(layout.status.read_text())
    assert status["state"] == "failed" and status["stop_envelope_digest"] == envelope.digest


def test_stop_after_stage_selects_the_failed_adapters_own_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dispatched rematerialization that fails prepared its own envelope; that one is terminal."""
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    with owned_run(tmp_path, "driver-adapter-failed", command="run", dispatch_kind="driver") as (layout, lease):
        finding = write_finding(tmp_path)
        consumed = amendment_envelope(layout, finding_path=finding)
        layout.write_stop_envelope(consumed)
        replacements: list = []

        def dispatch(_candidate):
            # The controller archived the consumed envelope and ran the child, which
            # prepared its own stop (and its own evidence bytes) before exiting non-zero.
            layout.stop_envelope.unlink()
            replacement = amendment_envelope(layout, finding_path=finding, attempt_seed="child-attempt")
            layout.write_stop_envelope(replacement)
            replacements.append(replacement)
            return run_controller.DispatchRefusal("adapter_failed", "rematerialization exited 3")

        with pytest.raises(SystemExit) as raised:
            run_shot._stop_after_stage(layout, lease, 7, "layer-2-builder", SimpleNamespace(dispatch=dispatch))

    assert raised.value.code == 7
    [replacement] = replacements
    status = json.loads(layout.status.read_text())
    assert status["stop_envelope_digest"] == replacement.digest != consumed.digest


def test_driver_replans_and_rebuilds_after_a_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The loop consumes one stop, continues from the replaced layer, and finishes passed."""
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    shot = SimpleNamespace(folder=tmp_path, id="controller-loop-shot")
    monkeypatch.setattr(run_shot, "load_shot", lambda _folder: shot)
    monkeypatch.setattr(run_shot, "preflight_probe", lambda _blender: _ok_preflight())
    chain = [SimpleNamespace(id="1", title="Camera"), SimpleNamespace(id="2", title="Form")]
    monkeypatch.setattr(
        run_shot,
        "_selected_run_layers",
        lambda _shot: ("authority", chain, {layer.id: layer for layer in chain}),
    )
    commands: list[list[str]] = []
    state = {"builder_2_calls": 0, "dispatched": False}
    layouts: list[run_artifacts.RunLayout] = []

    class FakeController:
        def __init__(self, shot_, layout, caps, *, run_stage, blender, python=None):
            layouts.append(layout)
            self.layout = layout
            self.rows: list[dict] = []

        def dispatch(self, envelope):
            state["dispatched"] = True
            self.layout.write_report("controller-consumed-stop-01", envelope.as_dict())
            self.layout.stop_envelope.unlink()
            return DISPATCHED

    monkeypatch.setattr(run_shot, "RunController", FakeController)

    def fake_run(command, *, dry=False, tee=None):
        commands.append([str(item) for item in command])
        text = " ".join(str(item) for item in command)
        if "vfx_harness.agents.builder" in text and "--layer 2" in text:
            state["builder_2_calls"] += 1
            if state["builder_2_calls"] == 1:
                layout = layouts[0]
                layout.write_stop_envelope(amendment_envelope(layout, finding_path=write_finding(tmp_path)))
                return 7
        return 0

    def fake_passed(_shot, layers, _authority):
        passed = {"1"}
        if state["builder_2_calls"] >= 2:
            passed.add("2")
        return passed & set(layers)

    monkeypatch.setattr(run_shot, "_run", fake_run)
    monkeypatch.setattr(run_shot, "_receipt_backed_passed_layers", fake_passed)
    monkeypatch.setattr(
        run_shot,
        "layer_publication",
        SimpleNamespace(
            require_current_layer_publication=lambda *_args: SimpleNamespace(ledger_status="passed"),
            LayerPublicationConflict=run_shot.layer_publication.LayerPublicationConflict,
        ),
    )
    monkeypatch.setattr(sys, "argv", ["vfx run", str(tmp_path), "--from", "2", "--skip-accept", "--skip-render"])

    try:
        run_shot.main()
    except SystemExit as raised:
        assert raised.code in (0, None), raised.code

    assert state["dispatched"]
    builders = [command for command in commands if "vfx_harness.agents.builder" in command]
    assert [command[command.index("--layer") + 1] for command in builders] == ["2", "2"]
    planners = [command for command in commands if "vfx_harness.agents.planner" in command]
    assert [command[command.index("--layer") + 1] for command in planners] == ["2", "2"]
    [layout] = layouts
    status = json.loads(layout.status.read_text())
    assert status["state"] == "passed"
    summary = json.loads((layout.reports / "summary.json").read_text())
    assert summary["controller"]["refusal"] is None


def test_single_pass_flag_disables_the_controller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    shot = SimpleNamespace(folder=tmp_path, id="single-pass-shot")
    monkeypatch.setattr(run_shot, "load_shot", lambda _folder: shot)
    monkeypatch.setattr(run_shot, "preflight_probe", lambda _blender: _ok_preflight())
    chain = [SimpleNamespace(id="1", title="Camera")]
    monkeypatch.setattr(
        run_shot,
        "_selected_run_layers",
        lambda _shot: ("authority", chain, {layer.id: layer for layer in chain}),
    )
    created: list[object] = []

    class ForbiddenController:
        def __init__(self, *args, **kwargs):
            created.append(self)

    monkeypatch.setattr(run_shot, "RunController", ForbiddenController)
    monkeypatch.setattr(run_shot, "_run", lambda command, *, dry=False, tee=None: 0)
    monkeypatch.setattr(run_shot, "_receipt_backed_passed_layers", lambda *_args: {"1"})
    monkeypatch.setattr(sys, "argv", ["vfx run", str(tmp_path), "--single-pass", "--skip-accept", "--skip-render"])

    try:
        run_shot.main()
    except SystemExit as raised:
        assert raised.code in (0, None), raised.code

    assert created == []


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
