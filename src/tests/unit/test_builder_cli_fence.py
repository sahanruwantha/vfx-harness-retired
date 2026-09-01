from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest

import vfx_harness.agents.builder.cli as builder_cli
from vfx_harness.orchestration.builder_execution_fence import (
    BuilderExecutionFenceError,
)


def test_cli_owns_one_fence_before_run_root_and_blender_start(
    tmp_path: Path,
    monkeypatch,
) -> None:
    events: list[str] = []
    active_fences = 0
    invocation_active = False
    milestone = object()
    layer = SimpleNamespace(
        id="1",
        title="fixture",
        judges=((1, "refs/reference.png"),),
        script="build/units/01/hero.py",
        as_milestone=lambda: milestone,
    )
    shot = SimpleNamespace(folder=tmp_path, id="cli-fence-fixture")
    request = builder_cli.PreparedBuildRequest(
        shot=shot,
        layer=layer,
        selected_authority=SimpleNamespace(),
    )

    @contextmanager
    def fence(folder):
        nonlocal active_fences
        assert folder == tmp_path
        assert active_fences == 0, "CLI attempted a nested non-reentrant shot fence"
        active_fences = 1
        events.append("fence-enter")
        try:
            yield tmp_path / "state/builder-execution/fence.lock"
        finally:
            events.append("fence-exit")
            active_fences = 0

    @contextmanager
    def invocation(folder, command, **_kwargs):
        nonlocal invocation_active
        assert folder == tmp_path
        assert command == "build"
        assert active_fences == 1
        invocation_active = True
        events.append("run-root")
        try:
            yield SimpleNamespace()
        finally:
            events.append("run-close")
            invocation_active = False

    class _Session:
        def __init__(self, **_kwargs) -> None:
            assert active_fences == 1
            assert invocation_active

        def start(self):
            events.append("session-start")
            return self

        def close(self) -> None:
            events.append("session-close")

    class _Ledger:
        path = tmp_path / "shot.json"

        def status(self, observed_milestone):
            assert observed_milestone is milestone
            return "passed"

    async def build_layer_already_fenced(*_args, **kwargs):
        assert active_fences == 1
        assert invocation_active
        assert kwargs["fence_lease"] == tmp_path / "state/builder-execution/fence.lock"
        events.append("build-already-fenced")
        return _Ledger()

    def prepare(*_args, **_kwargs):
        assert active_fences == 0
        assert not invocation_active
        events.append("prepare")
        return request

    monkeypatch.setattr(builder_cli, "load_environment", lambda: None)
    monkeypatch.setattr(builder_cli, "_prepare_build_request", prepare)
    monkeypatch.setattr(builder_cli, "builder_execution_fence", fence)
    monkeypatch.setattr(
        builder_cli,
        "require_builder_execution_lease",
        lambda lease, folder: (
            events.append("lease-check")
            if lease == tmp_path / "state/builder-execution/fence.lock"
            and folder == tmp_path
            else pytest.fail("CLI passed the wrong live fence lease")
        ),
    )
    monkeypatch.setattr(builder_cli.run_artifacts, "invocation", invocation)
    monkeypatch.setattr(
        builder_cli,
        "ensure_construction_read_namespace",
        lambda _folder: events.append("construction-namespace"),
    )
    monkeypatch.setattr(builder_cli, "BlenderSession", _Session)
    monkeypatch.setattr(
        builder_cli,
        "builder_package",
        lambda: SimpleNamespace(
            build_layer_already_fenced=build_layer_already_fenced,
        ),
    )
    monkeypatch.setattr(builder_cli, "load_unit_state", lambda *_args: {"units": {}})
    monkeypatch.setattr(
        builder_cli,
        "clear_layer_context",
        lambda _shot: events.append("clear-context"),
    )
    monkeypatch.setattr(builder_cli, "builder_model", lambda: "fixture-builder")
    monkeypatch.setattr(builder_cli, "critic_model", lambda: "fixture-critic")
    monkeypatch.setattr(
        sys,
        "argv",
        ["vfx-build", str(tmp_path), "--layer", "1"],
    )

    builder_cli.main()

    assert events == [
        "prepare",
        "fence-enter",
        "run-root",
        "lease-check",
        "construction-namespace",
        "session-start",
        "build-already-fenced",
        "session-close",
        "clear-context",
        "run-close",
        "fence-exit",
    ]


def test_already_fenced_cli_entry_refuses_before_shared_mutation_or_blender(
    tmp_path: Path,
    monkeypatch,
) -> None:
    touched: list[str] = []
    request = builder_cli.PreparedBuildRequest(
        shot=SimpleNamespace(folder=tmp_path, id="missing-fence"),
        layer=SimpleNamespace(),
        selected_authority=SimpleNamespace(),
    )
    monkeypatch.setattr(
        builder_cli,
        "ensure_construction_read_namespace",
        lambda *_args: touched.append("construction"),
    )
    monkeypatch.setattr(
        builder_cli,
        "BlenderSession",
        lambda **_kwargs: touched.append("blender") or pytest.fail(
            "Blender started without a live fence"
        ),
    )

    async def run() -> None:
        with pytest.raises(BuilderExecutionFenceError, match="live shot-wide fence lease"):
            await builder_cli._run_already_fenced(
                request,
                rounds=1,
                blender="blender",
                fence_lease=None,
            )

    anyio.run(run)
    assert touched == []
