"""One receipt-backed read boundary for every published layer consumer.

Unit completion, a composed script, a sealed outcome, and a passing ledger row are
individually insufficient.  This module accepts a layer only when all public
projections agree with the exact terminal finalization receipt that is current under
the selected authority and whose complete source closure still verifies.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vfx_harness.domain.authority_head_records import parse_authority_selection_token
from vfx_harness.domain.layer_finalizations import LayerFinalizationReceipt
from vfx_harness.domain.layer_outcomes import (
    LayerOutcomeContractError,
    SealedLayerOutcome,
    parse_sealed_layer_outcome,
)
from vfx_harness.infrastructure.trusted_files import (
    TrustedFileError,
    TrustedFileSnapshot,
    require_trusted_file_unchanged,
)
from vfx_harness.orchestration import plan_bundle_integrity
from vfx_harness.orchestration.authority_receipt_lineage import (
    AuthorityReceiptLineageError,
    LayerFinalizationLineageAuthorization,
    require_preserved_layer_finalization_authorization,
)
from vfx_harness.orchestration.layer_finalization_predecessors import (
    LayerFinalizationPredecessorConflict,
    selected_finalization_predecessor_layers,
)
from vfx_harness.orchestration.layer_finalization_state import (
    LayerFinalizationConflict,
    current_layer_finalization_receipt,
    terminal_layer_finalization_guard,
)
from vfx_harness.orchestration.layer_outcome_paths import (
    layer_outcome_locator,
    layer_outcome_path,
)
from vfx_harness.orchestration.plan_bundle_integrity import PlanPublicationError

if TYPE_CHECKING:
    from vfx_harness.orchestration.authority_selection import (
        ResolvedSelectedAuthority,
    )
    from vfx_harness.orchestration.ledger import Layer


class LayerPublicationConflict(ValueError):
    """A layer has no complete, current, mutually consistent publication."""


@dataclass(frozen=True, slots=True)
class VerifiedLayerPublication:
    """The exact immutable identities accepted at one bounded verification point."""

    receipt: LayerFinalizationReceipt
    outcome: SealedLayerOutcome
    outcome_bytes: bytes
    outcome_locator: str
    outcome_sha256: str
    ledger_status: str
    ledger_receipt_digest: str
    ledger_script_path: str
    ledger_script_sha256: str


@dataclass(frozen=True, slots=True)
class CapturedLayerPublicationProjections:
    """Descriptor-bound outcome and ledger projections for one terminal receipt."""

    layer_id: str
    receipt: LayerFinalizationReceipt
    outcome_snapshot: TrustedFileSnapshot
    outcome: SealedLayerOutcome
    ledger_snapshot: TrustedFileSnapshot
    ledger_status: str
    ledger_receipt_digest: str
    ledger_script_path: str
    ledger_script_sha256: str

    @property
    def receipt_digest(self) -> str:
        return self.receipt.receipt_digest


@dataclass(frozen=True, slots=True)
class _CapturedVerifiedLayerPublication:
    """Internal guard material retained until the complete prefix closes."""

    layer: Layer
    publication: VerifiedLayerPublication
    projections: CapturedLayerPublicationProjections
    lineage_authorization: LayerFinalizationLineageAuthorization | None


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    row: dict[str, Any] = {}
    for key, value in pairs:
        if key in row:
            raise ValueError(f"duplicate object key {key!r}")
        row[key] = value
    return row


def _reject_non_finite(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value!r}")


def _json_object(snapshot: TrustedFileSnapshot, where: str) -> dict[str, Any]:
    try:
        value = json.loads(
            snapshot.payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_non_finite,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise LayerPublicationConflict(f"{where} is not strict JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise LayerPublicationConflict(f"{where} must be a JSON object")
    return value


def _read_snapshot(root: Path, path: Path, where: str) -> TrustedFileSnapshot:
    try:
        return plan_bundle_integrity.read_real_file_snapshot(root, path, where)
    except (OSError, PlanPublicationError, TrustedFileError, ValueError) as exc:
        raise LayerPublicationConflict(str(exc)) from exc


def _require_exact_outcome(
    record: Mapping[str, Any],
    *,
    layer_id: str,
    receipt: LayerFinalizationReceipt,
) -> SealedLayerOutcome:
    try:
        outcome = parse_sealed_layer_outcome(
            record,
            expected_layer_id=layer_id,
        )
        embedded = LayerFinalizationReceipt.parse(
            record.get("finalization_receipt"),
            "sealed layer outcome.finalization_receipt",
        )
    except (LayerOutcomeContractError, ValueError) as exc:
        raise LayerPublicationConflict(str(exc)) from exc
    if embedded != receipt or record.get("finalization_receipt") != receipt.as_dict():
        raise LayerPublicationConflict(
            f"layer {layer_id} outcome does not embed the exact current terminal "
            "finalization receipt"
        )
    if outcome.status != "passed":
        raise LayerPublicationConflict(
            f"layer {layer_id} outcome is {outcome.status!r}, not 'passed'"
        )
    return outcome


def _require_exact_ledger_slot(
    ledger: Mapping[str, Any],
    *,
    layer_id: str,
    receipt: LayerFinalizationReceipt,
) -> tuple[str, str, str, str]:
    milestones = ledger.get("milestones")
    if not isinstance(milestones, Mapping):
        raise LayerPublicationConflict("shot.json.milestones must be an object")
    slot = milestones.get(layer_id)
    if not isinstance(slot, Mapping):
        raise LayerPublicationConflict(
            f"shot.json has no durable layer-ledger slot for {layer_id}"
        )
    status = slot.get("status")
    receipt_digest = slot.get("finalization_receipt_digest")
    script_path = slot.get("script")
    script_sha256 = slot.get("script_sha256")
    if status != "passed":
        raise LayerPublicationConflict(
            f"layer {layer_id} ledger status must be 'passed', found {status!r}"
        )
    if receipt_digest != receipt.receipt_digest:
        raise LayerPublicationConflict(
            f"layer {layer_id} ledger finalization receipt digest does not match "
            "the current terminal receipt"
        )
    if script_path != receipt.layer_script_path:
        raise LayerPublicationConflict(
            f"layer {layer_id} ledger script locator does not match the terminal receipt"
        )
    if script_sha256 != receipt.layer_script_sha256:
        raise LayerPublicationConflict(
            f"layer {layer_id} ledger script SHA-256 does not match the terminal receipt"
        )
    return str(status), str(receipt_digest), str(script_path), str(script_sha256)


def capture_layer_publication_projections(
    folder: str | Path,
    *,
    layer_id: str,
    receipt: LayerFinalizationReceipt,
) -> CapturedLayerPublicationProjections:
    """Capture and validate the mutable public projections without taking locks."""

    if not isinstance(receipt, LayerFinalizationReceipt):
        raise LayerPublicationConflict(
            "layer publication projection capture requires a typed terminal receipt"
        )
    layer_id = str(layer_id)
    if receipt.claim.layer_id != layer_id:
        raise LayerPublicationConflict(
            f"layer {layer_id} projection capture received another layer's receipt"
        )
    root = Path(folder).expanduser().absolute()
    outcome_snapshot = _read_snapshot(
        root,
        layer_outcome_path(root, layer_id),
        f"layer {layer_id} sealed outcome",
    )
    ledger_snapshot = _read_snapshot(
        root,
        root / "shot.json",
        "durable layer ledger",
    )
    outcome_record = _json_object(
        outcome_snapshot,
        f"layer {layer_id} sealed outcome",
    )
    ledger_record = _json_object(ledger_snapshot, "durable layer ledger")
    outcome = _require_exact_outcome(
        outcome_record,
        layer_id=layer_id,
        receipt=receipt,
    )
    status, receipt_digest, script_path, script_sha256 = _require_exact_ledger_slot(
        ledger_record,
        layer_id=layer_id,
        receipt=receipt,
    )
    captured = CapturedLayerPublicationProjections(
        layer_id=layer_id,
        receipt=receipt,
        outcome_snapshot=outcome_snapshot,
        outcome=outcome,
        ledger_snapshot=ledger_snapshot,
        ledger_status=status,
        ledger_receipt_digest=receipt_digest,
        ledger_script_path=script_path,
        ledger_script_sha256=script_sha256,
    )
    require_layer_publication_projections_unchanged(captured)
    return captured


def require_layer_publication_projections_unchanged(
    captured: CapturedLayerPublicationProjections,
) -> None:
    """Reject any outcome or ledger rebinding after a bounded capture."""

    if not isinstance(captured, CapturedLayerPublicationProjections):
        raise LayerPublicationConflict(
            "layer publication projection guard requires typed captured projections"
        )
    outcome_record = _json_object(
        captured.outcome_snapshot,
        f"layer {captured.layer_id} sealed outcome",
    )
    ledger_record = _json_object(
        captured.ledger_snapshot,
        "durable layer ledger",
    )
    outcome = _require_exact_outcome(
        outcome_record,
        layer_id=captured.layer_id,
        receipt=captured.receipt,
    )
    status, receipt_digest, script_path, script_sha256 = _require_exact_ledger_slot(
        ledger_record,
        layer_id=captured.layer_id,
        receipt=captured.receipt,
    )
    if (
        outcome != captured.outcome
        or status != captured.ledger_status
        or receipt_digest != captured.ledger_receipt_digest
        or script_path != captured.ledger_script_path
        or script_sha256 != captured.ledger_script_sha256
    ):
        raise LayerPublicationConflict(
            f"layer {captured.layer_id} captured publication projection changed "
            "typed identity"
        )
    try:
        require_trusted_file_unchanged(
            captured.outcome_snapshot.binding,
            f"layer {captured.layer_id} sealed outcome",
        )
        require_trusted_file_unchanged(
            captured.ledger_snapshot.binding,
            "durable layer ledger",
        )
    except TrustedFileError as exc:
        raise LayerPublicationConflict(str(exc)) from exc


def _require_captured_prefix_projections_current(
    root: Path,
    captured: Sequence[_CapturedVerifiedLayerPublication],
) -> None:
    """Close every captured outcome and ledger slot at one end-of-prefix point.

    Outcomes are per-layer immutable publications, so their exact descriptor bindings
    must survive.  The ledger is a shared merge target: reopen it once and compare only
    the receipt-bound slots in this prefix so a legitimate unrelated slot publication
    does not create a false conflict.
    """

    try:
        for row in captured:
            require_trusted_file_unchanged(
                row.projections.outcome_snapshot.binding,
                f"layer {row.projections.layer_id} sealed outcome",
            )
        ledger_snapshot = _read_snapshot(
            root,
            root / "shot.json",
            "durable layer ledger",
        )
        ledger_record = _json_object(ledger_snapshot, "durable layer ledger")
        for row in captured:
            observed = _require_exact_ledger_slot(
                ledger_record,
                layer_id=row.projections.layer_id,
                receipt=row.projections.receipt,
            )
            expected = (
                row.projections.ledger_status,
                row.projections.ledger_receipt_digest,
                row.projections.ledger_script_path,
                row.projections.ledger_script_sha256,
            )
            if observed != expected:
                raise LayerPublicationConflict(
                    f"layer {row.projections.layer_id} ledger projection changed "
                    "typed identity during prefix verification"
                )
        require_trusted_file_unchanged(
            ledger_snapshot.binding,
            "durable layer ledger",
        )
    except TrustedFileError as exc:
        raise LayerPublicationConflict(str(exc)) from exc


def _require_captured_prefix_current(
    root: Path,
    captured: Sequence[_CapturedVerifiedLayerPublication],
    selected_authority: ResolvedSelectedAuthority,
) -> None:
    """Revalidate one exact prefix while its terminal state is held stable."""

    if not captured:
        raise LayerPublicationConflict("layer publication prefix must not be empty")
    # The target receipt names the complete stable prefix.  Enter it first so its
    # canonical state-lock acquisition retains every involved layer while the
    # predecessor source guards nest reentrantly in stable prefix order.
    target = captured[-1]
    guard_order = (target, *captured[:-1])
    try:
        with ExitStack() as stack:
            for row in guard_order:
                stack.enter_context(
                    terminal_layer_finalization_guard(
                        root,
                        row.publication.receipt,
                        tuple(row.layer.stages),
                        selection_token=selected_authority.selection_token,
                        lineage_authorization=row.lineage_authorization,
                    )
                )
            _require_captured_prefix_projections_current(root, captured)
    except LayerPublicationConflict:
        raise
    except (LayerFinalizationConflict, OSError, TypeError, ValueError) as exc:
        raise LayerPublicationConflict(
            f"layer publication prefix changed during verification: {exc}"
        ) from exc


def _require_one_current_layer_publication(
    folder: str | Path,
    layer: Layer,
    selected_authority: ResolvedSelectedAuthority,
    *,
    expected_predecessors: Sequence[VerifiedLayerPublication],
) -> _CapturedVerifiedLayerPublication:
    """Verify one layer after its exact selected prefix has been verified."""

    root = Path(folder).expanduser().absolute()
    layer_id = str(layer.id)
    try:
        receipt = current_layer_finalization_receipt(root, layer_id)
    except (LayerFinalizationConflict, OSError, TypeError, ValueError) as exc:
        raise LayerPublicationConflict(
            f"layer {layer_id} terminal finalization state is invalid: {exc}"
        ) from exc
    if receipt is None:
        raise LayerPublicationConflict(
            f"layer {layer_id} has no current terminal finalization receipt"
        )
    if receipt.claim.layer_id != layer_id:
        raise LayerPublicationConflict(
            f"layer {layer_id} terminal receipt belongs to layer {receipt.claim.layer_id}"
        )
    if receipt.layer_script_path != str(layer.script):
        raise LayerPublicationConflict(
            f"layer {layer_id} terminal receipt names script "
            f"{receipt.layer_script_path!r}, expected {str(layer.script)!r}"
        )
    if receipt.final_status != "passed":
        raise LayerPublicationConflict(
            f"layer {layer_id} terminal finalization is {receipt.final_status!r}, "
            "not 'passed'"
        )
    expected_prefix = tuple(
        (
            publication.receipt.claim.layer_id,
            publication.receipt.receipt_digest,
            publication.receipt.layer_script_path,
            publication.receipt.layer_script_sha256,
        )
        for publication in expected_predecessors
    )
    observed_prefix = tuple(
        (
            row.layer_id,
            row.finalization_receipt_digest,
            row.script_path,
            row.script_sha256,
        )
        for row in receipt.claim.predecessor_inputs
    )
    if observed_prefix != expected_prefix:
        raise LayerPublicationConflict(
            f"layer {layer_id} terminal receipt does not bind the exact verified "
            "selected-layer publication prefix"
        )
    try:
        selected_projection = parse_authority_selection_token(
            selected_authority.selection_token.to_dict(),
            "selected authority token for layer publication",
        )
        lineage_authorization = (
            None
            if receipt.claim.selection_token == selected_projection
            else require_preserved_layer_finalization_authorization(
                root,
                receipt,
                selected_authority,
            )
        )
    except (AuthorityReceiptLineageError, TypeError, ValueError) as exc:
        raise LayerPublicationConflict(
            f"layer {layer_id} terminal receipt is not authorized by selected "
            f"authority: {exc}"
        ) from exc

    try:
        with terminal_layer_finalization_guard(
            root,
            receipt,
            tuple(layer.stages),
            selection_token=selected_authority.selection_token,
            lineage_authorization=lineage_authorization,
        ):
            projections = capture_layer_publication_projections(
                root,
                layer_id=layer_id,
                receipt=receipt,
            )
            require_layer_publication_projections_unchanged(projections)
    except LayerPublicationConflict:
        raise
    except (LayerFinalizationConflict, OSError, TypeError, ValueError) as exc:
        raise LayerPublicationConflict(
            f"layer {layer_id} publication is not current: {exc}"
        ) from exc

    publication = VerifiedLayerPublication(
        receipt=receipt,
        outcome=projections.outcome,
        outcome_bytes=projections.outcome_snapshot.payload,
        outcome_locator=layer_outcome_locator(layer_id),
        outcome_sha256=projections.outcome_snapshot.sha256,
        ledger_status=projections.ledger_status,
        ledger_receipt_digest=projections.ledger_receipt_digest,
        ledger_script_path=projections.ledger_script_path,
        ledger_script_sha256=projections.ledger_script_sha256,
    )
    return _CapturedVerifiedLayerPublication(
        layer=layer,
        publication=publication,
        projections=projections,
        lineage_authorization=lineage_authorization,
    )


def require_current_layer_publication(
    folder: str | Path,
    layer: Layer,
    selected_authority: ResolvedSelectedAuthority,
) -> VerifiedLayerPublication:
    """Require the exact stable selected prefix to close on current publications.

    The prefix is verified iteratively so every predecessor terminal receipt, source,
    outcome, and ledger projection is checked once.  This remains a bounded read;
    callers repeat it around long replay, render, or other paid work.
    """

    root = Path(folder).expanduser().absolute()
    try:
        predecessors = selected_finalization_predecessor_layers(
            root,
            str(layer.id),
            selection_token=selected_authority.selection_token,
            selected_authority=selected_authority,
        )
    except (LayerFinalizationPredecessorConflict, OSError, TypeError, ValueError) as exc:
        raise LayerPublicationConflict(
            f"layer {layer.id} selected predecessor prefix is invalid: {exc}"
        ) from exc
    captured: list[_CapturedVerifiedLayerPublication] = []
    for current in (*predecessors, layer):
        captured.append(
            _require_one_current_layer_publication(
                root,
                current,
                selected_authority,
                expected_predecessors=tuple(
                    row.publication for row in captured
                ),
            )
        )
    _require_captured_prefix_current(root, captured, selected_authority)
    return captured[-1].publication


__all__ = [
    "CapturedLayerPublicationProjections",
    "LayerPublicationConflict",
    "VerifiedLayerPublication",
    "capture_layer_publication_projections",
    "require_current_layer_publication",
    "require_layer_publication_projections_unchanged",
]
