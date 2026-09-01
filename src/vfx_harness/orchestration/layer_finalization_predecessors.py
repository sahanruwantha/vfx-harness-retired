"""Exact selected-DAG predecessor authority for layer finalization.

The builder normally supplies the cumulative replay prefix, but caller convention is
not an authority boundary.  Every claim and guard recompiles the stable topological
prefix from the selected plan/JIT view and compares the caller's typed rows exactly.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from vfx_harness.domain.brief import Shot
from vfx_harness.domain.layer_finalizations import (
    LayerFinalizationPredecessorInput,
)
from vfx_harness.orchestration.authority_selection import (
    ResolvedSelectedAuthority,
    resolve_selected_authority,
)
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionToken,
    require_matching_authority_selection_token,
)
from vfx_harness.orchestration.selected_layer_chain import selected_layer_chain

if TYPE_CHECKING:
    from vfx_harness.orchestration.ledger import Layer


class LayerFinalizationPredecessorConflict(ValueError):
    """A finalization prefix differs from the selected stable DAG prefix."""


def selected_finalization_predecessor_layers(
    folder: str | Path,
    layer_id: str,
    *,
    selection_token: AuthoritySelectionToken,
    selected_authority: ResolvedSelectedAuthority | None = None,
) -> tuple[Layer, ...]:
    """Compile the exact selected stable-topological prefix before ``layer_id``."""

    root = Path(folder).expanduser().absolute()
    selected = selected_authority or resolve_selected_authority(root)
    try:
        require_matching_authority_selection_token(
            selection_token,
            selected.selection_token,
        )
        chain = selected_layer_chain(
            Shot(folder=root, frontmatter={}, body=""),
            selected_authority=selected,
        )
    except (OSError, TypeError, ValueError) as exc:
        raise LayerFinalizationPredecessorConflict(str(exc)) from exc
    matches = tuple(
        index for index, candidate in enumerate(chain) if str(candidate.id) == str(layer_id)
    )
    if len(matches) != 1:
        raise LayerFinalizationPredecessorConflict(
            f"layer {layer_id!r} is not unique in the selected stable DAG"
        )
    return chain[: matches[0]]


def require_exact_selected_finalization_predecessors(
    folder: str | Path,
    layer_id: str,
    inputs: Sequence[LayerFinalizationPredecessorInput],
    *,
    selection_token: AuthoritySelectionToken,
    selected_authority: ResolvedSelectedAuthority | None = None,
) -> tuple[Layer, ...]:
    """Reject omitted, reordered, extra, or path-substituted prefix rows."""

    if any(not isinstance(row, LayerFinalizationPredecessorInput) for row in inputs):
        raise LayerFinalizationPredecessorConflict(
            "layer finalization predecessor prefix must contain typed inputs"
        )
    expected = selected_finalization_predecessor_layers(
        folder,
        str(layer_id),
        selection_token=selection_token,
        selected_authority=selected_authority,
    )
    expected_identity = tuple((str(layer.id), str(layer.script)) for layer in expected)
    observed_identity = tuple((row.layer_id, row.script_path) for row in inputs)
    if observed_identity != expected_identity:
        raise LayerFinalizationPredecessorConflict(
            f"layer {layer_id!r} finalization predecessor prefix differs from the "
            "selected stable DAG; "
            f"expected={expected_identity!r}; observed={observed_identity!r}"
        )
    return expected


__all__ = [
    "LayerFinalizationPredecessorConflict",
    "require_exact_selected_finalization_predecessors",
    "selected_finalization_predecessor_layers",
]
