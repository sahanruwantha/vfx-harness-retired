"""Receipt-verified prior-layer context for bounded model sessions."""

from __future__ import annotations

import json

from vfx_harness.domain.brief import Shot
from vfx_harness.domain.work_units import strict_topological_sparse_layer_ids
from vfx_harness.orchestration import layer_publication
from vfx_harness.orchestration.authority_selection import ResolvedSelectedAuthority
from vfx_harness.orchestration.ledger import Layer, load_layers_from_path
from vfx_harness.orchestration.selected_layer_chain import selected_layer_chain


def _selected_chain(
    shot: Shot,
    selected_authority: ResolvedSelectedAuthority,
) -> tuple[Layer, ...]:
    """Resolve stable layer order for selected bundles and explicit root authority."""

    if selected_authority.plan is not None:
        return selected_layer_chain(
            shot,
            selected_authority=selected_authority,
        )
    path = shot.folder / "layers.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"selected root layer DAG is unreadable: {path}") from exc
    rows = document.get("layers") if isinstance(document, dict) else None
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("selected root layer DAG must contain layer objects")
    order = strict_topological_sparse_layer_ids(rows)
    layers = load_layers_from_path(path)
    if set(layers) != set(order):
        raise ValueError("selected root executable layers do not match their exact DAG")
    return tuple(layers[layer_id] for layer_id in order)


def prior_outcomes_block(
    shot: Shot,
    layer_id: str,
    *,
    selected_authority: ResolvedSelectedAuthority,
) -> str:
    """Compile predecessor facts only from complete current layer publications."""

    chain = _selected_chain(shot, selected_authority)
    matches = [index for index, layer in enumerate(chain) if str(layer.id) == str(layer_id)]
    if len(matches) != 1:
        raise ValueError(
            f"layer {layer_id!r} is not uniquely present in selected layer authority"
        )
    prior_layers = chain[: matches[0]]
    if not prior_layers:
        return ""

    rows: list[dict] = []
    for layer in prior_layers:
        try:
            publication = layer_publication.require_current_layer_publication(
                shot.folder,
                layer,
                selected_authority,
            )
        except layer_publication.LayerPublicationConflict as exc:
            raise ValueError(
                f"prior layer {layer.id} has no current receipt-backed publication: {exc}"
            ) from exc
        row = json.loads(publication.outcome_bytes)
        rows.append(row)

    lines = ["## Receipt-backed prior-layer outcomes"]
    for row in rows:
        lines.append(
            f"- Layer {row['layer']} passed; script `{row.get('script', '?')}`; "
            f"terminal receipt `{row['finalization_receipt']['receipt_digest']}`; "
            f"canonical decision `{row.get('decided_by', '?')}`; authoritative checks "
            f"{row.get('authoritative_passed', 0)}/{row.get('authoritative_total', 0)}. "
            "Do not reopen it without an approved amendment."
        )
    return "\n".join(lines)


__all__ = ["prior_outcomes_block"]
