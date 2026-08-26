"""RFC 6901 JSON pointers for materialization findings and patches.

A write-hook that says ``owner_layer must be 1`` without naming the field costs a
full-document rewrite per defect. Pointers make the repair one set.
"""

from __future__ import annotations

from typing import Any


def encode(*parts: str | int) -> str:
    """Build a pointer from path tokens. ``encode("scene_contracts", 2, "owner_layer")``."""
    if not parts:
        return ""
    return "/" + "/".join(_escape(str(part)) for part in parts)


def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def split(pointer: str) -> list[str]:
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise ValueError(f"JSON pointer must start with '/'; got {pointer!r}")
    return [token.replace("~1", "/").replace("~0", "~") for token in pointer.split("/")[1:]]


def format_finding(pointer: str, message: str) -> str:
    return f"{pointer}: {message}" if pointer else message


def _step(current: Any, token: str) -> Any:
    if isinstance(current, list):
        if token == "-":
            raise ValueError("JSON pointer '-' is not a get location")
        try:
            index = int(token)
        except ValueError as exc:
            raise ValueError(f"JSON pointer list index must be an integer; got {token!r}") from exc
        try:
            return current[index]
        except IndexError as exc:
            raise ValueError(f"JSON pointer index {index} is out of range") from exc
    if isinstance(current, dict):
        if token not in current:
            raise ValueError(f"JSON pointer key {token!r} is absent")
        return current[token]
    raise ValueError("JSON pointer walked into a scalar")


def get(document: Any, pointer: str) -> Any:
    current = document
    for token in split(pointer):
        current = _step(current, token)
    return current


def set_at(document: Any, pointer: str, value: Any) -> None:
    """Replace the value at ``pointer``. Creates a missing object key; does not create lists."""
    tokens = split(pointer)
    if not tokens:
        raise ValueError("cannot replace the document root through a patch")
    current = document
    for token in tokens[:-1]:
        current = _step(current, token)
    last = tokens[-1]
    if isinstance(current, list):
        if last == "-":
            current.append(value)
            return
        try:
            index = int(last)
        except ValueError as exc:
            raise ValueError(f"JSON pointer list index must be an integer; got {last!r}") from exc
        try:
            current[index] = value
        except IndexError as exc:
            raise ValueError(f"JSON pointer index {index} is out of range") from exc
        return
    if isinstance(current, dict):
        current[last] = value
        return
    raise ValueError("JSON pointer parent is a scalar")
