"""Derived work-unit atomicity (HIR-0083).

Write clusters are computed from mutation namespaces, host class, and instrument
family. The materializer does not author a family string; more than one write
cluster without a typed exception is refused by name. Precedent: HIR-0057.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from vfx_harness.domain.publish_interfaces import (
    compile_unit_publish_interfaces,
    exported_role_tokens_from_unit,
)
from vfx_harness.domain.work_units import (
    ATOMICITY_PADDING_FIELDS,
    CONSUME_INTERFACE_RULE,
    CONSUMED_ROLE_MUTATION_RULE,
    WorkUnit,
    bound_claim_contract_ids,
    offered_interface_keys,
    plan_selector_declared,
)

INSTRUMENT_FAMILIES = frozenset(
    {
        "mesh",
        "light",
        "volume",
        "compositor",
        "keyframe",
        "shading",
        "camera",
        "control",
    }
)

# Every supported scene-contract kind maps here. ``None`` means the kind observes
# and does not create a write-family. Unknown kinds fail closed at the gate.
KIND_INSTRUMENT_FAMILY: dict[str, str | None] = {
    "bbox_width": None,
    "bbox_height": None,
    "bbox_center_x": None,
    "bbox_center_y": None,
    "bbox_top_y": None,
    "bbox_bottom_y": None,
    "object_count": None,
    "mesh_vertex_count": "mesh",
    "smooth_fraction": "mesh",
    "radial_inward_fraction": "mesh",
    "object_property": "control",
    "visible_fraction": None,
    "material_count": "shading",
    "material_user_count": "shading",
    "material_assignment_fraction": "shading",
    "node_count": "shading",
    "node_socket_value": "shading",
    "node_link_count": "shading",
    "animation_count": "keyframe",
    "compositor_enabled": "compositor",
    "keyframe_schedule": "keyframe",
    "onset_order": "keyframe",
    "radial_distance_trend": "keyframe",
    "transform_return_delta": "keyframe",
    "curve_derivative_max": "keyframe",
    "path_clearance_min": None,
    "parallax_displacement_profile": None,
    "control_render_response": None,
    "frame_delta": None,
    "render_region_stat": None,
}

HELPER_INSTRUMENT_FAMILY: dict[str, str] = {
    "bvfx_emission": "shading",
    "bvfx_emissive_windows": "shading",
    "bvfx_emissive_from_texture": "shading",
    "bvfx_import_asset": "mesh",
    "bvfx_aim": "camera",
    "bvfx_scatter_emissive": "shading",
    "bvfx_volumetric_world": "volume",
    "bvfx_volume": "volume",
    "bvfx_glare_bloom": "compositor",
    "bvfx_vector_blur": "compositor",
    "bvfx_light": "light",
    "bvfx_fcurves": "keyframe",
    "bvfx_interp": "keyframe",
    "bvfx_camera_rig": "camera",
    "bvfx_role": "control",
    "bvfx_control": "control",
}

_LIGHT_PROPERTIES = frozenset({"data.energy", "data.size"})
_CAMERA_PROPERTY_PREFIXES = (
    "data.lens",
    "data.angle",
    "data.clip_start",
    "data.clip_end",
    "data.sensor_width",
    "data.sensor_height",
    "data.ortho_scale",
)

ATOMICITY_RULE = (
    "a work unit publishes one derived write-cluster (role-namespace × host class × "
    "instrument family). Distinct two-token mutation prefixes are distinct namespaces. "
    "Instrument family comes from typed mutation targets and write-kind evidence; "
    "unresolved families fail closed rather than collapsing to control. Dressing, "
    "visibility observation/protection, and bounded coordination are typed exceptions. "
    "Consumed interfaces are read-only: they do not grant mutation of producer export "
    "roles and cannot hide a mixed cluster. Split the unit, bind dressing, consume a "
    "typed assembly interface, or reassign evidence; do not declare a coherent family."
)

UNRESOLVED_FAMILY_RULE = (
    "a write namespace whose mutation targets do not resolve to a typed instrument "
    "family is unpublished. Bind a write-kind contract to those roles, declare a unique "
    "provides capability for a single-role namespace, or split mixed hosts. Residual "
    "control is not a family."
)

ONE_REPAIR_OWNER_RULE = (
    "required claims on one unit share one repair_owner, which is the same ownership "
    "model compiled into cannot_express_in_scope fault_owner_options."
)


def role_namespace(selector: str) -> str:
    """First two dotted tokens, or the whole token when it has fewer parts."""
    parts = str(selector).split(".")
    if len(parts) >= 2:
        return f"{parts[0]}.{parts[1]}"
    return str(selector)


def host_class_for(unit: WorkUnit) -> str:
    provides = {str(item) for item in unit.provides}
    if "camera" in provides:
        return "camera"
    if "geometry" in provides:
        return "geometry"
    return "control_host"


def host_class_for_family(family: str) -> str:
    if family == "camera":
        return "camera"
    if family == "mesh":
        return "geometry"
    return "control_host"


def residual_instrument_family(unit: WorkUnit) -> str:
    provides = {str(item) for item in unit.provides}
    if "camera" in provides:
        return "camera"
    if "geometry" in provides:
        return "mesh"
    if unit.mutates.dresses and not unit.mutates.roles:
        return "shading"
    return "control"


def instrument_family_for_row(row: Mapping[str, Any]) -> str | None:
    """Family for one bound contract row, or None when the kind only observes."""
    kind = str(row.get("kind") or "")
    if kind not in KIND_INSTRUMENT_FAMILY:
        return None
    graph = str(row.get("graph") or "")
    if kind in {"node_count", "node_socket_value", "node_link_count"}:
        if graph == "compositor":
            return "compositor"
        if graph == "world":
            return "volume"
        return "shading"
    if kind == "object_property":
        prop = str(row.get("property") or "")
        if prop in _LIGHT_PROPERTIES:
            return "light"
        if any(prop == prefix or prop.startswith(f"{prefix}.") for prefix in _CAMERA_PROPERTY_PREFIXES):
            return "camera"
        if prop.startswith("data."):
            return "control"
        return "mesh"
    return KIND_INSTRUMENT_FAMILY[kind]


def bound_rows_for_unit(
    unit: WorkUnit,
    rows: Sequence[Mapping[str, Any]] | Iterable[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    by_id = {
        str(row.get("id")): dict(row)
        for row in rows
        if isinstance(row, Mapping) and row.get("id")
    }
    return tuple(by_id[cid] for cid in bound_claim_contract_ids(unit) if cid in by_id)


def unknown_bound_kinds(
    unit: WorkUnit,
    rows: Sequence[Mapping[str, Any]] | Iterable[Mapping[str, Any]],
) -> tuple[str, ...]:
    found: list[str] = []
    for row in bound_rows_for_unit(unit, rows):
        kind = str(row.get("kind") or "")
        if kind and kind not in KIND_INSTRUMENT_FAMILY:
            found.append(f"{row.get('id')}:{kind}")
    return tuple(found)


def _row_selectors(row: Mapping[str, Any]) -> tuple[str, ...]:
    keys = (
        "roles",
        "control_roles",
        "material_roles",
        "node_roles",
        "node_group_roles",
        "from_node_roles",
        "to_node_roles",
    )
    values: list[str] = []
    for key in keys:
        for item in row.get(key) or ():
            if str(item):
                values.append(str(item))
    return tuple(values)


def _coordination_claims_for(
    unit: WorkUnit,
    units: Sequence[WorkUnit],
) -> tuple[Any, ...]:
    """Interaction claims coordinated by ``unit`` across the candidate DAG.

    ``participants`` are unit ids, never role selectors. The only legal bridge from
    an interaction claim to mutation hosts is the coordination owner's exact
    ``control_roles`` mapping.
    """
    return tuple(
        claim
        for owner in units
        for claim in owner.evaluation.claims
        if claim.required
        and claim.kind == "interaction"
        and claim.coordination_owner == unit.id
    )


def _coordination_control_errors(
    unit: WorkUnit,
    units: Sequence[WorkUnit],
) -> tuple[str, ...]:
    mapping = dict(unit.mutates.control_roles)
    declared = set(unit.mutates.controls)
    errors: list[str] = []
    for claim in _coordination_claims_for(unit, units):
        for control in claim.controls:
            if control not in declared:
                errors.append(f"{claim.id}:{control} is not mutable by {unit.id}")
            elif control not in mapping:
                errors.append(f"{claim.id}:{control} has no control_roles mapping")
    return tuple(dict.fromkeys(errors))


def _coordination_namespaces(
    unit: WorkUnit,
    units: Sequence[WorkUnit],
) -> frozenset[str]:
    if _coordination_control_errors(unit, units):
        return frozenset()
    mapping = dict(unit.mutates.control_roles)
    namespaces: set[str] = set()
    for claim in _coordination_claims_for(unit, units):
        for control in claim.controls:
            namespaces.update(role_namespace(role) for role in mapping.get(control, ()))
    return frozenset(namespaces)


def _provides_residual_family(unit: WorkUnit) -> str | None:
    provides = {str(item) for item in unit.provides}
    if "camera" in provides and "geometry" not in provides:
        return "camera"
    if "geometry" in provides and "camera" not in provides:
        return "mesh"
    if unit.mutates.dresses and not unit.mutates.roles:
        return "shading"
    return None


def _write_namespaces(
    unit: WorkUnit,
    units: Sequence[WorkUnit],
) -> tuple[str, ...]:
    namespaces = tuple(dict.fromkeys(role_namespace(role) for role in unit.mutates.roles))
    coordination = _coordination_namespaces(unit, units)
    return tuple(namespace for namespace in namespaces if namespace not in coordination)


def _roles_in_namespace(unit: WorkUnit, namespace: str) -> tuple[str, ...]:
    return tuple(role for role in unit.mutates.roles if role_namespace(role) == namespace)


def _families_for_namespace(
    unit: WorkUnit,
    namespace: str,
    bound: Sequence[Mapping[str, Any]],
) -> set[str]:
    families: set[str] = set()
    for role in _roles_in_namespace(unit, namespace):
        for row in bound:
            family = instrument_family_for_row(row)
            if family is None:
                continue
            selectors = _row_selectors(row)
            if any(plan_selector_declared(role, (selector,)) for selector in selectors):
                families.add(family)
    if families:
        return families
    if len(_roles_in_namespace(unit, namespace)) > 1:
        return set()
    residual = _provides_residual_family(unit)
    return {residual or "control"}


def _unresolved_roles_for_namespace(
    unit: WorkUnit,
    namespace: str,
    bound: Sequence[Mapping[str, Any]],
) -> tuple[str, ...]:
    roles = _roles_in_namespace(unit, namespace)
    if len(roles) <= 1:
        return ()
    unresolved: list[str] = []
    for role in roles:
        has_family = any(
            instrument_family_for_row(row) is not None
            and any(
                plan_selector_declared(role, (selector,))
                for selector in _row_selectors(row)
            )
            for row in bound
        )
        if not has_family:
            unresolved.append(role)
    return tuple(unresolved)


def _consumed_export_roles(unit: WorkUnit, units: Sequence[WorkUnit]) -> frozenset[str]:
    by_id = {item.id: item for item in units}
    tokens: set[str] = set()
    for consume in unit.consumes:
        producer = by_id.get(consume.producer)
        if producer is None:
            continue
        tokens.update(exported_role_tokens_from_unit(producer))
    return frozenset(tokens)


def _mutated_consumed_roles(unit: WorkUnit, exported: Iterable[str]) -> tuple[str, ...]:
    found: list[str] = []
    tokens = tuple(exported)
    for role in unit.mutates.roles:
        if any(role == token or role.startswith(f"{token}.") for token in tokens):
            found.append(role)
    return tuple(found)


def write_clusters(
    unit: WorkUnit,
    rows: Sequence[Mapping[str, Any]] | Iterable[Mapping[str, Any]],
    *,
    units: Sequence[WorkUnit] = (),
) -> tuple[WriteCluster, ...]:
    """Derived write clusters after dressing, vis observation, and coordination exceptions."""
    bound = bound_rows_for_unit(unit, rows)
    clusters: list[WriteCluster] = []
    seen: set[tuple[str, str, str]] = set()
    candidate_units = tuple(units) or (unit,)
    for namespace in _write_namespaces(unit, candidate_units):
        for family in sorted(_families_for_namespace(unit, namespace, bound)):
            host = host_class_for_family(family)
            key = (namespace, host, family)
            if key in seen:
                continue
            seen.add(key)
            clusters.append(WriteCluster(namespace, host, family))
    return tuple(clusters)


def unresolved_write_namespaces(
    unit: WorkUnit,
    rows: Sequence[Mapping[str, Any]] | Iterable[Mapping[str, Any]],
    *,
    units: Sequence[WorkUnit] = (),
) -> tuple[str, ...]:
    bound = bound_rows_for_unit(unit, rows)
    candidate_units = tuple(units) or (unit,)
    return tuple(
        namespace
        for namespace in _write_namespaces(unit, candidate_units)
        if not _families_for_namespace(unit, namespace, bound)
        or _unresolved_roles_for_namespace(unit, namespace, bound)
    )


@dataclass(frozen=True, slots=True)
class WriteCluster:
    role_namespace: str
    host_class: str
    instrument_family: str

    def label(self) -> str:
        return f"{self.role_namespace}/{self.host_class}/{self.instrument_family}"


@dataclass(frozen=True, slots=True)
class AtomicityGap:
    unit_id: str
    code: str
    detail: str
    clusters: tuple[WriteCluster, ...] = ()
    considered_exceptions: tuple[str, ...] = ()


def _padding_fields(raw: Mapping[str, Any] | None) -> tuple[str, ...]:
    if not isinstance(raw, Mapping):
        return ()
    return tuple(sorted(name for name in ATOMICITY_PADDING_FIELDS if name in raw))


def atomicity_gaps(
    units: Sequence[WorkUnit],
    rows: Sequence[Mapping[str, Any]] | Iterable[Mapping[str, Any]],
    *,
    layer_id: str,
    raw_stages: Sequence[Mapping[str, Any]] = (),
) -> tuple[AtomicityGap, ...]:
    """Publication findings for heterogeneous or unpublishable units."""
    raw_by_id = {
        str(row.get("id")): row
        for row in raw_stages
        if isinstance(row, Mapping) and row.get("id")
    }
    authored_by_id = {
        uid: row.get("publishes")
        for uid, row in raw_by_id.items()
        if "publishes" in row
    }
    gaps: list[AtomicityGap] = []
    for unit in units:
        raw = raw_by_id.get(unit.id)
        padding = _padding_fields(raw)
        if padding:
            gaps.append(
                AtomicityGap(
                    unit.id,
                    "padding",
                    "authored atomicity field(s) "
                    + ", ".join(padding)
                    + " are unrepresentable; "
                    + ATOMICITY_RULE,
                )
            )
        unknown = unknown_bound_kinds(unit, rows)
        if unknown:
            gaps.append(
                AtomicityGap(
                    unit.id,
                    "unknown_kind",
                    "bound contract kinds are not in the instrument-family registry: "
                    + ", ".join(unknown),
                )
            )
        owners = tuple(
            dict.fromkeys(
                claim.repair_owner for claim in unit.evaluation.claims if claim.required
            )
        )
        if len(owners) > 1:
            gaps.append(
                AtomicityGap(
                    unit.id,
                    "repair_owners",
                    "required claims name repair owners "
                    + ", ".join(owners)
                    + ". "
                    + ONE_REPAIR_OWNER_RULE,
                )
            )
        consumed_exports = _consumed_export_roles(unit, units)
        mutated_consumed = _mutated_consumed_roles(unit, consumed_exports)
        if mutated_consumed:
            gaps.append(
                AtomicityGap(
                    unit.id,
                    "consumed_mutation",
                    "mutates.roles includes consumed producer export(s) "
                    + ", ".join(mutated_consumed)
                    + ". "
                    + CONSUMED_ROLE_MUTATION_RULE,
                )
            )
        coordination_errors = _coordination_control_errors(unit, units)
        if coordination_errors:
            gaps.append(
                AtomicityGap(
                    unit.id,
                    "invalid_coordination",
                    "interaction controls are not bounded by the coordination owner's "
                    "exact control_roles mapping: "
                    + "; ".join(coordination_errors)
                    + ". "
                    + ATOMICITY_RULE,
                )
            )
        unresolved = unresolved_write_namespaces(unit, rows, units=units)
        if unresolved:
            gaps.append(
                AtomicityGap(
                    unit.id,
                    "unresolved_family",
                    "write namespace(s) "
                    + ", ".join(unresolved)
                    + " have no typed instrument family. "
                    + UNRESOLVED_FAMILY_RULE,
                )
            )
        clusters = write_clusters(unit, rows, units=units)
        considered = []
        if unit.mutates.dresses:
            considered.append("dressing")
        if any(
            str(row.get("kind") or "") == "visible_fraction"
            for row in bound_rows_for_unit(unit, rows)
        ):
            considered.append("visibility")
        if any(claim.kind == "interaction" for claim in unit.evaluation.claims):
            considered.append("coordination")
        if unit.consumes:
            considered.append("assembly")
        if unit.consumes:
            by_id = {item.id: item for item in units}
            for consume in unit.consumes:
                producer = by_id.get(consume.producer)
                if producer is None:
                    continue
                offered = offered_interface_keys(producer)
                if (consume.interface_id, consume.kind) not in offered:
                    labels = ", ".join(
                        f"{interface_id}:{kind}" for interface_id, kind in offered
                    ) or "(none)"
                    gaps.append(
                        AtomicityGap(
                            unit.id,
                            "incompatible_interface",
                            f"consumes {consume.interface_id}:{consume.kind} from "
                            f"{consume.producer}, which offers {labels}. "
                            + CONSUME_INTERFACE_RULE,
                        )
                    )
        if len(clusters) > 1:
            labels = ", ".join(cluster.label() for cluster in clusters)
            gaps.append(
                AtomicityGap(
                    unit.id,
                    "mixed_clusters",
                    f"derived write-clusters [{labels}] exceed one publish. "
                    + ATOMICITY_RULE,
                    clusters,
                    tuple(considered),
                )
            )
        bound_ids = bound_claim_contract_ids(unit)
        try:
            interfaces = compile_unit_publish_interfaces(
                unit,
                layer_id=layer_id,
                bound_contract_ids=bound_ids,
                authored=authored_by_id.get(unit.id) if unit.id in authored_by_id else None,
                instrument_family=clusters[0].instrument_family if clusters else residual_instrument_family(unit),
            )
        except ValueError as exc:
            gaps.append(
                AtomicityGap(unit.id, "authored_exports", str(exc), clusters, tuple(considered))
            )
            continue
        if not interfaces:
            gaps.append(
                AtomicityGap(
                    unit.id,
                    "missing_interface",
                    "unit publishes no named successor interface (no mutation role, "
                    "control, dress selector, or bound contract id to export). "
                    + ATOMICITY_RULE,
                    clusters,
                    tuple(considered),
                )
            )
    return tuple(gaps)
