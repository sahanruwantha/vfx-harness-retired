"""Derive and re-verify the strict accepted-build index inside the canonical ledger.

``shot.json`` keeps its legacy milestone log, but its ``accepted_build`` member is a
``vfx-harness.shot-ledger/v2`` value that only this writer derives.  Every accepted
layer row comes from the selected DAG's stable order, the layer's current terminal
receipt, its sealed outcome bytes, its composed script bytes, and the coordinator
head's layer generation.  The chain digest is the same ``acceptance-chain/v2`` digest
acceptance judges.  Acceptance is bound only when a passing typed outcome names this
exact authority and chain and every moment has a durable evidence record whose
canonical digest is the outcome's evidence digest.  Callers never supply rows, and
readers re-derive instead of trusting the stored member.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from vfx_harness.domain.acceptance_outcomes import AcceptanceOutcome
from vfx_harness.domain.authority_head_records import parse_authority_selection_token
from vfx_harness.domain.layer_finalization_receipts import LayerFinalizationReceipt
from vfx_harness.domain.shot_ledger_v2 import (
    AcceptanceMomentEvidenceBinding,
    AcceptedLayerAuthority,
    ShotLedgerAcceptance,
    ShotLedgerV2,
)
from vfx_harness.domain.stop_envelope_primitives import canonical_digest
from vfx_harness.infrastructure.trusted_files import TrustedFileError, TrustedFileSnapshot
from vfx_harness.observability.runid import RUN_ID
from vfx_harness.orchestration import (
    accepted_chain,
    authority_receipt_lineage,
    authority_state_context,
    layer_publication,
    plan_bundle_integrity,
    selected_layer_chain,
)
from vfx_harness.orchestration.authority_selection import (
    ResolvedSelectedAuthority,
    SelectedAuthorityResolutionError,
    resolve_selected_authority,
)
from vfx_harness.orchestration.layer_finalization_state import (
    LayerFinalizationConflict,
    current_layer_finalization_receipt,
)
from vfx_harness.orchestration.layer_outcome_paths import layer_outcome_locator
from vfx_harness.orchestration.ledger import Layer
from vfx_harness.orchestration.shot_authority_capture import (
    ShotAuthorityWriterCapability,
    require_live_shot_authority_writer,
    shot_authority_writer_fence,
)
from vfx_harness.orchestration.shot_ledger_index import (
    ACCEPTED_BUILD_KEY,
    DerivedShotLedgerIndex,
    mint_derived_shot_ledger_index,
)
from vfx_harness.orchestration.shot_ledger_lock import LedgerSaveConflict
from vfx_harness.orchestration.shot_ledger_publication import (
    commit_shot_ledger_publication,
    discard_prepared_shot_ledger_publication,
    prepare_shot_ledger_publication,
)

ACCEPTED_BUILD_PROJECTION_BINDING_SCHEMA = (
    "vfx-harness.accepted-build-projection-binding/v1"
)
ACCEPTED_BUILD_PROJECTIONS = frozenset({"republished", "current"})


class ShotLedgerDerivationConflict(ValueError):
    """The accepted-build index cannot be derived, or the stored index is stale."""


@dataclass(frozen=True, slots=True)
class FinalizingLayerPublication:
    """The one layer whose ledger slot the caller publishes with this derivation.

    Its terminal receipt and sealed outcome are already durable; only the legacy
    ledger projection is still in flight, so the row is derived from those durable
    sources and the caller's exact receipt instead of from the on-disk ledger slot.
    """

    layer: Layer
    receipt: LayerFinalizationReceipt


def _root(shot_folder: str | Path) -> Path:
    return Path(shot_folder).expanduser().absolute()


def _snapshot(root: Path, locator: str, where: str) -> TrustedFileSnapshot:
    relative = PurePosixPath(locator)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ShotLedgerDerivationConflict(f"{where} must be a normalized shot-relative locator: {locator!r}")
    try:
        return plan_bundle_integrity.read_real_file_snapshot(root, root / relative, where)
    except (OSError, TrustedFileError, ValueError) as exc:
        raise ShotLedgerDerivationConflict(f"{where} is not a readable real file: {exc}") from exc


def _receipt_bound_publication(
    root: Path,
    layer: Layer,
    receipt: LayerFinalizationReceipt,
    *,
    token: Any,
    selected_authority: ResolvedSelectedAuthority,
    verified_prefix: tuple[tuple[str, str, str, str], ...],
) -> layer_publication.VerifiedLayerPublication:
    """Verify one durable terminal receipt against its sources, whatever its status.

    This serves the finalizing layer, whose ledger slot is still in flight, and every
    durable failed receipt, which the passed-only current-publication verifier cannot
    bind.  The returned value carries exactly what the post-commit verifier will read
    back: the ledger projection fields are the ones the reconciling transaction
    publishes from this same receipt, so a reader re-deriving after commit hashes
    identical rows.
    """

    layer_id = str(layer.id)
    try:
        current = current_layer_finalization_receipt(root, layer_id)
    except (LayerFinalizationConflict, OSError, TypeError, ValueError) as exc:
        raise ShotLedgerDerivationConflict(
            f"layer {layer_id} terminal state is invalid: {exc}"
        ) from exc
    if current != receipt:
        raise ShotLedgerDerivationConflict(
            f"layer {layer_id} receipt is not the durable current terminal receipt"
        )
    if receipt.claim.layer_id != layer_id or receipt.layer_script_path != str(layer.script):
        raise ShotLedgerDerivationConflict(
            f"layer {layer_id} receipt names another layer or script"
        )
    if receipt.claim.selection_token != token:
        # A preserved receipt may reconcile under a successor selection only through
        # the same contiguous lineage authorization the publication verifier requires.
        try:
            authority_receipt_lineage.require_preserved_layer_finalization_authorization(
                root,
                receipt,
                selected_authority,
            )
        except (authority_receipt_lineage.AuthorityReceiptLineageError, TypeError, ValueError) as exc:
            raise ShotLedgerDerivationConflict(
                f"layer {layer_id} receipt is not authorized by the selected authority: {exc}"
            ) from exc
    observed_prefix = tuple(
        (row.layer_id, row.finalization_receipt_digest, row.script_path, row.script_sha256)
        for row in receipt.claim.predecessor_inputs
    )
    if observed_prefix != verified_prefix:
        raise ShotLedgerDerivationConflict(
            f"layer {layer_id} receipt does not bind the exact verified accepted prefix"
        )
    try:
        outcome, outcome_snapshot = layer_publication.capture_sealed_outcome_projection(
            root,
            layer_id=layer_id,
            receipt=receipt,
            expect_passed=receipt.final_status == "passed",
        )
    except layer_publication.LayerPublicationConflict as exc:
        raise ShotLedgerDerivationConflict(str(exc)) from exc
    return layer_publication.VerifiedLayerPublication(
        receipt=receipt,
        outcome=outcome,
        outcome_bytes=outcome_snapshot.payload,
        outcome_locator=layer_outcome_locator(layer_id),
        outcome_sha256=outcome_snapshot.sha256,
        ledger_status=receipt.final_status,
        ledger_receipt_digest=receipt.receipt_digest,
        ledger_script_path=receipt.layer_script_path,
        ledger_script_sha256=receipt.layer_script_sha256,
    )


def _accepted_layer(
    root: Path,
    *,
    layer_id: str,
    generation_digest: str,
    receipt_digest: str,
    script_locator: str,
    expected_script_sha256: str,
    outcome_locator: str,
    outcome_sha256: str,
) -> AcceptedLayerAuthority:
    script = _snapshot(root, script_locator, f"layer {layer_id} composed script")
    if script.sha256 != expected_script_sha256:
        raise ShotLedgerDerivationConflict(
            f"layer {layer_id} composed script bytes differ from its terminal receipt; "
            f"expected {expected_script_sha256}, found {script.sha256}"
        )
    try:
        return AcceptedLayerAuthority(
            layer_id=layer_id,
            layer_generation_digest=generation_digest,
            finalization_receipt_digest=receipt_digest,
            composed_script_locator=script_locator,
            composed_script_sha256=script.sha256,
            sealed_outcome_locator=outcome_locator,
            sealed_outcome_sha256=outcome_sha256,
        )
    except ValueError as exc:
        raise ShotLedgerDerivationConflict(f"layer {layer_id} accepted row is invalid: {exc}") from exc


def _acceptance_binding(
    root: Path,
    selected_authority: ResolvedSelectedAuthority,
    acceptance_record: Mapping[str, Any] | None,
    *,
    chain_digest: str,
    complete: bool,
) -> ShotLedgerAcceptance | None:
    """Bind a passing outcome for this exact authority and chain, else nothing."""

    if not isinstance(acceptance_record, Mapping):
        return None
    raw_outcome = acceptance_record.get("outcome")
    if raw_outcome is None:
        return None
    try:
        outcome = AcceptanceOutcome.from_dict(raw_outcome, "shot.json.acceptance.outcome")
    except ValueError as exc:
        raise ShotLedgerDerivationConflict(f"typed acceptance outcome is malformed: {exc}") from exc
    assert selected_authority.plan is not None
    assert selected_authority.assertion.effective_view is not None
    if (
        not complete
        or not outcome.passed
        or outcome.chain_digest != chain_digest
        or outcome.bundle_digest != selected_authority.plan.bundle.content_hash
        or outcome.view_digest != selected_authority.assertion.effective_view.digest
    ):
        return None
    records = acceptance_record.get("evidence_records")
    moments = acceptance_record.get("moments")
    if not isinstance(records, Mapping) or not isinstance(moments, Mapping):
        return None
    bindings: list[AcceptanceMomentEvidenceBinding] = []
    for moment in outcome.moments:
        locator = records.get(moment.moment_id)
        row = moments.get(moment.moment_id)
        if not isinstance(locator, str) or not isinstance(row, Mapping):
            return None
        record = _snapshot(root, locator, f"acceptance moment {moment.moment_id} evidence record")
        try:
            capture = json.loads(record.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ShotLedgerDerivationConflict(
                f"acceptance moment {moment.moment_id} evidence record is not JSON"
            ) from exc
        if canonical_digest(capture) != moment.evidence_digest:
            raise ShotLedgerDerivationConflict(
                f"acceptance moment {moment.moment_id} evidence record does not hash to "
                "the typed outcome's evidence digest"
            )
        render_locator = row.get("render")
        reference_locator = row.get("ref")
        if not isinstance(render_locator, str) or not isinstance(reference_locator, str):
            return None
        render = _snapshot(root, render_locator, f"acceptance moment {moment.moment_id} render")
        reference = _snapshot(root, reference_locator, f"acceptance moment {moment.moment_id} reference")
        if (
            capture.get("render_sha256") != render.sha256
            or capture.get("reference_sha256") != reference.sha256
        ):
            raise ShotLedgerDerivationConflict(
                f"acceptance moment {moment.moment_id} render or reference bytes differ "
                "from its evidence record"
            )
        try:
            bindings.append(
                AcceptanceMomentEvidenceBinding(
                    moment_id=moment.moment_id,
                    evidence_digest=moment.evidence_digest,
                    evidence_record_locator=locator,
                    evidence_record_sha256=record.sha256,
                    render_locator=render_locator,
                    render_sha256=render.sha256,
                    reference_locator=reference_locator,
                    reference_sha256=reference.sha256,
                )
            )
        except ValueError as exc:
            raise ShotLedgerDerivationConflict(
                f"acceptance moment {moment.moment_id} evidence binding is invalid: {exc}"
            ) from exc
    try:
        return ShotLedgerAcceptance(outcome=outcome, moment_evidence=tuple(bindings))
    except ValueError as exc:
        raise ShotLedgerDerivationConflict(f"acceptance binding is invalid: {exc}") from exc


def derive_shot_ledger_index(
    shot_folder: str | Path,
    selected_authority: ResolvedSelectedAuthority,
    *,
    writer_capability: ShotAuthorityWriterCapability,
    acceptance_record: Mapping[str, Any] | None,
    finalizing: FinalizingLayerPublication | None = None,
) -> DerivedShotLedgerIndex:
    """Derive the complete accepted-build index from selected authority and receipts.

    The caller holds the shot-authority writer fence.  Accepted layers are the leading
    layers of the selected DAG's stable order whose current terminal receipt, sealed
    outcome, composed script, and ledger projection verify; ``finalizing`` names the
    one layer whose ledger projection is published together with this index.
    """

    root = _root(shot_folder)
    require_live_shot_authority_writer(writer_capability, root)
    if selected_authority.plan is None or selected_authority.assertion.effective_view is None:
        raise ShotLedgerDerivationConflict("accepted-build derivation requires selected plan authority")
    token = parse_authority_selection_token(
        selected_authority.selection_token.to_dict(),
        "selected authority token for accepted-build derivation",
    )
    try:
        context = authority_state_context.resolve_current_authority_state(root)
    except authority_state_context.AuthorityStateContextError as exc:
        raise ShotLedgerDerivationConflict(str(exc)) from exc
    if context.head.selection_token != token:
        raise ShotLedgerDerivationConflict(
            "authority-state coordinator head does not authorize the selected authority"
        )
    # The head lists the states its own transition installed; a layer's accepted
    # generation is its receipt's plan hash, which the finalization guard binds to
    # the layer's durable state, and the head must agree wherever it names the layer.
    head_generations = {
        image.layer_id: image.binding.layer_generation_digest
        for image in context.commit.installed_states
    }
    try:
        layers = selected_layer_chain.selected_authority_layer_chain(selected_authority)
    except ValueError as exc:
        raise ShotLedgerDerivationConflict(str(exc)) from exc

    rows: list[AcceptedLayerAuthority] = []
    publications: dict[str, layer_publication.VerifiedLayerPublication] = {}
    verified_prefix: list[tuple[str, str, str, str]] = []
    finalizing_seen = False

    def _generation(layer_id: str, receipt: LayerFinalizationReceipt) -> str:
        generation = receipt.claim.plan_hash
        bound = head_generations.get(layer_id)
        if bound is not None and bound != generation:
            raise ShotLedgerDerivationConflict(
                f"authority-state coordinator head binds layer {layer_id} to generation "
                f"{bound}, but its terminal receipt was minted for {generation}"
            )
        return generation

    # Every layer with a durable terminal receipt contributes its receipt-bound chain
    # row, whatever that receipt's status: the row must not depend on the on-disk
    # ledger slot that this same publication rewrites, or a crash-resume derivation
    # would drift from the first one.  The accepted prefix itself ends in front of
    # the first non-passed receipt; unfinalized layers contribute legacy rows.
    for layer in layers:
        layer_id = str(layer.id)
        is_finalizing = finalizing is not None and str(finalizing.layer.id) == layer_id
        if is_finalizing:
            assert finalizing is not None
            finalizing_seen = True
            publication = _receipt_bound_publication(
                root,
                finalizing.layer,
                finalizing.receipt,
                token=token,
                selected_authority=selected_authority,
                verified_prefix=tuple(verified_prefix),
            )
        else:
            try:
                receipt = current_layer_finalization_receipt(root, layer_id)
            except (LayerFinalizationConflict, OSError, TypeError, ValueError) as exc:
                raise ShotLedgerDerivationConflict(
                    f"layer {layer_id} terminal finalization state is invalid: {exc}"
                ) from exc
            if receipt is None:
                # Unfinalized terminal state ends the accepted prefix; only malformed
                # state above is an authority defect.
                break
            if receipt.final_status != "passed":
                # The current-publication verifier accepts only passed outcomes; a
                # durable failed receipt is still bound through the same receipt,
                # prefix, and sealed-outcome verification the finalizing layer uses.
                publication = _receipt_bound_publication(
                    root,
                    layer,
                    receipt,
                    token=token,
                    selected_authority=selected_authority,
                    verified_prefix=tuple(verified_prefix),
                )
            else:
                try:
                    publication = layer_publication.require_current_layer_publication(
                        root,
                        layer,
                        selected_authority,
                    )
                except layer_publication.LayerPublicationConflict as exc:
                    raise ShotLedgerDerivationConflict(
                        f"layer {layer_id} publication is not current: {exc}"
                    ) from exc
        publications[layer_id] = publication
        if publication.receipt.final_status != "passed":
            # A failed terminal receipt is durable authority that this layer is not
            # accepted; the accepted prefix ends in front of it.
            break
        verified_prefix.append(
            (
                layer_id,
                publication.receipt.receipt_digest,
                publication.receipt.layer_script_path,
                publication.receipt.layer_script_sha256,
            )
        )
        rows.append(
            _accepted_layer(
                root,
                layer_id=layer_id,
                generation_digest=_generation(layer_id, publication.receipt),
                receipt_digest=publication.receipt.receipt_digest,
                script_locator=publication.ledger_script_path,
                expected_script_sha256=publication.ledger_script_sha256,
                outcome_locator=publication.outcome_locator,
                outcome_sha256=publication.outcome_sha256,
            )
        )
        if is_finalizing:
            # Later layers cannot yet bind this new receipt as their predecessor.
            break
    if finalizing is not None and not finalizing_seen:
        raise ShotLedgerDerivationConflict(
            f"finalizing layer {finalizing.layer.id} is not the next layer of the "
            "verified accepted prefix"
        )

    chain_rows = accepted_chain.accepted_chain_rows(
        root,
        layers,
        publications,
        ledger_statuses=accepted_chain.legacy_ledger_statuses(root),
    )
    chain_digest = accepted_chain.accepted_chain_digest(chain_rows)
    acceptance = _acceptance_binding(
        root,
        selected_authority,
        acceptance_record,
        chain_digest=chain_digest,
        complete=len(rows) == len(layers),
    )
    try:
        ledger = ShotLedgerV2(
            selection_token=token,
            authority_state_head_ref=context.head_ref,
            accepted_layers=tuple(rows),
            accepted_chain_digest=chain_digest,
            acceptance=acceptance,
        )
    except ValueError as exc:
        raise ShotLedgerDerivationConflict(f"derived accepted-build index is invalid: {exc}") from exc
    return mint_derived_shot_ledger_index(ledger)


def _ledger_document(root: Path) -> Mapping[str, Any]:
    """Read the canonical ledger as an object, or an empty object when absent."""

    path = root / "shot.json"
    if not path.is_file():
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ShotLedgerDerivationConflict(f"shot.json is unreadable: {exc}") from exc
    if not isinstance(document, Mapping):
        raise ShotLedgerDerivationConflict("shot.json must be a JSON object")
    return document


def _stored_index(document: Mapping[str, Any]) -> ShotLedgerV2 | None:
    stored = document.get(ACCEPTED_BUILD_KEY)
    if stored is None:
        return None
    try:
        return ShotLedgerV2.from_dict(stored, "shot.json.accepted_build")
    except ValueError as exc:
        raise ShotLedgerDerivationConflict(f"stored accepted-build index is malformed: {exc}") from exc


def read_stored_shot_ledger_index(shot_root: str | Path) -> ShotLedgerV2 | None:
    """Parse the stored ``accepted_build`` member without trusting it as current."""

    return _stored_index(_ledger_document(_root(shot_root)))


def republish_shot_ledger_index(
    shot_folder: str | Path,
    *,
    writer_capability: ShotAuthorityWriterCapability,
    operation: str,
) -> tuple[str, ShotLedgerV2]:
    """Re-derive the member for the live selection and publish it only when it changed.

    Plan and JIT republication call this right after their successor coordinator head
    commits, and WAL recovery calls it in both dispositions, so the stored member never
    outlives the selection it was derived from.  When the stored member already equals
    the derivation the ledger is left byte-identical and ``"current"`` is returned;
    otherwise the new generation commits through the typed transport and
    ``"republished"`` is returned.  A shot without a ledger receives one holding only
    the derived member.
    """

    root = _root(shot_folder)
    require_live_shot_authority_writer(writer_capability, root)
    if not isinstance(operation, str) or not operation.strip():
        raise ShotLedgerDerivationConflict(
            "accepted-build republication requires a non-empty operation name"
        )
    try:
        selected = resolve_selected_authority(root)
    except SelectedAuthorityResolutionError as exc:
        raise ShotLedgerDerivationConflict(
            f"accepted-build republication cannot resolve selected authority: {exc}"
        ) from exc
    document = _ledger_document(root)
    stored = _stored_index(document)
    derived = derive_shot_ledger_index(
        root,
        selected,
        writer_capability=writer_capability,
        acceptance_record=document.get("acceptance"),
    )
    if stored is not None and stored == derived.ledger:
        return "current", derived.ledger
    data: dict[str, Any] = (
        dict(document) if document else {"shot": root.name, "milestones": {}}
    )
    binding = json.dumps(
        {
            "schema": ACCEPTED_BUILD_PROJECTION_BINDING_SCHEMA,
            "operation": operation,
            "selection_token": selected.selection_token.to_dict(),
            "authority_state_head_ref": derived.ledger.authority_state_head_ref.as_dict(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    try:
        prepared = prepare_shot_ledger_publication(
            root / "shot.json",
            data,
            data,
            frozenset(),
            run_id=RUN_ID,
            authority_binding=binding,
            derived_index=derived,
        )
    except LedgerSaveConflict as exc:
        raise ShotLedgerDerivationConflict(
            f"accepted-build republication could not be prepared: {exc}"
        ) from exc
    try:
        commit_shot_ledger_publication(
            prepared,
            authority_binding=binding,
            writer_capability=writer_capability,
        )
    except LedgerSaveConflict as exc:
        discard_prepared_shot_ledger_publication(prepared)
        raise ShotLedgerDerivationConflict(
            f"accepted-build republication could not commit: {exc}"
        ) from exc
    except BaseException:
        discard_prepared_shot_ledger_publication(prepared)
        raise
    return "republished", derived.ledger


def current_shot_ledger_index(
    shot_folder: str | Path,
    selected_authority: ResolvedSelectedAuthority,
) -> ShotLedgerV2:
    """Return the stored index only when a fresh derivation reproduces it exactly."""

    root = _root(shot_folder)
    stored = read_stored_shot_ledger_index(root)
    if stored is None:
        raise ShotLedgerDerivationConflict(
            "shot.json has no accepted-build index; an authority-state publication, "
            "layer finalization, or acceptance publication derives it"
        )
    with shot_authority_writer_fence(root) as capability:
        document = _ledger_document(root)
        derived = derive_shot_ledger_index(
            root,
            selected_authority,
            writer_capability=capability,
            acceptance_record=document.get("acceptance"),
        )
    if derived.ledger != stored:
        raise ShotLedgerDerivationConflict(
            "stored accepted-build index is stale: re-derivation yields "
            f"{derived.ledger.index_digest}, stored {stored.index_digest}; "
            "`vfx recover-authority-state` or the next authority-state, finalization, "
            "or acceptance publication republishes it"
        )
    return stored


__all__ = [
    "ACCEPTED_BUILD_PROJECTIONS",
    "ACCEPTED_BUILD_PROJECTION_BINDING_SCHEMA",
    "FinalizingLayerPublication",
    "ShotLedgerDerivationConflict",
    "current_shot_ledger_index",
    "derive_shot_ledger_index",
    "read_stored_shot_ledger_index",
    "republish_shot_ledger_index",
]
