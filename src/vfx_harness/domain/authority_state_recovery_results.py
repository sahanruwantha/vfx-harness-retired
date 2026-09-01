"""Pure public result contract for deterministic authority-state recovery."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from vfx_harness.domain.authority_head_records import AuthoritySelectionTokenProjection
from vfx_harness.domain.authority_state_record_primitives import (
    AUTHORITY_STATE_HEAD_SCHEMA,
    AUTHORITY_STATE_TRANSITION_INTENT_SCHEMA,
    AuthorityStateRecordError,
    AuthorityStateRecordRef,
    _finish,
    _identifier,
    _record_list,
    _revision,
    _row,
    _SemanticRecord,
    _token,
)

AUTHORITY_STATE_RECOVERY_RESULT_SCHEMA = (
    "vfx-harness.authority-state-recovery-result/v1"
)
AUTHORITY_STATE_RECOVERY_DISPOSITIONS = frozenset(
    {"recovered", "already_current"}
)


@dataclass(frozen=True, slots=True)
class AuthorityStateRecoveryResult(_SemanticRecord):
    """Typed evidence that the public recovery boundary resolved one exact head."""

    SCHEMA: ClassVar[str] = AUTHORITY_STATE_RECOVERY_RESULT_SCHEMA
    DIGEST_FIELD: ClassVar[str] = "result_digest"

    disposition: str
    transaction_id: str
    transition_revision: int
    intent_ref: AuthorityStateRecordRef
    coordinator_head_ref: AuthorityStateRecordRef
    selection_token: AuthoritySelectionTokenProjection
    state_member_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.disposition not in AUTHORITY_STATE_RECOVERY_DISPOSITIONS:
            raise AuthorityStateRecordError(
                "authority-state recovery disposition must be recovered or "
                "already_current"
            )
        _identifier(
            self.transaction_id,
            "authority-state recovery result.transaction_id",
        )
        _revision(
            self.transition_revision,
            "authority-state recovery result.transition_revision",
        )
        if (
            not isinstance(self.intent_ref, AuthorityStateRecordRef)
            or self.intent_ref.record_schema
            != AUTHORITY_STATE_TRANSITION_INTENT_SCHEMA
        ):
            raise AuthorityStateRecordError(
                "authority-state recovery result must reference an exact transition "
                "intent"
            )
        if (
            not isinstance(self.coordinator_head_ref, AuthorityStateRecordRef)
            or self.coordinator_head_ref.record_schema != AUTHORITY_STATE_HEAD_SCHEMA
        ):
            raise AuthorityStateRecordError(
                "authority-state recovery result must reference an exact coordinator "
                "head"
            )
        _token(
            self.selection_token,
            "authority-state recovery result.selection_token",
        )
        if not isinstance(self.state_member_ids, tuple):
            raise AuthorityStateRecordError(
                "authority-state recovery result.state_member_ids must be a tuple"
            )
        member_ids = tuple(
            _identifier(
                member_id,
                f"authority-state recovery result.state_member_ids[{index}]",
            )
            for index, member_id in enumerate(self.state_member_ids)
        )
        if member_ids != tuple(sorted(set(member_ids))):
            raise AuthorityStateRecordError(
                "authority-state recovery result.state_member_ids must be sorted and "
                "unique"
            )

    @classmethod
    def parse(
        cls,
        value: object,
        where: str = "authority-state recovery result",
    ) -> AuthorityStateRecoveryResult:
        row = _row(cls, value, where)
        return _finish(
            cls(
                disposition=row["disposition"],
                transaction_id=row["transaction_id"],
                transition_revision=row["transition_revision"],
                intent_ref=AuthorityStateRecordRef.parse(
                    row["intent_ref"],
                    f"{where}.intent_ref",
                ),
                coordinator_head_ref=AuthorityStateRecordRef.parse(
                    row["coordinator_head_ref"],
                    f"{where}.coordinator_head_ref",
                ),
                selection_token=_token(
                    row["selection_token"],
                    f"{where}.selection_token",
                ),
                state_member_ids=tuple(
                    _record_list(
                        row["state_member_ids"],
                        f"{where}.state_member_ids",
                    )
                ),
            ),
            row,
            where,
        )


__all__ = [
    "AUTHORITY_STATE_RECOVERY_DISPOSITIONS",
    "AUTHORITY_STATE_RECOVERY_RESULT_SCHEMA",
    "AuthorityStateRecoveryResult",
]
