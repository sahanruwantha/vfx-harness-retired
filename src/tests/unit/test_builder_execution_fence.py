"""Shot-wide live builder execution fence contracts."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import vfx_harness.agents.builder.layer as builder_layer
import vfx_harness.agents.builder.unit_loop as builder_unit_loop
import vfx_harness.agents.planner.generate as planner_generate
from tests.architecture.test_staged_architecture import _unit
from vfx_harness.agents.builder.attempt_guard import UnitAttemptGuard
from vfx_harness.orchestration import unit_state
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionToken,
)
from vfx_harness.orchestration.builder_execution_fence import (
    BUILDER_EXECUTION_FENCE,
    BuilderExecutionFenceActive,
    BuilderExecutionFenceError,
    builder_execution_fence,
    require_builder_execution_lease,
)
from vfx_harness.orchestration.unit_state_claims import (
    claim_ready_unit_for_planning,
)


def _child_environment() -> dict[str, str]:
    repository = Path(__file__).resolve().parents[3]
    source = str(repository / "src")
    environment = os.environ.copy()
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = source if not existing else f"{source}{os.pathsep}{existing}"
    return environment


def _start_holder(shot: Path, *, crash: bool = False) -> subprocess.Popen[str]:
    release = "os._exit(23)" if crash else "None"
    script = f"""
import os
import sys
from vfx_harness.orchestration.builder_execution_fence import builder_execution_fence

with builder_execution_fence(sys.argv[1]):
    print("acquired", flush=True)
    sys.stdin.readline()
    {release}
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(shot)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_child_environment(),
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "acquired"
    return process


def _release_holder(process: subprocess.Popen[str]) -> tuple[str, str]:
    stdout, stderr = process.communicate("release\n", timeout=5)
    return stdout, stderr


def test_fence_is_exclusive_across_threads_and_fails_without_waiting(tmp_path: Path) -> None:
    result: list[BaseException | None] = []

    def contend() -> None:
        try:
            with builder_execution_fence(tmp_path):
                result.append(None)
        except BaseException as exc:
            result.append(exc)

    with builder_execution_fence(tmp_path) as fence_path:
        contender = threading.Thread(target=contend)
        contender.start()
        contender.join(timeout=1)
        assert not contender.is_alive()

    assert len(result) == 1
    assert isinstance(result[0], BuilderExecutionFenceActive)
    message = str(result[0])
    assert str(fence_path) in message
    assert "reviewed recovery" in message
    assert "do not delete or replace" in message


def test_fence_lease_is_shot_bound_and_expires_with_context(tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()

    with builder_execution_fence(tmp_path) as lease:
        require_builder_execution_lease(lease, tmp_path)
        with pytest.raises(BuilderExecutionFenceError, match="belongs to"):
            require_builder_execution_lease(lease, other)

    with pytest.raises(BuilderExecutionFenceError, match="live shot-wide fence lease"):
        require_builder_execution_lease(lease, tmp_path)


def test_already_fenced_builder_refuses_without_live_lease_before_work(
    tmp_path: Path,
    monkeypatch,
) -> None:
    started = False

    async def forbidden_start(*_args, **_kwargs):
        nonlocal started
        started = True
        raise AssertionError("builder work started without a live fence lease")

    monkeypatch.setattr(
        builder_layer,
        "_build_layer_under_execution_fence",
        forbidden_start,
    )
    with pytest.raises(BuilderExecutionFenceError, match="live shot-wide fence lease"):
        asyncio.run(
            builder_layer.build_layer_already_fenced(
                SimpleNamespace(folder=tmp_path),
                SimpleNamespace(),
                SimpleNamespace(),
                fence_lease=None,
            )
        )

    assert started is False


def test_builder_operation_retains_fence_if_owner_context_exits_early(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def scenario() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def blocked_work(*_args, **_kwargs):
            entered.set()
            await release.wait()
            return "finished"

        monkeypatch.setattr(
            builder_layer,
            "_build_layer_under_execution_fence",
            blocked_work,
        )
        owner = builder_execution_fence(tmp_path)
        lease = owner.__enter__()
        task = asyncio.create_task(
            builder_layer.build_layer_already_fenced(
                SimpleNamespace(folder=tmp_path),
                SimpleNamespace(),
                SimpleNamespace(),
                fence_lease=lease,
            )
        )
        await entered.wait()
        owner.__exit__(None, None, None)

        with (
            pytest.raises(BuilderExecutionFenceActive, match="already active"),
            builder_execution_fence(tmp_path),
        ):
            pass

        release.set()
        assert await task == "finished"
        with builder_execution_fence(tmp_path):
            pass

    asyncio.run(scenario())


def test_exported_unit_engine_refuses_without_shot_fence_before_work(
    tmp_path: Path,
) -> None:
    with pytest.raises(BuilderExecutionFenceError, match="live shot-wide fence lease"):
        asyncio.run(
            builder_unit_loop.build_unit(
                SimpleNamespace(folder=tmp_path),
                fence_lease=None,
            )
        )


def test_exported_unit_planner_refuses_invalid_fence_leases_before_spend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = AuthoritySelectionToken(0, None, 0, None)
    unit = _unit("planner-fence")
    units = (unit,)
    plan_hash = "a" * 64
    unit_state.initialize(tmp_path, "1", units, plan_hash=plan_hash)
    claim = claim_ready_unit_for_planning(
        tmp_path,
        "1",
        unit.id,
        units,
        expected_plan_hash=plan_hash,
        eligible_passed=set(),
        run_id="planner-fence",
        selection_token=token,
        reason="fixture planning",
    )
    guard = UnitAttemptGuard.bind(
        tmp_path,
        "1",
        unit,
        units,
        claim,
        expected_plan_hash=plan_hash,
        selected_authority=SimpleNamespace(selection_token=token),
    )
    paid_calls = 0

    async def would_spend(*_args, **_kwargs):
        nonlocal paid_calls
        paid_calls += 1
        raise AssertionError("paid planner started without its exact live fence")

    monkeypatch.setattr(planner_generate, "_generate_layer_plan", would_spend)

    with pytest.raises(BuilderExecutionFenceError, match="live shot-wide fence lease"):
        asyncio.run(
            planner_generate.generate_layer_plan(
                tmp_path,
                "1",
                unit_id=unit.id,
                attempt_guard=guard,
            )
        )

    other = tmp_path / "other-shot"
    other.mkdir()
    with (
        builder_execution_fence(other) as wrong_lease,
        pytest.raises(BuilderExecutionFenceError, match="belongs to"),
    ):
        asyncio.run(
            planner_generate.generate_layer_plan(
                tmp_path,
                "1",
                unit_id=unit.id,
                attempt_guard=guard,
                fence_lease=wrong_lease,
            )
        )

    with builder_execution_fence(tmp_path) as released_lease:
        pass
    with pytest.raises(BuilderExecutionFenceError, match="live shot-wide fence lease"):
        asyncio.run(
            planner_generate.generate_layer_plan(
                tmp_path,
                "1",
                unit_id=unit.id,
                attempt_guard=guard,
                fence_lease=released_lease,
            )
        )

    assert paid_calls == 0


def test_materialize_only_layer_planner_does_not_require_builder_fence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = tmp_path / "plans/layers.json"

    async def materialize(*_args, **kwargs):
        assert kwargs["materialize_only"] is True
        return expected

    monkeypatch.setattr(planner_generate, "_generate_layer_plan", materialize)

    assert (
        asyncio.run(
            planner_generate.generate_layer_plan(
                tmp_path,
                "1",
                materialize_only=True,
            )
        )
        == expected
    )


def test_fence_is_exclusive_across_processes_and_close_releases_it(tmp_path: Path) -> None:
    holder = _start_holder(tmp_path)
    try:
        with (
            pytest.raises(BuilderExecutionFenceActive, match="already active"),
            builder_execution_fence(tmp_path),
        ):
            pass
    finally:
        stdout, stderr = _release_holder(holder)

    assert holder.returncode == 0, (stdout, stderr)
    with builder_execution_fence(tmp_path):
        pass


def test_process_crash_releases_fence_without_replacing_permanent_file(tmp_path: Path) -> None:
    holder = _start_holder(tmp_path, crash=True)
    fence_path = tmp_path / BUILDER_EXECUTION_FENCE
    inode = fence_path.stat().st_ino

    stdout, stderr = _release_holder(holder)

    assert holder.returncode == 23, (stdout, stderr)
    with builder_execution_fence(tmp_path) as reacquired:
        assert reacquired.stat().st_ino == inode
    assert fence_path.stat().st_ino == inode


@pytest.mark.parametrize("substitution", ["fence-parent", "fence-file"])
def test_live_fence_cannot_split_across_descendant_inode_substitution(
    tmp_path: Path,
    substitution: str,
) -> None:
    with builder_execution_fence(tmp_path):
        fence_parent = tmp_path / BUILDER_EXECUTION_FENCE.parent
        if substitution == "fence-parent":
            fence_parent.rename(fence_parent.with_name("builder-execution-retired"))
            fence_parent.mkdir()
        else:
            fence_path = tmp_path / BUILDER_EXECUTION_FENCE
            fence_path.rename(fence_path.with_name("fence.retired"))
            fence_path.touch()

        with (
            pytest.raises(BuilderExecutionFenceActive, match="already active"),
            builder_execution_fence(tmp_path),
        ):
            pass


def test_live_fence_cannot_split_when_shot_root_is_renamed_and_recreated(
    tmp_path: Path,
) -> None:
    shot = tmp_path / "shot"
    shot.mkdir()
    retired = tmp_path / "shot-retired"

    with builder_execution_fence(shot):
        shot.rename(retired)
        shot.mkdir()

        with (
            pytest.raises(BuilderExecutionFenceActive, match="already active"),
            builder_execution_fence(shot),
        ):
            pass

    with builder_execution_fence(shot):
        pass


def test_process_fence_survives_shot_root_rename_and_recreation(
    tmp_path: Path,
) -> None:
    shot = tmp_path / "shot"
    shot.mkdir()
    retired = tmp_path / "shot-retired"
    holder = _start_holder(shot)
    try:
        shot.rename(retired)
        shot.mkdir()

        with (
            pytest.raises(BuilderExecutionFenceActive, match="already active"),
            builder_execution_fence(shot),
        ):
            pass
    finally:
        stdout, stderr = _release_holder(holder)

    assert holder.returncode == 0, (stdout, stderr)
    with builder_execution_fence(shot):
        pass


def test_fence_rejects_a_symlink_in_an_ancestor_component(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    shot = outside / "shot"
    shot.mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(outside, target_is_directory=True)

    with (
        pytest.raises(BuilderExecutionFenceError, match="every ancestor"),
        builder_execution_fence(alias / "shot"),
    ):
        pass


@pytest.mark.parametrize("substitution", ["shot", "state", "fence-parent", "fence"])
def test_fence_rejects_symlinks(tmp_path: Path, substitution: str) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    shot = tmp_path / "shot"
    if substitution == "shot":
        shot.symlink_to(outside, target_is_directory=True)
    else:
        shot.mkdir()
        state = shot / "state"
        fence_parent = state / "builder-execution"
        if substitution == "state":
            state.symlink_to(outside, target_is_directory=True)
        elif substitution == "fence-parent":
            state.mkdir()
            fence_parent.symlink_to(outside, target_is_directory=True)
        else:
            fence_parent.mkdir(parents=True)
            (fence_parent / "fence.lock").symlink_to(outside / "other.lock")

    with (
        pytest.raises(BuilderExecutionFenceError, match=r"real directory|real regular file"),
        builder_execution_fence(shot),
    ):
        pass


@pytest.mark.parametrize("substitution", ["state", "fence-parent", "fence-directory", "fifo"])
def test_fence_rejects_nonregular_storage(tmp_path: Path, substitution: str) -> None:
    state = tmp_path / "state"
    fence_parent = state / "builder-execution"
    fence_path = fence_parent / "fence.lock"
    if substitution == "state":
        state.write_bytes(b"not a directory")
    elif substitution == "fence-parent":
        state.mkdir()
        fence_parent.write_bytes(b"not a directory")
    elif substitution == "fence-directory":
        fence_path.mkdir(parents=True)
    else:
        fence_parent.mkdir(parents=True)
        os.mkfifo(fence_path)

    with (
        pytest.raises(BuilderExecutionFenceError, match=r"real directory|real regular file"),
        builder_execution_fence(tmp_path),
    ):
        pass
