"""Framing findings of one materialization candidate: activation and downstream coverage.

A camera-providing layer that owns projected composition authors, at every judge frame it
shares with a later layer, a persistent ``bbox_*`` row over that layer's reserved subject
namespace (HIR-0184), and every deferred row activates at the compiled earliest
dependency-complete carrier (HIR-0158). ``validate_materialization`` reports both as
collectable findings, so the stage call lists them and finalize refuses them (HIR-0180).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from vfx_harness.domain.atomicity import write_clusters
from vfx_harness.domain.capability_origin import (
    CAPABILITY_ORIGIN_RULE,
    capability_origin_gaps,
)
from vfx_harness.domain.capability_origin import describe_gap as describe_origin_gap
from vfx_harness.domain.data_block_carriers import (
    DATA_BLOCK_CARRIER_RULE,
    data_block_carrier_gaps,
    describe_gap,
)
from vfx_harness.domain.json_pointer import encode as json_ptr
from vfx_harness.domain.work_units import WorkUnit, allowed_unit_provides
from vfx_harness.domain.work_units.capabilities import (
    DEFERRED_SUBJECT_ACTIVATION_RULE,
    compile_deferred_subject_activation,
    deferred_subject_activation_gaps,
)
from vfx_harness.domain.work_units.subject_framing import (
    DOWNSTREAM_SUBJECT_COVERAGE_RULE,
    successor_judge_rows,
    uncovered_downstream_subjects,
)


def owns_projected_composition(layer_row: Mapping) -> bool:
    domains = layer_row.get("evidence_domains")
    return isinstance(domains, list) and "projected_composition" in domains


def framing_findings(
    global_layers: Sequence[Mapping],
    global_row: Mapping,
    layer_row: Mapping,
    layer_id: str,
    scene_rows: Sequence[Mapping],
    *,
    layer_units: Sequence[WorkUnit] = (),
    base_layers: Sequence[Mapping] = (),
) -> list[tuple[str, str]]:
    """(pointer, message) findings for activation, downstream coverage, and data-block carriers."""
    findings: list[tuple[str, str]] = []
    if layer_units:
        earlier_families = earlier_layer_write_families(base_layers, layer_id, scene_rows)
        row_index = {str(row.get("id") or ""): index for index, row in enumerate(scene_rows)}
        for gap in data_block_carrier_gaps(layer_units, scene_rows, earlier_families=earlier_families):
            findings.append(
                (
                    json_ptr("scene_contracts", row_index.get(gap.contract_id, 0)),
                    f"data-block carrier: {describe_gap(gap)}. " + DATA_BLOCK_CARRIER_RULE,
                )
            )
        for origin_gap in capability_origin_gaps(layer_units):
            findings.append(
                (
                    json_ptr("layer", "stages"),
                    f"capability origin: {describe_origin_gap(origin_gap)}. "
                    + CAPABILITY_ORIGIN_RULE,
                )
            )
    activation_card = compile_deferred_subject_activation(global_layers, layer_id)
    for gap in deferred_subject_activation_gaps(activation_card, scene_rows):
        findings.append(
            (
                json_ptr("scene_contracts", gap.index, "activates_at"),
                f"scene contract {gap.contract_id} activates_at={gap.found!r}; compiled "
                f"earliest relevant dependency-complete subject carrier is {gap.expected!r}. "
                + DEFERRED_SUBJECT_ACTIVATION_RULE,
            )
        )
    if "camera" in allowed_unit_provides(global_row) and owns_projected_composition(layer_row):
        camera_judges = [
            int(item["frame"])
            for item in layer_row.get("judge") or []
            if isinstance(item, Mapping) and isinstance(item.get("frame"), int)
        ]
        for gap in uncovered_downstream_subjects(
            layer_id,
            camera_judges,
            successor_judge_rows(global_layers, layer_id),
            scene_rows,
        ):
            still = gap.ref or "that frame's reference still"
            findings.append(
                (
                    json_ptr("scene_contracts"),
                    f"downstream subject coverage: judge f{gap.frame} is shared with layer {gap.layer_id} "
                    f"({', '.join(gap.reserved_roles)}) but no camera-authored persistent bbox_* row over "
                    f"that namespace activates at layer {gap.layer_id}; measure the target on {still} "
                    "and author it here. " + DOWNSTREAM_SUBJECT_COVERAGE_RULE,
                )
            )
    return findings


def earlier_layer_write_families(
    base_layers: Sequence[Mapping],
    layer_id: str,
    scene_rows: Sequence[Mapping],
) -> frozenset[str]:
    """Instrument families every already-materialized earlier layer writes (HIR-0185)."""
    families: set[str] = set()
    for row in base_layers:
        if not isinstance(row, Mapping) or str(row.get("id")) == str(layer_id):
            continue
        if row.get("execution") != "ready":
            continue
        try:
            units = [WorkUnit.parse(stage, f"layer {row.get('id')} unit") for stage in row.get("stages") or []]
        except ValueError:
            continue
        for unit in units:
            families.update(
                cluster.instrument_family
                for cluster in write_clusters(unit, tuple(scene_rows), units=tuple(units))
            )
    return frozenset(families)
