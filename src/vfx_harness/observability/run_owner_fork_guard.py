"""Fork-safe descriptor ownership from acquisition start through lease cleanup."""

from __future__ import annotations

import os
import stat
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass


class RunOwnerForkGuardError(RuntimeError):
    """A descriptor acquisition crossed a process boundary or registry invariant."""


class RunOwnerForkGuardCleanupError(RunOwnerForkGuardError):
    """A managed acquisition retained exact live descriptors after cleanup failed."""

    def __init__(
        self,
        errors: tuple[BaseException, ...],
        retained: tuple[GuardedDescriptor, ...],
    ) -> None:
        self.errors = errors
        self.retained = retained
        super().__init__(
            "fork-protected acquisition cleanup could not neutralize every "
            f"descriptor; retained={tuple(item.descriptor for item in retained)!r}; "
            "route to engineering"
        )


_FORK_LOCK = threading.RLock()


@dataclass(frozen=True, slots=True)
class GuardedDescriptor:
    """A numeric descriptor joined to the file identity captured at ownership."""

    descriptor: int
    device: int
    inode: int
    file_type: int
    special_device: int

    @classmethod
    def capture(cls, descriptor: int) -> GuardedDescriptor:
        observed = os.fstat(descriptor)
        return cls(
            descriptor=descriptor,
            device=observed.st_dev,
            inode=observed.st_ino,
            file_type=stat.S_IFMT(observed.st_mode),
            special_device=observed.st_rdev,
        )

    def is_current(self) -> bool:
        """Whether the numeric slot still names the captured file identity."""

        try:
            observed = os.fstat(self.descriptor)
        except OSError:
            return False
        return (
            observed.st_dev == self.device
            and observed.st_ino == self.inode
            and stat.S_IFMT(observed.st_mode) == self.file_type
            and observed.st_rdev == self.special_device
        )


_PENDING_DESCRIPTORS: dict[object, list[GuardedDescriptor]] = {}
_ACTIVE_DESCRIPTORS: dict[object, tuple[GuardedDescriptor, ...]] = {}


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
        descriptor
        for rows in (*_PENDING_DESCRIPTORS.values(), *_ACTIVE_DESCRIPTORS.values())
        for descriptor in rows
    }
    _PENDING_DESCRIPTORS.clear()
    _ACTIVE_DESCRIPTORS.clear()
    for guarded in sorted(
        descriptors,
        key=lambda item: item.descriptor,
        reverse=True,
    ):
        if guarded.is_current():
            with suppress(OSError):
                # Never issue LOCK_UN here. Closing the child's duplicate leaves
                # the parent's lock intact. A stale numeric slot is never touched.
                os.close(guarded.descriptor)
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
    guard_managed: bool = False
    _finished: bool = False

    @property
    def belongs_to_current_process(self) -> bool:
        return os.getpid() == self.creator_pid

    def active_handoff_matches(self, descriptors: tuple[int, ...]) -> bool:
        """Report whether this acquisition committed the exact active lease."""

        return (
            self.belongs_to_current_process
            and self._finished
            and tuple(
                guarded.descriptor
                for guarded in _ACTIVE_DESCRIPTORS.get(self.token, ())
            )
            == descriptors
        )

    def cleanup_is_complete(self) -> bool:
        """Report that no pending or active descriptor row remains."""

        return (
            self.belongs_to_current_process
            and self._finished
            and self.token not in _PENDING_DESCRIPTORS
            and self.token not in _ACTIVE_DESCRIPTORS
        )

    def _pending(self) -> list[GuardedDescriptor]:
        if not self.belongs_to_current_process:
            raise RunOwnerForkGuardError("forked child cannot continue its parent's descriptor acquisition")
        try:
            return _PENDING_DESCRIPTORS[self.token]
        except KeyError as exc:
            raise RunOwnerForkGuardError("descriptor acquisition is absent from the pending fork registry") from exc

    def track(self, descriptor: int) -> None:
        rows = self._pending()
        if any(guarded.descriptor == descriptor for guarded in rows):
            raise RunOwnerForkGuardError("descriptor acquisition attempted to register one descriptor twice")
        try:
            rows.append(GuardedDescriptor.capture(descriptor))
        except OSError as exc:
            raise RunOwnerForkGuardError(
                "descriptor acquisition cannot track a closed or unreadable descriptor"
            ) from exc

    def adopt_descriptor(self, descriptor: int) -> None:
        """Adopt a caller-owned fd or neutralize it before reporting failure."""

        try:
            self.track(descriptor)
        except BaseException as adoption_error:
            rows = self._pending()
            if not any(
                guarded.descriptor == descriptor for guarded in rows
            ):
                # The caller still owns this exact numeric slot. Capture it
                # directly so managed unwind can neutralize it even when an
                # injected track failure occurred before the normal append.
                rows.append(GuardedDescriptor.capture(descriptor))
            try:
                self.retire(descriptor)
            except BaseException as cleanup_error:
                raise RunOwnerForkGuardCleanupError(
                    (cleanup_error,),
                    tuple(self._pending()),
                ) from adoption_error
            raise adoption_error

    def open_descriptor(self, opener: Callable[[], int]) -> int:
        """Open and adopt one fd inside an already-armed cleanup transaction."""

        descriptor: int | None = None
        try:
            descriptor = opener()
            self.adopt_descriptor(descriptor)
            return descriptor
        except BaseException:
            raise

    def guarded_descriptors(
        self,
        descriptors: tuple[int, ...],
    ) -> tuple[GuardedDescriptor, ...]:
        """Return the captured identities for an exact pending or active set."""

        if not self.belongs_to_current_process:
            raise RunOwnerForkGuardError(
                "forked child cannot inspect its parent's descriptor acquisition"
            )
        rows = _PENDING_DESCRIPTORS.get(self.token)
        if rows is None:
            active = _ACTIVE_DESCRIPTORS.get(self.token)
            if active is None:
                raise RunOwnerForkGuardError(
                    "descriptor acquisition is absent from the fork registry"
                )
            rows = list(active)
        if tuple(guarded.descriptor for guarded in rows) != descriptors:
            raise RunOwnerForkGuardError(
                "descriptor identities differ from their acquisition registry"
            )
        return tuple(rows)

    def forget(self, descriptor: int) -> None:
        rows = self._pending()
        matched = next(
            (guarded for guarded in rows if guarded.descriptor == descriptor),
            None,
        )
        if matched is None:
            raise RunOwnerForkGuardError(
                "descriptor acquisition cannot forget an unregistered descriptor"
            )
        rows.remove(matched)

    def retire(self, descriptor: int) -> None:
        """Close a pending descriptor without leaving a reused fd registered."""

        rows = self._pending()
        guarded = next(
            (item for item in rows if item.descriptor == descriptor),
            None,
        )
        if guarded is None:
            raise RunOwnerForkGuardError("descriptor acquisition cannot retire an unregistered descriptor")
        if not guarded.is_current():
            # The owner already closed or rebound this numeric slot. Retire the
            # stale row without touching the unrelated replacement.
            rows.remove(guarded)
            return
        null_descriptor: int | None = None
        substituted = False
        try:
            null_descriptor = os.open(
                os.devnull,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
            )
            os.set_inheritable(null_descriptor, False)
            # dup2 atomically releases the tracked file while keeping its numeric
            # slot allocated.  The at-fork registry can safely name that slot
            # until it is removed: it now refers only to /dev/null, never to a
            # concurrently reused application descriptor.
            os.dup2(null_descriptor, descriptor, inheritable=False)
            substituted = True
            rows.remove(guarded)
            with suppress(OSError):
                os.close(descriptor)
        except OSError as exc:
            if substituted:  # pragma: no cover - close errors are suppressed above
                return
            # A failed dup2 leaves the original descriptor known-live. Never
            # issue an ambiguous close and then retain its numeric slot: the
            # close may take effect before raising and a later fork could close
            # an unrelated descriptor that reused the number.
            raise RunOwnerForkGuardError("could not safely retire a pending descriptor") from exc
        finally:
            if null_descriptor is not None:
                with suppress(OSError):
                    os.close(null_descriptor)

    def handoff(self, descriptors: tuple[int, ...]) -> object:
        rows = self._pending()
        if (
            len(descriptors) != len(set(descriptors))
            or tuple(guarded.descriptor for guarded in rows) != descriptors
        ):
            raise RunOwnerForkGuardError(
                "active lease handoff must contain every and only pending acquisition descriptor"
            )
        try:
            guarded_descriptors = tuple(rows)
            _ACTIVE_DESCRIPTORS[self.token] = guarded_descriptors
            del _PENDING_DESCRIPTORS[self.token]
            self._finished = True
        except BaseException:
            # The creator still owns _FORK_LOCK here, so restore the complete
            # pending state before exposing the failure to its abort path. This
            # closes every interruption point between the two registry rows.
            self._finished = False
            _ACTIVE_DESCRIPTORS.pop(self.token, None)
            _PENDING_DESCRIPTORS[self.token] = list(guarded_descriptors)
            raise
        if not self.guard_managed:
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
        if not self.guard_managed:
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
def managed_fork_protected_acquisition() -> Iterator[ForkProtectedAcquisition]:
    """Arm fork-guard cleanup before exposing a pending acquisition."""

    token = object()
    acquisition = ForkProtectedAcquisition(
        token=token,
        creator_pid=os.getpid(),
        guard_managed=True,
    )
    with _FORK_LOCK:
        if token in _PENDING_DESCRIPTORS:  # pragma: no cover - identity guarantee
            raise RunOwnerForkGuardError("descriptor acquisition token collision")
        body_error: BaseException | None = None
        try:
            _PENDING_DESCRIPTORS[token] = []
            yield acquisition
        except BaseException as exc:
            body_error = exc
        pending = _PENDING_DESCRIPTORS.get(token)
        cleanup_errors: list[BaseException] = []
        if pending is not None:
            for guarded in tuple(reversed(pending)):
                try:
                    acquisition.retire(guarded.descriptor)
                except BaseException as exc:
                    cleanup_errors.append(exc)
            remaining = tuple(
                guarded.descriptor
                for guarded in _PENDING_DESCRIPTORS.get(token, ())
            )
            if remaining:
                try:
                    acquisition.handoff(remaining)
                except BaseException as exc:
                    cleanup_errors.append(exc)
            else:
                acquisition.abort()
        if cleanup_errors:
            retained = tuple(_ACTIVE_DESCRIPTORS.get(token, ()))
            failure = RunOwnerForkGuardCleanupError(
                tuple(cleanup_errors),
                retained,
            )
            if body_error is not None:
                raise failure from body_error
            raise failure
        if body_error is not None:
            raise body_error


@contextmanager
def active_descriptor_close(
    token: object,
    descriptors: tuple[GuardedDescriptor, ...],
) -> Iterator[None]:
    """Keep exact live identities fork-visible through owning cleanup."""

    _FORK_LOCK.acquire()
    invariant_error: RunOwnerForkGuardError | None = None
    visible = descriptors
    try:
        registered = _ACTIVE_DESCRIPTORS.get(token)
        if registered is None:
            invariant_error = RunOwnerForkGuardError("active lease is absent from the fork descriptor registry")
            registered = ()
        elif registered != descriptors:
            invariant_error = RunOwnerForkGuardError("active lease descriptors differ from their fork registry handoff")
        # Even a corrupt registry must not skip safe owning cleanup. Keep stored
        # identities visible, and never infer ownership from a bare fd number.
        visible = tuple(
            {guarded.descriptor: guarded for guarded in (*registered, *descriptors)}.values()
        )
        _ACTIVE_DESCRIPTORS[token] = visible
        yield
    finally:
        remaining = tuple(guarded for guarded in visible if guarded.is_current())
        if remaining:
            _ACTIVE_DESCRIPTORS[token] = remaining
        else:
            _ACTIVE_DESCRIPTORS.pop(token, None)
        _FORK_LOCK.release()
    if invariant_error is not None:
        raise invariant_error


def replace_active_descriptors_locked(
    token: object,
    expected: tuple[GuardedDescriptor, ...],
    retained: tuple[GuardedDescriptor, ...],
) -> None:
    """Shrink one active row while its owning close holds the fork guard."""

    observed = _ACTIVE_DESCRIPTORS.get(token)
    if observed != expected:
        raise RunOwnerForkGuardError(
            "active descriptor row changed before partial cleanup publication"
        )
    if any(item not in expected for item in retained):
        raise RunOwnerForkGuardError(
            "partial cleanup retained an identity outside its active lease"
        )
    if retained:
        _ACTIVE_DESCRIPTORS[token] = retained
    else:
        _ACTIVE_DESCRIPTORS.pop(token, None)
