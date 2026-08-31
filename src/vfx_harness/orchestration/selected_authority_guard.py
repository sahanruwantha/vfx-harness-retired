"""Exact selected-authority guards for short durable state mutations."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TypeVar

from vfx_harness.orchestration.authority_selection import ResolvedSelectedAuthority
from vfx_harness.orchestration.authority_selection_heads import (
    AuthoritySelectionHeadError,
    read_authority_selection_heads,
)
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionConflict,
    authority_selection_lock,
    require_matching_authority_selection_token,
)

_T = TypeVar("_T")


def _require_current(
    shot_folder: str | Path,
    selected_authority: ResolvedSelectedAuthority,
    *,
    operation: str,
) -> None:
    try:
        observed = read_authority_selection_heads(shot_folder).token
        require_matching_authority_selection_token(
            selected_authority.selection_token,
            observed,
        )
    except (AuthoritySelectionConflict, AuthoritySelectionHeadError) as exc:
        raise AuthoritySelectionConflict(
            f"{operation} refused because selected authority changed or became invalid: "
            f"{exc}"
        ) from exc


def require_selected_authority_unchanged(
    shot_folder: str | Path,
    selected_authority: ResolvedSelectedAuthority,
    *,
    operation: str,
) -> None:
    """Check the exact plan/JIT heads without retaining the lock afterwards."""

    with authority_selection_lock(shot_folder, exclusive=False):
        _require_current(
            shot_folder,
            selected_authority,
            operation=operation,
        )


@contextmanager
def selected_authority_commit(
    shot_folder: str | Path,
    selected_authority: ResolvedSelectedAuthority,
    *,
    operation: str,
) -> Iterator[None]:
    """Hold the shared selection lock across one exact-token durable mutation."""

    with authority_selection_lock(shot_folder, exclusive=False):
        _require_current(
            shot_folder,
            selected_authority,
            operation=operation,
        )
        yield


def commit_selected_authority(
    shot_folder: str | Path,
    selected_authority: ResolvedSelectedAuthority,
    *,
    operation: str,
    mutation: Callable[[], _T],
) -> _T:
    """Run one short durable mutation behind an exact two-head CAS guard."""

    with selected_authority_commit(
        shot_folder,
        selected_authority,
        operation=operation,
    ):
        return mutation()
