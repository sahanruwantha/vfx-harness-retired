"""Camera-owned projected-origin consumption and surface-visibility debt."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from vfx_harness.domain.evidence_kinds import PROJECTED_ORIGIN_KINDS
from vfx_harness.domain.publish_interfaces import exported_selector_tokens_from_interface
from vfx_harness.domain.work_units.unit import WorkUnit
from vfx_harness.domain.work_units.visibility import plan_selector_declared

PROJECTED_ORIGIN_REPAIR_RULE = (
    "projected_origin_x/y is camera-alignment evidence: its required claim's "
    "repair_owner provides camera. A fixed Empty/control producer proves world state "
    "with scene evidence; the downstream camera owner consumes that producer's exact "
    "typed placement interface and owns projection through the camera. The observed "
    "role/control is read-only. Do not fit the target after the camera."
)


@dataclass(frozen=True, slots=True)
class PointProjectionInterfaceGap:
    unit_id: str
    contract_id: str
    selector: str
    producer_ids: tuple[str, ...]
    reason: str


def point_projection_interface_gaps(
    units: Sequence[WorkUnit],
    rows: Sequence[Mapping[str, Any]] | Iterable[Mapping[str, Any]],
) -> tuple[PointProjectionInterfaceGap, ...]:
    """Require camera-owned point projection to read a typed producer interface.

    A same-layer target is a predecessor value, not camera mutation authority. If the
    selector is produced in this DAG, the camera owner must consume an interface that
    exports it. Roles owned only by an accepted upstream layer remain legal through the
    layer dependency/protected-interface mechanism.
    """
    unit_rows = tuple(units)
    by_id = {unit.id: unit for unit in unit_rows}
    row_by_id = {str(row.get("id")): row for row in rows if isinstance(row, Mapping) and row.get("id")}

    def _matches(selector: str, declarations: Iterable[str]) -> bool:
        return plan_selector_declared(selector, tuple(str(value) for value in declarations))

    gaps: list[PointProjectionInterfaceGap] = []
    seen: set[tuple[str, str, str, str]] = set()
    for declaring_unit in unit_rows:
        for claim in declaring_unit.evaluation.claims:
            if not claim.required:
                continue
            owner = by_id.get(claim.repair_owner)
            if owner is None or "camera" not in owner.provides:
                continue  # the independent ownership rule reports this case
            for binding in claim.evidence:
                if binding.kind != "scene_contract":
                    continue
                row = row_by_id.get(binding.id)
                if row is None or str(row.get("kind") or "") not in PROJECTED_ORIGIN_KINDS:
                    continue
                for field, mutation_field in (("roles", "roles"), ("control_roles", "controls")):
                    for raw_selector in row.get(field) or ():
                        selector = str(raw_selector)
                        owner_values = getattr(owner.mutates, mutation_field)
                        if _matches(selector, owner_values):
                            key = (owner.id, binding.id, selector, "owner_mutation")
                            if key not in seen:
                                seen.add(key)
                                gaps.append(
                                    PointProjectionInterfaceGap(
                                        owner.id,
                                        binding.id,
                                        selector,
                                        (owner.id,),
                                        "owner_mutation",
                                    )
                                )
                            continue
                        producers = tuple(
                            producer
                            for producer in unit_rows
                            if producer.id != owner.id and _matches(selector, getattr(producer.mutates, mutation_field))
                        )
                        if not producers:
                            continue
                        compatible: list[str] = []
                        for producer in producers:
                            for consume in owner.consumes:
                                if consume.producer != producer.id:
                                    continue
                                exported = exported_selector_tokens_from_interface(
                                    producer,
                                    interface_id=consume.interface_id,
                                    kind=consume.kind,
                                    selector_type=("role" if field == "roles" else "control"),
                                )
                                if _matches(selector, exported):
                                    compatible.append(producer.id)
                                    break
                        if compatible:
                            continue
                        producer_ids = tuple(sorted(producer.id for producer in producers))
                        key = (owner.id, binding.id, selector, "missing_consumption")
                        if key in seen:
                            continue
                        seen.add(key)
                        gaps.append(
                            PointProjectionInterfaceGap(
                                owner.id,
                                binding.id,
                                selector,
                                producer_ids,
                                "missing_consumption",
                            )
                        )
    return tuple(gaps)


def unit_requires_surface_visibility(unit: WorkUnit) -> bool:
    """Whether a unit owns or consumes a rendered subject at its judge frames.

    Camera rigs, lights, volumes, and Empty-style control hosts are scene interfaces,
    not rendered subjects. Forcing visible_fraction on them makes a builder invent mesh
    solely to pay evidence debt. Geometry, dressing, look work, and consumed asset/
    instance sources do own rendered surfaces and keep the occlusion-true requirement.
    """
    return bool(
        "geometry" in unit.provides
        or unit.mutates.dresses
        or unit.look_capabilities
        or any(claim.required and claim.asserts == "image" for claim in unit.evaluation.claims)
        or any(item.kind in {"asset_source", "instance_source"} for item in unit.consumes)
    )
