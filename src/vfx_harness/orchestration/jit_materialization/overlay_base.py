"""Immutable authority identity for unpublished rematerialization overlays."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from vfx_harness.orchestration import plan_bundle_integrity
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionConflict,
    AuthoritySelectionToken,
    durable_replace_file_bytes,
)
from vfx_harness.orchestration.jit_materialization.errors import (
    MaterializationSelectionConflict,
)

OVERLAY_BASE_SCHEMA = "vfx-harness.jit-overlay-base/v1"
OVERLAY_BASE_FILE = ".authority-base.json"
_OVERLAY_BASE_FIELDS = frozenset({"schema", "bundle_hash", "base_selection"})


def overlay_base_bytes(
    *,
    bundle_hash: str,
    base_selection: AuthoritySelectionToken,
) -> bytes:
    """Serialize the immutable base record stored with an unpublished overlay."""

    value = {
        "schema": OVERLAY_BASE_SCHEMA,
        "bundle_hash": bundle_hash,
        "base_selection": base_selection.to_dict(),
    }
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def write_overlay_base(
    overlay_root: Path,
    *,
    bundle_hash: str,
    base_selection: AuthoritySelectionToken,
) -> None:
    """Write one immutable base record into its token-addressed overlay directory."""

    payload = overlay_base_bytes(
        bundle_hash=bundle_hash,
        base_selection=base_selection,
    )
    target = overlay_root / OVERLAY_BASE_FILE
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_file() or target.read_bytes() != payload:
            raise MaterializationSelectionConflict(
                "content-addressed materialization overlay has conflicting base authority"
            )
        return
    durable_replace_file_bytes(overlay_root, target, payload)


def overlay_store_digest(
    *,
    view_hash: str,
    bundle_hash: str,
    base_selection: AuthoritySelectionToken,
) -> str:
    """Identify an unpublished overlay by content and its exact authority base."""

    payload = json.dumps(
        {
            "schema": OVERLAY_BASE_SCHEMA,
            "view_hash": view_hash,
            "bundle_hash": bundle_hash,
            "base_selection": base_selection.to_dict(),
        },
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def read_overlay_base(
    overlay_root: str | Path,
) -> tuple[str, AuthoritySelectionToken]:
    """Read one strict immutable overlay base record."""

    root = Path(overlay_root)
    path = root / OVERLAY_BASE_FILE
    if path.is_symlink() or not path.is_file():
        raise MaterializationSelectionConflict(
            "unpublished materialization overlay has no verified base-selection record"
        )
    try:
        value = plan_bundle_integrity.decode_json_object(
            path.read_bytes(),
            "materialization overlay base",
        )
        if (
            set(value) != _OVERLAY_BASE_FIELDS
            or value.get("schema") != OVERLAY_BASE_SCHEMA
        ):
            raise ValueError("materialization overlay base fields/schema are invalid")
        bundle_hash = str(value.get("bundle_hash") or "")
        if len(bundle_hash) != 64 or any(
            character not in "0123456789abcdef" for character in bundle_hash
        ):
            raise ValueError("materialization overlay base bundle_hash is invalid")
        token = AuthoritySelectionToken.from_dict(
            value.get("base_selection"),
            "materialization overlay base.base_selection",
        )
    except (AuthoritySelectionConflict, ValueError) as exc:
        raise MaterializationSelectionConflict(str(exc)) from exc
    return bundle_hash, token
