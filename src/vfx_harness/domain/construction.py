"""Typed construction-route authority for geometry-owning work units.

Image-to-3D is one construction route inside a mesh-family source unit, not a
department layer or a builder-time import choice (ADR-0009, HIR-0162). Route
legality is derived from the write cluster and bound evidence; ``reason`` is
audit only and never satisfies a gate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from vfx_harness.domain.field_parsing import mapping as _mapping
from vfx_harness.domain.field_parsing import text as _text

UNIT_CONSTRUCTION_ROUTES = frozenset({"procedural", "generate", "retrieve", "simplify"})
NON_UNIT_CONSTRUCTION_ROUTES = frozenset({"omit", "abstain"})
GENERATE_WITNESS_PATTERN = re.compile(r"^refobs-[A-Za-z0-9]+$")
RETRIEVE_WITNESS_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
MESH_CONSTRUCTION_FAMILIES = frozenset({"mesh"})
SIMPLIFY_CONSTRUCTION_FAMILIES = frozenset({"mesh", "volume"})

CONSTRUCTION_ROUTE_RULE = (
    "a work unit's construction route is derived legality, not padding. "
    "procedural is the default. generate and retrieve require a mesh write family; "
    "generate also requires non-empty refobs-* witnesses and cannot bind a required "
    "object_count whose minimum exceeds 1 (instance assembly belongs on a successor). "
    "simplify requires a mesh or volume family. omit and abstain are not unit routes. "
    "reason is audit only and never satisfies this gate."
)
GENERATE_WITNESS_RULE = (
    "generate requires at least one witness matching ^refobs-[A-Za-z0-9]+$; "
    "prose isolate_regen, whole frames, and text-to-image with no crop handle "
    "are not witnesses"
)
NON_UNIT_ROUTE_RULE = (
    "omit is a typed requirement decision and abstain is a blocker or "
    "cannot_express_in_scope; neither is a work-unit construction route"
)
ASSET_STAGE_RETIRED_RULE = (
    "vfx asset is retired. Image-to-3D is a generate construction route on a "
    "mesh-family work unit. Mint refobs-* witnesses during layer materialization, "
    "then vfx run. Dual shot-root assets/ plus typed construction is forbidden "
    "(ADR-0004, ADR-0009)"
)

_CONSTRUCTION_FIELDS = frozenset({"route", "witnesses", "reason"})


def _strings(value: Any, where: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{where} must be a list")
    out = tuple(_text(item, f"{where}[{index}]") for index, item in enumerate(value))
    if len(set(out)) != len(out):
        raise ValueError(f"{where} contains duplicates")
    return out


@dataclass(frozen=True)
class ConstructionSpec:
    """Authored construction route. Default procedural is omitted from unit_digest."""

    route: str = "procedural"
    witnesses: tuple[str, ...] = ()
    reason: str = ""

    def is_default(self) -> bool:
        return self.route == "procedural" and not self.witnesses and not self.reason

    def as_authoring_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"route": self.route}
        if self.witnesses:
            payload["witnesses"] = list(self.witnesses)
        if self.reason:
            payload["reason"] = self.reason
        return payload


@dataclass(frozen=True, slots=True)
class ConstructionRouteGap:
    """One unit whose construction route is illegal for its derived write cluster."""

    unit_id: str
    code: str
    detail: str
    contract_ids: tuple[str, ...] = ()
    families: tuple[str, ...] = ()


def parse_construction(value: Any, where: str) -> ConstructionSpec:
    """Parse an optional construction object; omitted or empty is procedural."""
    if value in (None, {}):
        return ConstructionSpec()
    row = _mapping(value, where)
    extra = sorted(set(row) - _CONSTRUCTION_FIELDS)
    if extra:
        raise ValueError(f"{where} has unknown fields: {', '.join(extra)}")
    route = _text(row.get("route", "procedural"), f"{where}.route")
    if route in NON_UNIT_CONSTRUCTION_ROUTES:
        raise ValueError(f"{where}.route {route!r} is not a unit route. " + NON_UNIT_ROUTE_RULE)
    if route not in UNIT_CONSTRUCTION_ROUTES:
        raise ValueError(
            f"{where}.route {route!r} is unknown; declare one of "
            + ", ".join(sorted(UNIT_CONSTRUCTION_ROUTES))
        )
    witnesses = _strings(row.get("witnesses", []), f"{where}.witnesses") if row.get("witnesses") else ()
    if len(set(witnesses)) != len(witnesses):
        raise ValueError(f"{where}.witnesses must not contain duplicates")
    reason_raw = row.get("reason")
    if reason_raw in (None, ""):
        reason = ""
    else:
        reason = _text(reason_raw, f"{where}.reason")
    if route == "procedural" and witnesses:
        raise ValueError(
            f"{where}.witnesses are illegal on a procedural unit; they authorize "
            "generate or retrieve only"
        )
    if route == "generate":
        if not witnesses:
            raise ValueError(f"{where}.witnesses is empty. " + GENERATE_WITNESS_RULE)
        invalid = [item for item in witnesses if not GENERATE_WITNESS_PATTERN.fullmatch(item)]
        if invalid:
            raise ValueError(
                f"{where}.witnesses {invalid} do not match ^refobs-[A-Za-z0-9]+$. "
                + GENERATE_WITNESS_RULE
            )
    if route == "retrieve":
        if not witnesses:
            raise ValueError(
                f"{where}.witnesses is empty; retrieve requires at least one provenance id"
            )
        invalid = [item for item in witnesses if not RETRIEVE_WITNESS_PATTERN.fullmatch(item)]
        if invalid:
            raise ValueError(
                f"{where}.witnesses {invalid} are not provenance ids "
                "matching ^[A-Za-z0-9][A-Za-z0-9._-]*$"
            )
    return ConstructionSpec(route=route, witnesses=witnesses, reason=reason)
