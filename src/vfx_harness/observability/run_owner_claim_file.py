"""Create-only inode allocation and durable publication for a run-owner claim."""

from __future__ import annotations

import json
import os
import secrets
import stat
from contextlib import suppress
from dataclasses import dataclass

from vfx_harness.domain.run_owner_claims import RUN_OWNER_CLAIM_LOCATOR, RunOwnerClaim

_CLAIM_NAME = RUN_OWNER_CLAIM_LOCATOR.rsplit("/", maxsplit=1)[-1]
_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)


class RunOwnerClaimFileError(RuntimeError):
    """The create-only claim inode could not be allocated or published safely."""


class RunOwnerClaimFileExists(RunOwnerClaimFileError):
    """The canonical create-only claim name already exists."""


def _identity(value: os.stat_result) -> tuple[int, int]:
    return value.st_dev, value.st_ino


def _require_regular(value: os.stat_result, where: str) -> None:
    if not stat.S_ISREG(value.st_mode):
        raise RunOwnerClaimFileError(f"{where} must be one real regular file")


def _write_all(descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:  # pragma: no cover - POSIX write either advances or raises
            raise OSError("owner claim write made no progress")
        remaining = remaining[written:]


@dataclass(slots=True)
class PreparedRunOwnerClaimFile:
    """One allocated inode whose identity is serialized before bytes are written."""

    temporary_name: str
    descriptor: int | None
    device: int
    inode: int
    canonical_link_created: bool = False
    staging_link_removed: bool = False

    @property
    def identity(self) -> tuple[int, int]:
        return self.device, self.inode


def allocate_run_owner_claim_file(owner_descriptor: int) -> PreparedRunOwnerClaimFile:
    """Allocate and verify the staging inode before the claim contract is minted."""

    temporary = f".claim.tmp.{os.getpid()}.{secrets.token_hex(8)}"
    descriptor: int | None = None
    allocated = False
    try:
        descriptor = os.open(temporary, _CREATE_FLAGS, 0o600, dir_fd=owner_descriptor)
        os.set_inheritable(descriptor, False)
        if os.get_inheritable(descriptor):  # pragma: no cover - kernel contract guard
            raise RunOwnerClaimFileError("owner claim staging descriptor remained inheritable")
        observed = os.fstat(descriptor)
        _require_regular(observed, "owner claim staging file")
        if observed.st_nlink != 1:
            raise RunOwnerClaimFileError("owner claim staging inode must initially have exactly one name")
        allocated = True
        return PreparedRunOwnerClaimFile(
            temporary_name=temporary,
            descriptor=descriptor,
            device=observed.st_dev,
            inode=observed.st_ino,
        )
    except RunOwnerClaimFileError:
        raise
    except OSError as exc:
        raise RunOwnerClaimFileError("could not allocate the create-only owner claim inode") from exc
    finally:
        if descriptor is not None and not allocated:
            with suppress(OSError):
                os.close(descriptor)
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=owner_descriptor)


def _named_identity(owner_descriptor: int, name: str, where: str) -> os.stat_result:
    try:
        observed = os.stat(name, dir_fd=owner_descriptor, follow_symlinks=False)
    except OSError as exc:
        raise RunOwnerClaimFileError(f"{where} is absent or unreadable") from exc
    _require_regular(observed, where)
    return observed


def _require_publication_pair(
    owner_descriptor: int,
    prepared: PreparedRunOwnerClaimFile,
) -> None:
    staged = _named_identity(owner_descriptor, prepared.temporary_name, "owner claim staging file")
    published = _named_identity(owner_descriptor, _CLAIM_NAME, "published run owner claim")
    if _identity(staged) != prepared.identity or _identity(published) != prepared.identity:
        raise RunOwnerClaimFileError("published run owner claim does not name its allocated staging inode")
    if staged.st_nlink != 2 or published.st_nlink != 2:
        raise RunOwnerClaimFileError(
            "owner claim publication inode must have exactly the staged and canonical names before commit"
        )


def _require_published_single_name(
    owner_descriptor: int,
    prepared: PreparedRunOwnerClaimFile,
) -> None:
    published = _named_identity(owner_descriptor, _CLAIM_NAME, "published run owner claim")
    if _identity(published) != prepared.identity:
        raise RunOwnerClaimFileError("published run owner claim changed its allocated inode")
    if published.st_nlink != 1:
        raise RunOwnerClaimFileError("published run owner claim must have exactly one filesystem name")


def discard_run_owner_claim_file(
    owner_descriptor: int,
    prepared: PreparedRunOwnerClaimFile,
) -> None:
    """Close and retire an unpublished staging name without erasing crash evidence."""

    if prepared.descriptor is not None:
        with suppress(OSError):
            os.close(prepared.descriptor)
        prepared.descriptor = None
    if not prepared.canonical_link_created or prepared.staging_link_removed:
        with suppress(FileNotFoundError):
            os.unlink(prepared.temporary_name, dir_fd=owner_descriptor)


def publish_run_owner_claim_file(
    owner_descriptor: int,
    prepared: PreparedRunOwnerClaimFile,
    claim: RunOwnerClaim,
) -> None:
    """Write the inode-bound claim, then durably commit its canonical sole name."""

    if not isinstance(claim, RunOwnerClaim):
        raise RunOwnerClaimFileError("owner claim publication requires the exact typed claim")
    if prepared.descriptor is None:
        raise RunOwnerClaimFileError("owner claim staging descriptor is already closed")
    if (claim.claim_device, claim.claim_inode) != prepared.identity:
        raise RunOwnerClaimFileError("owner claim serialized inode identity does not match its allocated staging inode")
    payload = (json.dumps(claim.as_dict(), indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        held = os.fstat(prepared.descriptor)
        if _identity(held) != prepared.identity or held.st_nlink != 1:
            raise RunOwnerClaimFileError("owner claim staging descriptor changed before serialization")
        _write_all(prepared.descriptor, payload)
        os.fsync(prepared.descriptor)
        try:
            os.link(
                prepared.temporary_name,
                _CLAIM_NAME,
                src_dir_fd=owner_descriptor,
                dst_dir_fd=owner_descriptor,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise RunOwnerClaimFileExists("run owner claim is create-only and already exists") from exc
        prepared.canonical_link_created = True
        _require_publication_pair(owner_descriptor, prepared)
        os.fsync(owner_descriptor)
        os.unlink(prepared.temporary_name, dir_fd=owner_descriptor)
        prepared.staging_link_removed = True
        _require_published_single_name(owner_descriptor, prepared)
        os.fsync(owner_descriptor)
        _require_published_single_name(owner_descriptor, prepared)
    except RunOwnerClaimFileError:
        raise
    except OSError as exc:
        raise RunOwnerClaimFileError("could not publish the create-only owner claim") from exc
