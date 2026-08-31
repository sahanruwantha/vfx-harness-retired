"""Derived construction-route legality against write clusters (ADR-0009, HIR-0162).

Separated from ``construction`` parse/types so ``WorkUnit.parse`` can import the
leaf contract without cycling through atomicity.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from vfx_harness.domain.atomicity import residual_instrument_family, write_clusters
from vfx_harness.domain.construction import (
    MESH_CONSTRUCTION_FAMILIES,
    SIMPLIFY_CONSTRUCTION_FAMILIES,
    ConstructionRouteGap,
    ConstructionSpec,
)


def _object_count_floor(row: Mapping[str, Any]) -> float | None:
    if str(row.get("kind") or "") != "object_count":
        return None
    op = str(row.get("op") or "")
    if op == "eq":
        value = row.get("value")
    elif op in {"min", "band"}:
        value = row.get("lo")
    else:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _required_bound_rows(unit: Any, rows: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    by_id = {
        str(row.get("id")): dict(row)
        for row in rows
        if isinstance(row, Mapping) and row.get("id")
    }
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for claim in getattr(getattr(unit, "evaluation", None), "claims", ()):
        if not getattr(claim, "required", False):
            continue
        for binding in getattr(claim, "evidence", ()):
            if getattr(binding, "kind", "") != "scene_contract":
                continue
            cid = str(getattr(binding, "id", "") or "")
            if cid and cid not in seen and cid in by_id:
                seen.add(cid)
                found.append(by_id[cid])
    return tuple(found)


def _derived_families(
    unit: Any,
    rows: Sequence[Mapping[str, Any]],
    units: Sequence[Any],
) -> tuple[str, ...]:
    clusters = write_clusters(unit, rows, units=units)
    families = tuple(dict.fromkeys(cluster.instrument_family for cluster in clusters))
    if families:
        return families
    return (residual_instrument_family(unit),)


def construction_route_gaps(
    units: Sequence[Any],
    rows: Sequence[Mapping[str, Any]] | Iterable[Mapping[str, Any]],
) -> tuple[ConstructionRouteGap, ...]:
    """Find units whose construction route is illegal for derived mutation authority."""
    unit_rows = tuple(units)
    contract_rows = tuple(rows)
    gaps: list[ConstructionRouteGap] = []
    for unit in unit_rows:
        spec = getattr(unit, "construction", None) or ConstructionSpec()
        route = spec.route
        if route == "procedural":
            continue
        families = _derived_families(unit, contract_rows, unit_rows)
        family_set = set(families)
        if route in {"generate", "retrieve"} and family_set - MESH_CONSTRUCTION_FAMILIES:
            gaps.append(
                ConstructionRouteGap(
                    unit_id=unit.id,
                    code="family",
                    detail=(
                        f"construction route {route!r} requires a mesh write family; "
                        f"derived families are {list(families)}"
                    ),
                    families=families,
                )
            )
            continue
        if route == "simplify" and (
            not family_set.intersection(SIMPLIFY_CONSTRUCTION_FAMILIES)
            or family_set - SIMPLIFY_CONSTRUCTION_FAMILIES
        ):
            gaps.append(
                ConstructionRouteGap(
                    unit_id=unit.id,
                    code="family",
                    detail=(
                        "construction route 'simplify' requires a mesh or volume "
                        f"write family; derived families are {list(families)}"
                    ),
                    families=families,
                )
            )
            continue
        if route != "generate":
            continue
        bound = _required_bound_rows(unit, contract_rows)
        instancing = [
            row
            for row in bound
            if (floor := _object_count_floor(row)) is not None and floor > 1
        ]
        if instancing:
            ids = tuple(str(row.get("id")) for row in instancing)
            gaps.append(
                ConstructionRouteGap(
                    unit_id=unit.id,
                    code="instancing",
                    detail=(
                        "construction route 'generate' is a unique source unit and cannot "
                        f"bind required object_count whose minimum exceeds 1: {list(ids)}. "
                        "Instance assembly belongs on a successor that consumes this source"
                    ),
                    contract_ids=ids,
                    families=families,
                )
            )
        clearance = [
            row for row in bound if str(row.get("kind") or "") == "path_clearance_min"
        ]
        if clearance:
            ids = tuple(str(row.get("id")) for row in clearance)
            gaps.append(
                ConstructionRouteGap(
                    unit_id=unit.id,
                    code="path_clearance",
                    detail=(
                        "construction route 'generate' cannot bind path_clearance_min "
                        f"{list(ids)}; that evidence is camera-path alignment, not a "
                        "generated source mesh"
                    ),
                    contract_ids=ids,
                    families=families,
                )
            )
    return tuple(gaps)
