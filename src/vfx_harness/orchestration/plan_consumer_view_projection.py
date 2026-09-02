"""Exact descriptor-rooted population of one plan-consumer view.

The consumer-view allocator returns a lexical path for diagnostics, but that path is
never mutation authority.  Population runs only while the opaque construction
capability retains the allocated view directory descriptor.  Every descendant is
created relative to that descriptor, every component is opened without following a
symlink, and every mutation is read back before returning.
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
import threading
import weakref
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from vfx_harness.observability import fork_coordination
from vfx_harness.observability.prepared_publication_descriptors import (
    block_deferred_signals,
)
from vfx_harness.observability.run_owner_fork_guard import (
    ForkProtectedAcquisition,
    managed_fork_protected_acquisition,
)
from vfx_harness.orchestration.plan_consumer_owned_directory import (
    create_and_publish_empty_directory,
)
from vfx_harness.orchestration.plan_consumer_view_capabilities import (
    PlanConsumerViewMutationCapability,
)
from vfx_harness.orchestration.plan_consumer_view_descriptors import (
    PlanConsumerDirectoryIdentity,
    PlanConsumerViewMutationConflict,
    absolute_path,
    open_real_directory,
    same_named_directory,
)
from vfx_harness.orchestration.plan_consumer_view_mutation import (
    _advance_constructed_plan_consumer_ledger_binding,
    _require_current_plan_consumer_view_mutation,
)

if TYPE_CHECKING:
    from vfx_harness.orchestration.plan_consumer_view_registry import CapabilityRecord

_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_READ_FILE_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
)
_CREATE_FILE_FLAGS = (
    os.O_RDWR
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
)
_OWNER_ONLY_MODE = 0o600
_PROTECTED_ROOT_FILES = frozenset({".plan-consumer-view.json", "shot.json"})


@dataclass(frozen=True, slots=True)
class _DirectoryLink:
    parent_descriptor: int
    name: str
    child_descriptor: int
    identity: PlanConsumerDirectoryIdentity


@dataclass(slots=True)
class _PopulationEntry:
    reference: weakref.ReferenceType[PlanConsumerViewMutationCapability]
    view_identity: PlanConsumerDirectoryIdentity
    directories: dict[tuple[str, ...], PlanConsumerDirectoryIdentity]


_POPULATION_LOCK = threading.RLock()
_POPULATIONS: dict[int, _PopulationEntry] = {}


def _after_fork_child() -> None:
    global _POPULATION_LOCK

    _POPULATIONS.clear()
    _POPULATION_LOCK = threading.RLock()


fork_coordination.register_fork_participant(
    "orchestration.plan_consumer_view_projection",
    lock_factory=lambda: _POPULATION_LOCK,
    after_in_child=_after_fork_child,
)


def _population_gone(
    identifier: int,
    reference: weakref.ReferenceType[PlanConsumerViewMutationCapability],
) -> None:
    with fork_coordination.fork_coordinated_lock(_POPULATION_LOCK):
        entry = _POPULATIONS.get(identifier)
        if entry is not None and entry.reference is reference:
            _POPULATIONS.pop(identifier, None)


def _population_entry(
    capability: PlanConsumerViewMutationCapability,
    record: CapabilityRecord,
) -> _PopulationEntry:
    identifier = id(capability)
    with fork_coordination.fork_coordinated_lock(_POPULATION_LOCK):
        entry = _POPULATIONS.get(identifier)
        if entry is None:
            reference = weakref.ref(
                capability,
                lambda observed, key=identifier: _population_gone(key, observed),
            )
            entry = _PopulationEntry(reference, record.view_identity, {})
            _POPULATIONS[identifier] = entry
        if (
            entry.reference() is not capability
            or entry.view_identity != record.view_identity
        ):
            raise PlanConsumerViewMutationConflict(
                "plan-consumer population registry belongs to another view generation"
            )
        return entry


def _known_directory(
    entry: _PopulationEntry,
    relative: tuple[str, ...],
) -> PlanConsumerDirectoryIdentity | None:
    with fork_coordination.fork_coordinated_lock(_POPULATION_LOCK):
        return entry.directories.get(relative)


def _register_directory(
    entry: _PopulationEntry,
    relative: tuple[str, ...],
    identity: PlanConsumerDirectoryIdentity,
) -> None:
    with fork_coordination.fork_coordinated_lock(_POPULATION_LOCK):
        current = entry.directories.get(relative)
        if current is not None and current != identity:
            raise PlanConsumerViewMutationConflict(
                "plan-consumer projected directory identity changed during registration: "
                f"{'/'.join(relative)}"
            )
        entry.directories[relative] = identity


def _parts(relative: str | Path, where: str) -> tuple[str, ...]:
    raw = str(relative)
    normalized = PurePosixPath(raw)
    if (
        not raw
        or "\\" in raw
        or normalized.is_absolute()
        or normalized.as_posix() != raw
        or any(part in {"", ".", ".."} for part in normalized.parts)
    ):
        raise PlanConsumerViewMutationConflict(
            f"{where} must be a normalized relative POSIX path: {raw!r}"
        )
    return tuple(normalized.parts)


def _construction_record(
    capability: PlanConsumerViewMutationCapability,
) -> CapabilityRecord:
    record = _require_current_plan_consumer_view_mutation(
        capability,
        phase="construction",
    )
    if record.scratch_descriptor is None:
        raise PlanConsumerViewMutationConflict(
            "plan-consumer construction has no exact barrier-retirement scratch"
        )
    return record


def _identity_from_stat(observed: os.stat_result) -> PlanConsumerDirectoryIdentity:
    return PlanConsumerDirectoryIdentity(
        observed.st_dev,
        observed.st_ino,
        stat.S_IFMT(observed.st_mode),
    )


def _mkdir_and_open(
    parent_descriptor: int,
    name: str,
    acquisition: ForkProtectedAcquisition,
    population: _PopulationEntry,
    relative: tuple[str, ...],
    *,
    create: bool,
) -> tuple[int, PlanConsumerDirectoryIdentity]:
    expected = _known_directory(population, relative)
    created = False
    descriptor: int | None = None
    if create and expected is None:
        try:
            descriptor, _created_identity = create_and_publish_empty_directory(
                parent_descriptor,
                name,
                acquisition,
                where=(
                    "plan-consumer projected directory " f"{'/'.join(relative)}"
                ),
            )
        except FileExistsError:
            raise PlanConsumerViewMutationConflict(
                "plan-consumer projected directory appeared without this "
                f"construction capability: {'/'.join(relative)}"
            ) from None
        created = True
    try:
        named = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if descriptor is None:
            descriptor = acquisition.open_descriptor(
                lambda: os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_descriptor)
            )
            os.set_inheritable(descriptor, False)
        held = os.fstat(descriptor)
    except OSError as exc:
        action = "created" if create else "opened"
        raise PlanConsumerViewMutationConflict(
            f"plan-consumer directory component could not be {action} without "
            f"following a symlink: {name!r}"
        ) from exc
    named_identity = _identity_from_stat(named)
    held_identity = _identity_from_stat(held)
    if (
        not stat.S_ISDIR(named.st_mode)
        or not stat.S_ISDIR(held.st_mode)
        or named_identity != held_identity
        or (expected is not None and held_identity != expected)
    ):
        raise PlanConsumerViewMutationConflict(
            f"plan-consumer directory component was substituted: {name!r}"
        )
    if created:
        try:
            with block_deferred_signals():
                os.fsync(parent_descriptor)
        except OSError as exc:
            raise PlanConsumerViewMutationConflict(
                f"plan-consumer directory creation was not durable: {name!r}"
            ) from exc
        _register_directory(population, relative, held_identity)
    elif expected is None:
        raise PlanConsumerViewMutationConflict(
            "plan-consumer projected directory has no capability-owned identity: "
            f"{'/'.join(relative)}"
        )
    return descriptor, held_identity


def _directory_chain(
    record: CapabilityRecord,
    parts: tuple[str, ...],
    acquisition: ForkProtectedAcquisition,
    population: _PopulationEntry,
    *,
    create: bool,
) -> tuple[int, tuple[_DirectoryLink, ...]]:
    parent = record.view_descriptor
    links: list[_DirectoryLink] = []
    prefix: tuple[str, ...] = ()
    for name in parts:
        prefix += (name,)
        child, identity = _mkdir_and_open(
            parent,
            name,
            acquisition,
            population,
            prefix,
            create=create,
        )
        links.append(_DirectoryLink(parent, name, child, identity))
        parent = child
    return parent, tuple(links)


def _verify_directory_chain(links: tuple[_DirectoryLink, ...]) -> None:
    for link in links:
        try:
            named = os.stat(
                link.name,
                dir_fd=link.parent_descriptor,
                follow_symlinks=False,
            )
            held = os.fstat(link.child_descriptor)
        except OSError as exc:
            raise PlanConsumerViewMutationConflict(
                "plan-consumer descendant directory disappeared during population: "
                f"{link.name!r}"
            ) from exc
        if (
            _identity_from_stat(named) != link.identity
            or _identity_from_stat(held) != link.identity
            or not stat.S_ISDIR(named.st_mode)
        ):
            raise PlanConsumerViewMutationConflict(
                "plan-consumer descendant directory was rebound during population: "
                f"{link.name!r}"
            )


def _read_descriptor(descriptor: int) -> bytes:
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        return b"".join(chunks)
    except OSError as exc:
        raise PlanConsumerViewMutationConflict(
            "plan-consumer regular-file readback failed"
        ) from exc


def _stable_file_identity(observed: os.stat_result) -> tuple[int, ...]:
    return (
        observed.st_dev,
        observed.st_ino,
        stat.S_IFMT(observed.st_mode),
        observed.st_size,
        observed.st_mtime_ns,
        observed.st_ctime_ns,
    )


def _write_exact_file(
    record: CapabilityRecord,
    parts: tuple[str, ...],
    payload: bytes,
    population: _PopulationEntry,
) -> str:
    if not isinstance(payload, bytes):
        raise PlanConsumerViewMutationConflict(
            "plan-consumer projected file payload must be bytes"
        )
    with managed_fork_protected_acquisition() as acquisition:
        parent, links = _directory_chain(
            record,
            parts[:-1],
            acquisition,
            population,
            create=True,
        )
        name = parts[-1]
        try:
            descriptor = acquisition.open_descriptor(
                lambda: os.open(
                    name,
                    _CREATE_FILE_FLAGS,
                    _OWNER_ONLY_MODE,
                    dir_fd=parent,
                )
            )
            os.set_inheritable(descriptor, False)
        except OSError as exc:
            raise PlanConsumerViewMutationConflict(
                "plan-consumer projected file must be absent and creatable only "
                f"through the exact view descriptor: {'/'.join(parts)}"
            ) from exc
        try:
            with block_deferred_signals():
                offset = 0
                while offset < len(payload):
                    written = os.write(descriptor, payload[offset:])
                    if written <= 0:  # pragma: no cover - POSIX write invariant
                        raise OSError(errno.EIO, "zero-byte write")
                    offset += written
                os.fsync(descriptor)
                readback = _read_descriptor(descriptor)
                held = os.fstat(descriptor)
                named = os.stat(name, dir_fd=parent, follow_symlinks=False)
                os.fsync(parent)
        except OSError as exc:
            raise PlanConsumerViewMutationConflict(
                f"plan-consumer projected file write/readback failed: {'/'.join(parts)}"
            ) from exc
        if (
            not stat.S_ISREG(held.st_mode)
            or not stat.S_ISREG(named.st_mode)
            or _stable_file_identity(held) != _stable_file_identity(named)
            or readback != payload
        ):
            raise PlanConsumerViewMutationConflict(
                "plan-consumer projected file changed during readback: "
                f"{'/'.join(parts)}"
            )
        _verify_directory_chain(links)
    return hashlib.sha256(payload).hexdigest()


def ensure_directory(
    capability: PlanConsumerViewMutationCapability,
    relative: str | Path,
) -> None:
    """Create and read back one real directory chain below the held view root."""

    parts = _parts(relative, "plan-consumer projected directory")
    record = _construction_record(capability)
    population = _population_entry(capability, record)
    with managed_fork_protected_acquisition() as acquisition:
        descriptor, links = _directory_chain(
            record,
            parts,
            acquisition,
            population,
            create=True,
        )
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):  # pragma: no cover
            raise PlanConsumerViewMutationConflict(
                f"plan-consumer projected directory is not real: {'/'.join(parts)}"
            )
        _verify_directory_chain(links)
    if _construction_record(capability) is not record:
        raise PlanConsumerViewMutationConflict(
            "plan-consumer capability changed during directory population"
        )


def create_regular_file(
    capability: PlanConsumerViewMutationCapability,
    relative: str | Path,
    payload: bytes,
) -> str:
    """Create one new regular file through the exact view descriptor and read it back."""

    parts = _parts(relative, "plan-consumer projected file")
    if len(parts) == 1 and parts[0] in _PROTECTED_ROOT_FILES:
        raise PlanConsumerViewMutationConflict(
            f"plan-consumer root member {parts[0]!r} has a dedicated typed owner"
        )
    record = _construction_record(capability)
    population = _population_entry(capability, record)
    digest = _write_exact_file(record, parts, payload, population)
    if _construction_record(capability) is not record:
        raise PlanConsumerViewMutationConflict(
            "plan-consumer capability changed during file population"
        )
    return digest


def create_construction_ledger_snapshot(
    capability: PlanConsumerViewMutationCapability,
    payload: bytes,
) -> str:
    """Create the construction-only ledger copy and advance its exact binding."""

    record = _construction_record(capability)
    population = _population_entry(capability, record)
    with block_deferred_signals():
        digest = _write_exact_file(
            record,
            ("shot.json",),
            payload,
            population,
        )
        _advance_constructed_plan_consumer_ledger_binding(
            capability,
            record,
            digest,
        )
    _construction_record(capability)
    return digest


def require_regular_file_bytes(
    capability: PlanConsumerViewMutationCapability,
    relative: str | Path,
    expected: bytes,
) -> None:
    """Read a real projected file through the held view and require exact bytes."""

    if not isinstance(expected, bytes):
        raise PlanConsumerViewMutationConflict(
            "plan-consumer projected readback expectation must be bytes"
        )
    parts = _parts(relative, "plan-consumer projected readback")
    record = _construction_record(capability)
    population = _population_entry(capability, record)
    with managed_fork_protected_acquisition() as acquisition:
        parent, links = _directory_chain(
            record,
            parts[:-1],
            acquisition,
            population,
            create=False,
        )
        try:
            descriptor = acquisition.open_descriptor(
                lambda: os.open(parts[-1], _READ_FILE_FLAGS, dir_fd=parent)
            )
            os.set_inheritable(descriptor, False)
            before = os.fstat(descriptor)
            payload = _read_descriptor(descriptor)
            after = os.fstat(descriptor)
            named = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
        except OSError as exc:
            raise PlanConsumerViewMutationConflict(
                f"plan-consumer projected readback is not a real file: {'/'.join(parts)}"
            ) from exc
        if (
            not stat.S_ISREG(before.st_mode)
            or _stable_file_identity(before) != _stable_file_identity(after)
            or _stable_file_identity(after) != _stable_file_identity(named)
            or payload != expected
        ):
            raise PlanConsumerViewMutationConflict(
                "plan-consumer projected file failed exact readback: "
                f"{'/'.join(parts)}"
            )
        _verify_directory_chain(links)
    if _construction_record(capability) is not record:
        raise PlanConsumerViewMutationConflict(
            "plan-consumer capability changed during file readback"
        )


@contextmanager
def _opened_source_file(
    source: str | Path,
    expected_sha256: str | None,
):
    path = absolute_path(source)
    with managed_fork_protected_acquisition() as acquisition:
        parent_descriptor = open_real_directory(path.parent, acquisition)
        parent_identity = PlanConsumerDirectoryIdentity.capture(parent_descriptor)
        try:
            descriptor = acquisition.open_descriptor(
                lambda: os.open(path.name, _READ_FILE_FLAGS, dir_fd=parent_descriptor)
            )
            os.set_inheritable(descriptor, False)
            before = os.fstat(descriptor)
            payload = _read_descriptor(descriptor)
            after = os.fstat(descriptor)
        except OSError as exc:
            raise PlanConsumerViewMutationConflict(
                f"plan-consumer symlink source must be a real regular file: {path}"
            ) from exc
        digest = hashlib.sha256(payload).hexdigest()
        if (
            not stat.S_ISREG(before.st_mode)
            or _stable_file_identity(before) != _stable_file_identity(after)
            or (expected_sha256 is not None and digest != expected_sha256)
        ):
            raise PlanConsumerViewMutationConflict(
                f"plan-consumer symlink source bytes changed or mismatched: {path}"
            )
        yield path, descriptor, _stable_file_identity(after), digest
        named = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        final = os.fstat(descriptor)
        if (
            _stable_file_identity(named) != _stable_file_identity(final)
            or _stable_file_identity(final) != _stable_file_identity(after)
            or hashlib.sha256(_read_descriptor(descriptor)).hexdigest() != digest
        ):
            raise PlanConsumerViewMutationConflict(
                f"plan-consumer symlink source changed during projection: {path}"
            )
        same_named_directory(path.parent, parent_identity)


def read_exact_source_file(
    source: str | Path,
    *,
    expected_sha256: str | None = None,
) -> tuple[bytes, str]:
    """Read one stable real source file without following its leaf or parents."""

    with _opened_source_file(source, expected_sha256) as (
        _path,
        descriptor,
        _identity,
        digest,
    ):
        payload = _read_descriptor(descriptor)
    return payload, digest


def _create_symlink(
    record: CapabilityRecord,
    parts: tuple[str, ...],
    source: Path,
    population: _PopulationEntry,
) -> None:
    with managed_fork_protected_acquisition() as acquisition:
        parent, links = _directory_chain(
            record,
            parts[:-1],
            acquisition,
            population,
            create=True,
        )
        target = str(source)
        try:
            with block_deferred_signals():
                os.symlink(target, parts[-1], dir_fd=parent)
                observed = os.stat(
                    parts[-1],
                    dir_fd=parent,
                    follow_symlinks=False,
                )
                readback = os.readlink(parts[-1], dir_fd=parent)
                os.fsync(parent)
        except OSError as exc:
            raise PlanConsumerViewMutationConflict(
                "plan-consumer projected symlink must be absent and creatable only "
                f"through the exact view descriptor: {'/'.join(parts)}"
            ) from exc
        if not stat.S_ISLNK(observed.st_mode) or readback != target:
            raise PlanConsumerViewMutationConflict(
                f"plan-consumer projected symlink failed readback: {'/'.join(parts)}"
            )
        _verify_directory_chain(links)


def create_verified_file_symlink(
    capability: PlanConsumerViewMutationCapability,
    relative: str | Path,
    source: str | Path,
    *,
    expected_sha256: str,
) -> None:
    """Link one exact verified regular source below the held view root."""

    parts = _parts(relative, "plan-consumer projected symlink")
    record = _construction_record(capability)
    population = _population_entry(capability, record)
    with _opened_source_file(source, expected_sha256) as (
        path,
        _descriptor,
        _identity,
        _digest,
    ):
        _create_symlink(record, parts, path, population)
    if _construction_record(capability) is not record:
        raise PlanConsumerViewMutationConflict(
            "plan-consumer capability changed during symlink population"
        )


def create_verified_state_symlink(
    capability: PlanConsumerViewMutationCapability,
    relative: str | Path,
    source: str | Path,
) -> None:
    """Link one real state file or directory after exact source readback."""

    path = absolute_path(source)
    try:
        observed = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise PlanConsumerViewMutationConflict(
            f"plan-consumer state symlink source is unreadable: {path}"
        ) from exc
    if stat.S_ISREG(observed.st_mode):
        _payload, digest = read_exact_source_file(path)
        create_verified_file_symlink(
            capability,
            relative,
            path,
            expected_sha256=digest,
        )
        return
    if not stat.S_ISDIR(observed.st_mode):
        raise PlanConsumerViewMutationConflict(
            f"plan-consumer state symlink source must be a real file or directory: {path}"
        )
    record = _construction_record(capability)
    population = _population_entry(capability, record)
    with managed_fork_protected_acquisition() as acquisition:
        descriptor = open_real_directory(path, acquisition)
        identity = PlanConsumerDirectoryIdentity.capture(descriptor)
        _create_symlink(
            record,
            _parts(relative, "plan-consumer state symlink"),
            path,
            population,
        )
        if PlanConsumerDirectoryIdentity.capture(descriptor) != identity:
            raise PlanConsumerViewMutationConflict(
                f"plan-consumer state directory changed during projection: {path}"
            )
        same_named_directory(path, identity)
    if _construction_record(capability) is not record:
        raise PlanConsumerViewMutationConflict(
            "plan-consumer capability changed during state symlink population"
        )


def require_member_absent(
    capability: PlanConsumerViewMutationCapability,
    relative: str | Path,
) -> None:
    """Prove a member is absent through the held view descriptor."""

    parts = _parts(relative, "plan-consumer absent member")
    record = _construction_record(capability)
    population = _population_entry(capability, record)
    with managed_fork_protected_acquisition() as acquisition:
        parent, links = _directory_chain(
            record,
            parts[:-1],
            acquisition,
            population,
            create=False,
        )
        try:
            os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise PlanConsumerViewMutationConflict(
                f"plan-consumer member absence could not be proven: {'/'.join(parts)}"
            ) from exc
        else:
            raise PlanConsumerViewMutationConflict(
                f"plan-consumer member must be absent: {'/'.join(parts)}"
            )
        _verify_directory_chain(links)
    if _construction_record(capability) is not record:
        raise PlanConsumerViewMutationConflict(
            "plan-consumer capability changed during absence proof"
        )


__all__ = [
    "create_construction_ledger_snapshot",
    "create_regular_file",
    "create_verified_file_symlink",
    "create_verified_state_symlink",
    "ensure_directory",
    "read_exact_source_file",
    "require_member_absent",
    "require_regular_file_bytes",
]
