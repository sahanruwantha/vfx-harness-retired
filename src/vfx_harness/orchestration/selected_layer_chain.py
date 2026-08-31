"""One dependency-ordered layer chain shared by replay and publication boundaries."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING

from vfx_harness.domain.brief import Shot
from vfx_harness.domain.work_units import (
    strict_topological_sparse_layer_ids,
)
from vfx_harness.orchestration.ledger import Layer, load_layers_from_path
from vfx_harness.orchestration.plan_authority import resolve_current

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
    expected_bundle_digest: str | None = None,
) -> tuple[Layer, ...]:
    """Return current executable layers in the selected global DAG's stable order."""

    selected_bundle = bundle or resolve_current(shot.folder)
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

    # Resolve the materialized member against the already-selected bundle.  Calling
    # selected_artifact_path here would resolve plans/current.json a second time and
    # could mix bundle A's DAG with bundle B's scripts.
    from vfx_harness.orchestration.jit_materialization import (  # noqa: PLC0415
        selected_view_artifact,
    )

    selected_path = selected_view_artifact(
        shot.folder,
        "layers.json",
        selected_bundle.content_hash,
    )
    if selected_path is None:
        selected_path = selected_bundle.root / "layers.json"
    selected = load_layers_from_path(selected_path)
    if set(selected) != set(global_ids):
        raise ValueError(
            "selected executable layer view does not contain the exact global layer set"
        )
    return tuple(selected[layer_id] for layer_id in order)
