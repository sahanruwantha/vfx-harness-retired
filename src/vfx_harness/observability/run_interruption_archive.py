"""Run-owned content-addressed archive and record publication for interruption evidence.

Archived objects are create-only files named by the SHA-256 of their bytes under the
target run.  Storing the same bytes twice is idempotent; a differing byte stream at the
same address, a symlink, or a non-regular file fails closed.  Records publish as
canonical JSON at their run-relative locators and return the typed reference that binds
the exact bytes written (HIR-0172).
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from vfx_harness.domain.run_interruption_archive import (
    INTERRUPTION_ARCHIVE_OBJECT_DIRECTORY,
    ArchivedSourceObject,
)
from vfx_harness.domain.run_record_refs import RunRecordRef

_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)


class RunInterruptionArchiveError(ValueError):
    """The run-owned archive or a run record could not be written or reopened exactly."""


class RunRecordAbsent(RunInterruptionArchiveError):
    """The named archive object or run record does not exist."""


class ArchiveObjectMismatch(RunInterruptionArchiveError):
    """The archived bytes do not match the identity their manifest row names."""


class _Record(Protocol):
    SCHEMA: str

    @property
    def digest(self) -> str: ...

    def as_dict(self) -> dict[str, Any]: ...


def _run_root(run_root: str | Path) -> Path:
    return Path(run_root).expanduser().absolute()


def _relative(locator: str) -> PurePosixPath:
    relative = PurePosixPath(locator)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise RunInterruptionArchiveError(f"run record locator must be a canonical relative path: {locator!r}")
    return relative


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:  # pragma: no cover - POSIX write either advances or raises
            raise OSError("archive write made no progress")
        view = view[written:]


def read_regular_file(path: Path, *, where: str) -> bytes:
    """Read one existing regular file without following a symlink at its name."""

    try:
        descriptor = os.open(path, _READ_FLAGS)
    except FileNotFoundError as exc:
        raise RunRecordAbsent(f"{where} is absent: {path}") from exc
    except OSError as exc:
        raise RunInterruptionArchiveError(f"{where} is not a readable regular file: {path}: {exc}") from exc
    try:
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode):
            raise RunInterruptionArchiveError(f"{where} is not a regular file: {path}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1 << 20)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _create_only(path: Path, payload: bytes) -> None:
    """Publish exact bytes at a create-only name through a staged sibling and hard link."""

    staging = path.with_name(f".{path.name}.tmp.{os.getpid()}.{secrets.token_hex(8)}")
    descriptor = os.open(staging, _CREATE_FLAGS, 0o600)
    try:
        _write_all(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.link(staging, path, follow_symlinks=False)
    except FileExistsError:
        pass
    finally:
        os.unlink(staging)
    _fsync_directory(path.parent)


def store_archive_object(
    run_root: str | Path,
    *,
    namespace: str,
    locator: str,
    payload: bytes,
) -> ArchivedSourceObject:
    """Copy exact source bytes into the run-owned archive and describe them."""

    root = _run_root(run_root)
    if not isinstance(payload, (bytes, bytearray)):
        raise RunInterruptionArchiveError("archive objects require exact bytes")
    payload = bytes(payload)
    archived = ArchivedSourceObject(namespace, locator, len(payload), hashlib.sha256(payload).hexdigest())
    directory = root / INTERRUPTION_ARCHIVE_OBJECT_DIRECTORY
    target = root / archived.archive_locator
    try:
        directory.mkdir(parents=True, exist_ok=True)
        if directory.is_symlink() or not directory.is_dir():
            raise RunInterruptionArchiveError(f"archive object directory is not a real directory: {directory}")
        if not os.path.lexists(target):
            _create_only(target, payload)
        stored = read_regular_file(target, where="archived object")
    except OSError as exc:
        raise RunInterruptionArchiveError(f"could not store archive object {archived.sha256}: {exc}") from exc
    if stored != payload:
        raise RunInterruptionArchiveError(
            f"archive object {archived.sha256} already holds different bytes; the archive is corrupt"
        )
    return archived


def read_archive_object(run_root: str | Path, archived: ArchivedSourceObject) -> bytes:
    """Reopen one archived object and require its exact byte identity."""

    if not isinstance(archived, ArchivedSourceObject):
        raise RunInterruptionArchiveError("archive reads require the typed archived object row")
    payload = read_regular_file(_run_root(run_root) / archived.archive_locator, where="archived object")
    if len(payload) != archived.byte_count or hashlib.sha256(payload).hexdigest() != archived.sha256:
        raise ArchiveObjectMismatch(
            f"archived object {archived.archive_locator} does not hold the bytes its manifest row names"
        )
    return payload


def record_bytes(record: _Record) -> bytes:
    """Return the exact canonical wire bytes of one run record."""

    return (json.dumps(record.as_dict(), indent=2, sort_keys=True) + "\n").encode("utf-8")


def publish_run_record(run_root: str | Path, locator: str, record: _Record) -> RunRecordRef:
    """Atomically write one record at its run-relative locator and return its exact reference."""

    root = _run_root(run_root)
    relative = _relative(locator)
    target = root / relative
    payload = record_bytes(record)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink():
            raise RunInterruptionArchiveError(f"run record target is a symlink: {target}")
        staging = target.with_name(f".{target.name}.tmp.{os.getpid()}.{secrets.token_hex(8)}")
        descriptor = os.open(staging, _CREATE_FLAGS, 0o644)
        try:
            _write_all(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(staging, target)
        _fsync_directory(target.parent)
    except OSError as exc:
        raise RunInterruptionArchiveError(f"could not publish run record {locator}: {exc}") from exc
    return RunRecordRef(
        locator=relative.as_posix(),
        sha256=hashlib.sha256(payload).hexdigest(),
        record_schema=record.SCHEMA,
        record_digest=record.digest,
    )


def read_run_record_bytes(run_root: str | Path, locator: str) -> bytes:
    """Read the exact bytes at one run-relative record locator."""

    return read_regular_file(_run_root(run_root) / _relative(locator), where=f"run record {locator}")


__all__ = [
    "ArchiveObjectMismatch",
    "RunInterruptionArchiveError",
    "RunRecordAbsent",
    "publish_run_record",
    "read_archive_object",
    "read_regular_file",
    "read_run_record_bytes",
    "record_bytes",
    "store_archive_object",
]
