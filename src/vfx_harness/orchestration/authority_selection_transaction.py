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
from typing import Any, ClassVar

from vfx_harness.domain.authority_head_records import (
    AUTHORITY_SELECTION_TOKEN_SCHEMA,
    AuthorityHeadRecordError,
    canonical_json_bytes,
    parse_authority_selection_token,
)

AUTHORITY_SELECTION_LOCK = Path("state/authority-selection/selection.lock")
_DIRECTORY_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
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


class AuthoritySelectionConflict(ValueError):
    """Authority selection changed or its low-level storage is unsafe."""


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


def _shot_path(shot_folder: str | Path) -> Path:
    raw = Path(shot_folder).expanduser()
    shot = Path(os.path.abspath(raw))
    try:
        descriptor = os.open(shot, _DIRECTORY_OPEN_FLAGS)
    except OSError as exc:
        raise AuthoritySelectionConflict(
            f"authority-selection shot root must be an existing real directory: {shot}"
        ) from exc
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise AuthoritySelectionConflict(
                f"authority-selection shot root must be a directory: {shot}"
            )
    finally:
        os.close(descriptor)
    return shot


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


def _open_directory_parts(
    shot: Path,
    parts: tuple[str, ...],
    *,
    create: bool,
    missing_ok: bool = False,
) -> int | None:
    try:
        current = os.open(shot, _DIRECTORY_OPEN_FLAGS)
    except OSError as exc:
        raise AuthoritySelectionConflict(
            f"authority-selection shot root became unsafe: {shot}"
        ) from exc
    try:
        for part in parts:
            try:
                following = os.open(part, _DIRECTORY_OPEN_FLAGS, dir_fd=current)
            except FileNotFoundError:
                if missing_ok:
                    os.close(current)
                    return None
                if not create:
                    raise AuthoritySelectionConflict(
                        f"authority-selection directory is missing: {part}"
                    ) from None
                try:
                    os.mkdir(part, mode=0o700, dir_fd=current)
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise AuthoritySelectionConflict(
                        f"cannot create authority-selection directory: {part}"
                    ) from exc
                try:
                    following = os.open(part, _DIRECTORY_OPEN_FLAGS, dir_fd=current)
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
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise AuthoritySelectionConflict(
                f"authority pointer is not a real regular file: {relative}"
            )
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = None
            return handle.read()
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent)


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
    lock_parent = _open_directory_parts(
        shot,
        AUTHORITY_SELECTION_LOCK.parts[:-1],
        create=True,
    )
    assert lock_parent is not None
    descriptor: int | None = None
    locked = False
    try:
        try:
            descriptor = os.open(
                AUTHORITY_SELECTION_LOCK.name,
                _LOCK_OPEN_FLAGS,
                0o600,
                dir_fd=lock_parent,
            )
        except OSError as exc:
            raise AuthoritySelectionConflict(
                "authority selection lock must be a real regular file"
            ) from exc
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise AuthoritySelectionConflict(
                "authority selection lock must be a real regular file"
            )
        os.fsync(descriptor)
        os.fsync(lock_parent)
        try:
            fcntl.flock(
                descriptor,
                fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH,
            )
        except OSError as exc:
            raise AuthoritySelectionConflict(
                "could not acquire the authority selection lock"
            ) from exc
        locked = True
        yield shot / AUTHORITY_SELECTION_LOCK
    finally:
        if descriptor is not None:
            if locked:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
        os.close(lock_parent)


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
            with os.fdopen(temporary_descriptor, "wb", closefd=True) as handle:
                temporary_descriptor = None
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            _validate_replace_target(parent, relative.name, relative)
            os.replace(
                temporary_name,
                relative.name,
                src_dir_fd=parent,
                dst_dir_fd=parent,
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
            os.close(temporary_descriptor)
        if temporary_name is not None:
            with suppress(FileNotFoundError):
                os.unlink(temporary_name, dir_fd=parent)
        os.close(parent)


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
