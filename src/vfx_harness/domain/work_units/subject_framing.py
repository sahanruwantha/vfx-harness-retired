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
from dataclasses import dataclass

from vfx_harness.domain.semantic_roles import match_semantic
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


DOWNSTREAM_SUBJECT_COVERAGE_RULE = (
    "a camera-providing layer authors, at every judge frame it shares with a later layer, a "
    "persistent bbox_* row over that layer's reserved subject namespace (owner_layer and "
    "fault_owner this camera layer, activates_at that layer) with its target measured from "
    "the reference still of that frame, and binds the ids through composition_context; the "
    "camera unit then proves every downstream namespace jointly feasible under its sealed "
    "path before it freezes"
)


@dataclass(frozen=True, slots=True)
class DownstreamSubjectGap:
    frame: int
    layer_id: str
    reserved_roles: tuple[str, ...]
    ref: str


def successor_judge_rows(layers: Iterable[Mapping], camera_layer_id: str) -> list[dict]:
    """Later sparse layers with their judge frames, reference stills, and reserved roles."""
    out: list[dict] = []
    by_id = {str(row.get("id")): row for row in layers if isinstance(row, Mapping) and row.get("id")}
    camera = by_id.get(str(camera_layer_id))
    if camera is None:
        return out
    for layer_id, row in by_id.items():
        if layer_id == str(camera_layer_id):
            continue
        jit = row.get("jit") if isinstance(row.get("jit"), Mapping) else {}
        depends = {str(item) for item in (jit.get("depends_on_layers") or row.get("depends_on_layers") or [])}
        reserved = tuple(str(item) for item in (jit.get("reserved_roles") or row.get("reserved_roles") or []))
        if str(camera_layer_id) not in depends or not reserved:
            continue
        judges = [
            {"frame": int(item["frame"]), "ref": str(item.get("ref") or "")}
            for item in row.get("judge") or []
            if isinstance(item, Mapping) and isinstance(item.get("frame"), int)
        ]
        out.append({"id": layer_id, "reserved_roles": reserved, "judges": judges})
    return out


def uncovered_downstream_subjects(
    camera_layer_id: str,
    camera_judges: Iterable[int],
    successors: Iterable[Mapping],
    scene_rows: Iterable,
) -> tuple[DownstreamSubjectGap, ...]:
    """Shared judge frames where a later layer's reserved subject has no camera-authored bbox row."""
    rows = [row for row in scene_rows if isinstance(row, Mapping)]
    camera_frames = {int(frame) for frame in camera_judges}
    gaps: list[DownstreamSubjectGap] = []
    for successor in successors:
        reserved = tuple(str(item) for item in successor.get("reserved_roles") or [])
        if not reserved:
            continue
        for judge in successor.get("judges") or []:
            frame = int(judge["frame"])
            if frame not in camera_frames:
                continue
            covered = any(
                str(row.get("owner_layer") or "") == str(camera_layer_id)
                and str(row.get("activates_at") or "") == str(successor.get("id"))
                and row.get("frame") == frame
                and str(row.get("kind") or "") in DEFERRED_SUBJECT_BBOX_KINDS
                and any(match_semantic(str(role), reserved) for role in row.get("roles") or [])
                for row in rows
            )
            if not covered:
                gaps.append(
                    DownstreamSubjectGap(frame, str(successor.get("id")), reserved, str(judge.get("ref") or ""))
                )
    return tuple(gaps)

