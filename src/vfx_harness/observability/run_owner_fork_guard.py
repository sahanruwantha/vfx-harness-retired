"""Fork-safe descriptor ownership from acquisition start through lease cleanup."""

from __future__ import annotations

import errno
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from vfx_harness.observability import fork_coordination
from vfx_harness.observability import run_owner_fork_registry as _registry
from vfx_harness.observability.run_owner_descriptor_identity import (
    PROCESS_CLEANUP_POISON,
    GuardedDescriptor,
    RunOwnerForkGuardCleanupError,
    RunOwnerForkGuardError,
    record_unproven_identity,
)


def _after_fork_child() -> None:
    if _registry.UNPROVEN_DESCRIPTORS:
        # The parent could not read or neutralize these slots; the child cannot
        # prove that inherited authority is released either.
        os._exit(_registry.CHILD_AUTHORITY_CLEANUP_EXIT)
    candidates = (
        *(
            guarded
            for rows in _registry.PENDING_DESCRIPTORS.values()
            for guarded in rows
        ),
        *(
            guarded
            for rows in _registry.ACTIVE_DESCRIPTORS.values()
            for guarded in rows
        ),
        *(record.guarded for record in _registry.INERT_DESCRIPTORS.values()),
    )
    live_by_descriptor: dict[int, GuardedDescriptor] = {}
    try:
        for guarded in candidates:
            if guarded.is_current():
                live_by_descriptor[guarded.descriptor] = guarded
    except BaseException:
        os._exit(_registry.CHILD_AUTHORITY_CLEANUP_EXIT)
    _registry.PENDING_DESCRIPTORS.clear()
    _registry.ACTIVE_DESCRIPTORS.clear()
    _registry.INERT_DESCRIPTORS.clear()
    _registry.INERT_CLOSES_IN_FLIGHT.clear()
    _registry.ORPHANED_AUTHORITY_TOKENS.clear()
    if not live_by_descriptor:
        return

    child_token = object()
    guarded = tuple(
        live_by_descriptor[descriptor]
        for descriptor in sorted(live_by_descriptor)
    )
    _registry.ACTIVE_DESCRIPTORS[child_token] = guarded
    _registry.ORPHANED_AUTHORITY_TOKENS.add(child_token)
    try:
        neutralize_active_descriptors(child_token, guarded)
    except BaseException:
        try:
            retained_authority = tuple(
                item
                for item in _registry.ACTIVE_DESCRIPTORS.get(child_token, ())
                if item.is_current()
            )
        except BaseException:
            os._exit(_registry.CHILD_AUTHORITY_CLEANUP_EXIT)
        if retained_authority:
            # A child that pins inherited authority can deadlock the parent even
            # if it never performs another harness operation. There is no caller
            # stack to route through, so terminate deterministically.
            os._exit(_registry.CHILD_AUTHORITY_CLEANUP_EXIT)
    finally:
        if not _registry.ACTIVE_DESCRIPTORS.get(child_token):
            _registry.ORPHANED_AUTHORITY_TOKENS.discard(child_token)

fork_coordination.register_fork_participant(
    "observability.run_owner_fork_guard",
    lock_factory=None,
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
                for guarded in _registry.ACTIVE_DESCRIPTORS.get(self.token, ())
            )
            == descriptors
        )

    def cleanup_is_complete(self) -> bool:
        """Report that no pending or active descriptor row remains."""

        return (
            self.belongs_to_current_process
            and self._finished
            and self.token not in _registry.PENDING_DESCRIPTORS
            and self.token not in _registry.ACTIVE_DESCRIPTORS
        )

    def _pending(self) -> list[GuardedDescriptor]:
        if not self.belongs_to_current_process:
            raise RunOwnerForkGuardError("forked child cannot continue its parent's descriptor acquisition")
        try:
            return _registry.PENDING_DESCRIPTORS[self.token]
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
                try:
                    rows.append(GuardedDescriptor.capture(descriptor))
                except OSError as capture_error:
                    if capture_error.errno == errno.EBADF:
                        raise RunOwnerForkGuardError(
                            "descriptor acquisition opener returned a closed "
                            f"descriptor {descriptor}"
                        ) from adoption_error
                    # The identity is unreadable, so no registry row can be
                    # built.  The slot is still ours: release it now rather
                    # than leaving it live, untracked, and fork-inheritable.
                    _neutralize_unproven_slot(
                        descriptor,
                        capture_error,
                        adoption_error,
                    )
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

        with _registry.block_descriptor_cleanup_signals():
            descriptor = opener()
            self.adopt_descriptor(descriptor)
            return descriptor

    def guarded_descriptors(
        self,
        descriptors: tuple[int, ...],
    ) -> tuple[GuardedDescriptor, ...]:
        """Return the captured identities for an exact pending or active set."""

        if not self.belongs_to_current_process:
            raise RunOwnerForkGuardError(
                "forked child cannot inspect its parent's descriptor acquisition"
            )
        rows = _registry.PENDING_DESCRIPTORS.get(self.token)
        if rows is None:
            active = _registry.ACTIVE_DESCRIPTORS.get(self.token)
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
        close_error: BaseException | None = None
        with _registry.block_descriptor_cleanup_signals():
            try:
                os.dup2(
                    _registry.NEUTRALIZER_DESCRIPTOR,
                    descriptor,
                    inheritable=False,
                )
            except BaseException as exc:
                raise RunOwnerForkGuardError(
                    "could not safely neutralize a pending descriptor"
                ) from exc
            if not _descriptor_matches(descriptor, _registry.NEUTRALIZER_IDENTITY):
                raise RunOwnerForkGuardError(
                    "pending descriptor did not become the exact neutral identity"
                )
            rows.remove(guarded)
            _registry.INERT_CLOSES_IN_FLIGHT.add(descriptor)
            try:
                os.close(descriptor)
            except BaseException as exc:
                # The raw close may have succeeded before raising.  Never retain
                # or retry this numeric slot; it now names at worst neutral data.
                close_error = exc
                PROCESS_CLEANUP_POISON.append(exc)
            finally:
                _registry.INERT_CLOSES_IN_FLIGHT.discard(descriptor)
        if close_error is not None:
            raise RunOwnerForkGuardError(
                "pending descriptor neutral close had an ambiguous outcome; "
                "the process is poisoned and no numeric slot will be retried"
            ) from close_error

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
            _registry.ACTIVE_DESCRIPTORS[self.token] = guarded_descriptors
            del _registry.PENDING_DESCRIPTORS[self.token]
            self._finished = True
        except BaseException:
            # The creator still owns the fork barrier here, so restore the complete
            # pending state before exposing the failure to its abort path. This
            # closes every interruption point between the two registry rows.
            self._finished = False
            _registry.ACTIVE_DESCRIPTORS.pop(self.token, None)
            _registry.PENDING_DESCRIPTORS[self.token] = list(guarded_descriptors)
            raise
        if not self.guard_managed:
            fork_coordination.release_fork_barrier()
        return self.token

    def abort(self) -> None:
        if self._finished:
            return
        self._finished = True
        if not self.belongs_to_current_process:
            # The child callback closed inherited descriptors, cleared the copied
            # registry, and replaced its lock. It must not release the new lock.
            return
        _registry.PENDING_DESCRIPTORS.pop(self.token, None)
        if not self.guard_managed:
            fork_coordination.release_fork_barrier()

def begin_fork_protected_acquisition() -> ForkProtectedAcquisition:
    """Exclude concurrent fork and expose every opened fd to child cleanup."""

    fork_coordination.acquire_fork_barrier()
    try:
        _registry.require_descriptor_acquisition_admission_locked()
    except BaseException:
        fork_coordination.release_fork_barrier()
        raise
    token = object()
    _registry.PENDING_DESCRIPTORS[token] = []
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
    with fork_coordination.fork_barrier():
        _registry.require_descriptor_acquisition_admission_locked()
        body_error: BaseException | None = None
        try:
            _registry.PENDING_DESCRIPTORS[token] = []
            yield acquisition
        except BaseException as exc:
            body_error = exc
        pending = _registry.PENDING_DESCRIPTORS.get(token)
        cleanup_errors: list[BaseException] = []
        if pending is not None:
            for guarded in tuple(reversed(pending)):
                try:
                    acquisition.retire(guarded.descriptor)
                except BaseException as exc:
                    cleanup_errors.append(exc)
            remaining = tuple(
                guarded.descriptor
                for guarded in _registry.PENDING_DESCRIPTORS.get(token, ())
            )
            if remaining:
                try:
                    acquisition.handoff(remaining)
                except BaseException as exc:
                    cleanup_errors.append(exc)
                pending_after_handoff = _registry.PENDING_DESCRIPTORS.pop(token, None)
                if pending_after_handoff:
                    _registry.ACTIVE_DESCRIPTORS[token] = tuple(pending_after_handoff)
                    acquisition._finished = True
                if _registry.ACTIVE_DESCRIPTORS.get(token):
                    _registry.ORPHANED_AUTHORITY_TOKENS.add(token)
            else:
                acquisition.abort()
        if cleanup_errors:
            retained = tuple(_registry.ACTIVE_DESCRIPTORS.get(token, ()))
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

    fork_coordination.acquire_fork_barrier()
    invariant_error: RunOwnerForkGuardError | None = None
    body_error: BaseException | None = None
    unproven_error: RunOwnerForkGuardError | None = None
    visible = descriptors
    remaining: tuple[GuardedDescriptor, ...] = ()
    try:
        registered = _registry.ACTIVE_DESCRIPTORS.get(token)
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
        _registry.ACTIVE_DESCRIPTORS[token] = visible
        try:
            yield
        except BaseException as exc:
            body_error = exc
    finally:
        try:
            live: list[GuardedDescriptor] = []
            for guarded in visible:
                try:
                    current = guarded.is_current()
                except RunOwnerForkGuardError as exc:
                    # Unreadable is not closed: keep the row and report it.
                    current = True
                    if unproven_error is None:
                        unproven_error = exc
                if current:
                    live.append(guarded)
            remaining = tuple(live)
            if remaining:
                _registry.ACTIVE_DESCRIPTORS[token] = remaining
            else:
                _registry.ACTIVE_DESCRIPTORS.pop(token, None)
        finally:
            fork_coordination.release_fork_barrier()
    if unproven_error is not None:
        failure = RunOwnerForkGuardCleanupError(
            (unproven_error, *(() if invariant_error is None else (invariant_error,))),
            retained_authority=remaining,
            retained_unproven=tuple(sorted(_registry.UNPROVEN_DESCRIPTORS)),
        )
        if body_error is not None:
            failure.add_note(
                f"primary cleanup failure: {type(body_error).__name__}: {body_error}"
            )
            raise failure from body_error
        raise failure
    if body_error is not None:
        if invariant_error is not None:
            body_error.add_note(f"fork registry diagnostic: {invariant_error}")
        raise body_error
    if invariant_error is not None:
        raise invariant_error

def _descriptor_matches(
    descriptor: int,
    expected: GuardedDescriptor,
) -> bool:
    return expected.rebased(descriptor).is_current()

def _neutralize_unproven_slot(
    descriptor: int,
    capture_error: OSError,
    adoption_error: BaseException,
) -> None:
    """Release a caller-owned slot whose identity cannot be read, then raise.

    The process is poisoned first: an unreadable identity is never treated as
    absence.  The slot is then replaced with the private neutral identity under
    blocked signals and closed exactly once.  If the replacement cannot be
    proven, the numeric slot stays retained as unproven so admission refuses
    new acquisitions and a forked child terminates instead of inheriting it.
    """

    failure = record_unproven_identity(descriptor, capture_error)
    failure.__cause__ = capture_error
    with _registry.block_descriptor_cleanup_signals():
        _registry.UNPROVEN_DESCRIPTORS.add(descriptor)
        try:
            os.dup2(_registry.NEUTRALIZER_DESCRIPTOR, descriptor, inheritable=False)
            neutralized = _descriptor_matches(descriptor, _registry.NEUTRALIZER_IDENTITY)
        except BaseException as exc:
            cleanup_failure = RunOwnerForkGuardCleanupError(
                (failure, exc),
                retained_unproven=(descriptor,),
            )
            cleanup_failure.add_note(
                "adoption diagnostic: "
                f"{type(adoption_error).__name__}: {adoption_error}"
            )
            raise cleanup_failure from exc
        if not neutralized:
            raise RunOwnerForkGuardCleanupError(
                (
                    failure,
                    RunOwnerForkGuardError(
                        "unproven descriptor did not become the exact neutral identity"
                    ),
                ),
                retained_unproven=(descriptor,),
            ) from adoption_error
        _registry.UNPROVEN_DESCRIPTORS.discard(descriptor)
        # One raw close for a slot that now names only neutral data; a raised
        # close is recorded and never retried against this numeric slot.
        _registry.INERT_CLOSES_IN_FLIGHT.add(descriptor)
        try:
            os.close(descriptor)
        except BaseException as exc:
            PROCESS_CLEANUP_POISON.append(exc)
            failure.add_note(
                f"neutral close diagnostic: {type(exc).__name__}: {exc}"
            )
        finally:
            _registry.INERT_CLOSES_IN_FLIGHT.discard(descriptor)
    raise failure from adoption_error

def neutralize_active_descriptors(
    token: object,
    descriptors: tuple[GuardedDescriptor, ...],
) -> None:
    """Release exact active fds before any ambiguous raw-close outcome.

    Each live authority descriptor is first replaced with the process-private
    neutral identity.  The active fork row is then shrunk while the numeric
    slot is still occupied, so a later close that succeeds before raising can
    never make a same-inode reused slot look like the original lease.  An inert
    close that fails without taking effect remains registered for engineering;
    it is never retried ambiguously in this call.  Every failure surfaces as a
    typed cleanup error whose retained rows are computed conservatively: a slot
    whose identity cannot be read is retained, never assumed closed.
    """

    try:
        _neutralize_active_descriptors(token, descriptors)
    except RunOwnerForkGuardCleanupError:
        raise
    except BaseException as exc:
        with fork_coordination.fork_barrier():
            retained_authority, retained_neutral = _registry.retained_rows_locked(token)
            retained_unproven = tuple(sorted(_registry.UNPROVEN_DESCRIPTORS))
        raise RunOwnerForkGuardCleanupError(
            (exc,),
            retained_authority=retained_authority,
            retained_neutral=retained_neutral,
            retained_unproven=retained_unproven,
        ) from exc

def _neutralize_active_descriptors(
    token: object,
    descriptors: tuple[GuardedDescriptor, ...],
) -> None:
    errors: list[BaseException] = []
    with fork_coordination.fork_barrier():
        # A retry for this exact cleanup token may safely drain only neutral
        # rows that the same prior transition retained before any raw close.
        errors.extend(_registry.drain_inert_descriptors_locked(owner_token=token))
        retained, reconciliation_error = _registry.reconcile_active_descriptors_locked(
            token,
            descriptors,
        )
        if reconciliation_error is not None:
            errors.append(reconciliation_error)
        if not _registry.NEUTRALIZER_IDENTITY.is_current():
            raise RunOwnerForkGuardCleanupError(
                (RunOwnerForkGuardError("descriptor neutralizer is no longer live"),),
                retained_authority=retained,
            )
        for guarded in reversed(tuple(retained)):
            if guarded not in retained:
                continue
            if not guarded.is_current():
                errors.append(
                    RunOwnerForkGuardError(
                        "active descriptor changed before neutralization"
                    )
                )
                retained = _registry.publish_neutralized_descriptor(
                    token,
                    retained,
                    guarded,
                )
                continue

            inert = _registry.neutral_descriptor(guarded.descriptor)
            neutralized = False
            authority_released = False
            transition_aborted = False
            try:
                with _registry.block_descriptor_cleanup_signals():
                    _registry.INERT_DESCRIPTORS[inert.descriptor] = (
                        _registry.InertDescriptorRecord(
                            guarded=inert,
                            owner_token=token,
                            in_flight=True,
                        )
                    )
                    try:
                        os.dup2(
                            _registry.NEUTRALIZER_DESCRIPTOR,
                            guarded.descriptor,
                            inheritable=False,
                        )
                    except BaseException as exc:
                        errors.append(exc)
                        transition_aborted = True
                    else:
                        authority_released = True

                    try:
                        neutralized = _descriptor_matches(
                            guarded.descriptor,
                            _registry.NEUTRALIZER_IDENTITY,
                        )
                    except BaseException as exc:
                        errors.append(exc)
                        transition_aborted = True
                        neutralized = inert.is_current()

                    authority_released = authority_released or neutralized
                    if authority_released:
                        retained = tuple(
                            item for item in retained if item != guarded
                        )
                        observed = _registry.ACTIVE_DESCRIPTORS.get(token, ())
                        active_retained = tuple(
                            item for item in observed if item != guarded
                        )
                        if active_retained:
                            _registry.ACTIVE_DESCRIPTORS[token] = active_retained
                        else:
                            _registry.ACTIVE_DESCRIPTORS.pop(token, None)

                    if not neutralized:
                        _registry.INERT_DESCRIPTORS.pop(inert.descriptor, None)
                        if authority_released:
                            errors.append(
                                RunOwnerForkGuardError(
                                    "neutralized authority descriptor was rebound "
                                    "before exact cleanup"
                                )
                            )
                        continue

                    if transition_aborted:
                        _registry.INERT_DESCRIPTORS[inert.descriptor] = (
                            _registry.InertDescriptorRecord(
                                guarded=inert,
                                owner_token=token,
                                in_flight=False,
                            )
                        )
                        continue

                    # Remove all future close authority before the sole raw-close
                    # attempt. A raised close can mean either outcome and the fd
                    # number can already have been reused with identical stat data.
                    _registry.INERT_DESCRIPTORS.pop(inert.descriptor, None)
                    _registry.INERT_CLOSES_IN_FLIGHT.add(inert.descriptor)
                    try:
                        os.close(inert.descriptor)
                    except BaseException as exc:
                        errors.append(exc)
                        PROCESS_CLEANUP_POISON.append(exc)
                    finally:
                        _registry.INERT_CLOSES_IN_FLIGHT.discard(inert.descriptor)
            except BaseException as exc:
                errors.append(exc)
                current = _registry.INERT_DESCRIPTORS.get(inert.descriptor)
                if current is not None and current.in_flight:
                    if inert.is_current():
                        _registry.INERT_DESCRIPTORS[inert.descriptor] = (
                            _registry.InertDescriptorRecord(
                                guarded=inert,
                                owner_token=token,
                                in_flight=False,
                            )
                        )
                    else:
                        _registry.INERT_DESCRIPTORS.pop(inert.descriptor, None)
                if authority_released:
                    retained = tuple(item for item in retained if item != guarded)
                    observed = _registry.ACTIVE_DESCRIPTORS.get(token, ())
                    active_retained = tuple(
                        item for item in observed if item != guarded
                    )
                    if active_retained:
                        _registry.ACTIVE_DESCRIPTORS[token] = active_retained
                    else:
                        _registry.ACTIVE_DESCRIPTORS.pop(token, None)
        retained = tuple(
            item
            for item in _registry.ACTIVE_DESCRIPTORS.get(token, ())
            if item.is_current()
        )
        if retained:
            _registry.ACTIVE_DESCRIPTORS[token] = retained
        else:
            _registry.ACTIVE_DESCRIPTORS.pop(token, None)
        inert_retained = _registry.live_inert_descriptors_locked(
            owner_token=token,
        )
    if errors or retained or inert_retained:
        raise RunOwnerForkGuardCleanupError(
            tuple(errors),
            retained_authority=retained,
            retained_neutral=inert_retained,
        )

def drain_inert_descriptors() -> None:
    """Make one close attempt for rows that have never been closed before."""

    with fork_coordination.fork_barrier():
        errors = (
            *_registry.drain_inert_descriptors_locked(),
            *PROCESS_CLEANUP_POISON,
        )
        retained = _registry.live_inert_descriptors_locked()
    if errors or retained:
        raise RunOwnerForkGuardCleanupError(
            tuple(errors),
            retained_neutral=retained,
        )
