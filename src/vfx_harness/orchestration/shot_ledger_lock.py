"""Fork-safe locking for the one canonical ``<shot>/shot.json`` ledger.

Preparation may hold the ledger lock by itself because it only reads the current
generation and writes an unreferenced staging inode.  A canonical mutation must
instead use :func:`shot_ledger_mutation_lock`, which couples the real OS lock to
the active shot-authority writer capability and its global inner-lock order.
"""

from __future__ import annotations

import fcntl
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from vfx_harness.observability.prepared_publication_descriptors import (
    block_deferred_signals,
)
from vfx_harness.observability.run_owner_fork_guard import (
    ForkProtectedAcquisition,
    GuardedDescriptor,
    RunOwnerForkGuardCleanupError,
    managed_fork_protected_acquisition,
    neutralize_active_descriptors,
)
from vfx_harness.orchestration.shot_authority_capture import (
    ShotAuthorityWriterCapability,
    ordered_authority_inner_lock,
    require_live_shot_authority_writer,
)

_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_LOCK_FLAGS = (
    os.O_RDWR
    | os.O_CREAT
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)


class LedgerSaveConflict(RuntimeError):
    """The ledger lock or prepared generation cannot authorize publication."""


def _absolute(path: str | Path) -> Path:
    return Path(os.path.abspath(Path(path).expanduser()))


def canonical_shot_ledger_path(path: str | Path) -> tuple[Path, Path]:
    """Return the canonical lexical shot and exact supported ledger target."""

    target = _absolute(path)
    if target.name != "shot.json":
        raise LedgerSaveConflict(
            f"shot-ledger locking requires the exact canonical shot.json target: {target}"
        )
    return target.parent, target


def _open_real_directory(
    path: Path,
    acquisition: ForkProtectedAcquisition,
) -> int:
    """Traverse a real directory with every component fork-registry-owned."""

    try:
        descriptor = acquisition.open_descriptor(
            lambda: os.open(path.anchor, _DIRECTORY_FLAGS)
        )
        os.set_inheritable(descriptor, False)
        for part in path.parts[1:]:
            following = acquisition.open_descriptor(
                lambda component=part, parent=descriptor: os.open(
                    component,
                    _DIRECTORY_FLAGS,
                    dir_fd=parent,
                )
            )
            os.set_inheritable(following, False)
            acquisition.retire(descriptor)
            descriptor = following
    except OSError as exc:
        raise LedgerSaveConflict(
            f"shot-ledger root must contain only real directory components: {path}"
        ) from exc
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        raise LedgerSaveConflict(f"shot-ledger root is not a real directory: {path}")
    return descriptor


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino, stat.S_IFMT(left.st_mode)) == (
        right.st_dev,
        right.st_ino,
        stat.S_IFMT(right.st_mode),
    )


def _require_lock_namespace_current(
    shot: Path,
    shot_descriptor: int,
    lock_descriptor: int,
    lock_name: str,
) -> None:
    """Reopen both names and require the held shot/lock inode pair."""

    try:
        # The reopen is transient, but it is still a real descriptor inherited by
        # fork.  Keep it pending in the same process-wide fork registry until the
        # identity comparison completes and managed unwind retires it.
        with managed_fork_protected_acquisition() as acquisition:
            current_shot = _open_real_directory(shot, acquisition)
            held_shot = os.fstat(shot_descriptor)
            named_shot = os.fstat(current_shot)
            held_lock = os.fstat(lock_descriptor)
            named_lock = os.stat(
                lock_name,
                dir_fd=shot_descriptor,
                follow_symlinks=False,
            )
    except (LedgerSaveConflict, OSError) as exc:
        raise LedgerSaveConflict(
            f"shot-ledger root or lock disappeared or became unreadable: {shot / lock_name}"
        ) from exc
    if (
        not stat.S_ISDIR(held_shot.st_mode)
        or not stat.S_ISREG(held_lock.st_mode)
        or not stat.S_ISREG(named_lock.st_mode)
        or not _same_identity(held_shot, named_shot)
        or not _same_identity(held_lock, named_lock)
    ):
        raise LedgerSaveConflict(
            f"shot-ledger root or lock was replaced or rebound: {shot / lock_name}"
        )


def _close_guarded_descriptors(
    token: object,
    guarded: tuple[GuardedDescriptor, ...],
) -> tuple[BaseException | None, bool]:
    """Neutralize exact active fds before any ambiguous raw-close outcome."""

    try:
        neutralize_active_descriptors(token, guarded)
    except RunOwnerForkGuardCleanupError as exc:
        if exc.retains_authority:
            failure = LedgerSaveConflict(
                "shot-ledger lock cleanup retained live descriptors; route to engineering"
            )
            failure.__cause__ = exc
            return failure, True
        if not exc.errors:
            return exc, False
        primary = exc.errors[0]
        for diagnostic in exc.errors[1:]:
            primary.add_note(
                "descriptor cleanup diagnostic: "
                f"{type(diagnostic).__name__}: {diagnostic}"
            )
        return primary, False
    return None, False


@contextmanager
def ledger_lock(
    path: str | Path,
    *,
    exclusive: bool,
    blocking: bool = True,
) -> Iterator[None]:
    """Hold the exact canonical ledger lock with fork-safe descriptor ownership."""

    if not isinstance(exclusive, bool):
        raise ValueError("ledger lock mode must be boolean")
    if not isinstance(blocking, bool):
        raise ValueError("ledger lock blocking mode must be boolean")
    shot, target = canonical_shot_ledger_path(path)
    lock_name = target.name + ".lock"
    creator_pid = os.getpid()
    token: object | None = None
    guarded: tuple[GuardedDescriptor, ...] = ()
    acquisition: ForkProtectedAcquisition | None = None
    descriptor_numbers: tuple[int, ...] = ()
    shot_descriptor: int | None = None
    lock_descriptor: int | None = None
    lock_acquired = False
    body_error: BaseException | None = None
    body_traceback = None
    try:
        # This outer transaction is armed before descriptor acquisition begins.
        # Pending descriptors belong to the managed acquisition; after handoff,
        # the finally path below owns the exact active registry row.  There is no
        # call/return window in which the descriptors have no cleanup owner.
        with managed_fork_protected_acquisition() as pending:
            acquisition = pending
            shot_descriptor = _open_real_directory(shot, pending)
            try:
                lock_descriptor = pending.open_descriptor(
                    lambda: os.open(
                        lock_name,
                        _LOCK_FLAGS,
                        0o600,
                        dir_fd=shot_descriptor,
                    )
                )
            except OSError as exc:
                raise LedgerSaveConflict(
                    f"ledger lock must be a real regular file: {shot / lock_name}"
                ) from exc
            if not stat.S_ISREG(os.fstat(lock_descriptor).st_mode):
                raise LedgerSaveConflict(
                    f"ledger lock must be a real regular file: {shot / lock_name}"
                )
            os.set_inheritable(lock_descriptor, False)
            os.fsync(lock_descriptor)
            os.fsync(shot_descriptor)
            descriptor_numbers = (shot_descriptor, lock_descriptor)
            guarded = pending.guarded_descriptors(descriptor_numbers)
            token = pending.token
            # Defer process signals across the pending-to-active registry handoff.
            # An arbitrary Python exception is still handled by the already-armed
            # outer finally transaction.
            with block_deferred_signals():
                pending.handoff(descriptor_numbers)

        # A blocking kernel lock must never retain the process-wide fork-acquisition
        # mutex.  The descriptors are already active and therefore remain visible to
        # the child cleanup callback while this thread waits here.
        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        if not blocking:
            operation |= fcntl.LOCK_NB
        assert lock_descriptor is not None and shot_descriptor is not None
        try:
            fcntl.flock(lock_descriptor, operation)
        except BlockingIOError as exc:
            raise LedgerSaveConflict(
                f"ledger lock is busy: {shot / lock_name}; retry from current shot.json"
            ) from exc
        except OSError as exc:
            raise LedgerSaveConflict(
                f"could not acquire ledger lock: {shot / lock_name}"
            ) from exc
        lock_acquired = True
        _require_lock_namespace_current(
            shot,
            shot_descriptor,
            lock_descriptor,
            lock_name,
        )
        yield
    except BaseException as exc:
        body_error = exc
        body_traceback = exc.__traceback__
    finally:
        release_error: BaseException | None = None
        cleanup_retained = False
        if os.getpid() != creator_pid:
            release_error = LedgerSaveConflict(
                "forked child cannot continue or release its parent's shot-ledger lock"
            )
        elif (
            acquisition is not None
            and descriptor_numbers
            and any(item.is_current_or_unproven() for item in guarded)
        ):
            assert token is not None
            assert shot_descriptor is not None and lock_descriptor is not None
            if lock_acquired:
                try:
                    _require_lock_namespace_current(
                        shot,
                        shot_descriptor,
                        lock_descriptor,
                        lock_name,
                    )
                except BaseException as exc:
                    release_error = exc
            with block_deferred_signals():
                cleanup_error, cleanup_retained = _close_guarded_descriptors(
                    token,
                    guarded,
                )
            if cleanup_error is not None:
                if cleanup_retained:
                    if release_error is not None:
                        cleanup_error.add_note(
                            f"lock validation diagnostic: {release_error}"
                        )
                    release_error = cleanup_error
                elif release_error is None:
                    release_error = cleanup_error
                else:
                    release_error.add_note(
                        f"lock cleanup diagnostic: {cleanup_error}"
                    )

        if body_error is not None:
            if release_error is not None:
                if cleanup_retained:
                    release_error.add_note(
                        f"primary lock-body failure: {type(body_error).__name__}: "
                        f"{body_error}"
                    )
                    raise release_error from body_error
                body_error.add_note(
                    f"shot-ledger lock release diagnostic: {release_error}"
                )
            raise body_error.with_traceback(body_traceback)
        if release_error is not None:
            raise release_error


@contextmanager
def shot_ledger_mutation_lock(
    path: str | Path,
    capability: ShotAuthorityWriterCapability,
) -> Iterator[None]:
    """Couple the actual nonblocking ledger EX lock to global writer order."""

    shot, target = canonical_shot_ledger_path(path)
    require_live_shot_authority_writer(capability, shot)
    with (
        ordered_authority_inner_lock(capability, "shot_ledger"),
        ledger_lock(target, exclusive=True, blocking=False),
    ):
        require_live_shot_authority_writer(capability, shot)
        yield
        require_live_shot_authority_writer(capability, shot)


__all__ = [
    "LedgerSaveConflict",
    "canonical_shot_ledger_path",
    "ledger_lock",
    "shot_ledger_mutation_lock",
]
