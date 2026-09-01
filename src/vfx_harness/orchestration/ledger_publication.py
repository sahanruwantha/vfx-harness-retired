"""Prepared, compare-and-swap publication for ``shot.json``.

The ledger merge can read and serialize an arbitrarily large history.  It therefore
runs under the ledger lock alone.  A caller may subsequently hold narrower authority
locks around :func:`commit_ledger_publication_locked`, whose work is limited to identity
checks, one same-parent rename, and a directory flush.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_TEMP_FLAGS = (
    os.O_RDWR
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)


class LedgerSaveConflict(RuntimeError):
    """A prepared merge no longer names the exact current ledger generation."""


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    device: int
    inode: int
    size: int
    modified_ns: int
    changed_ns: int


@dataclass(frozen=True, slots=True)
class PreparedLedgerPublication:
    """An fsynced, unreferenced ledger inode bound to one target generation."""

    path: Path
    temporary: Path
    payload_sha256: str
    prior_identity: _FileIdentity | None
    temporary_identity: _FileIdentity
    directory_device: int
    directory_inode: int
    directory_descriptor: int
    temporary_descriptor: int
    temporary_name: str
    authority_binding: str | None


def _identity(observed: os.stat_result) -> _FileIdentity:
    return _FileIdentity(
        device=observed.st_dev,
        inode=observed.st_ino,
        size=observed.st_size,
        modified_ns=observed.st_mtime_ns,
        changed_ns=observed.st_ctime_ns,
    )


def _open_real_directory(path: Path) -> int:
    absolute = path.expanduser().absolute()
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
        raise LedgerSaveConflict(
            f"ledger parent must be a real non-symlink directory: {absolute}"
        ) from exc
    if descriptor is None:
        raise LedgerSaveConflict(
            f"ledger parent must be a real non-symlink directory: {absolute}"
        )
    return descriptor


def _read_current(
    directory: int,
    name: str,
    path: Path,
) -> tuple[bytes | None, _FileIdentity | None]:
    try:
        descriptor = os.open(name, _READ_FLAGS, dir_fd=directory)
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        raise LedgerSaveConflict(
            f"ledger target must be absent or a real regular file: {path}"
        ) from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise LedgerSaveConflict(
                f"ledger target must be absent or a real regular file: {path}"
            )
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if _identity(after) != _identity(before):
            raise LedgerSaveConflict(
                f"ledger changed while it was read: {path}; restart from current shot.json"
            )
        return b"".join(chunks), _identity(after)
    finally:
        os.close(descriptor)


def _current_identity(
    directory: int,
    name: str,
    path: Path,
) -> _FileIdentity | None:
    try:
        observed = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise LedgerSaveConflict(f"ledger target is unreadable: {path}") from exc
    if not stat.S_ISREG(observed.st_mode):
        raise LedgerSaveConflict(
            f"ledger target must be absent or a real regular file: {path}"
        )
    return _identity(observed)


def _prepared_temporary_is_current(
    prepared: PreparedLedgerPublication,
) -> bool:
    """Prove the live temp name still refers to the exact held prepared inode."""

    try:
        held = os.fstat(prepared.temporary_descriptor)
        named = os.stat(
            prepared.temporary_name,
            dir_fd=prepared.directory_descriptor,
            follow_symlinks=False,
        )
    except OSError:
        return False
    return (
        stat.S_ISREG(held.st_mode)
        and stat.S_ISREG(named.st_mode)
        and _identity(held) == prepared.temporary_identity
        and _identity(named) == prepared.temporary_identity
    )


def _merge_payload(
    on_disk: Mapping[str, Any],
    data: Mapping[str, Any],
    loaded: Mapping[str, Any],
    touched: frozenset[str],
    run_id: str,
) -> bytes:
    merged = dict(on_disk)
    for key, value in data.items():
        if key == "milestones":
            continue
        if key not in on_disk or value != loaded.get(key):
            merged[key] = value
    slots = dict(on_disk.get("milestones", {}))
    local_slots = data.get("milestones", {})
    for layer_id in touched:
        slots[layer_id] = local_slots.get(layer_id, {})
    merged["milestones"] = slots
    merged.setdefault("runs", [])
    if run_id not in merged["runs"]:
        merged["runs"] = (merged["runs"] + [run_id])[-20:]
    return (json.dumps(merged, indent=2) + "\n").encode()


def _unique_temporary(directory: int, target_name: str) -> tuple[int, str]:
    for _attempt in range(128):
        name = f".{target_name}.prepared.{secrets.token_hex(12)}"
        try:
            return os.open(name, _TEMP_FLAGS, 0o600, dir_fd=directory), name
        except FileExistsError:
            continue
        except OSError as exc:
            raise LedgerSaveConflict(
                "could not allocate a prepared ledger publication"
            ) from exc
    raise LedgerSaveConflict("could not allocate a unique prepared ledger publication")


def _write_all(descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("short write while preparing shot.json")
        remaining = remaining[written:]


def prepare_ledger_publication_locked(
    path: str | Path,
    data: Mapping[str, Any],
    loaded: Mapping[str, Any],
    touched: frozenset[str],
    *,
    run_id: str,
    authority_binding: str | None,
) -> PreparedLedgerPublication:
    """Merge and fsync ledger bytes while the caller owns ledger EX only."""

    target = Path(path).expanduser().absolute()
    directory = _open_real_directory(target.parent)
    temporary_descriptor: int | None = None
    temporary_name: str | None = None
    try:
        directory_stat = os.fstat(directory)
        current, prior_identity = _read_current(directory, target.name, target)
        on_disk: Mapping[str, Any]
        if current is None:
            on_disk = {}
        else:
            parsed = json.loads(current)
            if not isinstance(parsed, dict):
                raise LedgerSaveConflict(
                    f"ledger root must be a JSON object: {target}; repair it before retrying"
                )
            on_disk = parsed
        payload = _merge_payload(on_disk, data, loaded, touched, run_id)
        temporary_descriptor, temporary_name = _unique_temporary(
            directory,
            target.name,
        )
        _write_all(temporary_descriptor, payload)
        os.fsync(temporary_descriptor)
        os.lseek(temporary_descriptor, 0, os.SEEK_SET)
        observed_digest = hashlib.sha256()
        while chunk := os.read(temporary_descriptor, 1024 * 1024):
            observed_digest.update(chunk)
        expected_digest = hashlib.sha256(payload).hexdigest()
        if observed_digest.hexdigest() != expected_digest:
            raise LedgerSaveConflict(
                f"prepared ledger bytes changed before publication: {target}"
            )
        temporary_identity = _identity(os.fstat(temporary_descriptor))
        return PreparedLedgerPublication(
            path=target,
            temporary=target.parent / temporary_name,
            payload_sha256=expected_digest,
            prior_identity=prior_identity,
            temporary_identity=temporary_identity,
            directory_device=directory_stat.st_dev,
            directory_inode=directory_stat.st_ino,
            directory_descriptor=directory,
            temporary_descriptor=temporary_descriptor,
            temporary_name=temporary_name,
            authority_binding=authority_binding,
        )
    except BaseException:
        if temporary_descriptor is not None:
            os.close(temporary_descriptor)
        if temporary_name is not None:
            with suppress(FileNotFoundError):
                os.unlink(temporary_name, dir_fd=directory)
        os.close(directory)
        raise


def commit_ledger_publication_locked(
    prepared: PreparedLedgerPublication,
    *,
    authority_binding: str | None,
) -> str:
    """CAS-publish one prepared ledger while the caller owns ledger EX."""

    if not isinstance(prepared, PreparedLedgerPublication):
        raise TypeError("ledger publication requires PreparedLedgerPublication")
    if prepared.authority_binding != authority_binding:
        raise LedgerSaveConflict(
            "prepared ledger authority binding changed; discard it and restart the "
            "owning operation from current authority"
        )
    if not _prepared_temporary_is_current(prepared):
        raise LedgerSaveConflict(
            f"prepared ledger name no longer binds its held inode: {prepared.temporary}"
        )
    current_directory = _open_real_directory(prepared.path.parent)
    try:
        current_directory_stat = os.fstat(current_directory)
    finally:
        os.close(current_directory)
    if (current_directory_stat.st_dev, current_directory_stat.st_ino) != (
        prepared.directory_device,
        prepared.directory_inode,
    ):
        raise LedgerSaveConflict(
            f"ledger parent changed after preparation: {prepared.path.parent}; "
            "restart from current shot.json"
        )
    current_identity = _current_identity(
        prepared.directory_descriptor,
        prepared.path.name,
        prepared.path,
    )
    if current_identity != prepared.prior_identity:
        raise LedgerSaveConflict(
            f"ledger changed after preparation: {prepared.path}; no update was written. "
            "Restart the owning operation from current shot.json and current authority"
        )
    try:
        os.replace(
            prepared.temporary_name,
            prepared.path.name,
            src_dir_fd=prepared.directory_descriptor,
            dst_dir_fd=prepared.directory_descriptor,
        )
        current_directory = _open_real_directory(prepared.path.parent)
        try:
            current_directory_stat = os.fstat(current_directory)
        finally:
            os.close(current_directory)
        if (current_directory_stat.st_dev, current_directory_stat.st_ino) != (
            prepared.directory_device,
            prepared.directory_inode,
        ):
            raise LedgerSaveConflict(
                f"ledger parent changed during publication: {prepared.path.parent}; "
                "the prepared update did not bind the current shot path"
            )
        os.fsync(prepared.directory_descriptor)
    except LedgerSaveConflict:
        raise
    except OSError as exc:
        raise LedgerSaveConflict(
            f"could not durably publish prepared ledger: {prepared.path}; "
            "restart from current shot.json"
        ) from exc
    return prepared.payload_sha256


def discard_prepared_ledger_publication(
    prepared: PreparedLedgerPublication,
) -> None:
    """Remove an uncommitted prepared inode and close its stable descriptors."""

    if not isinstance(prepared, PreparedLedgerPublication):
        return
    try:
        if _prepared_temporary_is_current(prepared):
            with suppress(FileNotFoundError):
                os.unlink(
                    prepared.temporary_name,
                    dir_fd=prepared.directory_descriptor,
                )
    finally:
        with suppress(OSError):
            os.close(prepared.temporary_descriptor)
        with suppress(OSError):
            os.close(prepared.directory_descriptor)
