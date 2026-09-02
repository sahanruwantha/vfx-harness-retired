"""Opaque process-local registry ownership for prepared side-file publications."""

from __future__ import annotations

import contextlib
import os
import stat
import threading
import weakref
from pathlib import Path
from typing import Any

from vfx_harness.observability import fork_coordination
from vfx_harness.observability import prepared_publication_capabilities as _capabilities
from vfx_harness.observability.prepared_publication_capabilities import (
    PreparedFilePayloadVerification,
    PreparedFilePublication,
    PreparedPublicationRegistryError,
)
from vfx_harness.observability.prepared_publication_cleanup import (
    unlink_guarded_temporary as _unlink_guarded_temporary,
)
from vfx_harness.observability.prepared_publication_descriptors import (
    block_deferred_signals as _block_deferred_signals,
)
from vfx_harness.observability.prepared_publication_descriptors import (
    drain_inert_descriptors as _drain_inert_descriptors,
)
from vfx_harness.observability.prepared_publication_descriptors import (
    neutralize_fork_child_descriptors as _neutralize_fork_child_descriptors,
)
from vfx_harness.observability.prepared_publication_descriptors import (
    neutralize_guarded_descriptor as _neutralize_guarded_descriptor,
)
from vfx_harness.observability.prepared_publication_descriptors import (
    require_prepared_descriptor_admission as _require_descriptor_admission,
)
from vfx_harness.observability.prepared_publication_records import (
    FileIdentity,
    _PendingAcquisition,
    _PendingTemporary,
    _PreparedFilePublicationRecord,
    _PreparedFileVerificationRecord,
    _PreparedPublicationCleanupError,
    _PublicationEntry,
    _PublicationPhase,
    _VerificationEntry,
)
from vfx_harness.observability.run_owner_fork_guard import (
    GuardedDescriptor,
)

_ACTIVE_PHASE = _PublicationPhase("active")
_CONSUMED_PHASE = _PublicationPhase("consumed")


class _PreparedFileCommitLease:
    """Ephemeral stack-lifetime owner for one in-flight commit."""

    __slots__ = ("__weakref__", "active")

    def __init__(self) -> None:
        self.active = True


_REGISTRY_LOCK = threading.RLock()
_PROCESS_TOKEN = object()
_THREAD_LOCAL = threading.local()
_PUBLICATIONS: dict[int, _PublicationEntry] = {}
_VERIFICATIONS: dict[int, _VerificationEntry] = {}
_PENDING_ACQUISITIONS: dict[object, _PendingAcquisition] = {}


def _after_fork_child() -> None:
    global _PROCESS_TOKEN, _REGISTRY_LOCK, _THREAD_LOCAL

    descriptors = {
        guarded
        for acquisition in _PENDING_ACQUISITIONS.values()
        for guarded in acquisition.descriptors
    }
    for entry in _PUBLICATIONS.values():
        if entry.phase.name in {"active", "committing"}:
            descriptors.update(entry.record.guarded_descriptors)
        elif entry.phase.name == "cleanup_required":
            descriptors.update(entry.phase.cleanup_descriptors)
    _neutralize_fork_child_descriptors(descriptors)
    _PUBLICATIONS.clear()
    _VERIFICATIONS.clear()
    _PENDING_ACQUISITIONS.clear()
    _PROCESS_TOKEN = object()
    _REGISTRY_LOCK = threading.RLock()
    _THREAD_LOCAL = threading.local()


fork_coordination.register_fork_participant(
    "observability.prepared_publication_registry",
    lock_factory=lambda: _REGISTRY_LOCK,
    after_in_child=_after_fork_child,
)


def current_registry_process_token() -> object:
    """Return the unforgeable token for this process generation."""

    return _PROCESS_TOKEN


def current_registry_thread_token() -> object:
    """Return a non-reusable identity for this exact thread generation."""

    token = getattr(_THREAD_LOCAL, "token", None)
    if token is None:
        token = object()
        _THREAD_LOCAL.token = token
    return token


def _drain_orphaned_descriptors_locked() -> None:
    errors: list[_PreparedPublicationCleanupError] = []
    for token, acquisition in tuple(_PENDING_ACQUISITIONS.items()):
        if not acquisition.aborting:
            continue
        try:
            _abort_descriptor_acquisition_locked(token)
        except _PreparedPublicationCleanupError as exc:
            errors.append(exc)
    if errors:
        raise errors[0]


def arm_descriptor_acquisition(token: object) -> None:
    """Arm caller-owned cleanup before opening any retained descriptor."""

    with fork_coordination.fork_coordinated_lock(_REGISTRY_LOCK):
        _drain_inert_descriptors()
        _drain_orphaned_descriptors_locked()
        _drain_orphaned_publications_locked()
        if token in _PENDING_ACQUISITIONS:  # pragma: no cover - object identity guarantee
            raise PreparedPublicationRegistryError("prepared descriptor acquisition token is already armed")
        _PENDING_ACQUISITIONS[token] = _PendingAcquisition([])


def bind_pending_temporary(token: object, parent_descriptor: int, name: str) -> None:
    """Bind a known name before its staged inode can be opened."""

    with fork_coordination.fork_coordinated_lock(_REGISTRY_LOCK):
        acquisition = _pending_acquisition(token)
        if acquisition.temporary is not None:
            raise PreparedPublicationRegistryError("prepared temporary candidate is already bound")
        acquisition.temporary = _PendingTemporary(parent_descriptor, name, [])


def clear_pending_temporary(token: object) -> None:
    """Clear a candidate name that was proven to preexist this acquisition."""

    with fork_coordination.fork_coordinated_lock(_REGISTRY_LOCK):
        acquisition = _pending_acquisition(token)
        temporary = acquisition.temporary
        if temporary is not None and temporary.staged_descriptors:
            raise PreparedPublicationRegistryError("created prepared temporary cannot be cleared as a collision")
        acquisition.temporary = None


def _pending_acquisition(token: object) -> _PendingAcquisition:
    try:
        return _PENDING_ACQUISITIONS[token]
    except KeyError as exc:
        raise PreparedPublicationRegistryError("prepared descriptor acquisition is no longer pending") from exc


def _pending_rows(token: object) -> list[GuardedDescriptor]:
    return _pending_acquisition(token).descriptors


def open_pending_descriptor(
    token: object,
    path: str | Path,
    flags: int,
    mode: int = 0o777,
    *,
    dir_fd: int | None = None,
) -> int:
    """Open one path directly into its fork-visible acquisition row."""

    with fork_coordination.fork_coordinated_lock(_REGISTRY_LOCK):
        _require_descriptor_admission()
        acquisition = _pending_acquisition(token)
        rows = acquisition.descriptors
        temporary = acquisition.temporary
        matches_temporary = (
            temporary is not None
            and dir_fd == temporary.parent_descriptor
            and os.fspath(path) == temporary.name
        )
        creates_temporary = bool(
            matches_temporary
            and flags & os.O_CREAT
            and flags & os.O_EXCL
        )
        with _block_deferred_signals():
            descriptor = os.open(path, flags, mode, dir_fd=dir_fd)
            capture_error: BaseException | None = None
            try:
                guarded = GuardedDescriptor.capture(descriptor)
            except BaseException as exc:
                if not creates_temporary:
                    with contextlib.suppress(OSError):
                        os.close(descriptor)
                    raise
                capture_error = exc
                try:
                    observed = os.fstat(descriptor)
                    guarded = GuardedDescriptor(
                        descriptor=descriptor,
                        device=observed.st_dev,
                        inode=observed.st_ino,
                        file_type=stat.S_IFMT(observed.st_mode),
                        special_device=observed.st_rdev,
                    )
                except BaseException:
                    with contextlib.suppress(OSError):
                        os.close(descriptor)
                    raise exc from None
            if any(item.descriptor == descriptor for item in rows):
                with contextlib.suppress(OSError):
                    os.close(descriptor)
                raise PreparedPublicationRegistryError(
                    "prepared descriptor acquisition reused a live numeric descriptor"
                )
            rows.append(guarded)
            if matches_temporary:
                assert temporary is not None
                if creates_temporary or (
                    temporary.staged_descriptors
                    and (
                        guarded.device,
                        guarded.inode,
                        guarded.file_type,
                    )
                    == (
                        temporary.staged_descriptors[0].device,
                        temporary.staged_descriptors[0].inode,
                        temporary.staged_descriptors[0].file_type,
                    )
                ):
                    temporary.staged_descriptors.append(guarded)
            if capture_error is not None:
                raise capture_error
        return descriptor


def retire_pending_descriptor(token: object, descriptor: int) -> None:
    """Close one exact pending descriptor before replacing it with read-only access."""

    with fork_coordination.fork_coordinated_lock(_REGISTRY_LOCK):
        rows = _pending_rows(token)
        guarded = next(
            (item for item in rows if item.descriptor == descriptor),
            None,
        )
        if guarded is None:
            raise PreparedPublicationRegistryError(
                "prepared descriptor acquisition cannot retire an unregistered descriptor"
            )
        def release() -> None:
            if guarded in rows:
                rows.remove(guarded)
            temporary = _pending_acquisition(token).temporary
            if temporary is not None and guarded in temporary.staged_descriptors:
                temporary.staged_descriptors.remove(guarded)

        try:
            retired = _neutralize_guarded_descriptor(guarded, release=release)
        except BaseException as exc:
            raise PreparedPublicationRegistryError("prepared descriptor could not be retired safely") from exc
        if not retired:
            raise PreparedPublicationRegistryError("prepared descriptor changed identity before retirement")


def pending_descriptor_identities(
    token: object,
    descriptors: tuple[int, ...],
) -> tuple[GuardedDescriptor, ...]:
    """Return the exact pending identities in their authoritative order."""

    with fork_coordination.fork_coordinated_lock(_REGISTRY_LOCK):
        rows = tuple(_pending_rows(token))
        if tuple(item.descriptor for item in rows) != descriptors:
            raise PreparedPublicationRegistryError("prepared descriptor set differs from its pending acquisition")
        return rows


def _require_owner(record: _PreparedFilePublicationRecord) -> None:
    if (
        record.process_id != os.getpid()
        or record.process_token is not _PROCESS_TOKEN
        or record.thread_id != threading.get_ident()
        or record.thread_token is not current_registry_thread_token()
    ):
        raise PreparedPublicationRegistryError("prepared file publication belongs to another process or thread")


def _resolve_entry(publication: PreparedFilePublication) -> _PublicationEntry:
    if not isinstance(publication, PreparedFilePublication):
        raise PreparedPublicationRegistryError("prepared file operation requires an exact typed publication capability")
    with fork_coordination.fork_coordinated_lock(_REGISTRY_LOCK):
        entry = _PUBLICATIONS.get(id(publication))
        if entry is None or entry.reference() is not publication:
            raise PreparedPublicationRegistryError(
                "prepared file publication is unregistered, expired, consumed, or copied"
            )
        _require_owner(entry.record)
        return entry


def _require_active_entry(publication: PreparedFilePublication) -> _PublicationEntry:
    entry = _resolve_entry(publication)
    if entry.phase.name == "committing":
        raise PreparedPublicationRegistryError("prepared file publication is already committing")
    if entry.phase.name != "active":
        raise PreparedPublicationRegistryError("prepared file publication is expired or already consumed")
    return entry


def inspect_prepared_file(
    publication: PreparedFilePublication,
) -> _PreparedFilePublicationRecord:
    """Read compatibility metadata from this exact active or consumed object."""

    return _resolve_entry(publication).record


def require_live_prepared_file(
    publication: PreparedFilePublication,
) -> _PreparedFilePublicationRecord:
    """Resolve the exact live capability to its private authoritative row."""

    return _require_active_entry(publication).record


_capabilities.bind_publication_inspector(inspect_prepared_file)


def _cleanup_entry_locked(
    entry: _PublicationEntry,
    *,
    unlink_temporary: bool,
    descriptors: tuple[GuardedDescriptor, ...],
) -> None:
    """Advance cleanup authority before any retired numeric slot is reusable."""

    if entry.phase.name != "cleanup_required":
        entry.phase = _PublicationPhase(
            "cleanup_required",
            unlink_temporary=unlink_temporary,
            cleanup_descriptors=descriptors,
        )
    phase = entry.phase
    if phase.unlink_temporary:
        try:
            _unlink_guarded_temporary(
                (entry.record.temporary_guard,),
                entry.record.destination_parent_descriptor,
                entry.record.temporary_name,
            )
        except BaseException as exc:
            raise _PreparedPublicationCleanupError(
                "prepared publication temporary cleanup remains incomplete",
                [exc],
                phase.cleanup_descriptors,
                unlink_pending=True,
            ) from exc
        entry.phase = _PublicationPhase(
            "cleanup_required",
            cleanup_descriptors=phase.cleanup_descriptors,
        )

    errors: list[BaseException] = []
    for guarded in reversed(entry.phase.cleanup_descriptors):

        def release(*, owned: GuardedDescriptor = guarded) -> None:
            current = entry.phase
            if current.name != "cleanup_required":
                return
            entry.phase = _PublicationPhase(
                "cleanup_required",
                unlink_temporary=current.unlink_temporary,
                cleanup_descriptors=tuple(
                    candidate
                    for candidate in current.cleanup_descriptors
                    if candidate is not owned
                ),
            )

        try:
            retired = _neutralize_guarded_descriptor(
                guarded,
                release=release,
            )
        except BaseException as exc:
            errors.append(exc)
        else:
            if not retired:
                errors.append(
                    RuntimeError(
                        "prepared publication descriptor changed identity before cleanup"
                    )
                )
    phase = entry.phase
    if not phase.cleanup_descriptors and not phase.unlink_temporary:
        entry.phase = _CONSUMED_PHASE
    if errors:
        retained = (
            entry.phase.cleanup_descriptors
            if entry.phase.name == "cleanup_required"
            else ()
        )
        raise _PreparedPublicationCleanupError(
            "prepared publication cleanup reported an exact-descriptor invariant failure",
            errors,
            retained,
            unlink_pending=(
                entry.phase.unlink_temporary
                if entry.phase.name == "cleanup_required"
                else False
            ),
        ) from errors[0]


def _drain_orphaned_publications_locked() -> None:
    errors: list[_PreparedPublicationCleanupError] = []
    for publication_id, entry in tuple(_PUBLICATIONS.items()):
        if entry.reference() is not None:
            continue
        if entry.phase.name in {"active", "committing"}:
            entry.phase = _PublicationPhase(
                "cleanup_required",
                unlink_temporary=True,
                cleanup_descriptors=entry.record.guarded_descriptors,
            )
        if entry.phase.name != "cleanup_required":
            _PUBLICATIONS.pop(publication_id, None)
            continue
        try:
            _cleanup_entry_locked(
                entry,
                unlink_temporary=entry.phase.unlink_temporary,
                descriptors=entry.phase.cleanup_descriptors,
            )
        except _PreparedPublicationCleanupError as exc:
            errors.append(exc)
        if entry.phase.name == "consumed":
            _PUBLICATIONS.pop(publication_id, None)
    if errors:
        raise errors[0]


def _invalidate_verification_locked(entry: _PublicationEntry) -> None:
    verification_id = entry.verification_id
    entry.verification_id = None
    if verification_id is not None:
        verification = _VERIFICATIONS.get(verification_id)
        publication = entry.reference()
        if (
            verification is not None
            and publication is not None
            and verification.record.publication_id == id(publication)
            and verification.record.publication_reference() is publication
            and verification.record.generation == entry.verification_generation
        ):
            _VERIFICATIONS.pop(verification_id, None)


def _drain_dead_verifications_locked() -> None:
    for verification_id, entry in tuple(_VERIFICATIONS.items()):
        if entry.reference() is not None:
            continue
        if _VERIFICATIONS.get(verification_id) is not entry:
            continue
        _VERIFICATIONS.pop(verification_id, None)
        publication = entry.record.publication_reference()
        publication_entry = _PUBLICATIONS.get(entry.record.publication_id)
        if (
            publication is not None
            and publication_entry is not None
            and publication_entry.reference() is publication
            and publication_entry.verification_id == verification_id
            and publication_entry.verification_generation == entry.record.generation
        ):
            publication_entry.verification_id = None


def _publication_gone(publication_id: int, reference: weakref.ReferenceType[Any]) -> None:
    with fork_coordination.fork_coordinated_lock(_REGISTRY_LOCK):
        entry = _PUBLICATIONS.get(publication_id)
        if entry is not None and entry.reference is reference:
            if entry.phase.name == "consumed":
                _PUBLICATIONS.pop(publication_id, None)
                return
            if entry.phase.name != "cleanup_required":
                entry.phase = _PublicationPhase(
                    "cleanup_required",
                    unlink_temporary=True,
                    cleanup_descriptors=entry.record.guarded_descriptors,
                )
            unlink_temporary = entry.phase.unlink_temporary
            descriptors = entry.phase.cleanup_descriptors
            _invalidate_verification_locked(entry)
            for _attempt in range(2):
                try:
                    _cleanup_entry_locked(
                        entry,
                        unlink_temporary=unlink_temporary,
                        descriptors=descriptors,
                    )
                    break
                except _PreparedPublicationCleanupError:
                    descriptors = entry.phase.cleanup_descriptors
                    unlink_temporary = entry.phase.unlink_temporary
                    if not descriptors:
                        break
            if entry.phase.name == "consumed":
                _PUBLICATIONS.pop(publication_id, None)


def register_prepared_file(
    record: _PreparedFilePublicationRecord,
    *,
    acquisition_token: object,
) -> PreparedFilePublication:
    """Mint and register one capability after every retained fd is fork-guarded."""

    if (
        record.process_id != os.getpid()
        or record.process_token is not _PROCESS_TOKEN
        or record.thread_id != threading.get_ident()
        or record.thread_token is not current_registry_thread_token()
    ):
        raise PreparedPublicationRegistryError("prepared publication record belongs to another process or thread")
    publication = object.__new__(PreparedFilePublication)
    publication_id = id(publication)
    reference = weakref.ref(
        publication,
        lambda observed, identifier=publication_id: _publication_gone(
            identifier,
            observed,
        ),
    )
    installed = _PublicationEntry(reference=reference, record=record)
    with fork_coordination.fork_coordinated_lock(_REGISTRY_LOCK):
        acquisition = _pending_acquisition(acquisition_token)
        rows = tuple(acquisition.descriptors)
        if rows != record.guarded_descriptors:
            raise PreparedPublicationRegistryError(
                "prepared publication record differs from its pending descriptor set"
            )
        temporary = acquisition.temporary
        if (
            temporary is None
            or temporary.parent_descriptor != record.destination_parent_descriptor
            or temporary.name != record.temporary_name
            or record.temporary_guard not in temporary.staged_descriptors
        ):
            raise PreparedPublicationRegistryError(
                "prepared publication record differs from its exact staged-name owner"
            )
        if publication_id in _PUBLICATIONS:  # pragma: no cover - live id guarantee
            raise PreparedPublicationRegistryError(
                "prepared publication object identity collided with a live capability"
            )
        # Install the durable publication owner before retiring the acquisition
        # owner. An interruption can therefore leave two discoverable owners,
        # never an unowned descriptor or temporary name.
        _PUBLICATIONS[publication_id] = installed
        _PENDING_ACQUISITIONS.pop(acquisition_token, None)
        return publication


def abandon_prepared_file_registration(publication: PreparedFilePublication) -> None:
    """Consume a just-registered row after an enclosing construction failure."""

    with fork_coordination.fork_coordinated_lock(_REGISTRY_LOCK):
        entry = _PUBLICATIONS.get(id(publication))
        if entry is None or entry.reference() is not publication:
            return
        _invalidate_verification_locked(entry)
        try:
            _cleanup_entry_locked(
                entry,
                unlink_temporary=True,
                descriptors=entry.record.guarded_descriptors,
            )
        finally:
            if entry.phase.name == "consumed":
                _PUBLICATIONS.pop(id(publication), None)


def _abort_descriptor_acquisition_locked(token: object) -> None:
    acquisition = _PENDING_ACQUISITIONS.get(token)
    if acquisition is None:
        return
    rows = acquisition.descriptors
    errors: list[BaseException] = []
    temporary = acquisition.temporary
    if temporary is not None:
        if not temporary.staged_descriptors:
            acquisition.temporary = None
        else:
            try:
                _unlink_guarded_temporary(
                    tuple(temporary.staged_descriptors),
                    temporary.parent_descriptor,
                    temporary.name,
                )
                acquisition.temporary = None
            except BaseException as exc:
                errors.append(exc)
    if acquisition.temporary is None:
        for guarded in reversed(tuple(rows)):

            def release(*, owned: GuardedDescriptor = guarded) -> None:
                if owned in rows:
                    rows.remove(owned)

            try:
                retired = _neutralize_guarded_descriptor(
                    guarded,
                    release=release,
                )
            except BaseException as exc:
                errors.append(exc)
            else:
                if not retired:
                    errors.append(
                        RuntimeError(
                            "pending prepared descriptor changed identity before cleanup"
                        )
                    )
    if not rows and acquisition.temporary is None:
        _PENDING_ACQUISITIONS.pop(token, None)
    if errors:
        raise _PreparedPublicationCleanupError(
            "prepared descriptor acquisition cleanup reported an invariant failure",
            errors,
            tuple(rows),
        ) from errors[0]


def abort_descriptor_acquisition(token: object) -> None:
    """Retire every exact pending descriptor after failed preparation."""

    with fork_coordination.fork_coordinated_lock(_REGISTRY_LOCK):
        acquisition = _PENDING_ACQUISITIONS.get(token)
        if acquisition is None:
            return
        # Cleanup ownership is published before the first syscall. A later
        # acquisition can retry the same row if this attempt is interrupted.
        acquisition.aborting = True
        for _attempt in range(2):
            try:
                _abort_descriptor_acquisition_locked(token)
            except _PreparedPublicationCleanupError:
                if _attempt:
                    raise
            else:
                return


def _verification_gone(
    verification_id: int,
    reference: weakref.ReferenceType[Any],
) -> None:
    with fork_coordination.fork_coordinated_lock(_REGISTRY_LOCK):
        entry = _VERIFICATIONS.get(verification_id)
        if entry is None or entry.reference is not reference:
            return
        _VERIFICATIONS.pop(verification_id, None)
        publication = entry.record.publication_reference()
        if publication is None:
            return
        publication_entry = _PUBLICATIONS.get(entry.record.publication_id)
        if (
            publication_entry is not None
            and publication_entry.reference() is publication
            and publication_entry.verification_id == verification_id
        ):
            publication_entry.verification_id = None


def mint_payload_verification(
    publication: PreparedFilePublication,
    *,
    expected_sha256: str,
    temporary_identity: FileIdentity,
    transaction_binding: object | None,
) -> PreparedFilePayloadVerification:
    """Mint the sole current verification generation for one publication."""

    with fork_coordination.fork_coordinated_lock(_REGISTRY_LOCK):
        entry = _require_active_entry(publication)
        _drain_dead_verifications_locked()
        _invalidate_verification_locked(entry)
        entry.verification_generation += 1
        verification = object.__new__(PreparedFilePayloadVerification)
        verification_id = id(verification)
        reference = weakref.ref(
            verification,
            lambda observed, identifier=verification_id: _verification_gone(
                identifier,
                observed,
            ),
        )
        record = _PreparedFileVerificationRecord(
            publication_id=id(publication),
            publication_reference=weakref.ref(publication),
            generation=entry.verification_generation,
            expected_sha256=expected_sha256,
            temporary_identity=temporary_identity,
            process_id=os.getpid(),
            process_token=_PROCESS_TOKEN,
            thread_id=threading.get_ident(),
            thread_token=current_registry_thread_token(),
            transaction_binding=transaction_binding,
        )
        if verification_id in _VERIFICATIONS:  # pragma: no cover - live id guarantee
            raise PreparedPublicationRegistryError(
                "prepared payload verification identity collided with a live capability"
            )
        _VERIFICATIONS[verification_id] = _VerificationEntry(reference, record)
        entry.verification_id = verification_id
        return verification


def consume_payload_verification(
    publication: PreparedFilePublication,
    verification: PreparedFilePayloadVerification,
    *,
    transaction_binding: object | None,
) -> _PreparedFileVerificationRecord:
    """Consume exactly the one current proof bound to this publication generation."""

    if not isinstance(verification, PreparedFilePayloadVerification):
        raise PreparedPublicationRegistryError("prepared payload verification requires an exact typed capability")
    with fork_coordination.fork_coordinated_lock(_REGISTRY_LOCK):
        publication_entry = _require_active_entry(publication)
        verification_id = id(verification)
        entry = _VERIFICATIONS.get(verification_id)
        if entry is None or entry.reference() is not verification:
            raise PreparedPublicationRegistryError(
                "prepared payload verification is unregistered, expired, consumed, or copied"
            )
        record = entry.record
        if (
            publication_entry.verification_id != verification_id
            or record.publication_id != id(publication)
            or record.publication_reference() is not publication
            or record.generation != publication_entry.verification_generation
            or record.process_id != os.getpid()
            or record.process_token is not _PROCESS_TOKEN
            or record.thread_id != threading.get_ident()
            or record.thread_token is not current_registry_thread_token()
            or record.transaction_binding is not transaction_binding
        ):
            raise PreparedPublicationRegistryError(
                "prepared payload verification does not bind this exact live publication generation"
            )
        _VERIFICATIONS.pop(verification_id, None)
        publication_entry.verification_id = None
        return record


def require_no_payload_verification(publication: PreparedFilePublication) -> None:
    """Allow proofless commit only when this publication was never verified."""

    with fork_coordination.fork_coordinated_lock(_REGISTRY_LOCK):
        entry = _require_active_entry(publication)
        if entry.verification_generation:
            raise PreparedPublicationRegistryError(
                "prepared file publication requires its exact latest payload verification"
            )


def begin_prepared_file_commit(
    publication: PreparedFilePublication,
    commit_token: _PreparedFileCommitLease,
) -> None:
    """Move one active exact capability into a non-reentrant commit phase."""

    with fork_coordination.fork_coordinated_lock(_REGISTRY_LOCK):
        entry = _require_active_entry(publication)
        entry.phase = _PublicationPhase(
            "committing",
            commit_reference=weakref.ref(commit_token),
        )


def cancel_prepared_file_commit(
    publication: PreparedFilePublication,
    commit_token: _PreparedFileCommitLease,
) -> None:
    """Restore a pre-rename transaction after its exact commit attempt stops."""

    with fork_coordination.fork_coordinated_lock(_REGISTRY_LOCK):
        entry = _resolve_entry(publication)
        if entry.phase == _ACTIVE_PHASE:
            # The begin call failed before it installed this token.
            return
        if (
            entry.phase.name != "committing"
            or entry.phase.commit_reference is None
            or entry.phase.commit_reference() is not commit_token
        ):
            raise PreparedPublicationRegistryError("prepared file commit phase belongs to another exact transaction")
        entry.phase = _ACTIVE_PHASE


def complete_prepared_file_commit(
    publication: PreparedFilePublication,
    commit_token: _PreparedFileCommitLease,
    *,
    unlink_temporary: bool,
) -> None:
    """Consume the exact non-reentrant commit phase and retire its resources."""

    with fork_coordination.fork_coordinated_lock(_REGISTRY_LOCK):
        entry = _resolve_entry(publication)
        if (
            entry.phase.name != "committing"
            or entry.phase.commit_reference is None
            or entry.phase.commit_reference() is not commit_token
        ):
            raise PreparedPublicationRegistryError(
                "prepared file commit completion belongs to another exact transaction"
            )
        _invalidate_verification_locked(entry)
        _cleanup_entry_locked(
            entry,
            unlink_temporary=unlink_temporary,
            descriptors=entry.record.guarded_descriptors,
        )


def consume_prepared_file(
    publication: PreparedFilePublication,
    *,
    unlink_temporary: bool,
) -> None:
    """Consume a capability and retire only its exact registered resources."""

    with fork_coordination.fork_coordinated_lock(_REGISTRY_LOCK):
        entry = _resolve_entry(publication)
        if entry.phase.name == "consumed":
            _drain_inert_descriptors()
            return
        if entry.phase.name == "committing":
            commit_lease = (
                entry.phase.commit_reference()
                if entry.phase.commit_reference is not None
                else None
            )
            if commit_lease is not None and commit_lease.active:
                raise PreparedPublicationRegistryError(
                    "prepared file publication is already committing"
                )
            entry.phase = _PublicationPhase(
                "cleanup_required",
                unlink_temporary=True,
                cleanup_descriptors=entry.record.guarded_descriptors,
            )
        effective_unlink = entry.phase.unlink_temporary if entry.phase.name == "cleanup_required" else unlink_temporary
        descriptors = (
            entry.phase.cleanup_descriptors
            if entry.phase.name == "cleanup_required"
            else entry.record.guarded_descriptors
        )
        _invalidate_verification_locked(entry)
        _cleanup_entry_locked(
            entry,
            unlink_temporary=effective_unlink,
            descriptors=descriptors,
        )


__all__ = [
    "FileIdentity",
    "PreparedFilePayloadVerification",
    "PreparedFilePublication",
    "PreparedPublicationRegistryError",
]
