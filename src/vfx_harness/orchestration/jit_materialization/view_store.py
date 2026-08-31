"""Crash-durable immutable storage for content-addressed JIT views."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

from vfx_harness.domain.authority_head_records import JIT_STATE_DIR
from vfx_harness.orchestration import plan_bundle_integrity
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionConflict,
    authority_selection_lock,
    durably_ensure_real_directory,
)
from vfx_harness.orchestration.jit_materialization.errors import (
    MaterializationSelectionConflict,
)

_VIEW_STORE = JIT_STATE_DIR / "views"


def _normalized_inputs(
    shot_folder: str | Path,
    view_root: str | Path,
    members: Mapping[str, bytes],
) -> tuple[Path, Path, dict[str, bytes]]:
    shot = Path(os.path.abspath(Path(shot_folder).expanduser()))
    root = Path(os.path.abspath(Path(view_root).expanduser()))
    try:
        relative = root.relative_to(shot)
    except ValueError as exc:
        raise MaterializationSelectionConflict(
            "materialized view durability root escapes the shot"
        ) from exc
    expected_prefix = _VIEW_STORE.parts
    if (
        len(relative.parts) != len(expected_prefix) + 1
        or relative.parts[: len(expected_prefix)] != expected_prefix
    ):
        raise MaterializationSelectionConflict(
            "materialized view durability root must be an immediate child of "
            "state/jit-layers/views"
        )
    digest = relative.name
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise MaterializationSelectionConflict(
            "materialized view store name must be a lowercase SHA-256 digest"
        )
    if not isinstance(members, Mapping) or not members:
        raise MaterializationSelectionConflict(
            "materialized view durability requires at least one member"
        )
    normalized: dict[str, bytes] = {}
    for raw_name, payload in members.items():
        if not isinstance(raw_name, str):
            raise MaterializationSelectionConflict(
                "materialized view member names must be strings"
            )
        name = PurePosixPath(raw_name)
        if (
            name.is_absolute()
            or len(name.parts) != 1
            or name.as_posix() != raw_name
            or raw_name in {"", ".", ".."}
        ):
            raise MaterializationSelectionConflict(
                "materialized view members must be normalized flat filenames"
            )
        if not isinstance(payload, bytes):
            raise MaterializationSelectionConflict(
                f"materialized view member {raw_name!r} must be bytes"
            )
        normalized[raw_name] = payload
    return shot, root, normalized


def _verify_members(
    shot: Path,
    root: Path,
    expected: Mapping[str, bytes],
) -> None:
    plan_bundle_integrity.require_real_directory(
        shot,
        root,
        "materialized view store",
    )
    try:
        children = tuple(root.iterdir())
    except OSError as exc:
        raise MaterializationSelectionConflict(
            f"materialized view store is unreadable: {root}"
        ) from exc
    found = {child.name for child in children}
    if found != set(expected):
        raise MaterializationSelectionConflict(
            "content-addressed materialized view members conflict; "
            f"missing={sorted(set(expected) - found)}; "
            f"unexpected={sorted(found - set(expected))}"
        )
    for child in children:
        if child.is_symlink() or not child.is_file():
            raise MaterializationSelectionConflict(
                f"materialized view member must be a real regular file: {child.name}"
            )
        observed = plan_bundle_integrity.read_real_file(
            root,
            child,
            f"materialized view member {child.name}",
        )
        if observed != expected[child.name]:
            raise MaterializationSelectionConflict(
                f"content-addressed materialized view conflicts at {child.name}"
            )


def durably_install_or_flush_view_directory(
    shot_folder: str | Path,
    view_root: str | Path,
    members: Mapping[str, bytes],
) -> Path:
    """Install or reflush one complete immutable view before selection.

    Installation writes and flushes every member in a private sibling directory,
    flushes that directory, atomically renames it to the content address, then flushes
    the rename parent and every ancestor through the shot root.  A retry that finds an
    orphan left after rename verifies its exact bytes and repeats the complete flush
    barrier.  The shared authority lock serializes equal-address installers; it does
    not authorize selection, which remains a later exact-token CAS.
    """

    shot, root, normalized = _normalized_inputs(shot_folder, view_root, members)
    try:
        with authority_selection_lock(shot, exclusive=True):
            durably_ensure_real_directory(shot, root.parent)
            if root.exists() or root.is_symlink():
                _verify_members(shot, root, normalized)
                plan_bundle_integrity.durably_flush_bundle_directory(shot, root)
            else:
                plan_bundle_integrity.durably_install_bundle_directory(
                    shot,
                    root,
                    normalized,
                )
                _verify_members(shot, root, normalized)
    except MaterializationSelectionConflict:
        raise
    except (AuthoritySelectionConflict, plan_bundle_integrity.PlanPublicationError, OSError) as exc:
        raise MaterializationSelectionConflict(
            f"could not durably install or flush materialized view: {root}"
        ) from exc
    return root
