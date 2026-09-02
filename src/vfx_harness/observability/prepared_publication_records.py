"""Private immutable records for opaque prepared-file transactions."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from weakref import ReferenceType

from vfx_harness.observability.prepared_publication_capabilities import (
    PreparedFilePayloadVerification,
    PreparedFilePublication,
    PreparedPublicationRegistryError,
)
from vfx_harness.observability.run_owner_fork_guard import GuardedDescriptor


@dataclass(frozen=True, slots=True)
class FileIdentity:
    """Full regular-file identity for one exact observed generation."""

    device: int
    inode: int
    size: int
    modified_ns: int
    changed_ns: int


@dataclass(frozen=True, slots=True)
class _PreparedFilePublicationRecord:
    """Private authoritative state behind one public capability object."""

    shot: Path
    shot_descriptor: int
    shot_identity: tuple[int, int]
    relative_path: Path
    destination: Path
    destination_parent: Path
    destination_parent_descriptor: int
    destination_parent_identity: tuple[int, int]
    destination_name: str
    predecessor: FileIdentity | None
    predecessor_sha256: str | None
    temporary: Path
    temporary_descriptor: int
    temporary_guard: GuardedDescriptor
    temporary_name: str
    temporary_identity: FileIdentity
    payload_sha256: str
    lock_parent: Path
    lock_parent_descriptor: int
    lock_parent_identity: tuple[int, int]
    lock_descriptor: int
    lock_name: str
    lock_identity: FileIdentity
    authority_binding: str
    commit_policy: Callable[[object, object], None] | None
    process_id: int
    process_token: object
    thread_id: int
    thread_token: object
    guarded_descriptors: tuple[GuardedDescriptor, ...]


@dataclass(frozen=True, slots=True)
class _PreparedFileVerificationRecord:
    """Private authority behind one exact single-generation proof."""

    publication_id: int
    publication_reference: ReferenceType[Any]
    generation: int
    expected_sha256: str
    temporary_identity: FileIdentity
    process_id: int
    process_token: object
    thread_id: int
    thread_token: object
    transaction_binding: object | None


class _PreparedPublicationCleanupError(PreparedPublicationRegistryError):
    """Cleanup failure retaining only the exact authority still owned."""

    def __init__(
        self,
        message: str,
        errors: list[BaseException],
        retained: tuple[GuardedDescriptor, ...],
        *,
        unlink_pending: bool = False,
    ) -> None:
        self.retained = retained
        self.unlink_pending = unlink_pending
        super().__init__(message)
        for error in errors:
            self.add_note(f"cleanup diagnostic: {type(error).__name__}: {error}")


@dataclass(frozen=True, slots=True)
class _PublicationPhase:
    name: str
    commit_reference: ReferenceType[Any] | None = None
    unlink_temporary: bool = False
    cleanup_descriptors: tuple[GuardedDescriptor, ...] = ()


@dataclass(slots=True)
class _PublicationEntry:
    reference: ReferenceType[PreparedFilePublication]
    record: _PreparedFilePublicationRecord
    verification_generation: int = 0
    verification_id: int | None = None
    phase: _PublicationPhase = _PublicationPhase("active")


@dataclass(slots=True)
class _VerificationEntry:
    reference: ReferenceType[PreparedFilePayloadVerification]
    record: _PreparedFileVerificationRecord


@dataclass(slots=True)
class _PendingTemporary:
    parent_descriptor: int
    name: str
    staged_descriptors: list[GuardedDescriptor]


@dataclass(slots=True)
class _PendingAcquisition:
    """One atomic owner for descriptors and candidate/staged-name state."""

    descriptors: list[GuardedDescriptor]
    temporary: _PendingTemporary | None = None
    aborting: bool = False


__all__ = ["FileIdentity"]
