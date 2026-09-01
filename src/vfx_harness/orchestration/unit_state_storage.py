"""Durable storage primitives for one work-unit state document."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from vfx_harness.orchestration.unit_state_lock import (
    read_state_file_bytes,
    write_state_file_bytes,
)
from vfx_harness.orchestration.unit_state_serialization import (
    serialize_work_unit_state,
)


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def read(path: Path) -> bytes | None:
    return read_state_file_bytes(path)


def write(path: Path, value: dict) -> None:
    write_state_file_bytes(path, serialize_work_unit_state(value))
