"""Filesystem primitives shared by key-addressed immutable state stores.

Receipt-backed adapters (environment recovery, the run controller) publish canonical JSON
records that must never change once written, read them back exactly, reference them as typed
stop evidence, and serialize one idempotency key at a time.  One leaf owns those primitives so
no adapter carries its own copy (HIR-0166, ADR-0010).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from vfx_harness.domain.stop_transaction_state import StopEvidenceRef


def canonical_bytes(value: dict[str, Any], *, where: str = "immutable record") -> bytes:
    try:
        return (
            json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{where} must be finite canonical JSON") from exc


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def require_key(value: str, *, where: str = "idempotency key") -> str:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"{where} must be a lowercase SHA-256 digest")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"immutable record contains duplicate JSON key {key!r}")
        value[key] = item
    return value


def read_json(path: Path, where: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{where} must be an immutable regular file")
    try:
        value = json.loads(path.read_bytes(), object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{where} is malformed JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{where} must contain an object")
    return value


def publish_immutable(path: Path, raw: bytes) -> None:
    """Write ``raw`` once; identical bytes are idempotent and different bytes conflict."""

    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != raw:
            raise ValueError(f"immutable record {path} conflicts with existing bytes")
        return
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink() or not path.is_file() or path.read_bytes() != raw:
                raise ValueError(f"immutable record {path} conflicts with existing bytes") from None
    finally:
        temporary.unlink(missing_ok=True)
        fsync_directory(path.parent)


def evidence_ref(shot: Path, path: Path, *, kind: str, schema: str, digest: str) -> StopEvidenceRef:
    raw = path.read_bytes()
    return StopEvidenceRef(
        kind=kind,
        locator=path.resolve().relative_to(shot).as_posix(),
        sha256=hashlib.sha256(raw).hexdigest(),
        record_schema=schema,
        record_digest=digest,
    )


def ensure_directories(*directories: Path) -> None:
    """Create real directories in order, refusing symlinks and non-directories."""

    for directory in directories:
        if directory.is_symlink():
            raise ValueError(f"state directory {directory} must not be a symlink")
        existed = directory.exists()
        directory.mkdir(exist_ok=True)
        if not directory.is_dir():
            raise ValueError(f"state path {directory} must be a directory")
        if not existed:
            fsync_directory(directory.parent)
        fsync_directory(directory)


@contextmanager
def key_lock(lock_path: Path) -> Iterator[None]:
    """Serialize one idempotency key through an exclusive lock on a regular file."""

    if lock_path.is_symlink() or (lock_path.exists() and not lock_path.is_file()):
        raise ValueError(f"lock {lock_path} must be a regular file")
    with lock_path.open("a+b") as handle:
        handle.flush()
        os.fsync(handle.fileno())
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


__all__ = [
    "canonical_bytes",
    "ensure_directories",
    "evidence_ref",
    "fsync_directory",
    "key_lock",
    "publish_immutable",
    "read_json",
    "require_key",
]
