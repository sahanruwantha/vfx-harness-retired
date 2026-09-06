"""Derived optical-signal and rendered-subject dependency for image-contract debt.

Image evidence cannot be paid from an all-black cumulative scene by a unit whose
write cluster can only change geometry, controls, cameras, or keyframes (HIR-0110).
Optical signal is also not a rendered carrier: a shading-only unit with no mesh or
volume in its replay prefix authors a material on an empty camera plate (HIR-0160).
Both predicates derive providers from the same typed write-cluster registry used by
atomicity; authored look labels and semantic role names are never capability authority.
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
from vfx_harness.domain.evidence_kinds import PIXEL_STATISTIC_KINDS
from vfx_harness.domain.image_debts import image_contract_debt_cards
from vfx_harness.domain.work_units import WorkUnit

#: Families that can put light into a scene on their own.
IMAGE_SIGNAL_SOURCE_FAMILIES = frozenset({"light", "volume", "compositor"})
#: Shading decides how a surface RESPONDS to light. It is a source only when emissive,
#: and emission is not visible at authoring time -- ``bvfx_emission`` and its siblings
#: resolve to the ``shading`` family, and the script that would call them does not exist
#: when the unit is staged. So a shading cluster alone does not establish that anything
#: emits: hansa_silk_road layer 2 passed this gate on a `shading` facade over a `mesh`
#: tower with `world=None` and zero light objects anywhere in the cumulative scene, and
#: authored four image debts that were unpayable in both directions -- darkness bounds
#: trivially met by a black adversary, brightness bounds with nothing to illuminate the
#: surface. Twice, on two independent designs (HIR-0234).
IMAGE_SIGNAL_MODIFIER_FAMILIES = frozenset({"shading"})
#: Every family that can affect pixels at all; used for witness guidance, not for
#: establishing that a prefix can be lit.
IMAGE_SIGNAL_FAMILIES = IMAGE_SIGNAL_SOURCE_FAMILIES | IMAGE_SIGNAL_MODIFIER_FAMILIES
IMAGE_SUBJECT_FAMILIES = frozenset({"mesh", "volume", "compositor"})

#: A shading unit that is itself the light -- an emissive facade, a glowing sign -- says
#: so with a typed capability, exactly as a camera or geometry producer does. A role name
#: or a look label never implies it (HIR-0234).
ILLUMINATION_CAPABILITY = "illumination"

IMAGE_SIGNAL_DEPENDENCY_RULE = (
    "a unit that owes required image_contract debt needs a light SOURCE in its replay "
    "prefix: its own light, volume or compositor write family, a same-layer dependency "
    "that derives one, an earlier materialized layer that does, or a unit declaring "
    "provides: [\"illumination\"] because it is itself emissive. A shading cluster alone "
    "is not a source -- shading decides how a surface responds to light, and whether it "
    "emits is not visible when the unit is staged -- so a shading-over-mesh prefix with "
    "no light and no world renders black and its image debt is unpayable in both "
    "directions. If this unit emits, declare the capability; if something else lights the "
    "scene, depend on it. "
    "look_capabilities, role names, object counts, geometry, cameras, controls, and "
    "keyframes do not imply optical signal. For genuine beauty evidence, reorder or split "
    "the DAG and bind the signal producer through typed write-kind evidence. If the unit "
    "owns only executable form/layout, declare look_capabilities [] and bind scene or "
    "projected-composition claims; its live compare_frame diagnostic defaults to Workbench "
    "solid but never pays beauty debt. Otherwise escalate the missing mutation authority."
)
IMAGE_SUBJECT_DEPENDENCY_RULE = (
    "a unit that owes required image_contract debt must have a rendered carrier in its "
    "replay prefix: its own mesh, volume, or compositor write family, a same-layer "
    "dependency that derives one, or an earlier materialized layer that does. Shading "
    "and lights are optical signal, not a subject; a camera-only prefix cannot pay "
    "beauty. Depend on the geometry or volume producer, or drop image-contract debt "
    "and bind executable scene claims."
)


@dataclass(frozen=True, slots=True)
class ImageSignalDependencyGap:
    """One image-debt owner whose dependency closure has no matching provider."""

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


def _family_provider_ids(
    units: Sequence[WorkUnit],
    rows: Sequence[Mapping[str, Any]] | Iterable[Mapping[str, Any]],
    families: frozenset[str],
) -> frozenset[str]:
    unit_rows = tuple(units)
    contract_rows = tuple(rows)
    return frozenset(
        unit.id
        for unit in unit_rows
        if any(
            cluster.instrument_family in families
            for cluster in write_clusters(unit, contract_rows, units=unit_rows)
        )
    )


def _family_dependency_gaps(
    units: Sequence[WorkUnit],
    rows: Sequence[Mapping[str, Any]] | Iterable[Mapping[str, Any]],
    *,
    families: frozenset[str],
    earlier_available: bool,
    extra_provider_ids: frozenset[str] = frozenset(),
) -> tuple[ImageSignalDependencyGap, ...]:
    if earlier_available:
        return ()
    unit_rows = tuple(units)
    contract_rows = tuple(rows)
    by_id = {unit.id: unit for unit in unit_rows}
    # `extra_provider_ids` carries capabilities a write cluster cannot express: an
    # emissive shading unit is a light source and declares it (HIR-0234).
    providers = _family_provider_ids(unit_rows, contract_rows, families) | extra_provider_ids
    gaps: list[ImageSignalDependencyGap] = []
    rows_by_id = {
        str(row.get("id")): row for row in contract_rows if isinstance(row, Mapping) and row.get("id")
    }
    for unit in unit_rows:
        debt_ids = tuple(
            dict.fromkeys(
                (
                    *(debt.id for debt in image_contract_debt_cards(unit)),
                    *functional_image_debt_ids(unit, rows_by_id),
                )
            )
        )
        if not debt_ids:
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
                contract_ids=debt_ids,
                available_provider_ids=tuple(sorted(providers - closure)),
            )
        )
    return tuple(gaps)


def functional_image_debt_ids(unit: Any, rows_by_id: Mapping[str, Mapping[str, Any]]) -> tuple[str, ...]:
    """Scene-contract ids of absolute pixel statistics a required image claim binds.

    A ``render_region_stat`` or ``control_render_response`` row measures the plate: on a
    camera-only replay prefix it can only read a black plate, and a camera unit paid such
    rows by painting the World grey (run 20260903T002758Z-1b6807, HIR-0176). They are
    image debts for the signal and subject bootstrap exactly like ``image_contract``
    bindings. ``frame_delta`` stays out: it is relative between two frames and its
    payments are refuted by the pre-unit adversary rule (HIR-0048).
    """
    ids: list[str] = []
    for claim in getattr(getattr(unit, "evaluation", None), "claims", ()) or ():
        if not getattr(claim, "required", False) or getattr(claim, "asserts", None) != "image":
            continue
        for binding in getattr(claim, "evidence", ()) or ():
            if getattr(binding, "kind", None) != "scene_contract":
                continue
            row = rows_by_id.get(str(getattr(binding, "id", "")))
            if row is not None and str(row.get("kind") or "") in PIXEL_STATISTIC_KINDS:
                ids.append(str(binding.id))
    return tuple(dict.fromkeys(ids))


def image_signal_provider_ids(
    units: Sequence[WorkUnit],
    rows: Sequence[Mapping[str, Any]] | Iterable[Mapping[str, Any]],
) -> frozenset[str]:
    """Unit ids whose one derived write cluster can affect optical image signal."""
    sources = _family_provider_ids(units, rows, IMAGE_SIGNAL_SOURCE_FAMILIES)
    # A declared capability is additive, exactly as `provides: ["geometry"]` adds a mesh
    # carrier in judgment_authority: an emissive shading unit is a source and says so.
    declared = frozenset(
        unit.id
        for unit in units
        if ILLUMINATION_CAPABILITY in tuple(getattr(unit, "provides", ()) or ())
    )
    return sources | declared


def image_signal_dependency_gaps(
    units: Sequence[WorkUnit],
    rows: Sequence[Mapping[str, Any]] | Iterable[Mapping[str, Any]],
    *,
    earlier_signal_available: bool = False,
) -> tuple[ImageSignalDependencyGap, ...]:
    """Find image debts due before any derived optical-signal mutation authority."""
    return _family_dependency_gaps(
        units,
        rows,
        families=IMAGE_SIGNAL_SOURCE_FAMILIES,
        earlier_available=earlier_signal_available,
        extra_provider_ids=frozenset(
            unit.id
            for unit in units
            if ILLUMINATION_CAPABILITY in tuple(getattr(unit, "provides", ()) or ())
        ),
    )


def image_subject_provider_ids(
    units: Sequence[WorkUnit],
    rows: Sequence[Mapping[str, Any]] | Iterable[Mapping[str, Any]],
) -> frozenset[str]:
    """Unit ids whose write cluster can occupy the plate (mesh, volume, compositor)."""
    return _family_provider_ids(units, rows, IMAGE_SUBJECT_FAMILIES)


def image_subject_dependency_gaps(
    units: Sequence[WorkUnit],
    rows: Sequence[Mapping[str, Any]] | Iterable[Mapping[str, Any]],
    *,
    earlier_subject_available: bool = False,
) -> tuple[ImageSignalDependencyGap, ...]:
    """Find image debts due before any rendered carrier exists in the replay prefix."""
    return _family_dependency_gaps(
        units,
        rows,
        families=IMAGE_SUBJECT_FAMILIES,
        earlier_available=earlier_subject_available,
    )
