"""Crash-durable immutable objects and pointers for authority-state transitions.

The selected plan/JIT pair is still owned by the HIR-0168 selection transaction.
This store supplies the second half of HIR-0171: content-addressed transition records
plus one pending WAL pointer and one evaluated-current pointer.  It deliberately does
not decide whether a transition is legal; domain records and the independent evaluator
own that judgment.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from vfx_harness.domain.authority_head_records import (
    AuthorityHeadRecordError,
    canonical_json_bytes,
    decode_canonical_json_object,
)
from vfx_harness.orchestration import plan_bundle_integrity
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionConflict,
    durable_remove_pointer,
    durable_replace_pointer_bytes,
    durably_ensure_real_directory,
    read_optional_pointer_bytes,
)

AUTHORITY_STATE_DIR = Path("state/authority-state")
AUTHORITY_STATE_OBJECTS_DIR = AUTHORITY_STATE_DIR / "objects"
AUTHORITY_STATE_CURRENT = AUTHORITY_STATE_DIR / "current.json"
AUTHORITY_STATE_PENDING = AUTHORITY_STATE_DIR / "pending.json"
_OBJECT_MEMBER = "record.json"


class AuthorityStateStoreError(ValueError):
    """Authority-state bytes are missing, mutable, ambiguous, or path-unsafe."""


@dataclass(frozen=True, slots=True)
class StoredAuthorityStateObject:
    """Exact immutable bytes installed below the authority-state object store."""

    locator: str
    sha256: str
    payload: bytes


def _shot_path(shot_folder: str | Path) -> Path:
    shot = Path(os.path.abspath(Path(shot_folder).expanduser()))
    if not shot.is_dir() or shot.is_symlink():
        raise AuthorityStateStoreError(
            f"authority-state shot root must be an existing real directory: {shot}"
        )
    return shot


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _object_locator(digest: str) -> str:
    return (AUTHORITY_STATE_OBJECTS_DIR / digest / _OBJECT_MEMBER).as_posix()


def authority_state_object_locator(sha256: str) -> str:
    """Return the sole canonical locator for one validated object byte digest."""

    return _object_locator(_require_digest(sha256, "authority-state object sha256"))


def _require_digest(value: object, where: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise AuthorityStateStoreError(f"{where} must be a lowercase SHA-256 digest")
    return value


def _require_object_locator(locator: object, sha256: str, where: str) -> str:
    if not isinstance(locator, str):
        raise AuthorityStateStoreError(f"{where} must be a string")
    relative = PurePosixPath(locator)
    expected = _object_locator(sha256)
    if (
        relative.is_absolute()
        or relative.as_posix() != locator
        or any(part in {"", ".", ".."} for part in relative.parts)
        or locator != expected
    ):
        raise AuthorityStateStoreError(
            f"{where} must name the exact content-addressed object {expected!r}"
        )
    return locator


def _verify_object_directory(
    shot: Path,
    root: Path,
    payload: bytes,
) -> StoredAuthorityStateObject:
    try:
        plan_bundle_integrity.require_real_directory(
            shot,
            root,
            "authority-state immutable object",
        )
        children = tuple(root.iterdir())
    except (OSError, plan_bundle_integrity.PlanPublicationError) as exc:
        raise AuthorityStateStoreError(str(exc)) from exc
    if len(children) != 1 or children[0].name != _OBJECT_MEMBER:
        raise AuthorityStateStoreError(
            "authority-state immutable object must contain exactly record.json"
        )
    member = children[0]
    if member.is_symlink() or not member.is_file():
        raise AuthorityStateStoreError(
            "authority-state immutable object member must be a real regular file"
        )
    try:
        observed = plan_bundle_integrity.read_real_file(
            root,
            member,
            "authority-state immutable object member",
        )
    except (OSError, plan_bundle_integrity.PlanPublicationError) as exc:
        raise AuthorityStateStoreError(str(exc)) from exc
    if observed != payload:
        raise AuthorityStateStoreError(
            "content-addressed authority-state object bytes conflict"
        )
    digest = _sha256(payload)
    return StoredAuthorityStateObject(
        locator=_object_locator(digest),
        sha256=digest,
        payload=payload,
    )


def install_authority_state_bytes(
    shot_folder: str | Path,
    payload: bytes,
) -> StoredAuthorityStateObject:
    """Install or reflush one exact immutable byte stream.

    The caller owns transition serialization.  Installation itself is idempotent and
    content addressed; a same-address byte conflict or path substitution fails closed.
    """

    if not isinstance(payload, bytes) or not payload:
        raise AuthorityStateStoreError(
            "authority-state immutable object payload must be non-empty bytes"
        )
    shot = _shot_path(shot_folder)
    digest = _sha256(payload)
    root = shot / AUTHORITY_STATE_OBJECTS_DIR / digest
    try:
        durably_ensure_real_directory(shot, root.parent)
        if root.exists() or root.is_symlink():
            stored = _verify_object_directory(shot, root, payload)
            plan_bundle_integrity.durably_flush_bundle_directory(shot, root)
            return stored
        plan_bundle_integrity.durably_install_bundle_directory(
            shot,
            root,
            {_OBJECT_MEMBER: payload},
        )
        return _verify_object_directory(shot, root, payload)
    except AuthorityStateStoreError:
        raise
    except (
        AuthoritySelectionConflict,
        OSError,
        plan_bundle_integrity.PlanPublicationError,
    ) as exc:
        raise AuthorityStateStoreError(
            f"could not install authority-state immutable object {digest}: {exc}"
        ) from exc


def install_authority_state_record(
    shot_folder: str | Path,
    record: Mapping[str, Any],
) -> StoredAuthorityStateObject:
    """Canonically encode and install one immutable JSON-object record."""

    try:
        payload = canonical_json_bytes(record)
    except AuthorityHeadRecordError as exc:
        raise AuthorityStateStoreError(str(exc)) from exc
    return install_authority_state_bytes(shot_folder, payload)


def read_authority_state_bytes(
    shot_folder: str | Path,
    *,
    locator: str,
    sha256: str,
) -> StoredAuthorityStateObject:
    """Read one exact object through its content-addressed locator."""

    shot = _shot_path(shot_folder)
    digest = _require_digest(sha256, "authority-state object sha256")
    relative = _require_object_locator(
        locator,
        digest,
        "authority-state object locator",
    )
    root = shot / Path(relative).parent
    try:
        payload = plan_bundle_integrity.read_real_file(
            root,
            shot / relative,
            "authority-state immutable object",
        )
    except (OSError, plan_bundle_integrity.PlanPublicationError) as exc:
        raise AuthorityStateStoreError(str(exc)) from exc
    if _sha256(payload) != digest:
        raise AuthorityStateStoreError(
            "authority-state immutable object bytes do not match their locator"
        )
    return _verify_object_directory(shot, root, payload)


def read_authority_state_record(
    shot_folder: str | Path,
    *,
    locator: str,
    sha256: str,
) -> tuple[dict[str, Any], StoredAuthorityStateObject]:
    """Read and strictly decode one immutable canonical JSON object."""

    stored = read_authority_state_bytes(
        shot_folder,
        locator=locator,
        sha256=sha256,
    )
    try:
        record = decode_canonical_json_object(
            stored.payload,
            "authority-state immutable record",
        )
    except AuthorityHeadRecordError as exc:
        raise AuthorityStateStoreError(str(exc)) from exc
    return record, stored


def _read_pointer_bytes(
    shot_folder: str | Path,
    path: Path,
) -> bytes | None:
    try:
        return read_optional_pointer_bytes(
            shot_folder,
            Path(shot_folder) / path,
        )
    except AuthoritySelectionConflict as exc:
        raise AuthorityStateStoreError(str(exc)) from exc


def read_pending_bytes(shot_folder: str | Path) -> bytes | None:
    """Return the exact pending WAL pointer bytes, if selected."""

    return _read_pointer_bytes(shot_folder, AUTHORITY_STATE_PENDING)


def read_current_bytes(shot_folder: str | Path) -> bytes | None:
    """Return the exact evaluated coordinator-head pointer bytes, if selected."""

    return _read_pointer_bytes(shot_folder, AUTHORITY_STATE_CURRENT)


def decode_pointer_bytes(payload: bytes, where: str) -> dict[str, Any]:
    """Decode pointer bytes through the shared canonical/duplicate-safe parser."""

    try:
        return decode_canonical_json_object(payload, where)
    except AuthorityHeadRecordError as exc:
        raise AuthorityStateStoreError(str(exc)) from exc


def require_no_pending_authority_state(shot_folder: str | Path) -> None:
    """Fail every ordinary authority reader while a WAL intent is selected."""

    if read_pending_bytes(shot_folder) is not None:
        raise AuthorityStateStoreError(
            "authority-state transition is pending; run deterministic recovery before "
            "reading or mutating selected authority"
        )


def replace_pending_bytes(shot_folder: str | Path, payload: bytes) -> None:
    """Select one already-installed immutable transition intent as the WAL."""

    try:
        durably_ensure_real_directory(shot_folder, AUTHORITY_STATE_DIR)
        durable_replace_pointer_bytes(
            shot_folder,
            Path(shot_folder) / AUTHORITY_STATE_PENDING,
            payload,
        )
    except AuthoritySelectionConflict as exc:
        raise AuthorityStateStoreError(str(exc)) from exc


def replace_current_bytes(shot_folder: str | Path, payload: bytes) -> None:
    """Select one independently evaluated coordinator head."""

    try:
        durably_ensure_real_directory(shot_folder, AUTHORITY_STATE_DIR)
        durable_replace_pointer_bytes(
            shot_folder,
            Path(shot_folder) / AUTHORITY_STATE_CURRENT,
            payload,
        )
    except AuthoritySelectionConflict as exc:
        raise AuthorityStateStoreError(str(exc)) from exc


def remove_pending(shot_folder: str | Path) -> None:
    """Durably clear the WAL only after current head publication is verified."""

    try:
        durable_remove_pointer(
            shot_folder,
            Path(shot_folder) / AUTHORITY_STATE_PENDING,
        )
    except AuthoritySelectionConflict as exc:
        raise AuthorityStateStoreError(str(exc)) from exc


__all__ = [
    "AUTHORITY_STATE_CURRENT",
    "AUTHORITY_STATE_DIR",
    "AUTHORITY_STATE_OBJECTS_DIR",
    "AUTHORITY_STATE_PENDING",
    "AuthorityStateStoreError",
    "StoredAuthorityStateObject",
    "authority_state_object_locator",
    "decode_pointer_bytes",
    "install_authority_state_bytes",
    "install_authority_state_record",
    "read_authority_state_bytes",
    "read_authority_state_record",
    "read_current_bytes",
    "read_pending_bytes",
    "remove_pending",
    "replace_current_bytes",
    "replace_pending_bytes",
    "require_no_pending_authority_state",
]
