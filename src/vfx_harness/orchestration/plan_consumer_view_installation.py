"""Rollback-safe physical installation of one allocated consumer-view directory."""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
import threading
import weakref
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vfx_harness.observability import fork_coordination
from vfx_harness.observability.prepared_publication_descriptors import (
    block_deferred_signals,
)
from vfx_harness.observability.run_owner_fork_guard import (
    managed_fork_protected_acquisition,
)
from vfx_harness.orchestration.plan_consumer_view_cleanup import (
    _NoReplaceDestinationOccupied,
    move_owned_directory_noreplace,
    retire_owned_directory,
)
from vfx_harness.orchestration.plan_consumer_view_descriptors import (
    PlanConsumerDirectoryIdentity,
    PlanConsumerLedgerBinding,
    PlanConsumerViewMutationConflict,
    absolute_path,
    capture_ledger_binding_at,
    open_real_directory,
    read_marker,
)


class PlanConsumerViewInstallationRename:
    """Opaque exact moved-name receipt retained through registration."""

    __slots__ = ("__weakref__",)

    def __new__(
        cls,
        *_args: Any,
        **_kwargs: Any,
    ) -> PlanConsumerViewInstallationRename:
        raise PlanConsumerViewMutationConflict(
            "plan-consumer rename receipts are issued only by installation"
        )

    def __reduce_ex__(self, _protocol: int) -> Any:
        raise PlanConsumerViewMutationConflict(
            "plan-consumer rename receipts cannot be serialized"
        )


@dataclass(frozen=True, slots=True)
class _InstallationRenameRecord:
    """Exact moved names behind one opaque registered receipt."""

    scratch: Path
    temporary: Path
    installed: Path
    installed_identity: PlanConsumerDirectoryIdentity
    previous: Path | None
    previous_identity: PlanConsumerDirectoryIdentity | None


@dataclass(frozen=True, slots=True)
class _RenameEntry:
    reference: weakref.ReferenceType[PlanConsumerViewInstallationRename]
    record: _InstallationRenameRecord


@dataclass(slots=True)
class _ClaimedRenameReceipt:
    receipt: PlanConsumerViewInstallationRename
    record: _InstallationRenameRecord
    identifier: int
    completed: bool = False

    def complete(self, continuation: Callable[[], None]) -> None:
        """Commit external state and receipt consumption without a signal gap."""

        with block_deferred_signals():
            continuation()
            with _rename_locked():
                entry = _RENAMES.get(self.identifier)
                if (
                    entry is None
                    or entry.reference() is not self.receipt
                    or entry.record is not self.record
                    or self.identifier not in _RENAME_CLAIMS
                ):
                    raise PlanConsumerViewMutationConflict(
                        "plan-consumer rename receipt changed before cleanup commit"
                    )
                _RENAME_CLAIMS.discard(self.identifier)
                _RENAMES.pop(self.identifier, None)
                self.completed = True


_RENAME_LOCK = threading.RLock()
_RENAMES: dict[int, _RenameEntry] = {}
_RENAME_CLAIMS: set[int] = set()


def _after_fork_child() -> None:
    global _RENAME_LOCK

    _RENAMES.clear()
    _RENAME_CLAIMS.clear()
    _RENAME_LOCK = threading.RLock()


fork_coordination.register_fork_participant(
    "orchestration.plan_consumer_view_installation",
    lock_factory=lambda: _RENAME_LOCK,
    after_in_child=_after_fork_child,
)


@contextmanager
def _rename_locked() -> Iterator[None]:
    with fork_coordination.fork_coordinated_lock(_RENAME_LOCK):
        yield


def _rename_gone(
    identifier: int,
    reference: weakref.ReferenceType[PlanConsumerViewInstallationRename],
) -> None:
    with _rename_locked():
        entry = _RENAMES.get(identifier)
        if entry is not None and entry.reference is reference:
            _RENAMES.pop(identifier, None)
            _RENAME_CLAIMS.discard(identifier)


def _mint_rename_receipt(
    record: _InstallationRenameRecord,
) -> PlanConsumerViewInstallationRename:
    receipt = object.__new__(PlanConsumerViewInstallationRename)
    identifier = id(receipt)
    reference = weakref.ref(
        receipt,
        lambda observed, key=identifier: _rename_gone(key, observed),
    )
    with _rename_locked():
        _RENAMES[identifier] = _RenameEntry(reference, record)
    return receipt


@contextmanager
def _claimed_rename_receipt(
    receipt: PlanConsumerViewInstallationRename,
) -> Iterator[_ClaimedRenameReceipt]:
    if type(receipt) is not PlanConsumerViewInstallationRename:
        raise PlanConsumerViewMutationConflict(
            "plan-consumer rename cleanup requires its exact opaque receipt"
        )
    identifier = id(receipt)
    claimed = False
    lease: _ClaimedRenameReceipt | None = None
    try:
        with block_deferred_signals(), _rename_locked():
            entry = _RENAMES.get(identifier)
            if entry is None or entry.reference() is not receipt:
                raise PlanConsumerViewMutationConflict(
                    "plan-consumer rename receipt is forged, expired, consumed, or "
                    "forked"
                )
            if identifier in _RENAME_CLAIMS:
                raise PlanConsumerViewMutationConflict(
                    "plan-consumer rename receipt already has an active cleanup"
                )
            _RENAME_CLAIMS.add(identifier)
            claimed = True
            lease = _ClaimedRenameReceipt(receipt, entry.record, identifier)
        yield lease
    finally:
        if claimed and (lease is None or not lease.completed):
            with block_deferred_signals(), _rename_locked():
                _RENAME_CLAIMS.discard(identifier)


def _named_directory_identity(
    parent_descriptor: int,
    name: str,
    *,
    where: str,
) -> PlanConsumerDirectoryIdentity:
    try:
        observed = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except OSError as exc:
        raise PlanConsumerViewMutationConflict(
            f"{where} disappeared or became unreadable"
        ) from exc
    if not stat.S_ISDIR(observed.st_mode):
        raise PlanConsumerViewMutationConflict(f"{where} must be a real directory")
    return PlanConsumerDirectoryIdentity(
        observed.st_dev,
        observed.st_ino,
        stat.S_IFMT(observed.st_mode),
    )


def _optional_named_directory_identity(
    parent_descriptor: int,
    name: str,
    *,
    where: str,
) -> PlanConsumerDirectoryIdentity | None:
    try:
        observed = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PlanConsumerViewMutationConflict(f"{where} is unreadable") from exc
    if not stat.S_ISDIR(observed.st_mode):
        raise PlanConsumerViewMutationConflict(f"{where} must be a real directory")
    return PlanConsumerDirectoryIdentity(
        observed.st_dev,
        observed.st_ino,
        stat.S_IFMT(observed.st_mode),
    )


def _recover_failed_begin(
    transaction: _InstallationRenameRecord,
    scratch_descriptor: int,
) -> None:
    """Recover whether either rename completed before its call returned."""

    temporary_identity = _optional_named_directory_identity(
        scratch_descriptor,
        transaction.temporary.name,
        where="failed-install temporary generation",
    )
    installed_identity = _optional_named_directory_identity(
        scratch_descriptor,
        transaction.installed.name,
        where="failed-install destination generation",
    )
    if installed_identity == transaction.installed_identity:
        if temporary_identity is not None:
            raise PlanConsumerViewMutationConflict(
                "failed installation exposes duplicate allocated directory identities"
            )
        move_owned_directory_noreplace(
            scratch_descriptor,
            transaction.installed.name,
            transaction.temporary.name,
            transaction.installed_identity,
            where="failed-install new-generation recovery",
        )
        installed_identity = None
        temporary_identity = transaction.installed_identity
    if temporary_identity != transaction.installed_identity:
        raise PlanConsumerViewMutationConflict(
            "failed installation no longer names the exact allocated temporary"
        )

    if transaction.previous is None:
        if installed_identity is not None:
            raise PlanConsumerViewMutationConflict(
                "failed first installation left an unexpected destination generation"
            )
        os.fsync(scratch_descriptor)
        return
    if transaction.previous_identity is None:
        raise PlanConsumerViewMutationConflict(
            "failed replacement omitted the prior view identity"
        )
    backup_identity = _optional_named_directory_identity(
        scratch_descriptor,
        transaction.previous.name,
        where="failed-install prior rollback generation",
    )
    if backup_identity == transaction.previous_identity:
        if installed_identity is not None:
            raise PlanConsumerViewMutationConflict(
                "failed replacement retained both prior destination and rollback generation"
            )
        move_owned_directory_noreplace(
            scratch_descriptor,
            transaction.previous.name,
            transaction.installed.name,
            transaction.previous_identity,
            where="failed-install predecessor recovery",
        )
    elif installed_identity == transaction.previous_identity:
        if backup_identity is not None:
            raise PlanConsumerViewMutationConflict(
                "failed replacement exposes an unexpected rollback generation"
            )
    else:
        raise PlanConsumerViewMutationConflict(
            "failed replacement no longer names the exact prior generation"
        )
    os.fsync(scratch_descriptor)


def _begin_plan_consumer_view_installation_rename(
    *,
    scratch: Path,
    temporary: Path,
    installed: Path,
    expected_identity: PlanConsumerDirectoryIdentity,
    expected_scratch_identity: PlanConsumerDirectoryIdentity,
    forbidden_identity: PlanConsumerDirectoryIdentity,
    continue_installation: Callable[
        [_InstallationRenameRecord], PlanConsumerViewInstallationRename
    ],
    installation_is_committed: Callable[[], bool],
) -> PlanConsumerViewInstallationRename:
    """Move temp into place while retaining an exact rollback generation."""

    scratch = absolute_path(scratch)
    temporary = absolute_path(temporary)
    installed = absolute_path(installed)
    if temporary.parent != scratch or installed.parent != scratch:
        raise PlanConsumerViewMutationConflict(
            "plan-consumer installation names must be exact scratch children"
        )
    previous: Path | None = None
    previous_identity: PlanConsumerDirectoryIdentity | None = None
    with managed_fork_protected_acquisition() as acquisition:
        scratch_descriptor = open_real_directory(scratch, acquisition)
        if (
            PlanConsumerDirectoryIdentity.capture(scratch_descriptor)
            != expected_scratch_identity
        ):
            raise PlanConsumerViewMutationConflict(
                "plan-consumer scratch root changed before installation rename"
            )
        temporary_descriptor = open_real_directory(temporary, acquisition)
        temporary_identity = PlanConsumerDirectoryIdentity.capture(
            temporary_descriptor
        )
        named_temporary_identity = _named_directory_identity(
            scratch_descriptor,
            temporary.name,
            where="allocated plan-consumer temporary",
        )
        if (
            temporary_identity != expected_identity
            or named_temporary_identity != expected_identity
        ):
            raise PlanConsumerViewMutationConflict(
                "allocated plan-consumer temporary changed before installation"
            )
        previous_identity = _optional_named_directory_identity(
            scratch_descriptor,
            installed.name,
            where="prior plan-consumer view",
        )
        if previous_identity == forbidden_identity:
            raise PlanConsumerViewMutationConflict(
                "prior plan-consumer destination physically aliases the canonical shot root"
            )
        if previous_identity is not None:
            previous = scratch / (
                f".plan-consumer-view.previous-{secrets.token_hex(16)}"
            )
        transaction = _InstallationRenameRecord(
            scratch=scratch,
            temporary=temporary,
            installed=installed,
            installed_identity=expected_identity,
            previous=previous,
            previous_identity=previous_identity,
        )
        try:
            if previous is not None:
                for attempt in range(32):
                    if attempt:
                        previous = scratch / (
                            f".plan-consumer-view.previous-{secrets.token_hex(16)}"
                        )
                    transaction = _InstallationRenameRecord(
                        scratch=scratch,
                        temporary=temporary,
                        installed=installed,
                        installed_identity=expected_identity,
                        previous=previous,
                        previous_identity=previous_identity,
                    )
                    try:
                        move_owned_directory_noreplace(
                            scratch_descriptor,
                            installed.name,
                            previous.name,
                            previous_identity,
                            where="plan-consumer predecessor staging",
                        )
                    except _NoReplaceDestinationOccupied:
                        continue
                    break
                else:  # pragma: no cover - cryptographic collision bound
                    raise PlanConsumerViewMutationConflict(
                        "could not move the prior plan-consumer view to a unique "
                        "no-replace name"
                    )
                if (
                    _named_directory_identity(
                        scratch_descriptor,
                        transaction.previous.name,
                        where="prior plan-consumer rollback generation",
                    )
                    != previous_identity
                ):
                    raise PlanConsumerViewMutationConflict(
                        "prior plan-consumer view changed during rollback staging"
                    )
            move_owned_directory_noreplace(
                scratch_descriptor,
                temporary.name,
                installed.name,
                expected_identity,
                where="plan-consumer generation installation",
            )
            if (
                _named_directory_identity(
                    scratch_descriptor,
                    installed.name,
                    where="newly installed plan-consumer view",
                )
                != expected_identity
            ):
                raise PlanConsumerViewMutationConflict(
                    "plan-consumer temporary changed during installation"
                )
            os.fsync(scratch_descriptor)
            return continue_installation(transaction)
        except BaseException as exc:
            if installation_is_committed():
                raise
            try:
                _recover_failed_begin(transaction, scratch_descriptor)
            except BaseException as rollback_exc:
                rollback_failure = PlanConsumerViewMutationConflict(
                    "plan-consumer installation failed and exact rollback failed; "
                    "route to engineering"
                )
                rollback_failure.add_note(
                    f"rollback diagnostic: {type(rollback_exc).__name__}: "
                    f"{rollback_exc}"
                )
                raise rollback_failure from exc
            if isinstance(exc, OSError):
                raise PlanConsumerViewMutationConflict(
                    "plan-consumer installation rename failed; the prior generation "
                    "was restored"
                ) from exc
            raise


def _install_and_verify_allocated_plan_consumer_view(
    *,
    shot: Path,
    scratch: Path,
    temporary: Path,
    installed: Path,
    expected_shot_identity: PlanConsumerDirectoryIdentity,
    expected_scratch_identity: PlanConsumerDirectoryIdentity,
    expected_identity: PlanConsumerDirectoryIdentity,
    marker_sha256: str,
    ledger_binding: PlanConsumerLedgerBinding,
    register_installation: Callable[[PlanConsumerViewInstallationRename], None],
    registration_is_current: Callable[
        [PlanConsumerViewInstallationRename], bool
    ],
) -> PlanConsumerViewInstallationRename:
    """Refuse shot aliases, rename with rollback, and verify the installed inode."""

    shot = absolute_path(shot)
    scratch = absolute_path(scratch)
    installed = absolute_path(installed)
    if installed == shot:
        raise PlanConsumerViewMutationConflict(
            "installed plan-consumer destination is the canonical shot root"
        )
    receipt: PlanConsumerViewInstallationRename | None = None

    def installation_is_committed() -> bool:
        return receipt is not None and registration_is_current(receipt)

    def verify_and_register(
        _transaction: _InstallationRenameRecord,
    ) -> PlanConsumerViewInstallationRename:
        nonlocal receipt
        with managed_fork_protected_acquisition() as verification:
            current_shot = open_real_directory(shot, verification)
            current_scratch = open_real_directory(scratch, verification)
            view_descriptor = open_real_directory(installed, verification)
            if (
                PlanConsumerDirectoryIdentity.capture(current_shot)
                != expected_shot_identity
                or PlanConsumerDirectoryIdentity.capture(current_scratch)
                != expected_scratch_identity
            ):
                raise PlanConsumerViewMutationConflict(
                    "shot or scratch generation changed during installation verification"
                )
            if expected_shot_identity == expected_identity:
                raise PlanConsumerViewMutationConflict(
                    "installed plan-consumer view became the canonical shot root"
                )
            identity = PlanConsumerDirectoryIdentity.capture(view_descriptor)
            named = _named_directory_identity(
                current_scratch,
                installed.name,
                where="installed plan-consumer view",
            )
            if named != expected_identity or identity != expected_identity:
                raise PlanConsumerViewMutationConflict(
                    "installed plan-consumer view is not the constructed temporary inode"
                )
            marker_payload = read_marker(view_descriptor, verification, installed)
            observed_ledger_binding = capture_ledger_binding_at(
                view_descriptor,
                installed,
            )
        if hashlib.sha256(marker_payload).hexdigest() != marker_sha256:
            raise PlanConsumerViewMutationConflict(
                "installed plan-consumer marker differs from the constructed generation"
            )
        if observed_ledger_binding != ledger_binding:
            raise PlanConsumerViewMutationConflict(
                "installed plan-consumer ledger differs from the constructed generation"
            )
        receipt = _mint_rename_receipt(_transaction)
        with block_deferred_signals():
            register_installation(receipt)
        return receipt

    with managed_fork_protected_acquisition() as acquisition:
        shot_descriptor = open_real_directory(shot, acquisition)
        scratch_descriptor = open_real_directory(scratch, acquisition)
        shot_identity = PlanConsumerDirectoryIdentity.capture(shot_descriptor)
        scratch_identity = PlanConsumerDirectoryIdentity.capture(scratch_descriptor)
        if (
            shot_identity != expected_shot_identity
            or scratch_identity != expected_scratch_identity
        ):
            raise PlanConsumerViewMutationConflict(
                "shot or scratch generation changed after construction"
            )
        destination_identity = _optional_named_directory_identity(
            scratch_descriptor,
            installed.name,
            where="installed plan-consumer destination",
        )
        if destination_identity == shot_identity:
            raise PlanConsumerViewMutationConflict(
                "installed plan-consumer destination physically aliases the "
                "canonical shot root"
            )
        return _begin_plan_consumer_view_installation_rename(
            scratch=scratch,
            temporary=temporary,
            installed=installed,
            expected_identity=expected_identity,
            expected_scratch_identity=expected_scratch_identity,
            forbidden_identity=shot_identity,
            continue_installation=verify_and_register,
            installation_is_committed=installation_is_committed,
        )


def require_plan_consumer_view_install_destination_isolated(
    *,
    shot: Path,
    scratch: Path,
    installed: Path,
) -> None:
    """Prove the final name is neither lexical nor physical shot-root authority."""

    shot = absolute_path(shot)
    scratch = absolute_path(scratch)
    installed = absolute_path(installed)
    if installed == shot:
        raise PlanConsumerViewMutationConflict(
            "installed plan-consumer destination is the canonical shot root"
        )
    with managed_fork_protected_acquisition() as acquisition:
        shot_descriptor = open_real_directory(shot, acquisition)
        scratch_descriptor = open_real_directory(scratch, acquisition)
        shot_identity = PlanConsumerDirectoryIdentity.capture(shot_descriptor)
        destination_identity = _optional_named_directory_identity(
            scratch_descriptor,
            installed.name,
            where="installed plan-consumer destination",
        )
        if destination_identity == shot_identity:
            raise PlanConsumerViewMutationConflict(
                "installed plan-consumer destination physically aliases the "
                "canonical shot root"
            )


def _discard_prepared_plan_consumer_temporary(
    *,
    shot: Path,
    scratch: Path,
    temporary: Path,
    shot_identity: PlanConsumerDirectoryIdentity,
    scratch_identity: PlanConsumerDirectoryIdentity,
    temporary_identity: PlanConsumerDirectoryIdentity,
    marker_sha256: str,
    ledger_binding: PlanConsumerLedgerBinding,
) -> None:
    """Retire only an upper owner's exact still-named prepared generation."""

    with managed_fork_protected_acquisition() as acquisition:
        shot_descriptor = open_real_directory(shot, acquisition)
        scratch_descriptor = open_real_directory(scratch, acquisition)
        if (
            PlanConsumerDirectoryIdentity.capture(shot_descriptor) != shot_identity
            or PlanConsumerDirectoryIdentity.capture(scratch_descriptor)
            != scratch_identity
        ):
            raise PlanConsumerViewMutationConflict(
                "plan-consumer temporary cleanup source generation changed"
            )
        named = _optional_named_directory_identity(
            scratch_descriptor,
            temporary.name,
            where="prepared plan-consumer temporary cleanup target",
        )
        if named == temporary_identity:
            temporary_descriptor = open_real_directory(temporary, acquisition)
            marker_payload = read_marker(
                temporary_descriptor,
                acquisition,
                temporary,
            )
            if hashlib.sha256(marker_payload).hexdigest() != marker_sha256:
                raise PlanConsumerViewMutationConflict(
                    "prepared plan-consumer temporary marker changed before cleanup"
                )
            if (
                capture_ledger_binding_at(temporary_descriptor, temporary)
                != ledger_binding
            ):
                raise PlanConsumerViewMutationConflict(
                    "prepared plan-consumer temporary ledger changed before cleanup"
                )
        retire_owned_directory(
            scratch_descriptor,
            temporary.name,
            temporary_identity,
        )


def _discard_previous_plan_consumer_view(
    receipt: PlanConsumerViewInstallationRename,
    *,
    complete_registration: Callable[[], None],
) -> None:
    """Retire only the exact predecessor after registration succeeds."""

    with _claimed_rename_receipt(receipt) as claim:
        transaction = claim.record
        if transaction.previous is None:
            claim.complete(complete_registration)
            return
        if transaction.previous_identity is None:
            raise PlanConsumerViewMutationConflict(
                "registered plan-consumer install omitted prior view identity"
            )
        with managed_fork_protected_acquisition() as acquisition:
            scratch_descriptor = open_real_directory(
                transaction.scratch,
                acquisition,
            )
            retire_owned_directory(
                scratch_descriptor,
                transaction.previous.name,
                transaction.previous_identity,
            )
        claim.complete(complete_registration)


__all__: list[str] = []
