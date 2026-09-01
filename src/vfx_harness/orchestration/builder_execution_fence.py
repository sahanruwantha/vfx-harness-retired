"""One live, shot-wide builder execution fence.

The durable work-unit ledger records authority; it is not a process mutex.  This
module supplies the separate live-execution boundary used while one claimed unit
attempt can spend model budget or mutate a Blender candidate.  A System V semaphore
keyed by the normalized shot pathname remains stable if any filesystem path is renamed
and recreated.  ``SEM_UNDO`` makes abnormal process exit release it.  Shot-root and
child-file locks provide inspectable defense in depth.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import os
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
from pathlib import Path

BUILDER_EXECUTION_FENCE = Path("state/builder-execution/fence.lock")

_IPC_CREAT = 0o1000
_IPC_EXCL = 0o2000
_IPC_NOWAIT = 0o4000
_IPC_RMID = 0
_SEM_UNDO = 0o10000
_SETVAL = 16
_SEM_COUNT = 20
_INIT_SEMAPHORE = 0
_MARKER_SEMAPHORE = 1
_FINGERPRINT_START = 2
_FINGERPRINT_COUNT = 17
_BUILDER_SEMAPHORE = 19
_HARNESS_MARKER = 0x6A3F
_MAX_KEY_PROBES = 32

_DIRECTORY_OPEN_FLAGS = (
    os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
)
_FENCE_OPEN_FLAGS = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)


class BuilderExecutionFenceError(RuntimeError):
    """The shot-wide builder fence is active or its storage is unsafe."""


class BuilderExecutionFenceActive(BuilderExecutionFenceError):
    """Another live builder attempt already owns the shot-wide fence."""


_LEASE_CONSTRUCTOR_KEY = object()
_LIVE_IDENTITY_CLAIM_KEY = object()
_LIVE_IDENTITY_PROCESS_TOKEN = object()


def _invalidate_forked_live_identity_claims() -> None:
    """Make every inherited claim capability inert in the forked child."""

    global _LIVE_IDENTITY_PROCESS_TOKEN

    _LIVE_IDENTITY_PROCESS_TOKEN = object()


os.register_at_fork(after_in_child=_invalidate_forked_live_identity_claims)


class BuilderExecutionFenceLease:
    """Live, shot-bound capability yielded only by the owning fence context."""

    __slots__ = (
        "_descriptor",
        "_live_identity",
        "_mutex",
        "_operations",
        "_owner_active",
        "_parent",
        "_released",
        "_shot",
        "_shot_descriptor",
        "_shot_identity",
        "path",
    )

    def __init__(
        self,
        shot: Path,
        path: Path,
        shot_identity: tuple[int, int],
        *,
        shot_descriptor: int,
        parent: int,
        descriptor: int,
        live_identity: LivePathIdentityClaim,
        _key: object,
    ) -> None:
        if _key is not _LEASE_CONSTRUCTOR_KEY:
            raise BuilderExecutionFenceError("builder execution fence leases are issued only by the live fence context")
        self._shot = shot
        self.path = path
        self._shot_identity = shot_identity
        self._shot_descriptor = shot_descriptor
        self._parent = parent
        self._descriptor = descriptor
        self._live_identity = live_identity
        self._mutex = threading.Lock()
        self._operations = 0
        self._owner_active = True
        self._released = False

    def __fspath__(self) -> str:
        return os.fspath(self.path)

    def __str__(self) -> str:
        return str(self.path)

    def stat(self) -> os.stat_result:
        return self.path.stat()

    def _require_live(self, shot_folder: str | Path) -> None:
        with self._mutex:
            live = not self._released and (self._owner_active or self._operations > 0)
        if not live:
            raise BuilderExecutionFenceError("paid builder execution requires a live shot-wide fence lease")
        expected = Path(os.path.abspath(Path(shot_folder).expanduser()))
        if expected != self._shot:
            raise BuilderExecutionFenceError(f"builder execution fence lease belongs to {self._shot}, not {expected}")
        observed_shot, descriptor = _open_shot_root(expected)
        try:
            observed = os.fstat(descriptor)
            identity = (observed.st_dev, observed.st_ino)
        finally:
            os.close(descriptor)
        if observed_shot != self._shot or identity != self._shot_identity:
            raise BuilderExecutionFenceError("builder execution shot root changed after its live fence was acquired")

    @contextmanager
    def operation(self, shot_folder: str | Path) -> Iterator[None]:
        """Retain the OS fence for one complete paid/mutating operation."""

        with self._mutex:
            if not self._owner_active or self._released:
                raise BuilderExecutionFenceError("paid builder execution requires its still-open fence context")
            self._operations += 1
        try:
            self._require_live(shot_folder)
            yield
        finally:
            with self._mutex:
                self._operations -= 1
            self._release_if_unowned()

    def _close_owner(self) -> None:
        with self._mutex:
            self._owner_active = False
        self._release_if_unowned()

    def _release_if_unowned(self) -> None:
        with self._mutex:
            if self._released or self._owner_active or self._operations:
                return
            self._released = True
            descriptor = self._descriptor
            parent = self._parent
            shot_descriptor = self._shot_descriptor
            live_identity = self._live_identity
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
        os.close(parent)
        fcntl.flock(shot_descriptor, fcntl.LOCK_UN)
        os.close(shot_descriptor)
        live_identity.release()


def require_builder_execution_lease(
    lease: BuilderExecutionFenceLease,
    shot_folder: str | Path,
) -> None:
    """Refuse paid/mutating execution without this shot's live fence capability."""

    if not isinstance(lease, BuilderExecutionFenceLease):
        raise BuilderExecutionFenceError("paid builder execution requires a live shot-wide fence lease")
    lease._require_live(shot_folder)


def builder_execution_fenced(function):
    """Require and retain a live shot fence around an async paid entry point."""

    @wraps(function)
    async def guarded(*args, **kwargs):
        lease = kwargs.pop("fence_lease", None)
        shot = args[0] if args else kwargs.get("shot")
        folder = getattr(shot, "folder", None)
        require_builder_execution_lease(lease, folder)
        with lease.operation(folder):
            return await function(*args, **kwargs)

    return guarded


class _SemBuf(ctypes.Structure):
    _fields_ = [
        ("sem_num", ctypes.c_ushort),
        ("sem_op", ctypes.c_short),
        ("sem_flg", ctypes.c_short),
    ]


@dataclass(frozen=True, slots=True)
class _KernelSemaphoreLease:
    semid: int

    def release(self) -> None:
        # Destroy the identity while still owning its builder semaphore.  This leaves
        # no persistent kernel objects after a normal exit.  A crash instead relies on
        # SEM_UNDO; the next successful owner removes that recovered set.
        if _LIBC.semctl(self.semid, 0, _IPC_RMID) != 0:
            observed_errno = ctypes.get_errno()
            raise OSError(observed_errno, os.strerror(observed_errno))


class LivePathIdentityClaim:
    """Armed, exact-process owner for one crash-safe SysV identity claim.

    Construct this capability before entering the ``try`` that calls
    :meth:`acquire`.  The acquisition stores cleanup authority here before the
    builder semaphore can remain claimed.  ``acquire`` deliberately returns
    ``None``: no resource ownership crosses a CALL-to-assignment boundary.

    Cleanup is process-bound, not thread-bound.  A foreign thread in the same
    process may unwind an owner, while a forked child can neither acquire nor
    remove its parent's semaphore set.
    """

    __slots__ = (
        "_cleanup_armed",
        "_fence_path",
        "_mutex",
        "_owner_pid",
        "_owner_process_token",
        "_released",
        "_semid",
        "_shot",
    )

    def __init__(self, shot: Path, fence_path: Path, *, _key: object) -> None:
        if _key is not _LIVE_IDENTITY_CLAIM_KEY:
            raise BuilderExecutionFenceError("live path identity claims must be created by arm_live_path_identity")
        self._shot = shot
        self._fence_path = fence_path
        self._owner_pid = os.getpid()
        self._owner_process_token = _LIVE_IDENTITY_PROCESS_TOKEN
        self._mutex = threading.RLock()
        self._semid: int | None = None
        self._cleanup_armed = False
        self._released = False

    @property
    def belongs_to_current_process(self) -> bool:
        """Whether this capability belongs to the exact creating process."""

        return os.getpid() == self._owner_pid and _LIVE_IDENTITY_PROCESS_TOKEN is self._owner_process_token

    def acquire(self) -> None:
        """Acquire the identity without returning a detached lease object."""

        if not self.belongs_to_current_process:
            raise BuilderExecutionFenceError("forked child cannot acquire its parent's live path identity claim")
        with self._mutex:
            if self._released:
                raise BuilderExecutionFenceError("released live path identity claim cannot be reacquired")
            if self._cleanup_armed:
                raise BuilderExecutionFenceError("live path identity claim is already acquired")
            try:
                _claim_live_identity_into(self, self._shot, self._fence_path)
                if not self._cleanup_armed:
                    raise BuilderExecutionFenceError("live path identity acquisition returned without arming cleanup")
            except BaseException:
                # The owner object existed before acquisition began.  It is therefore
                # safe to clean an armed claim even when interruption lands immediately
                # after the successful semop and before its Python call returns.
                self._release_owned()
                raise

    def release(self) -> None:
        """Release in the creator process; inherited child copies are inert."""

        if not self.belongs_to_current_process:
            return
        with self._mutex:
            self._release_owned()

    def _arm_cleanup(self, semid: int) -> None:
        if self._cleanup_armed or self._released:
            raise BuilderExecutionFenceError("live path identity cleanup was armed more than once")
        self._semid = semid
        self._cleanup_armed = True

    def _disarm_failed_semop(self, semid: int) -> None:
        if not self._cleanup_armed or self._semid != semid:
            raise BuilderExecutionFenceError("failed live path identity semop does not match its armed cleanup")
        self._cleanup_armed = False
        self._semid = None

    def _release_owned(self) -> None:
        semid = self._semid
        cleanup_armed = self._cleanup_armed
        self._semid = None
        self._cleanup_armed = False
        self._released = True
        if not cleanup_armed:
            return
        if semid is None:
            raise BuilderExecutionFenceError("armed live path identity cleanup is missing its semaphore id")
        try:
            _KernelSemaphoreLease(semid=semid).release()
        except OSError as exc:
            if exc.errno not in {errno.EIDRM, errno.EINVAL}:
                raise


def arm_live_path_identity(
    shot: str | Path,
    fence_path: str | Path,
) -> LivePathIdentityClaim:
    """Create a resource-free transaction before attempting a SysV claim."""

    return LivePathIdentityClaim(
        Path(os.path.abspath(Path(shot).expanduser())),
        Path(os.path.abspath(Path(fence_path).expanduser())),
        _key=_LIVE_IDENTITY_CLAIM_KEY,
    )


_LIBC = ctypes.CDLL(None, use_errno=True)
_LIBC.semget.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]
_LIBC.semget.restype = ctypes.c_int
_LIBC.semop.argtypes = [ctypes.c_int, ctypes.POINTER(_SemBuf), ctypes.c_size_t]
_LIBC.semop.restype = ctypes.c_int
_LIBC.semctl.restype = ctypes.c_int


def _semop(semid: int, operations: tuple[tuple[int, int, int], ...]) -> None:
    rows = (_SemBuf * len(operations))(*(_SemBuf(number, operation, flags) for number, operation, flags in operations))
    while _LIBC.semop(semid, rows, len(rows)) != 0:
        observed_errno = ctypes.get_errno()
        if observed_errno == errno.EINTR:
            continue
        raise OSError(observed_errno, os.strerror(observed_errno))


def _sem_value(semid: int, number: int) -> int:
    value = _LIBC.semctl(semid, number, 12)
    if value < 0:
        observed_errno = ctypes.get_errno()
        raise OSError(observed_errno, os.strerror(observed_errno))
    return int(value)


def _set_sem_value(semid: int, number: int, value: int) -> None:
    if _LIBC.semctl(semid, number, _SETVAL, int(value)) != 0:
        observed_errno = ctypes.get_errno()
        raise OSError(observed_errno, os.strerror(observed_errno))


def _identity_digest(shot: Path, probe: int) -> bytes:
    return hashlib.sha256(
        b"vfx-harness-builder-fence\0" + os.fsencode(str(shot)) + b"\0" + str(probe).encode("ascii")
    ).digest()


def _fingerprint(digest: bytes) -> tuple[int, ...]:
    value = int.from_bytes(digest, "big")
    return tuple((value >> (index * 15)) & 0x7FFF for index in range(_FINGERPRINT_COUNT))


def _semaphore_key(digest: bytes) -> int:
    return (int.from_bytes(digest[:4], "big") & 0x7FFFFFFF) or 1


def _open_semaphore_set(key: int) -> int:
    semid = _LIBC.semget(key, _SEM_COUNT, _IPC_CREAT | _IPC_EXCL | 0o600)
    if semid >= 0:
        return int(semid)
    observed_errno = ctypes.get_errno()
    if observed_errno != errno.EEXIST:
        raise OSError(observed_errno, os.strerror(observed_errno))
    semid = _LIBC.semget(key, _SEM_COUNT, 0o600)
    if semid < 0:
        observed_errno = ctypes.get_errno()
        raise OSError(observed_errno, os.strerror(observed_errno))
    return int(semid)


def _claim_live_identity_into(
    claim: LivePathIdentityClaim,
    shot: Path,
    fence_path: Path,
) -> None:
    """Claim a crash-safe kernel identity into an already-addressable owner."""

    for probe in range(_MAX_KEY_PROBES):
        digest = _identity_digest(shot, probe)
        expected_fingerprint = _fingerprint(digest)
        try:
            semid = _open_semaphore_set(_semaphore_key(digest))
            # A wait-for-zero plus increment is one atomic initialization mutex.
            # SEM_UNDO releases it if a creator dies during initialization.
            _semop(
                semid,
                (
                    (_INIT_SEMAPHORE, 0, _IPC_NOWAIT),
                    (_INIT_SEMAPHORE, 1, _IPC_NOWAIT | _SEM_UNDO),
                ),
            )
        except OSError as exc:
            if exc.errno == errno.EAGAIN:
                raise BuilderExecutionFenceActive(_active_message(fence_path)) from exc
            if exc.errno in {errno.EACCES, errno.EINVAL}:
                continue
            raise BuilderExecutionFenceError(
                f"could not acquire builder execution kernel identity for {shot}: {exc}"
            ) from exc

        init_claimed = False
        try:
            init_claimed = True
            marker = _sem_value(semid, _MARKER_SEMAPHORE)
            if marker == 0:
                for index, part in enumerate(expected_fingerprint):
                    _set_sem_value(semid, _FINGERPRINT_START + index, part)
                _set_sem_value(semid, _BUILDER_SEMAPHORE, 1)
                _set_sem_value(semid, _MARKER_SEMAPHORE, _HARNESS_MARKER)
            elif marker != _HARNESS_MARKER:
                continue

            observed_fingerprint = tuple(
                _sem_value(semid, _FINGERPRINT_START + index) for index in range(_FINGERPRINT_COUNT)
            )
            if observed_fingerprint != expected_fingerprint:
                continue
            builder_value = _sem_value(semid, _BUILDER_SEMAPHORE)
            if builder_value == 0:
                raise BuilderExecutionFenceActive(_active_message(fence_path))
            if builder_value != 1:
                raise BuilderExecutionFenceError(
                    "builder execution kernel identity has an invalid semaphore value; "
                    "route to engineering before paid execution"
                )

            # Arm the already-addressable transaction while the initialization
            # mutex excludes every cooperating claimant and while the builder
            # semaphore is proven unclaimed.  If interruption lands before or
            # immediately after semop returns, transaction cleanup may safely
            # remove this inactive-or-owned set.
            claim._arm_cleanup(semid)
            try:
                _semop(
                    semid,
                    ((_BUILDER_SEMAPHORE, -1, _IPC_NOWAIT | _SEM_UNDO),),
                )
            except OSError as exc:
                # SysV semop is atomic: an error applies none of the requested
                # operations, so this transaction does not own the set.
                claim._disarm_failed_semop(semid)
                if exc.errno == errno.EAGAIN:
                    raise BuilderExecutionFenceActive(_active_message(fence_path)) from exc
                raise
            return
        except BuilderExecutionFenceActive:
            raise
        except OSError as exc:
            raise BuilderExecutionFenceError(
                f"could not acquire builder execution kernel identity for {shot}: {exc}"
            ) from exc
        finally:
            if init_claimed:
                try:
                    _semop(semid, ((_INIT_SEMAPHORE, -1, _SEM_UNDO),))
                except OSError as exc:
                    # An armed acquisition will remove the entire set during
                    # transaction unwind.  EIDRM/EINVAL means that cleanup has
                    # already made the set unreachable.
                    if not claim._cleanup_armed or exc.errno not in {
                        errno.EIDRM,
                        errno.EINVAL,
                    }:
                        raise

    raise BuilderExecutionFenceError(
        "builder execution kernel identity exhausted collision probes; route to engineering before paid execution"
    )


def _claim_live_identity(shot: Path, fence_path: Path) -> _KernelSemaphoreLease:
    """Legacy test/migration bridge; production must use ``arm_live_path_identity``.

    This detached-return shape cannot close the CALL-to-assignment interruption
    window.  It remains temporarily for migration-only callers and must not be
    introduced into production code.
    """

    claim = arm_live_path_identity(shot, fence_path)
    claim.acquire()
    with claim._mutex:
        semid = claim._semid
        if semid is None or not claim._cleanup_armed:
            claim.release()
            raise BuilderExecutionFenceError("legacy live path identity bridge did not receive an armed claim")
        claim._semid = None
        claim._cleanup_armed = False
        claim._released = True
    return _KernelSemaphoreLease(semid=semid)


def _open_shot_root(shot_folder: str | Path) -> tuple[Path, int]:
    shot = Path(os.path.abspath(Path(shot_folder).expanduser()))
    current: int | None = None
    try:
        current = os.open(shot.anchor, _DIRECTORY_OPEN_FLAGS)
        for part in shot.parts[1:]:
            following = os.open(part, _DIRECTORY_OPEN_FLAGS, dir_fd=current)
            os.close(current)
            current = following
    except OSError as exc:
        if current is not None:
            os.close(current)
        raise BuilderExecutionFenceError(
            f"builder execution shot root and every ancestor must be an existing real directory component: {shot}"
        ) from exc
    if current is None or not stat.S_ISDIR(os.fstat(current).st_mode):
        if current is not None:
            os.close(current)
        raise BuilderExecutionFenceError(f"builder execution shot root must be a real directory: {shot}")
    return shot, current


def _open_fence_parent(shot_descriptor: int) -> int:
    current = os.dup(shot_descriptor)
    try:
        for part in BUILDER_EXECUTION_FENCE.parts[:-1]:
            try:
                following = os.open(part, _DIRECTORY_OPEN_FLAGS, dir_fd=current)
            except FileNotFoundError:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=current)
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise BuilderExecutionFenceError(
                        f"cannot create builder execution fence directory: {part}"
                    ) from exc
                try:
                    following = os.open(part, _DIRECTORY_OPEN_FLAGS, dir_fd=current)
                except OSError as exc:
                    raise BuilderExecutionFenceError(
                        f"builder execution fence component must be a real directory: {part}"
                    ) from exc
            except OSError as exc:
                raise BuilderExecutionFenceError(
                    f"builder execution fence component must be a real directory: {part}"
                ) from exc
            os.close(current)
            current = following
        return current
    except BaseException:
        os.close(current)
        raise


def _active_message(fence_path: Path) -> str:
    return (
        f"builder execution fence is already active: {fence_path}; "
        "reviewed recovery must prove the owning builder process has exited before retry; "
        "do not delete or replace the fence file"
    )


@contextmanager
def stable_live_path_identity(identity_path: str | Path) -> Iterator[None]:
    """Hold one crash-released kernel identity for an intended filesystem path.

    Callers first acquire their descriptor-backed file lock.  Therefore ordinary
    contention waits there; seeing this identity already owned means the filesystem
    path was rebound to a different lock inode and must fail closed.
    """

    identity = Path(os.path.abspath(Path(identity_path).expanduser()))
    claim = arm_live_path_identity(identity, identity)
    try:
        try:
            claim.acquire()
        except BuilderExecutionFenceActive as exc:
            raise BuilderExecutionFenceError(
                "live lock identity is already held through another filesystem inode; "
                f"the lock path may have been replaced: {identity}"
            ) from exc
        yield
    finally:
        claim.release()


@contextmanager
def builder_execution_fence(
    shot_folder: str | Path,
) -> Iterator[BuilderExecutionFenceLease]:
    """Acquire the one non-blocking live builder fence for ``shot_folder``.

    Contention fails immediately.  The caller must keep this context open for the
    complete claimed attempt; normal close, exception unwinding, or process exit
    releases the kernel lock.  The permanent file remains in place so cooperating
    attempts always contend on one inode.
    """

    shot, shot_descriptor = _open_shot_root(shot_folder)
    fence_path = shot / BUILDER_EXECUTION_FENCE
    live_identity = arm_live_path_identity(shot, fence_path)
    root_locked = False
    parent: int | None = None
    descriptor: int | None = None
    locked = False
    lease: BuilderExecutionFenceLease | None = None
    try:
        live_identity.acquire()
        try:
            # The kernel semaphore is the stable live identity.  Root and permanent
            # child locks preserve inspectability and defense in depth.
            fcntl.flock(shot_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BuilderExecutionFenceActive(_active_message(fence_path)) from exc
        except OSError as exc:
            raise BuilderExecutionFenceError(f"could not acquire builder execution shot-root fence: {shot}") from exc
        root_locked = True
        parent = _open_fence_parent(shot_descriptor)
        try:
            descriptor = os.open(
                BUILDER_EXECUTION_FENCE.name,
                _FENCE_OPEN_FLAGS,
                0o600,
                dir_fd=parent,
            )
        except OSError as exc:
            raise BuilderExecutionFenceError(
                f"builder execution fence must be a real regular file: {fence_path}"
            ) from exc
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise BuilderExecutionFenceError(f"builder execution fence must be a real regular file: {fence_path}")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BuilderExecutionFenceActive(_active_message(fence_path)) from exc
        except OSError as exc:
            raise BuilderExecutionFenceError(f"could not acquire builder execution fence: {fence_path}") from exc
        locked = True
        root_identity = os.fstat(shot_descriptor)
        lease = BuilderExecutionFenceLease(
            shot,
            fence_path,
            (root_identity.st_dev, root_identity.st_ino),
            shot_descriptor=shot_descriptor,
            parent=parent,
            descriptor=descriptor,
            live_identity=live_identity,
            _key=_LEASE_CONSTRUCTOR_KEY,
        )
        yield lease
    finally:
        if lease is not None:
            lease._close_owner()
        elif descriptor is not None:
            if locked:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
        if lease is None:
            if parent is not None:
                os.close(parent)
            if root_locked:
                fcntl.flock(shot_descriptor, fcntl.LOCK_UN)
            os.close(shot_descriptor)
            live_identity.release()
