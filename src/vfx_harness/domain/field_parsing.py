"""Shared strict parsers for identifier-bearing domain contracts."""

from __future__ import annotations

import re
from typing import Any

_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")


def mapping(value: Any, where: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{where} must be an object")
    return value


def text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where} must be a non-empty string")
    return value.strip()


def identifier(value: Any, where: str) -> str:
    result = text(value, where)
    if not _ID.fullmatch(result):
        raise ValueError(f"{where} has invalid id {result!r}")
    return result
