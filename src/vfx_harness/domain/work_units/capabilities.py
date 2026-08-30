"""Sparse-layer capabilities and deferred subject activation.

Camera availability is global DAG authority.  This module compiles that authority and
the first legal downstream geometry activation without depending on work-unit parsing.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

UNIT_PROVIDES = {"camera", "geometry"}
GLOBAL_SCENE_CAPABILITIES = {"camera"}

CAMERA_LAYER_DEFERS_SUBJECT_FORM_RULE = (
    "a sparse layer that globally provides camera may stage camera/control units only; "
    'it must not add a unit with provides:["geometry"] to manufacture framing. '
    "Author persistent bbox_* contracts over rendered-subject roles owned by the "
    "earliest downstream form layer, keep owner_layer and fault_owner on the camera "
    "layer, set activates_at to the compiled earliest_geometry_layer, and bind those ids "
    "through the camera unit's composition_context"
)
DEFERRED_SUBJECT_ACTIVATION_RULE = (
    "a camera layer that authors persistent bbox_* for a subject that does not exist "
    "yet must set activates_at to the compiled earliest_geometry_layer from the selected "
    "DAG. That occupancy is not a client question; do not ask_supervisor for it"
)
DEFERRED_SUBJECT_BBOX_KINDS = frozenset(
    {
        "bbox_width",
        "bbox_height",
        "bbox_center_x",
        "bbox_center_y",
        "bbox_top_y",
        "bbox_bottom_y",
    }
)
DEFERRED_CONTRACT_CONTEXT_RULE = (
    "a scene contract whose activates_at layer is later than its owner_layer is "
    "inactive at the owner's unit boundary and cannot be required claim evidence. "
    "Keep the contract out of claim.evidence and bind its id through "
    "evaluation.composition_context.contract_ids; the later mutator pays and "
    "freeze-protects it when it becomes active"
)
DEFERRED_CONTRACT_FRAME_AUTHORITY_RULE = (
    "a future-active scene contract keeps the authoring owner layer's judge-frame "
    "authority. The activation layer evaluates it as extra-frame evidence; do not add "
    "the owner's reference moments to the activation layer's judge list"
)


def allowed_unit_provides(global_layer_row: Mapping[str, Any]) -> frozenset[str]:
    """Compile unit capabilities from immutable sparse layer authority."""
    jit = global_layer_row.get("jit")
    raw_global = jit.get("provides") if isinstance(jit, Mapping) else {}
    global_capabilities = {str(value) for value in raw_global} if isinstance(raw_global, Mapping) else set()
    if "camera" in global_capabilities:
        return frozenset({"camera"})
    return frozenset(UNIT_PROVIDES - GLOBAL_SCENE_CAPABILITIES)


def _sparse_depends_on(row: Mapping[str, Any]) -> tuple[str, ...]:
    jit = row.get("jit") if isinstance(row.get("jit"), Mapping) else {}
    raw = jit.get("depends_on_layers") if isinstance(jit, Mapping) else None
    if raw is None:
        raw = row.get("depends_on") or []
    if not isinstance(raw, list):
        return ()
    return tuple(str(item) for item in raw if str(item).strip())


def topological_sparse_layer_ids(
    layers: Sequence[Mapping[str, Any]],
) -> tuple[str, ...]:
    """Authored order is the tie-break among independent ready layers (HIR-0119)."""
    indexed: list[tuple[int, str, Mapping[str, Any]]] = []
    for index, row in enumerate(layers):
        if not isinstance(row, Mapping):
            continue
        layer_id = str(row.get("id") or "").strip()
        if layer_id:
            indexed.append((index, layer_id, row))
    by_id = {layer_id: row for _index, layer_id, row in indexed}
    authored = {layer_id: index for index, layer_id, _row in indexed}
    incoming: dict[str, set[str]] = {layer_id: set() for layer_id in by_id}
    children: dict[str, set[str]] = {layer_id: set() for layer_id in by_id}
    for layer_id, row in by_id.items():
        for dependency in _sparse_depends_on(row):
            if dependency in by_id:
                incoming[layer_id].add(dependency)
                children[dependency].add(layer_id)
    ready = sorted(
        (layer_id for layer_id, deps in incoming.items() if not deps),
        key=lambda layer_id: authored[layer_id],
    )
    remaining = {layer_id: set(deps) for layer_id, deps in incoming.items()}
    ordered: list[str] = []
    while ready:
        layer_id = ready.pop(0)
        ordered.append(layer_id)
        unlocked: list[str] = []
        for child in children[layer_id]:
            remaining[child].discard(layer_id)
            if not remaining[child] and child not in ordered and child not in ready:
                unlocked.append(child)
        ready.extend(sorted(unlocked, key=lambda item: authored[item]))
    if len(ordered) != len(by_id):
        return tuple(layer_id for _index, layer_id, _row in indexed)
    return tuple(ordered)


def compile_deferred_subject_activation(
    layers: Sequence[Mapping[str, Any]],
    owner_layer_id: str,
) -> dict[str, Any]:
    """Compile legal deferred-bbox activation from the selected sparse DAG (HIR-0158)."""
    owner = str(owner_layer_id)
    by_id = {str(row.get("id")): row for row in layers if isinstance(row, Mapping) and row.get("id")}
    owner_row = by_id.get(owner)

    def dependency_closure(layer_id: str) -> set[str]:
        seen: set[str] = set()
        stack = list(_sparse_depends_on(by_id[layer_id])) if layer_id in by_id else []
        while stack:
            dependency = stack.pop()
            if dependency in seen or dependency not in by_id:
                continue
            seen.add(dependency)
            stack.extend(_sparse_depends_on(by_id[dependency]))
        return seen

    successors: list[dict[str, Any]] = []
    for layer_id in topological_sparse_layer_ids(layers):
        row = by_id.get(layer_id)
        if row is None or layer_id == owner or owner not in dependency_closure(layer_id):
            continue
        jit = row.get("jit") if isinstance(row.get("jit"), Mapping) else {}
        reserved_raw = (
            (jit.get("reserved_roles") if isinstance(jit, Mapping) else None) or row.get("reserved_roles") or []
        )
        reserved = [str(item) for item in reserved_raw if str(item).strip()]
        successors.append(
            {
                "id": layer_id,
                "title": row.get("title"),
                "reserved_roles": reserved,
                "allowed_provides": sorted(allowed_unit_provides(row)),
            }
        )
    earliest = next(
        (row["id"] for row in successors if "geometry" in row["allowed_provides"]),
        None,
    )
    return {
        "owner_layer": owner,
        "owner_provides_camera": bool(owner_row is not None and "camera" in allowed_unit_provides(owner_row)),
        "successors": successors,
        "earliest_geometry_layer": earliest,
    }


@dataclass(frozen=True, slots=True)
class DeferredSubjectActivationGap:
    contract_id: str
    index: int
    found: str
    expected: str | None


def deferred_subject_activation_gaps(
    card: Mapping[str, Any],
    scene_contracts: Sequence[Mapping[str, Any]],
) -> tuple[DeferredSubjectActivationGap, ...]:
    """Refuse deferred bbox activation that is not the compiled DAG successor."""
    if not card.get("owner_provides_camera"):
        return ()
    owner = str(card.get("owner_layer") or "")
    expected = card.get("earliest_geometry_layer")
    expected_id = str(expected) if expected not in (None, "") else None
    gaps: list[DeferredSubjectActivationGap] = []
    for index, row in enumerate(scene_contracts):
        if not isinstance(row, Mapping):
            continue
        kind = str(row.get("kind") or "")
        if kind not in DEFERRED_SUBJECT_BBOX_KINDS:
            continue
        if str(row.get("owner_layer") or "") != owner:
            continue
        found = str(row.get("activates_at") or owner)
        if found == owner:
            continue
        if expected_id is None or found != expected_id:
            gaps.append(
                DeferredSubjectActivationGap(
                    str(row.get("id") or f"<row {index}>"),
                    index,
                    found,
                    expected_id,
                )
            )
    return tuple(gaps)
