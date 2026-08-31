"""Serialized, revision-checked writes for one generated work-unit plan.

The planner writes a plan through an MCP tool and then publishes its authority sidecar in
two phases.  A failed gate or selected-authority CAS must restore the predecessor pair only
when both files still contain the exact bytes produced by that attempt.  The permanent
per-target lock prevents cooperating planner sessions from interleaving, while the byte CAS
keeps rollback from destroying a writer that did not hold that lock.
"""

from __future__ import annotations

import fcntl
import os
import secrets
import stat
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

import anyio


class WorkUnitPlanTransactionConflict(ValueError):
    """One plan transaction no longer owns the files it would roll back."""


@dataclass(frozen=True, slots=True)
class _FileRevision:
    exists: bool
    content: bytes | None

    @classmethod
    def absent(cls) -> _FileRevision:
        return cls(exists=False, content=None)


@dataclass(slots=True)
class _LockHandle:
    descriptor: int
    path: Path


class WorkUnitPlanTransaction:
    """Exact predecessor and owned revisions for one plan/sidecar pair."""

    def __init__(
        self,
        *,
        target: Path,
        authority: Path,
        predecessor: tuple[_FileRevision, _FileRevision],
        lock_path: Path,
    ) -> None:
        self.target = target
        self.authority = authority
        self.lock_path = lock_path
        self._predecessor = predecessor
        self._owned: tuple[_FileRevision, _FileRevision] | None = None
        self._active = True

    def claim_current(self) -> None:
        """Record the exact pair produced by this attempt at a write boundary."""

        self._require_active()
        self._owned = self._read_pair()

    def require_owned_current(self) -> None:
        """Fail unless both files still match the latest claimed write boundary."""

        self._require_active()
        if self._owned is None:
            raise WorkUnitPlanTransactionConflict(
                "work-unit plan transaction has not claimed its output bytes"
            )
        observed = self._read_pair()
        if observed != self._owned:
            changed = [
                path.name
                for path, expected, actual in zip(
                    (self.target, self.authority),
                    self._owned,
                    observed,
                    strict=True,
                )
                if actual != expected
            ]
            raise WorkUnitPlanTransactionConflict(
                "work-unit plan transaction lost ownership because a newer writer changed: "
                + ", ".join(changed)
            )

    def rollback(self) -> None:
        """Restore the predecessor only if this attempt still owns the whole pair.

        Comparison happens for both paths before either is changed.  This prevents a
        partial rollback when a newer writer has replaced only one member so far.
        """

        self._require_active()
        if self._owned is None:
            raise WorkUnitPlanTransactionConflict(
                "work-unit plan rollback refused because the attempt never claimed "
                "its exact output bytes"
            )
        try:
            self.require_owned_current()
        except WorkUnitPlanTransactionConflict as exc:
            raise WorkUnitPlanTransactionConflict(
                "work-unit plan rollback refused because a newer writer owns the output"
            ) from exc
        for path, predecessor in zip(
            (self.target, self.authority),
            self._predecessor,
            strict=True,
        ):
            _restore_revision(path, predecessor)
        self._owned = self._predecessor

    def _read_pair(self) -> tuple[_FileRevision, _FileRevision]:
        return (_read_revision(self.target), _read_revision(self.authority))

    def _require_active(self) -> None:
        if not self._active:
            raise WorkUnitPlanTransactionConflict(
                "work-unit plan transaction is no longer active"
            )

    def _close(self) -> None:
        self._active = False


def _absolute(path: str | Path) -> Path:
    return Path(os.path.abspath(Path(path).expanduser()))


def _read_revision(path: Path) -> _FileRevision:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return _FileRevision.absent()
    except OSError as exc:
        raise WorkUnitPlanTransactionConflict(
            f"work-unit plan transaction cannot read a real file: {path}"
        ) from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise WorkUnitPlanTransactionConflict(
                f"work-unit plan transaction requires a regular file: {path}"
            )
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        return _FileRevision(exists=True, content=b"".join(chunks))
    finally:
        os.close(descriptor)


def _fsync_parent(path: Path) -> None:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path.parent, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _replace_bytes(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.rollback-{secrets.token_hex(12)}")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, flags, 0o600)
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, path)
        _fsync_parent(path)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        with suppress(FileNotFoundError):
            temporary.unlink()


def _restore_revision(path: Path, revision: _FileRevision) -> None:
    if revision.exists:
        assert revision.content is not None
        _replace_bytes(path, revision.content)
        return
    try:
        path.unlink()
    except FileNotFoundError:
        return
    _fsync_parent(path)


def _open_lock(target: Path, authority: Path) -> _LockHandle:
    target.parent.mkdir(parents=True, exist_ok=True)
    if authority.parent != target.parent:
        raise WorkUnitPlanTransactionConflict(
            "work-unit plan and authority sidecar must share one parent directory"
        )
    lock_path = target.with_name(f".{target.name}.transaction.lock")
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise WorkUnitPlanTransactionConflict(
            f"work-unit plan transaction lock must be a real regular file: {lock_path}"
        ) from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise WorkUnitPlanTransactionConflict(
                f"work-unit plan transaction lock must be a regular file: {lock_path}"
        )
        os.fsync(descriptor)
        _fsync_parent(lock_path)
        return _LockHandle(descriptor=descriptor, path=lock_path)
    except BaseException:
        os.close(descriptor)
        raise


def _release(handle: _LockHandle) -> None:
    try:
        fcntl.flock(handle.descriptor, fcntl.LOCK_UN)
    finally:
        os.close(handle.descriptor)


@asynccontextmanager
async def work_unit_plan_transaction(
    target: str | Path,
    authority: str | Path,
) -> AsyncIterator[WorkUnitPlanTransaction]:
    """Serialize one target without blocking the caller's async event loop."""

    target_path = _absolute(target)
    authority_path = _absolute(authority)
    handle = _open_lock(target_path, authority_path)
    locked = False
    transaction: WorkUnitPlanTransaction | None = None
    try:
        while True:
            try:
                fcntl.flock(handle.descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                # Blocking flock here would deadlock two planner tasks sharing one
                # event loop.  Yield between bounded non-blocking attempts instead.
                await anyio.sleep(0.01)
                continue
            except OSError as exc:
                raise WorkUnitPlanTransactionConflict(
                    f"could not acquire work-unit plan transaction lock: {handle.path}"
                ) from exc
            locked = True
            break
        predecessor = (_read_revision(target_path), _read_revision(authority_path))
        transaction = WorkUnitPlanTransaction(
            target=target_path,
            authority=authority_path,
            predecessor=predecessor,
            lock_path=handle.path,
        )
        yield transaction
    finally:
        if transaction is not None:
            transaction._close()
        if locked:
            _release(handle)
        else:
            os.close(handle.descriptor)
