"""Independent authorization of receipts in an unpublished candidate view."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from vfx_harness.domain.authority_head_records import (
    JIT_CURRENT_PATH,
    OVERLAY_ARTIFACTS,
    AuthoritySelectionTokenProjection,
    canonical_view_hash,
    decode_canonical_json_object,
    parse_jit_view_pointer,
    require_live_jit_artifact_locators,
    require_materialized_layers_match,
)
from vfx_harness.domain.authority_preview_records import (
    AUTHORITY_PREVIEW_REFERENCE_PATH,
    AuthorityPreviewReference,
)
from vfx_harness.domain.authority_state_records import AuthorityStateTransitionIntent
from vfx_harness.domain.layer_finalizations import LayerFinalizationReceipt
from vfx_harness.domain.unit_completion_receipts import UnitCompletionReceipt
from vfx_harness.evaluation.authority_state_derivation import (
    verify_recompiled_authority_state_transition,
)
from vfx_harness.infrastructure.trusted_files import (
    TrustedFileBinding,
    require_trusted_file_unchanged,
)
from vfx_harness.orchestration import plan_bundle_integrity
from vfx_harness.orchestration.authority_capsule_resolution import (
    capture_selected_authority_capsules,
    compile_proposed_authority_capsules,
)
from vfx_harness.orchestration.authority_layer_finalization_sources import (
    prepare_transition_preserved_finalization_sources,
    require_transition_preserved_finalization_sources,
)
from vfx_harness.orchestration.authority_selection_heads import (
    read_authority_selection_heads,
)
from vfx_harness.orchestration.authority_selection_transaction import (
    authority_selection_lock,
    require_matching_authority_selection_token,
)
from vfx_harness.orchestration.authority_state_context import (
    resolve_current_authority_state,
)
from vfx_harness.orchestration.authority_state_store import (
    read_authority_state_bytes,
    read_authority_state_record,
)
from vfx_harness.orchestration.authority_unit_completion_sources import (
    prepare_transition_preserved_unit_sources,
    require_transition_preserved_unit_sources,
)
from vfx_harness.orchestration.unit_completion_authorizations import (
    CandidateAuthorizedUnitCompletionSet,
    completion_projection_digest,
)
from vfx_harness.orchestration.unit_state_serialization import (
    parse_work_unit_state_bytes,
)

if TYPE_CHECKING:
    from vfx_harness.orchestration.authority_selection import (
        ResolvedSelectedAuthority,
    )


def _selection_projection(value) -> AuthoritySelectionTokenProjection:
    return AuthoritySelectionTokenProjection(
        value.plan_revision,
        value.plan_pointer_sha256,
        value.jit_revision,
        value.jit_pointer_sha256,
    )


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(
                f"candidate authority preview contains duplicate JSON key {key!r}"
            )
        value[key] = item
    return value


def _reject_non_finite(value: str) -> None:
    raise ValueError(
        f"candidate authority preview contains non-finite number {value!r}"
    )


def _strict_json(payload: bytes, where: str) -> object:
    try:
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_non_finite,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{where} is not strict UTF-8 JSON: {exc}") from exc


def _snapshot_preview_documents(
    folder: Path,
) -> tuple[
    dict[str, object],
    dict[str, bytes],
    dict[str, str],
    tuple[TrustedFileBinding, ...],
]:
    documents: dict[str, object] = {}
    payloads: dict[str, bytes] = {}
    hashes: dict[str, str] = {}
    bindings: list[TrustedFileBinding] = []
    for name in OVERLAY_ARTIFACTS:
        snapshot = plan_bundle_integrity.read_real_file_snapshot(
            folder,
            folder / name,
            f"candidate authority preview {name}",
        )
        documents[name] = _strict_json(
            snapshot.payload,
            f"candidate authority preview {name}",
        )
        payloads[name] = snapshot.payload
        hashes[name] = hashlib.sha256(snapshot.payload).hexdigest()
        bindings.append(snapshot.binding)
    return documents, payloads, hashes, tuple(bindings)


def _read_preview_intent(
    shot: Path,
    reference: AuthorityPreviewReference,
) -> AuthorityStateTransitionIntent:
    value, _stored = read_authority_state_record(
        shot,
        locator=reference.transition_intent_ref.locator,
        sha256=reference.transition_intent_ref.sha256,
    )
    intent = AuthorityStateTransitionIntent.parse(
        value,
        "candidate authority preview transition intent",
    )
    if intent.digest != reference.transition_intent_ref.record_digest:
        raise ValueError(
            "candidate authority preview intent does not match its content-addressed reference"
        )
    return intent


def _state_documents(
    root: Path,
    shot: Path,
    intent: AuthorityStateTransitionIntent,
    *,
    side: str,
) -> tuple[
    dict[str, dict],
    dict[str, str],
    tuple[TrustedFileBinding, ...],
]:
    documents: dict[str, dict] = {}
    hashes: dict[str, str] = {}
    bindings: list[TrustedFileBinding] = []
    for member in intent.state_members:
        image = getattr(member, side)
        if image is None:
            continue
        snapshot = plan_bundle_integrity.read_real_file_snapshot(
            root,
            root / member.live_locator,
            f"candidate authority preview {side} state for layer {member.layer_id}",
        )
        observed_hash = hashlib.sha256(snapshot.payload).hexdigest()
        if observed_hash != image.sha256:
            raise ValueError(
                f"candidate authority preview {side} state hash differs for "
                f"layer {member.layer_id!r}"
            )
        stored = read_authority_state_bytes(
            shot,
            locator=image.locator,
            sha256=image.sha256,
        )
        if stored.payload != snapshot.payload:
            raise ValueError(
                f"candidate authority preview {side} state bytes differ from the "
                f"prepared image for layer {member.layer_id!r}"
            )
        decoded = parse_work_unit_state_bytes(
            snapshot.payload,
            f"candidate authority preview {side} state for layer {member.layer_id}",
        )
        documents[member.layer_id] = decoded
        hashes[member.layer_id] = observed_hash
        bindings.append(snapshot.binding)
    return documents, hashes, tuple(bindings)


def _snapshot_pointer_targets(
    folder: Path,
    *,
    pointer,
    documents: Mapping[str, object],
    payloads: Mapping[str, bytes],
) -> tuple[TrustedFileBinding, ...]:
    """Open every manifest target and prove it is the exact staged document."""

    require_live_jit_artifact_locators(pointer)
    require_materialized_layers_match(pointer, documents["layers.json"])
    bindings: list[TrustedFileBinding] = []
    for name in OVERLAY_ARTIFACTS:
        snapshot = plan_bundle_integrity.read_real_file_snapshot(
            folder,
            folder / pointer.artifacts[name],
            f"candidate authority preview JIT target {name}",
        )
        if (
            snapshot.sha256 != pointer.hashes[name]
            or snapshot.payload != payloads[name]
        ):
            raise ValueError(
                f"candidate authority preview JIT target differs for {name!r}"
            )
        bindings.append(snapshot.binding)
    return tuple(bindings)


def _reopen_transition_sources(
    shot: Path,
    intent: AuthorityStateTransitionIntent,
) -> None:
    """Reopen every content-addressed input named by the prepared transition."""

    references = {
        (reference.locator, reference.sha256): reference
        for reference in (*intent.staged_members, intent.proposal.producer_ref)
    }
    for reference in references.values():
        read_authority_state_bytes(
            shot,
            locator=reference.locator,
            sha256=reference.sha256,
        )


def _require_completion_receipt(
    state: Mapping[str, object],
    *,
    layer_id: str,
    unit_id: str,
    expected_digest: str,
    where: str,
) -> UnitCompletionReceipt:
    units = state.get("units")
    slot = units.get(unit_id) if isinstance(units, Mapping) else None
    if not isinstance(slot, Mapping) or slot.get("status") != "passed":
        raise ValueError(
            f"{where} does not retain passed state for preserved unit "
            f"{layer_id}.{unit_id}"
        )
    receipt = UnitCompletionReceipt.parse(
        slot.get("completion_receipt"),
        f"{where} preserved unit {layer_id}.{unit_id}",
    )
    if receipt.receipt_digest != expected_digest:
        raise ValueError(
            f"{where} completion receipt differs for preserved unit "
            f"{layer_id}.{unit_id}"
        )
    return receipt


def _require_finalization_receipt(
    state: Mapping[str, object],
    *,
    layer_id: str,
    expected_digest: str,
    where: str,
) -> LayerFinalizationReceipt:
    slot = state.get("layer_finalization")
    raw = slot.get("terminal_receipt") if isinstance(slot, Mapping) else None
    receipt = LayerFinalizationReceipt.parse(
        raw,
        f"{where} preserved layer {layer_id}",
    )
    if receipt.receipt_digest != expected_digest:
        raise ValueError(
            f"{where} finalization receipt differs for preserved layer {layer_id!r}"
        )
    return receipt


def _require_current_before_bindings(
    intent: AuthorityStateTransitionIntent,
    current,
    before_states: Mapping[str, Mapping[str, object]],
) -> None:
    """Verify effective bindings against live state and their selected base head."""

    current_images = {
        image.layer_id: image for image in current.commit.installed_states
    }
    before_members = {
        member.layer_id: member
        for member in intent.state_members
        if member.before is not None
    }
    if set(current_images) != set(before_members):
        raise ValueError(
            "candidate authority preview before-state namespace differs from the "
            "live coordinator"
        )
    for layer_id, member in before_members.items():
        before = member.before
        assert before is not None
        current_image = current_images[layer_id]
        prior = current_image.binding
        effective = before.binding
        state = before_states[layer_id]
        if (
            current_image.locator != member.live_locator
            or before.state_revision != state.get("revision")
            or effective.transition_revision != current.head.revision
            or effective.transition_proposal_digest
            != prior.transition_proposal_digest
            or effective.selection_token != current.head.selection_token
            or effective.layer_id != prior.layer_id
            or effective.layer_generation_digest
            != prior.layer_generation_digest
        ):
            raise ValueError(
                "candidate authority preview does not preserve the exact live "
                f"before binding for layer {layer_id!r}"
            )
        prior_units = {row.unit_id: row for row in prior.units}
        effective_units = {row.unit_id: row for row in effective.units}
        if set(prior_units) != set(effective_units):
            raise ValueError(
                "candidate authority preview before binding changes the selected "
                f"unit namespace for layer {layer_id!r}"
            )
        for unit_id, binding in effective_units.items():
            prior_unit = prior_units[unit_id]
            if (
                binding.unit_generation_digest
                != prior_unit.unit_generation_digest
            ):
                raise ValueError(
                    "candidate authority preview before binding changes the selected "
                    f"unit generation for {layer_id}.{unit_id}"
                )
            receipt_digest = binding.completion_receipt_digest
            if receipt_digest is None:
                if prior_unit.completion_receipt_digest is not None:
                    raise ValueError(
                        "candidate authority preview before binding drops a coordinator-"
                        f"authorized receipt for {layer_id}.{unit_id}"
                    )
                continue
            receipt = _require_completion_receipt(
                state,
                layer_id=layer_id,
                unit_id=unit_id,
                expected_digest=receipt_digest,
                where="candidate authority preview live predecessor",
            )
            if (
                receipt_digest != prior_unit.completion_receipt_digest
                and (
                    receipt.claim.selection_token != current.head.selection_token
                    or receipt.claim.plan_hash
                    != effective.layer_generation_digest
                )
            ):
                raise ValueError(
                    "candidate authority preview before binding adopts a receipt "
                    f"without current execution authority for {layer_id}.{unit_id}"
                )

        terminal_slot = state.get("layer_finalization")
        terminal_raw = (
            terminal_slot.get("terminal_receipt")
            if isinstance(terminal_slot, Mapping)
            else None
        )
        terminal_digest = effective.finalization_receipt_digest
        if terminal_digest is None:
            if prior.finalization_receipt_digest is not None:
                raise ValueError(
                    "candidate authority preview before binding drops a coordinator-"
                    f"authorized terminal receipt for layer {layer_id!r}"
                )
        else:
            terminal = LayerFinalizationReceipt.parse(
                terminal_raw,
                f"candidate authority preview live predecessor layer {layer_id}",
            )
            if terminal.receipt_digest != terminal_digest:
                raise ValueError(
                    "candidate authority preview before binding has the wrong terminal "
                    f"receipt for layer {layer_id!r}"
                )
            if (
                terminal_digest != prior.finalization_receipt_digest
                and (
                    terminal.claim.selection_token != current.head.selection_token
                    or terminal.claim.plan_hash
                    != effective.layer_generation_digest
                )
            ):
                raise ValueError(
                    "candidate authority preview before binding adopts a terminal "
                    f"receipt without current execution authority for layer {layer_id!r}"
                )

    effective_bindings = {
        layer_id: member.before.binding
        for layer_id, member in before_members.items()
        if member.before is not None
    }
    for layer_id, effective in effective_bindings.items():
        prior = current_images[layer_id].binding
        if {
            row.layer_id: row.layer_generation_digest
            for row in effective.predecessors
        } != {
            row.layer_id: row.layer_generation_digest
            for row in prior.predecessors
        }:
            raise ValueError(
                "candidate authority preview before binding changes selected "
                f"predecessor generations for layer {layer_id!r}"
            )
        expected_predecessors = {
            predecessor.layer_id: (
                predecessor.layer_generation_digest,
                predecessor.finalization_receipt_digest,
            )
            for predecessor in effective.predecessors
        }
        observed_predecessors = {
            predecessor_id: (
                effective_bindings[predecessor_id].layer_generation_digest,
                effective_bindings[predecessor_id].finalization_receipt_digest,
            )
            for predecessor_id in expected_predecessors
        }
        if expected_predecessors != observed_predecessors:
            raise ValueError(
                "candidate authority preview before binding does not close on exact "
                f"predecessor receipts for layer {layer_id!r}"
            )


def _require_after_capsule_bindings(
    intent: AuthorityStateTransitionIntent,
    capsule_set,
) -> None:
    layers = {row.layer_id: row for row in capsule_set.layers}
    units = {(row.layer_id, row.unit_id): row for row in capsule_set.units}
    for member in intent.state_members:
        after = member.after
        if after is None:
            continue
        capsule = layers.get(member.layer_id)
        if capsule is None or (
            after.binding.layer_generation_digest != capsule.capsule_digest
        ):
            raise ValueError(
                "candidate authority preview after binding has the wrong layer "
                f"capsule for {member.layer_id!r}"
            )
        expected_units = dict(capsule.unit_capsule_digests)
        observed_units = {
            row.unit_id: row.unit_generation_digest
            for row in after.binding.units
        }
        if observed_units != expected_units or any(
            units.get((member.layer_id, unit_id)) is None
            or units[(member.layer_id, unit_id)].capsule_digest != digest
            for unit_id, digest in observed_units.items()
        ):
            raise ValueError(
                "candidate authority preview after binding has stale unit capsules "
                f"for layer {member.layer_id!r}"
            )


@dataclass(frozen=True, slots=True)
class CandidatePreviewAuthorization:
    """Exact receipts one independently verified preview carries forward."""

    preview_reference: AuthorityPreviewReference
    unit_authorizations: tuple[CandidateAuthorizedUnitCompletionSet, ...]
    finalization_receipts: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.preview_reference, AuthorityPreviewReference):
            raise ValueError(
                "candidate preview authorization requires its typed reference"
            )
        if any(
            not isinstance(row, CandidateAuthorizedUnitCompletionSet)
            or row.preview_reference != self.preview_reference
            for row in self.unit_authorizations
        ):
            raise ValueError(
                "candidate preview unit authorizations must bind the same preview"
            )
        layer_ids = [row.layer_id for row in self.unit_authorizations]
        if layer_ids != sorted(layer_ids) or len(layer_ids) != len(set(layer_ids)):
            raise ValueError(
                "candidate preview unit authorizations must have unique sorted layers"
            )
        finalization_layers = [layer_id for layer_id, _digest in self.finalization_receipts]
        if (
            finalization_layers != sorted(finalization_layers)
            or len(finalization_layers) != len(set(finalization_layers))
        ):
            raise ValueError(
                "candidate preview finalization receipts must have unique sorted layers"
            )

    def unit_authorization(
        self,
        layer_id: str,
    ) -> CandidateAuthorizedUnitCompletionSet | None:
        layer_id = str(layer_id)
        return next(
            (row for row in self.unit_authorizations if row.layer_id == layer_id),
            None,
        )

    def finalization_receipt(self, layer_id: str) -> str | None:
        return dict(self.finalization_receipts).get(str(layer_id))


@dataclass(frozen=True, slots=True)
class _VerifiedCandidatePreview:
    authorization: CandidatePreviewAuthorization
    after_states: Mapping[str, Mapping[str, object]]


def _verify_candidate_preview(
    folder: Path,
    *,
    shot_folder: Path,
    selected_authority: ResolvedSelectedAuthority,
) -> _VerifiedCandidatePreview | None:
    """Resolve and independently verify one unpublished transition preview."""

    reference_path = folder / AUTHORITY_PREVIEW_REFERENCE_PATH
    if not reference_path.exists() and not reference_path.is_symlink():
        return None
    reference_snapshot = plan_bundle_integrity.read_real_file_snapshot(
        folder,
        reference_path,
        "candidate authority preview reference",
    )
    reference = AuthorityPreviewReference.from_bytes(reference_snapshot.payload)
    shot = shot_folder
    selected = selected_authority
    source_bindings: list[TrustedFileBinding] = [reference_snapshot.binding]

    with authority_selection_lock(shot, exclusive=False):
        heads = read_authority_selection_heads(shot)
        require_matching_authority_selection_token(
            selected.selection_token,
            heads.token,
        )
        before_selection = _selection_projection(heads.token)
        if reference.before_selection_token != before_selection:
            raise ValueError(
                "candidate authority preview does not name the exact live base selection"
            )
        current = resolve_current_authority_state(shot)
        assert current is not None
        if (
            current.head_ref != reference.predecessor_head_ref
            or current.head.selection_token != before_selection
        ):
            raise ValueError(
                "candidate authority preview predecessor is not the live coordinator head"
            )
        captured_before = capture_selected_authority_capsules(shot, selected)

        intent = _read_preview_intent(shot, reference)
        proposal = intent.proposal
        if (
            proposal.predecessor_head_ref != reference.predecessor_head_ref
            or proposal.predecessor_head_digest != current.head.digest
            or proposal.predecessor_head_revision != current.head.revision
            or proposal.transition_revision != current.head.revision + 1
            or proposal.before_selection_token != reference.before_selection_token
            or proposal.after_selection_token != reference.after_selection_token
            or proposal.effects_digest != reference.effects_digest
            or proposal.capsule_set_ref.record_digest
            != reference.capsule_set_digest
        ):
            raise ValueError(
                "candidate authority preview reference does not close on its prepared intent"
            )

        documents, document_payloads, artifact_hashes, document_bindings = (
            _snapshot_preview_documents(folder)
        )
        source_bindings.extend(document_bindings)
        captured = compile_proposed_authority_capsules(
            shot,
            selected,
            documents,
        )
        if (
            captured.capsule_set.capsule_set_digest
            != reference.capsule_set_digest
        ):
            raise ValueError(
                "candidate authority preview capsules differ from the proposed documents"
            )
        capsule_value, _stored_capsules = read_authority_state_record(
            shot,
            locator=proposal.capsule_set_ref.locator,
            sha256=proposal.capsule_set_ref.sha256,
        )
        if capsule_value != captured.capsule_set.as_dict():
            raise ValueError(
                "candidate authority preview capsule reference differs from the proposed documents"
            )

        pointer_snapshot = plan_bundle_integrity.read_real_file_snapshot(
            folder,
            folder / JIT_CURRENT_PATH,
            "candidate authority preview JIT pointer",
        )
        source_bindings.append(pointer_snapshot.binding)
        after_jit = proposal.jit_pointer.after
        if (
            after_jit is None
            or hashlib.sha256(pointer_snapshot.payload).hexdigest()
            != after_jit.sha256
        ):
            raise ValueError(
                "candidate authority preview JIT pointer differs from the prepared successor"
            )
        pointer = parse_jit_view_pointer(
            decode_canonical_json_object(
                pointer_snapshot.payload,
                "candidate authority preview JIT pointer",
            )
        )
        if (
            pointer.revision != reference.after_selection_token.jit_revision
            or pointer.plan_revision
            != reference.after_selection_token.plan_revision
            or pointer.hashes != artifact_hashes
            or pointer.view_hash != canonical_view_hash(documents)
            or selected.plan is None
            or pointer.bundle_hash != selected.plan.bundle.content_hash
        ):
            raise ValueError(
                "candidate authority preview JIT pointer does not bind the exact proposed view"
            )
        source_bindings.extend(
            _snapshot_pointer_targets(
                folder,
                pointer=pointer,
                documents=documents,
                payloads=document_payloads,
            )
        )

        before_states, before_hashes, before_bindings = _state_documents(
            shot,
            shot,
            intent,
            side="before",
        )
        after_states, after_hashes, after_bindings = _state_documents(
            folder,
            shot,
            intent,
            side="after",
        )
        source_bindings.extend((*before_bindings, *after_bindings))
        if (
            before_hashes != dict(reference.before_state_hashes)
            or after_hashes != dict(reference.after_state_hashes)
        ):
            raise ValueError(
                "candidate authority preview state hashes differ from its exact reference"
            )
        _require_current_before_bindings(intent, current, before_states)
        _require_after_capsule_bindings(intent, captured.capsule_set)
        verify_recompiled_authority_state_transition(
            shot,
            intent=intent,
            before_capsules=captured_before.capsule_set,
            after_capsules=captured.capsule_set,
            predecessor_images={
                image.layer_id: image for image in current.commit.installed_states
            },
            predecessor_head_revision=current.head.revision,
            before_selection_token=current.head.selection_token,
            before_states=before_states,
            after_states=after_states,
        )
        prepared_sources = prepare_transition_preserved_unit_sources(
            shot,
            capsules=captured.capsule_set,
            states=after_states,
            effects=proposal.effects,
        )
        prepared_finalization_sources = (
            prepare_transition_preserved_finalization_sources(
                shot,
                capsules=captured.capsule_set,
                states=after_states,
                effects=proposal.effects,
            )
        )
        unit_authorizations: list[CandidateAuthorizedUnitCompletionSet] = []
        finalizations: list[tuple[str, str]] = []
        for effect in proposal.effects:
            layer_id = effect.layer_id
            before_state = before_states.get(layer_id)
            after_state = after_states.get(layer_id)
            authorized: list[tuple[str, str]] = []
            for preserved in effect.preserved_units:
                receipt_digest = preserved.completion_receipt_digest
                if receipt_digest is None:
                    continue
                if before_state is None or after_state is None:
                    raise ValueError(
                        "candidate authority preview cannot preserve a receipt "
                        "without both state sides"
                    )
                _require_completion_receipt(
                    before_state,
                    layer_id=layer_id,
                    unit_id=preserved.unit_id,
                    expected_digest=receipt_digest,
                    where="candidate authority preview predecessor",
                )
                _require_completion_receipt(
                    after_state,
                    layer_id=layer_id,
                    unit_id=preserved.unit_id,
                    expected_digest=receipt_digest,
                    where="candidate authority preview successor",
                )
                authorized.append((preserved.unit_id, receipt_digest))
            if after_state is not None:
                unit_authorizations.append(
                    CandidateAuthorizedUnitCompletionSet(
                        layer_id=layer_id,
                        layer_generation_digest=str(after_state.get("plan_hash") or ""),
                        preview_reference=reference,
                        completion_projection_digest=completion_projection_digest(
                            after_state
                        ),
                        receipts=tuple(sorted(authorized)),
                    )
                )

            finalization_digest = effect.preserved_finalization_receipt_digest
            if finalization_digest is not None:
                if before_state is None or after_state is None:
                    raise ValueError(
                        "candidate authority preview cannot preserve a finalization "
                        "receipt without both state sides"
                    )
                _require_finalization_receipt(
                    before_state,
                    layer_id=layer_id,
                    expected_digest=finalization_digest,
                    where="candidate authority preview predecessor",
                )
                _require_finalization_receipt(
                    after_state,
                    layer_id=layer_id,
                    expected_digest=finalization_digest,
                    where="candidate authority preview successor",
                )
                finalizations.append((layer_id, finalization_digest))

        require_transition_preserved_unit_sources(
            prepared_sources,
            effects=proposal.effects,
        )
        require_transition_preserved_finalization_sources(
            prepared_finalization_sources,
            effects=proposal.effects,
            successor_state_sha256=after_hashes,
        )
        _reopen_transition_sources(shot, intent)
        if _read_preview_intent(shot, reference) != intent:
            raise ValueError(
                "candidate authority preview intent changed during verification"
            )
        captured_before.require_sources_unchanged()
        captured.require_sources_unchanged()
        for binding in source_bindings:
            require_trusted_file_unchanged(
                binding,
                "candidate authority preview source",
            )
        return _VerifiedCandidatePreview(
            authorization=CandidatePreviewAuthorization(
                preview_reference=reference,
                unit_authorizations=tuple(
                    sorted(unit_authorizations, key=lambda row: row.layer_id)
                ),
                finalization_receipts=tuple(sorted(finalizations)),
            ),
            after_states=after_states,
        )


def candidate_preview_authorization(
    folder: Path,
    *,
    shot_folder: Path,
    selected_authority: ResolvedSelectedAuthority,
) -> CandidatePreviewAuthorization | None:
    """Return every exact receipt authorized by a verified candidate transition."""

    verified = _verify_candidate_preview(
        folder,
        shot_folder=shot_folder,
        selected_authority=selected_authority,
    )
    return None if verified is None else verified.authorization


def candidate_preview_unit_authorization(
    folder: Path,
    *,
    shot_folder: Path,
    selected_authority: ResolvedSelectedAuthority,
    layer_id: str,
    state: Mapping[str, object],
) -> CandidateAuthorizedUnitCompletionSet | None:
    """Prove preserved unit receipts against the exact state the gate loaded."""

    verified = _verify_candidate_preview(
        folder,
        shot_folder=shot_folder,
        selected_authority=selected_authority,
    )
    if verified is None:
        return None
    layer_id = str(layer_id)
    if verified.after_states.get(layer_id) != dict(state):
        raise ValueError(
            f"candidate authority preview loaded state differs for layer {layer_id!r}"
        )
    authorization = verified.authorization.unit_authorization(layer_id)
    if authorization is None:
        raise ValueError(
            f"candidate authority preview has no typed unit authorization for layer "
            f"{layer_id!r}"
        )
    return authorization


__all__ = [
    "CandidatePreviewAuthorization",
    "candidate_preview_authorization",
    "candidate_preview_unit_authorization",
]
