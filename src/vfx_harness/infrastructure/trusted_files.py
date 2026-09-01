"""Descriptor-pinned reads beneath an explicit trusted filesystem root.

Path-level ``is_file``/``read_bytes`` sequences are not an authority boundary: an
ancestor can be renamed and replaced by a symlink between the check and the read.
This module opens every directory component with ``openat`` + ``O_NOFOLLOW``, reads
and hashes the leaf through one held descriptor, and retains the complete lineage
needed to reject a later rebinding before a prepared result is committed.
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path

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
)


class TrustedFileError(RuntimeError):
    """A trusted read escaped, changed, or crossed a symlink/rebound ancestor."""


class TrustedFileNotFound(TrustedFileError):
    """The relative trusted path was absent beneath an existing trusted root."""


@dataclass(frozen=True, slots=True)
class FileIdentity:
    device: int
    inode: int
    mode: int
    size: int
    modified_ns: int
    changed_ns: int


@dataclass(frozen=True, slots=True)
class DirectoryIdentity:
    """Stable directory identity; member writes legitimately change its timestamps."""

    device: int
    inode: int
    mode: int


@dataclass(frozen=True, slots=True)
class TrustedFileBinding:
    """Exact root, ancestor lineage, and leaf observed by one descriptor read."""

    root: Path
    path: Path
    relative: str
    root_identity: DirectoryIdentity
    ancestor_identities: tuple[DirectoryIdentity, ...]
    file_identity: FileIdentity


@dataclass(frozen=True, slots=True)
class TrustedDirectoryBinding:
    """Exact root, ancestor lineage, and directory capability identity."""

    root: Path
    path: Path
    relative: str
    root_identity: DirectoryIdentity
    ancestor_identities: tuple[DirectoryIdentity, ...]
    directory_identity: DirectoryIdentity


@dataclass(frozen=True, slots=True)
class TrustedFileAbsenceBinding:
    """Exact deepest real ancestor and unresolved suffix for an absent file."""

    root: Path
    path: Path
    relative: str
    existing_parent: TrustedDirectoryBinding
    missing_parts: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TrustedFileSnapshot:
    payload: bytes
    sha256: str
    binding: TrustedFileBinding


def _absolute(path: str | Path) -> Path:
    return Path(os.path.abspath(Path(path).expanduser()))


def _identity(observed: os.stat_result) -> FileIdentity:
    return FileIdentity(
        device=observed.st_dev,
        inode=observed.st_ino,
        mode=observed.st_mode,
        size=observed.st_size,
        modified_ns=observed.st_mtime_ns,
        changed_ns=observed.st_ctime_ns,
    )


def _directory_identity(observed: os.stat_result) -> DirectoryIdentity:
    return DirectoryIdentity(
        device=observed.st_dev,
        inode=observed.st_ino,
        mode=observed.st_mode,
    )


def _open_absolute_directory(path: Path, where: str) -> int:
    descriptor: int | None = None
    try:
        descriptor = os.open(path.anchor, _DIRECTORY_FLAGS)
        for part in path.parts[1:]:
            following = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = following
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise TrustedFileError(
            f"{where} trusted root must contain only real directory components: {path}"
        ) from exc
    if descriptor is None or not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        if descriptor is not None:
            os.close(descriptor)
        raise TrustedFileError(f"{where} trusted root is not a real directory: {path}")
    return descriptor


def _normalize(root: str | Path, path: str | Path, where: str) -> tuple[Path, Path, Path]:
    trusted_root = _absolute(root)
    supplied = Path(path).expanduser()
    target = _absolute(supplied if supplied.is_absolute() else trusted_root / supplied)
    try:
        relative = target.relative_to(trusted_root)
    except ValueError as exc:
        raise TrustedFileError(
            f"{where} escapes trusted root {trusted_root}: {target}"
        ) from exc
    if relative == Path(".") or not relative.name:
        raise TrustedFileError(f"{where} must name a file below trusted root {trusted_root}")
    return trusted_root, target, relative


def _normalize_directory(
    root: str | Path,
    path: str | Path,
    where: str,
) -> tuple[Path, Path, Path]:
    trusted_root = _absolute(root)
    supplied = Path(path).expanduser()
    target = _absolute(supplied if supplied.is_absolute() else trusted_root / supplied)
    try:
        relative = target.relative_to(trusted_root)
    except ValueError as exc:
        raise TrustedFileError(
            f"{where} escapes trusted root {trusted_root}: {target}"
        ) from exc
    return trusted_root, target, relative


def _open_relative_file(
    root_descriptor: int,
    relative: Path,
    path: Path,
    where: str,
) -> tuple[int, tuple[DirectoryIdentity, ...]]:
    current = os.dup(root_descriptor)
    ancestors: list[DirectoryIdentity] = []
    try:
        for part in relative.parts[:-1]:
            following = os.open(part, _DIRECTORY_FLAGS, dir_fd=current)
            os.close(current)
            current = following
            observed = os.fstat(current)
            if not stat.S_ISDIR(observed.st_mode):
                raise TrustedFileError(
                    f"{where} ancestor is not a real directory: {path.parent}"
                )
            ancestors.append(_directory_identity(observed))
        descriptor = os.open(relative.name, _FILE_FLAGS, dir_fd=current)
    except TrustedFileError:
        os.close(current)
        raise
    except FileNotFoundError as exc:
        os.close(current)
        raise TrustedFileNotFound(f"{where} is missing: {path}") from exc
    except OSError as exc:
        os.close(current)
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise TrustedFileError(
                f"{where} must not contain symlink or non-directory path components: {path}"
            ) from exc
        raise TrustedFileError(
            f"{where} is unreadable beneath its trusted root: {path}"
        ) from exc
    os.close(current)
    return descriptor, tuple(ancestors)


def _open_relative_directory(
    root_descriptor: int,
    relative: Path,
    path: Path,
    where: str,
) -> tuple[int, tuple[DirectoryIdentity, ...]]:
    current = os.dup(root_descriptor)
    ancestors: list[DirectoryIdentity] = []
    if relative == Path("."):
        return current, ()
    try:
        for index, part in enumerate(relative.parts):
            following = os.open(part, _DIRECTORY_FLAGS, dir_fd=current)
            os.close(current)
            current = following
            observed = os.fstat(current)
            if not stat.S_ISDIR(observed.st_mode):
                raise TrustedFileError(f"{where} is not a real directory: {path}")
            if index < len(relative.parts) - 1:
                ancestors.append(_directory_identity(observed))
    except TrustedFileError:
        os.close(current)
        raise
    except FileNotFoundError as exc:
        os.close(current)
        raise TrustedFileNotFound(f"{where} is missing: {path}") from exc
    except OSError as exc:
        os.close(current)
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise TrustedFileError(
                f"{where} must not contain symlink or non-directory path components: {path}"
            ) from exc
        raise TrustedFileError(
            f"{where} is unreadable beneath its trusted root: {path}"
        ) from exc
    return current, tuple(ancestors)


class PinnedTrustedFile:
    """Open root and leaf descriptors retained until publication or discard."""

    __slots__ = ("_closed", "_descriptor", "_where", "binding", "root_descriptor")

    def __init__(
        self,
        *,
        descriptor: int,
        root_descriptor: int,
        binding: TrustedFileBinding,
        where: str,
    ) -> None:
        self._descriptor = descriptor
        self.root_descriptor = root_descriptor
        self.binding = binding
        self._where = where
        self._closed = False

    @property
    def descriptor(self) -> int:
        self._require_open()
        return self._descriptor

    def _require_open(self) -> None:
        if self._closed:
            raise TrustedFileError(f"{self._where} trusted file handle is closed")

    def _require_held_identities(self) -> None:
        self._require_open()
        try:
            root_identity = _directory_identity(os.fstat(self.root_descriptor))
            file_identity = _identity(os.fstat(self._descriptor))
        except OSError as exc:
            raise TrustedFileError(
                f"{self._where} pinned descriptor is unreadable"
            ) from exc
        if root_identity != self.binding.root_identity:
            raise TrustedFileError(f"{self._where} trusted root identity changed")
        if file_identity != self.binding.file_identity:
            raise TrustedFileError(f"{self._where} trusted file changed while pinned")

    def read_and_hash(self) -> tuple[bytes, str]:
        """Read and hash the exact same held regular-file descriptor."""

        self._require_held_identities()
        try:
            os.lseek(self._descriptor, 0, os.SEEK_SET)
            digest = hashlib.sha256()
            chunks: list[bytes] = []
            while chunk := os.read(self._descriptor, 1024 * 1024):
                chunks.append(chunk)
                digest.update(chunk)
        except OSError as exc:
            raise TrustedFileError(
                f"{self._where} is unreadable: {self.binding.path}"
            ) from exc
        self._require_held_identities()
        return b"".join(chunks), digest.hexdigest()

    def copy_and_hash_to(self, destination_descriptor: int) -> str:
        """Copy from the pinned source while hashing those exact copied bytes."""

        self._require_held_identities()
        try:
            os.lseek(self._descriptor, 0, os.SEEK_SET)
            digest = hashlib.sha256()
            while chunk := os.read(self._descriptor, 1024 * 1024):
                digest.update(chunk)
                remaining = memoryview(chunk)
                while remaining:
                    written = os.write(destination_descriptor, remaining)
                    if written <= 0:
                        raise TrustedFileError(
                            f"{self._where} prepared copy produced a short write"
                        )
                    remaining = remaining[written:]
        except TrustedFileError:
            raise
        except OSError as exc:
            raise TrustedFileError(
                f"{self._where} could not copy pinned source {self.binding.path}"
            ) from exc
        self._require_held_identities()
        return digest.hexdigest()

    def require_current(self) -> None:
        """Refuse if the named root, any ancestor, or the leaf was rebound."""

        self._require_held_identities()
        current_root = _open_absolute_directory(self.binding.root, self._where)
        try:
            if (
                _directory_identity(os.fstat(current_root))
                != self.binding.root_identity
            ):
                raise TrustedFileError(
                    f"{self._where} trusted root was rebound: {self.binding.root}"
                )
            current_file, ancestors = _open_relative_file(
                current_root,
                Path(self.binding.relative),
                self.binding.path,
                self._where,
            )
            try:
                if ancestors != self.binding.ancestor_identities:
                    raise TrustedFileError(
                        f"{self._where} trusted ancestor was rebound: {self.binding.path}"
                    )
                if _identity(os.fstat(current_file)) != self.binding.file_identity:
                    raise TrustedFileError(
                        f"{self._where} trusted file was rebound: {self.binding.path}"
                    )
            finally:
                os.close(current_file)
        finally:
            os.close(current_root)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        os.close(self._descriptor)
        os.close(self.root_descriptor)

    def __enter__(self) -> PinnedTrustedFile:
        self._require_open()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


class PinnedTrustedDirectory:
    """A directory descriptor retained with its complete named root lineage."""

    __slots__ = ("_closed", "_descriptor", "_where", "binding", "root_descriptor")

    def __init__(
        self,
        *,
        descriptor: int,
        root_descriptor: int,
        binding: TrustedDirectoryBinding,
        where: str,
    ) -> None:
        self._descriptor = descriptor
        self.root_descriptor = root_descriptor
        self.binding = binding
        self._where = where
        self._closed = False

    @property
    def descriptor(self) -> int:
        if self._closed:
            raise TrustedFileError(f"{self._where} trusted directory handle is closed")
        return self._descriptor

    def require_current(self) -> None:
        if self._closed:
            raise TrustedFileError(f"{self._where} trusted directory handle is closed")
        try:
            held_root = _directory_identity(os.fstat(self.root_descriptor))
            held_directory = _directory_identity(os.fstat(self._descriptor))
        except OSError as exc:
            raise TrustedFileError(
                f"{self._where} pinned directory descriptor is unreadable"
            ) from exc
        if held_root != self.binding.root_identity:
            raise TrustedFileError(f"{self._where} trusted root identity changed")
        if held_directory != self.binding.directory_identity:
            raise TrustedFileError(f"{self._where} trusted directory identity changed")

        current_root = _open_absolute_directory(self.binding.root, self._where)
        try:
            if _directory_identity(os.fstat(current_root)) != self.binding.root_identity:
                raise TrustedFileError(
                    f"{self._where} trusted root was rebound: {self.binding.root}"
                )
            current, ancestors = _open_relative_directory(
                current_root,
                Path(self.binding.relative),
                self.binding.path,
                self._where,
            )
            try:
                if ancestors != self.binding.ancestor_identities:
                    raise TrustedFileError(
                        f"{self._where} trusted ancestor was rebound: {self.binding.path}"
                    )
                if (
                    _directory_identity(os.fstat(current))
                    != self.binding.directory_identity
                ):
                    raise TrustedFileError(
                        f"{self._where} trusted directory was rebound: {self.binding.path}"
                    )
            finally:
                os.close(current)
        finally:
            os.close(current_root)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        os.close(self._descriptor)
        os.close(self.root_descriptor)

    def __enter__(self) -> PinnedTrustedDirectory:
        if self._closed:
            raise TrustedFileError(f"{self._where} trusted directory handle is closed")
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


def open_pinned_trusted_file(
    root: str | Path,
    path: str | Path,
    where: str,
    *,
    require_nonempty: bool = False,
) -> PinnedTrustedFile:
    """Pin a trusted root and one descendant regular file without following links."""

    trusted_root, target, relative = _normalize(root, path, where)
    root_descriptor = _open_absolute_directory(trusted_root, where)
    descriptor: int | None = None
    try:
        root_identity = _directory_identity(os.fstat(root_descriptor))
        descriptor, ancestors = _open_relative_file(
            root_descriptor,
            relative,
            target,
            where,
        )
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode) or (require_nonempty and observed.st_size <= 0):
            requirement = "non-empty real regular file" if require_nonempty else "real regular file"
            raise TrustedFileError(f"{where} must be a {requirement}: {target}")
        return PinnedTrustedFile(
            descriptor=descriptor,
            root_descriptor=root_descriptor,
            binding=TrustedFileBinding(
                root=trusted_root,
                path=target,
                relative=relative.as_posix(),
                root_identity=root_identity,
                ancestor_identities=ancestors,
                file_identity=_identity(observed),
            ),
            where=where,
        )
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        os.close(root_descriptor)
        raise


def open_pinned_trusted_directory(
    root: str | Path,
    path: str | Path,
    where: str,
) -> PinnedTrustedDirectory:
    """Pin one directory and every ancestor beneath an explicit trusted root."""

    trusted_root, target, relative = _normalize_directory(root, path, where)
    root_descriptor = _open_absolute_directory(trusted_root, where)
    descriptor: int | None = None
    try:
        root_identity = _directory_identity(os.fstat(root_descriptor))
        descriptor, ancestors = _open_relative_directory(
            root_descriptor,
            relative,
            target,
            where,
        )
        observed = os.fstat(descriptor)
        if not stat.S_ISDIR(observed.st_mode):
            raise TrustedFileError(f"{where} must be a real directory: {target}")
        return PinnedTrustedDirectory(
            descriptor=descriptor,
            root_descriptor=root_descriptor,
            binding=TrustedDirectoryBinding(
                root=trusted_root,
                path=target,
                relative=relative.as_posix(),
                root_identity=root_identity,
                ancestor_identities=ancestors,
                directory_identity=_directory_identity(observed),
            ),
            where=where,
        )
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        os.close(root_descriptor)
        raise


def bind_trusted_directory(
    root: str | Path,
    path: str | Path,
    where: str,
) -> TrustedDirectoryBinding:
    """Capture an exact current directory lineage without retaining descriptors."""

    with open_pinned_trusted_directory(root, path, where) as pinned:
        pinned.require_current()
        return pinned.binding


def read_trusted_file(
    root: str | Path,
    path: str | Path,
    where: str,
    *,
    require_nonempty: bool = False,
) -> TrustedFileSnapshot:
    """Read/hash one descendant from the same descriptor and retain its lineage."""

    with open_pinned_trusted_file(
        root,
        path,
        where,
        require_nonempty=require_nonempty,
    ) as pinned:
        payload, digest = pinned.read_and_hash()
        pinned.require_current()
        return TrustedFileSnapshot(payload, digest, pinned.binding)


def bind_trusted_file_absence(
    root: str | Path,
    path: str | Path,
    where: str,
) -> TrustedFileAbsenceBinding:
    """Bind an absent descendant without following or forgetting missing ancestors."""

    trusted_root, target, relative = _normalize(root, path, where)
    root_descriptor = _open_absolute_directory(trusted_root, where)
    current = os.dup(root_descriptor)
    identities: list[DirectoryIdentity] = []
    opened_parts: list[str] = []
    missing_parts: tuple[str, ...] | None = None
    try:
        root_identity = _directory_identity(os.fstat(root_descriptor))
        for index, part in enumerate(relative.parts[:-1]):
            try:
                following = os.open(part, _DIRECTORY_FLAGS, dir_fd=current)
            except FileNotFoundError:
                missing_parts = tuple(relative.parts[index:])
                break
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise TrustedFileError(
                        f"{where} must not contain symlink or non-directory "
                        f"path components: {target}"
                    ) from exc
                raise TrustedFileError(
                    f"{where} is unreadable beneath its trusted root: {target}"
                ) from exc
            os.close(current)
            current = following
            observed = os.fstat(current)
            if not stat.S_ISDIR(observed.st_mode):
                raise TrustedFileError(
                    f"{where} ancestor is not a real directory: {target.parent}"
                )
            identities.append(_directory_identity(observed))
            opened_parts.append(part)
        if missing_parts is None:
            try:
                os.stat(relative.name, dir_fd=current, follow_symlinks=False)
            except FileNotFoundError:
                missing_parts = (relative.name,)
            except OSError as exc:
                raise TrustedFileError(
                    f"{where} absence is unreadable beneath its trusted root: {target}"
                ) from exc
            else:
                raise TrustedFileError(f"{where} must be absent: {target}")

        current_identity = _directory_identity(os.fstat(current))
        existing_path = trusted_root.joinpath(*opened_parts)
        existing_relative = (
            Path(*opened_parts).as_posix() if opened_parts else "."
        )
        parent_binding = TrustedDirectoryBinding(
            root=trusted_root,
            path=existing_path,
            relative=existing_relative,
            root_identity=root_identity,
            ancestor_identities=tuple(identities[:-1]),
            directory_identity=current_identity,
        )
        binding = TrustedFileAbsenceBinding(
            root=trusted_root,
            path=target,
            relative=relative.as_posix(),
            existing_parent=parent_binding,
            missing_parts=missing_parts,
        )
    finally:
        os.close(current)
        os.close(root_descriptor)
    require_trusted_directory_unchanged(parent_binding, f"{where} existing parent")
    return binding


def require_trusted_file_absent(
    binding: TrustedFileAbsenceBinding,
    where: str,
) -> None:
    """Require the same absent suffix beneath the same real ancestor lineage."""

    if not isinstance(binding, TrustedFileAbsenceBinding):
        raise TrustedFileError(f"{where} requires a typed trusted absence binding")
    current = bind_trusted_file_absence(binding.root, binding.path, where)
    if current != binding:
        raise TrustedFileError(
            f"{where} trusted absent path changed before publication: {binding.path}"
        )


def require_trusted_file_unchanged(
    binding: TrustedFileBinding,
    where: str,
) -> None:
    """Reopen a retained binding and require the exact same root lineage and leaf."""

    if not isinstance(binding, TrustedFileBinding):
        raise TrustedFileError(f"{where} requires a typed trusted-file binding")
    with open_pinned_trusted_file(binding.root, binding.path, where) as pinned:
        if pinned.binding != binding:
            raise TrustedFileError(
                f"{where} trusted path changed before publication: {binding.path}"
            )


def require_trusted_directory_unchanged(
    binding: TrustedDirectoryBinding,
    where: str,
) -> None:
    """Require the exact same named root, ancestor lineage, and directory inode."""

    if not isinstance(binding, TrustedDirectoryBinding):
        raise TrustedFileError(f"{where} requires a typed trusted-directory binding")
    with open_pinned_trusted_directory(binding.root, binding.path, where) as pinned:
        if pinned.binding != binding:
            raise TrustedFileError(
                f"{where} trusted directory changed before publication: {binding.path}"
            )
