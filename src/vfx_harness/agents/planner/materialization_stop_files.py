"""Content-bound file records shared by materialization stop evidence readers."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def read_file(path: Path, *, locator: str) -> tuple[dict[str, Any], bytes | None]:
    try:
        payload = path.read_bytes()
    except FileNotFoundError:
        return {"locator": locator, "state": "missing"}, None
    except OSError:
        return {"locator": locator, "state": "unreadable"}, None
    return {
        "locator": locator,
        "state": "present",
        "sha256": _sha256(payload),
        "bytes": len(payload),
    }, payload


def content_record(record: dict[str, Any]) -> dict[str, Any]:
    """Content identity only; the locator remains audit metadata."""

    return {
        key: record[key]
        for key in ("state", "sha256", "bytes")
        if key in record
    }
