"""Claim and composition bindings shared by unit and interface contracts."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vfx_harness.domain.work_units.unit import WorkUnit


def bound_claim_contract_ids(unit: WorkUnit) -> tuple[str, ...]:
    """Claim and composition contract ids this unit is answerable for."""
    ids: list[str] = []
    seen: set[str] = set()

    def add(contract_id: str) -> None:
        token = str(contract_id)
        if token and token not in seen:
            seen.add(token)
            ids.append(token)

    for claim in unit.evaluation.claims:
        for binding in claim.evidence:
            if binding.kind in {"scene_contract", "image_contract"}:
                add(binding.id)
    context = unit.evaluation.composition_context
    if context:
        for contract_id in context.contract_ids:
            add(contract_id)
    return tuple(ids)
