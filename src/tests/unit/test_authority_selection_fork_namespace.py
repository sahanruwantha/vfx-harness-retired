"""Fork and namespace regressions for the permanent shot-authority lease."""

from __future__ import annotations

import errno
import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
from collections.abc import Callable
from contextlib import contextmanager, suppress
from pathlib import Path
from threading import Event, Thread, get_ident
from typing import Any

import pytest

import vfx_harness.observability.run_owner_fork_guard as run_owner_fork_guard
import vfx_harness.observability.run_owner_fork_registry as run_owner_fork_registry
from vfx_harness.orchestration import authority_selection_process_registry as registry
from vfx_harness.orchestration import authority_selection_transaction as transaction
from vfx_harness.orchestration import builder_execution_fence
from vfx_harness.orchestration.authority_selection_transaction import (
    AUTHORITY_SELECTION_LOCK,
    AuthoritySelectionConflict,
    authority_selection_lock,
)
from vfx_harness.orchestration.shot_authority_capture import (
    current_shot_authority_capture,
    current_shot_authority_writer,
    ordered_authority_inner_lock,
    shot_authority_capture,
    shot_authority_writer_fence,
)

pytestmark = pytest.mark.skipif(not hasattr(os, "fork"), reason="requires os.fork")


def _in_fork(function: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    read_descriptor, write_descriptor = os.pipe()
    child = os.fork()
    if child == 0:  # pragma: no branch - the parent validates the child result
        os.close(read_descriptor)
        try:
            result = function()
            payload = json.dumps({"result": result}).encode()
            status = 0
        except BaseException as exc:
            payload = json.dumps({"error": f"{type(exc).__name__}: {exc}"}).encode()
            status = 1
        try:
            os.write(write_descriptor, payload)
        finally:
            os.close(write_descriptor)
        os._exit(status)

    os.close(write_descriptor)
    chunks: list[bytes] = []
    while True:
        chunk = os.read(read_descriptor, 65536)
        if not chunk:
            break
        chunks.append(chunk)
    os.close(read_descriptor)
    _pid, wait_status = os.waitpid(child, 0)
    decoded = json.loads(b"".join(chunks))
    assert os.waitstatus_to_exitcode(wait_status) == 0, decoded
    assert "error" not in decoded, decoded
    return decoded["result"]


def _descriptors_are_closed(descriptors: tuple[int, ...]) -> bool:
    for descriptor in descriptors:
        try:
            os.fstat(descriptor)
        except OSError as exc:
            if exc.errno != errno.EBADF:
                raise
        else:
            return False
    return True


def _current_binding(shot: Path) -> transaction._HeldAuthoritySelectionLock:
    key = str(shot / AUTHORITY_SELECTION_LOCK)
    return transaction._held_selection_locks()[key]


def _acquire_in_fork_with_alarm(shot: Path) -> dict[str, Any]:
    def acquire() -> dict[str, Any]:
        signal.alarm(3)
        with authority_selection_lock(shot, exclusive=True):
            signal.alarm(0)
            return {"acquired": True}

    return _in_fork(acquire)


def test_fork_child_cannot_reuse_same_thread_binding_or_capability(
    tmp_path: Path,
) -> None:
    with shot_authority_capture(tmp_path) as capability:
        binding = _current_binding(tmp_path)
        descriptors = binding.registration.descriptors

        def child_probe() -> dict[str, Any]:
            refusals: list[bool] = []
            for action in (
                lambda: current_shot_authority_capture(tmp_path),
                lambda: current_shot_authority_writer(tmp_path),
                lambda: ordered_authority_inner_lock(
                    capability,
                    "plan_resolution",
                ).__enter__(),
                lambda: authority_selection_lock(
                    tmp_path,
                    exclusive=False,
                ).__enter__(),
            ):
                try:
                    action()
                except AuthoritySelectionConflict:
                    refusals.append(True)
                else:
                    refusals.append(False)
            return {
                "descriptors_closed": _descriptors_are_closed(descriptors),
                "refusals": refusals,
            }

        result = _in_fork(child_probe)

    assert result == {"descriptors_closed": True, "refusals": [True] * 4}


def test_same_thread_fork_during_registered_acquisition_does_not_deadlock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = transaction._open_directory_parts
    injected = False

    def fork_before_open(*args, **kwargs):
        nonlocal injected
        if not injected:
            injected = True
            child = os.fork()
            if child == 0:
                os._exit(0)
            _pid, status = os.waitpid(child, 0)
            assert os.waitstatus_to_exitcode(status) == 0
        return original(*args, **kwargs)

    monkeypatch.setattr(transaction, "_open_directory_parts", fork_before_open)
    with authority_selection_lock(tmp_path, exclusive=False):
        pass
    assert injected


def test_different_shot_cleanup_and_acquisition_share_one_lock_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "cleanup-shot"
    second = tmp_path / "acquisition-shot"
    first.mkdir()
    second.mkdir()
    cleanup_entered = Event()
    release_cleanup = Event()
    acquisition_done = Event()
    failures: list[BaseException] = []
    original_active_close = registry.active_descriptor_close

    @contextmanager
    def pause_active_close(token, descriptors):
        with original_active_close(token, descriptors):
            cleanup_entered.set()
            if not release_cleanup.wait(timeout=5):
                raise AssertionError("cleanup release timed out")
            yield

    monkeypatch.setattr(registry, "active_descriptor_close", pause_active_close)

    def cleanup() -> None:
        try:
            with authority_selection_lock(first, exclusive=False):
                pass
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    def acquire() -> None:
        try:
            with authority_selection_lock(second, exclusive=False):
                acquisition_done.set()
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    cleanup_thread = Thread(target=cleanup)
    cleanup_thread.start()
    assert cleanup_entered.wait(timeout=5)
    acquisition_thread = Thread(target=acquire)
    acquisition_thread.start()
    assert not acquisition_done.wait(timeout=0.1)
    release_cleanup.set()
    cleanup_thread.join(timeout=5)
    acquisition_thread.join(timeout=5)

    assert not cleanup_thread.is_alive()
    assert not acquisition_thread.is_alive()
    assert acquisition_done.is_set()
    assert failures == []


@pytest.mark.parametrize("failure", ["unlock", "close"])
def test_cleanup_failure_neutralizes_lock_before_registry_retirement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    original_flock = transaction.fcntl.lockf
    original_close = transaction.os.close
    injected = False
    lock_descriptor: int | None = None

    def fail_unlock_once(descriptor: int, operation: int) -> None:
        nonlocal injected
        if failure == "unlock" and operation == transaction.fcntl.LOCK_UN and not injected:
            injected = True
            raise OSError("injected unlock failure")
        original_flock(descriptor, operation)

    def fail_close_once(descriptor: int) -> None:
        nonlocal injected
        if failure == "close" and descriptor == lock_descriptor and not injected:
            injected = True
            raise OSError("injected close failure")
        original_close(descriptor)

    monkeypatch.setattr(transaction.fcntl, "lockf", fail_unlock_once)
    monkeypatch.setattr(transaction.os, "close", fail_close_once)
    with (
        pytest.raises(registry.AuthoritySelectionCleanupFailure) as captured,
        authority_selection_lock(tmp_path, exclusive=True),
    ):
        lock_descriptor = _current_binding(tmp_path).lock_descriptor

    assert injected
    assert captured.value.retained == ()
    assert any(
        f"injected {failure} failure" in str(error)
        for error in captured.value.errors
    )
    assert registry._REGISTRY == {}
    probe = os.open(tmp_path / AUTHORITY_SELECTION_LOCK, os.O_RDWR)
    try:
        original_flock(probe, transaction.fcntl.LOCK_EX | transaction.fcntl.LOCK_NB)
        original_flock(probe, transaction.fcntl.LOCK_UN)
    finally:
        original_close(probe)


def test_fork_child_closes_selection_descriptors_held_by_another_thread(
    tmp_path: Path,
) -> None:
    ready = Event()
    release = Event()
    contender_started = Event()
    contender_acquired = Event()
    descriptors: list[int] = []
    failures: list[BaseException] = []

    def hold_selection() -> None:
        try:
            with authority_selection_lock(tmp_path, exclusive=False):
                descriptors.extend(_current_binding(tmp_path).registration.descriptors)
                ready.set()
                if not release.wait(timeout=5):
                    raise AssertionError("selection-holder release timed out")
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    holder = Thread(target=hold_selection)
    holder.start()
    assert ready.wait(timeout=5)

    def contend() -> None:
        try:
            contender_started.set()
            with authority_selection_lock(tmp_path, exclusive=True):
                contender_acquired.set()
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    try:
        result = _in_fork(lambda: {"descriptors_closed": _descriptors_are_closed(tuple(descriptors))})
        contender = Thread(target=contend)
        contender.start()
        assert contender_started.wait(timeout=5)
        assert not contender_acquired.wait(timeout=0.1)
    finally:
        release.set()
        holder.join(timeout=5)
    contender.join(timeout=5)

    assert not holder.is_alive()
    assert not contender.is_alive()
    assert contender_acquired.is_set()
    assert failures == []
    assert result == {"descriptors_closed": True}


def test_fork_child_close_without_effect_never_leaves_exact_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_pid = os.getpid()
    original_close = registry.os.close
    failed = False

    with authority_selection_lock(tmp_path, exclusive=True):
        registration = _current_binding(tmp_path).registration
        target = registration.descriptor_identities[-1]

        def fail_child_neutral_close(descriptor: int) -> None:
            nonlocal failed
            if (
                os.getpid() != parent_pid
                and descriptor == target.descriptor
                and not failed
            ):
                failed = True
                raise OSError("injected child neutral close without effect")
            original_close(descriptor)

        monkeypatch.setattr(registry.os, "close", fail_child_neutral_close)
        result = _in_fork(
            lambda: {
                "exact_authority_released": not target.is_current(),
                "registry_empty": not registry._REGISTRY,
            }
        )

    assert result == {
        "exact_authority_released": True,
        "registry_empty": True,
    }


def test_fork_child_exits_if_authority_dup2_release_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_pid = os.getpid()
    original_dup2 = registry.os.dup2

    with authority_selection_lock(tmp_path, exclusive=True):
        target = _current_binding(tmp_path).registration.descriptors[-1]

        def fail_child_authority_dup2(
            source: int,
            destination: int,
            *,
            inheritable: bool = True,
        ) -> int:
            if os.getpid() != parent_pid and destination == target:
                raise OSError("injected child authority dup2 failure")
            return original_dup2(source, destination, inheritable=inheritable)

        monkeypatch.setattr(registry.os, "dup2", fail_child_authority_dup2)
        child = os.fork()
        if child == 0:  # pragma: no cover - run-owner terminal handler exits first
            os._exit(0)
        waited, status = os.waitpid(child, 0)

    assert waited == child
    assert os.waitstatus_to_exitcode(status) == (
        run_owner_fork_registry.CHILD_AUTHORITY_CLEANUP_EXIT
    )


def test_fork_child_exits_if_authority_release_cannot_be_observed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_pid = os.getpid()
    original_fstat = registry.os.fstat

    with authority_selection_lock(tmp_path, exclusive=True):
        target = _current_binding(tmp_path).registration.descriptors[-1]

        def fail_child_authority_fstat(descriptor: int) -> os.stat_result:
            if os.getpid() != parent_pid and descriptor == target:
                raise OSError(errno.EIO, "injected child authority fstat failure")
            return original_fstat(descriptor)

        monkeypatch.setattr(registry.os, "fstat", fail_child_authority_fstat)
        child = os.fork()
        if child == 0:  # pragma: no cover - registry terminal handler exits first
            os._exit(0)
        waited, status = os.waitpid(child, 0)

    assert waited == child
    assert os.waitstatus_to_exitcode(status) == (
        run_owner_fork_registry.CHILD_AUTHORITY_CLEANUP_EXIT
    )


def test_fork_child_context_unwind_cannot_close_reused_descriptor(
    tmp_path: Path,
) -> None:
    manager = authority_selection_lock(tmp_path, exclusive=True)
    manager.__enter__()
    descriptors = _current_binding(tmp_path).registration.descriptors
    try:

        def child_probe() -> dict[str, Any]:
            opened: list[int] = []
            reused: int | None = None
            while reused is None and len(opened) < 64:
                descriptor = os.open("/dev/null", os.O_RDONLY)
                opened.append(descriptor)
                if descriptor in descriptors:
                    reused = descriptor
            refused = False
            try:
                manager.__exit__(None, None, None)
            except AuthoritySelectionConflict:
                refused = True
            alive = reused is not None
            if reused is not None:
                try:
                    os.fstat(reused)
                except OSError:
                    alive = False
            for descriptor in opened:
                with suppress(OSError):
                    os.close(descriptor)
            return {"refused": refused, "reused_descriptor_alive": alive}

        result = _in_fork(child_probe)
    finally:
        manager.__exit__(None, None, None)

    assert result == {"refused": True, "reused_descriptor_alive": True}


def test_root_replacement_with_transplanted_state_subtree_refuses_exit(
    tmp_path: Path,
) -> None:
    shot = tmp_path / "shot"
    retired = tmp_path / "retired-shot"
    shot.mkdir()

    with (
        pytest.raises(
            AuthoritySelectionConflict,
            match="lock path changed during lock exit",
        ),
        authority_selection_lock(shot, exclusive=False),
    ):
        shot.rename(retired)
        shot.mkdir()
        shutil.move(str(retired / "state"), str(shot / "state"))


def test_root_replacement_between_mutex_identity_and_retained_open_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shot = tmp_path / "shot-acquisition-race"
    retired = tmp_path / "retired-shot-acquisition-race"
    shot.mkdir()
    original_open_parts = transaction._open_directory_parts
    injected = False

    def replace_before_retained_open(*args, **kwargs):
        nonlocal injected
        if not injected and args[1] == ():
            injected = True
            shot.rename(retired)
            shot.mkdir()
        return original_open_parts(*args, **kwargs)

    monkeypatch.setattr(
        transaction,
        "_open_directory_parts",
        replace_before_retained_open,
    )
    with (
        pytest.raises(
            AuthoritySelectionConflict,
            match="shot root changed during lock acquisition",
        ),
        authority_selection_lock(shot, exclusive=False),
    ):
        pass

    assert injected


def test_waiter_refuses_root_replaced_while_blocked_on_stale_inode_mutex(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shot = tmp_path / "shot-waiter-race"
    retired = tmp_path / "retired-shot-waiter-race"
    shot.mkdir()
    holder_entered = Event()
    release_holder = Event()
    waiter_blocking = Event()
    failures: dict[str, BaseException] = {}
    waiter_thread_id: int | None = None
    original_mutex = transaction.shot_process_mutex

    @contextmanager
    def observe_mutex_wait(key):
        if get_ident() == waiter_thread_id:
            waiter_blocking.set()
        with original_mutex(key):
            yield

    monkeypatch.setattr(
        transaction,
        "shot_process_mutex",
        observe_mutex_wait,
    )

    def hold() -> None:
        try:
            with authority_selection_lock(shot, exclusive=True):
                holder_entered.set()
                if not release_holder.wait(timeout=5):
                    raise AssertionError("holder release timed out")
        except BaseException as exc:  # expected after root replacement
            failures["holder"] = exc

    def wait_on_stale_identity() -> None:
        nonlocal waiter_thread_id
        waiter_thread_id = get_ident()
        try:
            with authority_selection_lock(shot, exclusive=True):
                raise AssertionError("stale-identity waiter entered")
        except BaseException as exc:  # expected after root replacement
            failures["waiter"] = exc

    holder = Thread(target=hold)
    waiter = Thread(target=wait_on_stale_identity)
    holder.start()
    assert holder_entered.wait(timeout=5)
    waiter.start()
    assert waiter_blocking.wait(timeout=5)
    shot.rename(retired)
    shot.mkdir()
    release_holder.set()
    holder.join(timeout=5)
    waiter.join(timeout=5)

    assert not holder.is_alive()
    assert not waiter.is_alive()
    assert isinstance(failures.get("holder"), AuthoritySelectionConflict)
    waiter_failure = failures.get("waiter")
    assert isinstance(waiter_failure, AuthoritySelectionConflict)
    assert "shot root changed during lock acquisition" in str(waiter_failure)


def test_transplanted_state_cannot_reuse_active_writer_capability(
    tmp_path: Path,
) -> None:
    shot = tmp_path / "capability-shot"
    retired = tmp_path / "retired-capability-shot"
    shot.mkdir()

    with (
        pytest.raises(
            AuthoritySelectionConflict,
            match="lock path changed during lock exit",
        ),
        shot_authority_writer_fence(shot) as capability,
    ):
        shot.rename(retired)
        shot.mkdir()
        shutil.move(str(retired / "state"), str(shot / "state"))
        with (
            pytest.raises(AuthoritySelectionConflict, match="lock path changed"),
            ordered_authority_inner_lock(capability, "plan_resolution"),
        ):
            pass


def test_nested_selection_lease_refuses_a_different_shot(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    with (
        authority_selection_lock(first, exclusive=False),
        pytest.raises(AuthoritySelectionConflict, match="same canonical shot"),
        authority_selection_lock(second, exclusive=False),
    ):
        pass


def test_capability_cannot_cross_threads(tmp_path: Path) -> None:
    failures: list[BaseException] = []
    with shot_authority_capture(tmp_path) as capability:

        def misuse() -> None:
            for action in (
                lambda: current_shot_authority_writer(tmp_path),
                lambda: ordered_authority_inner_lock(
                    capability,
                    "plan_resolution",
                ).__enter__(),
            ):
                try:
                    action()
                except BaseException as exc:  # pragma: no cover - asserted below
                    failures.append(exc)

        thread = Thread(target=misuse)
        thread.start()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert len(failures) == 2
    assert all(isinstance(exc, AuthoritySelectionConflict) for exc in failures)


def test_foreign_thread_context_exit_cleans_before_reporting_affinity(
    tmp_path: Path,
) -> None:
    manager = authority_selection_lock(tmp_path, exclusive=True)
    manager.__enter__()
    failures: list[BaseException] = []

    def foreign_exit() -> None:
        try:
            manager.__exit__(None, None, None)
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    thread = Thread(target=foreign_exit)
    thread.start()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], AuthoritySelectionConflict)
    assert "another process or thread" in str(failures[0])
    assert transaction._held_selection_locks() == {}
    assert registry._REGISTRY == {}
    assert _acquire_in_fork_with_alarm(tmp_path) == {"acquired": True}


def test_fork_after_selection_open_before_track_cannot_pin_parent_lock(
    tmp_path: Path,
) -> None:
    """A child retaining the pre-track fd cannot retain a POSIX process lock."""

    report_read, report_write = os.pipe()
    keepalive_read, keepalive_write = os.pipe()
    owner = os.fork()
    if owner == 0:  # pragma: no branch - parent asserts the complete transcript
        os.close(report_read)
        os.close(keepalive_write)
        original_open = os.open
        injected = False

        def fork_after_real_lock_open(*args, **kwargs):
            nonlocal injected
            descriptor = original_open(*args, **kwargs)
            if args and args[0] == AUTHORITY_SELECTION_LOCK.name and not injected:
                injected = True
                inheritor = os.fork()
                if inheritor == 0:
                    os.close(report_write)
                    try:
                        while os.read(keepalive_read, 1):
                            pass
                    finally:
                        os.close(keepalive_read)
                    os._exit(0)
                os.close(keepalive_read)
                os.write(report_write, f"inheritor={inheritor}\n".encode())
            return descriptor

        transaction.os.open = fork_after_real_lock_open
        try:
            with authority_selection_lock(tmp_path, exclusive=True):
                os.write(report_write, b"entered\n")
                # Bypass lexical cleanup: only process-death semantics may release
                # the record lock while the pre-track inheritor remains alive.
                os._exit(0)
        except BaseException as exc:
            os.write(report_write, f"error={type(exc).__name__}:{exc}\n".encode())
            os._exit(1)

    os.close(report_write)
    os.close(keepalive_read)
    inheritor_pid: int | None = None
    try:
        _pid, status = os.waitpid(owner, 0)
        report = os.read(report_read, 65536).decode().splitlines()
        assert os.waitstatus_to_exitcode(status) == 0, report
        assert "entered" in report
        inheritor_rows = [row for row in report if row.startswith("inheritor=")]
        assert len(inheritor_rows) == 1
        inheritor_pid = int(inheritor_rows[0].partition("=")[2])
        os.kill(inheritor_pid, 0)

        # The contender completes before keepalive_write is closed, proving that
        # the still-live child cannot pin the dead parent's record lock.
        assert _acquire_in_fork_with_alarm(tmp_path) == {"acquired": True}
        os.kill(inheritor_pid, 0)
    finally:
        os.close(report_read)
        os.close(keepalive_write)
        if inheritor_pid is not None:
            with suppress(OSError):
                os.kill(inheritor_pid, signal.SIGTERM)


@pytest.mark.parametrize("after_commit", [False, True])
def test_handoff_failure_retires_registration_before_fd_reuse_and_fork(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    after_commit: bool,
) -> None:
    registrations: list[registry.DescriptorLeaseRegistration] = []
    original_register = registry.register_descriptors_locked
    original_handoff = run_owner_fork_guard.ForkProtectedAcquisition.handoff

    def record_registration(*args, **kwargs):
        registration = original_register(*args, **kwargs)
        registrations.append(registration)
        return registration

    def fail_handoff(self, descriptors):
        if after_commit:
            original_handoff(self, descriptors)
        raise RuntimeError("injected handoff failure")

    monkeypatch.setattr(
        registry,
        "register_descriptors_locked",
        record_registration,
    )
    monkeypatch.setattr(
        run_owner_fork_guard.ForkProtectedAcquisition,
        "handoff",
        fail_handoff,
    )
    with (
        pytest.raises(RuntimeError, match="injected handoff failure"),
        authority_selection_lock(tmp_path, exclusive=True),
    ):
        pass

    assert len(registrations) == 1
    retired_descriptors = registrations[0].descriptors
    assert registry._REGISTRY == {}
    assert run_owner_fork_registry.PENDING_DESCRIPTORS == {}
    assert run_owner_fork_registry.ACTIVE_DESCRIPTORS == {}

    opened: list[int] = []
    reused: int | None = None
    try:
        while reused is None and len(opened) < 64:
            descriptor = os.open(os.devnull, os.O_RDONLY)
            opened.append(descriptor)
            if descriptor in retired_descriptors:
                reused = descriptor
        assert reused is not None
        assert _in_fork(lambda: {"reused_descriptor_alive": os.fstat(reused) is not None}) == {
            "reused_descriptor_alive": True
        }
    finally:
        for descriptor in opened:
            with suppress(OSError):
                os.close(descriptor)


def test_same_shot_threads_serialize_complete_selection_leases(tmp_path: Path) -> None:
    first_entered = Event()
    release_first = Event()
    second_entered = Event()
    failures: list[BaseException] = []

    def first() -> None:
        try:
            with authority_selection_lock(tmp_path, exclusive=False):
                first_entered.set()
                if not release_first.wait(timeout=5):
                    raise AssertionError("first lease release timed out")
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    def second() -> None:
        try:
            with authority_selection_lock(tmp_path, exclusive=True):
                second_entered.set()
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    first_thread = Thread(target=first)
    second_thread = Thread(target=second)
    first_thread.start()
    assert first_entered.wait(timeout=5)
    second_thread.start()
    assert not second_entered.wait(timeout=0.1)
    release_first.set()
    first_thread.join(timeout=5)
    second_thread.join(timeout=5)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert second_entered.is_set()
    assert failures == []


def test_distinct_shots_can_hold_selection_leases_concurrently(tmp_path: Path) -> None:
    first_shot = tmp_path / "first"
    second_shot = tmp_path / "second"
    first_shot.mkdir()
    second_shot.mkdir()
    first_entered = Event()
    release_first = Event()
    second_entered = Event()
    release_second = Event()
    failures: list[BaseException] = []

    def hold(shot: Path, entered: Event, release: Event) -> None:
        try:
            with authority_selection_lock(shot, exclusive=True):
                entered.set()
                if not release.wait(timeout=5):
                    raise AssertionError("selection lease release timed out")
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    first = Thread(target=hold, args=(first_shot, first_entered, release_first))
    second = Thread(target=hold, args=(second_shot, second_entered, release_second))
    first.start()
    assert first_entered.wait(timeout=5)
    second.start()
    assert second_entered.wait(timeout=5)
    release_second.set()
    release_first.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert failures == []


def test_sigkill_releases_record_lock_and_live_identity(tmp_path: Path) -> None:
    ready_read, ready_write = os.pipe()
    owner = os.fork()
    if owner == 0:  # pragma: no branch - parent asserts process status
        os.close(ready_read)
        with authority_selection_lock(tmp_path, exclusive=True):
            os.write(ready_write, b"ready")
            signal.pause()
        os._exit(2)

    os.close(ready_write)
    try:
        assert os.read(ready_read, 5) == b"ready"
        os.kill(owner, signal.SIGKILL)
        _pid, status = os.waitpid(owner, 0)
        assert os.waitstatus_to_exitcode(status) == -signal.SIGKILL
        assert _acquire_in_fork_with_alarm(tmp_path) == {"acquired": True}
    finally:
        os.close(ready_read)
        with suppress(OSError, ChildProcessError):
            os.kill(owner, signal.SIGKILL)
            os.waitpid(owner, 0)


def test_unexpected_lock_file_close_cannot_admit_second_process(
    tmp_path: Path,
) -> None:
    results: list[dict[str, Any]] = []
    failures: list[BaseException] = []

    with authority_selection_lock(tmp_path, exclusive=True):
        unrelated = os.open(tmp_path / AUTHORITY_SELECTION_LOCK, os.O_RDONLY)
        os.close(unrelated)

        def contend_from_clean_thread() -> None:
            try:

                def contend() -> dict[str, Any]:
                    signal.alarm(3)
                    try:
                        with authority_selection_lock(tmp_path, exclusive=True):
                            return {"entered": True}
                    except AuthoritySelectionConflict as exc:
                        return {
                            "entered": False,
                            "live_identity_refused": "already held" in str(exc),
                        }
                    finally:
                        signal.alarm(0)

                results.append(_in_fork(contend))
            except BaseException as exc:  # pragma: no cover - asserted below
                failures.append(exc)

        contender = Thread(target=contend_from_clean_thread)
        contender.start()
        contender.join(timeout=5)
        assert not contender.is_alive()

    assert failures == []
    assert results == [{"entered": False, "live_identity_refused": True}]
    assert _acquire_in_fork_with_alarm(tmp_path) == {"acquired": True}


def test_legacy_flock_and_live_identity_bridge_refuses_lockf_client(
    tmp_path: Path,
) -> None:
    # Establish the permanent file before the legacy holder starts.
    with authority_selection_lock(tmp_path, exclusive=True):
        pass

    ready_read, ready_write = os.pipe()
    release_read, release_write = os.pipe()
    legacy = os.fork()
    if legacy == 0:  # pragma: no branch - parent asserts the result
        os.close(ready_read)
        os.close(release_write)
        descriptor = os.open(tmp_path / AUTHORITY_SELECTION_LOCK, os.O_RDWR)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        lease = builder_execution_fence._claim_live_identity(
            tmp_path / AUTHORITY_SELECTION_LOCK,
            tmp_path / AUTHORITY_SELECTION_LOCK,
        )
        os.write(ready_write, b"ready")
        os.read(release_read, 1)
        lease.release()
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
        os._exit(0)

    os.close(ready_write)
    os.close(release_read)
    try:
        assert os.read(ready_read, 5) == b"ready"

        def new_client() -> dict[str, Any]:
            signal.alarm(3)
            try:
                with authority_selection_lock(tmp_path, exclusive=True):
                    return {"entered": True}
            except AuthoritySelectionConflict as exc:
                return {
                    "entered": False,
                    "live_identity_refused": "already held" in str(exc),
                }
            finally:
                signal.alarm(0)

        assert _in_fork(new_client) == {
            "entered": False,
            "live_identity_refused": True,
        }
    finally:
        os.close(ready_read)
        os.close(release_write)
        _pid, status = os.waitpid(legacy, 0)
        assert os.waitstatus_to_exitcode(status) == 0

    assert _acquire_in_fork_with_alarm(tmp_path) == {"acquired": True}


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX root spelling")
def test_double_slash_alias_reuses_one_canonical_capability(tmp_path: Path) -> None:
    alias = f"//{str(tmp_path).lstrip('/')}"

    assert registry.canonical_authority_shot_path(alias) == tmp_path
    with (
        shot_authority_capture(tmp_path) as outer,
        shot_authority_writer_fence(alias) as nested,
    ):
        assert nested is outer


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX root spelling")
def test_physical_aliases_share_the_same_inode_process_mutex(tmp_path: Path) -> None:
    alias = Path(f"//{str(tmp_path).lstrip('/')}")
    canonical_identity = os.stat(tmp_path, follow_symlinks=False)
    alias_identity = os.stat(alias, follow_symlinks=False)
    canonical_key = (canonical_identity.st_dev, canonical_identity.st_ino)
    alias_key = (alias_identity.st_dev, alias_identity.st_ino)
    assert alias_key == canonical_key

    second_entered = Event()
    release_second = Event()
    failures: list[BaseException] = []

    def acquire_alias() -> None:
        try:
            with registry.shot_process_mutex(alias_key):
                second_entered.set()
                if not release_second.wait(timeout=5):
                    raise AssertionError("alias mutex release timed out")
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    with registry.shot_process_mutex(canonical_key):
        thread = Thread(target=acquire_alias)
        thread.start()
        assert not second_entered.wait(timeout=0.1)
    assert second_entered.wait(timeout=5)
    release_second.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert failures == []


def _assert_shot_mutex_available(key: tuple[int, int]) -> None:
    acquired = Event()
    release = Event()
    failures: list[BaseException] = []

    def probe() -> None:
        try:
            with registry.shot_process_mutex(key):
                acquired.set()
                if not release.wait(timeout=5):
                    raise AssertionError("shot mutex probe release timed out")
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    thread = Thread(target=probe, daemon=True)
    thread.start()
    assert acquired.wait(timeout=2)
    release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert failures == []


def test_shot_process_mutex_has_no_detached_cross_thread_release(
    tmp_path: Path,
) -> None:
    observed = os.stat(tmp_path, follow_symlinks=False)
    key = (observed.st_dev, observed.st_ino)
    manager = registry.shot_process_mutex(key)
    manager.__enter__()
    failures: list[BaseException] = []

    def foreign_release() -> None:
        try:
            manager.__exit__(None, None, None)
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    thread = Thread(target=foreign_release)
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert failures == []
    assert not hasattr(registry, "acquire_shot_process_mutex")
    assert not hasattr(registry, "release_shot_process_mutex")
    _assert_shot_mutex_available(key)


def test_begin_fork_guard_failure_releases_shot_process_mutex(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = os.stat(tmp_path, follow_symlinks=False)
    key = (observed.st_dev, observed.st_ino)

    @contextmanager
    def fail_begin():
        raise RuntimeError("injected begin failure")
        yield  # pragma: no cover - required generator shape

    monkeypatch.setattr(
        transaction,
        "managed_fork_protected_acquisition",
        fail_begin,
    )
    with (
        pytest.raises(RuntimeError, match="injected begin failure"),
        authority_selection_lock(tmp_path, exclusive=True),
    ):
        pass

    _assert_shot_mutex_available(key)


def test_managed_acquisition_enter_failure_releases_shot_process_mutex(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = os.stat(tmp_path, follow_symlinks=False)
    key = (observed.st_dev, observed.st_ino)
    original_managed = transaction.managed_fork_protected_acquisition

    @contextmanager
    def enter_then_fail():
        with original_managed():
            raise RuntimeError("injected managed-enter continuation failure")
            yield  # pragma: no cover - required generator shape

    monkeypatch.setattr(
        transaction,
        "managed_fork_protected_acquisition",
        enter_then_fail,
    )
    with (
        pytest.raises(RuntimeError, match="managed-enter continuation failure"),
        authority_selection_lock(tmp_path, exclusive=True),
    ):
        pass

    _assert_shot_mutex_available(key)


def test_registered_close_setup_failure_releases_shot_process_mutex(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = os.stat(tmp_path, follow_symlinks=False)
    key = (observed.st_dev, observed.st_ino)
    original_close = registry.registered_descriptor_close

    @contextmanager
    def safely_close_then_fail(registration):
        with original_close(registration) as proof:
            assert proof is not None
            for descriptor in registration.descriptors:
                with suppress(OSError):
                    os.close(descriptor)
        raise RuntimeError("injected registered-close setup failure")
        yield  # pragma: no cover - required generator shape

    monkeypatch.setattr(
        registry,
        "registered_descriptor_close",
        safely_close_then_fail,
    )
    with (
        pytest.raises(registry.AuthoritySelectionCleanupFailure) as captured,
        authority_selection_lock(tmp_path, exclusive=True),
    ):
        pass

    assert captured.value.retained == ()
    assert "registered-close setup failure" in str(captured.value.errors[0])
    _assert_shot_mutex_available(key)


def test_live_context_close_and_reuse_never_closes_unrelated_descriptor(
    tmp_path: Path,
) -> None:
    manager = authority_selection_lock(tmp_path, exclusive=True)
    manager.__enter__()
    binding = _current_binding(tmp_path)
    original_lock_descriptor = binding.lock_descriptor
    os.close(original_lock_descriptor)
    reused = os.open(os.devnull, os.O_RDONLY)
    assert reused == original_lock_descriptor

    try:
        with pytest.raises(
            AuthoritySelectionConflict,
            match="lock path changed during lock exit",
        ):
            manager.__exit__(None, None, None)
        os.fstat(reused)
        assert _in_fork(
            lambda: {"reused_descriptor_alive": os.fstat(reused) is not None}
        ) == {"reused_descriptor_alive": True}
    finally:
        os.close(reused)

    assert registry._REGISTRY == {}
    assert run_owner_fork_registry.ACTIVE_DESCRIPTORS == {}


def test_registration_insert_before_return_rolls_back_exact_row_and_fds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_register = registry.register_descriptors_locked
    inserted: list[registry.DescriptorLeaseRegistration] = []

    def insert_then_interrupt(*args, **kwargs):
        registration = original_register(*args, **kwargs)
        inserted.append(registration)
        raise RuntimeError("injected registration return interruption")

    monkeypatch.setattr(
        registry,
        "register_descriptors_locked",
        insert_then_interrupt,
    )
    with (
        pytest.raises(RuntimeError, match="registration return interruption"),
        authority_selection_lock(tmp_path, exclusive=True),
    ):
        pass

    assert len(inserted) == 1
    assert registry._REGISTRY == {}
    assert run_owner_fork_registry.PENDING_DESCRIPTORS == {}
    assert run_owner_fork_registry.ACTIVE_DESCRIPTORS == {}
    retired = set(inserted[0].descriptors)
    opened: list[int] = []
    reused: int | None = None
    try:
        while reused is None and len(opened) < 64:
            descriptor = os.open(os.devnull, os.O_RDONLY)
            opened.append(descriptor)
            if descriptor in retired:
                reused = descriptor
        assert reused is not None
        assert _in_fork(
            lambda: {"reused_descriptor_alive": os.fstat(reused) is not None}
        ) == {"reused_descriptor_alive": True}
    finally:
        for descriptor in opened:
            with suppress(OSError):
                os.close(descriptor)


def test_handoff_interruption_after_active_insert_repairs_both_states(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class InterruptAfterFirstInsert(dict):
        interrupted = False

        def __setitem__(self, key, value):
            super().__setitem__(key, value)
            if not self.interrupted:
                self.interrupted = True
                raise RuntimeError("injected active handoff interruption")

    active = InterruptAfterFirstInsert(
        run_owner_fork_registry.ACTIVE_DESCRIPTORS
    )
    monkeypatch.setattr(run_owner_fork_registry, "ACTIVE_DESCRIPTORS", active)

    with (
        pytest.raises(RuntimeError, match="active handoff interruption"),
        authority_selection_lock(tmp_path, exclusive=True),
    ):
        pass

    assert registry._REGISTRY == {}
    assert run_owner_fork_registry.PENDING_DESCRIPTORS == {}
    assert run_owner_fork_registry.ACTIVE_DESCRIPTORS == {}
    with authority_selection_lock(tmp_path, exclusive=True):
        pass


def test_retired_shot_mutex_entries_return_to_exact_baseline(tmp_path: Path) -> None:
    baseline = dict(registry._SHOT_MUTEXES)
    for index in range(40):
        shot = tmp_path / f"retired-shot-{index}"
        shot.mkdir()
        with authority_selection_lock(shot, exclusive=False):
            pass
        shot.rename(tmp_path / f"archived-shot-{index}")

    assert baseline == registry._SHOT_MUTEXES


def test_root_disappearance_before_mutex_identity_is_typed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shot = tmp_path / "disappearing-shot"
    shot.mkdir()
    original_stat = transaction.os.stat
    interrupted = False

    def disappear_once(path, *args, **kwargs):
        nonlocal interrupted
        if Path(path) == shot and not interrupted:
            interrupted = True
            shot.rmdir()
            raise FileNotFoundError(path)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(transaction.os, "stat", disappear_once)
    with (
        pytest.raises(
            AuthoritySelectionConflict,
            match="shot root changed before lock acquisition",
        ),
        authority_selection_lock(shot, exclusive=True),
    ):
        pass

    assert interrupted


def test_body_conflict_survives_complete_cleanup_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_lockf = registry.fcntl.lockf

    def unlock_after_effect_then_raise(descriptor: int, operation: int) -> None:
        original_lockf(descriptor, operation)
        if operation == fcntl.LOCK_UN:
            raise OSError("injected post-unlock diagnostic")

    monkeypatch.setattr(registry.fcntl, "lockf", unlock_after_effect_then_raise)
    with (
        pytest.raises(
            AuthoritySelectionConflict,
            match="body authority conflict",
        ) as captured,
        authority_selection_lock(tmp_path, exclusive=True),
    ):
        raise AuthoritySelectionConflict("body authority conflict")

    notes = getattr(captured.value, "__notes__", ())
    assert any("cleanup failed" in note for note in notes)
    assert any("post-unlock diagnostic" in note for note in notes)
    assert registry._REGISTRY == {}


def test_incomplete_cleanup_raises_typed_composite_retaining_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_dup2 = registry.os.dup2
    failed_descriptor: int | None = None

    def fail_one_dup2(source: int, target: int, *, inheritable: bool = True) -> None:
        if target == failed_descriptor:
            raise OSError("injected unneutralized descriptor")
        original_dup2(source, target, inheritable=inheritable)

    monkeypatch.setattr(registry.os, "dup2", fail_one_dup2)
    with (
        pytest.raises(registry.AuthoritySelectionCleanupFailure) as captured,
        authority_selection_lock(tmp_path, exclusive=True),
    ):
        failed_descriptor = _current_binding(tmp_path).shot_descriptor
        raise AuthoritySelectionConflict("body authority conflict")

    failure = captured.value
    assert isinstance(failure.body_error, AuthoritySelectionConflict)
    assert str(failure.body_error) == "body authority conflict"
    assert tuple(item.descriptor for item in failure.retained) == (
        failed_descriptor,
    )
    partial = next(iter(registry._REGISTRY.values()))
    monkeypatch.setattr(registry.os, "dup2", original_dup2)
    registry.neutralize_registered_descriptors(
        partial,
        unlock_record_lock=False,
    )
    assert registry._REGISTRY == {}
    assert run_owner_fork_registry.ACTIVE_DESCRIPTORS == {}


def test_partial_neutralization_shrinks_rows_before_ambiguous_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_dup2 = registry.os.dup2
    original_close = registry.os.close
    failed_descriptor: int | None = None
    close_attempts: list[int] = []

    def fail_one_dup2(source: int, target: int, *, inheritable: bool = True) -> None:
        if target == failed_descriptor:
            raise OSError("injected dup2 refusal")
        original_dup2(source, target, inheritable=inheritable)

    def observe_close(descriptor: int) -> None:
        close_attempts.append(descriptor)
        original_close(descriptor)

    monkeypatch.setattr(registry.os, "dup2", fail_one_dup2)
    monkeypatch.setattr(registry.os, "close", observe_close)
    with (
        pytest.raises(registry.AuthoritySelectionCleanupFailure) as captured,
        authority_selection_lock(tmp_path, exclusive=True),
    ):
        failed_descriptor = _current_binding(tmp_path).lock_descriptor
        close_attempts.clear()

    assert failed_descriptor not in close_attempts
    assert tuple(item.descriptor for item in captured.value.retained) == (
        failed_descriptor,
    )
    partial = next(iter(registry._REGISTRY.values()))
    assert partial.descriptors == (failed_descriptor,)
    active = next(iter(run_owner_fork_registry.ACTIVE_DESCRIPTORS.values()))
    assert tuple(item.descriptor for item in active) == (failed_descriptor,)

    retired_numbers = {
        descriptor
        for descriptor in close_attempts
        if descriptor != failed_descriptor
    }
    opened: list[int] = []
    reused: int | None = None
    try:
        while reused is None and len(opened) < 64:
            descriptor = os.open(os.devnull, os.O_RDONLY)
            opened.append(descriptor)
            if descriptor in retired_numbers:
                reused = descriptor
        assert reused is not None
        # The initial injected failure deliberately leaves exact authority live.
        # Fork-child policy must terminate if that same injection prevents
        # neutralization, so restore dup2 before testing that already-retired
        # numeric slots are not revisited by child cleanup.
        monkeypatch.setattr(registry.os, "dup2", original_dup2)
        assert _in_fork(
            lambda: {"reused_descriptor_alive": os.fstat(reused) is not None}
        ) == {"reused_descriptor_alive": True}
    finally:
        for descriptor in opened:
            with suppress(OSError):
                original_close(descriptor)

    monkeypatch.setattr(registry.os, "dup2", original_dup2)
    monkeypatch.setattr(registry.os, "close", original_close)
    registry.neutralize_registered_descriptors(
        partial,
        unlock_record_lock=False,
    )


def test_new_lockf_holder_refuses_legacy_flock_client_by_live_identity(
    tmp_path: Path,
) -> None:
    with authority_selection_lock(tmp_path, exclusive=True):

        def legacy_client() -> dict[str, Any]:
            descriptor = os.open(tmp_path / AUTHORITY_SELECTION_LOCK, os.O_RDWR)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            try:
                try:
                    builder_execution_fence._claim_live_identity(
                        tmp_path / AUTHORITY_SELECTION_LOCK,
                        tmp_path / AUTHORITY_SELECTION_LOCK,
                    )
                except builder_execution_fence.BuilderExecutionFenceActive:
                    return {"entered": False, "live_identity_refused": True}
                return {"entered": True, "live_identity_refused": False}
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

        assert _in_fork(legacy_client) == {
            "entered": False,
            "live_identity_refused": True,
        }


def test_selection_live_identity_return_interruption_releases_before_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_acquire = builder_execution_fence.LivePathIdentityClaim.acquire
    interrupted = False

    def acquire_then_interrupt(self) -> None:
        nonlocal interrupted
        original_acquire(self)
        if not interrupted:
            interrupted = True
            raise RuntimeError("injected live-identity return interruption")

    monkeypatch.setattr(
        builder_execution_fence.LivePathIdentityClaim,
        "acquire",
        acquire_then_interrupt,
    )
    with (
        pytest.raises(RuntimeError, match="live-identity return interruption"),
        authority_selection_lock(tmp_path, exclusive=True),
    ):
        pass

    assert interrupted
    monkeypatch.setattr(
        builder_execution_fence.LivePathIdentityClaim,
        "acquire",
        original_acquire,
    )
    with authority_selection_lock(tmp_path, exclusive=True):
        pass


def test_open_failure_abort_close_after_effect_cannot_register_reused_fd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolation_key = "VFX_TEST_AUTHORITY_ABORT_CLOSE_AFTER_EFFECT"
    if os.environ.get(isolation_key) != "1":
        environment = dict(os.environ)
        environment[isolation_key] = "1"
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                f"{__file__}::{test_open_failure_abort_close_after_effect_cannot_register_reused_fd.__name__}",
            ],
            cwd=Path(__file__).resolve().parents[3],
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return

    original_open = transaction.os.open
    original_close = transaction.os.close
    original_track = run_owner_fork_guard.ForkProtectedAcquisition.track
    tracked: list[int] = []
    interrupted_close: int | None = None

    def record_track(self, descriptor: int) -> None:
        tracked.append(descriptor)
        original_track(self, descriptor)

    def fail_selection_open(*args, **kwargs):
        if args and args[0] == AUTHORITY_SELECTION_LOCK.name:
            raise OSError("injected selection open failure")
        return original_open(*args, **kwargs)

    def close_after_effect(descriptor: int) -> None:
        nonlocal interrupted_close
        original_close(descriptor)
        if descriptor in tracked and interrupted_close is None:
            interrupted_close = descriptor
            raise OSError("injected close-after-effect")

    monkeypatch.setattr(
        run_owner_fork_guard.ForkProtectedAcquisition,
        "track",
        record_track,
    )
    monkeypatch.setattr(transaction.os, "open", fail_selection_open)
    monkeypatch.setattr(transaction.os, "close", close_after_effect)
    with (
        pytest.raises(
            registry.AuthoritySelectionCleanupFailure,
            match="cleanup failed",
        ),
        authority_selection_lock(tmp_path, exclusive=True),
    ):
        pass

    assert interrupted_close is not None
    assert run_owner_fork_registry.PENDING_DESCRIPTORS == {}
    assert run_owner_fork_registry.ACTIVE_DESCRIPTORS == {}
    assert registry._REGISTRY == {}
    opened: list[int] = []
    reused: int | None = None
    try:
        while reused is None and len(opened) < 64:
            descriptor = original_open(os.devnull, os.O_RDONLY)
            opened.append(descriptor)
            if descriptor == interrupted_close:
                reused = descriptor
        assert reused is not None
        assert _in_fork(
            lambda: {"reused_descriptor_alive": os.fstat(reused) is not None}
        ) == {"reused_descriptor_alive": True}
    finally:
        for descriptor in opened:
            with suppress(OSError):
                original_close(descriptor)


def test_mutex_owner_capture_failure_precedes_registry_reservation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = os.stat(tmp_path, follow_symlinks=False)
    key = (observed.st_dev, observed.st_ino)
    baseline = dict(registry._SHOT_MUTEXES)
    original_getpid = registry.os.getpid
    interrupted = False

    def fail_once() -> int:
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise RuntimeError("injected mutex owner capture failure")
        return original_getpid()

    monkeypatch.setattr(registry.os, "getpid", fail_once)
    with (
        pytest.raises(RuntimeError, match="mutex owner capture failure"),
        registry.shot_process_mutex(key),
    ):
        pass

    assert interrupted
    assert baseline == registry._SHOT_MUTEXES
    with registry.shot_process_mutex(key):
        pass
    assert baseline == registry._SHOT_MUTEXES


def test_managed_acquisition_constructor_failure_publishes_no_pending_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_constructor = run_owner_fork_guard.ForkProtectedAcquisition
    baseline_pending = dict(run_owner_fork_registry.PENDING_DESCRIPTORS)

    def fail_constructor(*args, **kwargs):
        raise RuntimeError("injected acquisition constructor failure")

    monkeypatch.setattr(
        run_owner_fork_guard,
        "ForkProtectedAcquisition",
        fail_constructor,
    )
    with (
        pytest.raises(RuntimeError, match="acquisition constructor failure"),
        authority_selection_lock(tmp_path, exclusive=True),
    ):
        pass

    assert baseline_pending == run_owner_fork_registry.PENDING_DESCRIPTORS
    monkeypatch.setattr(
        run_owner_fork_guard,
        "ForkProtectedAcquisition",
        original_constructor,
    )
    with authority_selection_lock(tmp_path, exclusive=True):
        pass


def test_first_track_failure_neutralizes_unadopted_descriptor_before_fork(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_track = run_owner_fork_guard.ForkProtectedAcquisition.track
    interrupted_descriptor: int | None = None

    def fail_first_track(self, descriptor: int) -> None:
        nonlocal interrupted_descriptor
        if interrupted_descriptor is None:
            interrupted_descriptor = descriptor
            raise RuntimeError("injected first track failure")
        original_track(self, descriptor)

    monkeypatch.setattr(
        run_owner_fork_guard.ForkProtectedAcquisition,
        "track",
        fail_first_track,
    )
    with (
        pytest.raises(RuntimeError, match="first track failure"),
        authority_selection_lock(tmp_path, exclusive=True),
    ):
        pass

    assert interrupted_descriptor is not None
    assert _descriptors_are_closed((interrupted_descriptor,))
    assert registry._REGISTRY == {}
    assert run_owner_fork_registry.PENDING_DESCRIPTORS == {}
    assert run_owner_fork_registry.ACTIVE_DESCRIPTORS == {}
    reused = os.open(os.devnull, os.O_RDONLY)
    try:
        assert reused == interrupted_descriptor
        assert _in_fork(
            lambda: {"reused_descriptor_alive": os.fstat(reused) is not None}
        ) == {"reused_descriptor_alive": True}
    finally:
        os.close(reused)
