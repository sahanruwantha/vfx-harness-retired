"""Pure commit, evaluation, and coordinator records for authority/state transitions."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

from vfx_harness.domain.authority_head_records import AuthoritySelectionTokenProjection
from vfx_harness.domain.authority_state_record_primitives import (
    AUTHORITY_STATE_HEAD_SCHEMA,
    AUTHORITY_STATE_PENDING_POINTER_SCHEMA,
    AUTHORITY_STATE_TRANSITION_COMMIT_SCHEMA,
    AUTHORITY_STATE_TRANSITION_EVALUATION_SCHEMA,
    AUTHORITY_STATE_TRANSITION_INTENT_SCHEMA,
    EVALUATION_RESULTS,
    AuthorityStateMemberImage,
    AuthorityStateRecordError,
    AuthorityStateRecordRef,
    _digest,
    _finish,
    _identifier,
    _optional_digest,
    _record_list,
    _require_sorted_records,
    _revision,
    _row,
    _SemanticRecord,
    _sorted_records,
    _text,
    _timestamp,
    _token,
)
from vfx_harness.domain.authority_state_transition_records import (
    AuthorityStateTransitionIntent,
)


@dataclass(frozen=True, slots=True)
class AuthorityStatePendingPointer(_SemanticRecord):
    SCHEMA: ClassVar[str] = AUTHORITY_STATE_PENDING_POINTER_SCHEMA
    DIGEST_FIELD: ClassVar[str] = "pending_digest"
    AUDIT_FIELDS: ClassVar[frozenset[str]] = frozenset({"selected_at"})

    transaction_id: str
    transition_revision: int
    intent_ref: AuthorityStateRecordRef
    selected_at: str

    def __post_init__(self) -> None:
        _identifier(self.transaction_id, "authority-state pending.transaction_id")
        _revision(self.transition_revision, "authority-state pending.transition_revision")
        if (
            not isinstance(self.intent_ref, AuthorityStateRecordRef)
            or self.intent_ref.record_schema != AUTHORITY_STATE_TRANSITION_INTENT_SCHEMA
        ):
            raise AuthorityStateRecordError("authority-state pending must reference a transition intent")
        _timestamp(self.selected_at, "authority-state pending.selected_at")

    @classmethod
    def mint(
        cls,
        *,
        intent_ref: AuthorityStateRecordRef,
        intent: AuthorityStateTransitionIntent,
        selected_at: str,
    ) -> AuthorityStatePendingPointer:
        if intent_ref.record_digest != intent.digest:
            raise AuthorityStateRecordError("authority-state pending intent reference is stale")
        return cls(intent.transaction_id, intent.transition_revision, intent_ref, selected_at)

    @classmethod
    def parse(cls, value: object, where: str = "authority-state pending pointer") -> AuthorityStatePendingPointer:
        row = _row(cls, value, where)
        return _finish(
            cls(
                row["transaction_id"],
                row["transition_revision"],
                AuthorityStateRecordRef.parse(row["intent_ref"], f"{where}.intent_ref"),
                row["selected_at"],
            ),
            row,
            where,
        )


def _installed_states(values: Iterable[AuthorityStateMemberImage], where: str) -> tuple[AuthorityStateMemberImage, ...]:
    return _sorted_records(values, where, key="layer_id")


def _require_installed_match_intent(
    installed: tuple[AuthorityStateMemberImage, ...],
    intent: AuthorityStateTransitionIntent,
) -> None:
    expected: dict[str, tuple[str, AuthorityStateMemberImage]] = {
        item.layer_id: (item.live_locator, item.after) for item in intent.state_members if item.after is not None
    }
    if {item.layer_id for item in installed} != set(expected):
        raise AuthorityStateRecordError("installed state layers do not match the intent's after-state set")
    for item in installed:
        live_locator, after = expected[item.layer_id]
        assert after is not None
        if (
            item.locator != live_locator
            or item.sha256 != after.sha256
            or item.state_revision != after.state_revision
            or item.binding != after.binding
        ):
            raise AuthorityStateRecordError(
                f"installed state for layer {item.layer_id!r} is not the exact intended after state"
            )


@dataclass(frozen=True, slots=True)
class AuthorityStateTransitionCommit(_SemanticRecord):
    SCHEMA: ClassVar[str] = AUTHORITY_STATE_TRANSITION_COMMIT_SCHEMA
    DIGEST_FIELD: ClassVar[str] = "commit_digest"
    AUDIT_FIELDS: ClassVar[frozenset[str]] = frozenset({"committed_at"})

    transaction_id: str
    transition_revision: int
    intent_ref: AuthorityStateRecordRef
    predecessor_head_revision: int
    predecessor_head_ref: AuthorityStateRecordRef | None
    predecessor_head_digest: str | None
    successor_head_revision: int
    observed_selection_token: AuthoritySelectionTokenProjection
    installed_states: tuple[AuthorityStateMemberImage, ...]
    effects_digest: str
    committed_at: str

    def __post_init__(self) -> None:
        _identifier(self.transaction_id, "authority-state commit.transaction_id")
        revision = _revision(self.transition_revision, "authority-state commit.transition_revision")
        predecessor = _revision(
            self.predecessor_head_revision,
            "authority-state commit.predecessor_head_revision",
            allow_zero=True,
        )
        if revision != predecessor + 1 or self.successor_head_revision != revision:
            raise AuthorityStateRecordError("authority-state commit head revisions must be one monotone transition")
        predecessor_digest = _optional_digest(
            self.predecessor_head_digest,
            "authority-state commit.predecessor_head_digest",
        )
        if (predecessor == 0) is not (
            predecessor_digest is None and self.predecessor_head_ref is None
        ):
            raise AuthorityStateRecordError(
                "authority-state commit predecessor reference and digest must both "
                "be null exactly at genesis"
            )
        if self.predecessor_head_ref is not None:
            if self.predecessor_head_ref.record_schema != AUTHORITY_STATE_HEAD_SCHEMA:
                raise AuthorityStateRecordError(
                    "authority-state commit predecessor must reference a coordinator head"
                )
            if self.predecessor_head_ref.record_digest != predecessor_digest:
                raise AuthorityStateRecordError(
                    "authority-state commit predecessor reference digest disagrees"
                )
        if (
            not isinstance(self.intent_ref, AuthorityStateRecordRef)
            or self.intent_ref.record_schema != AUTHORITY_STATE_TRANSITION_INTENT_SCHEMA
        ):
            raise AuthorityStateRecordError("authority-state commit must reference a transition intent")
        _token(self.observed_selection_token, "authority-state commit.observed_selection_token")
        _require_sorted_records(
            self.installed_states,
            "authority-state commit.installed_states",
            key="layer_id",
        )
        _digest(self.effects_digest, "authority-state commit.effects_digest")
        _timestamp(self.committed_at, "authority-state commit.committed_at")

    @classmethod
    def mint(
        cls,
        *,
        intent_ref: AuthorityStateRecordRef,
        intent: AuthorityStateTransitionIntent,
        observed_selection_token: AuthoritySelectionTokenProjection | Mapping[str, Any],
        installed_states: Iterable[AuthorityStateMemberImage],
        committed_at: str,
    ) -> AuthorityStateTransitionCommit:
        if intent_ref.record_digest != intent.digest:
            raise AuthorityStateRecordError("authority-state commit intent reference is stale")
        observed = _token(observed_selection_token, "authority-state commit.observed_selection_token")
        if observed != intent.proposal.after_selection_token:
            raise AuthorityStateRecordError("authority-state commit observed selection is not the proposed successor")
        installed = _installed_states(installed_states, "authority-state commit.installed_states")
        _require_installed_match_intent(installed, intent)
        proposal = intent.proposal
        return cls(
            proposal.transaction_id,
            proposal.transition_revision,
            intent_ref,
            proposal.predecessor_head_revision,
            proposal.predecessor_head_ref,
            proposal.predecessor_head_digest,
            proposal.transition_revision,
            observed,
            installed,
            proposal.effects_digest,
            committed_at,
        )

    @classmethod
    def parse(cls, value: object, where: str = "authority-state transition commit") -> AuthorityStateTransitionCommit:
        row = _row(cls, value, where)
        installed = tuple(
            AuthorityStateMemberImage.parse(item, f"{where}.installed_states[{index}]")
            for index, item in enumerate(_record_list(row["installed_states"], f"{where}.installed_states"))
        )
        return _finish(
            cls(
                row["transaction_id"],
                row["transition_revision"],
                AuthorityStateRecordRef.parse(row["intent_ref"], f"{where}.intent_ref"),
                row["predecessor_head_revision"],
                None
                if row["predecessor_head_ref"] is None
                else AuthorityStateRecordRef.parse(
                    row["predecessor_head_ref"],
                    f"{where}.predecessor_head_ref",
                ),
                row["predecessor_head_digest"],
                row["successor_head_revision"],
                _token(row["observed_selection_token"], f"{where}.observed_selection_token"),
                installed,
                row["effects_digest"],
                row["committed_at"],
            ),
            row,
            where,
        )


@dataclass(frozen=True, slots=True)
class AuthorityStateTransitionEvaluation(_SemanticRecord):
    SCHEMA: ClassVar[str] = AUTHORITY_STATE_TRANSITION_EVALUATION_SCHEMA
    DIGEST_FIELD: ClassVar[str] = "evaluation_digest"
    AUDIT_FIELDS: ClassVar[frozenset[str]] = frozenset({"evaluated_at"})

    transaction_id: str
    transition_revision: int
    intent_ref: AuthorityStateRecordRef
    commit_ref: AuthorityStateRecordRef
    observed_selection_token: AuthoritySelectionTokenProjection
    installed_states: tuple[AuthorityStateMemberImage, ...]
    effects_digest: str
    result: str
    findings: tuple[str, ...]
    evaluated_at: str

    def __post_init__(self) -> None:
        _identifier(self.transaction_id, "authority-state evaluation.transaction_id")
        _revision(self.transition_revision, "authority-state evaluation.transition_revision")
        if (
            not isinstance(self.intent_ref, AuthorityStateRecordRef)
            or self.intent_ref.record_schema != AUTHORITY_STATE_TRANSITION_INTENT_SCHEMA
        ):
            raise AuthorityStateRecordError("authority-state evaluation must reference a transition intent")
        if (
            not isinstance(self.commit_ref, AuthorityStateRecordRef)
            or self.commit_ref.record_schema != AUTHORITY_STATE_TRANSITION_COMMIT_SCHEMA
        ):
            raise AuthorityStateRecordError("authority-state evaluation must reference a transition commit")
        _token(self.observed_selection_token, "authority-state evaluation.observed_selection_token")
        _require_sorted_records(
            self.installed_states,
            "authority-state evaluation.installed_states",
            key="layer_id",
        )
        _digest(self.effects_digest, "authority-state evaluation.effects_digest")
        if self.result not in EVALUATION_RESULTS:
            raise AuthorityStateRecordError(
                f"authority-state evaluation.result must be one of {sorted(EVALUATION_RESULTS)}"
            )
        if not isinstance(self.findings, tuple):
            raise AuthorityStateRecordError("authority-state evaluation.findings must be a tuple")
        findings = tuple(
            _text(item, f"authority-state evaluation.findings[{index}]") for index, item in enumerate(self.findings)
        )
        if findings != tuple(sorted(set(findings))):
            raise AuthorityStateRecordError("authority-state evaluation.findings must be sorted and unique")
        if (self.result == "satisfied") is not (not findings):
            raise AuthorityStateRecordError(
                "a satisfied evaluation has no findings and a failed evaluation has findings"
            )
        _timestamp(self.evaluated_at, "authority-state evaluation.evaluated_at")

    @classmethod
    def mint(
        cls,
        *,
        intent_ref: AuthorityStateRecordRef,
        commit_ref: AuthorityStateRecordRef,
        commit: AuthorityStateTransitionCommit,
        result: str,
        findings: Iterable[str] = (),
        evaluated_at: str,
    ) -> AuthorityStateTransitionEvaluation:
        if intent_ref != commit.intent_ref:
            raise AuthorityStateRecordError("authority-state evaluation intent reference disagrees with commit")
        if commit_ref.record_digest != commit.digest:
            raise AuthorityStateRecordError("authority-state evaluation commit reference is stale")
        finding_rows = tuple(_text(item, "authority-state evaluation finding") for item in findings)
        if len(finding_rows) != len(set(finding_rows)):
            raise AuthorityStateRecordError("authority-state evaluation findings contain duplicates")
        return cls(
            commit.transaction_id,
            commit.transition_revision,
            intent_ref,
            commit_ref,
            commit.observed_selection_token,
            commit.installed_states,
            commit.effects_digest,
            result,
            tuple(sorted(finding_rows)),
            evaluated_at,
        )

    @classmethod
    def parse(
        cls, value: object, where: str = "authority-state transition evaluation"
    ) -> AuthorityStateTransitionEvaluation:
        row = _row(cls, value, where)
        installed = tuple(
            AuthorityStateMemberImage.parse(item, f"{where}.installed_states[{index}]")
            for index, item in enumerate(_record_list(row["installed_states"], f"{where}.installed_states"))
        )
        return _finish(
            cls(
                row["transaction_id"],
                row["transition_revision"],
                AuthorityStateRecordRef.parse(row["intent_ref"], f"{where}.intent_ref"),
                AuthorityStateRecordRef.parse(row["commit_ref"], f"{where}.commit_ref"),
                _token(row["observed_selection_token"], f"{where}.observed_selection_token"),
                installed,
                row["effects_digest"],
                row["result"],
                tuple(_record_list(row["findings"], f"{where}.findings")),
                row["evaluated_at"],
            ),
            row,
            where,
        )


@dataclass(frozen=True, slots=True)
class AuthorityStateCoordinatorHead(_SemanticRecord):
    SCHEMA: ClassVar[str] = AUTHORITY_STATE_HEAD_SCHEMA
    DIGEST_FIELD: ClassVar[str] = "head_digest"

    revision: int
    predecessor_head_ref: AuthorityStateRecordRef | None
    predecessor_head_digest: str | None
    selection_token: AuthoritySelectionTokenProjection
    commit_ref: AuthorityStateRecordRef
    evaluation_ref: AuthorityStateRecordRef

    def __post_init__(self) -> None:
        revision = _revision(self.revision, "authority-state head.revision")
        predecessor = _optional_digest(self.predecessor_head_digest, "authority-state head.predecessor_head_digest")
        if (revision == 1) is not (
            predecessor is None and self.predecessor_head_ref is None
        ):
            raise AuthorityStateRecordError(
                "authority-state head predecessor reference and digest must both be "
                "null exactly at revision one"
            )
        if self.predecessor_head_ref is not None:
            if self.predecessor_head_ref.record_schema != AUTHORITY_STATE_HEAD_SCHEMA:
                raise AuthorityStateRecordError(
                    "authority-state head predecessor must reference a coordinator head"
                )
            if self.predecessor_head_ref.record_digest != predecessor:
                raise AuthorityStateRecordError(
                    "authority-state head predecessor reference digest disagrees"
                )
        selection = _token(self.selection_token, "authority-state head.selection_token")
        if selection.plan_revision == 0 and selection.jit_revision == 0:
            raise AuthorityStateRecordError("a committed authority-state head requires selected authority")
        if (
            not isinstance(self.commit_ref, AuthorityStateRecordRef)
            or self.commit_ref.record_schema != AUTHORITY_STATE_TRANSITION_COMMIT_SCHEMA
        ):
            raise AuthorityStateRecordError("authority-state head must reference a transition commit")
        if (
            not isinstance(self.evaluation_ref, AuthorityStateRecordRef)
            or self.evaluation_ref.record_schema != AUTHORITY_STATE_TRANSITION_EVALUATION_SCHEMA
        ):
            raise AuthorityStateRecordError("authority-state head must reference a transition evaluation")

    @classmethod
    def mint(
        cls,
        *,
        commit_ref: AuthorityStateRecordRef,
        commit: AuthorityStateTransitionCommit,
        evaluation_ref: AuthorityStateRecordRef,
        evaluation: AuthorityStateTransitionEvaluation,
    ) -> AuthorityStateCoordinatorHead:
        if commit_ref.record_digest != commit.digest:
            raise AuthorityStateRecordError("authority-state head commit reference is stale")
        if evaluation_ref.record_digest != evaluation.digest:
            raise AuthorityStateRecordError("authority-state head evaluation reference is stale")
        if evaluation.commit_ref != commit_ref or evaluation.result != "satisfied":
            raise AuthorityStateRecordError(
                "authority-state head requires a satisfied independent evaluation of its commit"
            )
        if evaluation.transition_revision != commit.successor_head_revision:
            raise AuthorityStateRecordError("authority-state head evaluation revision disagrees with commit")
        return cls(
            commit.successor_head_revision,
            commit.predecessor_head_ref,
            commit.predecessor_head_digest,
            evaluation.observed_selection_token,
            commit_ref,
            evaluation_ref,
        )

    @classmethod
    def parse(cls, value: object, where: str = "authority-state coordinator head") -> AuthorityStateCoordinatorHead:
        row = _row(cls, value, where)
        return _finish(
            cls(
                row["revision"],
                None
                if row["predecessor_head_ref"] is None
                else AuthorityStateRecordRef.parse(
                    row["predecessor_head_ref"],
                    f"{where}.predecessor_head_ref",
                ),
                row["predecessor_head_digest"],
                _token(row["selection_token"], f"{where}.selection_token"),
                AuthorityStateRecordRef.parse(row["commit_ref"], f"{where}.commit_ref"),
                AuthorityStateRecordRef.parse(row["evaluation_ref"], f"{where}.evaluation_ref"),
            ),
            row,
            where,
        )


__all__ = [
    "AuthorityStateCoordinatorHead",
    "AuthorityStatePendingPointer",
    "AuthorityStateTransitionCommit",
    "AuthorityStateTransitionEvaluation",
]
