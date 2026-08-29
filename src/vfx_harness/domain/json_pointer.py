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


def _list_index(token: str, current: list[Any], *, allow_append: bool) -> int:
    """Parse one RFC 6901 array token and teach the exact legal locations."""
    if token == "-":
        if allow_append:
            return len(current)
        raise ValueError("JSON pointer '-' is not a get location")
    if not token.isascii() or not token.isdecimal():
        raise ValueError(
            f"JSON pointer list index must be a non-negative integer; got {token!r}. "
            + _list_location_card(current, allow_append=allow_append)
        )
    index = int(token)
    if index >= len(current):
        raise ValueError(
            f"JSON pointer index {index} is out of range. "
            + _list_location_card(current, allow_append=allow_append)
        )
    return index


def _list_location_card(current: list[Any], *, allow_append: bool) -> str:
    """Describe list occupancy without making the caller rediscover indices."""
    length = len(current)
    if length:
        locations = f"valid existing indices are 0..{length - 1}"
    else:
        locations = "there are no existing indices"
    identified = [
        f"{index}:{row['id']}"
        for index, row in enumerate(current)
        if isinstance(row, dict) and str(row.get("id") or "").strip()
    ]
    identities = f"; indexed ids are [{', '.join(identified)}]" if identified else ""
    append = "; use '-' as the final token to append" if allow_append else ""
    return f"list length is {length}; {locations}{identities}{append}"


def _step(current: Any, token: str) -> Any:
    if isinstance(current, list):
        return current[_list_index(token, current, allow_append=False)]
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
        current[_list_index(last, current, allow_append=True)] = value
        return
    if isinstance(current, dict):
        current[last] = value
        return
    raise ValueError("JSON pointer parent is a scalar")
