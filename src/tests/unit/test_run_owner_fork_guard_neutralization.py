"""Regression tests for exact descriptor neutralization and fork cleanup."""

from __future__ import annotations

import errno
import json
import os
import signal
from pathlib import Path

import pytest

from vfx_harness.observability import run_owner_fork_guard as fork_guard
from vfx_harness.observability import run_owner_fork_registry as fork_registry


def _handoff_paths(
    paths: tuple[Path, ...],
) -> tuple[object, tuple[fork_guard.GuardedDescriptor, ...]]:
    with fork_guard.managed_fork_protected_acquisition() as acquisition:
        descriptors = tuple(
            acquisition.open_descriptor(
                lambda path=path: os.open(
                    path,
                    os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
                )
            )
            for path in paths
        )
        guarded = acquisition.guarded_descriptors(descriptors)
        token = acquisition.handoff(descriptors)
    return token, guarded


def _seed_inert_descriptor(*, owner_token: object) -> fork_guard.GuardedDescriptor:
    descriptor = os.dup(fork_registry.NEUTRALIZER_DESCRIPTOR)
    os.set_inheritable(descriptor, False)
    guarded = fork_guard.GuardedDescriptor.capture(descriptor)
    fork_registry.INERT_DESCRIPTORS[descriptor] = fork_registry.InertDescriptorRecord(
        guarded=guarded,
        owner_token=owner_token,
        in_flight=False,
    )
    return guarded


def test_managed_acquisition_refuses_stale_inert_until_explicit_drain() -> None:
    inert = _seed_inert_descriptor(owner_token=object())
    body_entered = False

    with pytest.raises(
        fork_guard.RunOwnerForkGuardCleanupError
    ), fork_guard.managed_fork_protected_acquisition():
        body_entered = True

    assert not body_entered
    assert inert.is_current()
    fork_guard.drain_inert_descriptors()
    assert not inert.is_current()
    with fork_guard.managed_fork_protected_acquisition():
        body_entered = True
    assert body_entered


def test_post_dup2_classification_interruption_retains_exact_neutral(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = tmp_path / "authority.lock"
    authority.touch()
    token, (guarded,) = _handoff_paths((authority,))
    original_matches = fork_guard._descriptor_matches
    interrupted = False

    def interrupt_first_neutral_classification(
        descriptor: int,
        expected: fork_guard.GuardedDescriptor,
    ) -> bool:
        nonlocal interrupted
        if descriptor == guarded.descriptor and not interrupted:
            assert expected == fork_registry.NEUTRALIZER_IDENTITY
            assert original_matches(descriptor, expected)
            interrupted = True
            raise KeyboardInterrupt("injected after successful dup2")
        return original_matches(descriptor, expected)

    try:
        monkeypatch.setattr(
            fork_guard,
            "_descriptor_matches",
            interrupt_first_neutral_classification,
        )
        with pytest.raises(
            fork_guard.RunOwnerForkGuardCleanupError
        ) as captured:
            fork_guard.neutralize_active_descriptors(token, (guarded,))
    finally:
        monkeypatch.setattr(fork_guard, "_descriptor_matches", original_matches)

    assert interrupted
    assert captured.value.retained_authority == ()
    assert len(captured.value.retained_neutral) == 1
    retained = captured.value.retained_neutral[0]
    assert retained.descriptor == guarded.descriptor
    assert original_matches(retained.descriptor, fork_registry.NEUTRALIZER_IDENTITY)
    assert token not in fork_registry.ACTIVE_DESCRIPTORS
    record = fork_registry.INERT_DESCRIPTORS[retained.descriptor]
    assert record.owner_token is token
    assert not record.in_flight

    fork_guard.drain_inert_descriptors()
    assert not retained.is_current()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork semantics")
def test_ambiguous_close_never_retries_same_neutralizer_slot(tmp_path: Path) -> None:
    result_reader, result_writer = os.pipe()
    child = os.fork()
    if child == 0:  # pragma: no cover - assertions execute in the parent
        os.close(result_reader)
        authority = tmp_path / "authority.lock"
        authority.touch()
        token, (guarded,) = _handoff_paths((authority,))
        original_close = os.close
        replacement: int | None = None
        reentrant_rejected = False

        def close_then_reuse_neutralizer(descriptor: int) -> None:
            nonlocal replacement, reentrant_rejected
            if descriptor != guarded.descriptor:
                original_close(descriptor)
                return
            with pytest.raises(fork_guard.RunOwnerForkGuardCleanupError):
                fork_guard.drain_inert_descriptors()
            reentrant_rejected = True
            original_close(descriptor)
            replacement = os.dup(fork_registry.NEUTRALIZER_DESCRIPTOR)
            assert replacement == descriptor
            raise OSError("injected close return failure after same-slot reuse")

        try:
            os.close = close_then_reuse_neutralizer  # type: ignore[method-assign]
            with pytest.raises(fork_guard.RunOwnerForkGuardCleanupError):
                fork_guard.neutralize_active_descriptors(token, (guarded,))
        finally:
            os.close = original_close  # type: ignore[method-assign]
        body_entered = False
        with pytest.raises(
            fork_guard.RunOwnerForkGuardCleanupError
        ), fork_guard.managed_fork_protected_acquisition():
            body_entered = True
        result = {
            "body_entered": body_entered,
            "inert_tracked": guarded.descriptor in fork_registry.INERT_DESCRIPTORS,
            "reentrant_rejected": reentrant_rejected,
            "replacement_live": replacement is not None
            and fork_guard._descriptor_matches(
                replacement,
                fork_registry.NEUTRALIZER_IDENTITY,
            ),
        }
        os.write(result_writer, json.dumps(result, sort_keys=True).encode())
        if replacement is not None:
            original_close(replacement)
        original_close(result_writer)
        os._exit(0)

    os.close(result_writer)
    payload = os.read(result_reader, 4096)
    os.close(result_reader)
    _, status = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    assert json.loads(payload) == {
        "body_entered": False,
        "inert_tracked": False,
        "reentrant_rejected": True,
        "replacement_live": True,
    }


def test_cleanup_error_separates_token_authority_from_neutral_retention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first.lock"
    second = tmp_path / "second.lock"
    first.touch()
    second.touch()
    token, guarded = _handoff_paths((first, second))
    authority_target, neutral_target = guarded
    original_dup2 = os.dup2

    def refuse_authority_substitution(
        source: int,
        destination: int,
        *,
        inheritable: bool = True,
    ) -> int:
        if destination == authority_target.descriptor:
            raise OSError("injected authority substitution failure")
        return original_dup2(source, destination, inheritable=inheritable)

    try:
        monkeypatch.setattr(os, "dup2", refuse_authority_substitution)
        with pytest.raises(
            fork_guard.RunOwnerForkGuardCleanupError
        ) as captured:
            fork_guard.neutralize_active_descriptors(token, guarded)
    finally:
        monkeypatch.setattr(os, "dup2", original_dup2)

    assert captured.value.retained_authority == (authority_target,)
    assert captured.value.retained_neutral == ()
    assert captured.value.retained == (authority_target,)
    assert not neutral_target.is_current()

    fork_guard.neutralize_active_descriptors(token, (authority_target,))
    assert token not in fork_registry.ACTIVE_DESCRIPTORS
    assert not fork_registry.INERT_DESCRIPTORS


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork semantics")
def test_fork_child_poison_never_retries_ambiguous_neutral_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = tmp_path / "authority.lock"
    authority.touch()
    token, (guarded,) = _handoff_paths((authority,))
    result_reader, result_writer = os.pipe()
    original_close = os.close
    failed_once = False

    def fail_first_child_target_close(descriptor: int) -> None:
        nonlocal failed_once
        if descriptor == guarded.descriptor and not failed_once:
            failed_once = True
            raise OSError("injected fork-child close without effect")
        original_close(descriptor)

    monkeypatch.setattr(os, "close", fail_first_child_target_close)
    child = os.fork()
    if child == 0:
        original_close(result_reader)
        try:
            before = {
                "active_empty": not fork_registry.ACTIVE_DESCRIPTORS,
                "pending_empty": not fork_registry.PENDING_DESCRIPTORS,
                "tracked": guarded.descriptor
                in fork_registry.INERT_DESCRIPTORS,
                "current": guarded.is_current(),
            }
            body_entered = False
            with pytest.raises(
                fork_guard.RunOwnerForkGuardCleanupError
            ), fork_guard.managed_fork_protected_acquisition():
                body_entered = True
            result = {
                **before,
                "body_entered": body_entered,
            }
        except BaseException as exc:  # child reports the exact failure to parent
            result = {"error": f"{type(exc).__name__}: {exc}"}
        os.write(result_writer, json.dumps(result, sort_keys=True).encode())
        original_close(result_writer)
        os._exit(0)

    original_close(result_writer)
    payload = os.read(result_reader, 4096)
    original_close(result_reader)
    _, status = os.waitpid(child, 0)
    monkeypatch.setattr(os, "close", original_close)
    try:
        assert os.waitstatus_to_exitcode(status) == 0
        assert json.loads(payload) == {
            "active_empty": True,
            "body_entered": False,
            "current": False,
            "pending_empty": True,
            "tracked": False,
        }
    finally:
        fork_guard.neutralize_active_descriptors(token, (guarded,))


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork semantics")
def test_fork_child_exits_when_inherited_authority_cannot_be_neutralized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = tmp_path / "authority.lock"
    authority.touch()
    token, (guarded,) = _handoff_paths((authority,))
    original_dup2 = os.dup2

    def refuse_child_neutralization(
        source: int,
        destination: int,
        *,
        inheritable: bool = True,
    ) -> int:
        if destination == guarded.descriptor:
            raise OSError("injected inherited authority neutralization failure")
        return original_dup2(source, destination, inheritable=inheritable)

    monkeypatch.setattr(os, "dup2", refuse_child_neutralization)
    child = os.fork()
    if child == 0:  # pragma: no cover - at-fork callback exits first
        os._exit(0)
    _, status = os.waitpid(child, 0)
    monkeypatch.setattr(os, "dup2", original_dup2)
    try:
        assert os.waitstatus_to_exitcode(status) == 86
    finally:
        fork_guard.neutralize_active_descriptors(token, (guarded,))


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork semantics")
def test_fork_child_exits_when_authority_presence_is_unreadable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = tmp_path / "authority.lock"
    authority.touch()
    token, (guarded,) = _handoff_paths((authority,))
    parent_pid = os.getpid()
    original_fstat = os.fstat

    def refuse_child_identity_read(descriptor: int) -> os.stat_result:
        if os.getpid() != parent_pid and descriptor == guarded.descriptor:
            raise OSError(errno.EIO, "injected unreadable authority descriptor")
        return original_fstat(descriptor)

    monkeypatch.setattr(os, "fstat", refuse_child_identity_read)
    child = os.fork()
    if child == 0:  # pragma: no cover - at-fork callback exits first
        os._exit(0)
    _, status = os.waitpid(child, 0)
    monkeypatch.setattr(os, "fstat", original_fstat)
    try:
        assert os.waitstatus_to_exitcode(status) == 86
    finally:
        fork_guard.neutralize_active_descriptors(token, (guarded,))


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork semantics")
def test_unreadable_authority_cleanup_retains_and_poisons_admission(
    tmp_path: Path,
) -> None:
    result_reader, result_writer = os.pipe()
    child = os.fork()
    if child == 0:  # pragma: no cover - assertions execute in the parent
        os.close(result_reader)
        authority = tmp_path / "authority.lock"
        authority.touch()
        token, (guarded,) = _handoff_paths((authority,))
        original_fstat = os.fstat

        def refuse_identity_read(descriptor: int) -> os.stat_result:
            if descriptor == guarded.descriptor:
                raise OSError(errno.EIO, "injected unreadable authority descriptor")
            return original_fstat(descriptor)

        os.fstat = refuse_identity_read  # type: ignore[method-assign]
        try:
            with pytest.raises(fork_guard.RunOwnerForkGuardError):
                fork_guard.neutralize_active_descriptors(token, (guarded,))
        finally:
            os.fstat = original_fstat  # type: ignore[method-assign]
        body_entered = False
        with pytest.raises(
            fork_guard.RunOwnerForkGuardCleanupError
        ), fork_guard.managed_fork_protected_acquisition():
            body_entered = True
        result = {
            "body_entered": body_entered,
            "poisoned": bool(fork_registry.PROCESS_CLEANUP_POISON),
            "retained": fork_registry.ACTIVE_DESCRIPTORS.get(token) == (guarded,),
        }
        fork_guard.neutralize_active_descriptors(token, (guarded,))
        os.write(result_writer, json.dumps(result, sort_keys=True).encode())
        os.close(result_writer)
        os._exit(0)

    os.close(result_writer)
    payload = os.read(result_reader, 4096)
    os.close(result_reader)
    _, status = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    assert json.loads(payload) == {
        "body_entered": False,
        "poisoned": True,
        "retained": True,
    }


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork semantics")
def test_open_descriptor_defers_signal_fork_until_identity_is_registered(
    tmp_path: Path,
) -> None:
    authority = tmp_path / "authority.lock"
    authority.touch()
    result_reader, result_writer = os.pipe()
    prior_handler = signal.getsignal(signal.SIGUSR1)
    prior_mask = signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGUSR1})
    opened: dict[str, int] = {}
    child_status: list[int] = []

    def fork_from_signal(_signum: int, _frame: object) -> None:
        child = os.fork()
        if child == 0:  # pragma: no cover - assertions execute in the parent
            os.close(result_reader)
            try:
                os.fstat(opened["descriptor"])
            except OSError as exc:
                result = b"absent" if exc.errno == errno.EBADF else b"unproven"
            else:
                result = b"live"
            os.write(result_writer, result)
            os.close(result_writer)
            os._exit(0)
        _, status = os.waitpid(child, 0)
        child_status.append(os.waitstatus_to_exitcode(status))

    signal.signal(signal.SIGUSR1, fork_from_signal)
    try:
        with fork_guard.managed_fork_protected_acquisition() as acquisition:

            def open_then_signal() -> int:
                descriptor = os.open(authority, os.O_RDONLY)
                opened["descriptor"] = descriptor
                os.kill(os.getpid(), signal.SIGUSR1)
                return descriptor

            descriptor = acquisition.open_descriptor(open_then_signal)
            assert acquisition.guarded_descriptors((descriptor,))
    finally:
        signal.signal(signal.SIGUSR1, prior_handler)
        signal.pthread_sigmask(signal.SIG_SETMASK, prior_mask)

    assert os.read(result_reader, 16) == b"absent"
    os.close(result_reader)
    os.close(result_writer)
    assert child_status == [0]


def _unreadable_identity_for(target: os.stat_result):
    original_fstat = os.fstat

    def unreadable_authority_identity(descriptor: int) -> os.stat_result:
        observed = original_fstat(descriptor)
        if (observed.st_dev, observed.st_ino) == (target.st_dev, target.st_ino):
            raise OSError(errno.EIO, "injected unreadable authority identity")
        return observed

    return original_fstat, unreadable_authority_identity


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork semantics")
def test_unreadable_adoption_neutralizes_the_slot_and_poisons_admission(
    tmp_path: Path,
) -> None:
    result_reader, result_writer = os.pipe()
    child = os.fork()
    if child == 0:  # pragma: no cover - assertions execute in the parent
        os.close(result_reader)
        authority = tmp_path / "authority.lock"
        authority.touch()
        original_fstat, unreadable = _unreadable_identity_for(os.stat(authority))
        opened: dict[str, int] = {}

        def open_authority() -> int:
            opened["descriptor"] = os.open(
                authority,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
            )
            return opened["descriptor"]

        os.fstat = unreadable  # type: ignore[method-assign]
        try:
            with pytest.raises(
                fork_guard.RunOwnerForkGuardError,
                match="absence is unproven",
            ) as captured, fork_guard.managed_fork_protected_acquisition() as acquisition:
                acquisition.open_descriptor(open_authority)
        finally:
            os.fstat = original_fstat  # type: ignore[method-assign]
        try:
            original_fstat(opened["descriptor"])
        except OSError as exc:
            slot_state = "closed" if exc.errno == errno.EBADF else "unreadable"
        else:
            slot_state = "live"
        body_entered = False
        with pytest.raises(
            fork_guard.RunOwnerForkGuardCleanupError
        ) as admission, fork_guard.managed_fork_protected_acquisition():
            body_entered = True
        result = {
            "active_empty": not fork_registry.ACTIVE_DESCRIPTORS,
            "admission_unproven": list(admission.value.retained_unproven),
            "body_entered": body_entered,
            "cleanup_error": isinstance(
                captured.value,
                fork_guard.RunOwnerForkGuardCleanupError,
            ),
            "pending_empty": not fork_registry.PENDING_DESCRIPTORS,
            "poisoned": bool(fork_registry.PROCESS_CLEANUP_POISON),
            "slot_state": slot_state,
            "unproven_registered": sorted(fork_registry.UNPROVEN_DESCRIPTORS),
        }
        os.write(result_writer, json.dumps(result, sort_keys=True).encode())
        os.close(result_writer)
        os._exit(0)

    os.close(result_writer)
    payload = os.read(result_reader, 4096)
    os.close(result_reader)
    _, status = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    assert json.loads(payload) == {
        "active_empty": True,
        "admission_unproven": [],
        "body_entered": False,
        "cleanup_error": False,
        "pending_empty": True,
        "poisoned": True,
        "slot_state": "closed",
        "unproven_registered": [],
    }


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork semantics")
def test_unproven_slot_that_cannot_be_neutralized_is_retained_and_ends_children(
    tmp_path: Path,
) -> None:
    result_reader, result_writer = os.pipe()
    child = os.fork()
    if child == 0:  # pragma: no cover - assertions execute in the parent
        os.close(result_reader)
        authority = tmp_path / "authority.lock"
        authority.touch()
        original_fstat, unreadable = _unreadable_identity_for(os.stat(authority))
        original_dup2 = os.dup2
        opened: dict[str, int] = {}

        def open_authority() -> int:
            opened["descriptor"] = os.open(
                authority,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
            )
            return opened["descriptor"]

        def refuse_neutralization(
            source: int,
            destination: int,
            *,
            inheritable: bool = True,
        ) -> int:
            if destination == opened.get("descriptor"):
                raise OSError(errno.EBUSY, "injected neutralization failure")
            return original_dup2(source, destination, inheritable=inheritable)

        os.fstat = unreadable  # type: ignore[method-assign]
        os.dup2 = refuse_neutralization  # type: ignore[method-assign]
        try:
            with pytest.raises(
                fork_guard.RunOwnerForkGuardCleanupError
            ) as captured, fork_guard.managed_fork_protected_acquisition() as acquisition:
                acquisition.open_descriptor(open_authority)
        finally:
            os.fstat = original_fstat  # type: ignore[method-assign]
            os.dup2 = original_dup2  # type: ignore[method-assign]
        descriptor = opened["descriptor"]
        grandchild = os.fork()
        if grandchild == 0:  # pragma: no cover - the at-fork callback exits first
            os._exit(0)
        _, grandchild_status = os.waitpid(grandchild, 0)
        result = {
            "grandchild_exit": os.waitstatus_to_exitcode(grandchild_status),
            "poisoned": bool(fork_registry.PROCESS_CLEANUP_POISON),
            "retained_unproven": list(captured.value.retained_unproven) == [descriptor],
            "slot_live": original_fstat(descriptor) is not None,
            "unproven_registered": sorted(fork_registry.UNPROVEN_DESCRIPTORS) == [descriptor],
        }
        os.write(result_writer, json.dumps(result, sort_keys=True).encode())
        os.close(result_writer)
        os._exit(0)

    os.close(result_writer)
    payload = os.read(result_reader, 4096)
    os.close(result_reader)
    _, status = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    assert json.loads(payload) == {
        "grandchild_exit": 86,
        "poisoned": True,
        "retained_unproven": True,
        "slot_live": True,
        "unproven_registered": True,
    }
