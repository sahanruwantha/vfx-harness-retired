"""Immutable run-scoped evaluation receipts for claimed layer finalization."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from vfx_harness.domain.layer_evaluation_receipts import (
    LayerEvaluationReceipt,
    canonical_layer_evaluation_receipt_bytes,
)
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
from vfx_harness.orchestration.layer_replay_receipts import (
    LayerFinalizationPublicationGuard,
    capture_layer_replay_causal_sources,
    load_layer_replay_receipt,
)

_RECEIPT_DIRECTORY = Path("checkpoints/layer-finalizations")


class LayerEvaluationReceiptConflict(ValueError):
    """An immutable evaluation receipt is missing, stale, or conflicts by bytes."""


@dataclass(frozen=True, slots=True)
class StoredLayerEvaluationReceipt:
    receipt: LayerEvaluationReceipt
    locator: str
    sha256: str
    source_binding: TrustedFileBinding


@dataclass(frozen=True, slots=True)
class PreparedLayerEvaluationReceipt:
    shot: Path
    receipt: LayerEvaluationReceipt
    locator: str
    sha256: str
    publication: PreparedFilePublication | None
    existing_binding: TrustedFileBinding | None
    replay_receipt_bindings: tuple[TrustedFileBinding, ...]
    replay_causal_bindings: tuple[TrustedFileBinding, ...]
    authority_binding: str


def layer_evaluation_receipt_locator(receipt: LayerEvaluationReceipt) -> str:
    if not isinstance(receipt, LayerEvaluationReceipt):
        raise LayerEvaluationReceiptConflict(
            "layer evaluation locator requires a typed receipt"
        )
    return (
        Path("runs")
        / receipt.claim.run_id
        / _RECEIPT_DIRECTORY
        / f"{receipt.claim.claim_id}.evaluation.json"
    ).as_posix()


def prepare_layer_evaluation_receipt(
    folder: str | Path,
    receipt: LayerEvaluationReceipt,
) -> PreparedLayerEvaluationReceipt:
    """Fsync immutable evaluation bytes outside selected/state locks."""

    if not isinstance(receipt, LayerEvaluationReceipt):
        raise LayerEvaluationReceiptConflict(
            "receipt must be a typed layer evaluation receipt"
        )
    raw = canonical_layer_evaluation_receipt_bytes(receipt)
    sha256 = hashlib.sha256(raw).hexdigest()
    locator = layer_evaluation_receipt_locator(receipt)
    authority = f"layer-evaluation:{receipt.claim.claim_id}:{receipt.receipt_digest}"
    shot = Path(folder).expanduser().absolute()
    replay_receipt_bindings: list[TrustedFileBinding] = []
    replay_causal_bindings: list[TrustedFileBinding] = []
    for index, binding in enumerate(receipt.replay_receipts):
        stored = load_layer_replay_receipt(
            shot,
            binding.locator,
            expected_sha256=binding.sha256,
            expected_digest=binding.receipt.receipt_digest,
        )
        if stored.receipt != binding.receipt:
            raise LayerEvaluationReceiptConflict(
                f"layer evaluation replay group {index} bytes disagree with binding"
            )
        replay_receipt_bindings.append(stored.source_binding)
        replay_causal_bindings.extend(
            capture_layer_replay_causal_sources(shot, stored.receipt)
        )

    def create_only(current: bytes | None) -> tuple[bytes | None, None]:
        if current is None:
            return raw, None
        if current != raw:
            raise FilePublicationConflict(
                "immutable layer evaluation receipt conflicts with existing bytes"
            )
        return None, None

    try:
        prepared = prepare_file_update(
            folder,
            locator,
            create_only,
            authority_binding=authority,
        )
    except FilePublicationConflict as exc:
        raise LayerEvaluationReceiptConflict(str(exc)) from exc
    existing = None
    if prepared.publication is None:
        try:
            existing = read_trusted_file(
                shot,
                shot / locator,
                "immutable layer evaluation receipt",
                require_nonempty=True,
            ).binding
        except TrustedFileError as exc:
            raise LayerEvaluationReceiptConflict(str(exc)) from exc
    return PreparedLayerEvaluationReceipt(
        shot=shot,
        receipt=receipt,
        locator=locator,
        sha256=sha256,
        publication=prepared.publication,
        existing_binding=existing,
        replay_receipt_bindings=tuple(replay_receipt_bindings),
        replay_causal_bindings=tuple(replay_causal_bindings),
        authority_binding=authority,
    )


def commit_layer_evaluation_receipt(
    prepared: PreparedLayerEvaluationReceipt,
    guard: LayerFinalizationPublicationGuard,
) -> StoredLayerEvaluationReceipt:
    """Publish one evaluation under the exact still-active finalization claim."""

    if not isinstance(prepared, PreparedLayerEvaluationReceipt):
        raise LayerEvaluationReceiptConflict(
            "layer evaluation commit requires typed prepared receipt"
        )
    if guard.claim != prepared.receipt.claim:
        raise LayerEvaluationReceiptConflict(
            "layer evaluation publication guard belongs to another claim"
        )
    try:
        for source in (
            *prepared.replay_receipt_bindings,
            *prepared.replay_causal_bindings,
        ):
            require_trusted_file_unchanged(source, "layer evaluation causal source")
        if prepared.publication is not None:
            def publish() -> None:
                for source in (
                    *prepared.replay_receipt_bindings,
                    *prepared.replay_causal_bindings,
                ):
                    require_trusted_file_unchanged(
                        source,
                        "layer evaluation causal source",
                    )
                commit_prepared_file(
                    prepared.publication,
                    authority_binding=prepared.authority_binding,
                )

            guard.publish(
                "publish immutable layer evaluation receipt",
                publish,
            )
        else:
            guard.check("reuse immutable layer evaluation receipt")
            assert prepared.existing_binding is not None
            require_trusted_file_unchanged(
                prepared.existing_binding,
                "immutable layer evaluation receipt",
            )
        for source in (
            *prepared.replay_receipt_bindings,
            *prepared.replay_causal_bindings,
        ):
            require_trusted_file_unchanged(source, "layer evaluation causal source")
    except (FilePublicationConflict, TrustedFileError) as exc:
        raise LayerEvaluationReceiptConflict(str(exc)) from exc
    return load_layer_evaluation_receipt(
        prepared.shot,
        prepared.locator,
        expected_sha256=prepared.sha256,
        expected_digest=prepared.receipt.receipt_digest,
    )


def discard_layer_evaluation_receipt(
    prepared: PreparedLayerEvaluationReceipt,
) -> None:
    if isinstance(prepared, PreparedLayerEvaluationReceipt):
        discard_prepared_file(prepared.publication)


def load_layer_evaluation_receipt(
    folder: str | Path,
    locator: str,
    *,
    expected_sha256: str | None = None,
    expected_digest: str | None = None,
) -> StoredLayerEvaluationReceipt:
    """Read and strictly verify one immutable evaluation receipt and its bytes."""

    shot = Path(folder).expanduser().absolute()
    try:
        snapshot = read_trusted_file(
            shot,
            shot / locator,
            "immutable layer evaluation receipt",
            require_nonempty=True,
        )
    except TrustedFileError as exc:
        raise LayerEvaluationReceiptConflict(str(exc)) from exc
    sha256 = hashlib.sha256(snapshot.payload).hexdigest()
    if expected_sha256 is not None and sha256 != expected_sha256:
        raise LayerEvaluationReceiptConflict(
            "immutable layer evaluation receipt file digest changed"
        )
    try:
        receipt = LayerEvaluationReceipt.parse(
            json.loads(snapshot.payload),
            "immutable layer evaluation receipt",
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise LayerEvaluationReceiptConflict(str(exc)) from exc
    if expected_digest is not None and receipt.receipt_digest != expected_digest:
        raise LayerEvaluationReceiptConflict(
            "immutable layer evaluation receipt semantic digest changed"
        )
    return StoredLayerEvaluationReceipt(
        receipt=receipt,
        locator=locator,
        sha256=sha256,
        source_binding=snapshot.binding,
    )


__all__ = [
    "LayerEvaluationReceiptConflict",
    "PreparedLayerEvaluationReceipt",
    "StoredLayerEvaluationReceipt",
    "commit_layer_evaluation_receipt",
    "discard_layer_evaluation_receipt",
    "layer_evaluation_receipt_locator",
    "load_layer_evaluation_receipt",
    "prepare_layer_evaluation_receipt",
]
