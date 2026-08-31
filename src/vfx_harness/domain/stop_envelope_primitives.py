"""Dependency-free strict parsing and canonical digest helpers for stop contracts."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")


def canonical_digest(value: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(value, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("stop contract payload must be finite canonical JSON") from exc
    return hashlib.sha256(encoded).hexdigest()


def require_text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{where} must be a non-empty trimmed string")
    return value


def require_id(value: Any, where: str) -> str:
    value = require_text(value, where)
    if not _ID.fullmatch(value):
        raise ValueError(f"{where} must be an identifier")
    return value


def require_digest(value: Any, where: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"{where} must be a lowercase SHA-256 digest")
    return value


def require_optional_digest(value: Any, where: str) -> str | None:
    return None if value is None else require_digest(value, where)


def require_optional_id(value: Any, where: str) -> str | None:
    return None if value is None else require_id(value, where)


def require_text_tuple(value: Any, where: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, tuple) or (not value and not allow_empty):
        qualifier = "" if allow_empty else " non-empty"
        raise ValueError(f"{where} must be a{qualifier} tuple")
    rows = tuple(require_id(item, f"{where}[{index}]") for index, item in enumerate(value))
    if len(rows) != len(set(rows)):
        raise ValueError(f"{where} contains duplicates")
    return tuple(sorted(rows))


def record(value: Any, where: str, schema: str, fields: tuple[str, ...]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{where} must be an object")
    expected = {"schema", *fields}
    found = set(value)
    if found != expected:
        missing = sorted(expected - found)
        unexpected = sorted(found - expected)
        raise ValueError(f"{where} fields mismatch; missing={missing}; unexpected={unexpected}")
    if value["schema"] != schema:
        raise ValueError(f"{where}.schema must be {schema!r}, found {value['schema']!r}")
    return value


def list_value(value: Any, where: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{where} must be a list")
    return value


def text_tuple_from_list(value: Any, where: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    return require_text_tuple(tuple(list_value(value, where)), where, allow_empty=allow_empty)


def require_canonical_digest(found: Any, expected: str, where: str, field: str) -> None:
    require_digest(found, f"{where}.{field}")
    if found != expected:
        raise ValueError(f"{where}.{field} is stale; expected {expected!r}, found {found!r}")


def require_sequence(value: Any, where: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{where} must be a sequence")
    return value
