"""Subject-framing coverage for a composition-owning camera layer (HIR-0127, HIR-0158).

One predicate decides whether a judge frame has executable subject framing: a required
claim bound to a ``bbox_*`` row of a rendered subject at that frame, a
``composition_context`` binding of such a row, a consumed source unit's row that
activates on this layer, or a persistent row this layer authored for a subject a later
geometry layer will create. The plan gate reports the uncovered frames as
``composition-coverage``; the staging transaction refuses them at the camera unit's
stage call so the materializer does not pay a finalize round to learn the rule
(HIR-0177).
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable, Mapping

from vfx_harness.domain.work_units.capabilities import DEFERRED_SUBJECT_BBOX_KINDS

SUBJECT_FRAMING_COVERAGE_RULE = (
    "a projected_composition owner covers each judge frame with bbox_* of a rendered "
    "subject, not projected_origin of a camera-only host. When that subject does not "
    "exist yet, the camera layer authors the bbox with activates_at equal to the compiled "
    "earliest_geometry_layer, lifecycle persistent, and fault_owner on the camera owner "
    "layer; the camera unit binds the ids through composition_context and does not seal them"
)


def camera_only_host_roles(stages: Mapping[str, Mapping]) -> frozenset[str]:
    """Roles mutated by a camera provider that does not also provide geometry."""
    roles: set[str] = set()
    for unit in stages.values():
        provides = {str(item) for item in (unit.get("provides") or [])}
        if "camera" not in provides or "geometry" in provides:
            continue
        mutates = unit.get("mutates") or {}
        roles.update(str(item) for item in mutates.get("roles") or [])
        roles.update(str(item) for item in mutates.get("controls") or [])
    return frozenset(roles)


def role_matches_any(role: str, selectors: frozenset[str]) -> bool:
    return any(
        fnmatch.fnmatchcase(role, selector) or fnmatch.fnmatchcase(selector, role)
        for selector in selectors
    )


def is_subject_framing_row(row: Mapping, camera_only_roles: frozenset[str]) -> bool:
    """True when a row can certify subject composition (HIR-0127).

    ``projected_origin`` of a camera-only host is alignment, not framing. ``bbox_*`` of a
    rendered subject is framing. A bbox whose every role is a camera-only host is not.
    """
    if str(row.get("kind") or "") not in DEFERRED_SUBJECT_BBOX_KINDS:
        return False
    roles = [str(item) for item in row.get("roles") or [] if str(item)]
    if not roles or not camera_only_roles:
        return True
    return not all(role_matches_any(role, camera_only_roles) for role in roles)


def deferred_subject_framing_covers(
    scene_rows: Iterable,
    layer_id: str,
    frame: int,
    camera_only_roles: frozenset[str],
) -> bool:
    """A persistent row this layer authored for a later layer's subject at ``frame``."""
    for row in scene_rows:
        if not isinstance(row, Mapping):
            continue
        if str(row.get("owner_layer") or "") != layer_id:
            continue
        try:
            owner = int(row.get("owner_layer"))
            active = int(row.get("activates_at") or owner)
        except (TypeError, ValueError):
            continue
        if active <= owner:
            continue
        if row.get("frame") != frame:
            continue
        if is_subject_framing_row(row, camera_only_roles):
            return True
    return False


def _claim_covers(unit: Mapping, frame: int, scene_by_id: Mapping[str, Mapping], camera_only: frozenset[str]) -> bool:
    evaluation = unit.get("evaluation") or {}
    return any(
        isinstance(claim, Mapping)
        and claim.get("required")
        and frame in (claim.get("moments") or [])
        and any(
            isinstance(binding, Mapping)
            and binding.get("kind") == "scene_contract"
            and scene_by_id.get(str(binding.get("id")), {}).get("frame") == frame
            and is_subject_framing_row(scene_by_id.get(str(binding.get("id")), {}), camera_only)
            for binding in claim.get("evidence") or []
        )
        for claim in evaluation.get("claims") or []
    )


def _context_covers(unit: Mapping, frame: int, scene_by_id: Mapping[str, Mapping], camera_only: frozenset[str]) -> bool:
    context = (unit.get("evaluation") or {}).get("composition_context") or {}
    if frame not in (context.get("frames") or []):
        return False
    return any(
        cid in scene_by_id
        and scene_by_id[cid].get("frame") == frame
        and is_subject_framing_row(scene_by_id[cid], camera_only)
        for cid in {str(value) for value in context.get("contract_ids") or []}
    )


def _source_covers(
    unit: Mapping,
    frame: int,
    layer_id: str,
    stages: Mapping[str, Mapping],
    scene_rows: Iterable,
    camera_only: frozenset[str],
) -> bool:
    context = (unit.get("evaluation") or {}).get("composition_context") or {}
    source_id = str(context.get("source_unit") or "")
    source = stages.get(source_id)
    if not source or source_id not in {str(value) for value in unit.get("depends_on") or []}:
        return False
    source_contracts = {
        str(binding.get("id"))
        for claim in (source.get("evaluation") or {}).get("claims") or []
        if isinstance(claim, Mapping)
        for binding in claim.get("evidence") or []
        if isinstance(binding, Mapping) and binding.get("kind") == "scene_contract"
    }
    source_frames = {
        row.get("frame")
        for row in (source.get("evaluation") or {}).get("judge") or []
        if isinstance(row, Mapping)
    }
    return frame in source_frames and any(
        str(row.get("id")) in source_contracts
        and row.get("frame") == frame
        and str(row.get("activates_at") or "") == layer_id
        and is_subject_framing_row(row, camera_only)
        for row in scene_rows
        if isinstance(row, Mapping)
    )


def uncovered_subject_framing_frames(
    layer_id: str,
    judges: Iterable[int],
    stages: Mapping[str, Mapping],
    scene_rows: Iterable,
) -> tuple[int, ...]:
    """Judge frames of a composition-owning layer with no executable subject framing."""
    rows = [row for row in scene_rows if isinstance(row, Mapping)]
    scene_by_id = {str(row.get("id")): row for row in rows if row.get("id")}
    camera_only = camera_only_host_roles(stages)
    uncovered: list[int] = []
    for frame in judges:
        covered = any(
            _claim_covers(unit, frame, scene_by_id, camera_only)
            or _context_covers(unit, frame, scene_by_id, camera_only)
            or _source_covers(unit, frame, layer_id, stages, rows, camera_only)
            for unit in stages.values()
        ) or deferred_subject_framing_covers(rows, layer_id, frame, camera_only)
        if not covered:
            uncovered.append(int(frame))
    return tuple(uncovered)
