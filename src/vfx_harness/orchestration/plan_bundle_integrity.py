"""Filesystem integrity primitives for immutable plan bundles.

This leaf module owns byte, membership, and path verification only.  Plan selection,
publication policy, and authored-input freshness remain in ``plan_authority``.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from collections.abc import Callable, Collection
from pathlib import Path, PurePosixPath
from typing import Any

_BUNDLE_FIELDS = frozenset({"schema", "run_id", "content_hash", "outcome", "artifacts"})
_READ_FILE_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_READ_DIRECTORY_FLAGS = (
    _READ_FILE_FLAGS
    | getattr(os, "O_DIRECTORY", 0)
)
_CREATE_FILE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)


class PlanPublicationError(RuntimeError):
    """Plan authority is missing, conflicting, or corrupt."""


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def bundle_hash(payloads: dict[str, bytes]) -> str:
    value = hashlib.sha256()
    for name in sorted(payloads):
        value.update(name.encode("utf-8"))
        value.update(b"\0")
        value.update(digest(payloads[name]).encode("ascii"))
        value.update(b"\n")
    return value.hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise PlanPublicationError(f"plan authority contains duplicate JSON key {key!r}")
        value[key] = item
    return value


def decode_json_object(raw: bytes, where: str) -> dict[str, Any]:
    try:
        value = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlanPublicationError(f"{where} is not readable UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise PlanPublicationError(f"{where} must contain a JSON object")
    return value


def _canonical_json_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _relative_under(anchor: Path, path: Path, where: str) -> Path:
    try:
        relative = path.relative_to(anchor)
    except ValueError as exc:
        raise PlanPublicationError(f"{where} escapes {anchor}") from exc
    if relative == Path("."):
        raise PlanPublicationError(f"{where} must not replace its authority root")
    return relative


def _reject_symlink_components(anchor: Path, path: Path, where: str) -> None:
    relative = _relative_under(anchor, path, where)
    current = anchor
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise PlanPublicationError(f"{where} must not use symlink path components: {current}")


def read_real_file(anchor: Path, path: Path, where: str) -> bytes:
    _reject_symlink_components(anchor, path, where)
    if not path.is_file():
        raise PlanPublicationError(f"{where} is missing or not a regular file: {path}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise PlanPublicationError(f"{where} is unreadable: {path}") from exc


def require_real_directory(anchor: Path, path: Path, where: str) -> None:
    _reject_symlink_components(anchor, path, where)
    if not path.is_dir():
        raise PlanPublicationError(f"{where} is missing or unreadable or not a directory: {path}")


def regular_files_under(anchor: Path, directory: Path, where: str) -> tuple[Path, ...]:
    """Enumerate one real directory tree while refusing links and special members."""

    require_real_directory(anchor, directory, where)
    files: list[Path] = []
    pending = [directory]
    while pending:
        current = pending.pop()
        try:
            children = tuple(current.iterdir())
        except OSError as exc:
            raise PlanPublicationError(f"{where} is unreadable: {current}") from exc
        for child in children:
            if child.is_symlink():
                raise PlanPublicationError(
                    f"{where} must not use symlink path components: {child}"
                )
            if child.is_dir():
                pending.append(child)
            elif child.is_file():
                files.append(child)
            else:
                raise PlanPublicationError(f"{where} member is not a regular file: {child}")
    return tuple(sorted(files))


def ensure_real_directories(anchor: Path, path: Path, where: str) -> None:
    """Create a descendant one component at a time without following symlinks."""

    relative = _relative_under(anchor, path, where)
    current = anchor
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise PlanPublicationError(f"{where} must not use symlink path components: {current}")
        if current.exists():
            if not current.is_dir():
                raise PlanPublicationError(f"{where} component is not a directory: {current}")
            continue
        current.mkdir()


def read_json_object(anchor: Path, path: Path, where: str) -> tuple[dict[str, Any], bytes]:
    raw = read_real_file(anchor, path, where)
    return decode_json_object(raw, where), raw


def read_schema_object(
    anchor: Path,
    path: Path,
    where: str,
    *,
    schema: str,
    fields: Collection[str],
) -> dict[str, Any]:
    """Read one duplicate-free exact-field schema record from a real file."""

    record, _raw = read_json_object(anchor, path, where)
    expected = frozenset(fields)
    found = set(record)
    if found != expected:
        raise PlanPublicationError(
            f"{where} fields mismatch; missing={sorted(expected - found)}; "
            f"unexpected={sorted(found - expected)}"
        )
    if record["schema"] != schema:
        raise PlanPublicationError(f"unsupported {where} schema: {record['schema']!r}")
    return record


def _validate_manifest(
    manifest: dict[str, Any],
    *,
    schema: str,
    publishable_outcomes: Collection[str],
    supports_artifact: Callable[[str], bool],
    where: str,
) -> dict[str, str]:
    found = set(manifest)
    if found != _BUNDLE_FIELDS:
        raise PlanPublicationError(
            f"{where} fields mismatch; missing={sorted(_BUNDLE_FIELDS - found)}; "
            f"unexpected={sorted(found - _BUNDLE_FIELDS)}"
        )
    if manifest["schema"] != schema:
        raise PlanPublicationError(f"unsupported plan bundle schema: {manifest['schema']!r}")
    run_id = manifest["run_id"]
    if (
        not isinstance(run_id, str)
        or not run_id
        or run_id in {".", ".."}
        or "/" in run_id
        or "\\" in run_id
    ):
        raise PlanPublicationError(f"{where}.run_id is invalid: {run_id!r}")
    content_hash = manifest["content_hash"]
    if not is_digest(content_hash):
        raise PlanPublicationError(f"{where}.content_hash must be a lowercase SHA-256 digest")
    outcome = manifest["outcome"]
    if outcome not in publishable_outcomes:
        raise PlanPublicationError(f"{where}.outcome is not publishable: {outcome!r}")
    artifacts = manifest["artifacts"]
    if not isinstance(artifacts, dict) or not artifacts:
        raise PlanPublicationError("plan bundle carries no artifact manifest")
    artifact_hashes: dict[str, str] = {}
    for name, artifact_hash in artifacts.items():
        if not isinstance(name, str) or not supports_artifact(name):
            raise PlanPublicationError(f"plan bundle declares unsupported artifact: {name!r}")
        if not is_digest(artifact_hash):
            raise PlanPublicationError(
                f"plan bundle artifact hash must be a lowercase SHA-256 digest: {name}"
            )
        artifact_hashes[name] = artifact_hash
    return artifact_hashes


def _expected_directories(artifacts: dict[str, str]) -> set[str]:
    directories: set[str] = set()
    for name in artifacts:
        parent = Path(name).parent
        while parent != Path("."):
            directories.add(parent.as_posix())
            parent = parent.parent
    return directories


def _stored_members(root: Path) -> tuple[set[str], set[str]]:
    files: set[str] = set()
    directories: set[str] = set()
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            children = tuple(directory.iterdir())
        except OSError as exc:
            raise PlanPublicationError(f"plan bundle directory is unreadable: {directory}") from exc
        for child in children:
            relative = child.relative_to(root).as_posix()
            if child.is_symlink():
                raise PlanPublicationError(
                    f"plan bundle must not use symlink path components: {relative}"
                )
            if child.is_dir():
                directories.add(relative)
                pending.append(child)
            elif child.is_file():
                files.add(relative)
            else:
                raise PlanPublicationError(
                    f"plan bundle member is not a regular file or directory: {relative}"
                )
    return files, directories


def _normalized_member_name(name: object) -> str:
    if not isinstance(name, str) or not name or "\\" in name:
        raise PlanPublicationError("plan bundle member names must be normalized relative paths")
    relative = PurePosixPath(name)
    if (
        relative.is_absolute()
        or relative.as_posix() != name
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise PlanPublicationError("plan bundle member names must be normalized relative paths")
    return name


def _write_new_regular_file(path: Path, payload: bytes) -> None:
    descriptor: int | None = None
    try:
        descriptor = os.open(path, _CREATE_FILE_FLAGS, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = None
            handle.write(payload)
            handle.flush()
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _fsync_regular_file(path: Path) -> None:
    descriptor = os.open(path, _READ_FILE_FLAGS)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise PlanPublicationError(
                f"durable plan bundle member is not a regular file: {path}"
            )
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, _READ_DIRECTORY_FLAGS)
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise PlanPublicationError(
                f"durable plan bundle component is not a directory: {path}"
            )
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ancestor_directories(anchor: Path, start: Path) -> tuple[Path, ...]:
    try:
        start.relative_to(anchor)
    except ValueError as exc:
        raise PlanPublicationError("plan bundle durability root escapes the shot") from exc
    directories: list[Path] = []
    current = start
    while True:
        directories.append(current)
        if current == anchor:
            return tuple(directories)
        current = current.parent


def _nested_directory_order(root: Path, names: Collection[str]) -> tuple[Path, ...]:
    directories = {
        parent
        for name in names
        for parent in Path(name).parents
        if parent != Path(".")
    }
    ordered = sorted(
        directories,
        key=lambda relative: (-len(relative.parts), relative.as_posix()),
    )
    return (*(root / relative for relative in ordered), root)


def durably_install_bundle_directory(
    anchor: Path,
    root: Path,
    members: dict[str, bytes],
) -> None:
    """Install one absent immutable bundle before any pointer may select it.

    Every regular member is flushed in lexical order, every containing directory is
    flushed deepest-first, and the atomic rename is followed by a leaf-to-anchor directory
    barrier.  Therefore a returned call proves the bundle is durably reachable from the shot,
    not merely visible in the page cache.
    """

    anchor = Path(os.path.abspath(anchor))
    root = Path(os.path.abspath(root))
    _relative_under(anchor, root, "plan bundle durability root")
    require_real_directory(anchor, root.parent, "plan bundle durability parent")
    if root.exists() or root.is_symlink():
        raise PlanPublicationError(f"durable plan bundle target already exists: {root}")
    if not isinstance(members, dict) or not members:
        raise PlanPublicationError("durable plan bundle requires at least one member")
    normalized: dict[str, bytes] = {}
    for raw_name, payload in members.items():
        name = _normalized_member_name(raw_name)
        if not isinstance(payload, bytes):
            raise PlanPublicationError(
                f"durable plan bundle member {name!r} must be bytes"
            )
        normalized[name] = payload

    temp = Path(tempfile.mkdtemp(prefix=f".{root.name}.tmp-", dir=root.parent))
    try:
        directories = {
            parent
            for name in normalized
            for parent in Path(name).parents
            if parent != Path(".")
        }
        for relative in sorted(
            directories,
            key=lambda item: (len(item.parts), item.as_posix()),
        ):
            (temp / relative).mkdir()
        for name in sorted(normalized):
            target = temp / name
            _write_new_regular_file(target, normalized[name])
            _fsync_regular_file(target)
        for directory in _nested_directory_order(temp, normalized):
            _fsync_directory(directory)
        os.replace(temp, root)
        for directory in _ancestor_directories(anchor, root.parent):
            _fsync_directory(directory)
    except PlanPublicationError:
        raise
    except OSError as exc:
        raise PlanPublicationError(
            f"could not durably install plan bundle: {root}"
        ) from exc
    finally:
        shutil.rmtree(temp, ignore_errors=True)


def durably_flush_bundle_directory(anchor: Path, root: Path) -> None:
    """Re-establish the durability barrier for a verified pre-existing bundle."""

    anchor = Path(os.path.abspath(anchor))
    root = Path(os.path.abspath(root))
    _relative_under(anchor, root, "plan bundle durability root")
    try:
        files, directories = _stored_members(root)
        for name in sorted(files):
            _fsync_regular_file(root / name)
        for name in sorted(
            directories,
            key=lambda item: (-len(Path(item).parts), item),
        ):
            _fsync_directory(root / name)
        _fsync_directory(root)
        for directory in _ancestor_directories(anchor, root.parent):
            _fsync_directory(directory)
    except PlanPublicationError:
        raise
    except OSError as exc:
        raise PlanPublicationError(
            f"could not durably flush plan bundle: {root}"
        ) from exc


def verify_bundle_root(
    shot: Path,
    root: Path,
    *,
    schema: str,
    publishable_outcomes: Collection[str],
    supports_artifact: Callable[[str], bool],
    expected_manifest: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Verify every path, byte, member, and hash in one immutable bundle root."""

    require_real_directory(shot, root, "plan bundle root")
    relative = _relative_under(shot, root, "plan bundle root")
    if len(relative.parts) != 6 or relative.parts[0] != "runs" or relative.parts[2:5] != (
        "checkpoints",
        "plans",
        "bundles",
    ):
        raise PlanPublicationError(f"plan bundle root has a non-authoritative path: {root}")

    manifest, manifest_raw = read_json_object(root, root / "bundle.json", "plan bundle manifest")
    if manifest_raw != _canonical_json_bytes(manifest):
        raise PlanPublicationError("plan bundle manifest bytes are not canonical")
    artifacts = _validate_manifest(
        manifest,
        schema=schema,
        publishable_outcomes=publishable_outcomes,
        supports_artifact=supports_artifact,
        where="plan bundle manifest",
    )
    if expected_manifest is not None and manifest != expected_manifest:
        raise PlanPublicationError(f"immutable plan bundle conflicts with candidate: {root}")
    if manifest["run_id"] != relative.parts[1]:
        raise PlanPublicationError("plan bundle manifest and path run ids disagree")
    if manifest["content_hash"] != relative.parts[5]:
        raise PlanPublicationError("plan bundle manifest and path content hashes disagree")

    files, directories = _stored_members(root)
    expected_files = set(artifacts) | {"bundle.json"}
    expected_directories = _expected_directories(artifacts)
    if files != expected_files or directories != expected_directories:
        raise PlanPublicationError(
            "plan bundle member set disagrees with its manifest; "
            f"missing_files={sorted(expected_files - files)}; "
            f"unexpected_files={sorted(files - expected_files)}; "
            f"missing_directories={sorted(expected_directories - directories)}; "
            f"unexpected_directories={sorted(directories - expected_directories)}"
        )

    payloads: dict[str, bytes] = {}
    for name, expected_hash in artifacts.items():
        data = read_real_file(root, root / name, f"plan bundle artifact {name}")
        if digest(data) != expected_hash:
            raise PlanPublicationError(f"plan bundle artifact hash mismatch: {name}")
        payloads[name] = data
    if bundle_hash(payloads) != manifest["content_hash"]:
        raise PlanPublicationError("plan bundle aggregate hash mismatch")
    return manifest, payloads
