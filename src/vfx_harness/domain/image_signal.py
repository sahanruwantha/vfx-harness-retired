"""Derived optical-signal dependency for image-contract debt (HIR-0110).

Image evidence cannot be paid from an all-black cumulative scene by a unit whose
write cluster can only change geometry, controls, cameras, or keyframes.  The
publication predicate below derives pixel-affecting providers from the same typed
write-cluster registry used by atomicity; authored look labels and semantic role
names are never capability authority.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from vfx_harness.domain.atomicity import (
    KIND_INSTRUMENT_FAMILY,
    instrument_family_for_row,
    write_clusters,
)
from vfx_harness.domain.image_debts import image_contract_debt_cards
from vfx_harness.domain.work_units import WorkUnit

IMAGE_SIGNAL_FAMILIES = frozenset({"light", "shading", "volume", "compositor"})

IMAGE_SIGNAL_DEPENDENCY_RULE = (
    "a unit that owes required image_contract debt must itself derive a pixel-affecting "
    "write family (light, shading, volume, or compositor), depend transitively on a "
    "same-layer unit that does, or inherit one from an earlier materialized layer. "
    "look_capabilities, role names, object counts, geometry, cameras, controls, and "
    "keyframes do not imply optical signal. For genuine beauty evidence, reorder or split "
    "the DAG and bind the signal producer through typed write-kind evidence. If the unit "
    "owns only executable form/layout, declare look_capabilities [] and bind scene or "
    "projected-composition claims; its live compare_frame diagnostic defaults to Workbench "
    "solid but never pays beauty debt. Otherwise escalate the missing mutation authority."
)


@dataclass(frozen=True, slots=True)
class ImageSignalDependencyGap:
    """One image-debt owner whose dependency closure has no signal provider."""

    unit_id: str
    contract_ids: tuple[str, ...]
    available_provider_ids: tuple[str, ...]


def _witness_variants() -> tuple[tuple[str, dict[str, Any]], ...]:
    """Registered row shapes whose resolver can derive an image-signal family.

    The labels are feedback only.  Membership is computed by calling the canonical
    atomicity resolver, so adding or changing a family cannot silently leave this card
    with a second hand-maintained classification table.
    """
    variants: list[tuple[str, dict[str, Any]]] = []
    for kind in KIND_INSTRUMENT_FAMILY:
        variants.append((kind, {"kind": kind}))
    for graph in ("material", "world", "compositor"):
        for kind in ("node_count", "node_socket_value", "node_link_count"):
            variants.append((f"{kind}(graph={graph})", {"kind": kind, "graph": graph}))
    for prop in ("data.energy", "data.size"):
        variants.append(
            (
                f"object_property(property={prop})",
                {"kind": "object_property", "property": prop},
            )
        )
    return tuple(variants)


def image_signal_witnesses() -> dict[str, tuple[str, ...]]:
    """Legal scene-contract witnesses grouped by derived signal family."""
    grouped: dict[str, list[str]] = {family: [] for family in sorted(IMAGE_SIGNAL_FAMILIES)}
    for label, row in _witness_variants():
        family = instrument_family_for_row(row)
        if family in IMAGE_SIGNAL_FAMILIES and label not in grouped[family]:
            grouped[family].append(label)
    return {family: tuple(labels) for family, labels in grouped.items()}


def image_signal_witness_guidance() -> str:
    """Compact registry-derived next-action card for a rejected publication."""
    return "; ".join(
        f"{family}: {', '.join(witnesses)}"
        for family, witnesses in image_signal_witnesses().items()
        if witnesses
    )


def image_signal_provider_ids(
    units: Sequence[WorkUnit],
    rows: Sequence[Mapping[str, Any]] | Iterable[Mapping[str, Any]],
) -> frozenset[str]:
    """Unit ids whose one derived write cluster can affect optical image signal."""
    unit_rows = tuple(units)
    contract_rows = tuple(rows)
    return frozenset(
        unit.id
        for unit in unit_rows
        if any(
            cluster.instrument_family in IMAGE_SIGNAL_FAMILIES
            for cluster in write_clusters(unit, contract_rows, units=unit_rows)
        )
    )


def image_signal_dependency_gaps(
    units: Sequence[WorkUnit],
    rows: Sequence[Mapping[str, Any]] | Iterable[Mapping[str, Any]],
    *,
    earlier_signal_available: bool = False,
) -> tuple[ImageSignalDependencyGap, ...]:
    """Find image debts due before any derived optical-signal mutation authority."""
    if earlier_signal_available:
        return ()
    unit_rows = tuple(units)
    contract_rows = tuple(rows)
    by_id = {unit.id: unit for unit in unit_rows}
    providers = image_signal_provider_ids(unit_rows, contract_rows)
    gaps: list[ImageSignalDependencyGap] = []
    for unit in unit_rows:
        debts = image_contract_debt_cards(unit)
        if not debts:
            continue
        closure: set[str] = set()
        frontier = [unit.id]
        while frontier:
            current = frontier.pop()
            if current in closure:
                continue
            closure.add(current)
            dependency = by_id.get(current)
            if dependency is not None:
                frontier.extend(dependency.depends_on)
        if providers.intersection(closure):
            continue
        gaps.append(
            ImageSignalDependencyGap(
                unit_id=unit.id,
                contract_ids=tuple(dict.fromkeys(debt.id for debt in debts)),
                available_provider_ids=tuple(sorted(providers - closure)),
            )
        )
    return tuple(gaps)
