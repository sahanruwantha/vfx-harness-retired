"""Canonical namespace and inode operations for the run-owner fence."""

from __future__ import annotations

import fcntl
import os
import stat
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from vfx_harness.domain.run_ids import require_run_id
from vfx_harness.domain.run_owner_claims import (
    RUN_OWNER_CLAIM_LOCATOR,
    RUN_OWNER_FENCE_LOCATOR,
)
from vfx_harness.observability.run_owner_fork_guard import (
    ForkProtectedAcquisition,
    GuardedDescriptor,
)

RUN_OWNER_DIRECTORY = Path("owner")
RUN_OWNER_FENCE = Path(RUN_OWNER_FENCE_LOCATOR)
RUN_OWNER_CLAIM = Path(RUN_OWNER_CLAIM_LOCATOR)

_DIRECTORY_FLAGS = (
    os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
)
_FENCE_FLAGS = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)


class RunOwnerFenceError(RuntimeError):
    """The exact run owner fence or its immutable claim is unsafe."""


class RunOwnerFenceActive(RunOwnerFenceError):
    """The exact fence is still held by a live root invocation."""


class RunOwnerFenceSubstituted(RunOwnerFenceError):
    """A canonical owner namespace no longer names its claim-bound inode."""


class RunOwnerClaimExists(RunOwnerFenceError):
    """A create-only owner claim already exists for this run."""


def identity(value: os.stat_result) -> tuple[int, int]:
    return value.st_dev, value.st_ino


def require_regular(value: os.stat_result, *, where: str) -> None:
    if not stat.S_ISREG(value.st_mode):
        raise RunOwnerFenceError(f"{where} must be one real regular file")


def require_single_name(value: os.stat_result, *, where: str) -> None:
    if value.st_nlink != 1:
        raise RunOwnerFenceError(f"{where} must have exactly one filesystem name")


def _set_non_inheritable(descriptor: int, *, where: str) -> None:
    os.set_inheritable(descriptor, False)
    if os.get_inheritable(descriptor):  # pragma: no cover - kernel contract guard
        raise RunOwnerFenceError(f"{where} descriptor remained inheritable")


@dataclass(frozen=True, slots=True)
class OpenedRunNamespace:
    """Held canonical `<shot>/runs/<run>` namespace and exact identities."""

    root: Path
    shot_descriptor: int
    runs_descriptor: int
    root_descriptor: int
    shot_identity: tuple[int, int]
    runs_identity: tuple[int, int]
    root_identity: tuple[int, int]

    @property
    def descriptors(self) -> tuple[int, int, int]:
        return self.shot_descriptor, self.runs_descriptor, self.root_descriptor


def _close_namespace_descriptors(
    descriptors: tuple[int | None, ...],
    *,
    acquisition: ForkProtectedAcquisition | None = None,
    tracked: set[int] | None = None,
) -> None:
    first_error: BaseException | None = None
    for descriptor in reversed(descriptors):
        if descriptor is None:
            continue
        try:
            if (
                acquisition is not None
                and acquisition.belongs_to_current_process
                and (tracked is None or descriptor in tracked)
            ):
                acquisition.retire(descriptor)
            else:
                os.close(descriptor)
        except BaseException as exc:
            if first_error is None:
                first_error = exc
    if first_error is not None:
        raise first_error


def close_run_namespace(namespace: OpenedRunNamespace) -> None:
    _close_namespace_descriptors(namespace.descriptors)


def open_run_namespace(
    run_root: str | Path,
    run_id: str,
    *,
    acquisition: ForkProtectedAcquisition | None = None,
) -> OpenedRunNamespace:
    require_run_id(run_id, "run owner fence run_id")
    root = Path(os.path.abspath(Path(run_root).expanduser()))
    if root.name != run_id or root.parent.name != "runs":
        raise RunOwnerFenceError(
            f"run owner fence requires the exact <shot>/runs/<run-id> layout; found {root} for run {run_id!r}"
        )

    shot = root.parent.parent
    shot_descriptor: int | None = None
    runs_descriptor: int | None = None
    root_descriptor: int | None = None
    tracked: set[int] = set()

    def open_shot_root() -> int:
        descriptor = os.open(shot.anchor, _DIRECTORY_FLAGS)
        try:
            for component in shot.parts[1:]:
                following = os.open(
                    component,
                    _DIRECTORY_FLAGS,
                    dir_fd=descriptor,
                )
                try:
                    os.close(descriptor)
                except BaseException:
                    os.close(following)
                    raise
                descriptor = following
            return descriptor
        except BaseException:
            with suppress(OSError):
                os.close(descriptor)
            raise

    try:
        if acquisition is None:
            shot_descriptor = open_shot_root()
        else:
            shot_descriptor = acquisition.open_descriptor(open_shot_root)
            tracked.add(shot_descriptor)
        _set_non_inheritable(shot_descriptor, where="shot root")
        if acquisition is None:
            runs_descriptor = os.open(
                "runs",
                _DIRECTORY_FLAGS,
                dir_fd=shot_descriptor,
            )
        else:
            runs_descriptor = acquisition.open_descriptor(
                lambda: os.open(
                    "runs",
                    _DIRECTORY_FLAGS,
                    dir_fd=shot_descriptor,
                )
            )
            tracked.add(runs_descriptor)
        _set_non_inheritable(runs_descriptor, where="runs directory")
        if acquisition is None:
            root_descriptor = os.open(
                run_id,
                _DIRECTORY_FLAGS,
                dir_fd=runs_descriptor,
            )
        else:
            root_descriptor = acquisition.open_descriptor(
                lambda: os.open(
                    run_id,
                    _DIRECTORY_FLAGS,
                    dir_fd=runs_descriptor,
                )
            )
            tracked.add(root_descriptor)
        _set_non_inheritable(root_descriptor, where="run root")
    except OSError as exc:
        _close_namespace_descriptors(
            (shot_descriptor, runs_descriptor, root_descriptor),
            acquisition=acquisition,
            tracked=tracked,
        )
        raise RunOwnerFenceError(
            f"run root and every ancestor must be existing real directory components: {root}"
        ) from exc
    except BaseException:
        _close_namespace_descriptors(
            (shot_descriptor, runs_descriptor, root_descriptor),
            acquisition=acquisition,
            tracked=tracked,
        )
        raise

    try:
        if shot_descriptor is None or runs_descriptor is None or root_descriptor is None:
            raise RunOwnerFenceError("run namespace opening did not produce its complete descriptor chain")
        shot_identity = identity(os.fstat(shot_descriptor))
        runs_identity = identity(os.fstat(runs_descriptor))
        root_identity = identity(os.fstat(root_descriptor))
        named_runs = os.stat("runs", dir_fd=shot_descriptor, follow_symlinks=False)
        named_root = os.stat(run_id, dir_fd=runs_descriptor, follow_symlinks=False)
        if identity(named_runs) != runs_identity or identity(named_root) != root_identity:
            raise RunOwnerFenceSubstituted("run namespace changed while its canonical chain was opened")
        return OpenedRunNamespace(
            root=root,
            shot_descriptor=shot_descriptor,
            runs_descriptor=runs_descriptor,
            root_descriptor=root_descriptor,
            shot_identity=shot_identity,
            runs_identity=runs_identity,
            root_identity=root_identity,
        )
    except BaseException:
        _close_namespace_descriptors(
            (shot_descriptor, runs_descriptor, root_descriptor),
            acquisition=acquisition,
            tracked=tracked,
        )
        raise


def open_run_root(
    run_root: str | Path,
    run_id: str,
) -> tuple[Path, int, tuple[int, int]]:
    namespace = open_run_namespace(run_root, run_id)
    try:
        _close_namespace_descriptors((namespace.shot_descriptor, namespace.runs_descriptor))
    except BaseException:
        with suppress(OSError):
            os.close(namespace.root_descriptor)
        raise
    return namespace.root, namespace.root_descriptor, namespace.root_identity


def verify_named_run_namespace(
    root: Path,
    run_id: str,
    shot_descriptor: int,
    runs_descriptor: int,
    root_descriptor: int,
    *,
    expected_shot: tuple[int, int],
    expected_runs: tuple[int, int],
    expected_root: tuple[int, int],
) -> None:
    held = (
        identity(os.fstat(shot_descriptor)),
        identity(os.fstat(runs_descriptor)),
        identity(os.fstat(root_descriptor)),
    )
    expected = (expected_shot, expected_runs, expected_root)
    if held != expected:
        raise RunOwnerFenceSubstituted(f"held shot/runs/run namespace changed from {expected} to {held}")
    try:
        named_runs = identity(os.stat("runs", dir_fd=shot_descriptor, follow_symlinks=False))
        named_root = identity(os.stat(run_id, dir_fd=runs_descriptor, follow_symlinks=False))
    except OSError as exc:
        raise RunOwnerFenceSubstituted("canonical shot/runs/run namespace links are absent or unreadable") from exc
    if named_runs != expected_runs or named_root != expected_root:
        raise RunOwnerFenceSubstituted("held shot/runs/run namespace links changed after acquisition")
    current = open_run_namespace(root, run_id)
    try:
        current_identity = (
            current.shot_identity,
            current.runs_identity,
            current.root_identity,
        )
        if current.root != root or current_identity != expected:
            raise RunOwnerFenceSubstituted(
                "canonical shot/runs/run namespace no longer resolves to its acquired inodes; "
                f"expected {expected}, found {current_identity}"
            )
    finally:
        close_run_namespace(current)


def verify_named_owner_directory(
    root_descriptor: int,
    owner_descriptor: int,
    *,
    expected: tuple[int, int] | None = None,
) -> tuple[int, int]:
    held = os.fstat(owner_descriptor)
    if not stat.S_ISDIR(held.st_mode):  # pragma: no cover - O_DIRECTORY enforces
        raise RunOwnerFenceSubstituted("held run owner directory is not a real directory")
    try:
        named = os.stat(RUN_OWNER_DIRECTORY.name, dir_fd=root_descriptor, follow_symlinks=False)
    except OSError as exc:
        raise RunOwnerFenceSubstituted("named run owner directory is absent or unreadable") from exc
    if not stat.S_ISDIR(named.st_mode):
        raise RunOwnerFenceSubstituted("named run owner directory is not a real directory")
    held_identity = identity(held)
    named_identity = identity(named)
    if held_identity != named_identity:
        raise RunOwnerFenceSubstituted("named run owner directory no longer resolves to the held inode")
    if expected is not None and held_identity != expected:
        raise RunOwnerFenceSubstituted(
            "named run owner directory does not match its acquired inode; "
            f"expected device/inode {expected}, found {held_identity}"
        )
    return held_identity


def open_owner_directory(root_descriptor: int, *, create: bool) -> int:
    created = False
    try:
        descriptor = os.open(RUN_OWNER_DIRECTORY.name, _DIRECTORY_FLAGS, dir_fd=root_descriptor)
    except FileNotFoundError:
        if not create:
            raise RunOwnerFenceError("run has no owner directory") from None
        try:
            os.mkdir(RUN_OWNER_DIRECTORY.name, mode=0o700, dir_fd=root_descriptor)
            created = True
        except FileExistsError:
            pass
        except OSError as exc:
            raise RunOwnerFenceError("could not create the run owner directory") from exc
        try:
            descriptor = os.open(RUN_OWNER_DIRECTORY.name, _DIRECTORY_FLAGS, dir_fd=root_descriptor)
        except OSError as exc:
            raise RunOwnerFenceError("run owner directory must be one real directory") from exc
    except OSError as exc:
        raise RunOwnerFenceError("run owner directory must be one real directory") from exc
    try:
        _set_non_inheritable(descriptor, where="run owner directory")
        owner_identity = verify_named_owner_directory(root_descriptor, descriptor)
        if created:
            try:
                os.fsync(root_descriptor)
            except OSError as exc:
                raise RunOwnerFenceError("could not durably publish the run owner directory") from exc
            verify_named_owner_directory(root_descriptor, descriptor, expected=owner_identity)
        return descriptor
    except BaseException:
        with suppress(OSError):
            os.close(descriptor)
        raise


def claim_exists(owner_descriptor: int) -> bool:
    try:
        os.stat(RUN_OWNER_CLAIM.name, dir_fd=owner_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise RunOwnerFenceError("could not inspect the create-only owner claim") from exc
    return True


def open_fence(
    owner_descriptor: int,
    *,
    create: bool,
    acquisition: ForkProtectedAcquisition | None = None,
) -> tuple[int, bool]:
    created = False

    def open_exact_fence() -> int:
        nonlocal created
        if create:
            try:
                descriptor = os.open(
                    RUN_OWNER_FENCE.name,
                    _FENCE_FLAGS | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=owner_descriptor,
                )
                created = True
                return descriptor
            except FileExistsError:
                return os.open(
                    RUN_OWNER_FENCE.name,
                    _FENCE_FLAGS,
                    dir_fd=owner_descriptor,
                )
        return os.open(
            RUN_OWNER_FENCE.name,
            _FENCE_FLAGS,
            dir_fd=owner_descriptor,
        )

    try:
        if acquisition is None:
            descriptor = open_exact_fence()
        else:
            descriptor = acquisition.open_descriptor(open_exact_fence)
    except OSError as exc:
        raise RunOwnerFenceError(f"run owner fence must be a real regular file: {RUN_OWNER_FENCE.as_posix()}") from exc
    try:
        observed = os.fstat(descriptor)
        require_regular(observed, where="run owner fence")
        require_single_name(observed, where="run owner fence")
        _set_non_inheritable(descriptor, where="run owner fence")
        return descriptor, created
    except BaseException:
        if acquisition is None:
            os.close(descriptor)
        else:
            acquisition.retire(descriptor)
        raise


def acquire_exclusive(descriptor: int, *, path: Path) -> None:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise RunOwnerFenceActive(f"run owner fence is held by a live root invocation: {path}") from exc
    except OSError as exc:
        raise RunOwnerFenceError(f"could not acquire run owner fence: {path}") from exc


def verify_named_fence(
    owner_descriptor: int,
    fence_descriptor: int,
    *,
    expected: tuple[int, int] | None = None,
) -> tuple[int, int]:
    held = os.fstat(fence_descriptor)
    require_regular(held, where="held run owner fence")
    require_single_name(held, where="held run owner fence")
    try:
        named = os.stat(RUN_OWNER_FENCE.name, dir_fd=owner_descriptor, follow_symlinks=False)
    except OSError as exc:
        raise RunOwnerFenceSubstituted("named run owner fence is absent or unreadable") from exc
    require_regular(named, where="named run owner fence")
    require_single_name(named, where="named run owner fence")
    held_identity = identity(held)
    named_identity = identity(named)
    if held_identity != named_identity:
        raise RunOwnerFenceSubstituted("named run owner fence no longer resolves to the held inode")
    if expected is not None and held_identity != expected:
        raise RunOwnerFenceSubstituted(
            "named run owner fence does not match the inode bound by its claim; "
            f"expected device/inode {expected}, found {held_identity}"
        )
    return held_identity


def durably_publish_created_fence(
    owner_descriptor: int,
    fence_descriptor: int,
    *,
    expected: tuple[int, int],
) -> None:
    """Commit a new fence name before any claim may refer to its inode."""

    try:
        os.fsync(fence_descriptor)
    except OSError as exc:
        raise RunOwnerFenceError("could not durably initialize the new run owner fence") from exc
    try:
        os.fsync(owner_descriptor)
    except OSError as exc:
        raise RunOwnerFenceError("could not durably publish the new run owner fence name") from exc
    verify_named_fence(owner_descriptor, fence_descriptor, expected=expected)


def close_acquisition(
    *,
    fence_descriptor: int | None,
    owner_descriptor: int | None,
    root_descriptor: int | None,
    runs_descriptor: int | None,
    shot_descriptor: int | None,
    locked: bool,
) -> None:
    """Release every descriptor even if an explicit unlock or close reports failure."""

    try:
        if fence_descriptor is not None:
            try:
                if locked:
                    fcntl.flock(fence_descriptor, fcntl.LOCK_UN)
            finally:
                os.close(fence_descriptor)
    finally:
        try:
            if owner_descriptor is not None:
                os.close(owner_descriptor)
        finally:
            _close_namespace_descriptors((shot_descriptor, runs_descriptor, root_descriptor))


def close_guarded_acquisition(
    descriptors: tuple[GuardedDescriptor, ...],
) -> None:
    """Close only numeric slots still joined to this exact lease identity."""

    errors: list[BaseException] = []
    fence = descriptors[-1]
    if fence.is_current():
        try:
            close_acquisition(
                fence_descriptor=fence.descriptor,
                owner_descriptor=None,
                root_descriptor=None,
                runs_descriptor=None,
                shot_descriptor=None,
                locked=True,
            )
        except BaseException as exc:
            errors.append(exc)
    else:
        errors.append(
            RunOwnerFenceSubstituted(
                "run owner fence descriptor changed identity before release"
            )
        )
    for guarded in reversed(descriptors[:-1]):
        if not guarded.is_current():
            errors.append(
                RunOwnerFenceSubstituted(
                    "run owner directory descriptor changed identity before release"
                )
            )
            continue
        try:
            os.close(guarded.descriptor)
        except BaseException as exc:
            errors.append(exc)
    if not errors:
        return
    primary = errors[0]
    if isinstance(primary, RunOwnerFenceError):
        for diagnostic in errors[1:]:
            primary.add_note(
                f"cleanup diagnostic: {type(diagnostic).__name__}: {diagnostic}"
            )
        raise primary
    failure = RunOwnerFenceError(
        "run owner fence cleanup reported an operating-system failure"
    )
    for diagnostic in errors:
        failure.add_note(
            f"cleanup diagnostic: {type(diagnostic).__name__}: {diagnostic}"
        )
    raise failure from primary
