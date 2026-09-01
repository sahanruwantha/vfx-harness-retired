"""Verified current HIR-0171 authority-state coordinator context."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol, TypeVar

from vfx_harness.domain.authority_state_records import (
    AuthorityStateCoordinatorHead,
    AuthorityStatePendingPointer,
    AuthorityStateRecordError,
    AuthorityStateRecordRef,
    AuthorityStateTransitionCommit,
    AuthorityStateTransitionEvaluation,
    AuthorityStateTransitionIntent,
    AuthorityStateTransitionProposal,
)
from vfx_harness.orchestration.authority_selection_heads import (
    read_authority_selection_heads,
)
from vfx_harness.orchestration.authority_state_store import (
    AuthorityStateStoreError,
    authority_state_object_locator,
    decode_pointer_bytes,
    install_authority_state_record,
    read_authority_state_bytes,
    read_authority_state_record,
    read_current_bytes,
    read_pending_bytes,
)


class AuthorityStateContextError(ValueError):
    """Coordinator records do not close on one evaluated selected generation."""


class _AuthorityStateRecord(Protocol):
    SCHEMA: str

    @property
    def digest(self) -> str: ...

    def as_dict(self) -> dict: ...


_RecordT = TypeVar("_RecordT", bound=_AuthorityStateRecord)


@dataclass(frozen=True, slots=True)
class ResolvedAuthorityStateContext:
    """Complete immutable record chain selected by current.json."""

    head_ref: AuthorityStateRecordRef
    head: AuthorityStateCoordinatorHead
    evaluation: AuthorityStateTransitionEvaluation
    commit: AuthorityStateTransitionCommit
    intent: AuthorityStateTransitionIntent
    proposal: AuthorityStateTransitionProposal


def _require_context_closure(
    *,
    head_ref: AuthorityStateRecordRef,
    head: AuthorityStateCoordinatorHead,
    evaluation: AuthorityStateTransitionEvaluation,
    commit: AuthorityStateTransitionCommit,
    intent: AuthorityStateTransitionIntent,
    proposal: AuthorityStateTransitionProposal,
) -> None:
    intended_states = tuple(
        replace(member.after, locator=member.live_locator)
        for member in intent.state_members
        if member.after is not None
    )
    if (
        evaluation.commit_ref != head.commit_ref
        or evaluation.intent_ref != commit.intent_ref
        or commit.intent_ref.record_digest != intent.digest
        or intent.proposal_ref.record_digest != proposal.digest
        or intent.proposal != proposal
        or evaluation.result != "satisfied"
        or evaluation.transaction_id != proposal.transaction_id
        or commit.transaction_id != proposal.transaction_id
        or intent.transaction_id != proposal.transaction_id
        or evaluation.transition_revision != head.revision
        or commit.transition_revision != head.revision
        or intent.transition_revision != head.revision
        or commit.successor_head_revision != head.revision
        or commit.predecessor_head_revision != head.revision - 1
        or proposal.predecessor_head_revision != head.revision - 1
        or commit.predecessor_head_ref != head.predecessor_head_ref
        or commit.predecessor_head_digest != head.predecessor_head_digest
        or proposal.predecessor_head_ref != head.predecessor_head_ref
        or proposal.predecessor_head_digest != head.predecessor_head_digest
    ):
        raise AuthorityStateContextError(
            "authority-state head, evaluation, commit, intent, and proposal do not "
            "form one exact transition"
        )
    if (
        proposal.transition_revision != head.revision
        or proposal.after_selection_token != head.selection_token
        or commit.observed_selection_token != head.selection_token
        or evaluation.observed_selection_token != head.selection_token
        or commit.effects_digest != proposal.effects_digest
        or evaluation.effects_digest != proposal.effects_digest
        or commit.installed_states != intended_states
        or evaluation.installed_states != commit.installed_states
    ):
        raise AuthorityStateContextError(
            "authority-state transition evidence does not close on the selected head"
        )
    if head_ref.record_digest != head.digest:
        raise AuthorityStateContextError(
            "authority-state head reference does not match the resolved head"
        )


def resolve_authority_state_context_from_ref(
    shot_folder: str | Path,
    head_ref: AuthorityStateRecordRef,
) -> ResolvedAuthorityStateContext:
    """Resolve one immutable coordinator head and its complete evidence closure."""

    head = _load_record(
        shot_folder,
        head_ref,
        AuthorityStateCoordinatorHead,
        "authority-state coordinator head",
    )
    commit = _load_record(
        shot_folder,
        head.commit_ref,
        AuthorityStateTransitionCommit,
        "authority-state commit",
    )
    evaluation = _load_record(
        shot_folder,
        head.evaluation_ref,
        AuthorityStateTransitionEvaluation,
        "authority-state evaluation",
    )
    intent = _load_record(
        shot_folder,
        commit.intent_ref,
        AuthorityStateTransitionIntent,
        "authority-state intent",
    )
    proposal = _load_record(
        shot_folder,
        intent.proposal_ref,
        AuthorityStateTransitionProposal,
        "authority-state proposal",
    )
    _require_context_closure(
        head_ref=head_ref,
        head=head,
        evaluation=evaluation,
        commit=commit,
        intent=intent,
        proposal=proposal,
    )
    return ResolvedAuthorityStateContext(
        head_ref=head_ref,
        head=head,
        evaluation=evaluation,
        commit=commit,
        intent=intent,
        proposal=proposal,
    )


def install_record_ref(
    shot_folder: str | Path,
    record: _AuthorityStateRecord,
) -> AuthorityStateRecordRef:
    """Install one typed record and return its exact immutable reference."""

    try:
        stored = install_authority_state_record(shot_folder, record.as_dict())
        return AuthorityStateRecordRef.mint(
            locator=stored.locator,
            sha256=stored.sha256,
            record_schema=record.SCHEMA,
            record_digest=record.digest,
        )
    except (AuthorityStateRecordError, AuthorityStateStoreError, TypeError, ValueError) as exc:
        raise AuthorityStateContextError(str(exc)) from exc


def _load_record(
    shot_folder: str | Path,
    reference: AuthorityStateRecordRef,
    record_type: type[_RecordT],
    where: str,
) -> _RecordT:
    if reference.record_schema != record_type.SCHEMA:
        raise AuthorityStateContextError(
            f"{where} reference schema {reference.record_schema!r} does not name "
            f"{record_type.SCHEMA!r}"
        )
    try:
        value, _stored = read_authority_state_record(
            shot_folder,
            locator=reference.locator,
            sha256=reference.sha256,
        )
        record = record_type.parse(value, where)
    except (AuthorityStateRecordError, AuthorityStateStoreError, TypeError, ValueError) as exc:
        raise AuthorityStateContextError(str(exc)) from exc
    if record.digest != reference.record_digest:
        raise AuthorityStateContextError(
            f"{where} semantic digest does not match its immutable reference"
        )
    return record


def read_pending_authority_state(
    shot_folder: str | Path,
) -> AuthorityStatePendingPointer | None:
    """Parse the selected WAL pointer without treating it as committed authority."""

    payload = read_pending_bytes(shot_folder)
    if payload is None:
        return None
    try:
        return AuthorityStatePendingPointer.parse(
            decode_pointer_bytes(payload, "authority-state pending pointer")
        )
    except (AuthorityStateRecordError, AuthorityStateStoreError, ValueError) as exc:
        raise AuthorityStateContextError(str(exc)) from exc


def resolve_current_authority_state(
    shot_folder: str | Path,
    *,
    allow_absent: bool = False,
    verify_live_selection: bool = True,
) -> ResolvedAuthorityStateContext | None:
    """Resolve and independently close the current coordinator record chain.

    Ordinary callers must use this only while the authority-selection lock is held.
    A present WAL is rejected by ``read_authority_selection_heads`` before any current
    record can grant authority.
    """

    payload = read_current_bytes(shot_folder)
    if payload is None:
        if allow_absent:
            return None
        raise AuthorityStateContextError(
            "selected authority has no evaluated authority-state coordinator head"
        )
    try:
        head = AuthorityStateCoordinatorHead.parse(
            decode_pointer_bytes(payload, "authority-state current head")
        )
        head_sha256 = hashlib.sha256(payload).hexdigest()
        head_locator = authority_state_object_locator(head_sha256)
        stored_head = read_authority_state_bytes(
            shot_folder,
            locator=head_locator,
            sha256=head_sha256,
        )
        if stored_head.payload != payload:
            raise AuthorityStateContextError(
                "selected authority-state head differs from its immutable object"
            )
        head_ref = AuthorityStateRecordRef.mint(
            locator=head_locator,
            sha256=head_sha256,
            record_schema=head.SCHEMA,
            record_digest=head.digest,
        )
        context = resolve_authority_state_context_from_ref(
            shot_folder,
            head_ref,
        )
    except (AuthorityStateRecordError, AuthorityStateStoreError, ValueError) as exc:
        raise AuthorityStateContextError(str(exc)) from exc
    if context.head != head:
        raise AuthorityStateContextError(
            "authority-state current head differs from its immutable resolved record"
        )
    if verify_live_selection:
        try:
            observed = read_authority_selection_heads(shot_folder).token
        except ValueError as exc:
            raise AuthorityStateContextError(str(exc)) from exc
        if context.head.selection_token != type(context.head.selection_token)(
            observed.plan_revision,
            observed.plan_pointer_sha256,
            observed.jit_revision,
            observed.jit_pointer_sha256,
        ):
            raise AuthorityStateContextError(
                "authority-state current head does not authorize the live plan/JIT selection"
            )
    return context


__all__ = [
    "AuthorityStateContextError",
    "ResolvedAuthorityStateContext",
    "install_record_ref",
    "read_pending_authority_state",
    "resolve_authority_state_context_from_ref",
    "resolve_current_authority_state",
]
