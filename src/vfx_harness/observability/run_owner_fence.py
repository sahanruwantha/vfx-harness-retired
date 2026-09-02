"""Crash-released ownership fence for one exact root run invocation.

The claim records process identifiers for audit, but liveness comes only from the
held POSIX ``flock`` on the exact source-verified inode.  A later reconciler may
acquire that same inode after process death; it must never infer owner loss from a
PID, timestamp, or quiet transcript.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from vfx_harness.domain.run_lifecycle_primitives import chronological
from vfx_harness.domain.run_owner_claims import (
    RUN_OWNER_FENCE_IMPLEMENTATION,
    RunOwnerClaim,
)
from vfx_harness.observability.run_owner_claim_file import (
    PreparedRunOwnerClaimFile,
    RunOwnerClaimFileError,
    RunOwnerClaimFileExists,
    allocate_run_owner_claim_file,
    discard_run_owner_claim_file,
    publish_run_owner_claim_file,
)
from vfx_harness.observability.run_owner_fence_files import (
    RUN_OWNER_CLAIM,
    RUN_OWNER_FENCE,
    RunOwnerClaimExists,
    RunOwnerFenceActive,
    RunOwnerFenceCleanupError,
    RunOwnerFenceError,
    RunOwnerFenceSubstituted,
)
from vfx_harness.observability.run_owner_fence_files import (
    acquire_exclusive as _acquire_exclusive,
)
from vfx_harness.observability.run_owner_fence_files import (
    claim_exists as _claim_exists,
)
from vfx_harness.observability.run_owner_fence_files import (
    close_acquisition as _close_acquisition,
)
from vfx_harness.observability.run_owner_fence_files import (
    close_guarded_acquisition as _close_guarded_acquisition,
)
from vfx_harness.observability.run_owner_fence_files import (
    durably_publish_created_fence as _durably_publish_created_fence,
)
from vfx_harness.observability.run_owner_fence_files import (
    identity as _identity,
)
from vfx_harness.observability.run_owner_fence_files import (
    open_fence as _open_fence,
)
from vfx_harness.observability.run_owner_fence_files import (
    open_owner_directory as _open_owner_directory,
)
from vfx_harness.observability.run_owner_fence_files import (
    open_run_namespace as _open_run_namespace,
)
from vfx_harness.observability.run_owner_fence_files import (
    require_regular as _require_regular,
)
from vfx_harness.observability.run_owner_fence_files import (
    require_single_name as _require_single_name,
)
from vfx_harness.observability.run_owner_fence_files import (
    verify_named_fence as _verify_named_fence,
)
from vfx_harness.observability.run_owner_fence_files import (
    verify_named_owner_directory as _verify_named_owner_directory,
)
from vfx_harness.observability.run_owner_fence_files import (
    verify_named_run_namespace as _verify_named_run_namespace,
)
from vfx_harness.observability.run_owner_fork_guard import (
    ForkProtectedAcquisition,
    GuardedDescriptor,
    RunOwnerForkGuardError,
    managed_fork_protected_acquisition,
)
from vfx_harness.observability.run_owner_manifest import (
    RUN_MANIFEST_LOCATOR,
    RunOwnerManifestError,
    VerifiedRunOwnerManifest,
    parse_run_owner_manifest,
    strict_json_object,
)

RUN_MANIFEST = Path(RUN_MANIFEST_LOCATOR)

__all__ = [
    "RUN_OWNER_CLAIM",
    "RUN_OWNER_FENCE",
    "RunOwnerClaimExists",
    "RunOwnerFenceActive",
    "RunOwnerFenceCleanupError",
    "RunOwnerFenceError",
    "RunOwnerFenceLease",
    "RunOwnerFenceSubstituted",
    "acquire_run_owner_fence",
    "acquire_run_reconciler_fence",
    "read_run_owner_claim",
]

_READ_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
_MAX_RECORD_BYTES = 4 * 1024 * 1024
_LEASE_KEY = object()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _process_start_token() -> str:
    """Return a stable process-instance audit token; it grants no liveness authority."""

    try:
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_bytes().strip()
        process_stat = Path("/proc/self/stat").read_bytes().strip()
        closing_parenthesis = process_stat.rfind(b")")
        if closing_parenthesis < 0:
            raise ValueError("/proc/self/stat has no command terminator")
        fields_after_command = process_stat[closing_parenthesis + 2 :].split()
        # Field 22 is process start time; the tail begins with field 3.
        start_ticks = fields_after_command[19]
        material = b"\0".join((boot_id, str(os.getpid()).encode("ascii"), start_ticks))
    except (OSError, ValueError, IndexError):
        # Platforms without procfs still get a process-local audit join.  The held
        # inode fence, never this token, remains the liveness proof.
        material = secrets.token_bytes(32)
    return hashlib.sha256(material).hexdigest()


def _retire_claim_staging(
    acquisition: ForkProtectedAcquisition,
    owner_descriptor: int,
    prepared: PreparedRunOwnerClaimFile,
) -> None:
    if not acquisition.belongs_to_current_process:
        return
    discard_run_owner_claim_file(owner_descriptor, prepared, acquisition)


def _read_named_regular(
    parent_descriptor: int,
    name: str,
    *,
    where: str,
    require_single_name: bool = False,
) -> tuple[bytes, tuple[int, int]]:
    try:
        descriptor = os.open(name, _READ_FLAGS, dir_fd=parent_descriptor)
    except OSError as exc:
        raise RunOwnerFenceError(f"{where} must be a readable real file") from exc
    try:
        opened = os.fstat(descriptor)
        _require_regular(opened, where=where)
        if require_single_name:
            _require_single_name(opened, where=where)
        chunks: list[bytes] = []
        length = 0
        while True:
            chunk = os.read(descriptor, min(65536, _MAX_RECORD_BYTES + 1 - length))
            if not chunk:
                break
            chunks.append(chunk)
            length += len(chunk)
            if length > _MAX_RECORD_BYTES:
                raise RunOwnerFenceError(f"{where} exceeds the {_MAX_RECORD_BYTES}-byte safety limit")
        named = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        _require_regular(named, where=where)
        if require_single_name:
            _require_single_name(named, where=where)
        if _identity(opened) != _identity(named):
            raise RunOwnerFenceError(f"{where} changed inode while it was read")
        return b"".join(chunks), _identity(opened)
    except OSError as exc:
        raise RunOwnerFenceError(f"could not source-verify {where}") from exc
    finally:
        os.close(descriptor)


def _read_manifest(
    root_descriptor: int,
    *,
    run_id: str,
) -> VerifiedRunOwnerManifest:
    payload, _manifest_identity = _read_named_regular(
        root_descriptor,
        RUN_MANIFEST.name,
        where="run manifest",
    )
    try:
        return parse_run_owner_manifest(payload, run_id=run_id)
    except RunOwnerManifestError as exc:
        raise RunOwnerFenceError(str(exc)) from exc


def _read_claim_from_descriptors(
    shot_descriptor: int,
    runs_descriptor: int,
    root_descriptor: int,
    owner_descriptor: int,
    *,
    run_id: str,
    expected_digest: str | None = None,
) -> RunOwnerClaim:
    payload, claim_identity = _read_named_regular(
        owner_descriptor,
        RUN_OWNER_CLAIM.name,
        where="run owner claim",
        require_single_name=True,
    )
    try:
        raw = strict_json_object(payload, where="run owner claim")
    except RunOwnerManifestError as exc:
        raise RunOwnerFenceError(str(exc)) from exc
    try:
        claim = RunOwnerClaim.from_dict(raw, "run owner claim")
    except ValueError as exc:
        raise RunOwnerFenceError(f"invalid run owner claim: {exc}") from exc
    if claim.run_id != run_id:
        raise RunOwnerFenceError(f"run owner claim names {claim.run_id!r}, expected {run_id!r}")
    if claim.fence_locator != RUN_OWNER_FENCE.as_posix():
        raise RunOwnerFenceError(f"run owner claim selects a non-canonical fence locator: {claim.fence_locator!r}")
    observed_shot = _identity(os.fstat(shot_descriptor))
    expected_shot = (claim.shot_root_device, claim.shot_root_inode)
    if observed_shot != expected_shot:
        raise RunOwnerFenceSubstituted(
            "shot root does not match the namespace inode bound by its claim; "
            f"expected device/inode {expected_shot}, found {observed_shot}"
        )
    observed_runs = _identity(os.fstat(runs_descriptor))
    expected_runs = (claim.runs_directory_device, claim.runs_directory_inode)
    if observed_runs != expected_runs:
        raise RunOwnerFenceSubstituted(
            "runs directory does not match the namespace inode bound by its claim; "
            f"expected device/inode {expected_runs}, found {observed_runs}"
        )
    observed_root = _identity(os.fstat(root_descriptor))
    expected_root = (claim.run_root_device, claim.run_root_inode)
    if observed_root != expected_root:
        raise RunOwnerFenceSubstituted(
            "run root does not match the namespace inode bound by its claim; "
            f"expected device/inode {expected_root}, found {observed_root}"
        )
    try:
        named_runs = _identity(os.stat("runs", dir_fd=shot_descriptor, follow_symlinks=False))
        named_root = _identity(os.stat(run_id, dir_fd=runs_descriptor, follow_symlinks=False))
    except OSError as exc:
        raise RunOwnerFenceSubstituted("held shot/runs/run namespace is incomplete or unreadable") from exc
    if named_runs != expected_runs or named_root != expected_root:
        raise RunOwnerFenceSubstituted("held shot/runs/run namespace no longer has the links bound by its claim")
    expected_owner = (claim.owner_directory_device, claim.owner_directory_inode)
    _verify_named_owner_directory(
        root_descriptor,
        owner_descriptor,
        expected=expected_owner,
    )
    expected_claim = (claim.claim_device, claim.claim_inode)
    if claim_identity != expected_claim:
        raise RunOwnerFenceSubstituted(
            "run owner claim does not match its serialized inode identity; "
            f"expected device/inode {expected_claim}, found {claim_identity}"
        )
    if expected_digest is not None and claim.digest != expected_digest:
        raise RunOwnerFenceError(
            f"run owner claim digest mismatch; expected {expected_digest!r}, found {claim.digest!r}"
        )

    manifest = _read_manifest(root_descriptor, run_id=run_id)
    manifest_sha256 = hashlib.sha256(manifest.payload).hexdigest()
    if claim.manifest_sha256 != manifest_sha256:
        raise RunOwnerFenceError(
            f"run owner claim manifest digest is stale; expected {claim.manifest_sha256!r}, found {manifest_sha256!r}"
        )
    if claim.invocation_digest != manifest.invocation_digest:
        raise RunOwnerFenceError(
            "run owner claim invocation digest is stale; "
            f"expected {claim.invocation_digest!r}, found {manifest.invocation_digest!r}"
        )
    if claim.command != manifest.command or claim.owner_kind != manifest.owner_kind:
        raise RunOwnerFenceError(
            "run owner claim command/owner_kind disagree with the verified manifest invocation; "
            f"claim=({claim.command!r}, {claim.owner_kind!r}), "
            f"manifest=({manifest.command!r}, {manifest.owner_kind!r})"
        )
    return claim


@dataclass(slots=True)
class RunOwnerFenceLease:
    """Live capability for the exact owner or reconciler fence acquisition."""

    run_root: Path
    claim: RunOwnerClaim
    acquisition_kind: str
    _shot_descriptor: int = field(repr=False)
    _runs_descriptor: int = field(repr=False)
    _root_descriptor: int = field(repr=False)
    _owner_descriptor: int = field(repr=False)
    _fence_descriptor: int = field(repr=False)
    _shot_identity: tuple[int, int] = field(repr=False)
    _runs_identity: tuple[int, int] = field(repr=False)
    _root_identity: tuple[int, int] = field(repr=False)
    _owner_identity: tuple[int, int] = field(repr=False)
    _creator_pid: int = field(repr=False)
    _registry_token: object = field(repr=False)
    _registry_descriptors: tuple[GuardedDescriptor, ...] = field(repr=False)
    _released: bool = field(default=False, init=False, repr=False)
    _constructor_key: object = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self._constructor_key is not _LEASE_KEY:
            raise RunOwnerFenceError("run owner fence leases are issued only by a successful acquisition")
        if self.acquisition_kind not in {"owner", "reconciler"}:
            raise RunOwnerFenceError("unknown run owner fence acquisition kind")
        if self._creator_pid != os.getpid():
            raise RunOwnerFenceError("run owner fence lease must be issued in its creator process")
        if self.acquisition_kind == "owner" and self._creator_pid != self.claim.process_id:
            raise RunOwnerFenceError("root owner lease creator must be the process named by its claim")
        if self._shot_identity != (self.claim.shot_root_device, self.claim.shot_root_inode):
            raise RunOwnerFenceError("run owner lease shot-root identity differs from its claim")
        if self._runs_identity != (
            self.claim.runs_directory_device,
            self.claim.runs_directory_inode,
        ):
            raise RunOwnerFenceError("run owner lease runs-directory identity differs from its claim")
        if self._root_identity != (self.claim.run_root_device, self.claim.run_root_inode):
            raise RunOwnerFenceError("run owner lease root identity differs from its claim")
        if self._owner_identity != (self.claim.owner_directory_device, self.claim.owner_directory_inode):
            raise RunOwnerFenceError("run owner lease owner-directory identity differs from its claim")

    def _require_creator_process(self) -> None:
        if os.getpid() != self._creator_pid:
            raise RunOwnerFenceError(
                "forked descendants cannot use or release the root process's run owner fence lease"
            )

    @property
    def descriptor_inheritable(self) -> bool:
        self.require_current_identity()
        return os.get_inheritable(self._fence_descriptor)

    @property
    def fence_identity(self) -> tuple[int, int]:
        self.require_current_identity()
        return _identity(os.fstat(self._fence_descriptor))

    def require_current_identity(self) -> None:
        self._require_creator_process()
        if self._released:
            raise RunOwnerFenceError("run owner fence lease has been released")
        if os.get_inheritable(self._fence_descriptor):
            raise RunOwnerFenceError("run owner fence descriptor became inheritable")
        _verify_named_run_namespace(
            self.run_root,
            self.claim.run_id,
            self._shot_descriptor,
            self._runs_descriptor,
            self._root_descriptor,
            expected_shot=self._shot_identity,
            expected_runs=self._runs_identity,
            expected_root=self._root_identity,
        )
        _verify_named_owner_directory(
            self._root_descriptor,
            self._owner_descriptor,
            expected=self._owner_identity,
        )
        observed = _read_claim_from_descriptors(
            self._shot_descriptor,
            self._runs_descriptor,
            self._root_descriptor,
            self._owner_descriptor,
            run_id=self.claim.run_id,
            expected_digest=self.claim.digest,
        )
        if observed != self.claim:
            raise RunOwnerFenceSubstituted("run owner claim changed after its fence was acquired")
        _verify_named_fence(
            self._owner_descriptor,
            self._fence_descriptor,
            expected=(self.claim.fence_device, self.claim.fence_inode),
        )

    def release(self) -> None:
        self._require_creator_process()
        if self._released:
            return
        self._released = True
        descriptors = (
            self._shot_descriptor,
            self._runs_descriptor,
            self._root_descriptor,
            self._owner_descriptor,
            self._fence_descriptor,
        )
        if tuple(
            item.descriptor for item in self._registry_descriptors
        ) != descriptors:
            raise RunOwnerFenceError(
                "run owner lease descriptors differ from their captured fork identities"
            )
        _close_guarded_acquisition(
            self._registry_token,
            self._registry_descriptors,
        )

    def __enter__(self) -> RunOwnerFenceLease:
        try:
            self.require_current_identity()
            return self
        except BaseException:
            self.release()
            raise

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.release()


def _acquire_run_owner_fence_managed(
    run_root: str | Path,
    *,
    run_id: str,
    command: str,
    owner_kind: str,
    acquisition: ForkProtectedAcquisition,
) -> RunOwnerFenceLease:
    """Acquire one owner lease inside an already-armed fork transaction."""

    shot_descriptor: int | None = None
    runs_descriptor: int | None = None
    root_descriptor: int | None = None
    owner_descriptor: int | None = None
    fence_descriptor: int | None = None
    prepared = None
    try:
        namespace = _open_run_namespace(run_root, run_id, acquisition=acquisition)
        root = namespace.root
        shot_descriptor = namespace.shot_descriptor
        runs_descriptor = namespace.runs_descriptor
        root_descriptor = namespace.root_descriptor
        shot_identity = namespace.shot_identity
        runs_identity = namespace.runs_identity
        root_identity = namespace.root_identity
        owner_descriptor = acquisition.open_descriptor(
            lambda: _open_owner_directory(root_descriptor, create=True)
        )
        owner_identity = _verify_named_owner_directory(
            root_descriptor,
            owner_descriptor,
        )
        if _claim_exists(owner_descriptor):
            raise RunOwnerClaimExists(
                "run owner claim already exists; only owner-loss reconciliation may acquire this run again"
            )
        fence_descriptor, fence_created = _open_fence(
            owner_descriptor,
            create=True,
            acquisition=acquisition,
        )
        _acquire_exclusive(fence_descriptor, path=root / RUN_OWNER_FENCE)
        if _claim_exists(owner_descriptor):
            raise RunOwnerClaimExists("run owner claim appeared before ownership could be established")
        fence_device, fence_inode = _verify_named_fence(
            owner_descriptor,
            fence_descriptor,
        )
        if fence_created:
            _durably_publish_created_fence(
                owner_descriptor,
                fence_descriptor,
                expected=(fence_device, fence_inode),
            )
        _verify_named_run_namespace(
            root,
            run_id,
            shot_descriptor,
            runs_descriptor,
            root_descriptor,
            expected_shot=shot_identity,
            expected_runs=runs_identity,
            expected_root=root_identity,
        )
        _verify_named_owner_directory(
            root_descriptor,
            owner_descriptor,
            expected=owner_identity,
        )
        manifest = _read_manifest(root_descriptor, run_id=run_id)
        if command != manifest.command or owner_kind != manifest.owner_kind:
            raise RunOwnerFenceError(
                "requested command/owner_kind disagree with the verified manifest invocation; "
                f"requested=({command!r}, {owner_kind!r}), "
                f"manifest=({manifest.command!r}, {manifest.owner_kind!r})"
            )
        claimed_at = _now()
        try:
            chronological(
                manifest.started_at,
                claimed_at,
                "run manifest start/owner claim",
            )
        except ValueError as exc:
            raise RunOwnerFenceError(str(exc)) from exc
        prepared = allocate_run_owner_claim_file(owner_descriptor, acquisition)
        claim = RunOwnerClaim(
            run_id=run_id,
            command=manifest.command,
            invocation_digest=manifest.invocation_digest,
            owner_kind=manifest.owner_kind,
            owner_id=secrets.token_hex(32),
            process_id=os.getpid(),
            process_start_token=_process_start_token(),
            shot_root_device=shot_identity[0],
            shot_root_inode=shot_identity[1],
            runs_directory_device=runs_identity[0],
            runs_directory_inode=runs_identity[1],
            run_root_device=root_identity[0],
            run_root_inode=root_identity[1],
            owner_directory_device=owner_identity[0],
            owner_directory_inode=owner_identity[1],
            claim_device=prepared.device,
            claim_inode=prepared.inode,
            fence_locator=RUN_OWNER_FENCE.as_posix(),
            fence_implementation=RUN_OWNER_FENCE_IMPLEMENTATION,
            fence_device=fence_device,
            fence_inode=fence_inode,
            descriptor_inheritable=os.get_inheritable(fence_descriptor),
            manifest_locator=RUN_MANIFEST.as_posix(),
            manifest_sha256=hashlib.sha256(manifest.payload).hexdigest(),
            claimed_at=claimed_at,
        )
        try:
            publish_run_owner_claim_file(owner_descriptor, prepared, claim)
        except RunOwnerClaimFileExists as exc:
            raise RunOwnerClaimExists(str(exc)) from exc
        except RunOwnerClaimFileError as exc:
            raise RunOwnerFenceError(str(exc)) from exc
        finally:
            _retire_claim_staging(acquisition, owner_descriptor, prepared)
            prepared = None
        observed = _read_claim_from_descriptors(
            shot_descriptor,
            runs_descriptor,
            root_descriptor,
            owner_descriptor,
            run_id=run_id,
            expected_digest=claim.digest,
        )
        if observed != claim:
            raise RunOwnerFenceError("run owner claim read-back changed its fields")
        _verify_named_owner_directory(
            root_descriptor,
            owner_descriptor,
            expected=owner_identity,
        )
        _verify_named_fence(
            owner_descriptor,
            fence_descriptor,
            expected=(claim.fence_device, claim.fence_inode),
        )
        descriptor_numbers = (
            shot_descriptor,
            runs_descriptor,
            root_descriptor,
            owner_descriptor,
            fence_descriptor,
        )
        registry_descriptors = acquisition.guarded_descriptors(
            descriptor_numbers
        )
        lease = RunOwnerFenceLease(
            run_root=root,
            claim=claim,
            acquisition_kind="owner",
            _shot_descriptor=shot_descriptor,
            _runs_descriptor=runs_descriptor,
            _root_descriptor=root_descriptor,
            _owner_descriptor=owner_descriptor,
            _fence_descriptor=fence_descriptor,
            _shot_identity=shot_identity,
            _runs_identity=runs_identity,
            _root_identity=root_identity,
            _owner_identity=owner_identity,
            _creator_pid=os.getpid(),
            _registry_token=acquisition.token,
            _registry_descriptors=registry_descriptors,
            _constructor_key=_LEASE_KEY,
        )
        acquisition.handoff(descriptor_numbers)
        return lease
    except BaseException:
        if prepared is not None and owner_descriptor is not None:
            _retire_claim_staging(acquisition, owner_descriptor, prepared)
        raise


def acquire_run_owner_fence(
    run_root: str | Path,
    *,
    run_id: str,
    command: str,
    owner_kind: str,
) -> RunOwnerFenceLease:
    """Acquire the root fence and atomically mint its immutable owner claim."""

    try:
        with managed_fork_protected_acquisition() as acquisition:
            return _acquire_run_owner_fence_managed(
                run_root,
                run_id=run_id,
                command=command,
                owner_kind=owner_kind,
                acquisition=acquisition,
            )
    except RunOwnerForkGuardError as exc:
        raise RunOwnerFenceError(str(exc)) from exc


def read_run_owner_claim(
    run_root: str | Path,
    *,
    run_id: str,
    expected_digest: str | None = None,
) -> RunOwnerClaim:
    """Source-verify the immutable claim, manifest, and currently named fence."""

    namespace = _open_run_namespace(run_root, run_id)
    root_descriptor = namespace.root_descriptor
    owner_descriptor: int | None = None
    fence_descriptor: int | None = None
    try:
        owner_descriptor = _open_owner_directory(root_descriptor, create=False)
        owner_identity = _verify_named_owner_directory(
            root_descriptor,
            owner_descriptor,
        )
        claim = _read_claim_from_descriptors(
            namespace.shot_descriptor,
            namespace.runs_descriptor,
            root_descriptor,
            owner_descriptor,
            run_id=run_id,
            expected_digest=expected_digest,
        )
        fence_descriptor, _fence_created = _open_fence(owner_descriptor, create=False)
        _verify_named_fence(
            owner_descriptor,
            fence_descriptor,
            expected=(claim.fence_device, claim.fence_inode),
        )
        _verify_named_owner_directory(
            root_descriptor,
            owner_descriptor,
            expected=owner_identity,
        )
        _verify_named_run_namespace(
            namespace.root,
            run_id,
            namespace.shot_descriptor,
            namespace.runs_descriptor,
            namespace.root_descriptor,
            expected_shot=(claim.shot_root_device, claim.shot_root_inode),
            expected_runs=(claim.runs_directory_device, claim.runs_directory_inode),
            expected_root=(claim.run_root_device, claim.run_root_inode),
        )
        observed = _read_claim_from_descriptors(
            namespace.shot_descriptor,
            namespace.runs_descriptor,
            root_descriptor,
            owner_descriptor,
            run_id=run_id,
            expected_digest=claim.digest,
        )
        if observed != claim:
            raise RunOwnerFenceSubstituted("run owner claim changed while it was source-verified")
        _verify_named_fence(
            owner_descriptor,
            fence_descriptor,
            expected=(claim.fence_device, claim.fence_inode),
        )
        return claim
    finally:
        _close_acquisition(
            fence_descriptor=fence_descriptor,
            owner_descriptor=owner_descriptor,
            root_descriptor=namespace.root_descriptor,
            runs_descriptor=namespace.runs_descriptor,
            shot_descriptor=namespace.shot_descriptor,
            locked=False,
        )


def _acquire_run_reconciler_fence_managed(
    run_root: str | Path,
    *,
    prior_owner: RunOwnerClaim,
    acquisition: ForkProtectedAcquisition,
) -> RunOwnerFenceLease:
    """Acquire one reconciler lease inside an armed fork transaction."""

    if not isinstance(prior_owner, RunOwnerClaim):
        raise RunOwnerFenceError("reconciler acquisition requires the exact typed prior owner claim")
    shot_descriptor: int | None = None
    runs_descriptor: int | None = None
    root_descriptor: int | None = None
    owner_descriptor: int | None = None
    fence_descriptor: int | None = None
    try:
        namespace = _open_run_namespace(
            run_root,
            prior_owner.run_id,
            acquisition=acquisition,
        )
        root = namespace.root
        shot_descriptor = namespace.shot_descriptor
        runs_descriptor = namespace.runs_descriptor
        root_descriptor = namespace.root_descriptor
        shot_identity = namespace.shot_identity
        runs_identity = namespace.runs_identity
        root_identity = namespace.root_identity
        owner_descriptor = acquisition.open_descriptor(
            lambda: _open_owner_directory(root_descriptor, create=False)
        )
        owner_identity = _verify_named_owner_directory(
            root_descriptor,
            owner_descriptor,
        )
        observed = _read_claim_from_descriptors(
            shot_descriptor,
            runs_descriptor,
            root_descriptor,
            owner_descriptor,
            run_id=prior_owner.run_id,
            expected_digest=prior_owner.digest,
        )
        if observed != prior_owner:
            raise RunOwnerFenceError("source owner claim differs from the reconciler's exact prior claim")
        fence_descriptor, _fence_created = _open_fence(
            owner_descriptor,
            create=False,
            acquisition=acquisition,
        )
        expected = (prior_owner.fence_device, prior_owner.fence_inode)
        _verify_named_fence(
            owner_descriptor,
            fence_descriptor,
            expected=expected,
        )
        _acquire_exclusive(fence_descriptor, path=root / RUN_OWNER_FENCE)
        # Reopen all immutable joins after acquiring.  A rename/substitution race is
        # refused before the reconciler may publish owner-loss evidence.
        observed = _read_claim_from_descriptors(
            shot_descriptor,
            runs_descriptor,
            root_descriptor,
            owner_descriptor,
            run_id=prior_owner.run_id,
            expected_digest=prior_owner.digest,
        )
        if observed != prior_owner:
            raise RunOwnerFenceError("run owner claim changed during reconciler acquisition")
        _verify_named_run_namespace(
            root,
            prior_owner.run_id,
            shot_descriptor,
            runs_descriptor,
            root_descriptor,
            expected_shot=shot_identity,
            expected_runs=runs_identity,
            expected_root=root_identity,
        )
        _verify_named_owner_directory(
            root_descriptor,
            owner_descriptor,
            expected=owner_identity,
        )
        _verify_named_fence(
            owner_descriptor,
            fence_descriptor,
            expected=expected,
        )
        descriptor_numbers = (
            shot_descriptor,
            runs_descriptor,
            root_descriptor,
            owner_descriptor,
            fence_descriptor,
        )
        registry_descriptors = acquisition.guarded_descriptors(
            descriptor_numbers
        )
        lease = RunOwnerFenceLease(
            run_root=root,
            claim=prior_owner,
            acquisition_kind="reconciler",
            _shot_descriptor=shot_descriptor,
            _runs_descriptor=runs_descriptor,
            _root_descriptor=root_descriptor,
            _owner_descriptor=owner_descriptor,
            _fence_descriptor=fence_descriptor,
            _shot_identity=shot_identity,
            _runs_identity=runs_identity,
            _root_identity=root_identity,
            _owner_identity=owner_identity,
            _creator_pid=os.getpid(),
            _registry_token=acquisition.token,
            _registry_descriptors=registry_descriptors,
            _constructor_key=_LEASE_KEY,
        )
        acquisition.handoff(descriptor_numbers)
        return lease
    except BaseException:
        raise


def acquire_run_reconciler_fence(
    run_root: str | Path,
    *,
    prior_owner: RunOwnerClaim,
) -> RunOwnerFenceLease:
    """Acquire the exact prior owner's released fence without PID or time."""

    try:
        with managed_fork_protected_acquisition() as acquisition:
            return _acquire_run_reconciler_fence_managed(
                run_root,
                prior_owner=prior_owner,
                acquisition=acquisition,
            )
    except RunOwnerForkGuardError as exc:
        raise RunOwnerFenceError(str(exc)) from exc
