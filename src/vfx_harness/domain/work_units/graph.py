"""Cross-unit claim binding, interface readiness, and DAG ordering."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from vfx_harness.domain.publish_interfaces import derived_interface_key

if TYPE_CHECKING:
    from vfx_harness.domain.work_units.unit import WorkUnit


@dataclass(frozen=True, slots=True)
class DeferredClaimBindingGap:
    unit_id: str
    claim_id: str
    contract_id: str
    owner_layer: str
    activates_at: str


def deferred_claim_binding_gaps(
    units: Iterable[WorkUnit],
    scene_contracts: Iterable[Mapping[str, Any]],
) -> tuple[DeferredClaimBindingGap, ...]:
    """Find future-active contracts incorrectly authored as unit claim evidence."""
    rows = {str(row.get("id")): row for row in scene_contracts if isinstance(row, Mapping) and row.get("id")}
    gaps: list[DeferredClaimBindingGap] = []
    for unit in units:
        for claim in unit.evaluation.claims:
            for binding in claim.evidence:
                if binding.kind != "scene_contract":
                    continue
                row = rows.get(str(binding.id))
                if row is None:
                    continue
                owner = str(row.get("owner_layer") or "")
                activates = str(row.get("activates_at") or owner)
                if not owner or not activates or activates == owner:
                    continue
                gaps.append(
                    DeferredClaimBindingGap(
                        unit.id,
                        claim.id,
                        str(binding.id),
                        owner,
                        activates,
                    )
                )
    return tuple(gaps)


def offered_interface_keys(unit: WorkUnit) -> tuple[tuple[str, str], ...]:
    """Interface id/kind pairs this unit currently publishes."""
    if unit.publishes:
        return tuple((spec.id, spec.kind) for spec in unit.publishes)

    derived = derived_interface_key(unit)
    return (derived,) if derived is not None else ()


def consumption_is_satisfied(
    unit: WorkUnit,
    units: Sequence[WorkUnit],
    sealed_producers: set[str],
) -> bool:
    """True when every consume matches a sealed producer's offered interface."""
    if not unit.consumes:
        return True
    by_id = {item.id: item for item in units}
    for consume in unit.consumes:
        if consume.producer not in sealed_producers:
            return False
        producer = by_id.get(consume.producer)
        if producer is None:
            return False
        if (consume.interface_id, consume.kind) not in offered_interface_keys(producer):
            return False
    return True


def ready_units(
    units: tuple[WorkUnit, ...],
    passed: set[str],
    *,
    sealed_producers: set[str] | None = None,
) -> tuple[WorkUnit, ...]:
    """Return pending units whose dependencies and consumed interfaces are sealed."""
    producers = passed if sealed_producers is None else sealed_producers
    return tuple(
        unit
        for unit in units
        if unit.id not in passed
        and set(unit.depends_on) <= producers
        and consumption_is_satisfied(unit, units, producers)
    )


def dependency_ordered_units(
    units: Sequence[WorkUnit],
) -> tuple[WorkUnit, ...]:
    """Return a deterministic topological order with authored position as tie-break."""
    ordered_input = tuple(units)
    by_id = {unit.id: unit for unit in ordered_input}
    if len(by_id) != len(ordered_input):
        raise ValueError("work-unit dependency order requires unique unit ids")
    position = {unit.id: index for index, unit in enumerate(ordered_input)}
    missing = {dependency for unit in ordered_input for dependency in unit.depends_on if dependency not in by_id}
    if missing:
        raise ValueError("work-unit dependency order names missing producer(s): " + ", ".join(sorted(missing)))

    remaining = {unit.id: set(unit.depends_on) for unit in ordered_input}
    result: list[WorkUnit] = []
    while remaining:
        ready = sorted(
            (uid for uid, dependencies in remaining.items() if not dependencies),
            key=position.__getitem__,
        )
        if not ready:
            involved = sorted(remaining, key=position.__getitem__)
            raise ValueError("work-unit dependency graph is cyclic: " + ", ".join(involved))
        for uid in ready:
            result.append(by_id[uid])
            remaining.pop(uid)
        ready_set = set(ready)
        for dependencies in remaining.values():
            dependencies.difference_update(ready_set)
    return tuple(result)
