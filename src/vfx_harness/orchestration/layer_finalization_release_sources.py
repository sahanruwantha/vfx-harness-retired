"""Immutable source closure for reviewed layer-finalization release history."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vfx_harness.domain.layer_finalization_releases import (
    LAYER_FINALIZATION_RELEASE_REFERENCE_PREFIX,
    LayerFinalizationReleaseReceipt,
    LayerFinalizationReleaseReference,
    canonical_layer_finalization_release_receipt_bytes,
    layer_finalization_release_claim_evidence,
)
from vfx_harness.domain.layer_finalizations import (
    LayerFinalizationClaim,
    LayerReplayReceipt,
    canonical_layer_replay_receipt_bytes,
)
from vfx_harness.infrastructure.trusted_files import (
    TrustedFileBinding,
    TrustedFileError,
    read_trusted_file,
)
from vfx_harness.orchestration.layer_replay_receipts import (
    layer_replay_receipt_locator,
)


class LayerFinalizationReleaseSourceConflict(ValueError):
    """A release archive no longer closes against its immutable receipt."""


@dataclass(frozen=True, slots=True)
class StoredLayerFinalizationRelease:
    """One content-addressed immutable release receipt."""

    receipt: LayerFinalizationReleaseReceipt
    reference: LayerFinalizationReleaseReference
    source_binding: TrustedFileBinding

    @property
    def locator(self) -> str:
        return self.reference.locator

    @property
    def sha256(self) -> str:
        return self.reference.sha256


def decode_layer_finalization_release_receipt(
    payload: bytes,
    where: str,
) -> LayerFinalizationReleaseReceipt:
    """Parse only the canonical immutable receipt encoding."""

    try:
        receipt = LayerFinalizationReleaseReceipt.from_dict(
            json.loads(payload),
            where,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise LayerFinalizationReleaseSourceConflict(str(exc)) from exc
    if canonical_layer_finalization_release_receipt_bytes(receipt) != payload:
        raise LayerFinalizationReleaseSourceConflict(
            f"{where} does not use the canonical immutable encoding"
        )
    return receipt


def release_reference_from_claim_archive(
    archive: Mapping[str, Any],
) -> LayerFinalizationReleaseReference:
    """Resolve the one exact release reference retained by a claim archive."""

    evidence = archive.get("evidence")
    if not isinstance(evidence, list):
        raise LayerFinalizationReleaseSourceConflict(
            "released finalization claim archive has no evidence list"
        )
    refs = [
        value
        for value in evidence
        if isinstance(value, str)
        and value.startswith(f"{LAYER_FINALIZATION_RELEASE_REFERENCE_PREFIX}:")
    ]
    if len(refs) != 1:
        raise LayerFinalizationReleaseSourceConflict(
            "released finalization claim must cite exactly one release receipt"
        )
    try:
        return LayerFinalizationReleaseReference.from_archive_evidence(refs[0])
    except ValueError as exc:
        raise LayerFinalizationReleaseSourceConflict(str(exc)) from exc


def load_layer_finalization_release_receipt(
    shot_folder: str | Path,
    reference: LayerFinalizationReleaseReference,
) -> StoredLayerFinalizationRelease:
    """Load and content-verify one immutable release receipt."""

    shot = Path(shot_folder).expanduser().absolute()
    try:
        snapshot = read_trusted_file(
            shot,
            shot / reference.locator,
            "immutable layer finalization release receipt",
            require_nonempty=True,
        )
    except TrustedFileError as exc:
        raise LayerFinalizationReleaseSourceConflict(str(exc)) from exc
    if snapshot.sha256 != reference.sha256:
        raise LayerFinalizationReleaseSourceConflict(
            "immutable layer finalization release receipt file digest changed"
        )
    receipt = decode_layer_finalization_release_receipt(
        snapshot.payload,
        "immutable layer finalization release receipt",
    )
    if receipt.receipt_digest != reference.receipt_digest:
        raise LayerFinalizationReleaseSourceConflict(
            "immutable layer finalization release semantic digest changed"
        )
    return StoredLayerFinalizationRelease(
        receipt=receipt,
        reference=reference,
        source_binding=snapshot.binding,
    )


def _require_replay_receipts_current(
    shot_folder: str | Path,
    stored: StoredLayerFinalizationRelease,
) -> None:
    request = stored.receipt.request
    for group_index, evidence in enumerate(request.replay_receipts):
        expected_locator = layer_replay_receipt_locator(request.claim, group_index)
        if (
            evidence.group_index != group_index
            or evidence.locator != expected_locator
        ):
            raise LayerFinalizationReleaseSourceConflict(
                "released replay receipt does not use its exact deterministic "
                f"group locator at index {group_index}"
            )
        try:
            snapshot = read_trusted_file(
                shot_folder,
                evidence.locator,
                f"released layer replay receipt group {group_index}",
                require_nonempty=True,
            )
        except TrustedFileError as exc:
            raise LayerFinalizationReleaseSourceConflict(str(exc)) from exc
        if snapshot.sha256 != evidence.sha256:
            raise LayerFinalizationReleaseSourceConflict(
                "released layer replay receipt file digest changed: "
                f"group={group_index}, locator={evidence.locator}"
            )
        try:
            replay = LayerReplayReceipt.parse(
                json.loads(snapshot.payload),
                f"released layer replay receipt group {group_index}",
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise LayerFinalizationReleaseSourceConflict(str(exc)) from exc
        if canonical_layer_replay_receipt_bytes(replay) != snapshot.payload:
            raise LayerFinalizationReleaseSourceConflict(
                "released layer replay receipt is not canonically encoded: "
                f"group={group_index}"
            )
        if (
            replay.claim != request.claim
            or replay.receipt_digest != evidence.record_digest
            or replay.observation.plan.group_index != group_index
            or replay.observation.plan.planned_group_count
            != evidence.planned_group_count
        ):
            raise LayerFinalizationReleaseSourceConflict(
                "released layer replay receipt does not match its exact archived "
                f"claim/group identity at index {group_index}"
            )


def require_released_claim_archive_current(
    shot_folder: str | Path,
    archive: Mapping[str, Any],
) -> StoredLayerFinalizationRelease:
    """Prove that one released claim archive has its exact immutable receipt."""

    if archive.get("disposition") != "released":
        raise LayerFinalizationReleaseSourceConflict(
            "release source verification requires a released claim archive"
        )
    reference = release_reference_from_claim_archive(archive)
    stored = load_layer_finalization_release_receipt(shot_folder, reference)
    try:
        claim = LayerFinalizationClaim.parse(
            archive.get("claim"),
            "released layer finalization claim",
        )
    except ValueError as exc:
        raise LayerFinalizationReleaseSourceConflict(str(exc)) from exc
    if (
        stored.receipt.request.claim != claim
        or archive.get("reason") != stored.receipt.request.reason
        or archive.get("at") != stored.receipt.released_at
        or tuple(archive.get("evidence") or ())
        != layer_finalization_release_claim_evidence(stored.receipt, reference)
    ):
        raise LayerFinalizationReleaseSourceConflict(
            "released claim archive does not match its exact immutable receipt"
        )
    for index, evidence in enumerate(stored.receipt.request.review_evidence):
        try:
            snapshot = read_trusted_file(
                shot_folder,
                evidence.locator,
                f"immutable finalization review evidence snapshot {index}",
                require_nonempty=True,
            )
        except TrustedFileError as exc:
            raise LayerFinalizationReleaseSourceConflict(str(exc)) from exc
        if snapshot.sha256 != evidence.sha256:
            raise LayerFinalizationReleaseSourceConflict(
                "immutable finalization review evidence snapshot changed: "
                f"{evidence.locator}"
            )
    _require_replay_receipts_current(shot_folder, stored)
    return stored


def require_released_claim_history_current(
    shot_folder: str | Path,
    history: Sequence[object],
) -> None:
    """Verify every release that permits a later monotone claim."""

    if isinstance(history, (str, bytes)) or not isinstance(history, Sequence):
        raise LayerFinalizationReleaseSourceConflict(
            "layer finalization claim history must be a sequence"
        )
    for row in history:
        if isinstance(row, Mapping) and row.get("disposition") == "released":
            require_released_claim_archive_current(shot_folder, row)


__all__ = [
    "LayerFinalizationReleaseSourceConflict",
    "StoredLayerFinalizationRelease",
    "decode_layer_finalization_release_receipt",
    "load_layer_finalization_release_receipt",
    "release_reference_from_claim_archive",
    "require_released_claim_archive_current",
    "require_released_claim_history_current",
]
