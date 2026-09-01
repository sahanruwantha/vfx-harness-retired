"""Permanent per-layer locks for whole-file work-unit state mutations."""

from __future__ import annotations

import fcntl
import os
import secrets
import stat
from collections.abc import Callable
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from threading import local
from typing import ParamSpec, TypeVar

from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionConflict,
    authority_selection_lock,
)
from vfx_harness.orchestration.builder_execution_fence import (
    BuilderExecutionFenceError,
    stable_live_path_identity,
)

_P = ParamSpec("_P")
_T = TypeVar("_T")
STATE_DIR = "state/work-units"
STATE_STAGING_DIR = "state/work-unit-state-staging"
_THREAD_LOCKS = local()
_DIRECTORY_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_LOCK_OPEN_FLAGS = (
    os.O_RDWR
    | os.O_CREAT
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_STATE_READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_TEMP_OPEN_FLAGS = (
    os.O_RDWR
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)


@dataclass(frozen=True, slots=True)
class _NodeIdentity:
    device: int
    inode: int


@dataclass(slots=True)
class _HeldStatePath:
    state_path: Path
    shot: Path
    lock_path: Path
    lock_descriptor: int
    parent_descriptor: int
    lineage: tuple[_NodeIdentity, ...]
    exclusive: bool
    depth: int


def _node_identity(observed: os.stat_result) -> _NodeIdentity:
    return _NodeIdentity(observed.st_dev, observed.st_ino)


def unit_state_path(folder: str | Path, layer_id: str) -> Path:
    """Return the one durable state path owned by a layer."""

    safe_layer = str(layer_id).strip()
    if not safe_layer or "/" in safe_layer or "\\" in safe_layer or safe_layer in {".", ".."}:
        raise ValueError(f"invalid layer id for work-unit state: {layer_id!r}")
    return Path(folder) / STATE_DIR / f"layer_{safe_layer}.json"


def _open_real_directory(path: Path) -> int:
    current: int | None = None
    try:
        current = os.open(path.anchor, _DIRECTORY_OPEN_FLAGS)
        for part in path.parts[1:]:
            following = os.open(part, _DIRECTORY_OPEN_FLAGS, dir_fd=current)
            os.close(current)
            current = following
    except OSError as exc:
        if current is not None:
            os.close(current)
        raise ValueError(
            f"work-unit state root and ancestors must be real directories: {path}"
        ) from exc
    assert current is not None
    return current


def _open_managed_directory(
    shot: Path,
    relative: Path,
    *,
    create: bool,
) -> tuple[int, tuple[_NodeIdentity, ...]]:
    current = _open_real_directory(shot)
    lineage = [_node_identity(os.fstat(current))]
    try:
        for part in relative.parts:
            try:
                following = os.open(part, _DIRECTORY_OPEN_FLAGS, dir_fd=current)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, mode=0o700, dir_fd=current)
                following = os.open(part, _DIRECTORY_OPEN_FLAGS, dir_fd=current)
                os.fsync(following)
                os.fsync(current)
            os.close(current)
            current = following
            lineage.append(_node_identity(os.fstat(current)))
        return current, tuple(lineage)
    except BaseException:
        os.close(current)
        raise


def _open_state_parent(
    shot: Path,
    *,
    create: bool,
) -> tuple[int, tuple[_NodeIdentity, ...]]:
    return _open_managed_directory(shot, Path(STATE_DIR), create=create)


def _open_state_staging(
    shot: Path,
    *,
    create: bool,
) -> tuple[int, tuple[_NodeIdentity, ...]]:
    return _open_managed_directory(shot, Path(STATE_STAGING_DIR), create=create)


def state_namespace_entries(
    folder: str | Path,
) -> tuple[tuple[str, int], ...]:
    """Return descriptor-bound names and modes in the live state namespace.

    The caller interprets the names.  This storage boundary only guarantees that
    enumeration did not follow a symlink component or race an ancestor replacement.
    """

    shot = Path(os.path.abspath(Path(folder).expanduser()))
    parent: int | None = None
    current: int | None = None
    try:
        try:
            parent, lineage = _open_state_parent(shot, create=False)
        except FileNotFoundError:
            return ()
        names = sorted(os.listdir(parent))
        entries = tuple(
            (
                name,
                os.stat(name, dir_fd=parent, follow_symlinks=False).st_mode,
            )
            for name in names
        )
        current, current_lineage = _open_state_parent(shot, create=False)
        if (
            current_lineage != lineage
            or _node_identity(os.fstat(parent)) != lineage[-1]
            or _node_identity(os.fstat(current)) != lineage[-1]
        ):
            raise ValueError(
                f"work-unit state namespace changed during enumeration: "
                f"{shot / STATE_DIR}"
            )
        return entries
    except ValueError:
        raise
    except OSError as exc:
        raise ValueError(
            f"work-unit state namespace is unsafe or unreadable: {shot / STATE_DIR}"
        ) from exc
    finally:
        if current is not None:
            os.close(current)
        if parent is not None:
            os.close(parent)


def _state_location(state_path: Path) -> tuple[Path, Path, Path]:
    absolute = Path(os.path.abspath(state_path.expanduser()))
    expected_parent = Path(STATE_DIR)
    if tuple(absolute.parent.parts[-len(expected_parent.parts) :]) != expected_parent.parts:
        raise ValueError(
            f"work-unit state path is outside its exact {STATE_DIR} namespace: {absolute}"
        )
    shot = absolute.parents[len(expected_parent.parts)]
    return absolute, shot, absolute.with_name(f"{absolute.name}.lock")


def _held_locks() -> dict[str, _HeldStatePath]:
    held = getattr(_THREAD_LOCKS, "held", None)
    if held is None:
        held = {}
        _THREAD_LOCKS.held = held
    return held


def _require_state_path_current(binding: _HeldStatePath, phase: str) -> None:
    """Require the live namespace to resolve to the exact held parent and lock."""

    current_parent: int | None = None
    try:
        current_parent, current_lineage = _open_state_parent(
            binding.shot,
            create=False,
        )
        if current_lineage != binding.lineage:
            raise ValueError(
                f"work-unit state parent lineage changed {phase}: "
                f"{binding.state_path.parent}"
            )
        if _node_identity(os.fstat(binding.parent_descriptor)) != binding.lineage[-1]:
            raise ValueError(
                f"held work-unit state parent changed {phase}: "
                f"{binding.state_path.parent}"
            )
        current_lock = os.stat(
            binding.lock_path.name,
            dir_fd=current_parent,
            follow_symlinks=False,
        )
        held_lock = os.fstat(binding.lock_descriptor)
        if (
            not stat.S_ISREG(current_lock.st_mode)
            or _node_identity(current_lock) != _node_identity(held_lock)
        ):
            raise ValueError(
                f"work-unit state lock path changed {phase}: {binding.lock_path}"
            )
    except ValueError:
        raise
    except OSError as exc:
        raise ValueError(
            f"work-unit state parent lineage changed {phase}: "
            f"{binding.state_path.parent}"
        ) from exc
    finally:
        if current_parent is not None:
            os.close(current_parent)


def _require_staging_path_current(
    shot: Path,
    descriptor: int,
    lineage: tuple[_NodeIdentity, ...],
    phase: str,
) -> None:
    """Require the disposable staging namespace to retain its held lineage."""

    current: int | None = None
    try:
        current, current_lineage = _open_state_staging(shot, create=False)
        if (
            current_lineage != lineage
            or _node_identity(os.fstat(descriptor)) != lineage[-1]
            or _node_identity(os.fstat(current)) != lineage[-1]
        ):
            raise ValueError(
                f"work-unit state staging lineage changed {phase}: "
                f"{shot / STATE_STAGING_DIR}"
            )
    except ValueError:
        raise
    except OSError as exc:
        raise ValueError(
            f"work-unit state staging lineage changed {phase}: "
            f"{shot / STATE_STAGING_DIR}"
        ) from exc
    finally:
        if current is not None:
            os.close(current)


def _require_held_state_path(state_path: Path, *, exclusive: bool) -> _HeldStatePath:
    absolute, _shot, lock_path = _state_location(state_path)
    binding = _held_locks().get(str(lock_path))
    if binding is None:
        raise RuntimeError(f"work-unit state path is not locked: {absolute}")
    if exclusive and not binding.exclusive:
        raise RuntimeError(
            f"work-unit state write requires an exclusive lock: {absolute}"
        )
    return binding


@contextmanager
def _locked_state_path(state_path: Path, *, exclusive: bool = True):
    state_path, shot, lock_path = _state_location(state_path)
    lock_key = str(lock_path)
    held = _held_locks()
    existing = held.get(lock_key)
    if existing is not None:
        if exclusive and not existing.exclusive:
            raise RuntimeError(
                f"cannot upgrade a shared work-unit state lock to exclusive: {lock_path}"
            )
        existing.depth += 1
        try:
            _require_state_path_current(existing, "during nested lock entry")
            yield
        finally:
            try:
                _require_state_path_current(existing, "during nested lock exit")
            finally:
                existing.depth -= 1
        return

    parent: int | None = None
    try:
        parent, lineage = _open_state_parent(shot, create=True)
        descriptor = os.open(lock_path.name, _LOCK_OPEN_FLAGS, 0o600, dir_fd=parent)
    except OSError as exc:
        if parent is not None:
            os.close(parent)
        raise ValueError(
            f"work-unit state lock must be a real regular file: {lock_path}"
        ) from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(
                f"work-unit state lock must be a regular file: {lock_path}"
            )
        # The lock is intentionally exclusive for both read and write guards.  These
        # sections are metadata-only and short; serializing them lets the stable kernel
        # identity distinguish ordinary contention (which waits here) from an ancestor
        # rename/recreate split (which reaches the kernel identity and fails closed).
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            with stable_live_path_identity(lock_path):
                binding = _HeldStatePath(
                    state_path=state_path,
                    shot=shot,
                    lock_path=lock_path,
                    lock_descriptor=descriptor,
                    parent_descriptor=parent,
                    lineage=lineage,
                    exclusive=exclusive,
                    depth=1,
                )
                _require_state_path_current(binding, "during lock acquisition")
                held[lock_key] = binding
                try:
                    yield
                finally:
                    try:
                        _require_state_path_current(binding, "during lock exit")
                    finally:
                        held.pop(lock_key, None)
        except BuilderExecutionFenceError as exc:
            raise ValueError(str(exc)) from exc
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)
        if parent is not None:
            os.close(parent)


def read_state_file_bytes(state_path: Path) -> bytes | None:
    """Read one complete state document through its exact held parent descriptor."""

    with _locked_state_path(state_path, exclusive=False):
        binding = _require_held_state_path(state_path, exclusive=False)
        _require_state_path_current(binding, "before state read")
        descriptor: int | None = None
        try:
            try:
                descriptor = os.open(
                    binding.state_path.name,
                    _STATE_READ_FLAGS,
                    dir_fd=binding.parent_descriptor,
                )
            except FileNotFoundError:
                _require_state_path_current(binding, "after absent state read")
                return None
            except OSError as exc:
                raise ValueError(
                    f"work-unit state must be a real regular file: {binding.state_path}"
                ) from exc
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise ValueError(
                    f"work-unit state must be a real regular file: {binding.state_path}"
                )
            chunks: list[bytes] = []
            while chunk := os.read(descriptor, 1024 * 1024):
                chunks.append(chunk)
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
                raise ValueError(
                    f"work-unit state changed while it was read: {binding.state_path}"
                )
            _require_state_path_current(binding, "after state read")
            return b"".join(chunks)
        finally:
            if descriptor is not None:
                os.close(descriptor)


def _temporary_state_file(parent: int, target_name: str) -> tuple[int, str]:
    for _attempt in range(128):
        name = f".{target_name}.prepared.{secrets.token_hex(12)}"
        try:
            return os.open(name, _TEMP_OPEN_FLAGS, 0o600, dir_fd=parent), name
        except FileExistsError:
            continue
        except OSError as exc:
            raise ValueError("could not allocate a prepared work-unit state file") from exc
    raise ValueError("could not allocate a unique prepared work-unit state file")


def _state_file_identity(observed: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        observed.st_dev,
        observed.st_ino,
        observed.st_size,
        observed.st_mtime_ns,
        observed.st_ctime_ns,
    )


def _require_prepared_state_current(
    parent: int,
    state_path: Path,
    name: str,
    descriptor: int,
    expected: tuple[int, int, int, int, int],
) -> None:
    try:
        held = os.fstat(descriptor)
        named = os.stat(
            name,
            dir_fd=parent,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise AuthoritySelectionConflict(
            f"prepared work-unit state disappeared: {state_path}"
        ) from exc
    if (
        not stat.S_ISREG(held.st_mode)
        or not stat.S_ISREG(named.st_mode)
        or _state_file_identity(held) != expected
        or _state_file_identity(named) != expected
    ):
        raise AuthoritySelectionConflict(
            f"prepared work-unit state changed before publication: {state_path}"
        )


def _require_published_state_current(
    binding: _HeldStatePath,
    descriptor: int,
    expected: tuple[int, int, int, int, int],
) -> None:
    try:
        held = os.fstat(descriptor)
        named = os.stat(
            binding.state_path.name,
            dir_fd=binding.parent_descriptor,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise AuthoritySelectionConflict(
            f"published work-unit state disappeared: {binding.state_path}"
        ) from exc
    if (
        not stat.S_ISREG(held.st_mode)
        or not stat.S_ISREG(named.st_mode)
        or (held.st_dev, held.st_ino) != expected[:2]
        or (named.st_dev, named.st_ino) != expected[:2]
        or (held.st_size, held.st_mtime_ns) != (expected[2], expected[3])
        or _state_file_identity(named) != _state_file_identity(held)
    ):
        raise AuthoritySelectionConflict(
            f"published work-unit state changed during publication: {binding.state_path}"
        )


def _unlink_prepared_state_if_owned(
    parent: int,
    name: str,
    descriptor: int,
) -> bool:
    try:
        held = os.fstat(descriptor)
        named = os.stat(
            name,
            dir_fd=parent,
            follow_symlinks=False,
        )
    except OSError:
        return False
    if (
        stat.S_ISREG(held.st_mode)
        and stat.S_ISREG(named.st_mode)
        and (held.st_dev, held.st_ino) == (named.st_dev, named.st_ino)
    ):
        with suppress(FileNotFoundError):
            os.unlink(name, dir_fd=parent)
            return True
    return False


def write_state_file_bytes(state_path: Path, payload: bytes) -> None:
    """Durably replace state from a closed sibling staging namespace."""

    if not isinstance(payload, bytes):
        raise TypeError("work-unit state payload must be bytes")
    with _locked_state_path(state_path, exclusive=True):
        binding = _require_held_state_path(state_path, exclusive=True)
        _require_state_path_current(binding, "before state preparation")
        descriptor: int | None = None
        staging_descriptor: int | None = None
        staging_lineage: tuple[_NodeIdentity, ...] | None = None
        temporary_name: str | None = None
        try:
            staging_descriptor, staging_lineage = _open_state_staging(
                binding.shot,
                create=True,
            )
            if (
                os.fstat(staging_descriptor).st_dev
                != os.fstat(binding.parent_descriptor).st_dev
            ):
                raise AuthoritySelectionConflict(
                    "work-unit state staging and live namespaces must share one "
                    f"filesystem: {binding.shot}"
                )
            _require_staging_path_current(
                binding.shot,
                staging_descriptor,
                staging_lineage,
                "before state preparation",
            )
            descriptor, temporary_name = _temporary_state_file(
                staging_descriptor,
                binding.state_path.name,
            )
            remaining = memoryview(payload)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError("short write while preparing work-unit state")
                remaining = remaining[written:]
            os.fsync(descriptor)
            os.fsync(staging_descriptor)
            temporary_identity = _state_file_identity(os.fstat(descriptor))
            _require_prepared_state_current(
                staging_descriptor,
                binding.state_path,
                temporary_name,
                descriptor,
                temporary_identity,
            )
            _require_staging_path_current(
                binding.shot,
                staging_descriptor,
                staging_lineage,
                "before state publication",
            )
            _require_state_path_current(binding, "before state publication")
            os.replace(
                temporary_name,
                binding.state_path.name,
                src_dir_fd=staging_descriptor,
                dst_dir_fd=binding.parent_descriptor,
            )
            _require_published_state_current(
                binding,
                descriptor,
                temporary_identity,
            )
            temporary_name = None
            os.fsync(binding.parent_descriptor)
            os.fsync(staging_descriptor)
            _require_state_path_current(binding, "after state publication")
            _require_staging_path_current(
                binding.shot,
                staging_descriptor,
                staging_lineage,
                "after state publication",
            )
        except OSError as exc:
            raise AuthoritySelectionConflict(
                f"could not durably replace work-unit state: {binding.state_path}"
            ) from exc
        finally:
            if descriptor is not None:
                if temporary_name is not None and staging_descriptor is not None:
                    removed = _unlink_prepared_state_if_owned(
                        staging_descriptor,
                        temporary_name,
                        descriptor,
                    )
                    if removed:
                        os.fsync(staging_descriptor)
                os.close(descriptor)
            if staging_descriptor is not None:
                os.close(staging_descriptor)


def remove_state_file(state_path: Path) -> None:
    """Durably remove one state member through its exact held parent descriptor."""

    with _locked_state_path(state_path, exclusive=True):
        binding = _require_held_state_path(state_path, exclusive=True)
        _require_state_path_current(binding, "before state removal")
        try:
            observed = os.stat(
                binding.state_path.name,
                dir_fd=binding.parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            _require_state_path_current(binding, "after absent state removal")
            return
        except OSError as exc:
            raise AuthoritySelectionConflict(
                f"work-unit state is unreadable before removal: {binding.state_path}"
            ) from exc
        if not stat.S_ISREG(observed.st_mode):
            raise AuthoritySelectionConflict(
                f"work-unit state removal requires a regular file: {binding.state_path}"
            )
        try:
            os.unlink(binding.state_path.name, dir_fd=binding.parent_descriptor)
            os.fsync(binding.parent_descriptor)
        except OSError as exc:
            raise AuthoritySelectionConflict(
                f"could not durably remove work-unit state: {binding.state_path}"
            ) from exc
        _require_state_path_current(binding, "after state removal")


@contextmanager
def unit_state_lock(
    folder: str | Path,
    layer_id: str,
    *,
    exclusive: bool,
):
    """Hold a shared or exclusive lock for one layer's complete state document."""

    with _locked_state_path(
        unit_state_path(folder, layer_id),
        exclusive=exclusive,
    ):
        yield


def serialized_state_mutation(
    state_path: Callable[[str | Path, str], Path],
) -> Callable[[Callable[_P, _T]], Callable[_P, _T]]:
    """Decorate a mutation whose first arguments are folder and layer id."""

    def decorate(mutation: Callable[_P, _T]) -> Callable[_P, _T]:
        @wraps(mutation)
        def guarded(folder, layer_id, *args, **kwargs):
            with (
                authority_selection_lock(folder, exclusive=False),
                _locked_state_path(state_path(folder, layer_id), exclusive=True),
            ):
                return mutation(folder, layer_id, *args, **kwargs)

        return guarded

    return decorate
