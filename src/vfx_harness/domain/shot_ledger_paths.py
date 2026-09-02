"""Pure identity predicate for the reserved canonical shot-ledger namespace."""

from __future__ import annotations

from pathlib import Path


def is_shot_ledger_target(path: str | Path) -> bool:
    """Return whether a lexical target names the ledger or its physical lock."""

    return Path(path).name in {"shot.json", "shot.json.lock"}


__all__ = ["is_shot_ledger_target"]
