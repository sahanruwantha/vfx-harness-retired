"""Sparse-layer capabilities and deferred subject activation.

Camera availability is global DAG authority.  This module compiles that authority and
the first legal downstream geometry activation without depending on work-unit parsing.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from vfx_harness.domain.judgment_debts import JudgmentProvider, compile_provider_activation
from vfx_harness.domain.semantic_roles import match_semantic

UNIT_PROVIDES = {"camera", "geometry"}
GLOBAL_SCENE_CAPABILITIES = {"camera"}

CAMERA_LAYER_DEFERS_SUBJECT_FORM_RULE = (
    "a sparse layer that globally provides camera may stage camera/control units only; "
    'it must not add a unit with provides:["geometry"] to manufacture framing. '
    "Global reserved_roles on that layer may only match the camera grant; extra form "
    "selectors belong on a later layer that does not provide camera. "
    "Author persistent bbox_* contracts over rendered-subject roles owned by the "
    "earliest downstream form layer, keep owner_layer and fault_owner on the camera "
    "layer, set activates_at to the compiled earliest_geometry_layer, and bind those ids "
    "through the camera unit's composition_context"
)
DEFERRED_SUBJECT_ACTIVATION_RULE = (
    "a camera layer that authors persistent bbox_* for a subject that does not exist "
    "yet must set activates_at to the compiled earliest dependency-complete geometry "
    "prefix whose reserved roles semantically match every contract subject selector. "
    "An unrelated mesh is not occupancy for this debt. That boundary is not a client "
    "question; do not ask_supervisor for it"
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


def extra_reserved_roles_on_camera_layer(
    *,
    provided_capabilities: Sequence[str] | set[str],
    camera_selectors: Sequence[Any],
    reserved_roles: Sequence[Any],
) -> tuple[str, ...]:
    """Form selectors a camera-providing sparse layer reserved but cannot mutate.

    Camera grant is typed ``jit.provides.camera``, not a role-name heuristic.
    A reserved selector is legal only when it matches that grant (HIR-0128).
    """
    if "camera" not in {str(item) for item in provided_capabilities}:
        return ()
    granted = tuple(str(item).strip() for item in camera_selectors if str(item).strip())
    extra: list[str] = []
    for role in reserved_roles:
        text = str(role).strip()
        if not text:
            continue
        if granted and match_semantic(text, granted):
            continue
        extra.append(text)
    return tuple(extra)


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


def sparse_layer_dependencies(
    layers: Sequence[Mapping[str, Any]],
) -> dict[str, tuple[str, ...]]:
    """Selected layer DAG edges, independent of JIT materialization state."""
    return {
        str(row.get("id")): _sparse_depends_on(row)
        for row in layers
        if isinstance(row, Mapping) and str(row.get("id") or "").strip()
    }


def _sparse_reserved_roles(row: Mapping[str, Any]) -> tuple[str, ...]:
    jit = row.get("jit") if isinstance(row.get("jit"), Mapping) else {}
    raw = jit.get("reserved_roles") if isinstance(jit, Mapping) else None
    if raw is None:
        raw = row.get("reserved_roles") or []
    if not isinstance(raw, list):
        return ()
    return tuple(str(value) for value in raw if str(value).strip())


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
        # Re-sort the whole queue, not just this batch. Sorting only `unlocked` left
        # a layer unlocked earlier sitting ahead of a lower-authored one unlocked later:
        # hansa_silk_road's DAG (3 needs 1,2; 4 needs 1) yielded 1,2,4,3 because 4 was
        # queued when 1 completed and 3 only when 2 did. The authored order was itself a
        # valid topological order, so the capsule set and this function disagreed and
        # judgment replay capture refused the shot (HIR-0221).
        ready.extend(unlocked)
        ready.sort(key=lambda item: authored[item])
    if len(ordered) != len(by_id):
        return tuple(layer_id for _index, layer_id, _row in indexed)
    return tuple(ordered)


def strict_topological_sparse_layer_ids(
    layers: Sequence[Mapping[str, Any]],
) -> tuple[str, ...]:
    """Return the stable DAG order, rejecting identity, edge, and cycle defects."""

    layer_ids = [str(row.get("id") or "").strip() for row in layers]
    if any(not layer_id for layer_id in layer_ids):
        raise ValueError("selected global layer DAG contains a missing layer id")
    if len(layer_ids) != len(set(layer_ids)):
        raise ValueError("selected global layer DAG contains duplicate layer ids")
    dependencies = sparse_layer_dependencies(layers)
    unknown = sorted(
        {
            dependency
            for layer_dependencies in dependencies.values()
            for dependency in layer_dependencies
            if dependency not in dependencies
        }
    )
    if unknown:
        raise ValueError(
            "selected global layer DAG names unknown dependencies: "
            + ", ".join(unknown)
        )
    order = topological_sparse_layer_ids(layers)
    positions = {layer_id: index for index, layer_id in enumerate(order)}
    if set(order) != set(layer_ids) or any(
        positions[dependency] >= positions[layer_id]
        for layer_id, layer_dependencies in dependencies.items()
        for dependency in layer_dependencies
    ):
        raise ValueError("selected global layer DAG is cyclic or not topologically executable")
    return order


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

    topological = topological_sparse_layer_ids(layers)
    dependencies = sparse_layer_dependencies(layers)
    topological_index = {layer_id: index for index, layer_id in enumerate(topological)}
    successors: list[dict[str, Any]] = []
    for layer_id in topological:
        row = by_id.get(layer_id)
        if row is None or layer_id == owner or owner not in dependency_closure(layer_id):
            continue
        reserved = list(_sparse_reserved_roles(row))
        successors.append(
            {
                "id": layer_id,
                "title": row.get("title"),
                "reserved_roles": reserved,
                "judges": [
                    {"frame": int(item["frame"]), "ref": str(item.get("ref") or "")}
                    for item in row.get("judge") or []
                    if isinstance(item, Mapping) and isinstance(item.get("frame"), int)
                ],
                "allowed_provides": sorted(allowed_unit_provides(row)),
                "replay_prefix_layers": sorted(
                    dependency_closure(layer_id) | {layer_id},
                    key=topological_index.__getitem__,
                ),
            }
        )
    earliest = next(
        (row["id"] for row in successors if "geometry" in row["allowed_provides"]),
        None,
    )
    owner_frames = {
        int(item["frame"])
        for item in (owner_row.get("judge") if owner_row is not None else []) or []
        if isinstance(item, Mapping) and isinstance(item.get("frame"), int)
    }
    framing_obligations = [
        {
            "frame": judge["frame"],
            "ref": judge["ref"],
            "layer_id": successor["id"],
            "reserved_roles": list(successor["reserved_roles"]),
        }
        for successor in successors
        if successor["reserved_roles"]
        for judge in successor["judges"]
        if judge["frame"] in owner_frames
    ]
    return {
        "owner_layer": owner,
        # Every later subject this camera layer must frame at a shared judge frame; the
        # camera authors a persistent bbox_* row for each and proves them jointly feasible
        # before it freezes (HIR-0184).
        "framing_obligations": framing_obligations,
        "owner_provides_camera": bool(owner_row is not None and "camera" in allowed_unit_provides(owner_row)),
        "successors": successors,
        "earliest_geometry_layer": earliest,
        "layer_dependencies": {
            layer_id: list(dependencies[layer_id]) for layer_id in topological
        },
        "layer_order": list(topological),
        "geometry_providers": [
            {
                "id": f"sparse-layer:{layer_id}:mesh",
                "layer_id": layer_id,
                "carrier_family": "mesh",
                "subject_roles": list(_sparse_reserved_roles(by_id[layer_id])),
            }
            for layer_id in topological
            if "geometry" in allowed_unit_provides(by_id[layer_id])
            and _sparse_reserved_roles(by_id[layer_id])
        ],
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
    """Refuse deferred bbox activation without a relevant complete subject carrier.

    ``earliest_geometry_layer`` remains diagnostic summary for older callers.  Actual
    contract authority is role-bound: each subject selector must overlap a geometry
    promise in the candidate replay prefix, using the repository's canonical semantic
    matcher.  This prevents an unrelated mesh layer from activating camera framing debt.
    """
    if not card.get("owner_provides_camera"):
        return ()
    owner = str(card.get("owner_layer") or "")
    raw_dependencies = card.get("layer_dependencies")
    raw_order = card.get("layer_order")
    dependencies = (
        {
            str(layer_id): tuple(str(value) for value in values)
            for layer_id, values in raw_dependencies.items()
            if isinstance(values, list)
        }
        if isinstance(raw_dependencies, Mapping)
        else {}
    )
    order = tuple(str(value) for value in raw_order) if isinstance(raw_order, list) else ()
    providers = tuple(
        JudgmentProvider(
            id=str(row.get("id") or ""),
            layer_id=str(row.get("layer_id") or ""),
            carrier_family=str(row.get("carrier_family") or ""),
            subject_roles=tuple(str(value) for value in (row.get("subject_roles") or ())),
        )
        for row in (card.get("geometry_providers") or ())
        if isinstance(row, Mapping)
    )

    def expected_activation(subject_roles: tuple[str, ...]) -> str | None:
        if not subject_roles:
            return None
        try:
            return compile_provider_activation(
                subject_roles,
                ("mesh",),
                owner,
                providers,
                layer_dependencies=dependencies,
                layer_order=order,
            ).activates_at
        except ValueError:
            return None

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
        subject_roles = tuple(
            str(value).strip()
            for value in (row.get("roles") or ())
            if str(value).strip()
        )
        expected_id = expected_activation(subject_roles)
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
