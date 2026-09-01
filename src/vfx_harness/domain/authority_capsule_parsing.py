"""Strict decoding of immutable HIR-0171 authority-capsule records.

Capsule compilation owns semantic projection from plan documents.  Historical
transition evaluation, however, must reopen the exact compiled capsule set named by
an immutable predecessor head.  This module validates that closed wire record and
reconstructs the typed value without consulting mutable plan/JIT files.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from vfx_harness.domain.authority_capsules import (
    AUTHORITY_CAPSULE_SET_SCHEMA,
    LAYER_AUTHORITY_CAPSULE_SCHEMA,
    UNIT_AUTHORITY_CAPSULE_SCHEMA,
    AuthorityCapsuleError,
    AuthorityCapsuleSet,
    LayerAuthorityCapsule,
    UnitAuthorityCapsule,
)
from vfx_harness.domain.authority_head_records import OVERLAY_ARTIFACTS

_CAPSULE_SET_FIELDS = frozenset(
    {"schema", "provenance", "units", "layers", "capsule_set_digest"}
)
_PROVENANCE_FIELDS = frozenset(
    {
        "global_authority_digest",
        "effective_authority_digest",
        "document_schemas",
    }
)
_UNIT_FIELDS = frozenset(
    {
        "schema",
        "layer_id",
        "unit_id",
        "work_unit_digest",
        "projection",
        "capsule_digest",
    }
)
_LAYER_FIELDS = frozenset(
    {
        "schema",
        "layer_id",
        "unit_capsules",
        "predecessor_layers",
        "projection",
        "capsule_digest",
    }
)
_EXPECTED_DOCUMENT_SCHEMAS = {
    "layers.json": "5",
    "scene_checks.json": "2",
    "checks.json": "2",
    "requirements.json": "vfx-harness.requirements/v2",
    "acceptance.json": "vfx-harness.acceptance-array/v1",
}


def _canonical_bytes(value: Any, where: str) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AuthorityCapsuleError(
            f"{where} must contain finite canonical JSON values: {exc}"
        ) from exc


def _copy(value: Any, where: str) -> Any:
    return json.loads(_canonical_bytes(value, where))


def _digest(value: Any, where: str) -> str:
    return hashlib.sha256(_canonical_bytes(value, where)).hexdigest()


def _mapping(value: object, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise AuthorityCapsuleError(f"{where} must be an object with string keys")
    return value


def _fields(value: Mapping[str, Any], expected: frozenset[str], where: str) -> None:
    if set(value) != expected:
        raise AuthorityCapsuleError(
            f"{where} fields mismatch; missing={sorted(expected - set(value))}; "
            f"unexpected={sorted(set(value) - expected)}"
        )


def _text(value: object, where: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise AuthorityCapsuleError(f"{where} must be a non-empty trimmed string")
    return value


def _sha256(value: object, where: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise AuthorityCapsuleError(f"{where} must be a lowercase SHA-256 digest")
    return value


def _rows(value: object, where: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(row, Mapping) for row in value):
        raise AuthorityCapsuleError(f"{where} must be a list of objects")
    return list(value)


def _digest_pairs(
    value: object,
    *,
    id_field: str,
    where: str,
) -> tuple[tuple[str, str], ...]:
    pairs: list[tuple[str, str]] = []
    for index, row in enumerate(_rows(value, where)):
        if set(row) != {id_field, "digest"}:
            raise AuthorityCapsuleError(
                f"{where}[{index}] must contain exactly {id_field!r} and 'digest'"
            )
        pairs.append(
            (
                _text(row[id_field], f"{where}[{index}].{id_field}"),
                _sha256(row["digest"], f"{where}[{index}].digest"),
            )
        )
    if len(pairs) != len({identifier for identifier, _digest_value in pairs}):
        raise AuthorityCapsuleError(f"{where} contains duplicate identifiers")
    return tuple(pairs)


def _unit(value: Mapping[str, Any], where: str) -> UnitAuthorityCapsule:
    _fields(value, _UNIT_FIELDS, where)
    if value.get("schema") != UNIT_AUTHORITY_CAPSULE_SCHEMA:
        raise AuthorityCapsuleError(f"{where}.schema is unsupported")
    layer_id = _text(value["layer_id"], f"{where}.layer_id")
    unit_id = _text(value["unit_id"], f"{where}.unit_id")
    projection = _copy(_mapping(value["projection"], f"{where}.projection"), where)
    work_identity = _mapping(projection.get("work_unit"), f"{where}.projection.work_unit")
    if (
        set(work_identity) != {"schema", "layer_id", "unit_id", "row"}
        or work_identity.get("schema")
        != "vfx-harness.work-unit-authority-identity/v1"
        or work_identity.get("layer_id") != layer_id
        or work_identity.get("unit_id") != unit_id
    ):
        raise AuthorityCapsuleError(
            f"{where}.projection.work_unit does not name the exact unit identity"
        )
    work_unit_digest = _sha256(
        value["work_unit_digest"], f"{where}.work_unit_digest"
    )
    if _digest(work_identity, f"{where}.projection.work_unit") != work_unit_digest:
        raise AuthorityCapsuleError(f"{where}.work_unit_digest is stale")
    capsule_digest = _sha256(value["capsule_digest"], f"{where}.capsule_digest")
    payload = {
        "schema": UNIT_AUTHORITY_CAPSULE_SCHEMA,
        "layer_id": layer_id,
        "unit_id": unit_id,
        "work_unit_digest": work_unit_digest,
        "projection": projection,
    }
    if _digest(payload, where) != capsule_digest:
        raise AuthorityCapsuleError(f"{where}.capsule_digest is stale")
    return UnitAuthorityCapsule(
        layer_id,
        unit_id,
        work_unit_digest,
        capsule_digest,
        _canonical_bytes(projection, f"{where}.projection").decode("utf-8"),
    )


def _layer(value: Mapping[str, Any], where: str) -> LayerAuthorityCapsule:
    _fields(value, _LAYER_FIELDS, where)
    if value.get("schema") != LAYER_AUTHORITY_CAPSULE_SCHEMA:
        raise AuthorityCapsuleError(f"{where}.schema is unsupported")
    layer_id = _text(value["layer_id"], f"{where}.layer_id")
    unit_pairs = _digest_pairs(
        value["unit_capsules"],
        id_field="unit_id",
        where=f"{where}.unit_capsules",
    )
    predecessor_pairs = _digest_pairs(
        value["predecessor_layers"],
        id_field="layer_id",
        where=f"{where}.predecessor_layers",
    )
    projection = _copy(_mapping(value["projection"], f"{where}.projection"), where)
    if (
        projection.get("unit_capsules") != list(value["unit_capsules"])
        or projection.get("predecessor_layers") != list(value["predecessor_layers"])
    ):
        raise AuthorityCapsuleError(
            f"{where}.projection does not close on its unit/predecessor digests"
        )
    capsule_digest = _sha256(value["capsule_digest"], f"{where}.capsule_digest")
    payload = {
        "schema": LAYER_AUTHORITY_CAPSULE_SCHEMA,
        "layer_id": layer_id,
        "unit_capsules": list(value["unit_capsules"]),
        "predecessor_layers": list(value["predecessor_layers"]),
        "projection": projection,
    }
    if _digest(payload, where) != capsule_digest:
        raise AuthorityCapsuleError(f"{where}.capsule_digest is stale")
    return LayerAuthorityCapsule(
        layer_id,
        unit_pairs,
        predecessor_pairs,
        capsule_digest,
        _canonical_bytes(projection, f"{where}.projection").decode("utf-8"),
    )


def parse_authority_capsule_set(
    value: object,
    where: str = "authority capsule set",
) -> AuthorityCapsuleSet:
    """Strictly reconstruct one closed immutable capsule-set record."""

    row = _mapping(value, where)
    _fields(row, _CAPSULE_SET_FIELDS, where)
    if row.get("schema") != AUTHORITY_CAPSULE_SET_SCHEMA:
        raise AuthorityCapsuleError(f"{where}.schema is unsupported")
    provenance = _mapping(row["provenance"], f"{where}.provenance")
    _fields(provenance, _PROVENANCE_FIELDS, f"{where}.provenance")
    global_digest = _sha256(
        provenance["global_authority_digest"],
        f"{where}.provenance.global_authority_digest",
    )
    effective_digest = _sha256(
        provenance["effective_authority_digest"],
        f"{where}.provenance.effective_authority_digest",
    )
    schemas = _mapping(
        provenance["document_schemas"],
        f"{where}.provenance.document_schemas",
    )
    if dict(schemas) != _EXPECTED_DOCUMENT_SCHEMAS or set(schemas) != set(
        OVERLAY_ARTIFACTS
    ):
        raise AuthorityCapsuleError(
            f"{where}.provenance.document_schemas is not the closed v1 vocabulary"
        )
    units = tuple(
        _unit(unit, f"{where}.units[{index}]")
        for index, unit in enumerate(_rows(row["units"], f"{where}.units"))
    )
    layers = tuple(
        _layer(layer, f"{where}.layers[{index}]")
        for index, layer in enumerate(_rows(row["layers"], f"{where}.layers"))
    )
    layer_ids = [layer.layer_id for layer in layers]
    if len(layer_ids) != len(set(layer_ids)):
        raise AuthorityCapsuleError(f"{where}.layers contains duplicate layer ids")
    unit_keys = [(unit.layer_id, unit.unit_id) for unit in units]
    if len(unit_keys) != len(set(unit_keys)):
        raise AuthorityCapsuleError(f"{where}.units contains duplicate layer/unit ids")
    unit_by_key = {(unit.layer_id, unit.unit_id): unit for unit in units}
    expected_unit_order: list[tuple[str, str]] = []
    seen_layers: dict[str, str] = {}
    for layer in layers:
        for predecessor_id, predecessor_digest in layer.predecessor_layer_digests:
            if seen_layers.get(predecessor_id) != predecessor_digest:
                raise AuthorityCapsuleError(
                    f"{where} layer {layer.layer_id!r} has a stale or non-prior "
                    f"predecessor {predecessor_id!r}"
                )
        for unit_id, unit_digest in layer.unit_capsule_digests:
            unit = unit_by_key.get((layer.layer_id, unit_id))
            if unit is None or unit.capsule_digest != unit_digest:
                raise AuthorityCapsuleError(
                    f"{where} layer {layer.layer_id!r} has a stale unit capsule "
                    f"binding for {unit_id!r}"
                )
            expected_unit_order.append((layer.layer_id, unit_id))
        seen_layers[layer.layer_id] = layer.capsule_digest
    if expected_unit_order != unit_keys:
        raise AuthorityCapsuleError(
            f"{where}.units is not the exact layer-declared unit sequence"
        )
    capsule_set_digest = _sha256(
        row["capsule_set_digest"], f"{where}.capsule_set_digest"
    )
    payload = {
        "schema": AUTHORITY_CAPSULE_SET_SCHEMA,
        "provenance": _copy(provenance, f"{where}.provenance"),
        "units": [_copy(unit, f"{where}.units") for unit in row["units"]],
        "layers": [_copy(layer, f"{where}.layers") for layer in row["layers"]],
    }
    if _digest(payload, where) != capsule_set_digest:
        raise AuthorityCapsuleError(f"{where}.capsule_set_digest is stale")
    return AuthorityCapsuleSet(
        global_digest,
        effective_digest,
        tuple((name, str(schemas[name])) for name in OVERLAY_ARTIFACTS),
        units,
        layers,
        capsule_set_digest,
    )


__all__ = ["parse_authority_capsule_set"]
