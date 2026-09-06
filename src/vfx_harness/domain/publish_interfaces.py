"""Typed successor publish interfaces (HIR-0084).

Kinds are a closed registry. Export values are role tokens, control tokens, or
sealed contract ids — never producer prose. Producer digest is filled at compile
time; validity is derived from durable unit state, not a parallel lifecycle.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from vfx_harness.domain.claim_bindings import bound_claim_contract_ids
from vfx_harness.domain.field_parsing import identifier as _id
from vfx_harness.domain.field_parsing import mapping as _mapping
from vfx_harness.domain.field_parsing import text as _text

if TYPE_CHECKING:
    from vfx_harness.domain.work_units.unit import WorkUnit

SCHEMA = "vfx-harness.publish-interface/v1"
PUBLISH_INTERFACE_KINDS = frozenset(
    {
        "asset_source",
        "instance_source",
        "placement_control",
        "material_slot",
        "light_control",
        "volume_control",
        "cache_output",
        "render_pass",
    }
)
_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
REFERENCE_ONLY_EXPORTS_RULE = (
    "every publish-interface export value is a mutation role, dress selector, "
    "control token, or bound contract id. Scalars such as origin/pivot/dimensions "
    "are exported only as the contract id that measured them. Producer prose "
    f"({SCHEMA} notwithstanding) is not an executable fact."
)
UNKNOWN_KIND_RULE = (
    f"publish-interface kind must be one of {sorted(PUBLISH_INTERFACE_KINDS)}; "
    "unknown kinds are rejected the same way as an unknown look-vector metric."
)

_FAMILY_KIND = {
    "mesh": "asset_source",
    "camera": "placement_control",
    "light": "light_control",
    "volume": "volume_control",
    "compositor": "render_pass",
    "shading": "material_slot",
    "keyframe": "placement_control",
    "control": "placement_control",
}


def _export_key(value: Any, where: str) -> str:
    key = _text(value, where)
    if not _ID.fullmatch(key):
        raise ValueError(f"{where} has invalid export key {key!r}")
    return key


@dataclass(frozen=True, slots=True)
class PublishInterface:
    id: str
    kind: str
    exports: tuple[tuple[str, str], ...]
    producer_layer_id: str
    producer_unit_id: str
    producer_unit_digest: str

    def as_dict(self) -> dict[str, Any]:
        body = {
            "id": self.id,
            "kind": self.kind,
            "schema": SCHEMA,
            "producer": {
                "layer_id": self.producer_layer_id,
                "unit_id": self.producer_unit_id,
                "unit_digest": self.producer_unit_digest,
            },
            "exports": dict(self.exports),
        }
        body["digest"] = interface_digest(body)
        return body


def interface_digest(document: Mapping[str, Any]) -> str:
    payload = {key: value for key, value in document.items() if key != "digest"}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def legal_export_tokens(
    unit: WorkUnit,
    bound_contract_ids: Iterable[str],
) -> frozenset[str]:
    return frozenset(
        {
            *unit.mutates.roles,
            *unit.mutates.dresses,
            *unit.mutates.controls,
            *(str(item) for item in bound_contract_ids if str(item)),
        }
    )


def parse_publish_interface(
    value: Any,
    where: str,
    *,
    legal_tokens: Iterable[str],
    layer_id: str,
    unit_id: str,
    unit_digest: str = "",
) -> PublishInterface:
    row = _mapping(value, where)
    kind = _text(row.get("kind"), f"{where}.kind")
    if kind not in PUBLISH_INTERFACE_KINDS:
        raise ValueError(f"{where}.kind {kind!r} is unknown. {UNKNOWN_KIND_RULE}")
    schema = row.get("schema", SCHEMA)
    if schema != SCHEMA:
        raise ValueError(f"{where}.schema must be {SCHEMA}")
    extra = sorted(
        set(row) - {"id", "kind", "schema", "producer", "exports", "digest"}
    )
    if extra:
        raise ValueError(f"{where} has unknown fields: {', '.join(extra)}")
    raw_exports = row.get("exports")
    if not isinstance(raw_exports, dict) or not raw_exports:
        raise ValueError(f"{where}.exports must be a non-empty object")
    allowed = {str(item) for item in legal_tokens}
    exports: list[tuple[str, str]] = []
    for key, token in raw_exports.items():
        slot = _export_key(key, f"{where}.exports key")
        found = _text(token, f"{where}.exports.{slot}")
        if found not in allowed:
            raise ValueError(
                f"{where}.exports.{slot} value {found!r} is not a role, control, "
                f"or bound contract id. {REFERENCE_ONLY_EXPORTS_RULE}"
            )
        exports.append((slot, found))
    producer = row.get("producer")
    layer = str(layer_id)
    uid = str(unit_id)
    digest = str(unit_digest or "")
    if producer is not None:
        block = _mapping(producer, f"{where}.producer")
        if block.get("layer_id") not in {None, layer}:
            raise ValueError(f"{where}.producer.layer_id must match the producing layer")
        if block.get("unit_id") not in {None, uid}:
            raise ValueError(f"{where}.producer.unit_id must match the producing unit")
    return PublishInterface(
        _id(row.get("id"), f"{where}.id"),
        kind,
        tuple(exports),
        layer,
        uid,
        digest,
    )


def kind_for_instrument_family(family: str) -> str:
    kind = _FAMILY_KIND.get(str(family) or "control")
    if kind is None:
        raise ValueError(f"unknown instrument family {family!r}")
    return kind


def derived_family_for_unit(unit: WorkUnit) -> str:
    provides = {str(item) for item in unit.provides}
    if "camera" in provides:
        return "camera"
    if "geometry" in provides:
        return "mesh"
    if unit.mutates.dresses and not unit.mutates.roles:
        return "shading"
    return "control"


def derived_interface_key(unit: WorkUnit) -> tuple[str, str] | None:
    """Id and kind of the harness-derived successor interface, if any."""
    if not (
        unit.mutates.roles
        or unit.mutates.dresses
        or unit.mutates.controls
        or bound_claim_contract_ids(unit)
    ):
        return None
    return (f"{unit.id}.publish", kind_for_instrument_family(derived_family_for_unit(unit)))


def derive_publish_interface(
    unit: WorkUnit,
    *,
    layer_id: str,
    bound_contract_ids: Sequence[str],
    instrument_family: str = "control",
    unit_digest: str = "",
) -> PublishInterface | None:
    """One named interface from mutation tokens and bound contract ids."""
    exports: list[tuple[str, str]] = []
    if unit.mutates.roles:
        exports.append(("role", unit.mutates.roles[0]))
    elif unit.mutates.dresses:
        exports.append(("role", unit.mutates.dresses[0]))
    if unit.mutates.controls:
        exports.append(("control", unit.mutates.controls[0]))
    for index, contract_id in enumerate(bound_contract_ids):
        key = "mesh_contract_id" if index == 0 else f"contract_{index}_id"
        exports.append((key, str(contract_id)))
    if not exports:
        return None
    legal = legal_export_tokens(unit, bound_contract_ids)
    # Keys are harness-assigned; values still have to be legal tokens.
    for _key, token in exports:
        if token not in legal:
            return None
    return PublishInterface(
        f"{unit.id}.publish",
        kind_for_instrument_family(instrument_family),
        tuple(exports),
        str(layer_id),
        unit.id,
        str(unit_digest or ""),
    )


def compile_unit_publish_interfaces(
    unit: WorkUnit,
    *,
    layer_id: str,
    bound_contract_ids: Sequence[str],
    authored: Any = None,
    instrument_family: str = "control",
    unit_digest: str = "",
) -> tuple[PublishInterface, ...]:
    """Authored interfaces if present and valid; otherwise one derived interface."""
    legal = legal_export_tokens(unit, bound_contract_ids)
    source = authored
    if source is None and unit.publishes:
        source = [spec.as_authoring_dict() for spec in unit.publishes]
    if source is None:
        derived = derive_publish_interface(
            unit,
            layer_id=layer_id,
            bound_contract_ids=bound_contract_ids,
            instrument_family=instrument_family,
            unit_digest=unit_digest,
        )
        return (derived,) if derived is not None else ()
    if not isinstance(source, list) or not source:
        raise ValueError(
            f"unit {unit.id} publishes must be a non-empty list when declared"
        )
    return tuple(
        parse_publish_interface(
            row,
            f"unit {unit.id}.publishes[{index}]",
            legal_tokens=legal,
            layer_id=layer_id,
            unit_id=unit.id,
            unit_digest=unit_digest,
        )
        for index, row in enumerate(source)
    )


def exported_role_tokens(interfaces: Iterable[PublishInterface]) -> frozenset[str]:
    return frozenset(
        value
        for interface in interfaces
        for key, value in interface.exports
        if key == "role" or key.endswith("_role")
    )


def exported_role_tokens_from_unit(unit: WorkUnit) -> frozenset[str]:
    """Role tokens a successor may read from this producer, never mutate."""
    if unit.publishes:
        return exported_role_tokens(unit.publishes)
    if unit.mutates.roles:
        return frozenset({unit.mutates.roles[0]})
    if unit.mutates.dresses:
        return frozenset({unit.mutates.dresses[0]})
    return frozenset()


def exported_selector_tokens_from_interface(
    unit: WorkUnit,
    *,
    interface_id: str,
    kind: str,
    selector_type: str,
) -> frozenset[str]:
    """Selectors exported by one exact offered interface, never the producer union."""
    if selector_type not in {"role", "control"}:
        raise ValueError("selector_type must be role or control")
    if unit.publishes:
        spec = next(
            (
                item
                for item in unit.publishes
                if item.id == interface_id and item.kind == kind
            ),
            None,
        )
        if spec is None:
            return frozenset()
        return frozenset(
            value
            for key, value in spec.exports
            if key == selector_type or key.endswith(f"_{selector_type}")
        )
    if derived_interface_key(unit) != (interface_id, kind):
        return frozenset()
    if selector_type == "role":
        if unit.mutates.roles:
            return frozenset({unit.mutates.roles[0]})
        if unit.mutates.dresses:
            return frozenset({unit.mutates.dresses[0]})
        return frozenset()
    return frozenset(unit.mutates.controls[:1])
