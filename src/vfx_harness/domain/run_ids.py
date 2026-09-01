"""Canonical safe identifiers for one harness invocation."""

from __future__ import annotations

import re

_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_RESERVED_RUN_IDS = frozenset({".", "..", "latest"})


def require_run_id(value: object, where: str = "run_id") -> str:
    """Return one path-component-safe run id or fail before authority is minted."""

    if (
        not isinstance(value, str)
        or value in _RESERVED_RUN_IDS
        or not _SAFE_RUN_ID.fullmatch(value)
    ):
        raise ValueError(
            f"{where} must match [A-Za-z0-9][A-Za-z0-9._-]* and must not be "
            "'.', '..', or 'latest'"
        )
    return value
