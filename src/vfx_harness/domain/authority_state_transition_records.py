"""Pure proposal and intent records for authority/state transitions."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

from vfx_harness.domain.authority_head_records import AuthoritySelectionTokenProjection
from vfx_harness.domain.authority_state_record_primitives import (
    AUTHORITY_CAPSULE_SET_SCHEMA,
    AUTHORITY_STATE_TRANSITION_INTENT_SCHEMA,
    AUTHORITY_STATE_TRANSITION_PROPOSAL_SCHEMA,
    AuthorityPointerTransition,
    AuthorityStateLayerEffect,
    AuthorityStateMemberTransition,
    AuthorityStateRecordError,
    AuthorityStateRecordRef,
    AuthorityUnitBinding,
    LayerAuthorityBinding,
    _digest,
    _finish,
    _identifier,
    _optional_digest,
    _record_list,
    _require_pointer_matches_token,
    _require_sorted_records,
    _revision,
    _row,
    _SemanticRecord,
    _sorted_records,
    _timestamp,
    _token,
    authority_state_effects_digest,
)


@dataclass(frozen=True, slots=True)
class AuthorityStateTransitionProposal(_SemanticRecord):
    SCHEMA: ClassVar[str] = AUTHORITY_STATE_TRANSITION_PROPOSAL_SCHEMA
    DIGEST_FIELD: ClassVar[str] = "proposal_digest"
    AUDIT_FIELDS: ClassVar[frozenset[str]] = frozenset({"proposed_at"})

    transaction_id: str
    transition_revision: int
    predecessor_head_revision: int
    predecessor_head_ref: AuthorityStateRecordRef | None
    predecessor_head_digest: str | None
    before_selection_token: AuthoritySelectionTokenProjection
    after_selection_token: AuthoritySelectionTokenProjection
    plan_pointer: AuthorityPointerTransition
    jit_pointer: AuthorityPointerTransition
    producer_ref: AuthorityStateRecordRef
    capsule_set_ref: AuthorityStateRecordRef
    effects: tuple[AuthorityStateLayerEffect, ...]
    effects_digest: str
    proposed_at: str

    def __post_init__(self) -> None:
        _identifier(self.transaction_id, "authority-state proposal.transaction_id")
        revision = _revision(self.transition_revision, "authority-state proposal.transition_revision")
        predecessor = _revision(
            self.predecessor_head_revision,
            "authority-state proposal.predecessor_head_revision",
            allow_zero=True,
        )
        if revision != predecessor + 1:
            raise AuthorityStateRecordError(
                "authority-state proposal transition revision must immediately follow its predecessor"
            )
        predecessor_digest = _optional_digest(
            self.predecessor_head_digest,
            "authority-state proposal.predecessor_head_digest",
        )
        if (predecessor == 0) is not (
            predecessor_digest is None and self.predecessor_head_ref is None
        ):
            raise AuthorityStateRecordError(
                "authority-state proposal predecessor reference and digest must both "
                "be null exactly at genesis"
            )
        if self.predecessor_head_ref is not None:
            if (
                self.predecessor_head_ref.record_schema
                != "vfx-harness.authority-state-head/v1"
            ):
                raise AuthorityStateRecordError(
                    "authority-state proposal predecessor must reference a coordinator head"
                )
            if self.predecessor_head_ref.record_digest != predecessor_digest:
                raise AuthorityStateRecordError(
                    "authority-state proposal predecessor reference digest disagrees"
                )
        before = _token(
            self.before_selection_token,
            "authority-state proposal.before_selection_token",
        )
        after = _token(
            self.after_selection_token,
            "authority-state proposal.after_selection_token",
        )
        if before == after:
            raise AuthorityStateRecordError("authority-state proposal must not encode a semantic no-op selection")
        if not isinstance(self.plan_pointer, AuthorityPointerTransition) or self.plan_pointer.pointer_kind != "plan":
            raise AuthorityStateRecordError("authority-state proposal.plan_pointer must be a plan transition")
        if not isinstance(self.jit_pointer, AuthorityPointerTransition) or self.jit_pointer.pointer_kind != "jit":
            raise AuthorityStateRecordError("authority-state proposal.jit_pointer must be a JIT transition")
        _require_pointer_matches_token(self.plan_pointer, before=before, after=after)
        _require_pointer_matches_token(self.jit_pointer, before=before, after=after)
        if not isinstance(self.producer_ref, AuthorityStateRecordRef):
            raise AuthorityStateRecordError("authority-state proposal.producer_ref is invalid")
        if not isinstance(self.capsule_set_ref, AuthorityStateRecordRef):
            raise AuthorityStateRecordError("authority-state proposal.capsule_set_ref is invalid")
        if self.capsule_set_ref.record_schema != AUTHORITY_CAPSULE_SET_SCHEMA:
            raise AuthorityStateRecordError("authority-state proposal capsule_set_ref must name a v1 capsule set")
        _require_sorted_records(self.effects, "authority-state proposal.effects", key="layer_id")
        expected_effects = authority_state_effects_digest(self.effects)
        if _digest(self.effects_digest, "authority-state proposal.effects_digest") != expected_effects:
            raise AuthorityStateRecordError("authority-state proposal.effects_digest does not match its effects")
        _timestamp(self.proposed_at, "authority-state proposal.proposed_at")
        if after.plan_revision == 0 and after.jit_revision == 0:
            raise AuthorityStateRecordError("authority-state proposal successor selection cannot be absent")

    @classmethod
    def mint(
        cls,
        *,
        transaction_id: str,
        transition_revision: int,
        predecessor_head_revision: int,
        predecessor_head_ref: AuthorityStateRecordRef | None,
        predecessor_head_digest: str | None,
        before_selection_token: AuthoritySelectionTokenProjection | Mapping[str, Any],
        after_selection_token: AuthoritySelectionTokenProjection | Mapping[str, Any],
        plan_pointer: AuthorityPointerTransition,
        jit_pointer: AuthorityPointerTransition,
        producer_ref: AuthorityStateRecordRef,
        capsule_set_ref: AuthorityStateRecordRef,
        effects: Iterable[AuthorityStateLayerEffect],
        proposed_at: str,
    ) -> AuthorityStateTransitionProposal:
        effect_rows = _sorted_records(effects, "authority-state proposal.effects", key="layer_id")
        return cls(
            transaction_id,
            transition_revision,
            predecessor_head_revision,
            predecessor_head_ref,
            predecessor_head_digest,
            _token(before_selection_token, "authority-state proposal.before_selection_token"),
            _token(after_selection_token, "authority-state proposal.after_selection_token"),
            plan_pointer,
            jit_pointer,
            producer_ref,
            capsule_set_ref,
            effect_rows,
            authority_state_effects_digest(effect_rows),
            proposed_at,
        )

    @classmethod
    def parse(
        cls, value: object, where: str = "authority-state transition proposal"
    ) -> AuthorityStateTransitionProposal:
        row = _row(cls, value, where)
        effects = tuple(
            AuthorityStateLayerEffect.parse(item, f"{where}.effects[{index}]")
            for index, item in enumerate(_record_list(row["effects"], f"{where}.effects"))
        )
        return _finish(
            cls(
                row["transaction_id"],
                row["transition_revision"],
                row["predecessor_head_revision"],
                None
                if row["predecessor_head_ref"] is None
                else AuthorityStateRecordRef.parse(
                    row["predecessor_head_ref"],
                    f"{where}.predecessor_head_ref",
                ),
                row["predecessor_head_digest"],
                _token(row["before_selection_token"], f"{where}.before_selection_token"),
                _token(row["after_selection_token"], f"{where}.after_selection_token"),
                AuthorityPointerTransition.parse(row["plan_pointer"], f"{where}.plan_pointer"),
                AuthorityPointerTransition.parse(row["jit_pointer"], f"{where}.jit_pointer"),
                AuthorityStateRecordRef.parse(row["producer_ref"], f"{where}.producer_ref"),
                AuthorityStateRecordRef.parse(row["capsule_set_ref"], f"{where}.capsule_set_ref"),
                effects,
                row["effects_digest"],
                row["proposed_at"],
            ),
            row,
            where,
        )


def _effect_by_layer(proposal: AuthorityStateTransitionProposal) -> dict[str, AuthorityStateLayerEffect]:
    return {item.layer_id: item for item in proposal.effects}


def _units_by_id(binding: LayerAuthorityBinding) -> dict[str, AuthorityUnitBinding]:
    return {item.unit_id: item for item in binding.units}


def _validate_member_effect(
    member: AuthorityStateMemberTransition,
    effect: AuthorityStateLayerEffect,
    proposal: AuthorityStateTransitionProposal,
) -> None:
    before = member.before
    after = member.after
    if effect.effect_kind == "added" and (before is not None or after is None):
        raise AuthorityStateRecordError(f"added layer {member.layer_id!r} must have only an after state")
    if effect.effect_kind == "removed" and (before is None or after is not None):
        raise AuthorityStateRecordError(f"removed layer {member.layer_id!r} must have only a before state")
    if effect.effect_kind not in {"added", "removed"} and (before is None or after is None):
        raise AuthorityStateRecordError(
            f"effect {effect.effect_kind!r} for layer {member.layer_id!r} requires both state sides"
        )
    if before is not None:
        binding = before.binding
        if binding.transition_revision != proposal.predecessor_head_revision:
            raise AuthorityStateRecordError(
                f"before binding for layer {member.layer_id!r} is not from the immediate predecessor"
            )
        if binding.selection_token != proposal.before_selection_token:
            raise AuthorityStateRecordError(
                f"before binding for layer {member.layer_id!r} has the wrong selection token"
            )
        before_units = _units_by_id(binding)
        for preserved in effect.preserved_units:
            if before_units.get(preserved.unit_id) != preserved:
                raise AuthorityStateRecordError(
                    f"preserved unit {preserved.unit_id!r} is not exact in the "
                    "immediate-predecessor binding"
                )
    if after is not None:
        binding = after.binding
        if binding.transition_revision != proposal.transition_revision:
            raise AuthorityStateRecordError(
                f"after binding for layer {member.layer_id!r} has the wrong transition revision"
            )
        if binding.transition_proposal_digest != proposal.digest:
            raise AuthorityStateRecordError(f"after binding for layer {member.layer_id!r} does not bind this proposal")
        if binding.selection_token != proposal.after_selection_token:
            raise AuthorityStateRecordError(
                f"after binding for layer {member.layer_id!r} has the wrong selection token"
            )
        after_units = _units_by_id(binding)
        for preserved in effect.preserved_units:
            if after_units.get(preserved.unit_id) != preserved:
                raise AuthorityStateRecordError(
                    f"preserved unit {preserved.unit_id!r} is not exact in the after binding"
                )
        for unit_id in effect.invalidated_unit_ids:
            if unit_id in after_units and after_units[unit_id].completion_receipt_digest is not None:
                raise AuthorityStateRecordError(f"invalidated unit {unit_id!r} remains completion-authorized")
        if binding.finalization_receipt_digest != effect.preserved_finalization_receipt_digest:
            raise AuthorityStateRecordError(
                f"after binding finalization for layer {member.layer_id!r} disagrees with its effect"
            )
    if before is not None and after is not None:
        same_generation = before.binding.layer_generation_digest == after.binding.layer_generation_digest
        if effect.effect_kind == "unchanged" and not same_generation:
            raise AuthorityStateRecordError(f"unchanged layer {member.layer_id!r} changed generation digest")
        # An incomparable layer crosses digest generations: equal digest strings are
        # coincidence, not identity, so only a comparable `changed` layer must differ.
        if effect.effect_kind == "changed" and same_generation:
            raise AuthorityStateRecordError(
                f"changed layer {member.layer_id!r} retained its generation digest"
            )


@dataclass(frozen=True, slots=True)
class AuthorityStateTransitionIntent(_SemanticRecord):
    SCHEMA: ClassVar[str] = AUTHORITY_STATE_TRANSITION_INTENT_SCHEMA
    DIGEST_FIELD: ClassVar[str] = "intent_digest"
    AUDIT_FIELDS: ClassVar[frozenset[str]] = frozenset({"prepared_at"})

    proposal_ref: AuthorityStateRecordRef
    proposal: AuthorityStateTransitionProposal
    state_members: tuple[AuthorityStateMemberTransition, ...]
    staged_members: tuple[AuthorityStateRecordRef, ...]
    prepared_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.proposal_ref, AuthorityStateRecordRef):
            raise AuthorityStateRecordError("authority-state intent.proposal_ref is invalid")
        if not isinstance(self.proposal, AuthorityStateTransitionProposal):
            raise AuthorityStateRecordError("authority-state intent.proposal is invalid")
        if (
            self.proposal_ref.record_schema != self.proposal.SCHEMA
            or self.proposal_ref.record_digest != self.proposal.digest
        ):
            raise AuthorityStateRecordError("authority-state intent proposal reference does not close on its proposal")
        _require_sorted_records(
            self.state_members,
            "authority-state intent.state_members",
            key="layer_id",
        )
        _require_sorted_records(
            self.staged_members,
            "authority-state intent.staged_members",
            key="locator",
        )
        if self.proposal_ref not in self.staged_members:
            raise AuthorityStateRecordError("authority-state intent staged members must include the proposal")
        effects = _effect_by_layer(self.proposal)
        members = {item.layer_id: item for item in self.state_members}
        if set(effects) != set(members):
            raise AuthorityStateRecordError(
                "authority-state intent state-member layers must exactly match effect layers"
            )
        staged = {(item.locator, item.sha256) for item in self.staged_members}
        for layer_id, member in members.items():
            _validate_member_effect(member, effects[layer_id], self.proposal)
            if member.after is not None and (member.after.locator, member.after.sha256) not in staged:
                raise AuthorityStateRecordError(
                    f"authority-state intent omits staged after-state bytes for layer {layer_id!r}"
                )
        for transition in (self.proposal.plan_pointer, self.proposal.jit_pointer):
            if (
                transition.after is not None
                and (
                    transition.before is None
                    or transition.before.revision != transition.after.revision
                    or transition.before.sha256 != transition.after.sha256
                )
                and (transition.after.locator, transition.after.sha256) not in staged
            ):
                raise AuthorityStateRecordError(
                    f"authority-state intent omits staged {transition.pointer_kind} pointer bytes"
                )
        _timestamp(self.prepared_at, "authority-state intent.prepared_at")

    @property
    def transaction_id(self) -> str:
        return self.proposal.transaction_id

    @property
    def transition_revision(self) -> int:
        return self.proposal.transition_revision

    @classmethod
    def mint(
        cls,
        *,
        proposal_ref: AuthorityStateRecordRef,
        proposal: AuthorityStateTransitionProposal,
        state_members: Iterable[AuthorityStateMemberTransition],
        staged_members: Iterable[AuthorityStateRecordRef],
        prepared_at: str,
    ) -> AuthorityStateTransitionIntent:
        return cls(
            proposal_ref,
            proposal,
            _sorted_records(state_members, "authority-state intent.state_members", key="layer_id"),
            _sorted_records(staged_members, "authority-state intent.staged_members", key="locator"),
            prepared_at,
        )

    @classmethod
    def parse(cls, value: object, where: str = "authority-state transition intent") -> AuthorityStateTransitionIntent:
        row = _row(cls, value, where)
        state_members = tuple(
            AuthorityStateMemberTransition.parse(item, f"{where}.state_members[{index}]")
            for index, item in enumerate(_record_list(row["state_members"], f"{where}.state_members"))
        )
        staged_members = tuple(
            AuthorityStateRecordRef.parse(item, f"{where}.staged_members[{index}]")
            for index, item in enumerate(_record_list(row["staged_members"], f"{where}.staged_members"))
        )
        return _finish(
            cls(
                AuthorityStateRecordRef.parse(row["proposal_ref"], f"{where}.proposal_ref"),
                AuthorityStateTransitionProposal.parse(row["proposal"], f"{where}.proposal"),
                state_members,
                staged_members,
                row["prepared_at"],
            ),
            row,
            where,
        )


__all__ = [
    "AuthorityStateTransitionIntent",
    "AuthorityStateTransitionProposal",
]
