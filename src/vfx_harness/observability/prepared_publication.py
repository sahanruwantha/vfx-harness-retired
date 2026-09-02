"""Opaque compare-and-swap publication for mutable generated side files.

Preparation performs every content read and write outside the caller's authority
guard. It retains only read-only staged bytes plus exact directory and lock
descriptors. Commit resolves an opaque process-and-thread-bound capability, takes
the permanent target lock without waiting, compares the exact predecessor, and
renames the staged inode before stably rehashing that held generation.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import secrets
import stat
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, TypeVar

from vfx_harness.observability import (
    prepared_publication_descriptors as _descriptors,
)
from vfx_harness.observability import prepared_publication_registry as _registry
from vfx_harness.observability.prepared_publication_cleanup import (
    raise_prepared_transaction_errors,
)
from vfx_harness.observability.prepared_publication_destinations import (
    _PreparedPublicationDestinationAuthorization,
    absolute_prepared_publication_path,
    consume_prepared_publication_destination_authorization,
    normalize_prepared_publication_destination,
)
from vfx_harness.observability.prepared_publication_registry import (
    FileIdentity,
    PreparedFilePayloadVerification,
    PreparedFilePublication,
)

_T = TypeVar("_T")

_DIRECTORY_FLAGS = (
    os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
)
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
_CREATE_FLAGS = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
_LOCK_FLAGS = (
    os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
)
_LOCK_DIRECTORY = Path("state/publication-locks")

# Registry failures are publication conflicts at this public boundary. Keeping one
# class also means opaque-object property access fails in the same typed vocabulary.
FilePublicationConflict = _registry.PreparedPublicationRegistryError


@dataclass(frozen=True, slots=True)
class PreparedFileUpdate(Generic[_T]):
    """Typed preparation result; ``publication=None`` is an exact no-op."""

    publication: PreparedFilePublication | None
    result: _T


def _identity(observed: os.stat_result) -> FileIdentity:
    return FileIdentity(
        device=observed.st_dev,
        inode=observed.st_ino,
        size=observed.st_size,
        modified_ns=observed.st_mtime_ns,
        changed_ns=observed.st_ctime_ns,
    )


def _open_pending_real_directory(
    acquisition_token: object,
    path: Path,
    label: str,
) -> int:
    """Traverse a real directory with every intermediate fd registry-owned."""

    absolute = absolute_prepared_publication_path(path)
    try:
        current = _registry.open_pending_descriptor(
            acquisition_token,
            absolute.anchor,
            _DIRECTORY_FLAGS,
        )
        for part in absolute.parts[1:]:
            following = _registry.open_pending_descriptor(
                acquisition_token,
                part,
                _DIRECTORY_FLAGS,
                dir_fd=current,
            )
            _registry.retire_pending_descriptor(acquisition_token, current)
            current = following
    except OSError as exc:
        raise FilePublicationConflict(f"{label} must be a real non-symlink directory: {absolute}") from exc
    if not stat.S_ISDIR(os.fstat(current).st_mode):
        raise FilePublicationConflict(f"{label} must be a real directory: {absolute}")
    return current


def _open_or_create_relative_directory(
    acquisition_token: object,
    shot_descriptor: int,
    parts: tuple[str, ...],
    *,
    label: str,
) -> int:
    current = _registry.open_pending_descriptor(
        acquisition_token,
        ".",
        _DIRECTORY_FLAGS,
        dir_fd=shot_descriptor,
    )
    try:
        for part in parts:
            try:
                following = _registry.open_pending_descriptor(
                    acquisition_token,
                    part,
                    _DIRECTORY_FLAGS,
                    dir_fd=current,
                )
            except FileNotFoundError:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(part, 0o700, dir_fd=current)
                following = _registry.open_pending_descriptor(
                    acquisition_token,
                    part,
                    _DIRECTORY_FLAGS,
                    dir_fd=current,
                )
                os.fsync(current)
                os.fsync(following)
            _registry.retire_pending_descriptor(acquisition_token, current)
            current = following
        return current
    except OSError as exc:
        raise FilePublicationConflict(f"{label} must contain only real directory components") from exc


def _read_current(
    acquisition_token: object,
    directory: int,
    name: str,
    path: Path,
) -> tuple[bytes | None, FileIdentity | None, str | None]:
    try:
        descriptor = _registry.open_pending_descriptor(
            acquisition_token,
            name,
            _READ_FLAGS,
            dir_fd=directory,
        )
    except FileNotFoundError:
        return None, None, None
    except OSError as exc:
        raise FilePublicationConflict(f"side-file target must be absent or a real regular file: {path}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise FilePublicationConflict(f"side-file target must be absent or a real regular file: {path}")
        digest = hashlib.sha256()
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
            digest.update(chunk)
        after = os.fstat(descriptor)
        if _identity(after) != _identity(before):
            raise FilePublicationConflict(f"side-file target changed while it was prepared: {path}")
        return b"".join(chunks), _identity(after), digest.hexdigest()
    finally:
        _registry.retire_pending_descriptor(acquisition_token, descriptor)


def _current_identity(directory: int, name: str, path: Path) -> FileIdentity | None:
    try:
        observed = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise FilePublicationConflict(f"side-file target is unreadable: {path}") from exc
    if not stat.S_ISREG(observed.st_mode):
        raise FilePublicationConflict(f"side-file target must be absent or a real regular file: {path}")
    return _identity(observed)


def _open_publication_lock(
    acquisition_token: object,
    shot_descriptor: int,
    shot: Path,
    relative: Path,
) -> tuple[Path, int, tuple[int, int], int, str, FileIdentity]:
    lock_parent_descriptor = _open_or_create_relative_directory(
        acquisition_token,
        shot_descriptor,
        _LOCK_DIRECTORY.parts,
        label="side-file publication lock directory",
    )
    lock_parent = shot / _LOCK_DIRECTORY
    parent_stat = os.fstat(lock_parent_descriptor)
    lock_name = hashlib.sha256(str(relative).encode("utf-8")).hexdigest() + ".lock"
    try:
        lock_descriptor = _registry.open_pending_descriptor(
            acquisition_token,
            lock_name,
            _LOCK_FLAGS,
            0o600,
            dir_fd=lock_parent_descriptor,
        )
    except OSError as exc:
        raise FilePublicationConflict(
            f"side-file publication lock must be a real regular file: {lock_parent / lock_name}"
        ) from exc
    lock_stat = os.fstat(lock_descriptor)
    try:
        named = os.stat(lock_name, dir_fd=lock_parent_descriptor, follow_symlinks=False)
    except OSError as exc:
        raise FilePublicationConflict(f"side-file publication lock is not stable: {lock_parent / lock_name}") from exc
    if (
        not stat.S_ISREG(lock_stat.st_mode)
        or not stat.S_ISREG(named.st_mode)
        or _identity(lock_stat) != _identity(named)
    ):
        raise FilePublicationConflict(
            f"side-file publication lock must be a stable regular file: {lock_parent / lock_name}"
        )
    os.fsync(lock_descriptor)
    os.fsync(lock_parent_descriptor)
    return (
        lock_parent,
        lock_parent_descriptor,
        (parent_stat.st_dev, parent_stat.st_ino),
        lock_descriptor,
        lock_name,
        _identity(os.fstat(lock_descriptor)),
    )


def _require_directory_current(
    held_descriptor: int,
    path: Path,
    expected: tuple[int, int],
    label: str,
) -> None:
    try:
        held = os.fstat(held_descriptor)
    except OSError as exc:
        raise FilePublicationConflict(f"{label} descriptor is no longer valid: {path}") from exc
    acquisition_token = object()
    try:
        _registry.arm_descriptor_acquisition(acquisition_token)
        descriptor = _open_pending_real_directory(
            acquisition_token,
            path,
            label,
        )
        live = os.fstat(descriptor)
    finally:
        _registry.abort_descriptor_acquisition(acquisition_token)
    held_identity = (held.st_dev, held.st_ino)
    live_identity = (live.st_dev, live.st_ino)
    if (
        not stat.S_ISDIR(held.st_mode)
        or not stat.S_ISDIR(live.st_mode)
        or held_identity != expected
        or live_identity != expected
        or held_identity != live_identity
    ):
        raise FilePublicationConflict(f"{label} changed after side-file preparation: {path}")


def _require_lock_current(record: _registry._PreparedFilePublicationRecord) -> None:
    try:
        held = os.fstat(record.lock_descriptor)
        named = os.stat(
            record.lock_name,
            dir_fd=record.lock_parent_descriptor,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise FilePublicationConflict(
            f"side-file publication lock disappeared: {record.lock_parent / record.lock_name}"
        ) from exc
    if (
        not stat.S_ISREG(held.st_mode)
        or not stat.S_ISREG(named.st_mode)
        or _identity(held) != record.lock_identity
        or _identity(named) != record.lock_identity
    ):
        raise FilePublicationConflict(f"side-file publication lock changed: {record.lock_parent / record.lock_name}")


def _require_temporary_current(
    record: _registry._PreparedFilePublicationRecord,
) -> FileIdentity:
    try:
        held = os.fstat(record.temporary_descriptor)
        named = os.stat(
            record.temporary_name,
            dir_fd=record.destination_parent_descriptor,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise FilePublicationConflict(f"prepared side-file inode disappeared: {record.temporary}") from exc
    held_identity = _identity(held)
    if (
        not stat.S_ISREG(held.st_mode)
        or not stat.S_ISREG(named.st_mode)
        or held_identity != record.temporary_identity
        or _identity(named) != record.temporary_identity
    ):
        raise FilePublicationConflict(f"prepared side-file inode changed: {record.temporary}")
    return held_identity


def _require_commit_inputs_current(
    record: _registry._PreparedFilePublicationRecord,
) -> FileIdentity:
    """Reopen every named input and prove the registered CAS generation."""

    _require_directory_current(
        record.shot_descriptor,
        record.shot,
        record.shot_identity,
        "side-file shot root",
    )
    _require_directory_current(
        record.lock_parent_descriptor,
        record.lock_parent,
        record.lock_parent_identity,
        "side-file publication lock directory",
    )
    _require_lock_current(record)
    _require_directory_current(
        record.destination_parent_descriptor,
        record.destination_parent,
        record.destination_parent_identity,
        "side-file destination parent",
    )
    if (
        _current_identity(
            record.destination_parent_descriptor,
            record.destination_name,
            record.destination,
        )
        != record.predecessor
    ):
        raise FilePublicationConflict(
            f"side-file target changed after preparation: {record.destination}; no update was written"
        )
    return _require_temporary_current(record)


def _require_published_current(
    record: _registry._PreparedFilePublicationRecord,
    before_replace: FileIdentity,
) -> None:
    """Require stable held bytes from the exact renamed staged generation."""

    try:
        held_before = os.fstat(record.temporary_descriptor)
        named_before = os.stat(
            record.destination_name,
            dir_fd=record.destination_parent_descriptor,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise FilePublicationConflict(f"published side-file inode disappeared: {record.destination}") from exc
    held_before_identity = _identity(held_before)
    named_before_identity = _identity(named_before)
    if (
        not stat.S_ISREG(held_before.st_mode)
        or not stat.S_ISREG(named_before.st_mode)
        or held_before_identity != named_before_identity
        or (held_before_identity.device, held_before_identity.inode) != (before_replace.device, before_replace.inode)
        or held_before_identity.size != before_replace.size
        or held_before_identity.modified_ns != before_replace.modified_ns
        # A same-directory rename legitimately advances ctime on supported POSIX
        # filesystems. Regression would mean this is not the staged generation.
        or held_before_identity.changed_ns < before_replace.changed_ns
    ):
        raise FilePublicationConflict(f"published side-file inode changed: {record.destination}")

    digest = hashlib.sha256()
    offset = 0
    while chunk := os.pread(record.temporary_descriptor, 1024 * 1024, offset):
        digest.update(chunk)
        offset += len(chunk)
    try:
        held_after = os.fstat(record.temporary_descriptor)
        named_after = os.stat(
            record.destination_name,
            dir_fd=record.destination_parent_descriptor,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise FilePublicationConflict(f"published side-file inode disappeared: {record.destination}") from exc
    held_after_identity = _identity(held_after)
    if (
        not stat.S_ISREG(held_after.st_mode)
        or not stat.S_ISREG(named_after.st_mode)
        or held_after_identity != held_before_identity
        or _identity(named_after) != held_after_identity
    ):
        raise FilePublicationConflict(f"published side-file inode changed: {record.destination}")
    if digest.hexdigest() != record.payload_sha256:
        raise FilePublicationConflict(
            f"published side-file bytes do not match the prepared payload: {record.destination}"
        )


def prepare_file_update(
    shot_folder: str | Path,
    destination: str | Path,
    update: Callable[[bytes | None], tuple[bytes | None, _T]],
    *,
    authority_binding: str,
    commit_policy: Callable[[object, object], None] | None = None,
    destination_authorization: (
        _PreparedPublicationDestinationAuthorization | None
    ) = None,
) -> PreparedFileUpdate[_T]:
    """Read, merge, and stage one exact replacement as an opaque transaction."""

    if not isinstance(authority_binding, str) or not authority_binding.strip():
        raise FilePublicationConflict("side-file preparation requires a non-empty authority binding")
    if commit_policy is not None and not callable(commit_policy):
        raise FilePublicationConflict("side-file commit policy must be callable")
    shot, destination_path, relative = (
        normalize_prepared_publication_destination(shot_folder, destination)
    )
    protected_family = consume_prepared_publication_destination_authorization(
        destination_authorization,
        shot=shot,
        target=destination_path,
        relative=relative,
    )
    if protected_family is not None and commit_policy is None:
        raise FilePublicationConflict(
            f"protected {protected_family} destination requires a stored commit policy"
        )
    acquisition_token = object()
    temporary_name: str | None = None
    temporary_identity: FileIdentity | None = None
    publication: PreparedFilePublication | None = None
    parent_descriptor: int | None = None
    writable_descriptor: int | None = None
    try:
        # The caller owns the token before registry mutation, closing the
        # function-return interruption gap around acquisition setup.
        _registry.arm_descriptor_acquisition(acquisition_token)
        shot_descriptor = _open_pending_real_directory(
            acquisition_token,
            shot,
            "side-file shot root",
        )
        shot_stat = os.fstat(shot_descriptor)
        parent_descriptor = _open_or_create_relative_directory(
            acquisition_token,
            shot_descriptor,
            relative.parent.parts if relative.parent != Path(".") else (),
            label="side-file destination parent",
        )
        parent_stat = os.fstat(parent_descriptor)
        current, predecessor, predecessor_sha256 = _read_current(
            acquisition_token,
            parent_descriptor,
            destination_path.name,
            destination_path,
        )
        payload, result = update(current)
        if payload is None:
            _registry.abort_descriptor_acquisition(acquisition_token)
            return PreparedFileUpdate(publication=None, result=result)
        if not isinstance(payload, bytes):
            raise FilePublicationConflict("side-file update must return bytes or an exact no-op")
        (
            lock_parent,
            lock_parent_descriptor,
            lock_parent_identity,
            lock_descriptor,
            lock_name,
            lock_identity,
        ) = _open_publication_lock(
            acquisition_token,
            shot_descriptor,
            shot,
            relative,
        )
        for _attempt in range(128):
            temporary_name = f".{destination_path.name}.prepared.{secrets.token_hex(12)}"
            _registry.bind_pending_temporary(
                acquisition_token,
                parent_descriptor,
                temporary_name,
            )
            try:
                writable_descriptor = _registry.open_pending_descriptor(
                    acquisition_token,
                    temporary_name,
                    _CREATE_FLAGS,
                    0o600,
                    dir_fd=parent_descriptor,
                )
                break
            except FileExistsError:
                _registry.clear_pending_temporary(acquisition_token)
                temporary_name = None
            except OSError as exc:
                raise FilePublicationConflict("could not allocate a same-parent side-file preparation") from exc
        else:
            raise FilePublicationConflict("could not allocate a unique same-parent side-file preparation")
        assert temporary_name is not None and writable_descriptor is not None
        temporary_identity = _identity(os.fstat(writable_descriptor))
        _descriptors.write_all(writable_descriptor, payload)
        os.fsync(writable_descriptor)
        before_hash = _identity(os.fstat(writable_descriptor))
        observed_digest = hashlib.sha256()
        offset = 0
        while chunk := os.pread(writable_descriptor, 1024 * 1024, offset):
            observed_digest.update(chunk)
            offset += len(chunk)
        after_hash = _identity(os.fstat(writable_descriptor))
        payload_sha256 = hashlib.sha256(payload).hexdigest()
        if before_hash != after_hash or observed_digest.hexdigest() != payload_sha256:
            raise FilePublicationConflict(f"prepared side-file bytes changed before publication: {destination_path}")
        temporary_identity = after_hash

        # Acquire the read-only owner before releasing the writable one.  The
        # pending row therefore owns at least one exact staged-inode descriptor
        # across every interruptible handoff boundary.
        readonly_descriptor = _registry.open_pending_descriptor(
            acquisition_token,
            temporary_name,
            _READ_FLAGS,
            dir_fd=parent_descriptor,
        )
        readonly_identity = _identity(os.fstat(readonly_descriptor))
        named_identity = _identity(
            os.stat(
                temporary_name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        )
        if readonly_identity != temporary_identity or named_identity != temporary_identity:
            raise FilePublicationConflict(
                f"prepared side-file inode changed while it became read-only: {destination_path}"
            )
        _registry.retire_pending_descriptor(acquisition_token, writable_descriptor)
        readonly_identity = _identity(os.fstat(readonly_descriptor))
        named_identity = _identity(
            os.stat(
                temporary_name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        )
        if readonly_identity != temporary_identity or named_identity != temporary_identity:
            raise FilePublicationConflict(
                f"prepared side-file inode changed while writable access retired: {destination_path}"
            )
        descriptors = (
            shot_descriptor,
            parent_descriptor,
            lock_parent_descriptor,
            lock_descriptor,
            readonly_descriptor,
        )
        guarded = _registry.pending_descriptor_identities(
            acquisition_token,
            descriptors,
        )
        temporary_guard = next(item for item in guarded if item.descriptor == readonly_descriptor)
        record = _registry._PreparedFilePublicationRecord(
            shot=shot,
            shot_descriptor=shot_descriptor,
            shot_identity=(shot_stat.st_dev, shot_stat.st_ino),
            relative_path=relative,
            destination=destination_path,
            destination_parent=destination_path.parent,
            destination_parent_descriptor=parent_descriptor,
            destination_parent_identity=(parent_stat.st_dev, parent_stat.st_ino),
            destination_name=destination_path.name,
            predecessor=predecessor,
            predecessor_sha256=predecessor_sha256,
            temporary=destination_path.parent / temporary_name,
            temporary_descriptor=readonly_descriptor,
            temporary_guard=temporary_guard,
            temporary_name=temporary_name,
            temporary_identity=temporary_identity,
            payload_sha256=payload_sha256,
            lock_parent=lock_parent,
            lock_parent_descriptor=lock_parent_descriptor,
            lock_parent_identity=lock_parent_identity,
            lock_descriptor=lock_descriptor,
            lock_name=lock_name,
            lock_identity=lock_identity,
            authority_binding=authority_binding,
            commit_policy=commit_policy,
            process_id=os.getpid(),
            process_token=_registry.current_registry_process_token(),
            thread_id=threading.get_ident(),
            thread_token=_registry.current_registry_thread_token(),
            guarded_descriptors=guarded,
        )
        publication = _registry.register_prepared_file(
            record,
            acquisition_token=acquisition_token,
        )
        return PreparedFileUpdate(publication=publication, result=result)
    except BaseException as body_error:
        cleanup_errors: list[BaseException] = []
        try:
            if publication is None:
                _registry.abort_descriptor_acquisition(acquisition_token)
            else:
                _registry.abandon_prepared_file_registration(publication)
        except BaseException as exc:
            cleanup_errors.append(exc)
        if cleanup_errors:
            failure = FilePublicationConflict("prepared side-file acquisition cleanup retained live resources")
            for diagnostic in cleanup_errors[1:]:
                failure.add_note(f"cleanup diagnostic: {type(diagnostic).__name__}: {diagnostic}")
            raise failure from cleanup_errors[0]
        raise body_error


def verify_prepared_file_payload(
    prepared: PreparedFilePublication,
    *,
    expected_sha256: str,
    transaction_binding: object | None = None,
) -> PreparedFilePayloadVerification:
    """Hash the held read-only inode and mint a one-generation exact proof."""

    if (
        not isinstance(expected_sha256, str)
        or len(expected_sha256) != 64
        or any(character not in "0123456789abcdef" for character in expected_sha256)
    ):
        raise FilePublicationConflict("prepared payload verification requires a lowercase SHA-256 digest")
    record = _registry.require_live_prepared_file(prepared)
    _require_directory_current(
        record.destination_parent_descriptor,
        record.destination_parent,
        record.destination_parent_identity,
        "side-file destination parent",
    )
    _require_temporary_current(record)
    before = _identity(os.fstat(record.temporary_descriptor))
    digest = hashlib.sha256()
    offset = 0
    while chunk := os.pread(record.temporary_descriptor, 1024 * 1024, offset):
        digest.update(chunk)
        offset += len(chunk)
    after = _identity(os.fstat(record.temporary_descriptor))
    if before != after:
        raise FilePublicationConflict(f"prepared side-file bytes changed while they were verified: {record.temporary}")
    _require_temporary_current(record)
    if (
        after != record.temporary_identity
        or digest.hexdigest() != expected_sha256
        or record.payload_sha256 != expected_sha256
    ):
        raise FilePublicationConflict(f"prepared side-file bytes do not match typed authority: {record.destination}")
    return _registry.mint_payload_verification(
        prepared,
        expected_sha256=expected_sha256,
        temporary_identity=after,
        transaction_binding=transaction_binding,
    )


def _consume_payload_verification_current(
    prepared: PreparedFilePublication,
    record: _registry._PreparedFilePublicationRecord,
    verification: PreparedFilePayloadVerification,
    transaction_binding: object | None,
) -> None:
    proof = _registry.consume_payload_verification(
        prepared,
        verification,
        transaction_binding=transaction_binding,
    )
    observed = _identity(os.fstat(record.temporary_descriptor))
    if (
        proof.expected_sha256 != record.payload_sha256
        or proof.temporary_identity != record.temporary_identity
        or observed != proof.temporary_identity
    ):
        raise FilePublicationConflict(f"prepared side-file bytes changed after verification: {record.temporary}")


def _acquire_publication_lock(
    record: _registry._PreparedFilePublicationRecord,
    *,
    contention: str,
) -> None:
    try:
        fcntl.flock(record.lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise FilePublicationConflict(f"{contention}: {record.destination}") from exc
    except OSError as exc:
        raise FilePublicationConflict(f"could not acquire side-file publication lock: {record.destination}") from exc


def commit_prepared_file(
    prepared: PreparedFilePublication,
    *,
    authority_binding: str,
    payload_verification: PreparedFilePayloadVerification | None = None,
    transaction_binding: object | None = None,
    commit_authorization: object | None = None,
) -> str:
    """Perform one policy-bound CAS rename and stable post-rename hash."""

    record = _registry.require_live_prepared_file(prepared)
    if authority_binding != record.authority_binding:
        raise FilePublicationConflict("prepared side-file publication belongs to another authority binding")
    if record.commit_policy is None and commit_authorization is not None:
        raise FilePublicationConflict(
            "unbound side-file publication cannot consume a commit authorization"
        )
    if record.commit_policy is not None and commit_authorization is None:
        raise FilePublicationConflict(
            "policy-bound side-file publication requires its commit authorization"
        )
    if record.commit_policy is not None and transaction_binding is None:
        raise FilePublicationConflict(
            "policy-bound side-file publication requires its exact transaction binding"
        )
    replace_attempted = False
    committed = False
    commit_token = _registry._PreparedFileCommitLease()
    body_error: BaseException | None = None
    result: str | None = None
    cleanup_error: BaseException | None = None
    try:
        try:
            _acquire_publication_lock(
                record,
                contention="side-file publication is already committing; retry from current bytes",
            )
            staged_before_replace = _require_commit_inputs_current(record)
            if payload_verification is None:
                if transaction_binding is not None and record.commit_policy is None:
                    raise FilePublicationConflict("prepared transaction binding requires a payload verification")
                _registry.require_no_payload_verification(prepared)
            else:
                _consume_payload_verification_current(
                    prepared,
                    record,
                    payload_verification,
                    transaction_binding,
                )
            # The token is allocated before the registry transition, so an
            # interruption at the call/return boundary can still unwind only its own
            # exact phase. Reentrant commit or discard is rejected from this point.
            _registry.begin_prepared_file_commit(prepared, commit_token)
            if record.commit_policy is not None:
                assert transaction_binding is not None
                record.commit_policy(commit_authorization, transaction_binding)
                _acquire_publication_lock(
                    record,
                    contention="side-file publication lock ownership changed during final authority validation",
                )
            # Recheck the exact registered physical inputs after the stored policy
            # returns so policy code cannot mutate what the rename will publish.
            if _require_commit_inputs_current(record) != staged_before_replace:
                raise FilePublicationConflict(f"prepared side-file inode changed: {record.temporary}")
            replace_attempted = True
            os.replace(
                record.temporary_name,
                record.destination_name,
                src_dir_fd=record.destination_parent_descriptor,
                dst_dir_fd=record.destination_parent_descriptor,
            )
            _require_published_current(record, staged_before_replace)
            _require_directory_current(
                record.destination_parent_descriptor,
                record.destination_parent,
                record.destination_parent_identity,
                "side-file destination parent",
            )
            os.fsync(record.destination_parent_descriptor)
            committed = True
            result = record.payload_sha256
        except FilePublicationConflict as exc:
            body_error = exc
        except OSError as exc:
            body_error = FilePublicationConflict(f"could not durably publish side file: {record.destination}")
            body_error.__cause__ = exc
        except BaseException as exc:
            body_error = exc
    finally:
        try:
            try:
                fcntl.flock(record.lock_descriptor, fcntl.LOCK_UN)
            except BaseException as exc:
                cleanup_error = exc
            finally:
                # No caller callback can run after this point. Expiring the
                # stack lease before the cleanup call makes a call-entry
                # interruption recoverable through exact-object discard.
                commit_token.active = False
                try:
                    if committed or replace_attempted:
                        _registry.complete_prepared_file_commit(
                            prepared,
                            commit_token,
                            unlink_temporary=not committed,
                        )
                    else:
                        _registry.cancel_prepared_file_commit(prepared, commit_token)
                except BaseException as exc:
                    if cleanup_error is None:
                        cleanup_error = exc
                    else:
                        cleanup_error.add_note(f"publication cleanup diagnostic: {type(exc).__name__}: {exc}")
        finally:
            commit_token.active = False
    raise_prepared_transaction_errors(
        body_error,
        cleanup_error,
        record.destination,
    )
    assert result is not None
    return result


def discard_prepared_file(prepared: PreparedFilePublication | None) -> None:
    """Consume and remove only the exact registered uncommitted temporary.

    Repeating cleanup with the same minted object is inert for existing ``finally``
    blocks. The consumed object has no remaining commit, verification, or descriptor
    authority; an unregistered or copied object is still rejected.
    """

    if prepared is None:
        return
    if not isinstance(prepared, PreparedFilePublication):
        return
    _registry.consume_prepared_file(prepared, unlink_temporary=True)


def publish_file_update(
    shot_folder: str | Path,
    destination: str | Path,
    update: Callable[[bytes | None], tuple[bytes | None, _T]],
    *,
    authority_binding: str,
) -> _T:
    """Prepare and immediately CAS-commit outside any broader authority guard."""

    prepared = prepare_file_update(
        shot_folder,
        destination,
        update,
        authority_binding=authority_binding,
    )
    if prepared.publication is None:
        return prepared.result
    try:
        commit_prepared_file(
            prepared.publication,
            authority_binding=authority_binding,
        )
    except BaseException as exc:
        try:
            discard_prepared_file(prepared.publication)
        except FilePublicationConflict as cleanup_error:
            exc.add_note(f"prepared publication cleanup diagnostic: {cleanup_error}")
        raise
    return prepared.result


__all__ = [
    "FileIdentity",
    "FilePublicationConflict",
    "PreparedFilePayloadVerification",
    "PreparedFilePublication",
    "PreparedFileUpdate",
    "commit_prepared_file",
    "discard_prepared_file",
    "prepare_file_update",
    "publish_file_update",
    "verify_prepared_file_payload",
]
