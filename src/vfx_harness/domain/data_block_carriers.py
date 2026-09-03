"""Data-block property rows need a unit that produces the data-block (HIR-0185).

``data.energy`` lives on a Light, ``data.lens`` on a Camera. A ``keyframe_schedule`` or
``object_property`` row on such a path is only payable when some unit in the binding
unit's dependency closure, or an earlier materialized layer, writes that carrier family.
Run 20260903T100335Z-fa5dbb bound a streetlight flicker schedule on ``data.energy`` over
a control whose hosts a mesh unit had built as fixtures; the builder proved the gap by
abstaining after two minutes of typed reads. The gap is decidable at materialization.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from vfx_harness.domain.atomicity import CAMERA_PROPERTY_PREFIXES, LIGHT_PROPERTIES, write_clusters
from vfx_harness.domain.work_units.unit import WorkUnit

DATA_BLOCK_CARRIER_RULE = (
    "a scene contract that reads or schedules a data-block property (data.energy and "
    "data.size on a Light; data.lens, data.angle, data.clip_*, data.sensor_*, and "
    "data.ortho_scale on a Camera) binds on a unit whose dependency closure, or an earlier "
    "materialized layer, contains a unit of that carrier family; a control or mesh unit "
    "cannot create the Light or Camera the row measures, so add the carrier unit (for "
    "example a bvfx_light unit) as a dependency or bind the row on it"
)

_DATA_BLOCK_FAMILIES: dict[str, str] = {
    **dict.fromkeys(LIGHT_PROPERTIES, "light"),
    **dict.fromkeys(CAMERA_PROPERTY_PREFIXES, "camera"),
}


def data_block_property_family(path: str) -> str | None:
    """``light`` / ``camera`` for a registered ``data.*`` path; None otherwise."""
    text = str(path or "")
    if not text.startswith("data."):
        return None
    for prefix, family in _DATA_BLOCK_FAMILIES.items():
        if text == prefix or text.startswith(f"{prefix}."):
            return family
    return None


def row_data_block_paths(row: Mapping[str, Any]) -> tuple[str, ...]:
    """Every ``data.*`` path a contract row measures."""
    kind = str(row.get("kind") or "")
    if kind == "object_property":
        prop = str(row.get("property") or "")
        return (prop,) if prop.startswith("data.") else ()
    if kind == "keyframe_schedule":
        paths: list[str] = []
        for sample in row.get("samples") or []:
            values = sample.get("values") if isinstance(sample, Mapping) else None
            if isinstance(values, Mapping):
                paths.extend(str(key) for key in values if str(key).startswith("data."))
        return tuple(dict.fromkeys(paths))
    return ()


@dataclass(frozen=True, slots=True)
class DataBlockCarrierGap:
    unit_id: str
    contract_id: str
    path: str
    family: str | None
    available_provider_ids: tuple[str, ...]

    @property
    def code(self) -> str:
        return "unknown_data_block_property" if self.family is None else "missing_carrier"


def _binding_units(units: Sequence[WorkUnit], contract_id: str) -> list[WorkUnit]:
    return [
        unit
        for unit in units
        if any(
            binding.kind == "scene_contract" and binding.id == contract_id
            for claim in unit.evaluation.claims
            if claim.required
            for binding in claim.evidence
        )
    ]


def data_block_carrier_gaps(
    units: Sequence[WorkUnit],
    rows: Iterable[Mapping[str, Any]],
    *,
    earlier_families: Iterable[str] = (),
) -> tuple[DataBlockCarrierGap, ...]:
    """Rows on data-block paths whose binding unit reaches no carrier of that family."""
    unit_rows = tuple(units)
    contract_rows = tuple(row for row in rows if isinstance(row, Mapping))
    by_id = {unit.id: unit for unit in unit_rows}
    earlier = {str(item) for item in earlier_families}
    families_by_unit = {
        unit.id: frozenset(
            cluster.instrument_family for cluster in write_clusters(unit, contract_rows, units=unit_rows)
        )
        for unit in unit_rows
    }
    gaps: list[DataBlockCarrierGap] = []
    for row in contract_rows:
        contract_id = str(row.get("id") or "")
        for path in row_data_block_paths(row):
            family = data_block_property_family(path)
            for unit in _binding_units(unit_rows, contract_id):
                if family is None:
                    gaps.append(DataBlockCarrierGap(unit.id, contract_id, path, None, ()))
                    continue
                if family in earlier:
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
                if any(family in families_by_unit.get(member, frozenset()) for member in closure):
                    continue
                providers = tuple(
                    sorted(
                        uid
                        for uid, families in families_by_unit.items()
                        if family in families and uid not in closure
                    )
                )
                gaps.append(DataBlockCarrierGap(unit.id, contract_id, path, family, providers))
    return tuple(gaps)


def describe_gap(gap: DataBlockCarrierGap) -> str:
    if gap.family is None:
        return (
            f"unit {gap.unit_id} contract {gap.contract_id} measures {gap.path!r}, which is not a "
            "registered data-block property (light: data.energy, data.size; camera: data.lens, "
            "data.angle, data.clip_start, data.clip_end, data.sensor_width, data.sensor_height, "
            "data.ortho_scale); use an object-level property or register the carrier property"
        )
    outside = ", ".join(gap.available_provider_ids) or "(none on this layer)"
    return (
        f"unit {gap.unit_id} contract {gap.contract_id} measures {gap.path!r} on a {gap.family} "
        f"data-block, but no unit in its dependency closure or an earlier layer writes the "
        f"{gap.family} family; {gap.family} producers on this layer outside the closure: {outside}"
    )
