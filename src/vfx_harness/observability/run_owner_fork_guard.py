"""Fork-safe descriptor ownership from acquisition start through lease cleanup."""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass


class RunOwnerForkGuardError(RuntimeError):
    """A descriptor acquisition crossed a process boundary or registry invariant."""


_FORK_LOCK = threading.RLock()
_PENDING_DESCRIPTORS: dict[object, list[int]] = {}
_ACTIVE_DESCRIPTORS: dict[object, tuple[int, ...]] = {}


def _before_fork() -> None:
    # Other threads block until a pending acquisition reaches active handoff or
    # cleanup. The acquisition thread may enter reentrantly for an intentional
    # fork; its pending descriptors are already visible to the child callback.
    _FORK_LOCK.acquire()


def _after_fork_parent() -> None:
    _FORK_LOCK.release()


def _after_fork_child() -> None:
    global _FORK_LOCK

    descriptors = {
        descriptor for rows in (*_PENDING_DESCRIPTORS.values(), *_ACTIVE_DESCRIPTORS.values()) for descriptor in rows
    }
    _PENDING_DESCRIPTORS.clear()
    _ACTIVE_DESCRIPTORS.clear()
    for descriptor in sorted(descriptors, reverse=True):
        with suppress(OSError):
            # Never issue LOCK_UN here. Closing the child's duplicate leaves the
            # parent's shared open-file-description lock intact.
            os.close(descriptor)
    # The pre-fork RLock may retain recursion/owner state that names a vanished
    # thread. The child starts a fresh registry generation and a fresh lock.
    _FORK_LOCK = threading.RLock()


os.register_at_fork(
    before=_before_fork,
    after_in_parent=_after_fork_parent,
    after_in_child=_after_fork_child,
)


@dataclass(slots=True)
class ForkProtectedAcquisition:
    """One creator-process descriptor set protected until active handoff."""

    token: object
    creator_pid: int
    _finished: bool = False

    @property
    def belongs_to_current_process(self) -> bool:
        return os.getpid() == self.creator_pid

    def _pending(self) -> list[int]:
        if not self.belongs_to_current_process:
            raise RunOwnerForkGuardError("forked child cannot continue its parent's descriptor acquisition")
        try:
            return _PENDING_DESCRIPTORS[self.token]
        except KeyError as exc:
            raise RunOwnerForkGuardError("descriptor acquisition is absent from the pending fork registry") from exc

    def track(self, descriptor: int) -> None:
        rows = self._pending()
        if descriptor in rows:
            raise RunOwnerForkGuardError("descriptor acquisition attempted to register one descriptor twice")
        rows.append(descriptor)

    def forget(self, descriptor: int) -> None:
        rows = self._pending()
        try:
            rows.remove(descriptor)
        except ValueError as exc:
            raise RunOwnerForkGuardError("descriptor acquisition cannot forget an unregistered descriptor") from exc

    def retire(self, descriptor: int) -> None:
        """Close a pending OFD without ever leaving a reused fd in the registry."""

        rows = self._pending()
        if descriptor not in rows:
            raise RunOwnerForkGuardError("descriptor acquisition cannot retire an unregistered descriptor")
        null_descriptor: int | None = None
        substituted = False
        try:
            null_descriptor = os.open(
                os.devnull,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
            )
            os.set_inheritable(null_descriptor, False)
            # dup2 atomically releases the tracked OFD while keeping its numeric
            # slot allocated.  The at-fork registry can safely name that slot
            # until it is removed: it now refers only to /dev/null, never to a
            # concurrently reused application descriptor.
            os.dup2(null_descriptor, descriptor, inheritable=False)
            substituted = True
            rows.remove(descriptor)
            with suppress(OSError):
                os.close(descriptor)
        except OSError as exc:
            if substituted:  # pragma: no cover - close errors are suppressed above
                return
            # Failure to obtain the harmless replacement still retires the
            # registry entry before closing.  This fallback may let a child
            # inherit only the staging file in the exact forget/close window;
            # the separately tracked fence OFD is still closed by at-fork, and
            # a reused numeric fd can never remain registered.
            with suppress(ValueError):
                rows.remove(descriptor)
            with suppress(OSError):
                os.close(descriptor)
            raise RunOwnerForkGuardError("could not safely retire a pending descriptor") from exc
        finally:
            if null_descriptor is not None:
                with suppress(OSError):
                    os.close(null_descriptor)

    def handoff(self, descriptors: tuple[int, ...]) -> object:
        rows = self._pending()
        if len(descriptors) != len(set(descriptors)) or set(rows) != set(descriptors):
            raise RunOwnerForkGuardError(
                "active lease handoff must contain every and only pending acquisition descriptor"
            )
        _ACTIVE_DESCRIPTORS[self.token] = descriptors
        del _PENDING_DESCRIPTORS[self.token]
        self._finished = True
        _FORK_LOCK.release()
        return self.token

    def abort(self) -> None:
        if self._finished:
            return
        self._finished = True
        if not self.belongs_to_current_process:
            # The child callback closed inherited descriptors, cleared the copied
            # registry, and replaced its lock. It must not release the new lock.
            return
        _PENDING_DESCRIPTORS.pop(self.token, None)
        _FORK_LOCK.release()


def begin_fork_protected_acquisition() -> ForkProtectedAcquisition:
    """Exclude concurrent fork and expose every opened fd to child cleanup."""

    _FORK_LOCK.acquire()
    token = object()
    if token in _PENDING_DESCRIPTORS:  # pragma: no cover - object identity guarantee
        _FORK_LOCK.release()
        raise RunOwnerForkGuardError("descriptor acquisition token collision")
    _PENDING_DESCRIPTORS[token] = []
    return ForkProtectedAcquisition(token=token, creator_pid=os.getpid())


@contextmanager
def active_descriptor_close(
    token: object,
    descriptors: tuple[int, ...],
) -> Iterator[None]:
    """Keep active descriptors fork-visible until their owning close completes."""

    _FORK_LOCK.acquire()
    invariant_error: RunOwnerForkGuardError | None = None
    try:
        registered = _ACTIVE_DESCRIPTORS.get(token)
        if registered is None:
            invariant_error = RunOwnerForkGuardError("active lease is absent from the fork descriptor registry")
            registered = ()
        elif registered != descriptors:
            invariant_error = RunOwnerForkGuardError("active lease descriptors differ from their fork registry handoff")
        # Even a corrupt registry must not skip the owning close.  Keep the union
        # fork-visible until the caller has unlocked and closed its exact lease.
        _ACTIVE_DESCRIPTORS[token] = tuple(dict.fromkeys((*registered, *descriptors)))
        yield
    finally:
        _ACTIVE_DESCRIPTORS.pop(token, None)
        _FORK_LOCK.release()
    if invariant_error is not None:
        raise invariant_error
