"""Builder adapter for terminal-receipt-bound layer-outcome publication."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

from vfx_harness.agents.builder.layer_finalization_guard import (
    LayerFinalizationReceiptGuard,
)
from vfx_harness.orchestration.authority_selection import ResolvedSelectedAuthority
from vfx_harness.orchestration.layer_finalization_state import (
    authorize_terminal_layer_finalization_mutation,
)
from vfx_harness.orchestration.layer_outcome_publication import (
    LayerOutcomePublicationConflict,
    verify_prepared_layer_outcome,
)
from vfx_harness.orchestration.layer_plans import (
    commit_layer_outcome,
    discard_layer_outcome,
    finalization_layer_outcome_authority,
    prepare_layer_outcome,
)


def _refuse_conflicting_current_projection(
    path: Path,
    *,
    receipt_digest: str,
    expected_sha256: str,
) -> None:
    """Refuse to overwrite a different projection of this exact terminal receipt."""

    if not path.is_file():
        return
    payload = path.read_bytes()
    try:
        current = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"existing layer outcome is not valid JSON: {path}") from exc
    embedded = (
        current.get("finalization_receipt")
        if isinstance(current, Mapping)
        else None
    )
    if (
        isinstance(embedded, Mapping)
        and embedded.get("receipt_digest") == receipt_digest
        and hashlib.sha256(payload).hexdigest() != expected_sha256
    ):
        raise ValueError(
            "existing layer outcome conflicts with the exact current terminal receipt"
        )


def publish_finalized_layer_outcome(
    shot_folder: str | Path,
    layer,
    *,
    best: dict,
    canonical: list,
    blender_version: str,
    selected_authority: ResolvedSelectedAuthority,
    finalization_guard: LayerFinalizationReceiptGuard,
) -> Path:
    """Project one outcome from the exact current terminal receipt.

    Expensive record assembly, source hashing, and fsync happen outside the
    authority lock. Only the bounded CAS, rename, stable rehash, and parent fsync
    run through the receipt guard, so a superseded finalization can never publish
    its prepared bytes.
    """

    if not isinstance(finalization_guard, LayerFinalizationReceiptGuard):
        raise TypeError(
            "layer outcome publication requires a LayerFinalizationReceiptGuard"
    )
    receipt = finalization_guard.receipt
    finalization_authorization = authorize_terminal_layer_finalization_mutation(
        shot_folder,
        receipt,
        finalization_guard.units,
        selected_authority,
    )
    authority = finalization_layer_outcome_authority(
        layer,
        selected_authority,
        finalization_receipt=receipt,
        finalization_authorization=finalization_authorization,
    )
    finalization_guard.check(f"start layer {layer.id} outcome preparation")
    prepared = prepare_layer_outcome(
        shot_folder,
        layer,
        best=best,
        canonical=canonical,
        finalization_receipt=receipt,
        blender_version=blender_version,
        selected_authority=selected_authority,
        authority=authority,
        guard=finalization_guard,
    )
    try:
        _refuse_conflicting_current_projection(
            prepared.destination,
            receipt_digest=receipt.receipt_digest,
            expected_sha256=prepared.payload_sha256,
        )
        verification = verify_prepared_layer_outcome(
            shot_folder,
            prepared,
            authority,
        )
        publication_calls = 0
        publication_completed = False
        committed: Path | None = None

        def publish(authorization: object) -> Path:
            nonlocal publication_calls, publication_completed, committed
            publication_calls += 1
            if publication_calls != 1:
                raise LayerOutcomePublicationConflict(
                    "layer outcome finalization guard invoked its prepared mutation more than once"
                )
            committed = commit_layer_outcome(
                shot_folder,
                prepared,
                authority=authority,
                authorization=authorization,
                verification=verification,
            )
            publication_completed = True
            return committed

        finalization_guard.publish_prepared(
            f"publish layer {layer.id} outcome",
            prepared,
            publish,
        )
        if publication_calls != 1 or not publication_completed or committed is None:
            raise LayerOutcomePublicationConflict(
                "layer outcome finalization guard returned without completing its exact prepared mutation"
            )
        return committed
    except BaseException as exc:
        try:
            discard_layer_outcome(prepared)
        except LayerOutcomePublicationConflict as cleanup_error:
            exc.add_note(f"layer-outcome cleanup diagnostic: {cleanup_error}")
        raise


__all__ = ["publish_finalized_layer_outcome"]
