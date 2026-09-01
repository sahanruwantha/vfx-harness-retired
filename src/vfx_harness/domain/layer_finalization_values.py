"""Shared JSON-value primitives for layer-finalization contracts."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from vfx_harness.domain.stop_envelope_primitives import canonical_digest


def json_value(value: object, where: str) -> Any:
    """Return a detached finite JSON value without lossy key coercion."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{where} must contain only finite JSON values")
        return value
    if isinstance(value, Mapping):
        row: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{where} object keys must be strings")
            row[key] = json_value(item, f"{where}.{key}")
        return row
    if isinstance(value, list):
        return [json_value(item, f"{where}[{index}]") for index, item in enumerate(value)]
    raise ValueError(f"{where} must contain only JSON values")


def json_object(value: object, where: str) -> dict[str, Any]:
    """Return one detached, canonical-encodable JSON object."""

    if not isinstance(value, Mapping):
        raise ValueError(f"{where} must be an object")
    row = json_value(value, where)
    assert isinstance(row, dict)
    # Exercise the canonical encoder now so receipt construction cannot defer a
    # serialization failure until publication.
    canonical_digest(row)
    return row


def json_object_rows(
    value: object,
    where: str,
) -> tuple[dict[str, Any], ...]:
    """Return an ordered tuple of detached, canonical-encodable JSON objects."""

    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{where} must be an ordered list of objects")
    return tuple(json_object(item, f"{where}[{index}]") for index, item in enumerate(value))


def semantic_digest(value: Mapping[str, Any]) -> str:
    """Digest evidence identity while deliberately excluding audit time."""

    return canonical_digest(dict(value))


__all__ = ["json_object", "json_object_rows", "json_value", "semantic_digest"]
