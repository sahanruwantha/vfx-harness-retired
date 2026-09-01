"""Typed, staged publication for one sealed layer outcome."""

from __future__ import annotations

import contextlib
import hashlib
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

from vfx_harness.domain.authority_head_records import parse_authority_selection_token
from vfx_harness.domain.layer_finalizations import LayerFinalizationReceipt
from vfx_harness.domain.stop_envelope_primitives import require_digest
from vfx_harness.infrastructure.trusted_files import (
    TrustedFileAbsenceBinding,
    TrustedFileBinding,
    TrustedFileError,
    TrustedFileNotFound,
    bind_trusted_file_absence,
    open_pinned_trusted_file,
)
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionToken,
    durably_ensure_real_directory,
)
from vfx_harness.orchestration.layer_finalization_authorizations import (
    AuthorizedLayerFinalizationMutation,
)


class LayerOutcomePublicationConflict(ValueError):
    """A prepared layer outcome no longer names its exact causal generation."""


@dataclass(frozen=True, slots=True)
class LayerOutcomePublicationAuthority:
    """Exact terminal finalization allowed to project one layer outcome."""

    kind: str
    layer_id: str
    layer_digest: str
    selection_token: AuthoritySelectionToken
    receipt: LayerFinalizationReceipt
    finalization_authorization: AuthorizedLayerFinalizationMutation

    def __post_init__(self) -> None:
        if self.kind != "finalization":
            raise LayerOutcomePublicationConflict(
                f"unsupported layer-outcome authority kind: {self.kind!r}"
            )
        if not self.layer_id or self.layer_id != self.layer_id.strip():
            raise LayerOutcomePublicationConflict(
                "layer-outcome authority requires a non-empty trimmed layer id"
            )
        require_digest(self.layer_digest, "layer-outcome layer digest")
        if not isinstance(self.selection_token, AuthoritySelectionToken):
            raise LayerOutcomePublicationConflict(
                "layer-outcome authority requires an exact selection token"
            )
        if not isinstance(self.receipt, LayerFinalizationReceipt):
            raise LayerOutcomePublicationConflict(
                "layer outcome requires an exact terminal finalization receipt"
            )
        authorization = self.finalization_authorization
        if not isinstance(authorization, AuthorizedLayerFinalizationMutation):
            raise LayerOutcomePublicationConflict(
                "layer outcome requires typed current-head finalization authorization"
            )
        current_projection = parse_authority_selection_token(
            self.selection_token.to_dict(),
            "layer-outcome current selection token",
        )
        if self.receipt.claim.layer_id != self.layer_id:
            raise LayerOutcomePublicationConflict(
                "layer-outcome finalization receipt belongs to another layer"
            )
        if (
            authorization.receipt != self.receipt
            or authorization.completion_authorization.selection_token
            != current_projection
        ):
            raise LayerOutcomePublicationConflict(
                "layer-outcome finalization authorization does not bind the exact "
                "receipt and current selection"
            )


@dataclass(frozen=True, slots=True)
class LayerOutcomePathIdentity:
    """Exact real-file identity, preserving authoritative absence."""

    path: Path
    exists: bool
    device: int | None = None
    inode: int | None = None
    size: int | None = None
    modified_ns: int | None = None
    changed_ns: int | None = None
    trusted_file_binding: TrustedFileBinding | None = None
    trusted_absence_binding: TrustedFileAbsenceBinding | None = None


@dataclass(frozen=True, slots=True)
class PreparedLayerOutcomePublication:
    """Fsynced inert outcome bytes plus every identity needed by its short commit."""

    authority: LayerOutcomePublicationAuthority
    destination: Path
    destination_parent_device: int
    destination_parent_inode: int
    destination_parent_descriptor: int
    destination_name: str
    predecessor: LayerOutcomePathIdentity
    sources: tuple[LayerOutcomePathIdentity, ...]
    temporary: Path
    temporary_descriptor: int
    temporary_name: str
    temporary_identity: LayerOutcomePathIdentity
    payload_sha256: str


def _absolute(path: str | Path) -> Path:
    return Path(os.path.abspath(Path(path).expanduser()))


def _unique_temporary(parent: int, target_name: str) -> tuple[int, str]:
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    for _attempt in range(128):
        name = f".{target_name}.prepared.{secrets.token_hex(12)}"
        try:
            return os.open(name, flags, 0o600, dir_fd=parent), name
        except FileExistsError:
            continue
        except OSError as exc:
            raise LayerOutcomePublicationConflict(
                "could not allocate a prepared layer-outcome publication"
            ) from exc
    raise LayerOutcomePublicationConflict(
        "could not allocate a unique prepared layer-outcome publication"
    )


def _unlink_owned_temporary(parent: int, name: str, descriptor: int) -> None:
    """Unlink only when ``name`` still refers to the held prepared inode."""

    try:
        held = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except OSError:
        return
    if (
        stat.S_ISREG(held.st_mode)
        and stat.S_ISREG(named.st_mode)
        and (held.st_dev, held.st_ino) == (named.st_dev, named.st_ino)
    ):
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name, dir_fd=parent)


def _capture_named_identity(
    parent: int,
    name: str,
    path: Path,
) -> LayerOutcomePathIdentity:
    """Capture one final component relative to an already trusted parent."""

    try:
        observed = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return LayerOutcomePathIdentity(path=path, exists=False)
    except OSError as exc:
        raise LayerOutcomePublicationConflict(
            f"layer-outcome file is unreadable: {path}"
        ) from exc
    if not stat.S_ISREG(observed.st_mode):
        raise LayerOutcomePublicationConflict(
            f"layer-outcome file must be absent or a real regular file: {path}"
        )
    return LayerOutcomePathIdentity(
        path=path,
        exists=True,
        device=observed.st_dev,
        inode=observed.st_ino,
        size=observed.st_size,
        modified_ns=observed.st_mtime_ns,
        changed_ns=observed.st_ctime_ns,
    )


def _require_published_inode_current(
    prepared: PreparedLayerOutcomePublication,
) -> None:
    try:
        held = os.fstat(prepared.temporary_descriptor)
        named = os.stat(
            prepared.destination_name,
            dir_fd=prepared.destination_parent_descriptor,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise LayerOutcomePublicationConflict(
            "published layer-outcome inode disappeared during publication"
        ) from exc
    expected = prepared.temporary_identity
    if (
        not stat.S_ISREG(held.st_mode)
        or not stat.S_ISREG(named.st_mode)
        or (held.st_dev, held.st_ino) != (expected.device, expected.inode)
        or (named.st_dev, named.st_ino) != (expected.device, expected.inode)
        or (held.st_size, held.st_mtime_ns) != (expected.size, expected.modified_ns)
        or (
            named.st_dev,
            named.st_ino,
            named.st_size,
            named.st_mtime_ns,
            named.st_ctime_ns,
        )
        != (
            held.st_dev,
            held.st_ino,
            held.st_size,
            held.st_mtime_ns,
            held.st_ctime_ns,
        )
    ):
        raise LayerOutcomePublicationConflict(
            "published layer-outcome inode changed during publication"
        )


def capture_layer_outcome_source_identities(
    paths: tuple[Path, ...],
) -> tuple[LayerOutcomePathIdentity, ...]:
    """Capture exact source lineage without following any symlink component."""

    identities: list[LayerOutcomePathIdentity] = []
    for path in sorted({_absolute(value) for value in paths}, key=lambda value: str(value)):
        where = "layer-outcome causal source"
        try:
            with open_pinned_trusted_file(path.anchor, path, where) as pinned:
                pinned.require_current()
                binding = pinned.binding
        except TrustedFileNotFound:
            try:
                absence = bind_trusted_file_absence(
                    path.anchor,
                    path,
                    where,
                )
            except TrustedFileError as exc:
                raise LayerOutcomePublicationConflict(
                    f"layer-outcome source absence is untrusted: {path}"
                ) from exc
            identities.append(
                LayerOutcomePathIdentity(
                    path=path,
                    exists=False,
                    trusted_absence_binding=absence,
                )
            )
            continue
        except TrustedFileError as exc:
            raise LayerOutcomePublicationConflict(
                f"layer-outcome source is not a trusted regular file: {path}"
            ) from exc
        observed = binding.file_identity
        identities.append(
            LayerOutcomePathIdentity(
                path=path,
                exists=True,
                device=observed.device,
                inode=observed.inode,
                size=observed.size,
                modified_ns=observed.modified_ns,
                changed_ns=observed.changed_ns,
                trusted_file_binding=binding,
            )
        )
    return tuple(identities)


def require_layer_outcome_sources_current(
    identities: tuple[LayerOutcomePathIdentity, ...],
) -> None:
    """Metadata-only CAS for already hashed outcome inputs."""

    observed = capture_layer_outcome_source_identities(
        tuple(identity.path for identity in identities)
    )
    if observed != identities:
        raise LayerOutcomePublicationConflict(
            "layer-outcome causal inputs changed after preparation"
        )


def prepare_layer_outcome_publication(
    shot_folder: str | Path,
    destination: str | Path,
    payload: bytes,
    *,
    authority: LayerOutcomePublicationAuthority,
    sources: tuple[LayerOutcomePathIdentity, ...],
) -> PreparedLayerOutcomePublication:
    """Write and fsync inert outcome bytes before selection/state locks are acquired."""

    if not isinstance(payload, bytes):
        raise LayerOutcomePublicationConflict("prepared layer outcome payload must be bytes")
    shot = _absolute(shot_folder)
    output = _absolute(destination)
    try:
        relative = output.relative_to(shot)
    except ValueError as exc:
        raise LayerOutcomePublicationConflict(
            "layer-outcome destination escapes the shot root"
        ) from exc
    durably_ensure_real_directory(shot, relative.parent)
    parent = output.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode):
        raise LayerOutcomePublicationConflict(
            "layer-outcome destination parent must be a real directory"
        )
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    directory = os.open(output.parent, flags)
    descriptor: int | None = None
    temporary_name: str | None = None
    try:
        directory_stat = os.fstat(directory)
        if (
            directory_stat.st_dev != parent.st_dev
            or directory_stat.st_ino != parent.st_ino
        ):
            raise LayerOutcomePublicationConflict(
                "layer-outcome destination parent changed during preparation"
            )
        predecessor = _capture_named_identity(directory, output.name, output)
        descriptor, temporary_name = _unique_temporary(directory, output.name)
        temporary = output.parent / temporary_name
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        staged = _capture_named_identity(directory, temporary_name, temporary)
        observed = os.fstat(descriptor)
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_dev != staged.device
            or observed.st_ino != staged.inode
            or observed.st_size != staged.size
            or observed.st_mtime_ns != staged.modified_ns
            or observed.st_ctime_ns != staged.changed_ns
        ):
            raise LayerOutcomePublicationConflict(
                "prepared layer-outcome inode changed during staging"
            )
        return PreparedLayerOutcomePublication(
            authority=authority,
            destination=output,
            destination_parent_device=parent.st_dev,
            destination_parent_inode=parent.st_ino,
            destination_parent_descriptor=directory,
            destination_name=output.name,
            predecessor=predecessor,
            sources=sources,
            temporary=temporary,
            temporary_descriptor=descriptor,
            temporary_name=temporary_name,
            temporary_identity=staged,
            payload_sha256=hashlib.sha256(payload).hexdigest(),
        )
    except BaseException:
        if temporary_name is not None and descriptor is not None:
            _unlink_owned_temporary(directory, temporary_name, descriptor)
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory)
        raise


def commit_layer_outcome_publication(
    prepared: PreparedLayerOutcomePublication,
    *,
    authority: LayerOutcomePublicationAuthority,
) -> Path:
    """CAS and rename a prepared outcome; caller holds its bound short authority guard."""

    if not isinstance(prepared, PreparedLayerOutcomePublication):
        raise LayerOutcomePublicationConflict(
            "layer-outcome commit requires a typed prepared publication"
        )
    if authority != prepared.authority:
        raise LayerOutcomePublicationConflict(
            "prepared layer outcome belongs to another execution identity"
        )
    output = prepared.destination
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    directory = prepared.destination_parent_descriptor
    parent = os.fstat(directory)
    if (
        parent.st_dev != prepared.destination_parent_device
        or parent.st_ino != prepared.destination_parent_inode
    ):
        raise LayerOutcomePublicationConflict(
            "layer-outcome destination parent changed before publication"
        )
    require_layer_outcome_sources_current(prepared.sources)
    current_predecessor = _capture_named_identity(
        directory,
        prepared.destination_name,
        output,
    )
    if current_predecessor != prepared.predecessor:
        raise LayerOutcomePublicationConflict(
            "layer-outcome destination changed after preparation"
        )
    current_temporary = _capture_named_identity(
        directory,
        prepared.temporary_name,
        prepared.temporary,
    )
    try:
        held_temporary = os.fstat(prepared.temporary_descriptor)
    except OSError as exc:
        raise LayerOutcomePublicationConflict(
            "prepared layer-outcome inode disappeared before publication"
        ) from exc
    if (
        current_temporary != prepared.temporary_identity
        or not stat.S_ISREG(held_temporary.st_mode)
        or held_temporary.st_dev != prepared.temporary_identity.device
        or held_temporary.st_ino != prepared.temporary_identity.inode
        or held_temporary.st_size != prepared.temporary_identity.size
        or held_temporary.st_mtime_ns != prepared.temporary_identity.modified_ns
        or held_temporary.st_ctime_ns != prepared.temporary_identity.changed_ns
    ):
        raise LayerOutcomePublicationConflict(
            "prepared layer-outcome bytes changed before publication"
        )
    if prepared.temporary.parent != output.parent:
        raise LayerOutcomePublicationConflict(
            "prepared layer outcome belongs to another destination parent"
        )
    os.replace(
        prepared.temporary_name,
        prepared.destination_name,
        src_dir_fd=directory,
        dst_dir_fd=directory,
    )
    _require_published_inode_current(prepared)
    current_directory = os.open(output.parent, flags)
    try:
        current_parent = os.fstat(current_directory)
        if (
            current_parent.st_dev != prepared.destination_parent_device
            or current_parent.st_ino != prepared.destination_parent_inode
        ):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(output.name, dir_fd=directory)
            os.fsync(directory)
            raise LayerOutcomePublicationConflict(
                "layer-outcome destination parent changed during publication"
            )
    finally:
        os.close(current_directory)
    os.fsync(directory)
    return output


def discard_layer_outcome_publication(
    prepared: PreparedLayerOutcomePublication,
) -> None:
    """Remove only the exact uncommitted temp inode owned by this preparation."""

    try:
        try:
            observed = os.fstat(prepared.temporary_descriptor)
        except OSError:
            return
        if stat.S_ISREG(observed.st_mode):
            _unlink_owned_temporary(
                prepared.destination_parent_descriptor,
                prepared.temporary_name,
                prepared.temporary_descriptor,
            )
    finally:
        with contextlib.suppress(OSError):
            os.close(prepared.temporary_descriptor)
        with contextlib.suppress(OSError):
            os.close(prepared.destination_parent_descriptor)
