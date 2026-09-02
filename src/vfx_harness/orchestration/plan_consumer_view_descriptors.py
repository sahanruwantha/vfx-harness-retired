"""Fork-safe physical directory primitives for plan-consumer views."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from vfx_harness.observability.run_owner_fork_guard import (
    ForkProtectedAcquisition,
    GuardedDescriptor,
    RunOwnerForkGuardCleanupError,
    managed_fork_protected_acquisition,
    neutralize_active_descriptors,
)

_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_FILE_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
)


class PlanConsumerViewMutationConflict(ValueError):
    """The requested view is not a live physically isolated mutation target."""


@dataclass(frozen=True, slots=True)
class PlanConsumerDirectoryIdentity:
    device: int
    inode: int
    file_type: int

    @classmethod
    def capture(cls, descriptor: int) -> PlanConsumerDirectoryIdentity:
        observed = os.fstat(descriptor)
        return cls(
            observed.st_dev,
            observed.st_ino,
            stat.S_IFMT(observed.st_mode),
        )


@dataclass(frozen=True, slots=True)
class PlanConsumerLedgerBinding:
    """Exact optional ledger inode and bytes inside one view generation."""

    identity: tuple[int, int, int] | None
    sha256: str | None


def absolute_path(path: str | Path) -> Path:
    return Path(os.path.abspath(Path(path).expanduser()))


def open_real_directory(
    path: Path,
    acquisition: ForkProtectedAcquisition,
) -> int:
    descriptor: int | None = None
    try:
        descriptor = acquisition.open_descriptor(
            lambda: os.open(path.anchor, _DIRECTORY_FLAGS)
        )
        os.set_inheritable(descriptor, False)
        for part in path.parts[1:]:
            following = acquisition.open_descriptor(
                lambda component=part, parent=descriptor: os.open(
                    component,
                    _DIRECTORY_FLAGS,
                    dir_fd=parent,
                )
            )
            os.set_inheritable(following, False)
            acquisition.retire(descriptor)
            descriptor = following
    except OSError as exc:
        raise PlanConsumerViewMutationConflict(
            f"plan-consumer view path must contain only real directory components: {path}"
        ) from exc
    if descriptor is None or not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        raise PlanConsumerViewMutationConflict(
            f"plan-consumer view path must be a real directory: {path}"
        )
    return descriptor


def read_marker(
    view_descriptor: int,
    acquisition: ForkProtectedAcquisition,
    view: Path,
) -> bytes:
    try:
        descriptor = acquisition.open_descriptor(
            lambda: os.open(
                ".plan-consumer-view.json",
                _FILE_FLAGS,
                dir_fd=view_descriptor,
            )
        )
    except OSError as exc:
        raise PlanConsumerViewMutationConflict(
            f"plan-consumer mutation target has no real marker: {view}"
        ) from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise PlanConsumerViewMutationConflict(
                f"plan-consumer mutation target marker must be a real file: {view}"
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
        if before_identity != after_identity:
            raise PlanConsumerViewMutationConflict(
                f"plan-consumer mutation target marker changed while read: {view}"
            )
        return b"".join(chunks)
    finally:
        acquisition.retire(descriptor)


def ledger_leaf_identity(directory: int, root: Path) -> tuple[int, int] | None:
    try:
        observed = os.stat(
            "shot.json",
            dir_fd=directory,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PlanConsumerViewMutationConflict(
            f"plan-consumer ledger member is unreadable: {root / 'shot.json'}"
        ) from exc
    if not stat.S_ISREG(observed.st_mode):
        raise PlanConsumerViewMutationConflict(
            "plan-consumer ledger member must be absent or a real regular file: "
            f"{root / 'shot.json'}"
        )
    return observed.st_dev, observed.st_ino


def capture_ledger_binding(
    view: Path,
    expected_view: PlanConsumerDirectoryIdentity,
) -> PlanConsumerLedgerBinding:
    """Read one stable real ledger leaf through its exact view generation."""

    with managed_fork_protected_acquisition() as acquisition:
        view_descriptor = open_real_directory(view, acquisition)
        if PlanConsumerDirectoryIdentity.capture(view_descriptor) != expected_view:
            raise PlanConsumerViewMutationConflict(
                "plan-consumer ledger binding view generation changed"
            )
        try:
            descriptor = acquisition.open_descriptor(
                lambda: os.open(
                    "shot.json",
                    _FILE_FLAGS,
                    dir_fd=view_descriptor,
                )
            )
        except FileNotFoundError:
            return PlanConsumerLedgerBinding(identity=None, sha256=None)
        except OSError as exc:
            raise PlanConsumerViewMutationConflict(
                f"plan-consumer ledger member is unreadable: {view / 'shot.json'}"
            ) from exc
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise PlanConsumerViewMutationConflict(
                    "plan-consumer ledger member must be a real regular file: "
                    f"{view / 'shot.json'}"
                )
            digest = hashlib.sha256()
            while chunk := os.read(descriptor, 1024 * 1024):
                digest.update(chunk)
            after = os.fstat(descriptor)
            if (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise PlanConsumerViewMutationConflict(
                    "plan-consumer ledger member changed while binding its bytes"
                )
            return PlanConsumerLedgerBinding(
                identity=(
                    before.st_dev,
                    before.st_ino,
                    stat.S_IFMT(before.st_mode),
                ),
                sha256=digest.hexdigest(),
            )
        finally:
            acquisition.retire(descriptor)


def capture_ledger_binding_at(
    view_descriptor: int,
    view: Path,
) -> PlanConsumerLedgerBinding:
    """Read the ledger through one already-held exact view directory."""

    with managed_fork_protected_acquisition() as acquisition:
        try:
            descriptor = acquisition.open_descriptor(
                lambda: os.open(
                    "shot.json",
                    _FILE_FLAGS,
                    dir_fd=view_descriptor,
                )
            )
        except FileNotFoundError:
            return PlanConsumerLedgerBinding(identity=None, sha256=None)
        except OSError as exc:
            raise PlanConsumerViewMutationConflict(
                f"plan-consumer ledger member is unreadable: {view / 'shot.json'}"
            ) from exc
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise PlanConsumerViewMutationConflict(
                    "plan-consumer ledger member must be a real regular file: "
                    f"{view / 'shot.json'}"
                )
            digest = hashlib.sha256()
            while chunk := os.read(descriptor, 1024 * 1024):
                digest.update(chunk)
            after = os.fstat(descriptor)
            if (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise PlanConsumerViewMutationConflict(
                    "plan-consumer ledger member changed while binding its bytes"
                )
            return PlanConsumerLedgerBinding(
                identity=(
                    before.st_dev,
                    before.st_ino,
                    stat.S_IFMT(before.st_mode),
                ),
                sha256=digest.hexdigest(),
            )
        finally:
            acquisition.retire(descriptor)


def same_named_directory(
    path: Path,
    expected: PlanConsumerDirectoryIdentity,
) -> None:
    with managed_fork_protected_acquisition() as acquisition:
        descriptor = open_real_directory(path, acquisition)
        observed = PlanConsumerDirectoryIdentity.capture(descriptor)
    if observed != expected:
        raise PlanConsumerViewMutationConflict(
            f"plan-consumer directory was replaced or rebound: {path}"
        )


def close_descriptors(
    token: object,
    guarded: tuple[GuardedDescriptor, ...],
) -> tuple[BaseException | None, bool]:
    """Neutralize exact active fds before any ambiguous raw-close outcome."""

    try:
        neutralize_active_descriptors(token, guarded)
    except RunOwnerForkGuardCleanupError as exc:
        if exc.retains_authority:
            failure = PlanConsumerViewMutationConflict(
                "plan-consumer mutation cleanup retained live descriptors; "
                "route to engineering"
            )
            failure.__cause__ = exc
            return failure, True
        return exc, False
    return None, False


__all__ = [
    "PlanConsumerDirectoryIdentity",
    "PlanConsumerLedgerBinding",
    "PlanConsumerViewMutationConflict",
    "absolute_path",
    "capture_ledger_binding",
    "capture_ledger_binding_at",
    "close_descriptors",
    "ledger_leaf_identity",
    "open_real_directory",
    "read_marker",
    "same_named_directory",
]
