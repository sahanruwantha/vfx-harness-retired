"""Closed path predicates shared by generic durable-write boundaries."""

from __future__ import annotations

from pathlib import Path

from vfx_harness.domain.shot_ledger_paths import is_shot_ledger_target
from vfx_harness.orchestration.authority_selection_process_registry import (
    AuthoritySelectionConflict,
)


def require_not_shot_ledger_target(relative: Path) -> None:
    """Reserve every rerooted ledger or lock name for its typed owner."""

    if is_shot_ledger_target(relative):
        raise AuthoritySelectionConflict(
            "canonical shot.json and shot.json.lock require the typed "
            "shot-ledger publication transaction"
        )


__all__ = ["require_not_shot_ledger_target"]
