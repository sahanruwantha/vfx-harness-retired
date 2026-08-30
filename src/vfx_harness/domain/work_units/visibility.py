"""Visibility ownership and geometry dependency rules for work units."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from vfx_harness.domain.contracts import active_for
from vfx_harness.domain.semantic_roles import match_semantic

if TYPE_CHECKING:
    from vfx_harness.domain.work_units.unit import WorkUnit

VIS_REPAIR_OWNER_RULE = (
    "a required claim that binds visible_fraction is repaired by a unit that can "
    'change the rays: provides:["camera"] or mutates/dresses every `roles` '
    "selector on that row. A volume-only unit cannot bind mesh vis as required repair."
)

GEOMETRY_VIS_DEPENDENCY_RULE = (
    "a required visible_fraction row becomes due at its typed repair-owner unit. Every "
    "later geometry unit freeze-protects that row and must include the owner in its "
    "dependency closure; a geometry unit before the owner does not pretend future surfaces "
    "already exist. Order the units with a real acyclic dependency, use already-existing "
    "owner-granted dressable geometry, or split the DAG. Rows without typed ownership "
    "remain conservatively layer-active."
)

GEOMETRY_VIS_CYCLE_RULE = (
    "mutually protecting geometry units cannot be made sealable by reordering, "
    "narrowing protects, or changing visibility lifecycle: every geometry provider "
    "freezes every lifecycle-active visible_fraction row on its layer. Retire the "
    "involved unpublished units in reverse dependency order, then either author all "
    "judged surfaces in one geometry unit whose roles truthfully form one derived "
    "write-cluster, or use already-existing owner-granted dressable geometry so only "
    "one unit provides geometry. Do not remove a real geometry capability, weaken "
    "visibility, or add cyclic depends_on edges."
)


def plan_selector_declared(selector: str, declarations: Iterable[str]) -> bool:
    """Whether selector and declared role namespaces can address the same tag."""
    token = str(selector)
    return any(
        match_semantic(token, (str(declared),)) or match_semantic(str(declared), (token,)) for declared in declarations
    )


def vis_roles_unrepairable_by(
    *,
    provides: Iterable[str],
    mutation_roles: Iterable[str],
    vis_roles: Iterable[str],
) -> tuple[str, ...]:
    """Vis roles the named owner cannot change (HIR-0051). Camera may observe any."""
    if "camera" in {str(item) for item in provides}:
        return ()
    declared = {str(item) for item in mutation_roles}
    return tuple(
        sorted(str(role) for role in vis_roles if str(role) and not plan_selector_declared(str(role), declared))
    )


def layer_active_visible_fraction_ids(
    rows: Sequence[Mapping[str, Any]] | Iterable[Mapping[str, Any]],
    layer_id: str | int,
    frame: int | None = None,
) -> tuple[str, ...]:
    """Lifecycle-active `visible_fraction` ids on this layer (optionally one frame)."""
    return tuple(
        sorted(
            {
                str(row["id"])
                for row in rows
                if isinstance(row, Mapping)
                and str(row.get("kind") or "") == "visible_fraction"
                and row.get("id")
                and active_for(dict(row), layer_id, frame)
            }
        )
    )


def geometry_vis_protection_ids(
    provides: Iterable[str],
    layer_active_vis_ids: Iterable[str],
) -> tuple[str, ...]:
    """Geometry units freeze-protect active-layer vis, including sibling rows."""
    if "geometry" not in {str(item) for item in provides}:
        return ()
    return tuple(sorted({str(item) for item in layer_active_vis_ids if str(item)}))


def _dependency_closure(
    unit: WorkUnit,
    by_id: Mapping[str, WorkUnit],
) -> set[str]:
    found: set[str] = set()
    frontier = list(unit.depends_on)
    while frontier:
        current = frontier.pop()
        if current in found:
            continue
        found.add(current)
        dependency = by_id.get(current)
        if dependency is not None:
            frontier.extend(dependency.depends_on)
    return found


def visible_fraction_repair_owners(
    units: Sequence[WorkUnit],
) -> dict[str, tuple[str, ...]]:
    """Required-claim repair owners that make each vis row due (HIR-0132)."""
    owners: dict[str, set[str]] = {}
    for unit in units:
        for claim in unit.evaluation.claims:
            if not claim.required:
                continue
            for binding in claim.evidence:
                if binding.kind != "scene_contract":
                    continue
                owners.setdefault(str(binding.id), set()).add(str(claim.repair_owner))
    return {contract_id: tuple(sorted(values)) for contract_id, values in owners.items()}


def geometry_vis_protection_ids_for_unit(
    units: Sequence[WorkUnit],
    unit: WorkUnit,
    rows: Sequence[Mapping[str, Any]] | Iterable[Mapping[str, Any]],
    layer_id: str | int,
    *,
    frame: int | None = None,
) -> tuple[str, ...]:
    """Vis rows due at this geometry unit after typed unit activation."""
    if "geometry" not in unit.provides:
        return ()
    unit_rows = tuple(units)
    by_id = {item.id: item for item in unit_rows}
    closure = _dependency_closure(unit, by_id) | {unit.id}
    owners = visible_fraction_repair_owners(unit_rows)
    active_ids = layer_active_visible_fraction_ids(rows, layer_id, frame=frame)
    return tuple(
        contract_id
        for contract_id in active_ids
        if not owners.get(contract_id) or set(owners[contract_id]).intersection(closure)
    )


@dataclass(frozen=True, slots=True)
class GeometryVisDependencyGap:
    unit_id: str
    contract_id: str
    role: str
    producer_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GeometryVisDependencyCycle:
    unit_ids: tuple[str, ...]
    contract_ids: tuple[str, ...]
    roles: tuple[str, ...]
    edges: tuple[tuple[str, str], ...]


def geometry_vis_dependency_gaps(
    units: Sequence[WorkUnit],
    rows: Sequence[Mapping[str, Any]] | Iterable[Mapping[str, Any]],
    layer_id: str | int,
) -> tuple[GeometryVisDependencyGap, ...]:
    """Find geometry/visibility order that lacks an authority edge."""
    unit_rows = tuple(units)
    by_id = {unit.id: unit for unit in unit_rows}
    row_by_id = {str(row.get("id")): row for row in rows if isinstance(row, Mapping) and row.get("id")}
    active_ids = layer_active_visible_fraction_ids(row_by_id.values(), layer_id)
    closures = {unit.id: _dependency_closure(unit, by_id) for unit in unit_rows}
    order = {unit.id: index for index, unit in enumerate(unit_rows)}
    owners_by_contract = visible_fraction_repair_owners(unit_rows)

    gaps: list[GeometryVisDependencyGap] = []
    for unit in unit_rows:
        if "geometry" not in unit.provides:
            continue
        dependencies = closures[unit.id]
        own_roles = (*unit.mutates.roles, *unit.mutates.dresses)
        for contract_id in active_ids:
            row = row_by_id[contract_id]
            typed_owners = tuple(owner for owner in owners_by_contract.get(contract_id, ()) if owner in by_id)
            if typed_owners:
                earlier_unordered = tuple(
                    owner
                    for owner in typed_owners
                    if owner != unit.id
                    and owner not in dependencies
                    and unit.id not in closures[owner]
                    and order[owner] < order[unit.id]
                )
                if not earlier_unordered:
                    continue
            else:
                earlier_unordered = ()
            unresolved = vis_roles_unrepairable_by(
                provides=(),
                mutation_roles=own_roles,
                vis_roles=row.get("roles") or (),
            )
            for role in unresolved:
                producers = earlier_unordered or tuple(
                    sorted(
                        other.id
                        for other in unit_rows
                        if other.id != unit.id
                        and plan_selector_declared(role, (*other.mutates.roles, *other.mutates.dresses))
                    )
                )
                if producers and not any(producer in dependencies for producer in producers):
                    gaps.append(GeometryVisDependencyGap(unit.id, contract_id, role, producers))
    return tuple(gaps)


def geometry_vis_dependency_cycles(
    units: Sequence[WorkUnit],
    rows: Sequence[Mapping[str, Any]] | Iterable[Mapping[str, Any]],
    layer_id: str | int,
) -> tuple[GeometryVisDependencyCycle, ...]:
    """Compile mutually unsealable geometry-protection gaps into cycle cards."""
    gaps = geometry_vis_dependency_gaps(units, rows, layer_id)
    graph: dict[str, set[str]] = {unit.id: set() for unit in units}
    for gap in gaps:
        graph.setdefault(gap.unit_id, set()).update(gap.producer_ids)

    index = 0
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[tuple[str, ...]] = []

    def visit(node: str) -> None:
        nonlocal index
        indices[node] = index
        lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for successor in sorted(graph.get(node, ())):
            if successor not in indices:
                visit(successor)
                lowlinks[node] = min(lowlinks[node], lowlinks[successor])
            elif successor in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[successor])
        if lowlinks[node] != indices[node]:
            return
        component: list[str] = []
        while stack:
            member = stack.pop()
            on_stack.remove(member)
            component.append(member)
            if member == node:
                break
        if len(component) > 1:
            components.append(tuple(sorted(component)))

    for unit_id in sorted(graph):
        if unit_id not in indices:
            visit(unit_id)

    cycles: list[GeometryVisDependencyCycle] = []
    for component in sorted(components):
        members = set(component)
        relevant = tuple(
            gap for gap in gaps if gap.unit_id in members and any(pid in members for pid in gap.producer_ids)
        )
        edges = tuple(
            sorted(
                {(gap.unit_id, producer) for gap in relevant for producer in gap.producer_ids if producer in members}
            )
        )
        cycles.append(
            GeometryVisDependencyCycle(
                unit_ids=component,
                contract_ids=tuple(sorted({gap.contract_id for gap in relevant})),
                roles=tuple(sorted({gap.role for gap in relevant})),
                edges=edges,
            )
        )
    return tuple(cycles)
