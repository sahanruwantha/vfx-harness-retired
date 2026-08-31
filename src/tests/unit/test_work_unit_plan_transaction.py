"""Revision and concurrency contracts for generated work-unit plan publication."""

from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from vfx_harness.orchestration.work_unit_plan_transaction import (
    WorkUnitPlanTransactionConflict,
    work_unit_plan_transaction,
)


def _paths(root: Path, name: str = "camera.md") -> tuple[Path, Path]:
    target = root / "plans" / name
    authority = target.with_name(target.name + ".authority.json")
    return target, authority


@pytest.mark.parametrize(
    ("prior_plan", "prior_authority"),
    (
        (None, None),
        (b"prior plan\x00bytes", b'{"prior":"authority"}\n'),
    ),
)
def test_owned_failed_attempt_restores_exact_predecessor_pair(
    tmp_path: Path,
    prior_plan: bytes | None,
    prior_authority: bytes | None,
) -> None:
    target, authority = _paths(tmp_path)
    target.parent.mkdir()
    if prior_plan is not None:
        target.write_bytes(prior_plan)
    if prior_authority is not None:
        authority.write_bytes(prior_authority)

    async def exercise() -> None:
        async with work_unit_plan_transaction(target, authority) as transaction:
            target.write_bytes(b"attempt candidate")
            authority.write_bytes(b'{"attempt":"integrity stamp"}\n')
            transaction.claim_current()
            transaction.rollback()

    anyio.run(exercise)
    observed_plan = target.read_bytes() if target.exists() else None
    observed_authority = authority.read_bytes() if authority.exists() else None
    assert observed_plan == prior_plan
    assert observed_authority == prior_authority


@pytest.mark.parametrize("newer_member", ["plan", "authority", "both"])
def test_stale_failed_attempt_never_overwrites_or_deletes_newer_writer(
    tmp_path: Path,
    newer_member: str,
) -> None:
    target, authority = _paths(tmp_path)
    target.parent.mkdir()
    old_plan = b"old accepted plan"
    old_authority = b'{"generation":"old"}\n'
    target.write_bytes(old_plan)
    authority.write_bytes(old_authority)

    async def exercise() -> tuple[bytes, bytes]:
        async with work_unit_plan_transaction(target, authority) as older:
            target.write_bytes(b"older failed candidate")
            authority.write_bytes(b'{"generation":"older-attempt"}\n')
            older.claim_current()

            # Inject a writer that does not honor the cooperative lock.  Rollback is
            # still a byte CAS, so even a one-member in-progress publication is safe.
            if newer_member in {"plan", "both"}:
                target.write_bytes(b"newer accepted plan")
            if newer_member in {"authority", "both"}:
                authority.write_bytes(b'{"generation":"newer"}\n')
            expected = (target.read_bytes(), authority.read_bytes())

            with pytest.raises(WorkUnitPlanTransactionConflict, match="newer writer"):
                older.rollback()
            return expected

    expected_plan, expected_authority = anyio.run(exercise)
    assert target.read_bytes() == expected_plan
    assert authority.read_bytes() == expected_authority
    assert (target.read_bytes(), authority.read_bytes()) != (old_plan, old_authority)


def test_same_target_sessions_serialize_without_blocking_async_event_loop(
    tmp_path: Path,
) -> None:
    target, authority = _paths(tmp_path)
    first_entered = anyio.Event()
    second_started = anyio.Event()
    second_entered = anyio.Event()
    release_first = anyio.Event()
    event_loop_progressed = anyio.Event()
    order: list[str] = []

    async def first() -> None:
        async with work_unit_plan_transaction(target, authority):
            order.append("first-entered")
            first_entered.set()
            await release_first.wait()
            order.append("first-leaving")

    async def second() -> None:
        await first_entered.wait()
        second_started.set()
        async with work_unit_plan_transaction(target, authority):
            order.append("second-entered")
            second_entered.set()

    async def heartbeat() -> None:
        await second_started.wait()
        await anyio.sleep(0.05)
        assert not second_entered.is_set()
        event_loop_progressed.set()
        release_first.set()

    async def exercise() -> None:
        with anyio.fail_after(3):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(first)
                tasks.start_soon(second)
                tasks.start_soon(heartbeat)

    anyio.run(exercise)
    assert event_loop_progressed.is_set()
    assert second_entered.is_set()
    assert order == ["first-entered", "first-leaving", "second-entered"]


def test_different_plan_targets_have_independent_locks(tmp_path: Path) -> None:
    first_target, first_authority = _paths(tmp_path, "camera.md")
    second_target, second_authority = _paths(tmp_path, "geometry.md")
    first_entered = anyio.Event()
    second_entered = anyio.Event()
    release = anyio.Event()

    async def first() -> None:
        async with work_unit_plan_transaction(first_target, first_authority):
            first_entered.set()
            await second_entered.wait()
            release.set()

    async def second() -> None:
        await first_entered.wait()
        async with work_unit_plan_transaction(second_target, second_authority):
            second_entered.set()
            await release.wait()

    async def exercise() -> None:
        with anyio.fail_after(3):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(first)
                tasks.start_soon(second)

    anyio.run(exercise)


def test_transaction_lock_is_permanent_and_reuses_one_inode(tmp_path: Path) -> None:
    target, authority = _paths(tmp_path)
    inodes: list[int] = []

    async def exercise() -> None:
        async with work_unit_plan_transaction(target, authority) as first:
            assert first.lock_path.is_file()
            assert not first.lock_path.is_symlink()
            inodes.append(first.lock_path.stat().st_ino)
        async with work_unit_plan_transaction(target, authority) as second:
            inodes.append(second.lock_path.stat().st_ino)

    anyio.run(exercise)
    assert len(set(inodes)) == 1


def test_cancelled_waiter_closes_its_descriptor_without_releasing_owner(
    tmp_path: Path,
) -> None:
    target, authority = _paths(tmp_path)
    waiter_started = anyio.Event()
    waiter_finished = anyio.Event()
    waiter_entered = False
    waiter_scope: anyio.CancelScope | None = None

    async def waiter() -> None:
        nonlocal waiter_entered, waiter_scope
        with anyio.CancelScope() as scope:
            waiter_scope = scope
            waiter_started.set()
            async with work_unit_plan_transaction(target, authority):
                waiter_entered = True
        waiter_finished.set()

    async def exercise() -> None:
        async with (
            work_unit_plan_transaction(target, authority),
            anyio.create_task_group() as tasks,
        ):
            tasks.start_soon(waiter)
            await waiter_started.wait()
            await anyio.sleep(0.03)
            assert waiter_scope is not None
            waiter_scope.cancel()
            await waiter_finished.wait()
        with anyio.fail_after(1):
            async with work_unit_plan_transaction(target, authority):
                pass

    anyio.run(exercise)
    assert waiter_entered is False
