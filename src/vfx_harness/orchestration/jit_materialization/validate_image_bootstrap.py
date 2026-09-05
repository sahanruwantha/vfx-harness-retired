"""Image-debt bootstrap findings for one materialized layer.

Split out of ``validate.py`` when that module reached its line budget: optical signal
and rendered-carrier availability are one cohesive question — can the plate this layer
owes image evidence on carry any image at all — asked twice over the same replay prefix
(HIR-0110, HIR-0160). It follows the ``validate_requirements``/``validate_framing``
pattern: the caller owns ``note`` and this module owns the predicate and its wording.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from vfx_harness.domain.image_signal import (
    IMAGE_SIGNAL_DEPENDENCY_RULE,
    IMAGE_SUBJECT_DEPENDENCY_RULE,
    image_signal_dependency_gaps,
    image_signal_provider_ids,
    image_signal_witness_guidance,
    image_subject_dependency_gaps,
    image_subject_provider_ids,
)
from vfx_harness.domain.json_pointer import encode as json_ptr


def _earlier_available(
    combined_layers: Sequence[Mapping[str, Any]],
    parsed: Mapping[str, Any],
    combined_scene_rows: Sequence[Mapping[str, Any]],
    target_index: int,
    provider_ids: Callable[[Any, Any], frozenset[str]],
) -> bool:
    """Whether any earlier ready layer already supplies this family."""
    return any(
        str(row.get("execution") or "") == "ready"
        and str(row.get("id") or "") in parsed
        and provider_ids(parsed[str(row.get("id"))].stages, combined_scene_rows)
        for row in combined_layers[:target_index]
    )


def note_image_bootstrap_gaps(
    *,
    note,
    layer,
    layer_id: str,
    combined_layers: Sequence[Mapping[str, Any]],
    combined_scene_rows: Sequence[Mapping[str, Any]],
    parsed: Mapping[str, Any],
    unit_index_by_id: Mapping[str, int],
) -> None:
    """Report units owing image-contract debt before signal or carrier exists."""
    target_index = next(
        (index for index, row in enumerate(combined_layers) if str(row.get("id") or "") == layer_id),
        0,
    )
    for gap in image_signal_dependency_gaps(
        layer.stages,
        combined_scene_rows,
        earlier_signal_available=_earlier_available(
            combined_layers, parsed, combined_scene_rows, target_index, image_signal_provider_ids
        ),
    ):
        available = (
            " Same-layer signal provider(s) exist but are outside the dependency "
            f"closure: {list(gap.available_provider_ids)}."
            if gap.available_provider_ids
            else " No same-layer unit currently derives a signal family."
        )
        note(
            json_ptr("layer", "stages", unit_index_by_id[gap.unit_id], "depends_on"),
            f"unit {gap.unit_id} owes image-contract debt "
            f"{list(gap.contract_ids)} before optical signal is available."
            + available
            + " Registered write-kind witnesses: "
            + image_signal_witness_guidance()
            + ". "
            + IMAGE_SIGNAL_DEPENDENCY_RULE,
        )
    for gap in image_subject_dependency_gaps(
        layer.stages,
        combined_scene_rows,
        earlier_subject_available=_earlier_available(
            combined_layers, parsed, combined_scene_rows, target_index, image_subject_provider_ids
        ),
    ):
        available = (
            " Same-layer rendered-carrier unit(s) exist but are outside the "
            f"dependency closure: {list(gap.available_provider_ids)}."
            if gap.available_provider_ids
            else " No same-layer unit currently derives a mesh, volume, or compositor family."
        )
        note(
            json_ptr("layer", "stages", unit_index_by_id[gap.unit_id], "depends_on"),
            f"unit {gap.unit_id} owes image-contract debt "
            f"{list(gap.contract_ids)} before a rendered carrier is available."
            + available
            + " "
            + IMAGE_SUBJECT_DEPENDENCY_RULE,
        )
