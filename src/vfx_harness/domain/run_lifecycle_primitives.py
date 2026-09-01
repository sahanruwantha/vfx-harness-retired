"""Dependency-light strict parsing helpers for run-lifecycle records."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any

from vfx_harness.domain.stop_envelope_primitives import (
    require_digest,
    require_text,
)


def exact_record(
    value: object,
    where: str,
    schema: str,
    fields: frozenset[str],
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{where} must be an object")
    expected = {"schema", *fields}
    found = set(value)
    if found != expected:
        raise ValueError(
            f"{where} fields mismatch; missing={sorted(expected - found)}; unexpected={sorted(found - expected)}"
        )
    if value.get("schema") != schema:
        raise ValueError(f"{where}.schema must be {schema!r}, found {value.get('schema')!r}")
    return value


def positive_int(value: object, where: str, *, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{where} must be a {qualifier} integer")
    return value


def timestamp(value: object, where: str) -> str:
    text = require_text(value, where)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{where} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{where} must include a UTC offset")
    return text


def chronological(before: str, after: str, where: str) -> None:
    earlier = datetime.fromisoformat(before.replace("Z", "+00:00"))
    later = datetime.fromisoformat(after.replace("Z", "+00:00"))
    if later < earlier:
        raise ValueError(f"{where} timestamps are out of order")


def relative_locator(value: object, where: str) -> str:
    text = require_text(value, where)
    path = PurePosixPath(text)
    if (
        path.is_absolute()
        or text == "."
        or "\\" in text
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != text
    ):
        raise ValueError(f"{where} must be a canonical relative POSIX path")
    return text


def optional_pair(
    locator: object,
    digest: object,
    where: str,
) -> tuple[str | None, str | None]:
    if locator is None and digest is None:
        return None, None
    if locator is None or digest is None:
        raise ValueError(f"{where} locator and digest must appear together")
    return relative_locator(locator, f"{where} locator"), require_digest(
        digest,
        f"{where} digest",
    )


def supervisor_wait_result(
    wait_status: int | None,
) -> tuple[int | None, int | None]:
    """Decode one exact POSIX supervisor wait status without reading process state."""

    if wait_status is None:
        return None, None
    status = positive_int(
        wait_status,
        "owner-loss supervisor_wait_status",
        allow_zero=True,
    )
    if status > 0xFFFF:
        raise ValueError("owner-loss supervisor_wait_status must fit an unsigned 16-bit wait status")
    low = status & 0x7F
    if low == 0:
        return None, (status >> 8) & 0xFF
    if low == 0x7F:
        raise ValueError("a stopped process is not an observed owner loss")
    return low, 128 + low
