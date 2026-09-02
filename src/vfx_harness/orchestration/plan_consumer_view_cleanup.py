"""Identity-bound, non-destructive retirement of consumer-view directories."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import stat
from dataclasses import dataclass

from vfx_harness.orchestration.plan_consumer_view_descriptors import (
    PlanConsumerDirectoryIdentity,
    PlanConsumerViewMutationConflict,
)


@dataclass(frozen=True, slots=True)
class _OwnedDirectoryRetirement:
    retired_name: str
    identity: PlanConsumerDirectoryIdentity


class _NoReplaceDestinationOccupied(PlanConsumerViewMutationConflict):
    """The exact source was preserved because the destination was occupied."""


_RENAME_NOREPLACE = 1
_LIBC = ctypes.CDLL(None, use_errno=True)
_RENAMEAT2 = getattr(_LIBC, "renameat2", None)
if _RENAMEAT2 is not None:
    _RENAMEAT2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    _RENAMEAT2.restype = ctypes.c_int


def _rename_child_noreplace(
    parent_descriptor: int,
    source_name: str,
    destination_name: str,
) -> None:
    if _RENAMEAT2 is None:
        raise PlanConsumerViewMutationConflict(
            "plan-consumer retirement requires atomic renameat2 RENAME_NOREPLACE; "
            "this platform cannot safely retire the directory"
        )
    result = _RENAMEAT2(
        parent_descriptor,
        os.fsencode(source_name),
        parent_descriptor,
        os.fsencode(destination_name),
        _RENAME_NOREPLACE,
    )
    if result == 0:
        return
    observed_errno = ctypes.get_errno()
    if observed_errno in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP}:
        raise PlanConsumerViewMutationConflict(
            "plan-consumer retirement filesystem does not support atomic "
            "RENAME_NOREPLACE"
        )
    if observed_errno == errno.EEXIST:
        raise _NoReplaceDestinationOccupied(
            "plan-consumer retirement destination became occupied; the foreign "
            "directory and owned source were preserved"
        )
    raise PlanConsumerViewMutationConflict(
        "plan-consumer retirement no-replace rename failed; both names must be "
        "inspected before retry"
    ) from OSError(observed_errno, os.strerror(observed_errno))


def _optional_identity(
    parent_descriptor: int,
    name: str,
) -> PlanConsumerDirectoryIdentity | None:
    try:
        observed = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PlanConsumerViewMutationConflict(
            f"owned plan-consumer cleanup name became unreadable: {name}"
        ) from exc
    if not stat.S_ISDIR(observed.st_mode):
        raise PlanConsumerViewMutationConflict(
            f"owned plan-consumer cleanup name must be a real directory: {name}"
        )
    return PlanConsumerDirectoryIdentity(
        observed.st_dev,
        observed.st_ino,
        stat.S_IFMT(observed.st_mode),
    )


def _retired_name(
    source_name: str,
    expected: PlanConsumerDirectoryIdentity,
) -> str:
    digest = hashlib.sha256(
        f"{source_name}\0{expected.device}\0{expected.inode}\0{expected.file_type}".encode()
    ).hexdigest()[:24]
    return f".plan-consumer-view.retired-{digest}"


def move_owned_directory_noreplace(
    parent_descriptor: int,
    source_name: str,
    destination_name: str,
    expected: PlanConsumerDirectoryIdentity,
    *,
    where: str,
) -> None:
    """Move one named exact inode without replacing a destination.

    A path exchange immediately before ``renameat2`` can move a substitute, but
    it cannot destroy either destination generation.  Post-rename identity
    proof therefore decides whether the move committed; a substitute move is
    preserved at the destination and fails closed.
    """

    for name in (source_name, destination_name):
        if not name or name in {".", ".."} or "/" in name:
            raise PlanConsumerViewMutationConflict(
                f"{where} requires exact child names"
            )
    source = _optional_identity(parent_descriptor, source_name)
    if source != expected:
        raise PlanConsumerViewMutationConflict(
            f"{where} source is absent or was substituted; no move was authorized"
        )
    if _optional_identity(parent_descriptor, destination_name) is not None:
        raise _NoReplaceDestinationOccupied(
            f"{where} destination is occupied; both generations were preserved"
        )
    _rename_child_noreplace(
        parent_descriptor,
        source_name,
        destination_name,
    )
    moved = _optional_identity(parent_descriptor, destination_name)
    remaining_source = _optional_identity(parent_descriptor, source_name)
    if moved != expected or remaining_source is not None:
        raise PlanConsumerViewMutationConflict(
            f"{where} could not prove that the exact source generation moved; "
            "all observed generations were preserved"
        )
    os.fsync(parent_descriptor)


def _retire_owned_directory(
    parent_descriptor: int,
    source_name: str,
    expected: PlanConsumerDirectoryIdentity,
) -> _OwnedDirectoryRetirement:
    """Move only the exact owned inode to its deterministic cleanup name.

    The deterministic retained tombstone is the recovery receipt: an interruption
    after rename can retry from that name without touching a later source
    substitute. A raced substitute may be moved to the retired name, but it is
    never deleted.
    """

    if not source_name or source_name in {".", ".."} or "/" in source_name:
        raise PlanConsumerViewMutationConflict(
            "owned plan-consumer cleanup requires one exact child name"
        )
    retired_name = _retired_name(source_name, expected)
    retired = _optional_identity(parent_descriptor, retired_name)
    source = _optional_identity(parent_descriptor, source_name)
    if retired is not None and retired != expected:
        raise PlanConsumerViewMutationConflict(
            "plan-consumer retirement tombstone contains a foreign substitute; "
            "it was preserved"
        )
    if retired == expected:
        if source is not None:
            raise PlanConsumerViewMutationConflict(
                "plan-consumer retirement found a foreign source beside its exact "
                "tombstone; both were preserved"
            )
        return _OwnedDirectoryRetirement(
            retired_name=retired_name,
            identity=expected,
        )
    if source is None:
        raise PlanConsumerViewMutationConflict(
            "plan-consumer retirement cannot prove either its exact source or its "
            "required retained tombstone"
        )
    if source != expected:
        raise PlanConsumerViewMutationConflict(
            "plan-consumer cleanup source was substituted; the substitute was preserved"
        )

    move_owned_directory_noreplace(
        parent_descriptor,
        source_name,
        retired_name,
        expected,
        where="plan-consumer directory retirement",
    )
    return _OwnedDirectoryRetirement(
        retired_name=retired_name,
        identity=expected,
    )


def retire_owned_directory(
    parent_descriptor: int,
    source_name: str,
    expected: PlanConsumerDirectoryIdentity,
) -> None:
    """Atomically retire one exact generation without destructive path I/O.

    POSIX has no conditional rmdir-by-inode.  The empty top directory therefore
    cannot be removed safely under a concurrently exchangeable name.  The whole
    retired tree deliberately remains disposable run scratch until whole-run
    disposal; this boundary issues no rmtree, rmdir, or unlink operation.
    """

    _retire_owned_directory(
        parent_descriptor,
        source_name,
        expected,
    )
    os.fsync(parent_descriptor)


__all__: list[str] = []
