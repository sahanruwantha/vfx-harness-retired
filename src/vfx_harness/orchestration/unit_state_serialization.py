"""Sole byte encoding for durable ``work-unit-state/v1`` documents."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


class WorkUnitStateSerializationError(ValueError):
    """Work-unit state bytes are ambiguous, noncanonical, or not finite JSON."""


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise WorkUnitStateSerializationError(
                f"work-unit state contains duplicate JSON key {key!r}"
            )
        value[key] = item
    return value


def _reject_non_finite(value: str) -> None:
    raise WorkUnitStateSerializationError(
        f"work-unit state contains non-finite number {value!r}"
    )


def serialize_work_unit_state(value: Mapping[str, Any]) -> bytes:
    """Encode the established v1 state wire format exactly.

    ``ensure_ascii=True`` is deliberate compatibility with every durable state writer
    and the HIR-0171 preparer that established this representation.
    """

    if not isinstance(value, Mapping):
        raise WorkUnitStateSerializationError("work-unit state must be an object")
    try:
        return (
            json.dumps(
                dict(value),
                allow_nan=False,
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise WorkUnitStateSerializationError(
            f"work-unit state must contain finite JSON values: {exc}"
        ) from exc


def parse_work_unit_state_bytes(payload: bytes, where: str) -> dict[str, Any]:
    """Decode strict UTF-8 JSON and require the sole v1 producer representation."""

    if not isinstance(payload, bytes):
        raise WorkUnitStateSerializationError(f"{where} must be bytes")
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_non_finite,
        )
    except WorkUnitStateSerializationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkUnitStateSerializationError(
            f"{where} is not strict UTF-8 JSON: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise WorkUnitStateSerializationError(f"{where} must contain an object")
    if payload != serialize_work_unit_state(value):
        raise WorkUnitStateSerializationError(
            f"{where} does not use canonical work-unit-state/v1 bytes"
        )
    return value


__all__ = [
    "WorkUnitStateSerializationError",
    "parse_work_unit_state_bytes",
    "serialize_work_unit_state",
]
