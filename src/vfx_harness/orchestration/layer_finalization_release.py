"""Reviewed release transaction for an orphaned pre-terminal layer claim."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vfx_harness.domain.authority_head_records import (
    parse_authority_selection_token,
)
from vfx_harness.domain.layer_finalization_releases import (
    LayerFinalizationReleaseEvidence,
    LayerFinalizationReleaseReceipt,
    LayerFinalizationReleaseReference,
    LayerFinalizationReleaseRequest,
    canonical_layer_finalization_release_receipt_bytes,
    layer_finalization_release_claim_evidence,
    layer_finalization_review_evidence_locator,
)
from vfx_harness.domain.layer_finalizations import (
    LAYER_REPLAY_RECEIPT_SCHEMA,
    LayerFinalizationClaim,
    LayerReplayReceipt,
    canonical_layer_replay_receipt_bytes,
)
from vfx_harness.domain.work_units import dependency_ordered_units
from vfx_harness.infrastructure.trusted_files import (
    TrustedFileBinding,
    TrustedFileError,
    read_trusted_file,
    require_trusted_file_unchanged,
)
from vfx_harness.observability.prepared_publication import (
    FilePublicationConflict,
    PreparedFilePublication,
    commit_prepared_file,
    discard_prepared_file,
    prepare_file_update,
)
from vfx_harness.orchestration import (
    layer_finalization_state,
    unit_state,
)
from vfx_harness.orchestration.authority_selection import ResolvedSelectedAuthority
from vfx_harness.orchestration.builder_execution_fence import (
    BuilderExecutionFenceError,
    BuilderExecutionFenceLease,
    require_builder_execution_lease,
)
from vfx_harness.orchestration.layer_finalization_release_sources import (
    LayerFinalizationReleaseSourceConflict,
    StoredLayerFinalizationRelease,
    decode_layer_finalization_release_receipt,
    load_layer_finalization_release_receipt,
    require_released_claim_archive_current,
)
from vfx_harness.orchestration.layer_replay_receipts import (
    layer_replay_receipt_locator,
)
from vfx_harness.orchestration.unit_state_storage import now

_RELEASE_DIRECTORY = Path("state/layer-finalization-releases")


class LayerFinalizationReleaseConflict(ValueError):
    """The requested reviewed release is stale, ambiguous, or source-incomplete."""


@dataclass(frozen=True, slots=True)
class _CapturedEvidence:
    reference: LayerFinalizationReleaseEvidence
    source_binding: TrustedFileBinding
    payload: bytes


@dataclass(frozen=True, slots=True)
class _CapturedReplayPrefix:
    receipts: tuple[_CapturedEvidence, ...]
    group_indices: tuple[int, ...]
    planned_group_count: int | None


@dataclass(frozen=True, slots=True)
class _PreparedRelease:
    receipt: LayerFinalizationReleaseReceipt
    locator: str
    sha256: str
    publication: PreparedFilePublication | None
    existing_binding: TrustedFileBinding | None


def _release_locator(request: LayerFinalizationReleaseRequest) -> str:
    return (_RELEASE_DIRECTORY / f"{request.release_id}.json").as_posix()


def _decode_release(
    payload: bytes,
    where: str,
) -> LayerFinalizationReleaseReceipt:
    try:
        return decode_layer_finalization_release_receipt(
            payload,
            where,
        )
    except LayerFinalizationReleaseSourceConflict as exc:
        raise LayerFinalizationReleaseConflict(str(exc)) from exc


def _load_release(
    shot: Path,
    locator: str,
    *,
    expected_sha256: str,
    expected_digest: str,
) -> StoredLayerFinalizationRelease:
    try:
        return load_layer_finalization_release_receipt(
            shot,
            LayerFinalizationReleaseReference(
                locator=locator,
                sha256=expected_sha256,
                receipt_digest=expected_digest,
            ),
        )
    except (LayerFinalizationReleaseSourceConflict, ValueError) as exc:
        raise LayerFinalizationReleaseConflict(str(exc)) from exc


def _capture_review_evidence(
    shot: Path,
    locators: Sequence[str | Path],
) -> tuple[_CapturedEvidence, ...]:
    if isinstance(locators, (str, bytes)) or not isinstance(locators, Sequence):
        raise LayerFinalizationReleaseConflict(
            "reviewed layer finalization release evidence must be a sequence"
        )
    if not locators:
        raise LayerFinalizationReleaseConflict(
            "reviewed layer finalization release requires evidence"
        )
    captured: list[_CapturedEvidence] = []
    for index, locator in enumerate(locators):
        try:
            snapshot = read_trusted_file(
                shot,
                locator,
                f"layer finalization release review evidence {index}",
                require_nonempty=True,
            )
        except TrustedFileError as exc:
            raise LayerFinalizationReleaseConflict(str(exc)) from exc
        captured.append(
            _CapturedEvidence(
                reference=LayerFinalizationReleaseEvidence(
                    kind="review_evidence",
                    locator=layer_finalization_review_evidence_locator(
                        snapshot.sha256
                    ),
                    sha256=snapshot.sha256,
                    source_locator=snapshot.binding.relative,
                ),
                source_binding=snapshot.binding,
                payload=snapshot.payload,
            )
        )
    captured.sort(key=lambda row: row.reference.source_locator or "")
    locators_seen = [row.reference.source_locator for row in captured]
    if len(locators_seen) != len(set(locators_seen)):
        raise LayerFinalizationReleaseConflict(
            "layer finalization release review evidence repeats a locator"
        )
    return tuple(captured)


def _publish_review_evidence_snapshots(
    shot: Path,
    captured: tuple[_CapturedEvidence, ...],
) -> tuple[_CapturedEvidence, ...]:
    """Publish the reviewed bytes before their mutable source can disappear."""

    unique = {row.reference.locator: row for row in captured}
    prepared: list[PreparedFilePublication | None] = []
    try:
        for locator, row in sorted(unique.items()):
            payload = row.payload

            def create_only(
                current: bytes | None,
                *,
                expected: bytes = payload,
            ) -> tuple[bytes | None, None]:
                if current is None:
                    return expected, None
                if current != expected:
                    raise FilePublicationConflict(
                        "content-addressed finalization review evidence conflicts "
                        "with its digest locator"
                    )
                return None, None

            update = prepare_file_update(
                shot,
                locator,
                create_only,
                authority_binding=(
                    f"layer-finalization-review-evidence:{row.reference.sha256}"
                ),
            )
            prepared.append(update.publication)
        for row in captured:
            require_trusted_file_unchanged(
                row.source_binding,
                "layer finalization release review evidence source",
            )
        for publication in prepared:
            if publication is not None:
                commit_prepared_file(
                    publication,
                    authority_binding=publication.authority_binding,
                )
        bindings: dict[str, TrustedFileBinding] = {}
        for locator, row in unique.items():
            snapshot = read_trusted_file(
                shot,
                shot / locator,
                "immutable layer finalization review evidence snapshot",
                require_nonempty=True,
            )
            if snapshot.sha256 != row.reference.sha256 or snapshot.payload != row.payload:
                raise LayerFinalizationReleaseConflict(
                    "immutable layer finalization review evidence snapshot changed"
                )
            bindings[locator] = snapshot.binding
        return tuple(
            _CapturedEvidence(
                reference=row.reference,
                source_binding=bindings[row.reference.locator],
                payload=row.payload,
            )
            for row in captured
        )
    except (FilePublicationConflict, TrustedFileError) as exc:
        raise LayerFinalizationReleaseConflict(str(exc)) from exc
    finally:
        for publication in prepared:
            discard_prepared_file(publication)


def _discover_replay_group_indices(
    shot: Path,
    claim: LayerFinalizationClaim,
) -> tuple[int, ...]:
    group_zero = Path(layer_replay_receipt_locator(claim, 0))
    directory = (shot / group_zero.parent).resolve()
    if not directory.is_relative_to(shot.resolve()):
        raise LayerFinalizationReleaseConflict(
            "layer replay receipt directory escapes the shot root"
        )
    if not directory.exists():
        return ()
    if not directory.is_dir():
        raise LayerFinalizationReleaseConflict(
            "layer replay receipt parent is not a directory"
        )
    prefix = f"{claim.claim_id}.group-"
    suffix = ".replay.json"
    indices: list[int] = []
    try:
        entries = tuple(directory.iterdir())
    except OSError as exc:
        raise LayerFinalizationReleaseConflict(str(exc)) from exc
    for entry in entries:
        name = entry.name
        if not name.startswith(prefix) or not name.endswith(suffix):
            continue
        raw_index = name[len(prefix) : -len(suffix)]
        if (
            not raw_index.isascii()
            or not raw_index.isdigit()
            or str(int(raw_index)) != raw_index
        ):
            raise LayerFinalizationReleaseConflict(
                "orphaned layer replay receipt has a non-canonical group locator"
            )
        indices.append(int(raw_index))
    ordered = tuple(sorted(indices))
    if ordered != tuple(range(len(ordered))):
        raise LayerFinalizationReleaseConflict(
            "orphaned layer replay receipts must be a contiguous prefix starting "
            "at group zero"
        )
    return ordered


def _capture_replay_receipt(
    shot: Path,
    claim: LayerFinalizationClaim,
    group_index: int,
) -> tuple[_CapturedEvidence, LayerReplayReceipt]:
    locator = layer_replay_receipt_locator(claim, group_index)
    try:
        snapshot = read_trusted_file(
            shot,
            shot / locator,
            f"orphaned layer replay receipt group {group_index}",
            require_nonempty=True,
        )
    except TrustedFileError as exc:
        raise LayerFinalizationReleaseConflict(str(exc)) from exc
    try:
        receipt = LayerReplayReceipt.parse(
            json.loads(snapshot.payload),
            f"orphaned layer replay receipt group {group_index}",
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise LayerFinalizationReleaseConflict(str(exc)) from exc
    if canonical_layer_replay_receipt_bytes(receipt) != snapshot.payload:
        raise LayerFinalizationReleaseConflict(
            "orphaned layer replay receipt is not canonically encoded"
        )
    if receipt.claim != claim:
        raise LayerFinalizationReleaseConflict(
            "orphaned layer replay receipt belongs to another finalization claim"
        )
    if receipt.observation.plan.group_index != group_index:
        raise LayerFinalizationReleaseConflict(
            "orphaned layer replay receipt group identity does not match its locator"
        )
    return (
        _CapturedEvidence(
            reference=LayerFinalizationReleaseEvidence(
                kind="layer_replay_receipt",
                locator=locator,
                sha256=snapshot.sha256,
                record_schema=LAYER_REPLAY_RECEIPT_SCHEMA,
                record_digest=receipt.receipt_digest,
                group_index=group_index,
                planned_group_count=receipt.observation.plan.planned_group_count,
            ),
            source_binding=snapshot.binding,
            payload=snapshot.payload,
        ),
        receipt,
    )


def _capture_replay_prefix(
    shot: Path,
    claim: LayerFinalizationClaim,
) -> _CapturedReplayPrefix:
    indices = _discover_replay_group_indices(shot, claim)
    if not indices:
        return _CapturedReplayPrefix((), (), None)
    first, first_receipt = _capture_replay_receipt(shot, claim, 0)
    planned_group_count = first_receipt.observation.plan.planned_group_count
    if len(indices) > planned_group_count:
        raise LayerFinalizationReleaseConflict(
            "orphaned layer replay receipt prefix contains an out-of-range group"
        )
    captured = [first]
    for group_index in indices[1:]:
        row, receipt = _capture_replay_receipt(shot, claim, group_index)
        observed_count = receipt.observation.plan.planned_group_count
        if observed_count != planned_group_count:
            raise LayerFinalizationReleaseConflict(
                "orphaned layer replay receipts disagree on planned group count"
            )
        captured.append(row)
    return _CapturedReplayPrefix(
        receipts=tuple(captured),
        group_indices=indices,
        planned_group_count=planned_group_count,
    )


def _require_captured_sources_unchanged(
    shot: Path,
    claim: LayerFinalizationClaim,
    evidence: tuple[_CapturedEvidence, ...],
    replay_prefix: _CapturedReplayPrefix,
) -> None:
    try:
        for row in evidence:
            require_trusted_file_unchanged(
                row.source_binding,
                "layer finalization release review evidence",
            )
        current_indices = _discover_replay_group_indices(shot, claim)
        if current_indices != replay_prefix.group_indices:
            raise LayerFinalizationReleaseConflict(
                "orphaned layer replay receipt prefix changed during reviewed release"
            )
        for replay in replay_prefix.receipts:
            require_trusted_file_unchanged(
                replay.source_binding,
                "orphaned layer replay receipt",
            )
    except TrustedFileError as exc:
        raise LayerFinalizationReleaseConflict(str(exc)) from exc


def _prepare_release(
    shot: Path,
    request: LayerFinalizationReleaseRequest,
) -> _PreparedRelease:
    candidate = LayerFinalizationReleaseReceipt.mint(
        request=request,
        released_at=now(),
    )
    raw = canonical_layer_finalization_release_receipt_bytes(candidate)
    locator = _release_locator(request)

    def create_only(
        current: bytes | None,
    ) -> tuple[bytes | None, LayerFinalizationReleaseReceipt]:
        if current is None:
            return raw, candidate
        existing = _decode_release(
            current,
            "existing immutable layer finalization release receipt",
        )
        if existing.request != request:
            raise FilePublicationConflict(
                "immutable layer finalization release receipt conflicts with request"
            )
        return None, existing

    try:
        prepared = prepare_file_update(
            shot,
            locator,
            create_only,
            authority_binding=f"layer-finalization-release:{request.release_id}",
        )
    except (FilePublicationConflict, LayerFinalizationReleaseConflict) as exc:
        raise LayerFinalizationReleaseConflict(str(exc)) from exc
    receipt = prepared.result
    receipt_raw = canonical_layer_finalization_release_receipt_bytes(receipt)
    existing_binding = None
    if prepared.publication is None:
        try:
            snapshot = read_trusted_file(
                shot,
                shot / locator,
                "existing immutable layer finalization release receipt",
                require_nonempty=True,
            )
        except TrustedFileError as exc:
            raise LayerFinalizationReleaseConflict(str(exc)) from exc
        if snapshot.payload != receipt_raw:
            raise LayerFinalizationReleaseConflict(
                "existing immutable layer finalization release receipt changed"
            )
        existing_binding = snapshot.binding
    return _PreparedRelease(
        receipt=receipt,
        locator=locator,
        sha256=hashlib.sha256(receipt_raw).hexdigest(),
        publication=prepared.publication,
        existing_binding=existing_binding,
    )


def _released_archive_for_claim(
    slot: Mapping[str, Any],
    claim_id: str,
) -> Mapping[str, Any] | None:
    history = slot.get("claim_history")
    if not isinstance(history, list):
        raise LayerFinalizationReleaseConflict(
            "layer finalization claim history must be a list"
        )
    matches: list[Mapping[str, Any]] = []
    for row in history:
        if not isinstance(row, Mapping) or row.get("disposition") != "released":
            continue
        try:
            claim = LayerFinalizationClaim.parse(
                row.get("claim"),
                "released layer finalization claim",
            )
        except ValueError as exc:
            raise LayerFinalizationReleaseConflict(str(exc)) from exc
        if claim.claim_id == claim_id:
            matches.append(row)
    if len(matches) > 1:
        raise LayerFinalizationReleaseConflict(
            "layer finalization claim has ambiguous reviewed release history"
        )
    return None if not matches else matches[0]


def _claim_from_state(
    state: Mapping[str, Any],
    claim_id: str,
) -> tuple[LayerFinalizationClaim, Mapping[str, Any] | None]:
    slot = state.get("layer_finalization")
    if not isinstance(slot, Mapping):
        raise LayerFinalizationReleaseConflict(
            "layer has no initialized finalization state"
        )
    active_raw = slot.get("active_claim")
    if active_raw is not None:
        try:
            active = LayerFinalizationClaim.parse(
                active_raw,
                "active layer finalization claim",
            )
        except ValueError as exc:
            raise LayerFinalizationReleaseConflict(str(exc)) from exc
        if active.claim_id != claim_id:
            raise LayerFinalizationReleaseConflict(
                "reviewed release targets another active finalization claim: "
                f"expected={claim_id}, active={active.claim_id}"
            )
        return active, None
    if slot.get("terminal_receipt") is not None:
        raise LayerFinalizationReleaseConflict(
            "reviewed release refuses a terminal finalization; reconcile its exact "
            "receipt projections instead"
        )
    archive = _released_archive_for_claim(slot, claim_id)
    if archive is None:
        raise LayerFinalizationReleaseConflict(
            f"layer has no exact active or released finalization claim {claim_id!r}"
        )
    try:
        return (
            LayerFinalizationClaim.parse(
                archive.get("claim"),
                "released layer finalization claim",
            ),
            archive,
        )
    except ValueError as exc:
        raise LayerFinalizationReleaseConflict(str(exc)) from exc


def _require_claim_matches_layer(
    claim: LayerFinalizationClaim,
    layer: Any,
    selected_authority: ResolvedSelectedAuthority,
) -> None:
    if (
        claim.layer_id != str(layer.id)
        or claim.layer_script_path != str(layer.script)
    ):
        raise LayerFinalizationReleaseConflict(
            "reviewed release claim does not match the exact selected layer"
        )
    try:
        selected_projection = parse_authority_selection_token(
            selected_authority.selection_token.to_dict(),
            "reviewed release selected authority",
        )
    except (AttributeError, ValueError) as exc:
        raise LayerFinalizationReleaseConflict(str(exc)) from exc
    if claim.selection_token != selected_projection:
        raise LayerFinalizationReleaseConflict(
            "reviewed release claim belongs to another authority selection"
        )


def _requested_evidence_locators(
    shot: Path,
    locators: Sequence[str | Path],
) -> tuple[str, ...]:
    if isinstance(locators, (str, bytes)) or not isinstance(locators, Sequence):
        raise LayerFinalizationReleaseConflict(
            "reviewed layer finalization release evidence must be a sequence"
        )
    if not locators:
        raise LayerFinalizationReleaseConflict(
            "reviewed layer finalization release requires evidence"
        )
    normalized: list[str] = []
    for locator in locators:
        path = Path(locator).expanduser()
        absolute = path.absolute() if path.is_absolute() else (shot / path).absolute()
        try:
            normalized.append(absolute.relative_to(shot).as_posix())
        except ValueError as exc:
            raise LayerFinalizationReleaseConflict(
                f"layer finalization release evidence is outside the shot: {locator}"
            ) from exc
    if len(normalized) != len(set(normalized)):
        raise LayerFinalizationReleaseConflict(
            "layer finalization release review evidence repeats a locator"
        )
    return tuple(sorted(normalized))


def _reconcile_released_claim(
    shot: Path,
    claim: LayerFinalizationClaim,
    archive: Mapping[str, Any],
    *,
    reason: str,
    evidence: Sequence[str | Path],
) -> StoredLayerFinalizationRelease:
    """Return the committed release without rereading mutable review evidence."""

    try:
        stored = require_released_claim_archive_current(shot, archive)
    except LayerFinalizationReleaseSourceConflict as exc:
        raise LayerFinalizationReleaseConflict(str(exc)) from exc
    reference = stored.reference
    request = stored.receipt.request
    requested_locators = _requested_evidence_locators(shot, evidence)
    committed_locators = tuple(
        row.source_locator for row in request.review_evidence
    )
    expected_archive = layer_finalization_release_claim_evidence(
        stored.receipt,
        reference,
    )
    if (
        request.claim != claim
        or reason != request.reason
        or requested_locators != committed_locators
        or archive.get("reason") != request.reason
        or archive.get("at") != stored.receipt.released_at
        or tuple(archive.get("evidence") or ()) != expected_archive
    ):
        raise LayerFinalizationReleaseConflict(
            "reviewed finalization release repeat conflicts with the archived transaction"
        )
    return stored


def release_active_layer_finalization(
    shot_folder: str | Path,
    layer: Any,
    *,
    claim_id: str,
    selected_authority: ResolvedSelectedAuthority,
    reason: str,
    evidence: Sequence[str | Path],
    fence_lease: BuilderExecutionFenceLease,
) -> StoredLayerFinalizationRelease:
    """Release one exact orphaned claim without touching accepted unit authority."""

    shot = Path(shot_folder).expanduser().absolute()
    if not isinstance(claim_id, str) or not claim_id.strip():
        raise LayerFinalizationReleaseConflict(
            "reviewed layer finalization release requires an exact claim id"
        )
    try:
        require_builder_execution_lease(fence_lease, shot)
    except BuilderExecutionFenceError as exc:
        raise LayerFinalizationReleaseConflict(str(exc)) from exc
    with fence_lease.operation(shot):
        initial = unit_state.load(shot, str(layer.id))
        claim, prior_archive = _claim_from_state(initial, claim_id)
        _require_claim_matches_layer(claim, layer, selected_authority)
        if prior_archive is not None:
            return _reconcile_released_claim(
                shot,
                claim,
                prior_archive,
                reason=reason,
                evidence=evidence,
            )
        captured_evidence = _publish_review_evidence_snapshots(
            shot,
            _capture_review_evidence(shot, evidence),
        )
        captured_replays = _capture_replay_prefix(shot, claim)
        request = LayerFinalizationReleaseRequest.mint(
            claim=claim,
            reason=reason,
            review_evidence=tuple(row.reference for row in captured_evidence),
            replay_receipts=tuple(
                row.reference for row in captured_replays.receipts
            ),
        )
        prepared = _prepare_release(shot, request)
        ordered_units = dependency_ordered_units(layer.stages)
        try:
            _require_captured_sources_unchanged(
                shot,
                claim,
                captured_evidence,
                captured_replays,
            )
            authority_binding = f"layer-finalization-release:{request.release_id}"
            if prepared.publication is not None:
                commit_prepared_file(
                    prepared.publication,
                    authority_binding=authority_binding,
                )
            elif prepared.existing_binding is not None:
                require_trusted_file_unchanged(
                    prepared.existing_binding,
                    "existing immutable layer finalization release receipt",
                )
            stored = _load_release(
                shot,
                prepared.locator,
                expected_sha256=prepared.sha256,
                expected_digest=prepared.receipt.receipt_digest,
            )
            _require_captured_sources_unchanged(
                shot,
                claim,
                captured_evidence,
                captured_replays,
            )
            require_trusted_file_unchanged(
                stored.source_binding,
                "immutable layer finalization release receipt",
            )
            reference = LayerFinalizationReleaseReference(
                locator=stored.locator,
                sha256=stored.sha256,
                receipt_digest=stored.receipt.receipt_digest,
            )
            layer_finalization_state.release_layer_finalization_claim(
                shot,
                stored.receipt,
                reference,
                ordered_units,
                selected_authority=selected_authority,
            )
        except (
            FilePublicationConflict,
            TrustedFileError,
            ValueError,
        ) as exc:
            if isinstance(exc, LayerFinalizationReleaseConflict):
                raise
            raise LayerFinalizationReleaseConflict(str(exc)) from exc
        finally:
            discard_prepared_file(prepared.publication)
        return stored


__all__ = [
    "LayerFinalizationReleaseConflict",
    "StoredLayerFinalizationRelease",
    "release_active_layer_finalization",
]
