"""Receipt-backed layer-prefix reads shared by deterministic evaluations.

An absent terminal receipt is the normal boundary of a partially built shot.  Once a
terminal receipt exists, however, it is not enough on its own: the shared publication
verifier must close the receipt, outcome, ledger, script, replay inputs, predecessors,
and selected authority before an evaluation may include that layer.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from vfx_harness.orchestration.layer_finalization_state import (
    LayerFinalizationConflict,
    current_layer_finalization_receipt,
)
from vfx_harness.orchestration.layer_publication import (
    LayerPublicationConflict,
    require_current_layer_publication,
)

if TYPE_CHECKING:
    from vfx_harness.orchestration.authority_selection import (
        ResolvedSelectedAuthority,
    )
    from vfx_harness.orchestration.ledger import Layer


class EvaluationLayerPublicationConflict(LayerPublicationConflict):
    """One exact layer in an evaluation prefix has corrupt/stale publication."""

    def __init__(
        self,
        layer_id: str,
        cause: BaseException,
        *,
        published_count: int,
    ) -> None:
        super().__init__(f"layer {layer_id} publication is invalid: {cause}")
        self.layer_id = layer_id
        self.published_count = published_count


@dataclass(frozen=True, slots=True)
class EvaluationLayerPublicationIdentity:
    """Exact public identities retained across one long evaluation operation."""

    layer_id: str
    finalization_receipt_digest: str
    outcome_sha256: str
    script_sha256: str


def receipt_backed_layer_prefix(
    folder: str | Path,
    layers: Sequence[Layer],
    selected_authority: ResolvedSelectedAuthority,
) -> tuple[Layer, ...]:
    """Return the leading layers with complete current receipt-backed publications.

    Missing terminal state means the layer has not published yet and ends the prefix.
    Malformed terminal state or any disagreement among its projections is an authority
    defect and fails closed instead of being reinterpreted as ordinary pending work.
    """

    root = Path(folder).expanduser().absolute()
    published: list[Layer] = []
    for layer in layers:
        try:
            receipt = current_layer_finalization_receipt(root, str(layer.id))
        except (LayerFinalizationConflict, OSError, TypeError, ValueError) as exc:
            raise EvaluationLayerPublicationConflict(
                str(layer.id),
                exc,
                published_count=len(published),
            ) from exc
        if receipt is None:
            break
        try:
            require_current_layer_publication(root, layer, selected_authority)
        except LayerPublicationConflict as exc:
            raise EvaluationLayerPublicationConflict(
                str(layer.id),
                exc,
                published_count=len(published),
            ) from exc
        published.append(layer)
    return tuple(published)


def capture_exact_layer_publications(
    folder: str | Path,
    layers: Sequence[Layer],
    selected_authority: ResolvedSelectedAuthority,
) -> tuple[EvaluationLayerPublicationIdentity, ...]:
    """Capture verified publication identities immediately before long work."""

    captured: list[EvaluationLayerPublicationIdentity] = []
    for layer in layers:
        try:
            publication = require_current_layer_publication(
                folder,
                layer,
                selected_authority,
            )
        except LayerPublicationConflict as exc:
            raise EvaluationLayerPublicationConflict(
                str(layer.id),
                exc,
                published_count=len(captured),
            ) from exc
        captured.append(
            EvaluationLayerPublicationIdentity(
                layer_id=str(layer.id),
                finalization_receipt_digest=publication.receipt.receipt_digest,
                outcome_sha256=publication.outcome_sha256,
                script_sha256=publication.ledger_script_sha256,
            )
        )
    return tuple(captured)


def require_exact_layer_publications(
    folder: str | Path,
    layers: Sequence[Layer],
    selected_authority: ResolvedSelectedAuthority,
    *,
    expected: Sequence[EvaluationLayerPublicationIdentity],
) -> None:
    """Require the same exact publications after long-running evaluation work."""

    expected_identities = tuple(expected)
    observed = capture_exact_layer_publications(
        folder,
        layers,
        selected_authority,
    )
    if observed != expected_identities:
        differing = next(
            (
                index
                for index, pair in enumerate(
                    zip(expected_identities, observed, strict=False)
                )
                if pair[0] != pair[1]
            ),
            min(len(expected_identities), len(observed)),
        )
        layer_id = (
            expected_identities[differing].layer_id
            if differing < len(expected_identities)
            else (
                observed[differing].layer_id
                if differing < len(observed)
                else "unknown"
            )
        )
        raise EvaluationLayerPublicationConflict(
            layer_id,
            ValueError(
                "exact publication identity changed during evaluation; "
                f"expected={expected_identities!r}, observed={observed!r}"
            ),
            published_count=differing,
        )


__all__ = [
    "EvaluationLayerPublicationConflict",
    "EvaluationLayerPublicationIdentity",
    "capture_exact_layer_publications",
    "receipt_backed_layer_prefix",
    "require_exact_layer_publications",
]
