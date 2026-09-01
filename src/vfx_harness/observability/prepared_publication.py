"""Prepared compare-and-swap publication for mutable generated side files.

Some builder tools update files whose histories may grow without bound.  Reading,
parsing, merging, serializing, and flushing those bytes while selected-authority and
unit-state locks are held would make a legitimate replan wait on unrelated I/O.  This
module stages the complete replacement as an inert, fsynced same-parent inode.  The
later commit takes a permanent per-target lock without waiting, compares exact file
and directory identities, renames the inode, and flushes the directory.  No content
I/O occurs in that commit.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import secrets
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, TypeVar

_T = TypeVar("_T")

_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
_CREATE_FLAGS = (
    os.O_RDWR
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_LOCK_FLAGS = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
_LOCK_DIRECTORY = Path("state/publication-locks")


class FilePublicationConflict(RuntimeError):
    """A prepared side-file update no longer names the current generation."""


@dataclass(frozen=True, slots=True)
class FileIdentity:
    """Exact regular-file identity, with ``None`` preserving authoritative absence."""

    device: int
    inode: int
    size: int
    modified_ns: int
    changed_ns: int


@dataclass(frozen=True, slots=True)
class PreparedFilePublication:
    """Fsynced replacement bytes and the metadata needed by a short commit."""

    shot: Path
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
    temporary_name: str
    temporary_identity: FileIdentity
    payload_sha256: str
    lock_parent: Path
    lock_parent_descriptor: int
    lock_parent_identity: tuple[int, int]
    lock_descriptor: int
    lock_name: str
    lock_identity: tuple[int, int]
    authority_binding: str


@dataclass(frozen=True, slots=True)
class PreparedFileUpdate(Generic[_T]):
    """Typed preparation result; ``publication=None`` is an exact no-op."""

    publication: PreparedFilePublication | None
    result: _T


def _absolute(path: str | Path) -> Path:
    return Path(os.path.abspath(Path(path).expanduser()))


def _identity(observed: os.stat_result) -> FileIdentity:
    return FileIdentity(
        device=observed.st_dev,
        inode=observed.st_ino,
        size=observed.st_size,
        modified_ns=observed.st_mtime_ns,
        changed_ns=observed.st_ctime_ns,
    )


def _open_real_directory(path: Path, label: str) -> int:
    absolute = _absolute(path)
    descriptor: int | None = None
    try:
        descriptor = os.open(absolute.anchor, _DIRECTORY_FLAGS)
        for part in absolute.parts[1:]:
            following = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = following
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise FilePublicationConflict(
            f"{label} must be a real non-symlink directory: {absolute}"
        ) from exc
    if descriptor is None or not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        if descriptor is not None:
            os.close(descriptor)
        raise FilePublicationConflict(f"{label} must be a real directory: {absolute}")
    return descriptor


def _open_or_create_relative_directory(
    shot_descriptor: int,
    parts: tuple[str, ...],
    *,
    label: str,
) -> int:
    current = os.dup(shot_descriptor)
    try:
        for part in parts:
            try:
                following = os.open(part, _DIRECTORY_FLAGS, dir_fd=current)
            except FileNotFoundError:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(part, 0o700, dir_fd=current)
                following = os.open(part, _DIRECTORY_FLAGS, dir_fd=current)
                os.fsync(current)
                os.fsync(following)
            os.close(current)
            current = following
        return current
    except OSError as exc:
        os.close(current)
        raise FilePublicationConflict(
            f"{label} must contain only real directory components"
        ) from exc


def _normalize_destination(shot: Path, destination: str | Path) -> tuple[Path, Path]:
    raw = Path(destination).expanduser()
    target = _absolute(raw if raw.is_absolute() else shot / raw)
    try:
        relative = target.relative_to(shot)
    except ValueError as exc:
        raise FilePublicationConflict(
            f"side-file publication destination escapes the shot root: {target}"
        ) from exc
    if relative == Path(".") or not relative.name:
        raise FilePublicationConflict("side-file publication requires a non-root file path")
    return target, relative


def _read_current(
    directory: int,
    name: str,
    path: Path,
) -> tuple[bytes | None, FileIdentity | None, str | None]:
    try:
        descriptor = os.open(name, _READ_FLAGS, dir_fd=directory)
    except FileNotFoundError:
        return None, None, None
    except OSError as exc:
        raise FilePublicationConflict(
            f"side-file target must be absent or a real regular file: {path}"
        ) from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise FilePublicationConflict(
                f"side-file target must be absent or a real regular file: {path}"
            )
        digest = hashlib.sha256()
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
            digest.update(chunk)
        after = os.fstat(descriptor)
        if _identity(after) != _identity(before):
            raise FilePublicationConflict(
                f"side-file target changed while it was prepared: {path}"
            )
        return b"".join(chunks), _identity(after), digest.hexdigest()
    finally:
        os.close(descriptor)


def _current_identity(directory: int, name: str, path: Path) -> FileIdentity | None:
    try:
        observed = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise FilePublicationConflict(f"side-file target is unreadable: {path}") from exc
    if not stat.S_ISREG(observed.st_mode):
        raise FilePublicationConflict(
            f"side-file target must be absent or a real regular file: {path}"
        )
    return _identity(observed)


def _write_all(descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("short write while preparing side-file publication")
        remaining = remaining[written:]


def _allocate_temporary(directory: int, target_name: str) -> tuple[int, str]:
    for _attempt in range(128):
        name = f".{target_name}.prepared.{secrets.token_hex(12)}"
        try:
            return os.open(name, _CREATE_FLAGS, 0o600, dir_fd=directory), name
        except FileExistsError:
            continue
        except OSError as exc:
            raise FilePublicationConflict(
                "could not allocate a same-parent side-file preparation"
            ) from exc
    raise FilePublicationConflict(
        "could not allocate a unique same-parent side-file preparation"
    )


def _unlink_named_descriptor_if_owned(
    directory: int,
    name: str,
    descriptor: int,
) -> None:
    """Remove only a temporary name still bound to the held inode."""

    try:
        held = os.fstat(descriptor)
        named = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except OSError:
        return
    if (
        stat.S_ISREG(held.st_mode)
        and stat.S_ISREG(named.st_mode)
        and (held.st_dev, held.st_ino) == (named.st_dev, named.st_ino)
    ):
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name, dir_fd=directory)


def _open_publication_lock(
    shot_descriptor: int,
    shot: Path,
    relative: Path,
) -> tuple[Path, int, tuple[int, int], int, str, tuple[int, int]]:
    lock_parent_descriptor = _open_or_create_relative_directory(
        shot_descriptor,
        _LOCK_DIRECTORY.parts,
        label="side-file publication lock directory",
    )
    lock_parent = shot / _LOCK_DIRECTORY
    parent_stat = os.fstat(lock_parent_descriptor)
    lock_name = hashlib.sha256(str(relative).encode("utf-8")).hexdigest() + ".lock"
    try:
        lock_descriptor = os.open(
            lock_name,
            _LOCK_FLAGS,
            0o600,
            dir_fd=lock_parent_descriptor,
        )
    except OSError as exc:
        os.close(lock_parent_descriptor)
        raise FilePublicationConflict(
            f"side-file publication lock must be a real regular file: {lock_parent / lock_name}"
        ) from exc
    lock_stat = os.fstat(lock_descriptor)
    try:
        named = os.stat(lock_name, dir_fd=lock_parent_descriptor, follow_symlinks=False)
    except OSError as exc:
        os.close(lock_descriptor)
        os.close(lock_parent_descriptor)
        raise FilePublicationConflict(
            f"side-file publication lock is not stable: {lock_parent / lock_name}"
        ) from exc
    if (
        not stat.S_ISREG(lock_stat.st_mode)
        or not stat.S_ISREG(named.st_mode)
        or (lock_stat.st_dev, lock_stat.st_ino) != (named.st_dev, named.st_ino)
    ):
        os.close(lock_descriptor)
        os.close(lock_parent_descriptor)
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
        (lock_stat.st_dev, lock_stat.st_ino),
    )


def prepare_file_update(
    shot_folder: str | Path,
    destination: str | Path,
    update: Callable[[bytes | None], tuple[bytes | None, _T]],
    *,
    authority_binding: str,
) -> PreparedFileUpdate[_T]:
    """Read/merge/serialize/fsync one replacement without holding attempt locks.

    ``update`` returns ``(None, result)`` for an exact no-op.  Any byte payload is
    staged as an unreferenced same-parent inode and bound to the predecessor observed
    by this preparation.
    """

    if not isinstance(authority_binding, str) or not authority_binding.strip():
        raise FilePublicationConflict(
            "side-file preparation requires a non-empty authority binding"
        )
    shot = _absolute(shot_folder)
    shot_descriptor = _open_real_directory(shot, "side-file shot root")
    destination_path, relative = _normalize_destination(shot, destination)
    parent_descriptor: int | None = None
    lock_parent_descriptor: int | None = None
    lock_descriptor: int | None = None
    temporary_descriptor: int | None = None
    temporary_name: str | None = None
    try:
        parent_descriptor = _open_or_create_relative_directory(
            shot_descriptor,
            relative.parent.parts if relative.parent != Path(".") else (),
            label="side-file destination parent",
        )
        parent_stat = os.fstat(parent_descriptor)
        current, predecessor, predecessor_sha256 = _read_current(
            parent_descriptor,
            destination_path.name,
            destination_path,
        )
        payload, result = update(current)
        if payload is None:
            os.close(parent_descriptor)
            parent_descriptor = None
            return PreparedFileUpdate(publication=None, result=result)
        if not isinstance(payload, bytes):
            raise FilePublicationConflict(
                "side-file update must return bytes or an exact no-op"
            )
        (
            lock_parent,
            lock_parent_descriptor,
            lock_parent_identity,
            lock_descriptor,
            lock_name,
            lock_identity,
        ) = _open_publication_lock(shot_descriptor, shot, relative)
        temporary_descriptor, temporary_name = _allocate_temporary(
            parent_descriptor,
            destination_path.name,
        )
        _write_all(temporary_descriptor, payload)
        os.fsync(temporary_descriptor)
        os.lseek(temporary_descriptor, 0, os.SEEK_SET)
        observed_digest = hashlib.sha256()
        while chunk := os.read(temporary_descriptor, 1024 * 1024):
            observed_digest.update(chunk)
        payload_sha256 = hashlib.sha256(payload).hexdigest()
        if observed_digest.hexdigest() != payload_sha256:
            raise FilePublicationConflict(
                f"prepared side-file bytes changed before publication: {destination_path}"
            )
        temporary_identity = _identity(os.fstat(temporary_descriptor))
        publication = PreparedFilePublication(
            shot=shot,
            relative_path=relative,
            destination=destination_path,
            destination_parent=destination_path.parent,
            destination_parent_descriptor=parent_descriptor,
            destination_parent_identity=(parent_stat.st_dev, parent_stat.st_ino),
            destination_name=destination_path.name,
            predecessor=predecessor,
            predecessor_sha256=predecessor_sha256,
            temporary=destination_path.parent / temporary_name,
            temporary_descriptor=temporary_descriptor,
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
        )
        parent_descriptor = None
        lock_parent_descriptor = None
        lock_descriptor = None
        temporary_descriptor = None
        return PreparedFileUpdate(publication=publication, result=result)
    except BaseException:
        if (
            temporary_name is not None
            and temporary_descriptor is not None
            and parent_descriptor is not None
        ):
            _unlink_named_descriptor_if_owned(
                parent_descriptor,
                temporary_name,
                temporary_descriptor,
            )
        for descriptor in (
            temporary_descriptor,
            lock_descriptor,
            lock_parent_descriptor,
            parent_descriptor,
        ):
            if descriptor is not None:
                with contextlib.suppress(OSError):
                    os.close(descriptor)
        raise
    finally:
        os.close(shot_descriptor)


def _require_directory_current(path: Path, expected: tuple[int, int], label: str) -> None:
    descriptor = _open_real_directory(path, label)
    try:
        observed = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (observed.st_dev, observed.st_ino) != expected:
        raise FilePublicationConflict(f"{label} changed after side-file preparation: {path}")


def _require_lock_current(prepared: PreparedFilePublication) -> None:
    held = os.fstat(prepared.lock_descriptor)
    try:
        named = os.stat(
            prepared.lock_name,
            dir_fd=prepared.lock_parent_descriptor,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise FilePublicationConflict(
            f"side-file publication lock disappeared: {prepared.lock_parent / prepared.lock_name}"
        ) from exc
    if (
        not stat.S_ISREG(held.st_mode)
        or not stat.S_ISREG(named.st_mode)
        or (held.st_dev, held.st_ino) != prepared.lock_identity
        or (named.st_dev, named.st_ino) != prepared.lock_identity
    ):
        raise FilePublicationConflict(
            f"side-file publication lock changed: {prepared.lock_parent / prepared.lock_name}"
        )


def _require_temporary_current(prepared: PreparedFilePublication) -> None:
    held = os.fstat(prepared.temporary_descriptor)
    try:
        named = os.stat(
            prepared.temporary_name,
            dir_fd=prepared.destination_parent_descriptor,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise FilePublicationConflict(
            f"prepared side-file inode disappeared: {prepared.temporary}"
        ) from exc
    if (
        not stat.S_ISREG(held.st_mode)
        or not stat.S_ISREG(named.st_mode)
        or _identity(held) != prepared.temporary_identity
        or _identity(named) != prepared.temporary_identity
    ):
        raise FilePublicationConflict(
            f"prepared side-file inode changed: {prepared.temporary}"
        )


def _require_published_current(prepared: PreparedFilePublication) -> None:
    """Require the destination name to resolve to the exact held staged inode."""

    held = os.fstat(prepared.temporary_descriptor)
    try:
        named = os.stat(
            prepared.destination_name,
            dir_fd=prepared.destination_parent_descriptor,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise FilePublicationConflict(
            f"published side-file inode disappeared: {prepared.destination}"
        ) from exc
    if (
        not stat.S_ISREG(held.st_mode)
        or not stat.S_ISREG(named.st_mode)
        or (held.st_dev, held.st_ino)
        != (
            prepared.temporary_identity.device,
            prepared.temporary_identity.inode,
        )
        or (named.st_dev, named.st_ino) != (held.st_dev, held.st_ino)
        or (held.st_size, held.st_mtime_ns)
        != (
            prepared.temporary_identity.size,
            prepared.temporary_identity.modified_ns,
        )
        or _identity(named) != _identity(held)
    ):
        raise FilePublicationConflict(
            f"published side-file inode changed: {prepared.destination}"
        )


def _close_prepared(prepared: PreparedFilePublication) -> None:
    for descriptor in (
        prepared.temporary_descriptor,
        prepared.destination_parent_descriptor,
        prepared.lock_descriptor,
        prepared.lock_parent_descriptor,
    ):
        with contextlib.suppress(OSError):
            os.close(descriptor)


def commit_prepared_file(
    prepared: PreparedFilePublication,
    *,
    authority_binding: str,
) -> str:
    """Perform the non-blocking metadata-only CAS commit for prepared bytes."""

    if not isinstance(prepared, PreparedFilePublication):
        raise FilePublicationConflict(
            "side-file commit requires a typed prepared publication"
        )
    if authority_binding != prepared.authority_binding:
        raise FilePublicationConflict(
            "prepared side-file publication belongs to another authority binding"
        )
    try:
        fcntl.flock(prepared.lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise FilePublicationConflict(
            f"side-file publication is already committing: {prepared.destination}; retry from current bytes"
        ) from exc
    except OSError as exc:
        raise FilePublicationConflict(
            f"could not acquire side-file publication lock: {prepared.destination}"
        ) from exc
    committed = False
    try:
        _require_directory_current(
            prepared.lock_parent,
            prepared.lock_parent_identity,
            "side-file publication lock directory",
        )
        _require_lock_current(prepared)
        _require_directory_current(
            prepared.destination_parent,
            prepared.destination_parent_identity,
            "side-file destination parent",
        )
        if (
            _current_identity(
                prepared.destination_parent_descriptor,
                prepared.destination_name,
                prepared.destination,
            )
            != prepared.predecessor
        ):
            raise FilePublicationConflict(
                f"side-file target changed after preparation: {prepared.destination}; no update was written"
            )
        _require_temporary_current(prepared)
        os.replace(
            prepared.temporary_name,
            prepared.destination_name,
            src_dir_fd=prepared.destination_parent_descriptor,
            dst_dir_fd=prepared.destination_parent_descriptor,
        )
        _require_published_current(prepared)
        _require_directory_current(
            prepared.destination_parent,
            prepared.destination_parent_identity,
            "side-file destination parent",
        )
        os.fsync(prepared.destination_parent_descriptor)
        committed = True
        return prepared.payload_sha256
    except FilePublicationConflict:
        raise
    except OSError as exc:
        raise FilePublicationConflict(
            f"could not durably publish side file: {prepared.destination}"
        ) from exc
    finally:
        fcntl.flock(prepared.lock_descriptor, fcntl.LOCK_UN)
        if committed:
            _close_prepared(prepared)


def discard_prepared_file(prepared: PreparedFilePublication | None) -> None:
    """Remove only the exact uncommitted temp and close stable descriptors."""

    if not isinstance(prepared, PreparedFilePublication):
        return
    try:
        try:
            held = os.fstat(prepared.temporary_descriptor)
            named = os.stat(
                prepared.temporary_name,
                dir_fd=prepared.destination_parent_descriptor,
                follow_symlinks=False,
            )
        except OSError:
            return
        if (
            stat.S_ISREG(held.st_mode)
            and stat.S_ISREG(named.st_mode)
            and (held.st_dev, held.st_ino) == (
                prepared.temporary_identity.device,
                prepared.temporary_identity.inode,
            )
            and (named.st_dev, named.st_ino) == (
                prepared.temporary_identity.device,
                prepared.temporary_identity.inode,
            )
        ):
            _unlink_named_descriptor_if_owned(
                prepared.destination_parent_descriptor,
                prepared.temporary_name,
                prepared.temporary_descriptor,
            )
    finally:
        _close_prepared(prepared)


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
    except BaseException:
        discard_prepared_file(prepared.publication)
        raise
    return prepared.result
