"""Same-layer dressing is not ADR-0007 authority.

Dressing is appearance-assignment on another layer's owner-granted geometry.
A selector mutated by any unit on this layer cannot be dressed here (HIR-0161).
This layer's ``dressable`` grants later layers; it does not authorize a same-layer
dresser.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

SAME_LAYER_DRESS_RULE = (
    "dressing is appearance-assignment on another layer's owner-granted geometry "
    "(ADR-0007). A selector mutated by any unit on this layer cannot be dressed "
    "here: drop those `dresses` and assign through this unit's own mutation or "
    "material_roles, or move look to a later layer after this layer lists the "
    "selectors under `dressable`. `layer_updates.dressable` on this layer grants "
    "downstream layers only; it does not authorize a same-layer dresser. "
    "Same-layer `depends_on` a geometry carrier remains legal for image-subject "
    "bootstrap and does not require dressing."
)

DRESSING_CLOSURE_FIX = (
    "a different layer that mutates these roles must list them under `dressable`; "
    "this layer's `dressable` / `layer_updates.dressable` grants later layers only. "
    "Dressing is granted by the owner, never taken"
)


@dataclass(frozen=True)
class SameLayerDressGap:
    unit_id: str
    selectors: tuple[str, ...]
    producer_ids: tuple[str, ...]


def _mutates(unit: Any) -> Any:
    if isinstance(unit, Mapping):
        return unit.get("mutates") or {}
    return getattr(unit, "mutates", None)


def _string_seq(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    return tuple(str(item) for item in value if str(item))


def _unit_id(unit: Any) -> str:
    if isinstance(unit, Mapping):
        return str(unit.get("id") or "")
    return str(getattr(unit, "id", "") or "")


def mutation_roles_of(unit: Any) -> tuple[str, ...]:
    mutates = _mutates(unit)
    if isinstance(mutates, Mapping):
        return _string_seq(mutates.get("roles"))
    return _string_seq(getattr(mutates, "roles", ()))


def dresses_of(unit: Any) -> tuple[str, ...]:
    mutates = _mutates(unit)
    if isinstance(mutates, Mapping):
        return _string_seq(mutates.get("dresses"))
    return _string_seq(getattr(mutates, "dresses", ()))


def same_layer_dress_gaps(stages: Sequence[Any]) -> tuple[SameLayerDressGap, ...]:
    """Return dressers whose selectors are mutated by any unit on this layer."""
    producers: dict[str, list[str]] = {}
    for unit in stages:
        uid = _unit_id(unit)
        if not uid:
            continue
        for role in mutation_roles_of(unit):
            producers.setdefault(role, []).append(uid)
    gaps: list[SameLayerDressGap] = []
    for unit in stages:
        uid = _unit_id(unit)
        if not uid:
            continue
        hits: list[str] = []
        owners: set[str] = set()
        for selector in dresses_of(unit):
            if selector in producers:
                hits.append(selector)
                owners.update(producers[selector])
        if hits:
            gaps.append(
                SameLayerDressGap(
                    unit_id=uid,
                    selectors=tuple(hits),
                    producer_ids=tuple(sorted(owners)),
                )
            )
    return tuple(gaps)
