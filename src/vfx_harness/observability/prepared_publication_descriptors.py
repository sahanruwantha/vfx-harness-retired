"""Exact descriptor retirement for opaque prepared-file transactions."""

from __future__ import annotations

import contextlib
import errno
import os
import signal
import stat
import threading
from collections.abc import Callable, Iterable, Iterator

from vfx_harness.observability import fork_coordination
from vfx_harness.observability.prepared_publication_capabilities import (
    PreparedPublicationRegistryError,
)
from vfx_harness.observability.run_owner_descriptor_identity import GuardedDescriptor


class PreparedDescriptorCleanupError(PreparedPublicationRegistryError):
    """An exact descriptor could not be retired without ambiguity."""


_BLOCKABLE_SIGNALS = signal.valid_signals() - {signal.SIGKILL, signal.SIGSTOP}
_NEUTRALIZER_DESCRIPTOR, _neutralizer_writer = os.pipe2(
    getattr(os, "O_CLOEXEC", 0)
)
os.close(_neutralizer_writer)
_NEUTRALIZER_IDENTITY = GuardedDescriptor.capture(_NEUTRALIZER_DESCRIPTOR)
_INERT_LOCK = threading.RLock()
_INERT_DESCRIPTORS: dict[int, GuardedDescriptor] = {}
_AMBIGUOUS_CLOSE_SLOTS: set[int] = set()
_DESCRIPTOR_GENERATION = object()
_PROCESS_POISON: str | None = None
_FORK_AUTHORITY_FAILURE_EXIT_CODE = 86


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


def _guard_state(guarded: GuardedDescriptor) -> str:
    """Observe an exact slot without collapsing an unreadable fd into closed."""

    try:
        observed = os.fstat(guarded.descriptor)
    except OSError as exc:
        if exc.errno == errno.EBADF:
            return "closed"
        raise PreparedDescriptorCleanupError(
            "prepared descriptor identity could not be observed safely"
        ) from exc
    if (
        observed.st_dev == guarded.device
        and observed.st_ino == guarded.inode
        and stat.S_IFMT(observed.st_mode) == guarded.file_type
        and observed.st_rdev == guarded.special_device
    ):
        return "current"
    return "different"


def _neutral_guard(descriptor: int) -> GuardedDescriptor:
    return GuardedDescriptor(
        descriptor=descriptor,
        device=_NEUTRALIZER_IDENTITY.device,
        inode=_NEUTRALIZER_IDENTITY.inode,
        file_type=_NEUTRALIZER_IDENTITY.file_type,
        special_device=_NEUTRALIZER_IDENTITY.special_device,
    )


def _guard_state_or_poison(
    guarded: GuardedDescriptor,
    *,
    reason: str,
) -> str:
    try:
        return _guard_state(guarded)
    except BaseException:
        _poison_process(reason)
        raise


def _poison_process(reason: str) -> None:
    global _PROCESS_POISON

    if _PROCESS_POISON is None:
        _PROCESS_POISON = reason


def _require_admission_locked() -> None:
    if _PROCESS_POISON is not None:
        raise PreparedDescriptorCleanupError(
            "prepared descriptor admission is poisoned after ambiguous cleanup: "
            f"{_PROCESS_POISON}; route to engineering without retrying this process"
        )


def require_prepared_descriptor_admission() -> None:
    """Refuse new preparation after a numeric close became ambiguous."""

    with fork_coordination.fork_coordinated_lock(_INERT_LOCK):
        _require_admission_locked()


def _register_neutral_descriptor(descriptor: int) -> GuardedDescriptor:
    if descriptor in _AMBIGUOUS_CLOSE_SLOTS:
        raise PreparedDescriptorCleanupError(
            "ambiguous neutral descriptor slot has permanently surrendered cleanup authority"
        )
    existing = _INERT_DESCRIPTORS.get(descriptor)
    if existing is not None:
        if _guard_state_or_poison(
            existing,
            reason="a registered neutral descriptor could not be observed",
        ) == "current":
            return existing
        _INERT_DESCRIPTORS.pop(descriptor, None)
        _AMBIGUOUS_CLOSE_SLOTS.add(descriptor)
        _poison_process(
            "a registered neutral descriptor changed identity before its one-shot close"
        )
        raise PreparedDescriptorCleanupError(
            "registered neutral prepared descriptor changed identity before cleanup"
        )
    if _guard_state_or_poison(
        _neutral_guard(descriptor),
        reason="a new neutral descriptor could not be observed",
    ) != "current":
        raise PreparedDescriptorCleanupError(
            "prepared descriptor did not become the private neutral identity"
        )
    inert = _neutral_guard(descriptor)
    _INERT_DESCRIPTORS[descriptor] = inert
    return inert


def _retire_inert_descriptor_once(guarded: GuardedDescriptor) -> None:
    """Make exactly one close attempt, surrendering all numeric-slot authority."""

    registered = _INERT_DESCRIPTORS.get(guarded.descriptor)
    if registered is not guarded:
        return
    close_started = False
    try:
        with block_deferred_signals():
            if _guard_state_or_poison(
                guarded,
                reason=(
                    "a registered neutral descriptor could not be observed "
                    "before close"
                ),
            ) != "current":
                _INERT_DESCRIPTORS.pop(guarded.descriptor, None)
                _AMBIGUOUS_CLOSE_SLOTS.add(guarded.descriptor)
                _poison_process(
                    "a registered neutral descriptor changed identity before "
                    "its one-shot close"
                )
                raise PreparedDescriptorCleanupError(
                    "neutral prepared descriptor changed identity before cleanup"
                )
            # Pop immediately before the syscall while signals remain deferred.
            # Once close begins, no return path may recover numeric-slot authority.
            _INERT_DESCRIPTORS.pop(guarded.descriptor, None)
            close_started = True
            os.close(guarded.descriptor)
    except BaseException as exc:
        if not close_started:
            raise
        _AMBIGUOUS_CLOSE_SLOTS.add(guarded.descriptor)
        _poison_process("a post-neutral descriptor close had an ambiguous outcome")
        raise PreparedDescriptorCleanupError(
            "neutral prepared descriptor close outcome is ambiguous"
        ) from exc


def _raise_retirement_error(
    error: BaseException,
    *diagnostics: BaseException | None,
) -> None:
    for diagnostic in diagnostics:
        if diagnostic is not None and diagnostic is not error:
            error.add_note(
                "cleanup diagnostic: "
                f"{type(diagnostic).__name__}: {diagnostic}"
            )
    raise error


def _finish_neutral_retirement(
    inert: GuardedDescriptor,
    *,
    release: Callable[[], None] | None,
    transition_error: BaseException | None,
    generation: object,
) -> bool:
    release_error: BaseException | None = None
    try:
        if release is not None:
            release()
    except BaseException as exc:
        release_error = exc
    if generation is not _DESCRIPTOR_GENERATION:
        failure = PreparedDescriptorCleanupError(
            "prepared descriptor retirement crossed a fork generation"
        )
        _raise_retirement_error(failure, release_error, transition_error)
    close_error: BaseException | None = None
    try:
        _retire_inert_descriptor_once(inert)
    except BaseException as exc:
        close_error = exc
    if close_error is not None:
        _raise_retirement_error(close_error, release_error, transition_error)
    if release_error is not None:
        _raise_retirement_error(release_error, transition_error)
    if transition_error is not None:
        raise transition_error
    return True


def _release_changed_authority(
    descriptor: int,
    release: Callable[[], None] | None,
    transition_error: BaseException | None,
) -> None:
    release_error: BaseException | None = None
    try:
        if release is not None:
            release()
    except BaseException as exc:
        release_error = exc
    _AMBIGUOUS_CLOSE_SLOTS.add(descriptor)
    _poison_process(
        "an exact prepared descriptor changed without becoming the private neutral identity"
    )
    failure = PreparedDescriptorCleanupError(
        "prepared descriptor authority changed to an unrelated numeric-slot occupant"
    )
    _raise_retirement_error(failure, release_error, transition_error)


def _surrender_unregistered_neutral(
    descriptor: int,
    release: Callable[[], None] | None,
    registration_error: BaseException,
) -> None:
    release_error: BaseException | None = None
    try:
        if release is not None:
            release()
    except BaseException as exc:
        release_error = exc
    _AMBIGUOUS_CLOSE_SLOTS.add(descriptor)
    _poison_process("a proven neutral descriptor could not acquire cleanup authority")
    failure = PreparedDescriptorCleanupError(
        "neutral prepared descriptor cleanup authority was permanently surrendered"
    )
    _raise_retirement_error(failure, registration_error, release_error)


def drain_inert_descriptors() -> None:
    """Close registered neutrals before admitting another preparation."""

    with fork_coordination.fork_coordinated_lock(_INERT_LOCK):
        _require_admission_locked()
        for guarded in tuple(_INERT_DESCRIPTORS.values()):
            _retire_inert_descriptor_once(guarded)


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
    """Replace exact authority with a neutral fd before releasing its owner.

    A neutral fd is closed at most once. If that close reports failure, the
    process is permanently poisoned for later prepared acquisitions; cleanup of
    other already-owned exact descriptors remains legal so locks can be released.
    """

    with fork_coordination.fork_coordinated_lock(_INERT_LOCK):
        generation = _DESCRIPTOR_GENERATION
        existing = _INERT_DESCRIPTORS.get(guarded.descriptor)
        if existing is not None:
            if _guard_state_or_poison(
                existing,
                reason="a registered neutral descriptor could not be observed",
            ) == "current":
                return _finish_neutral_retirement(
                    existing,
                    release=release,
                    transition_error=None,
                    generation=generation,
                )
            _INERT_DESCRIPTORS.pop(guarded.descriptor, None)
            _AMBIGUOUS_CLOSE_SLOTS.add(guarded.descriptor)
            _poison_process(
                "a registered neutral descriptor changed identity before cleanup"
            )
        authority_state = _guard_state_or_poison(
            guarded,
            reason="exact prepared descriptor authority could not be observed",
        )
        if authority_state != "current":
            if release is not None:
                release()
            return False
        if _guard_state_or_poison(
            _NEUTRALIZER_IDENTITY,
            reason="the private prepared descriptor neutralizer could not be observed",
        ) != "current":
            _poison_process("the private prepared descriptor neutralizer is not live")
            raise PreparedDescriptorCleanupError(
                "prepared descriptor neutralizer is no longer live"
            )
        transition_error: BaseException | None = None
        with block_deferred_signals():
            try:
                os.dup2(
                    _NEUTRALIZER_DESCRIPTOR,
                    guarded.descriptor,
                    inheritable=False,
                )
            except BaseException as exc:
                authority_state = _guard_state_or_poison(
                    guarded,
                    reason=(
                        "exact prepared descriptor state became unreadable after "
                        "neutralization"
                    ),
                )
                if authority_state == "current":
                    raise
                transition_error = exc
            if generation is not _DESCRIPTOR_GENERATION:
                raise PreparedDescriptorCleanupError(
                    "prepared descriptor neutralization crossed a fork generation"
                )
            authority_state = _guard_state_or_poison(
                guarded,
                reason=(
                    "exact prepared descriptor state became unreadable after "
                    "neutralization"
                ),
            )
            if authority_state == "current":
                _poison_process(
                    "descriptor neutralization returned without replacing exact authority"
                )
                raise PreparedDescriptorCleanupError(
                    "prepared descriptor neutralization did not replace exact authority"
                )
            neutral_state = _guard_state_or_poison(
                _neutral_guard(guarded.descriptor),
                reason="neutralized prepared descriptor could not be observed",
            )
            if neutral_state != "current":
                _release_changed_authority(
                    guarded.descriptor,
                    release,
                    transition_error,
                )
            try:
                inert = _register_neutral_descriptor(guarded.descriptor)
            except BaseException as exc:
                _surrender_unregistered_neutral(
                    guarded.descriptor,
                    release,
                    exc,
                )
            return _finish_neutral_retirement(
                inert,
                release=release,
                transition_error=transition_error,
                generation=generation,
            )


def _fork_child_neutralize_authority(
    guarded: GuardedDescriptor,
) -> GuardedDescriptor | None:
    with contextlib.suppress(BaseException), block_deferred_signals():
        os.dup2(
            _NEUTRALIZER_DESCRIPTOR,
            guarded.descriptor,
            inheritable=False,
        )
    try:
        authority_state = _guard_state(guarded)
    except BaseException:
        os._exit(_FORK_AUTHORITY_FAILURE_EXIT_CODE)
    if authority_state == "current":
        os._exit(_FORK_AUTHORITY_FAILURE_EXIT_CODE)
    try:
        neutral_state = _guard_state(_neutral_guard(guarded.descriptor))
    except BaseException:
        os._exit(_FORK_AUTHORITY_FAILURE_EXIT_CODE)
    if neutral_state != "current":
        _AMBIGUOUS_CLOSE_SLOTS.add(guarded.descriptor)
        _poison_process(
            "fork-child authority changed without becoming the private neutral identity"
        )
        return None
    try:
        inert = _register_neutral_descriptor(guarded.descriptor)
    except BaseException:
        _AMBIGUOUS_CLOSE_SLOTS.add(guarded.descriptor)
        _poison_process("fork-child neutral descriptor registration failed")
        return None
    return inert


def neutralize_fork_child_descriptors(
    descriptors: Iterable[GuardedDescriptor],
) -> None:
    """Release every inherited exact authority or terminate the fork child."""

    with (
        fork_coordination.fork_coordinated_lock(_INERT_LOCK),
        block_deferred_signals(),
    ):
        by_descriptor: dict[int, list[GuardedDescriptor]] = {}
        for guarded in descriptors:
            by_descriptor.setdefault(guarded.descriptor, []).append(guarded)

        # Phase one releases all exact inherited authority. No neutral close is
        # attempted until every lock-bearing descriptor has crossed this gate.
        neutrals: dict[int, GuardedDescriptor] = {}
        for descriptor in sorted(by_descriptor, reverse=True):
            try:
                states = tuple(
                    (guarded, _guard_state(guarded))
                    for guarded in by_descriptor[descriptor]
                )
            except BaseException:
                os._exit(_FORK_AUTHORITY_FAILURE_EXIT_CODE)
            current = next(
                (guarded for guarded, state in states if state == "current"),
                None,
            )
            if current is not None:
                try:
                    neutralizer_state = _guard_state(_NEUTRALIZER_IDENTITY)
                except BaseException:
                    os._exit(_FORK_AUTHORITY_FAILURE_EXIT_CODE)
                if neutralizer_state != "current":
                    os._exit(_FORK_AUTHORITY_FAILURE_EXIT_CODE)
                inert = _fork_child_neutralize_authority(current)
                if inert is not None:
                    neutrals[descriptor] = inert
            else:
                try:
                    neutral_state = _guard_state(_neutral_guard(descriptor))
                except BaseException:
                    # An unreadable inherited slot cannot prove that authority
                    # was released; the child must not continue.
                    os._exit(_FORK_AUTHORITY_FAILURE_EXIT_CODE)
                if neutral_state == "current":
                    # Identity cannot prove who duplicated this neutral open-file
                    # description before the callback. Only neutrals minted by
                    # this callback carry one-shot close authority.
                    _AMBIGUOUS_CLOSE_SLOTS.add(descriptor)
                    _poison_process(
                        "fork child inherited an unowned same-neutralizer descriptor"
                    )

        # Phase two performs one close attempt per proven neutral. Ambiguity
        # poisons future admission but can never revive original authority.
        for inert in neutrals.values():
            with contextlib.suppress(BaseException):
                _retire_inert_descriptor_once(inert)


def _after_fork_child() -> None:
    global _DESCRIPTOR_GENERATION, _INERT_LOCK

    _DESCRIPTOR_GENERATION = object()
    for guarded in tuple(_INERT_DESCRIPTORS.values()):
        with contextlib.suppress(BaseException):
            _retire_inert_descriptor_once(guarded)
    _INERT_LOCK = threading.RLock()


fork_coordination.register_fork_participant(
    "observability.prepared_publication_descriptors",
    lock_factory=lambda: _INERT_LOCK,
    after_in_child=_after_fork_child,
)


__all__ = [
    "PreparedDescriptorCleanupError",
    "block_deferred_signals",
    "drain_inert_descriptors",
    "neutralize_fork_child_descriptors",
    "neutralize_guarded_descriptor",
    "require_prepared_descriptor_admission",
    "write_all",
]
