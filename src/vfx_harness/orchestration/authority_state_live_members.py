"""Strict live work-unit-state namespace checks for HIR-0171.

The authority-selection lock makes sanctioned directory membership stable.  These
helpers deliberately reject every unrecognised member instead of inferring authority
from a filename or silently overlooking a state file that an intent did not bind.
"""

from __future__ import annotations

import stat
from collections.abc import Iterable
from pathlib import Path

from vfx_harness.orchestration.unit_state_lock import (
    state_namespace_entries,
    unit_state_path,
)


class AuthorityStateLiveMemberError(ValueError):
    """The live work-unit-state namespace is unsafe or incomplete."""


def _layer_id_from_name(name: str, *, lock: bool) -> str | None:
    suffix = ".json.lock" if lock else ".json"
    if not name.startswith("layer_") or not name.endswith(suffix):
        return None
    layer_id = name[len("layer_") : -len(suffix)]
    if not layer_id:
        return None
    try:
        expected = unit_state_path(Path("/shot"), layer_id).name
    except ValueError:
        return None
    return layer_id if expected == f"layer_{layer_id}.json" else None


def authority_state_layer_ids(shot_folder: str | Path) -> tuple[str, ...]:
    """Enumerate the sole supported live state members, rejecting everything else."""

    layer_ids: list[str] = []
    try:
        entries = state_namespace_entries(shot_folder)
    except ValueError as exc:
        raise AuthorityStateLiveMemberError(str(exc)) from exc
    for name, mode in entries:
        lock_layer_id = _layer_id_from_name(name, lock=True)
        if lock_layer_id is not None:
            if not stat.S_ISREG(mode):
                raise AuthorityStateLiveMemberError(
                    f"work-unit state lock must be a regular file: {name}"
                )
            continue
        layer_id = _layer_id_from_name(name, lock=False)
        if layer_id is None or not stat.S_ISREG(mode):
            raise AuthorityStateLiveMemberError(
                f"unknown or unsafe work-unit state member: {name}"
            )
        layer_ids.append(layer_id)
    if len(layer_ids) != len(set(layer_ids)):  # pragma: no cover - filenames are unique
        raise AuthorityStateLiveMemberError(
            "work-unit state layer ids are ambiguous"
        )
    return tuple(layer_ids)


def require_live_state_namespace(
    shot_folder: str | Path,
    *,
    required_layer_ids: Iterable[str],
    allowed_layer_ids: Iterable[str] | None = None,
    where: str,
) -> tuple[str, ...]:
    """Require exact required membership inside one explicitly allowed namespace.

    ``allowed_layer_ids`` differs from ``required_layer_ids`` only during WAL
    recovery, where an added/removed member may legally be on either recorded side.
    """

    required = {str(layer_id) for layer_id in required_layer_ids}
    allowed = required if allowed_layer_ids is None else {
        str(layer_id) for layer_id in allowed_layer_ids
    }
    if not required.issubset(allowed):
        raise AuthorityStateLiveMemberError(
            f"{where} required state namespace is not a subset of its allowed namespace"
        )
    observed_rows = authority_state_layer_ids(shot_folder)
    observed = set(observed_rows)
    missing = sorted(required - observed)
    unexpected = sorted(observed - allowed)
    if missing or unexpected:
        raise AuthorityStateLiveMemberError(
            f"{where} state namespace mismatch; missing={missing}; "
            f"unexpected={unexpected}"
        )
    return observed_rows


__all__ = [
    "AuthorityStateLiveMemberError",
    "authority_state_layer_ids",
    "require_live_state_namespace",
]
