"""Immutable run-scoped replay receipts for claimed layer finalization."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from vfx_harness.domain.layer_finalizations import (
    LayerFinalizationClaim,
    LayerReplayReceipt,
    canonical_layer_replay_receipt_bytes,
)
from vfx_harness.infrastructure.trusted_files import (
    TrustedFileBinding,
    TrustedFileError,
    TrustedFileNotFound,
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

_RECEIPT_DIRECTORY = Path("checkpoints/layer-finalizations")


class LayerReplayReceiptConflict(ValueError):
    """An immutable replay receipt is missing, stale, or conflicts by bytes."""


class LayerFinalizationPublicationGuard(Protocol):
    """The small guard surface used by replay and projection publishers."""

    claim: LayerFinalizationClaim

    def check(self, operation: str) -> LayerFinalizationClaim: ...

    def publish(self, operation: str, mutation): ...


@dataclass(frozen=True, slots=True)
class StoredLayerReplayReceipt:
    receipt: LayerReplayReceipt
    locator: str
    sha256: str
    source_binding: TrustedFileBinding


@dataclass(frozen=True, slots=True)
class PreparedLayerReplayReceipt:
    shot: Path
    receipt: LayerReplayReceipt
    locator: str
    sha256: str
    publication: PreparedFilePublication | None
    existing_binding: TrustedFileBinding | None
    causal_source_bindings: tuple[TrustedFileBinding, ...]
    authority_binding: str


def layer_replay_receipt_locator(
    claim: LayerFinalizationClaim,
    group_index: int = 0,
) -> str:
    if not isinstance(claim, LayerFinalizationClaim):
        raise LayerReplayReceiptConflict("layer replay receipt requires a typed finalization claim")
    if (
        not isinstance(group_index, int)
        or isinstance(group_index, bool)
        or group_index < 0
    ):
        raise LayerReplayReceiptConflict(
            "layer replay receipt group_index must be a non-negative integer"
        )
    return (
        Path("runs")
        / claim.run_id
        / _RECEIPT_DIRECTORY
        / f"{claim.claim_id}.group-{group_index}.replay.json"
    ).as_posix()


def _authority_binding(receipt: LayerReplayReceipt) -> str:
    return f"layer-replay:{receipt.claim.claim_id}:{receipt.receipt_digest}"


def capture_layer_replay_causal_sources(
    shot: Path,
    receipt: LayerReplayReceipt,
) -> tuple[TrustedFileBinding, ...]:
    sources: list[TrustedFileBinding] = []
    seen: set[tuple[str, str]] = set()

    def capture(locator: str, sha256: str, where: str) -> None:
        key = (locator, sha256)
        if key in seen:
            return
        try:
            snapshot = read_trusted_file(
                shot,
                shot / locator,
                where,
                require_nonempty=True,
            )
        except TrustedFileError as exc:
            raise LayerReplayReceiptConflict(str(exc)) from exc
        observed = hashlib.sha256(snapshot.payload).hexdigest()
        if observed != sha256:
            raise LayerReplayReceiptConflict(
                f"{where} SHA-256 changed; expected={sha256}, observed={observed}"
            )
        sources.append(snapshot.binding)
        seen.add(key)

    for input_index, replay_input in enumerate(receipt.replay_inputs):
        capture(
            replay_input.script_path,
            replay_input.script_sha256,
            f"layer replay input {input_index}",
        )
        for dependency_index, dependency in enumerate(replay_input.dependencies):
            capture(
                dependency.path,
                dependency.sha256,
                f"layer replay input {input_index} dependency {dependency_index}",
            )
    for point_index, point in enumerate(receipt.observation.points):
        capture(
            point.ref,
            point.ref_sha256,
            f"layer replay point {point_index} reference",
        )
        if point.render is not None:
            assert point.render_sha256 is not None
            capture(
                point.render,
                point.render_sha256,
                f"layer replay point {point_index} render",
            )
    for capture_index, auxiliary in enumerate(
        receipt.observation.auxiliary_captures
    ):
        capture(
            auxiliary["locator"],
            auxiliary["sha256"],
            f"layer replay auxiliary capture {capture_index}",
        )
    return tuple(sources)


def prepare_layer_replay_receipt(
    folder: str | Path,
    receipt: LayerReplayReceipt,
) -> PreparedLayerReplayReceipt:
    """Fsync immutable receipt bytes outside selected/state locks."""

    if not isinstance(receipt, LayerReplayReceipt):
        raise LayerReplayReceiptConflict("receipt must be a typed layer replay receipt")
    raw = canonical_layer_replay_receipt_bytes(receipt)
    sha256 = hashlib.sha256(raw).hexdigest()
    locator = layer_replay_receipt_locator(
        receipt.claim,
        receipt.observation.group_index,
    )
    authority = _authority_binding(receipt)
    shot = Path(folder).expanduser().absolute()
    causal_sources = capture_layer_replay_causal_sources(shot, receipt)

    def create_only(current: bytes | None) -> tuple[bytes | None, None]:
        if current is None:
            return raw, None
        if current != raw:
            raise FilePublicationConflict("immutable layer replay receipt conflicts with existing bytes")
        return None, None

    try:
        prepared = prepare_file_update(
            folder,
            locator,
            create_only,
            authority_binding=authority,
        )
    except FilePublicationConflict as exc:
        raise LayerReplayReceiptConflict(str(exc)) from exc
    existing: TrustedFileBinding | None = None
    if prepared.publication is None:
        try:
            existing = read_trusted_file(
                shot,
                shot / locator,
                "immutable layer replay receipt",
                require_nonempty=True,
            ).binding
        except TrustedFileError as exc:
            raise LayerReplayReceiptConflict(str(exc)) from exc
    return PreparedLayerReplayReceipt(
        shot=shot,
        receipt=receipt,
        locator=locator,
        sha256=sha256,
        publication=prepared.publication,
        existing_binding=existing,
        causal_source_bindings=causal_sources,
        authority_binding=authority,
    )


def commit_layer_replay_receipt(
    prepared: PreparedLayerReplayReceipt,
    guard: LayerFinalizationPublicationGuard,
) -> StoredLayerReplayReceipt:
    """Publish one prepared replay receipt under its exact active claim."""

    if not isinstance(prepared, PreparedLayerReplayReceipt):
        raise LayerReplayReceiptConflict("layer replay commit requires typed prepared receipt")
    if guard.claim != prepared.receipt.claim:
        raise LayerReplayReceiptConflict("layer replay publication guard belongs to another claim")
    try:
        for source in prepared.causal_source_bindings:
            require_trusted_file_unchanged(source, "layer replay causal source")
        if prepared.publication is not None:
            def publish() -> None:
                for source in prepared.causal_source_bindings:
                    require_trusted_file_unchanged(
                        source,
                        "layer replay causal source",
                    )
                commit_prepared_file(
                    prepared.publication,
                    authority_binding=prepared.authority_binding,
                )

            guard.publish(
                "publish immutable layer replay receipt",
                publish,
            )
        else:
            guard.check("reuse immutable layer replay receipt")
            assert prepared.existing_binding is not None
            require_trusted_file_unchanged(
                prepared.existing_binding,
                "immutable layer replay receipt",
            )
        for source in prepared.causal_source_bindings:
            require_trusted_file_unchanged(source, "layer replay causal source")
    except (FilePublicationConflict, TrustedFileError) as exc:
        raise LayerReplayReceiptConflict(str(exc)) from exc
    return load_layer_replay_receipt(
        prepared.shot,
        prepared.locator,
        expected_sha256=prepared.sha256,
        expected_digest=prepared.receipt.receipt_digest,
    )


def discard_layer_replay_receipt(prepared: PreparedLayerReplayReceipt) -> None:
    if isinstance(prepared, PreparedLayerReplayReceipt):
        discard_prepared_file(prepared.publication)


def load_layer_replay_receipt(
    folder: str | Path,
    locator: str,
    *,
    expected_sha256: str | None = None,
    expected_digest: str | None = None,
) -> StoredLayerReplayReceipt:
    """Read and strictly verify one immutable replay receipt and its bytes."""

    shot = Path(folder).expanduser().absolute()
    path = shot / locator
    try:
        snapshot = read_trusted_file(
            shot,
            path,
            "immutable layer replay receipt",
            require_nonempty=True,
        )
    except (TrustedFileError, TrustedFileNotFound) as exc:
        raise LayerReplayReceiptConflict(str(exc)) from exc
    sha256 = hashlib.sha256(snapshot.payload).hexdigest()
    if expected_sha256 is not None and sha256 != expected_sha256:
        raise LayerReplayReceiptConflict("immutable layer replay receipt file digest changed")
    try:
        receipt = LayerReplayReceipt.parse(
            json.loads(snapshot.payload),
            "immutable layer replay receipt",
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise LayerReplayReceiptConflict(str(exc)) from exc
    if expected_digest is not None and receipt.receipt_digest != expected_digest:
        raise LayerReplayReceiptConflict("immutable layer replay receipt semantic digest changed")
    return StoredLayerReplayReceipt(
        receipt=receipt,
        locator=locator,
        sha256=sha256,
        source_binding=snapshot.binding,
    )


__all__ = [
    "LayerFinalizationPublicationGuard",
    "LayerReplayReceiptConflict",
    "PreparedLayerReplayReceipt",
    "StoredLayerReplayReceipt",
    "capture_layer_replay_causal_sources",
    "commit_layer_replay_receipt",
    "discard_layer_replay_receipt",
    "layer_replay_receipt_locator",
    "load_layer_replay_receipt",
    "prepare_layer_replay_receipt",
]
