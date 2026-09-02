"""Exclusive physical transaction claim for one plan-consumer view name."""

from __future__ import annotations

import fcntl
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager

from vfx_harness.observability.prepared_publication_descriptors import (
    block_deferred_signals,
)
from vfx_harness.observability.run_artifacts import RunLayout
from vfx_harness.observability.run_owner_fork_guard import (
    ForkProtectedAcquisition,
    GuardedDescriptor,
    RunOwnerForkGuardCleanupError,
    managed_fork_protected_acquisition,
    neutralize_active_descriptors,
)
from vfx_harness.orchestration.plan_consumer_view_allocation import (
    _canonical_layout_descriptors,
)
from vfx_harness.orchestration.plan_consumer_view_descriptors import (
    PlanConsumerDirectoryIdentity,
    PlanConsumerViewMutationConflict,
)

_LOCK_NAME = ".plan-consumer-view.transaction.lock"
_LOCK_FLAGS = (
    os.O_RDWR
    | os.O_CREAT
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)


def _require_named_lock(
    scratch_descriptor: int,
    lock_descriptor: int,
) -> None:
    opened = os.fstat(lock_descriptor)
    try:
        named = os.stat(
            _LOCK_NAME,
            dir_fd=scratch_descriptor,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise PlanConsumerViewMutationConflict(
            "plan-consumer transaction lock disappeared from exact run scratch"
        ) from exc
    if (
        not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(named.st_mode)
        or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
    ):
        raise PlanConsumerViewMutationConflict(
            "plan-consumer transaction lock must be one real stable regular file"
        )


@contextmanager
def _exclusive_plan_consumer_view_transaction(
    layout: RunLayout,
    *,
    expected_shot_identity: PlanConsumerDirectoryIdentity,
    expected_scratch_identity: PlanConsumerDirectoryIdentity,
) -> Iterator[None]:
    """Hold the exact run view-name claim across install or mutation."""

    creator_pid = os.getpid()
    acquisition: ForkProtectedAcquisition | None = None
    guarded: tuple[GuardedDescriptor, ...] = ()
    descriptor_numbers: tuple[int, ...] = ()
    lock_descriptor: int | None = None
    body_error: BaseException | None = None
    body_traceback = None
    try:
        with managed_fork_protected_acquisition() as pending:
            acquisition = pending
            shot_descriptor, root_descriptor, scratch_descriptor = (
                _canonical_layout_descriptors(layout, pending)
            )
            if (
                PlanConsumerDirectoryIdentity.capture(shot_descriptor)
                != expected_shot_identity
                or PlanConsumerDirectoryIdentity.capture(scratch_descriptor)
                != expected_scratch_identity
            ):
                raise PlanConsumerViewMutationConflict(
                    "plan-consumer source shot or scratch generation changed before "
                    "its physical transaction"
                )
            try:
                lock_descriptor = pending.open_descriptor(
                    lambda: os.open(
                        _LOCK_NAME,
                        _LOCK_FLAGS,
                        0o600,
                        dir_fd=scratch_descriptor,
                    )
                )
            except OSError as exc:
                raise PlanConsumerViewMutationConflict(
                    "plan-consumer transaction lock could not be opened below "
                    "the exact run scratch"
                ) from exc
            os.set_inheritable(lock_descriptor, False)
            _require_named_lock(scratch_descriptor, lock_descriptor)
            os.fsync(lock_descriptor)
            os.fsync(scratch_descriptor)
            descriptor_numbers = (
                shot_descriptor,
                root_descriptor,
                scratch_descriptor,
                lock_descriptor,
            )
            guarded = pending.guarded_descriptors(descriptor_numbers)
            with block_deferred_signals():
                pending.handoff(descriptor_numbers)

        assert lock_descriptor is not None
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PlanConsumerViewMutationConflict(
                "plan-consumer view already has an active install or mutation "
                "transaction"
            ) from exc
        except OSError as exc:
            raise PlanConsumerViewMutationConflict(
                "plan-consumer view transaction lock could not be acquired"
            ) from exc
        _require_named_lock(scratch_descriptor, lock_descriptor)
        yield
    except BaseException as exc:
        body_error = exc
        body_traceback = exc.__traceback__
    finally:
        cleanup_error: BaseException | None = None
        if os.getpid() != creator_pid:
            cleanup_error = PlanConsumerViewMutationConflict(
                "forked child cannot release its parent's plan-consumer transaction"
            )
        elif (
            acquisition is not None
            and descriptor_numbers
            and any(item.is_current_or_unproven() for item in guarded)
        ):
            try:
                neutralize_active_descriptors(acquisition.token, guarded)
            except RunOwnerForkGuardCleanupError as exc:
                cleanup_error = exc
        if body_error is not None:
            if cleanup_error is not None:
                if cleanup_error.retains_authority:
                    cleanup_error.add_note(
                        "primary plan-consumer transaction failure: "
                        f"{type(body_error).__name__}: {body_error}"
                    )
                    raise cleanup_error from body_error
                body_error.add_note(
                    "plan-consumer transaction cleanup diagnostic: "
                    f"{type(cleanup_error).__name__}: {cleanup_error}"
                )
            raise body_error.with_traceback(body_traceback)
        if cleanup_error is not None:
            raise cleanup_error


__all__: list[str] = []
