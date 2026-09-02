"""Exact descriptor retirement for opaque prepared-file transactions."""

from __future__ import annotations

import contextlib
import os
import signal
import stat
import threading
from collections.abc import Callable, Iterator

from vfx_harness.observability.run_owner_fork_guard import GuardedDescriptor


class PreparedDescriptorCleanupError(RuntimeError):
    """An exact descriptor could not be retired without ambiguity."""


_BLOCKABLE_SIGNALS = signal.valid_signals() - {signal.SIGKILL, signal.SIGSTOP}
_NEUTRALIZER_DESCRIPTOR, _neutralizer_writer = os.pipe2(getattr(os, "O_CLOEXEC", 0))
os.close(_neutralizer_writer)
_NEUTRALIZER_IDENTITY = GuardedDescriptor.capture(_NEUTRALIZER_DESCRIPTOR)
_INERT_LOCK = threading.RLock()
_INERT_DESCRIPTORS: dict[int, GuardedDescriptor] = {}


@contextlib.contextmanager
def block_deferred_signals() -> Iterator[None]:
    """Defer Python signal handlers across syscall-to-registry handoffs."""

    # Querying with an empty set is non-mutating, so an interruption before the
    # try has no state to restore. The mutating block call itself is inside the
    # protected region and always converges on the captured prior mask.
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, set())
    try:
        signal.pthread_sigmask(signal.SIG_BLOCK, _BLOCKABLE_SIGNALS)
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def _descriptor_matches(descriptor: int, expected: GuardedDescriptor) -> bool:
    try:
        observed = os.fstat(descriptor)
    except OSError:
        return False
    return (
        observed.st_dev == expected.device
        and observed.st_ino == expected.inode
        and stat.S_IFMT(observed.st_mode) == expected.file_type
        and observed.st_rdev == expected.special_device
    )


def _retire_inert_descriptor(guarded: GuardedDescriptor) -> None:
    """Close one registry-owned neutral descriptor, retaining failed closes."""

    if not guarded.is_current():
        _INERT_DESCRIPTORS.pop(guarded.descriptor, None)
        return
    try:
        with block_deferred_signals():
            os.close(guarded.descriptor)
    except BaseException as exc:
        if guarded.is_current():
            raise PreparedDescriptorCleanupError(
                "neutral prepared descriptor could not be closed safely"
            ) from exc
        raise
    finally:
        if not guarded.is_current():
            _INERT_DESCRIPTORS.pop(guarded.descriptor, None)


def _drain_inert_descriptors(*, exclude: int | None = None) -> None:
    """Retry inert closes except a slot whose authority handoff is in flight."""

    errors: list[BaseException] = []
    for guarded in tuple(_INERT_DESCRIPTORS.values()):
        if guarded.descriptor == exclude:
            continue
        try:
            _retire_inert_descriptor(guarded)
        except BaseException as exc:
            errors.append(exc)
    if errors:
        failure = PreparedDescriptorCleanupError(
            "neutral prepared descriptor cleanup remains incomplete"
        )
        for error in errors:
            failure.add_note(f"cleanup diagnostic: {type(error).__name__}: {error}")
        raise failure from errors[0]


def drain_inert_descriptors() -> None:
    """Retry every inert close left by an interrupted prior cleanup."""

    with _INERT_LOCK:
        _drain_inert_descriptors()


def write_all(descriptor: int, payload: bytes) -> None:
    """Write an exact byte payload, refusing a zero-length forward step."""

    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("short write while preparing side-file publication")
        remaining = remaining[written:]


def neutralize_guarded_descriptor(
    guarded: GuardedDescriptor,
    *,
    release: Callable[[], None] | None = None,
) -> bool:
    """Retire an exact descriptor and publish release before slot reuse.

    ``release`` advances the owning registry while the numeric slot is occupied
    by the private neutral identity.  If Python is interrupted on either side
    of that callback, one of those two registries still owns the slot.
    """

    with _INERT_LOCK:
        existing = _INERT_DESCRIPTORS.get(guarded.descriptor)
        if existing is not None:
            if existing.is_current():
                if release is not None:
                    release()
                _retire_inert_descriptor(existing)
                return True
            _INERT_DESCRIPTORS.pop(guarded.descriptor, None)
        _drain_inert_descriptors(exclude=guarded.descriptor)
        if not guarded.is_current():
            if release is not None:
                release()
            return False
        if not _NEUTRALIZER_IDENTITY.is_current():
            raise PreparedDescriptorCleanupError(
                "prepared descriptor neutralizer is no longer live"
            )
        interruption: BaseException | None = None
        try:
            with block_deferred_signals():
                try:
                    os.dup2(
                        _NEUTRALIZER_DESCRIPTOR,
                        guarded.descriptor,
                        inheritable=False,
                    )
                except BaseException as exc:
                    if guarded.is_current():
                        raise
                    if not _descriptor_matches(
                        guarded.descriptor,
                        _NEUTRALIZER_IDENTITY,
                    ):
                        if release is not None:
                            release()
                        return False
                    interruption = exc
                if not _descriptor_matches(
                    guarded.descriptor,
                    _NEUTRALIZER_IDENTITY,
                ):
                    raise PreparedDescriptorCleanupError(
                        "prepared descriptor did not become the private neutral identity"
                    )
                inert = GuardedDescriptor(
                    descriptor=guarded.descriptor,
                    device=_NEUTRALIZER_IDENTITY.device,
                    inode=_NEUTRALIZER_IDENTITY.inode,
                    file_type=_NEUTRALIZER_IDENTITY.file_type,
                    special_device=_NEUTRALIZER_IDENTITY.special_device,
                )
                _INERT_DESCRIPTORS[inert.descriptor] = inert
                if release is not None:
                    release()
                if interruption is not None:
                    raise interruption
        except BaseException:
            # A successful dup2 is registered while signals are deferred. If a
            # wrapper raises after the syscall, the inert descriptor still has a
            # durable cleanup owner.
            if _descriptor_matches(guarded.descriptor, _NEUTRALIZER_IDENTITY):
                inert = GuardedDescriptor(
                    descriptor=guarded.descriptor,
                    device=_NEUTRALIZER_IDENTITY.device,
                    inode=_NEUTRALIZER_IDENTITY.inode,
                    file_type=_NEUTRALIZER_IDENTITY.file_type,
                    special_device=_NEUTRALIZER_IDENTITY.special_device,
                )
                _INERT_DESCRIPTORS[inert.descriptor] = inert
            raise
        _retire_inert_descriptor(inert)
        return True


def _before_fork() -> None:
    _INERT_LOCK.acquire()


def _after_fork_parent() -> None:
    _INERT_LOCK.release()


def _after_fork_child() -> None:
    global _INERT_LOCK

    for guarded in tuple(_INERT_DESCRIPTORS.values()):
        if guarded.is_current():
            with contextlib.suppress(OSError):
                os.close(guarded.descriptor)
    _INERT_DESCRIPTORS.clear()
    _INERT_LOCK = threading.RLock()


os.register_at_fork(
    before=_before_fork,
    after_in_parent=_after_fork_parent,
    after_in_child=_after_fork_child,
)


__all__ = [
    "PreparedDescriptorCleanupError",
    "block_deferred_signals",
    "drain_inert_descriptors",
    "neutralize_guarded_descriptor",
    "write_all",
]
