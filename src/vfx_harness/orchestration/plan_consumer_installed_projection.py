"""Descriptor-rooted mutation of one installed plan-consumer view.

An installed consumer view is mutable scratch, but its lexical path is never
mutation authority.  Every operation in this module accepts the opaque installed
capability, traverses from its retained view descriptor without following links or
crossing mounts, and reads back the exact inode or namespace effect it authored.
"""

from __future__ import annotations

import errno
import hashlib
import os
import secrets
import stat
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Literal

from vfx_harness.observability.prepared_publication_descriptors import (
    block_deferred_signals,
)
from vfx_harness.observability.run_owner_fork_guard import (
    ForkProtectedAcquisition,
    managed_fork_protected_acquisition,
)
from vfx_harness.orchestration.plan_consumer_owned_directory import (
    create_and_publish_empty_directory,
    open_beneath_directory,
)
from vfx_harness.orchestration.plan_consumer_view_capabilities import (
    PlanConsumerViewMutationCapability,
)
from vfx_harness.orchestration.plan_consumer_view_cleanup import (
    move_owned_directory_noreplace,
)
from vfx_harness.orchestration.plan_consumer_view_descriptors import (
    PlanConsumerDirectoryIdentity,
    PlanConsumerViewMutationConflict,
)
from vfx_harness.orchestration.plan_consumer_view_mutation import (
    _require_current_plan_consumer_view_mutation,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from vfx_harness.orchestration.plan_consumer_view_registry import CapabilityRecord

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
_RESERVED_ROOT_FILES = frozenset({"shot.json"})

MemberKind = Literal["directory", "regular", "symlink", "other"]


class _DirectoryLink(tuple):
    """Compact immutable parent/name/child/identity traversal record."""

    __slots__ = ()

    def __new__(
        cls,
        parent: int,
        name: str,
        child: int,
        identity: PlanConsumerDirectoryIdentity,
    ) -> _DirectoryLink:
        return tuple.__new__(cls, (parent, name, child, identity))

    @property
    def parent(self) -> int:
        return self[0]

    @property
    def name(self) -> str:
        return self[1]

    @property
    def child(self) -> int:
        return self[2]

    @property
    def identity(self) -> PlanConsumerDirectoryIdentity:
        return self[3]


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


def _installed_record(
    capability: PlanConsumerViewMutationCapability,
) -> CapabilityRecord:
    record = _require_current_plan_consumer_view_mutation(
        capability,
        phase="installed",
    )
    if record.scratch_descriptor is None:
        raise PlanConsumerViewMutationConflict(
            "installed plan-consumer mutation has no exact scratch descriptor"
        )
    return record


def _identity(observed: os.stat_result) -> PlanConsumerDirectoryIdentity:
    return PlanConsumerDirectoryIdentity(
        observed.st_dev,
        observed.st_ino,
        stat.S_IFMT(observed.st_mode),
    )


def _stable_file_identity(observed: os.stat_result) -> tuple[int, ...]:
    """Identity that survives the no-replace rename which publishes a staged file.

    ``st_ctime`` is deliberately excluded: every rename or link updates it, so
    it cannot join the staged inode to its published name.  Content identity is
    carried by size and modification time, which a rename leaves untouched.
    """

    return (
        observed.st_dev,
        observed.st_ino,
        stat.S_IFMT(observed.st_mode),
        observed.st_size,
        observed.st_mtime_ns,
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
            "installed plan-consumer regular-file readback failed"
        ) from exc


def _verify_links(links: tuple[_DirectoryLink, ...]) -> None:
    for link in links:
        try:
            named = os.stat(
                link.name,
                dir_fd=link.parent,
                follow_symlinks=False,
            )
            held = os.fstat(link.child)
        except OSError as exc:
            raise PlanConsumerViewMutationConflict(
                f"installed plan-consumer directory disappeared: {link.name!r}"
            ) from exc
        if (
            not stat.S_ISDIR(named.st_mode)
            or _identity(named) != link.identity
            or _identity(held) != link.identity
        ):
            raise PlanConsumerViewMutationConflict(
                f"installed plan-consumer directory was substituted: {link.name!r}"
            )


def _open_chain(
    record: CapabilityRecord,
    parts: tuple[str, ...],
    acquisition: ForkProtectedAcquisition,
    *,
    create: bool,
) -> tuple[int, tuple[_DirectoryLink, ...]]:
    parent = record.view_descriptor
    links: list[_DirectoryLink] = []
    for index, name in enumerate(parts):
        descriptor: int | None = None
        try:
            descriptor = acquisition.open_descriptor(
                lambda component=name, directory=parent: open_beneath_directory(
                    directory,
                    component,
                )
            )
            os.set_inheritable(descriptor, False)
        except FileNotFoundError:
            if not create:
                raise PlanConsumerViewMutationConflict(
                    "installed plan-consumer directory is absent: "
                    f"{'/'.join(parts[: index + 1])}"
                ) from None
            assert record.scratch_descriptor is not None
            try:
                descriptor, _created = create_and_publish_empty_directory(
                    parent,
                    name,
                    acquisition,
                    where=(
                        "installed plan-consumer directory "
                        f"{'/'.join(parts[: index + 1])}"
                    ),
                )
            except FileExistsError:
                raise PlanConsumerViewMutationConflict(
                    "installed plan-consumer directory appeared concurrently: "
                    f"{'/'.join(parts[: index + 1])}"
                ) from None
        except OSError as exc:
            raise PlanConsumerViewMutationConflict(
                "installed plan-consumer directory cannot be traversed without "
                f"following a link: {'/'.join(parts[: index + 1])}"
            ) from exc
        assert descriptor is not None
        try:
            named = os.stat(name, dir_fd=parent, follow_symlinks=False)
            held = os.fstat(descriptor)
        except OSError as exc:
            raise PlanConsumerViewMutationConflict(
                "installed plan-consumer directory changed during traversal: "
                f"{'/'.join(parts[: index + 1])}"
            ) from exc
        identity = _identity(held)
        if not stat.S_ISDIR(named.st_mode) or _identity(named) != identity:
            raise PlanConsumerViewMutationConflict(
                "installed plan-consumer directory was substituted during traversal: "
                f"{'/'.join(parts[: index + 1])}"
            )
        links.append(_DirectoryLink(parent, name, descriptor, identity))
        parent = descriptor
    return parent, tuple(links)


def _member_stat(parent: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PlanConsumerViewMutationConflict(
            f"installed plan-consumer member is unreadable: {name!r}"
        ) from exc


def _write_new_file_at(
    parent: int,
    name: str,
    payload: bytes,
    acquisition: ForkProtectedAcquisition,
) -> tuple[int, tuple[int, ...]]:
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
        with block_deferred_signals():
            offset = 0
            while offset < len(payload):
                written = os.write(descriptor, payload[offset:])
                if written <= 0:  # pragma: no cover - POSIX write invariant
                    raise OSError(errno.EIO, "zero-byte write")
                offset += written
            os.fsync(descriptor)
        held = os.fstat(descriptor)
        if not stat.S_ISREG(held.st_mode) or _read_descriptor(descriptor) != payload:
            raise PlanConsumerViewMutationConflict(
                f"installed plan-consumer staged file failed readback: {name!r}"
            )
        return descriptor, _stable_file_identity(held)
    except OSError as exc:
        raise PlanConsumerViewMutationConflict(
            f"installed plan-consumer staged file could not be created: {name!r}"
        ) from exc


def member_kind(
    capability: PlanConsumerViewMutationCapability,
    relative: str | Path,
) -> MemberKind | None:
    """Inspect one leaf through the retained installed-view descriptor."""

    parts = _parts(relative, "installed plan-consumer member inspection")
    record = _installed_record(capability)
    with managed_fork_protected_acquisition() as acquisition:
        parent, links = _open_chain(record, parts[:-1], acquisition, create=False)
        observed = _member_stat(parent, parts[-1])
        _verify_links(links)
    _installed_record(capability)
    if observed is None:
        return None
    if stat.S_ISDIR(observed.st_mode):
        return "directory"
    if stat.S_ISREG(observed.st_mode):
        return "regular"
    if stat.S_ISLNK(observed.st_mode):
        return "symlink"
    return "other"


def ensure_directory(
    capability: PlanConsumerViewMutationCapability,
    relative: str | Path,
) -> None:
    """Create or prove one real directory chain below the exact installed view."""

    parts = _parts(relative, "installed plan-consumer directory")
    record = _installed_record(capability)
    with managed_fork_protected_acquisition() as acquisition:
        _directory, links = _open_chain(record, parts, acquisition, create=True)
        _verify_links(links)
    _installed_record(capability)


def replace_regular_file(
    capability: PlanConsumerViewMutationCapability,
    relative: str | Path,
    payload: bytes,
) -> str:
    """Atomically replace one regular leaf and prove its exact staged inode/bytes."""

    if not isinstance(payload, bytes):
        raise PlanConsumerViewMutationConflict(
            "installed plan-consumer file payload must be bytes"
        )
    parts = _parts(relative, "installed plan-consumer file replacement")
    if len(parts) == 1 and parts[0] in _RESERVED_ROOT_FILES:
        raise PlanConsumerViewMutationConflict(
            "installed plan-consumer shot.json has a dedicated typed CAS owner"
        )
    record = _installed_record(capability)
    with managed_fork_protected_acquisition() as acquisition:
        parent, links = _open_chain(record, parts[:-1], acquisition, create=True)
        current = _member_stat(parent, parts[-1])
        if current is not None and not stat.S_ISREG(current.st_mode):
            raise PlanConsumerViewMutationConflict(
                "installed plan-consumer replacement target must be absent or a "
                f"real regular file: {'/'.join(parts)}"
            )
        staged_name = f".plan-consumer-file.tmp-{secrets.token_hex(16)}"
        descriptor, staged_identity = _write_new_file_at(
            parent,
            staged_name,
            payload,
            acquisition,
        )
        try:
            with block_deferred_signals():
                os.replace(
                    staged_name,
                    parts[-1],
                    src_dir_fd=parent,
                    dst_dir_fd=parent,
                )
                os.fsync(parent)
            named = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
            held = os.fstat(descriptor)
        except OSError as exc:
            raise PlanConsumerViewMutationConflict(
                "installed plan-consumer regular-file replacement could not be "
                f"post-proven: {'/'.join(parts)}"
            ) from exc
        if (
            _stable_file_identity(named) != staged_identity
            or _stable_file_identity(held) != staged_identity
            or _read_descriptor(descriptor) != payload
        ):
            raise PlanConsumerViewMutationConflict(
                "installed plan-consumer replacement changed during readback: "
                f"{'/'.join(parts)}"
            )
        _verify_links(links)
    _installed_record(capability)
    return hashlib.sha256(payload).hexdigest()


def _remove_leaf(
    capability: PlanConsumerViewMutationCapability,
    relative: str | Path,
    expected_kind: Literal["regular", "symlink"],
) -> None:
    parts = _parts(relative, "installed plan-consumer member removal")
    if len(parts) == 1 and parts[0] in _RESERVED_ROOT_FILES:
        raise PlanConsumerViewMutationConflict(
            "installed plan-consumer shot.json has a dedicated typed CAS owner"
        )
    record = _installed_record(capability)
    with managed_fork_protected_acquisition() as acquisition:
        parent, links = _open_chain(record, parts[:-1], acquisition, create=False)
        observed = _member_stat(parent, parts[-1])
        if observed is None:
            return
        matches = (
            stat.S_ISREG(observed.st_mode)
            if expected_kind == "regular"
            else stat.S_ISLNK(observed.st_mode)
        )
        if not matches:
            raise PlanConsumerViewMutationConflict(
                f"installed plan-consumer removal requires a {expected_kind} leaf: "
                f"{'/'.join(parts)}"
            )
        try:
            with block_deferred_signals():
                os.unlink(parts[-1], dir_fd=parent)
                os.fsync(parent)
        except OSError as exc:
            raise PlanConsumerViewMutationConflict(
                f"installed plan-consumer leaf removal failed: {'/'.join(parts)}"
            ) from exc
        if _member_stat(parent, parts[-1]) is not None:
            raise PlanConsumerViewMutationConflict(
                f"installed plan-consumer leaf remained after removal: {'/'.join(parts)}"
            )
        _verify_links(links)
    _installed_record(capability)


def remove_regular_file(
    capability: PlanConsumerViewMutationCapability,
    relative: str | Path,
) -> None:
    """Remove one absent-or-regular leaf through its exact parent descriptor."""

    _remove_leaf(capability, relative, "regular")


def remove_symlink(
    capability: PlanConsumerViewMutationCapability,
    relative: str | Path,
) -> None:
    """Remove one absent-or-symlink leaf through its exact parent descriptor."""

    _remove_leaf(capability, relative, "symlink")


def create_symlink(
    capability: PlanConsumerViewMutationCapability,
    relative: str | Path,
    target: str | Path,
) -> None:
    """Create one new symlink and read its exact target back without following it."""

    parts = _parts(relative, "installed plan-consumer symlink")
    target_text = str(target)
    if not target_text:
        raise PlanConsumerViewMutationConflict(
            "installed plan-consumer symlink target must not be empty"
        )
    record = _installed_record(capability)
    with managed_fork_protected_acquisition() as acquisition:
        parent, links = _open_chain(record, parts[:-1], acquisition, create=True)
        try:
            with block_deferred_signals():
                os.symlink(target_text, parts[-1], dir_fd=parent)
                observed = os.stat(
                    parts[-1],
                    dir_fd=parent,
                    follow_symlinks=False,
                )
                readback = os.readlink(parts[-1], dir_fd=parent)
                os.fsync(parent)
        except OSError as exc:
            raise PlanConsumerViewMutationConflict(
                f"installed plan-consumer symlink creation failed: {'/'.join(parts)}"
            ) from exc
        if not stat.S_ISLNK(observed.st_mode) or readback != target_text:
            raise PlanConsumerViewMutationConflict(
                f"installed plan-consumer symlink failed readback: {'/'.join(parts)}"
            )
        _verify_links(links)
    _installed_record(capability)


def read_symlink(
    capability: PlanConsumerViewMutationCapability,
    relative: str | Path,
) -> str:
    """Read one symlink target through its exact parent descriptor."""

    parts = _parts(relative, "installed plan-consumer symlink readback")
    record = _installed_record(capability)
    with managed_fork_protected_acquisition() as acquisition:
        parent, links = _open_chain(record, parts[:-1], acquisition, create=False)
        observed = _member_stat(parent, parts[-1])
        if observed is None or not stat.S_ISLNK(observed.st_mode):
            raise PlanConsumerViewMutationConflict(
                f"installed plan-consumer member is not a symlink: {'/'.join(parts)}"
            )
        target = os.readlink(parts[-1], dir_fd=parent)
        _verify_links(links)
    _installed_record(capability)
    return target


def list_directory(
    capability: PlanConsumerViewMutationCapability,
    relative: str | Path,
) -> tuple[str, ...]:
    """List one real directory through the exact installed-view descriptor chain."""

    parts = _parts(relative, "installed plan-consumer directory listing")
    record = _installed_record(capability)
    with managed_fork_protected_acquisition() as acquisition:
        descriptor, links = _open_chain(record, parts, acquisition, create=False)
        try:
            names = tuple(sorted(os.listdir(descriptor)))
        except OSError as exc:
            raise PlanConsumerViewMutationConflict(
                f"installed plan-consumer directory is unreadable: {'/'.join(parts)}"
            ) from exc
        _verify_links(links)
    _installed_record(capability)
    return names


def read_regular_file(
    capability: PlanConsumerViewMutationCapability,
    relative: str | Path,
) -> bytes:
    """Read and stabilize one real regular file below the installed view."""

    parts = _parts(relative, "installed plan-consumer regular-file readback")
    record = _installed_record(capability)
    with managed_fork_protected_acquisition() as acquisition:
        parent, links = _open_chain(record, parts[:-1], acquisition, create=False)
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
                "installed plan-consumer member is not a real regular file: "
                f"{'/'.join(parts)}"
            ) from exc
        if (
            not stat.S_ISREG(before.st_mode)
            or _stable_file_identity(before) != _stable_file_identity(after)
            or _stable_file_identity(after) != _stable_file_identity(named)
        ):
            raise PlanConsumerViewMutationConflict(
                "installed plan-consumer regular file changed during readback: "
                f"{'/'.join(parts)}"
            )
        _verify_links(links)
    _installed_record(capability)
    return payload


def _verify_directory_payloads_at(
    descriptor: int,
    expected: Mapping[str, bytes],
    acquisition: ForkProtectedAcquisition,
    *,
    where: str,
) -> None:
    names = tuple(sorted(os.listdir(descriptor)))
    if set(names) != set(expected):
        raise PlanConsumerViewMutationConflict(
            f"{where} member set differs: missing={sorted(set(expected) - set(names))}; "
            f"unexpected={sorted(set(names) - set(expected))}"
        )
    for name in names:
        _parts(name, f"{where} member")
        try:
            member = acquisition.open_descriptor(
                lambda child=name: os.open(child, _READ_FILE_FLAGS, dir_fd=descriptor)
            )
            os.set_inheritable(member, False)
            before = os.fstat(member)
            payload = _read_descriptor(member)
            after = os.fstat(member)
            named = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except OSError as exc:
            raise PlanConsumerViewMutationConflict(
                f"{where} member is not a real regular file: {name!r}"
            ) from exc
        if (
            not stat.S_ISREG(before.st_mode)
            or _stable_file_identity(before) != _stable_file_identity(after)
            or _stable_file_identity(after) != _stable_file_identity(named)
            or payload != expected[name]
        ):
            raise PlanConsumerViewMutationConflict(
                f"{where} member conflicts with expected bytes: {name!r}"
            )


def require_directory_payloads(
    capability: PlanConsumerViewMutationCapability,
    relative: str | Path,
    expected: Mapping[str, bytes],
) -> None:
    """Require one real directory to contain exactly the supplied regular files."""

    parts = _parts(relative, "installed plan-consumer directory verification")
    copied = dict(expected)
    if any(not isinstance(name, str) or not isinstance(payload, bytes) for name, payload in copied.items()):
        raise PlanConsumerViewMutationConflict(
            "installed plan-consumer directory expectations must map names to bytes"
        )
    record = _installed_record(capability)
    with managed_fork_protected_acquisition() as acquisition:
        descriptor, links = _open_chain(record, parts, acquisition, create=False)
        _verify_directory_payloads_at(
            descriptor,
            copied,
            acquisition,
            where=f"installed plan-consumer directory {'/'.join(parts)}",
        )
        _verify_links(links)
    _installed_record(capability)


def install_immutable_directory(
    capability: PlanConsumerViewMutationCapability,
    relative: str | Path,
    payloads: Mapping[str, bytes],
) -> Path:
    """Install-or-verify one content-addressed directory without exposing partial bytes."""

    parts = _parts(relative, "installed plan-consumer immutable directory")
    copied = dict(payloads)
    for name, payload in copied.items():
        if len(_parts(name, "installed immutable member")) != 1 or not isinstance(payload, bytes):
            raise PlanConsumerViewMutationConflict(
                "installed immutable directory members must be exact names with bytes"
            )
    record = _installed_record(capability)
    with managed_fork_protected_acquisition() as acquisition:
        parent, links = _open_chain(record, parts[:-1], acquisition, create=True)
        existing = _member_stat(parent, parts[-1])
        if existing is not None:
            if not stat.S_ISDIR(existing.st_mode):
                raise PlanConsumerViewMutationConflict(
                    "installed immutable directory destination is not a real directory: "
                    f"{'/'.join(parts)}"
                )
            root = acquisition.open_descriptor(
                lambda: open_beneath_directory(parent, parts[-1])
            )
            _verify_directory_payloads_at(
                root,
                copied,
                acquisition,
                where=f"installed immutable directory {'/'.join(parts)}",
            )
        else:
            assert record.scratch_descriptor is not None
            staging_name = f".plan-consumer-bundle.tmp-{secrets.token_hex(16)}"
            staging, staging_identity = create_and_publish_empty_directory(
                parent,
                staging_name,
                acquisition,
                where=f"installed immutable staging {'/'.join(parts)}",
            )
            for name, payload in copied.items():
                _descriptor, _identity = _write_new_file_at(
                    staging,
                    name,
                    payload,
                    acquisition,
                )
            os.fsync(staging)
            move_owned_directory_noreplace(
                parent,
                staging_name,
                parts[-1],
                staging_identity,
                where=f"installed immutable publication {'/'.join(parts)}",
            )
            _verify_directory_payloads_at(
                staging,
                copied,
                acquisition,
                where=f"installed immutable directory {'/'.join(parts)}",
            )
        _verify_links(links)
    _installed_record(capability)
    return record.view.joinpath(*parts)


__all__ = [
    "create_symlink",
    "ensure_directory",
    "install_immutable_directory",
    "list_directory",
    "member_kind",
    "read_regular_file",
    "read_symlink",
    "remove_regular_file",
    "remove_symlink",
    "replace_regular_file",
    "require_directory_payloads",
]
