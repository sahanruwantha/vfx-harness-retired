"""Process-wide identity and fork guard for authority-selection leases."""

from __future__ import annotations

import fcntl
import os
import secrets
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from threading import get_ident

import vfx_harness.orchestration.builder_execution_fence as builder_execution_fence
from vfx_harness.observability.run_owner_fork_guard import (
    ForkProtectedAcquisition,
    GuardedDescriptor,
    active_descriptor_close,
    replace_active_descriptors_locked,
)

_DIRECTORY_OPEN_FLAGS = (
    os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
)
_REGISTRY_LOCK = threading.RLock()
_REGISTRY: dict[str, DescriptorLeaseRegistration] = {}
_SHOT_MUTEX_GUARD = threading.Lock()
_SHOT_MUTEXES: dict[tuple[int, int], _ShotProcessMutexEntry] = {}
_PROCESS_TOKEN = secrets.token_hex(16)


class AuthoritySelectionConflict(ValueError):
    """Authority selection changed or its low-level storage is unsafe."""


class AuthoritySelectionCleanupFailure(AuthoritySelectionConflict):
    """Exact cleanup failed and may retain live authority descriptors."""

    def __init__(
        self,
        *,
        errors: tuple[BaseException, ...],
        retained: tuple[GuardedDescriptor, ...],
        body_error: BaseException | None = None,
    ) -> None:
        self.errors = errors
        self.retained = retained
        self.body_error = body_error
        super().__init__(
            "authority-selection cleanup failed; "
            f"retained_descriptors={tuple(item.descriptor for item in retained)!r}; "
            "route to engineering before retrying publication"
        )


def canonical_authority_shot_path(shot_folder: str | Path) -> Path:
    """Resolve and validate the one canonical absolute shot-root spelling."""

    raw = Path(shot_folder).expanduser()
    absolute = os.path.abspath(raw)
    if os.name == "posix" and absolute.startswith("//"):
        # POSIX permits implementation-defined handling for exactly two leading
        # slashes.  This local-filesystem backend deliberately has one root
        # namespace, so //shot and /shot must not mint distinct capabilities.
        absolute = f"/{absolute.lstrip('/')}"
    shot = Path(absolute)
    descriptor: int | None = None
    try:
        descriptor = os.open(shot.anchor, _DIRECTORY_OPEN_FLAGS)
        for part in shot.parts[1:]:
            following = os.open(part, _DIRECTORY_OPEN_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = following
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise AuthoritySelectionConflict(
            f"authority-selection shot root and ancestors must be existing real directories: {shot}"
        ) from exc
    try:
        assert descriptor is not None
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise AuthoritySelectionConflict(f"authority-selection shot root must be a directory: {shot}")
    finally:
        os.close(descriptor)
    return shot


def open_authority_directory_parts(
    shot: Path,
    parts: tuple[str, ...],
    *,
    create: bool,
    missing_ok: bool = False,
) -> int | None:
    """Open an exact descriptor-relative directory chain without symlinks."""

    current: int | None = None
    try:
        current = os.open(shot.anchor, _DIRECTORY_OPEN_FLAGS)
        for part in shot.parts[1:]:
            following = os.open(part, _DIRECTORY_OPEN_FLAGS, dir_fd=current)
            os.close(current)
            current = following
    except OSError as exc:
        if current is not None:
            os.close(current)
        raise AuthoritySelectionConflict(f"authority-selection shot root became unsafe: {shot}") from exc
    assert current is not None
    try:
        for part in parts:
            try:
                following = os.open(part, _DIRECTORY_OPEN_FLAGS, dir_fd=current)
            except FileNotFoundError:
                if missing_ok:
                    os.close(current)
                    return None
                if not create:
                    raise AuthoritySelectionConflict(f"authority-selection directory is missing: {part}") from None
                try:
                    os.mkdir(part, mode=0o700, dir_fd=current)
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise AuthoritySelectionConflict(f"cannot create authority-selection directory: {part}") from exc
                try:
                    following = os.open(
                        part,
                        _DIRECTORY_OPEN_FLAGS,
                        dir_fd=current,
                    )
                except OSError as exc:
                    raise AuthoritySelectionConflict(
                        f"authority-selection component is not a real directory: {part}"
                    ) from exc
            except OSError as exc:
                raise AuthoritySelectionConflict(
                    f"authority-selection component is not a real directory: {part}"
                ) from exc
            if create:
                try:
                    os.fsync(following)
                    os.fsync(current)
                except BaseException:
                    os.close(following)
                    raise
            os.close(current)
            current = following
        return current
    except BaseException:
        os.close(current)
        raise


def require_selection_lease_identity(
    *,
    shot: Path,
    shot_descriptor: int,
    lock_parent_descriptor: int,
    lock_descriptor: int,
    lock_relative: Path,
    phase: str,
) -> None:
    """Reopen and compare shot root, lock parent, and lock inode identities."""

    current_shot: int | None = None
    final_shot: int | None = None
    current_parent: int | None = None
    try:
        current_shot = open_authority_directory_parts(shot, (), create=False)
        assert current_shot is not None
        current_parent = os.dup(current_shot)
        for part in lock_relative.parts[:-1]:
            following = os.open(
                part,
                _DIRECTORY_OPEN_FLAGS,
                dir_fd=current_parent,
            )
            os.close(current_parent)
            current_parent = following
        final_shot = open_authority_directory_parts(shot, (), create=False)
        assert final_shot is not None
        live_shot = os.fstat(current_shot)
        final_live_shot = os.fstat(final_shot)
        held_shot = os.fstat(shot_descriptor)
        live_parent = os.fstat(current_parent)
        held_parent = os.fstat(lock_parent_descriptor)
        live_lock = os.stat(
            lock_relative.name,
            dir_fd=current_parent,
            follow_symlinks=False,
        )
        held_lock = os.fstat(lock_descriptor)
        if (
            not stat.S_ISREG(live_lock.st_mode)
            or (live_shot.st_dev, live_shot.st_ino) != (held_shot.st_dev, held_shot.st_ino)
            or (final_live_shot.st_dev, final_live_shot.st_ino) != (held_shot.st_dev, held_shot.st_ino)
            or (live_parent.st_dev, live_parent.st_ino) != (held_parent.st_dev, held_parent.st_ino)
            or (live_lock.st_dev, live_lock.st_ino) != (held_lock.st_dev, held_lock.st_ino)
        ):
            raise AuthoritySelectionConflict(f"authority selection lock path changed {phase}")
    except AuthoritySelectionConflict:
        raise
    except OSError as exc:
        raise AuthoritySelectionConflict(f"authority selection lock path changed {phase}") from exc
    finally:
        if current_parent is not None:
            os.close(current_parent)
        if final_shot is not None:
            os.close(final_shot)
        if current_shot is not None:
            os.close(current_shot)


@dataclass(frozen=True, slots=True)
class DescriptorLeaseRegistration:
    """Exact descriptors owned by one process/thread selection-lock lease."""

    lease_id: str
    process_id: int
    process_token: str
    thread_id: int
    descriptors: tuple[int, ...]
    descriptor_identities: tuple[GuardedDescriptor, ...]
    fork_token: object


@dataclass(slots=True)
class DescriptorCloseProof:
    """Exact identities still live after one registered cleanup transaction."""

    registration: DescriptorLeaseRegistration
    retained: tuple[GuardedDescriptor, ...] = ()
    published: bool = False

    def publish_retained(
        self,
        retained: tuple[GuardedDescriptor, ...],
    ) -> None:
        """Atomically shrink fork and local rows before closing safe replacements."""

        if self.published:
            raise RuntimeError(
                "authority descriptor cleanup published its retained subset twice"
            )
        replace_active_descriptors_locked(
            self.registration.fork_token,
            self.registration.descriptor_identities,
            retained,
        )
        observed = _REGISTRY.get(self.registration.lease_id)
        if observed != self.registration:
            raise RuntimeError(
                "authority descriptor lease changed before partial cleanup publication"
            )
        try:
            if retained:
                _REGISTRY[self.registration.lease_id] = DescriptorLeaseRegistration(
                    lease_id=self.registration.lease_id,
                    process_id=self.registration.process_id,
                    process_token=self.registration.process_token,
                    thread_id=self.registration.thread_id,
                    descriptors=tuple(item.descriptor for item in retained),
                    descriptor_identities=retained,
                    fork_token=self.registration.fork_token,
                )
            else:
                del _REGISTRY[self.registration.lease_id]
        except BaseException:
            # Restore the fork-visible row before surfacing an interrupted local
            # update. The caller's finally path may then retry exact publication.
            replace_active_descriptors_locked(
                self.registration.fork_token,
                retained,
                self.registration.descriptor_identities,
            )
            _REGISTRY[self.registration.lease_id] = self.registration
            raise
        self.retained = retained
        self.published = True


@dataclass(slots=True)
class ProvisionalDescriptorRegistration:
    """One local row that becomes durable only after the fork handoff commits."""

    registration: DescriptorLeaseRegistration
    acquisition: ForkProtectedAcquisition
    committed: bool = False

    def commit(self) -> None:
        if not self.acquisition.active_handoff_matches(
            self.registration.descriptors
        ):
            raise RuntimeError(
                "authority descriptor registration cannot commit before its exact fork handoff"
            )
        self.committed = True


def current_process_token() -> str:
    return _PROCESS_TOKEN


@contextmanager
def process_bound_live_lock_identity(lock_path: Path) -> Iterator[None]:
    """Retain the crash-safe identity until exact-process context cleanup."""

    claim = builder_execution_fence.arm_live_path_identity(lock_path, lock_path)
    try:
        try:
            claim.acquire()
        except builder_execution_fence.BuilderExecutionFenceActive as exc:
            raise AuthoritySelectionConflict(
                "live lock identity is already held through another filesystem inode; "
                f"the lock path may have been replaced: {lock_path}"
            ) from exc
        except builder_execution_fence.BuilderExecutionFenceError as exc:
            raise AuthoritySelectionConflict(str(exc)) from exc
        yield
    finally:
        claim.release()


@dataclass(frozen=True, slots=True)
class _ShotProcessMutexEntry:
    mutex: threading.Lock
    reservations: int = 0


def _retire_shot_process_mutex_reservation(
    key: tuple[int, int],
    entry: _ShotProcessMutexEntry,
) -> None:
    with _SHOT_MUTEX_GUARD:
        observed = _SHOT_MUTEXES.get(key)
        if observed is None or observed.mutex is not entry.mutex:
            raise AuthoritySelectionConflict(
                "shot process mutex reservation changed before retirement"
            )
        if observed.reservations == 1:
            del _SHOT_MUTEXES[key]
        else:
            _SHOT_MUTEXES[key] = _ShotProcessMutexEntry(
                observed.mutex,
                observed.reservations - 1,
            )


@contextmanager
def shot_process_mutex(key: tuple[int, int]) -> Iterator[None]:
    """Arm exact-process mutex cleanup before any transaction body runs."""

    owner_pid = os.getpid()
    owner_token = _PROCESS_TOKEN
    entry: _ShotProcessMutexEntry | None = None
    # Reserve inline while the guard is held. There is no function-return gap in
    # which an acquired/reserved resource exists before this context owns cleanup.
    try:
        with _SHOT_MUTEX_GUARD:
            observed = _SHOT_MUTEXES.get(key)
            if observed is None:
                entry = _ShotProcessMutexEntry(threading.Lock(), 1)
            else:
                entry = _ShotProcessMutexEntry(
                    observed.mutex,
                    observed.reservations + 1,
                )
            _SHOT_MUTEXES[key] = entry
        with entry.mutex:
            yield
    finally:
        if (
            entry is not None
            and os.getpid() == owner_pid
            and owner_token == _PROCESS_TOKEN
        ):
            _retire_shot_process_mutex_reservation(key, entry)


@contextmanager
def descriptor_registry_mutation() -> Iterator[None]:
    """Exclude fork across descriptor open/register or retire/close sequences."""

    _REGISTRY_LOCK.acquire()
    try:
        yield
    finally:
        _REGISTRY_LOCK.release()


def register_descriptors_locked(
    lease_id: str,
    descriptors: tuple[int, ...],
    descriptor_identities: tuple[GuardedDescriptor, ...],
    fork_token: object,
) -> DescriptorLeaseRegistration:
    """Register a fully opened lease while ``descriptor_registry_mutation`` is held."""

    if not lease_id or lease_id in _REGISTRY:
        raise RuntimeError("authority descriptor lease id must be new and non-empty")
    if not descriptors or len(set(descriptors)) != len(descriptors):
        raise RuntimeError("authority descriptor lease requires distinct descriptors")
    if tuple(item.descriptor for item in descriptor_identities) != descriptors:
        raise RuntimeError(
            "authority descriptor lease identities must match its exact descriptor order"
        )
    registration = DescriptorLeaseRegistration(
        lease_id=lease_id,
        process_id=os.getpid(),
        process_token=_PROCESS_TOKEN,
        thread_id=get_ident(),
        descriptors=descriptors,
        descriptor_identities=descriptor_identities,
        fork_token=fork_token,
    )
    _REGISTRY[lease_id] = registration
    return registration


@contextmanager
def provisional_descriptor_registration(
    descriptors: tuple[int, ...],
    acquisition: ForkProtectedAcquisition,
) -> Iterator[ProvisionalDescriptorRegistration]:
    """Arm local-row rollback before registration can become caller-visible."""

    lease_id = secrets.token_hex(16)
    descriptor_identities = acquisition.guarded_descriptors(descriptors)
    registration: DescriptorLeaseRegistration | None = None
    try:
        with descriptor_registry_mutation():
            registration = register_descriptors_locked(
                lease_id,
                descriptors,
                descriptor_identities,
                acquisition.token,
            )
    except BaseException as registration_error:
        with descriptor_registry_mutation():
            observed = _REGISTRY.get(lease_id)
            if observed is not None:
                if (
                    observed.descriptors != descriptors
                    or observed.descriptor_identities != descriptor_identities
                    or observed.fork_token is not acquisition.token
                    or observed.process_id != os.getpid()
                    or observed.process_token != _PROCESS_TOKEN
                    or observed.thread_id != get_ident()
                ):
                    raise RuntimeError(
                        "interrupted authority descriptor registration does not "
                        "match its caller-owned lease id"
                    ) from registration_error
                del _REGISTRY[lease_id]
        raise
    assert registration is not None
    transaction = ProvisionalDescriptorRegistration(
        registration=registration,
        acquisition=acquisition,
    )
    try:
        yield transaction
    finally:
        if not transaction.committed:
            if acquisition.active_handoff_matches(registration.descriptors):
                neutralize_registered_descriptors(
                    registration,
                    unlock_record_lock=False,
                )
            else:
                with descriptor_registry_mutation():
                    if _REGISTRY.get(lease_id) != registration:
                        raise RuntimeError(
                            "provisional authority descriptor registration changed "
                            "before rollback"
                        )
                    del _REGISTRY[lease_id]


def abort_or_preserve_descriptor_acquisition(
    acquisition: ForkProtectedAcquisition,
    descriptors: tuple[int | None, ...],
) -> None:
    """Neutralize pending fds and preserve only known-live failures."""

    if acquisition.cleanup_is_complete():
        return
    failed: list[int] = []
    for descriptor in reversed(descriptors):
        if descriptor is None:
            continue
        try:
            acquisition.retire(descriptor)
        except BaseException:
            failed.append(descriptor)
    if not failed:
        acquisition.abort()
        return
    acquisition.handoff(tuple(reversed(failed)))


@contextmanager
def registered_descriptor_close(
    registration: DescriptorLeaseRegistration,
) -> Iterator[DescriptorCloseProof | None]:
    """Keep exact active descriptors fork-visible through owning cleanup."""

    if registration.process_id != os.getpid() or registration.process_token != _PROCESS_TOKEN:
        yield None
        return
    proof = DescriptorCloseProof(registration=registration)
    with (
        active_descriptor_close(
            registration.fork_token,
            registration.descriptor_identities,
        ),
        descriptor_registry_mutation(),
    ):
        if _REGISTRY.get(registration.lease_id) != registration:
            raise RuntimeError("authority descriptor lease is absent during owning cleanup")
        try:
            yield proof
        finally:
            if not proof.published:
                proof.publish_retained(
                    tuple(
                        item
                        for item in registration.descriptor_identities
                        if item.is_current()
                    )
                )


def _neutralize_registered_descriptors(
    registration: DescriptorLeaseRegistration,
    *,
    unlock_record_lock: bool,
) -> None:
    """Neutralize one exact registered selection lease before retiring its row."""

    errors: list[BaseException] = []
    retained: tuple[GuardedDescriptor, ...] = ()
    substituted: set[int] = set()
    with registered_descriptor_close(registration) as proof:
        if proof is None:
            return
        lock_identity = registration.descriptor_identities[-1]
        if unlock_record_lock and lock_identity.is_current():
            try:
                fcntl.lockf(registration.descriptors[-1], fcntl.LOCK_UN)
            except BaseException as exc:
                errors.append(exc)
        null_descriptor: int | None = None
        null_identity: GuardedDescriptor | None = None
        try:
            null_descriptor = os.open(
                os.devnull,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
            )
            null_identity = GuardedDescriptor.capture(null_descriptor)
            for identity in registration.descriptor_identities:
                if not identity.is_current():
                    continue
                try:
                    os.dup2(
                        null_descriptor,
                        identity.descriptor,
                        inheritable=False,
                    )
                except BaseException as exc:
                    errors.append(exc)
                    if _descriptor_matches_identity(
                        identity.descriptor,
                        null_identity,
                    ):
                        # The replacement took effect before an injected or
                        # platform close-style exception was reported.
                        substituted.add(identity.descriptor)
                else:
                    substituted.add(identity.descriptor)
        except BaseException as exc:
            errors.append(exc)
        finally:
            if null_descriptor is not None:
                with suppress(OSError):
                    os.close(null_descriptor)
        # The nested registries compute and publish the exact still-live identity
        # subset before these now-/dev/null numeric slots are closed. A dup2
        # failure leaves its original fd open and registered; it is never passed
        # to ambiguous close.
        retained = tuple(
            identity
            for identity in registration.descriptor_identities
            if identity.is_current()
        )
        proof.publish_retained(retained)
        for descriptor in substituted:
            try:
                os.close(descriptor)
            except BaseException as exc:
                errors.append(exc)
    if errors:
        raise AuthoritySelectionCleanupFailure(
            errors=tuple(errors),
            retained=retained,
        )


def neutralize_registered_descriptors(
    registration: DescriptorLeaseRegistration,
    *,
    unlock_record_lock: bool,
) -> None:
    """Expose only typed cleanup failures from the descriptor state machine."""

    try:
        _neutralize_registered_descriptors(
            registration,
            unlock_record_lock=unlock_record_lock,
        )
    except AuthoritySelectionCleanupFailure:
        raise
    except BaseException as exc:
        retained = tuple(
            identity
            for identity in registration.descriptor_identities
            if identity.is_current()
        )
        raise AuthoritySelectionCleanupFailure(
            errors=(exc,),
            retained=retained,
        ) from exc


def _descriptor_matches_identity(
    descriptor: int,
    expected: GuardedDescriptor,
) -> bool:
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


def registration_is_current(registration: DescriptorLeaseRegistration) -> bool:
    """Verify process/thread ownership and exact process-wide registration."""

    with descriptor_registry_mutation():
        return (
            registration.process_id == os.getpid()
            and registration.process_token == _PROCESS_TOKEN
            and registration.thread_id == get_ident()
            and _REGISTRY.get(registration.lease_id) == registration
        )


def _after_fork_child() -> None:
    """Start a fresh local binding generation after descriptor child cleanup."""

    global _PROCESS_TOKEN, _REGISTRY_LOCK, _SHOT_MUTEX_GUARD
    descriptors = {
        identity
        for registration in _REGISTRY.values()
        for identity in registration.descriptor_identities
    }
    for identity in descriptors:
        if identity.is_current():
            with suppress(OSError):
                os.close(identity.descriptor)
    _REGISTRY.clear()
    _PROCESS_TOKEN = secrets.token_hex(16)
    _REGISTRY_LOCK = threading.RLock()
    _SHOT_MUTEXES.clear()
    _SHOT_MUTEX_GUARD = threading.Lock()


os.register_at_fork(after_in_child=_after_fork_child)


__all__ = [
    "AuthoritySelectionCleanupFailure",
    "AuthoritySelectionConflict",
    "DescriptorCloseProof",
    "DescriptorLeaseRegistration",
    "abort_or_preserve_descriptor_acquisition",
    "canonical_authority_shot_path",
    "current_process_token",
    "descriptor_registry_mutation",
    "neutralize_registered_descriptors",
    "open_authority_directory_parts",
    "process_bound_live_lock_identity",
    "provisional_descriptor_registration",
    "register_descriptors_locked",
    "registered_descriptor_close",
    "registration_is_current",
    "require_selection_lease_identity",
    "shot_process_mutex",
]
