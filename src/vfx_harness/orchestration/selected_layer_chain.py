"""One dependency-ordered layer chain shared by replay and publication boundaries."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING

from vfx_harness.domain.brief import Shot
from vfx_harness.domain.work_units import (
    strict_topological_sparse_layer_ids,
)
from vfx_harness.orchestration.authority_selection import (
    ResolvedSelectedAuthority,
    SelectedAuthorityResolutionError,
    resolve_selected_authority,
)
from vfx_harness.orchestration.ledger import Layer, load_layers_from_path

if TYPE_CHECKING:
    from vfx_harness.orchestration.plan_authority import PlanBundle


def _global_layer_rows(bundle: PlanBundle) -> list[Mapping[str, object]]:
    path = bundle.root / "layers.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"selected global layer DAG is unreadable: {path}") from exc
    rows = document.get("layers") if isinstance(document, Mapping) else None
    if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
        raise ValueError("selected global layer DAG must contain layer objects")
    return list(rows)


def selected_layer_chain(
    shot: Shot,
    *,
    bundle: PlanBundle | None = None,
    selected_authority: ResolvedSelectedAuthority | None = None,
    expected_bundle_digest: str | None = None,
) -> tuple[Layer, ...]:
    """Return current executable layers in the selected global DAG's stable order."""

    try:
        selected = selected_authority or resolve_selected_authority(shot.folder)
    except SelectedAuthorityResolutionError as exc:
        raise ValueError(str(exc)) from exc
    if selected.plan is None or selected.assertion.effective_view is None:
        raise ValueError("selected layer chain requires selected plan authority")
    selected_bundle = selected.plan.bundle
    if bundle is not None and bundle != selected_bundle:
        raise ValueError("selected layer chain bundle disagrees with its authority snapshot")
    if (
        expected_bundle_digest is not None
        and selected_bundle.content_hash != expected_bundle_digest
    ):
        raise ValueError("selected layer chain changed plan generation before replay")
    global_rows = _global_layer_rows(selected_bundle)
    global_ids = [str(row.get("id") or "").strip() for row in global_rows]
    if any(not layer_id for layer_id in global_ids) or len(global_ids) != len(set(global_ids)):
        raise ValueError("selected global layer DAG contains missing or duplicate layer ids")

    order = strict_topological_sparse_layer_ids(global_rows)

    try:
        selected_path = selected.artifact_paths["layers.json"]
    except KeyError as exc:
        raise ValueError("selected authority omits layers.json") from exc
    selected = load_layers_from_path(selected_path)
    if set(selected) != set(global_ids):
        raise ValueError(
            "selected executable layer view does not contain the exact global layer set"
        )
    return tuple(selected[layer_id] for layer_id in order)
