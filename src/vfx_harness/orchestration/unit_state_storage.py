"""Durable storage primitives for one work-unit state document."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from vfx_harness.orchestration.unit_state_lock import (
    read_state_file_bytes,
    write_state_file_bytes,
)


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def read(path: Path) -> bytes | None:
    return read_state_file_bytes(path)


def write(path: Path, value: dict) -> None:
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    write_state_file_bytes(path, payload)
