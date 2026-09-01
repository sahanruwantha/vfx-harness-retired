"""Low-level locking, CAS identity, and durable writes for authority pointers.

This module knows nothing about plan or JIT pointer schemas.  Callers parse those records,
then bind their monotone revisions to the exact bytes observed under the shared shot lock.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import secrets
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from threading import get_ident, local
from typing import Any, ClassVar

from vfx_harness.domain.authority_head_records import (
    AUTHORITY_SELECTION_TOKEN_SCHEMA,
    AuthorityHeadRecordError,
    canonical_json_bytes,
    parse_authority_selection_token,
)
from vfx_harness.observability.run_owner_fork_guard import (
    RunOwnerForkGuardCleanupError,
    managed_fork_protected_acquisition,
)
from vfx_harness.orchestration.authority_selection_process_registry import (
    AuthoritySelectionCleanupFailure,
    AuthoritySelectionConflict,
    DescriptorLeaseRegistration,
    canonical_authority_shot_path,
    current_process_token,
    descriptor_registry_mutation,
    neutralize_registered_descriptors,
    open_authority_directory_parts,
    process_bound_live_lock_identity,
    provisional_descriptor_registration,
    registration_is_current,
    require_selection_lease_identity,
    shot_process_mutex,
)

AUTHORITY_SELECTION_LOCK = Path("state/authority-selection/selection.lock")
_SELECTED_AUTHORITY_POINTERS = frozenset(
    {
        Path("plans/current.json"),
        Path("state/jit-layers/current.json"),
        Path("state/authority-state/current.json"),
        Path("state/authority-state/pending.json"),
    }
)
_FILE_READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_LOCK_OPEN_FLAGS = (
    os.O_RDWR
    | os.O_CREAT
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_TEMP_OPEN_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_THREAD_LOCKS = local()


@dataclass(frozen=True, slots=True)
class AuthoritySelectionToken:
    """Exact plan/JIT pointer heads observed under one shot-selection lock."""

    SCHEMA: ClassVar[str] = AUTHORITY_SELECTION_TOKEN_SCHEMA

    plan_revision: int
    plan_pointer_sha256: str | None
    jit_revision: int
    jit_pointer_sha256: str | None

    def __post_init__(self) -> None:
        try:
            parse_authority_selection_token(self.to_dict())
        except AuthorityHeadRecordError as exc:
            raise AuthoritySelectionConflict(str(exc)) from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "plan_revision": self.plan_revision,
            "plan_pointer_sha256": self.plan_pointer_sha256,
            "jit_revision": self.jit_revision,
            "jit_pointer_sha256": self.jit_pointer_sha256,
        }

    @classmethod
    def from_dict(cls, value: Any, where: str = "authority selection token") -> AuthoritySelectionToken:
        try:
            token = parse_authority_selection_token(value, where)
        except AuthorityHeadRecordError as exc:
            raise AuthoritySelectionConflict(str(exc)) from exc
        return cls(
            plan_revision=token.plan_revision,
            plan_pointer_sha256=token.plan_pointer_sha256,
            jit_revision=token.jit_revision,
            jit_pointer_sha256=token.jit_pointer_sha256,
        )


@dataclass(slots=True)
class _HeldAuthoritySelectionLock:
    shot: Path
    lock_path: Path
    shot_descriptor: int
    parent_descriptor: int
    lock_descriptor: int
    process_id: int
    process_token: str
    thread_id: int
    registration: DescriptorLeaseRegistration
    exclusive: bool
    depth: int


def _held_selection_locks() -> dict[str, _HeldAuthoritySelectionLock]:
    """Return this thread's re-entrant selection locks by canonical path."""

    held = getattr(_THREAD_LOCKS, "held", None)
    if held is None:
        held = {}
        _THREAD_LOCKS.held = held
    return held


def _require_selection_lock_current(
    binding: _HeldAuthoritySelectionLock,
    phase: str,
) -> None:
    """Require this process/thread lease and its complete namespace identity."""

    if (
        binding.process_id != os.getpid()
        or binding.process_token != current_process_token()
        or binding.thread_id != get_ident()
        or not registration_is_current(binding.registration)
    ):
        raise AuthoritySelectionConflict(
            "authority selection lock binding belongs to another process or thread"
        )
    require_selection_lease_identity(
        shot=binding.shot,
        shot_descriptor=binding.shot_descriptor,
        lock_parent_descriptor=binding.parent_descriptor,
        lock_descriptor=binding.lock_descriptor,
        lock_relative=AUTHORITY_SELECTION_LOCK,
        phase=phase,
    )


def require_current_authority_selection_lock(
    shot_folder: str | Path,
    *,
    exclusive: bool,
) -> Path:
    """Revalidate this thread's exact live selection-lock lease."""

    shot = canonical_authority_shot_path(shot_folder)
    lock_path = shot / AUTHORITY_SELECTION_LOCK
    binding = _held_selection_locks().get(str(lock_path))
    if binding is None:
        raise AuthoritySelectionConflict(
            "authority selection lock is not active on this thread"
        )
    _require_selection_lock_current(binding, "while requiring the active lease")
    if exclusive and not binding.exclusive:
        raise AuthoritySelectionConflict(
            "authority operation requires an exclusive selection-lock lease"
        )
    return lock_path


def pointer_sha256(pointer_bytes: bytes | None) -> str | None:
    """Return the canonical digest of exact pointer bytes, preserving absence."""

    if pointer_bytes is None:
        return None
    if not isinstance(pointer_bytes, bytes):
        raise AuthoritySelectionConflict("pointer bytes must be bytes or null")
    return hashlib.sha256(pointer_bytes).hexdigest()


def authority_selection_token_from_pointer_bytes(
    *,
    plan_revision: int,
    plan_pointer_bytes: bytes | None,
    jit_revision: int,
    jit_pointer_bytes: bytes | None,
) -> AuthoritySelectionToken:
    """Bind already-parsed revisions to the exact raw pointer bytes that carried them."""

    return AuthoritySelectionToken(
        plan_revision=plan_revision,
        plan_pointer_sha256=pointer_sha256(plan_pointer_bytes),
        jit_revision=jit_revision,
        jit_pointer_sha256=pointer_sha256(jit_pointer_bytes),
    )


def require_matching_authority_selection_token(
    expected: AuthoritySelectionToken,
    observed: AuthoritySelectionToken,
) -> None:
    """Fail the transaction unless both plan and JIT heads match exactly."""

    if not isinstance(expected, AuthoritySelectionToken) or not isinstance(
        observed,
        AuthoritySelectionToken,
    ):
        raise AuthoritySelectionConflict(
            "authority selection comparison requires two typed tokens"
        )
    if observed != expected:
        raise AuthoritySelectionConflict(
            "authority selection changed; "
            f"expected={expected.to_dict()}; observed={observed.to_dict()}"
        )


_shot_path = canonical_authority_shot_path


def _relative_in_shot(shot: Path, path: str | Path, where: str) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_absolute():
        normalized = Path(os.path.abspath(candidate))
        try:
            relative = normalized.relative_to(shot)
        except ValueError as exc:
            raise AuthoritySelectionConflict(f"{where} escapes the shot root") from exc
    else:
        relative = candidate
    if (
        relative == Path(".")
        or relative.is_absolute()
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise AuthoritySelectionConflict(
            f"{where} must be a normalized non-root in-shot path"
        )
    return relative


_open_directory_parts = open_authority_directory_parts


def durably_ensure_real_directory(
    shot_folder: str | Path,
    directory_path: str | Path,
) -> Path:
    """Create or reflush one real in-shot directory chain.

    Every component is opened descriptor-relative without following symlinks.  Existing
    components are flushed as well as newly created ones, so retrying after a crash
    re-establishes the parent-entry durability barrier instead of mistaking a visible
    but not yet durable directory for completed storage.
    """

    shot = _shot_path(shot_folder)
    relative = _relative_in_shot(
        shot,
        directory_path,
        "durable authority directory",
    )
    descriptor: int | None = None
    try:
        descriptor = _open_directory_parts(
            shot,
            relative.parts,
            create=True,
        )
        assert descriptor is not None
    except AuthoritySelectionConflict:
        raise
    except OSError as exc:
        raise AuthoritySelectionConflict(
            f"could not durably create or flush authority directory: {relative}"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return shot / relative


def read_optional_pointer_bytes(
    shot_folder: str | Path,
    pointer_path: str | Path,
) -> bytes | None:
    """Read one optional regular in-shot pointer without following symlink components."""

    shot = _shot_path(shot_folder)
    relative = _relative_in_shot(shot, pointer_path, "authority pointer")
    parent = _open_directory_parts(
        shot,
        relative.parts[:-1],
        create=False,
        missing_ok=True,
    )
    if parent is None:
        return None
    descriptor: int | None = None
    try:
        try:
            descriptor = os.open(relative.name, _FILE_READ_FLAGS, dir_fd=parent)
        except FileNotFoundError:
            return None
        except OSError as exc:
            try:
                observed = os.stat(
                    relative.name,
                    dir_fd=parent,
                    follow_symlinks=False,
                )
            except OSError:
                observed = None
            if observed is not None and stat.S_ISLNK(observed.st_mode):
                raise AuthoritySelectionConflict(
                    f"authority pointer must not be a symlink: {relative}"
                ) from exc
            raise AuthoritySelectionConflict(
                f"authority pointer is not a real regular file: {relative}"
            ) from exc
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise AuthoritySelectionConflict(
                f"authority pointer is not a real regular file: {relative}"
            )
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        after = os.fstat(descriptor)
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if after_identity != before_identity:
            raise AuthoritySelectionConflict(
                f"authority pointer changed while it was read: {relative}"
            )
        current_parent = _open_directory_parts(
            shot,
            relative.parts[:-1],
            create=False,
            missing_ok=True,
        )
        if current_parent is None:
            raise AuthoritySelectionConflict(
                f"authority pointer parent changed while it was read: {relative}"
            )
        try:
            held_parent = os.fstat(parent)
            live_parent = os.fstat(current_parent)
            try:
                named = os.stat(
                    relative.name,
                    dir_fd=current_parent,
                    follow_symlinks=False,
                )
            except OSError as exc:
                raise AuthoritySelectionConflict(
                    f"authority pointer changed while it was read: {relative}"
                ) from exc
            named_identity = (
                named.st_dev,
                named.st_ino,
                named.st_size,
                named.st_mtime_ns,
                named.st_ctime_ns,
            )
            if (
                (held_parent.st_dev, held_parent.st_ino)
                != (live_parent.st_dev, live_parent.st_ino)
                or not stat.S_ISREG(named.st_mode)
                or named_identity != after_identity
            ):
                raise AuthoritySelectionConflict(
                    f"authority pointer changed while it was read: {relative}"
                )
        finally:
            os.close(current_parent)
        return b"".join(chunks)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent)


@contextmanager
def _uncontended_authority_selection_lock(
    *,
    shot: Path,
    lock_path: Path,
    lock_key: str,
    held: dict[str, _HeldAuthoritySelectionLock],
    exclusive: bool,
    shot_identity: os.stat_result,
) -> Iterator[Path]:
    shot_descriptor: int | None = None
    lock_parent: int | None = None
    descriptor: int | None = None
    registration: DescriptorLeaseRegistration | None = None
    locked = False
    try:
        with managed_fork_protected_acquisition() as acquisition, descriptor_registry_mutation():
            shot_descriptor = acquisition.open_descriptor(
                lambda: _open_directory_parts(shot, (), create=False)
            )
            assert shot_descriptor is not None
            retained_shot = os.fstat(shot_descriptor)
            if (retained_shot.st_dev, retained_shot.st_ino) != (
                shot_identity.st_dev,
                shot_identity.st_ino,
            ):
                raise AuthoritySelectionConflict(
                    "authority-selection shot root changed during lock acquisition"
                )
            lock_parent = acquisition.open_descriptor(
                lambda: _open_directory_parts(
                    shot,
                    AUTHORITY_SELECTION_LOCK.parts[:-1],
                    create=True,
                )
            )
            assert lock_parent is not None
            descriptor = acquisition.open_descriptor(
                lambda: os.open(
                    AUTHORITY_SELECTION_LOCK.name,
                    _LOCK_OPEN_FLAGS,
                    0o600,
                    dir_fd=lock_parent,
                )
            )
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise AuthoritySelectionConflict(
                    "authority selection lock must be a real regular file"
                )
            os.fsync(descriptor)
            os.fsync(lock_parent)
            descriptors = (shot_descriptor, lock_parent, descriptor)
            with provisional_descriptor_registration(
                descriptors,
                acquisition,
            ) as provisional:
                registration = provisional.registration
                acquisition.handoff(descriptors)
                provisional.commit()
    except OSError as exc:
        raise AuthoritySelectionConflict(
            "authority selection lock must be a real regular file"
        ) from exc
    except RunOwnerForkGuardCleanupError as exc:
        raise AuthoritySelectionCleanupFailure(
            errors=exc.errors,
            retained=exc.retained,
            body_error=exc.__cause__,
        ) from exc
    assert (
        shot_descriptor is not None
        and lock_parent is not None
        and descriptor is not None
        and registration is not None
    )
    body_error: BaseException | None = None
    try:
        try:
            fcntl.lockf(descriptor, fcntl.LOCK_EX)
        except OSError as exc:
            raise AuthoritySelectionConflict(
                "could not acquire the authority selection lock"
            ) from exc
        locked = True
        with process_bound_live_lock_identity(shot / AUTHORITY_SELECTION_LOCK):
            binding = _HeldAuthoritySelectionLock(
                shot=shot,
                lock_path=lock_path,
                shot_descriptor=shot_descriptor,
                parent_descriptor=lock_parent,
                lock_descriptor=descriptor,
                process_id=os.getpid(),
                process_token=current_process_token(),
                thread_id=get_ident(),
                registration=registration,
                exclusive=exclusive,
                depth=1,
            )
            _require_selection_lock_current(binding, "during lock acquisition")
            held[lock_key] = binding
            try:
                yield lock_path
            except BaseException as exc:
                body_error = exc
                raise
            finally:
                try:
                    _require_selection_lock_current(binding, "during lock exit")
                finally:
                    held.pop(lock_key, None)
    finally:
        try:
            neutralize_registered_descriptors(
                registration,
                unlock_record_lock=locked,
            )
        except AuthoritySelectionCleanupFailure as cleanup_error:
            if body_error is not None and not cleanup_error.retained:
                body_error.add_note(str(cleanup_error))
                for error in cleanup_error.errors:
                    body_error.add_note(
                        f"cleanup diagnostic: {type(error).__name__}: {error}"
                    )
            elif body_error is not None:
                raise AuthoritySelectionCleanupFailure(
                    errors=cleanup_error.errors,
                    retained=cleanup_error.retained,
                    body_error=body_error,
                ) from body_error
            else:
                raise


@contextmanager
def authority_selection_lock(
    shot_folder: str | Path,
    *,
    exclusive: bool,
) -> Iterator[Path]:
    """Hold the permanent shot-wide selection lock in shared or exclusive mode."""

    if not isinstance(exclusive, bool):
        raise AuthoritySelectionConflict("authority selection lock mode must be boolean")
    shot = _shot_path(shot_folder)
    lock_path = shot / AUTHORITY_SELECTION_LOCK
    lock_key = str(lock_path)
    held = _held_selection_locks()
    binding = held.get(lock_key)
    if binding is not None:
        # Both semantic modes use one exclusive POSIX record lock.  Nested calls
        # reuse the creator thread's retained lease; the per-shot process mutex
        # serializes independent same-process threads because record locks do not.
        _require_selection_lock_current(binding, "during nested lock entry")
        if exclusive and not binding.exclusive:
            raise AuthoritySelectionConflict(
                "cannot upgrade a nested shared authority selection lock to exclusive; "
                "restart the transaction with the exclusive shot-authority boundary outermost"
            )
        binding.depth += 1
        try:
            yield lock_path
        finally:
            try:
                _require_selection_lock_current(binding, "during nested lock exit")
            finally:
                binding.depth -= 1
        return
    if held:
        active_shots = ", ".join(sorted(str(active.shot) for active in held.values()))
        raise AuthoritySelectionConflict(
            "nested authority selection locks require the same canonical shot; "
            f"active={active_shots}; requested={shot}"
        )
    try:
        shot_identity = os.stat(shot, follow_symlinks=False)
    except OSError as exc:
        raise AuthoritySelectionConflict(
            f"authority-selection shot root changed before lock acquisition: {shot}"
        ) from exc
    with shot_process_mutex(
        (shot_identity.st_dev, shot_identity.st_ino)
    ), _uncontended_authority_selection_lock(
        shot=shot,
        lock_path=lock_path,
        lock_key=lock_key,
        held=held,
        exclusive=exclusive,
        shot_identity=shot_identity,
    ):
        yield lock_path


def _validate_replace_target(parent: int, name: str, relative: Path) -> None:
    try:
        target = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise AuthoritySelectionConflict(
            f"authority pointer target is unreadable: {relative}"
        ) from exc
    if not stat.S_ISREG(target.st_mode):
        raise AuthoritySelectionConflict(
            f"authority pointer target must be absent or a regular file: {relative}"
        )


def _unique_temporary(parent: int, target_name: str) -> tuple[int, str]:
    for _attempt in range(128):
        name = f".{target_name}.tmp-{secrets.token_hex(12)}"
        try:
            descriptor = os.open(
                name,
                _TEMP_OPEN_FLAGS,
                0o600,
                dir_fd=parent,
            )
        except FileExistsError:
            continue
        except OSError as exc:
            raise AuthoritySelectionConflict(
                "could not create a unique temporary authority pointer"
            ) from exc
        return descriptor, name
    raise AuthoritySelectionConflict(
        "could not allocate a unique temporary authority pointer"
    )


def _file_identity(observed: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        observed.st_dev,
        observed.st_ino,
        observed.st_size,
        observed.st_mtime_ns,
        observed.st_ctime_ns,
    )


def _require_named_temporary_current(
    parent: int,
    name: str,
    descriptor: int,
    expected: tuple[int, int, int, int, int],
    relative: Path,
) -> None:
    try:
        held = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except OSError as exc:
        raise AuthoritySelectionConflict(
            f"prepared authority pointer disappeared before publication: {relative}"
        ) from exc
    if (
        not stat.S_ISREG(held.st_mode)
        or not stat.S_ISREG(named.st_mode)
        or _file_identity(held) != expected
        or _file_identity(named) != expected
    ):
        raise AuthoritySelectionConflict(
            f"prepared authority pointer changed before publication: {relative}"
        )


def _require_published_descriptor_current(
    parent: int,
    name: str,
    descriptor: int,
    expected: tuple[int, int, int, int, int],
    relative: Path,
) -> None:
    try:
        held = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except OSError as exc:
        raise AuthoritySelectionConflict(
            f"published authority pointer disappeared during publication: {relative}"
        ) from exc
    if (
        not stat.S_ISREG(held.st_mode)
        or not stat.S_ISREG(named.st_mode)
        or (held.st_dev, held.st_ino) != expected[:2]
        or (named.st_dev, named.st_ino) != expected[:2]
        or (held.st_size, held.st_mtime_ns) != (expected[2], expected[3])
        or _file_identity(named) != _file_identity(held)
    ):
        raise AuthoritySelectionConflict(
            f"published authority pointer changed during publication: {relative}"
        )


def _unlink_named_descriptor_if_owned(parent: int, name: str, descriptor: int) -> None:
    """Remove only a temporary name still bound to the held inode."""

    try:
        held = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except OSError:
        return
    if (
        stat.S_ISREG(held.st_mode)
        and stat.S_ISREG(named.st_mode)
        and (held.st_dev, held.st_ino) == (named.st_dev, named.st_ino)
    ):
        with suppress(FileNotFoundError):
            os.unlink(name, dir_fd=parent)


def durable_replace_pointer_bytes(
    shot_folder: str | Path,
    pointer_path: str | Path,
    payload: bytes,
) -> None:
    """Durably replace one pointer through a unique file in its existing real parent."""

    if not isinstance(payload, bytes):
        raise AuthoritySelectionConflict("authority pointer payload must be bytes")
    shot = _shot_path(shot_folder)
    relative = _relative_in_shot(shot, pointer_path, "authority pointer")
    if relative == AUTHORITY_SELECTION_LOCK:
        raise AuthoritySelectionConflict(
            "the permanent authority selection lock is not a replaceable pointer"
        )
    parent = _open_directory_parts(
        shot,
        relative.parts[:-1],
        create=False,
    )
    assert parent is not None
    temporary_descriptor: int | None = None
    temporary_name: str | None = None
    try:
        _validate_replace_target(parent, relative.name, relative)
        temporary_descriptor, temporary_name = _unique_temporary(parent, relative.name)
        try:
            with os.fdopen(temporary_descriptor, "wb", closefd=False) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            temporary_identity = _file_identity(os.fstat(temporary_descriptor))
            _require_named_temporary_current(
                parent,
                temporary_name,
                temporary_descriptor,
                temporary_identity,
                relative,
            )
            _validate_replace_target(parent, relative.name, relative)
            os.replace(
                temporary_name,
                relative.name,
                src_dir_fd=parent,
                dst_dir_fd=parent,
            )
            _require_published_descriptor_current(
                parent,
                relative.name,
                temporary_descriptor,
                temporary_identity,
                relative,
            )
            temporary_name = None
            os.fsync(parent)
        except AuthoritySelectionConflict:
            raise
        except OSError as exc:
            raise AuthoritySelectionConflict(
                f"could not durably replace authority pointer: {relative}"
            ) from exc
    finally:
        if temporary_descriptor is not None:
            if temporary_name is not None:
                _unlink_named_descriptor_if_owned(
                    parent,
                    temporary_name,
                    temporary_descriptor,
                )
            os.close(temporary_descriptor)
        os.close(parent)


def durable_replace_file_bytes(
    shot_folder: str | Path,
    file_path: str | Path,
    payload: bytes,
) -> None:
    """Durably replace one ordinary in-shot file through its real parent.

    Unlike authority-head publication, ordinary durable state may create its parent
    chain. Directory entries, staged bytes, and the final same-parent rename are all
    flushed before success is reported.
    """

    if not isinstance(payload, bytes):
        raise AuthoritySelectionConflict("durable file payload must be bytes")
    shot = _shot_path(shot_folder)
    relative = _relative_in_shot(shot, file_path, "durable file")
    if relative == AUTHORITY_SELECTION_LOCK:
        raise AuthoritySelectionConflict(
            "the permanent authority selection lock is not a replaceable file"
        )
    if relative in _SELECTED_AUTHORITY_POINTERS:
        raise AuthoritySelectionConflict(
            "selected authority heads require the authority-pointer transaction: "
            f"{relative}"
        )
    if relative.parent != Path("."):
        durably_ensure_real_directory(shot, relative.parent)
    durable_replace_pointer_bytes(shot, relative, payload)


def durable_replace_pointer_json(
    shot_folder: str | Path,
    pointer_path: str | Path,
    value: Mapping[str, Any],
) -> bytes:
    """Canonically encode and durably replace one JSON-object pointer."""

    payload = canonical_pointer_json_bytes(value)
    durable_replace_pointer_bytes(shot_folder, pointer_path, payload)
    return payload


def durable_remove_pointer(
    shot_folder: str | Path,
    pointer_path: str | Path,
) -> None:
    """Durably restore an optional authority head to absence during rollback."""

    shot = _shot_path(shot_folder)
    relative = _relative_in_shot(shot, pointer_path, "authority pointer")
    if relative == AUTHORITY_SELECTION_LOCK:
        raise AuthoritySelectionConflict(
            "the permanent authority selection lock is not a removable pointer"
        )
    parent = _open_directory_parts(
        shot,
        relative.parts[:-1],
        create=False,
        missing_ok=True,
    )
    if parent is None:
        return
    try:
        _validate_replace_target(parent, relative.name, relative)
        try:
            os.unlink(relative.name, dir_fd=parent)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise AuthoritySelectionConflict(
                f"could not durably remove authority pointer: {relative}"
            ) from exc
        os.fsync(parent)
    finally:
        os.close(parent)


def canonical_pointer_json_bytes(value: Mapping[str, Any]) -> bytes:
    """Encode the sole accepted byte representation of an authority pointer."""

    try:
        return canonical_json_bytes(value)
    except AuthorityHeadRecordError as exc:
        raise AuthoritySelectionConflict(
            str(exc).replace("authority head JSON", "authority pointer JSON")
        ) from exc
