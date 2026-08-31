"""Pure authoritative commit contract for external environment recovery."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from vfx_harness.domain.environment_results import EnvironmentResult
from vfx_harness.domain.stop_envelope_primitives import (
    canonical_digest,
    record,
    require_canonical_digest,
    require_digest,
    require_id,
    require_text_tuple,
    text_tuple_from_list,
)
from vfx_harness.domain.stop_transaction_state import StopEvidenceRef
from vfx_harness.domain.stop_transactions import (
    EnvironmentReverified,
    RecoverEnvironmentTarget,
    StopAction,
)
from vfx_harness.domain.transaction_receipts import TransactionReceipt


@dataclass(frozen=True, slots=True)
class EnvironmentRecoveryCommit:
    """One key-bound proof that the failed environment was independently reverified."""

    SCHEMA: ClassVar[str] = "vfx-harness.environment-recovery-commit/v1"

    idempotency_key: str
    action_digest: str
    postcondition_digest: str
    running_receipt_revision: int
    running_receipt_digest: str
    probe_id: str
    probe_spec_digest: str
    before_result_digest: str
    before_environment_digest: str
    failed_check_ids: tuple[str, ...]
    after_result: StopEvidenceRef
    after_environment_digest: str

    def __post_init__(self) -> None:
        for name in (
            "idempotency_key",
            "action_digest",
            "postcondition_digest",
            "running_receipt_digest",
            "probe_spec_digest",
            "before_result_digest",
            "before_environment_digest",
            "after_environment_digest",
        ):
            require_digest(getattr(self, name), f"EnvironmentRecoveryCommit.{name}")
        if (
            not isinstance(self.running_receipt_revision, int)
            or isinstance(self.running_receipt_revision, bool)
            or self.running_receipt_revision < 2
        ):
            raise ValueError(
                "EnvironmentRecoveryCommit.running_receipt_revision must be an integer >= 2"
            )
        require_id(self.probe_id, "EnvironmentRecoveryCommit.probe_id")
        object.__setattr__(
            self,
            "failed_check_ids",
            require_text_tuple(
                self.failed_check_ids,
                "EnvironmentRecoveryCommit.failed_check_ids",
            ),
        )
        if not isinstance(self.after_result, StopEvidenceRef):
            raise ValueError("EnvironmentRecoveryCommit.after_result must be typed evidence")
        if (
            self.after_result.kind != "environment_result"
            or self.after_result.record_schema != EnvironmentResult.SCHEMA
        ):
            raise ValueError(
                "EnvironmentRecoveryCommit.after_result must cite an exact environment result"
            )
        if self.before_environment_digest == self.after_environment_digest:
            raise ValueError("environment recovery must change authoritative environment state")

    def _wire_payload(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "idempotency_key": self.idempotency_key,
            "action_digest": self.action_digest,
            "postcondition_digest": self.postcondition_digest,
            "running_receipt_revision": self.running_receipt_revision,
            "running_receipt_digest": self.running_receipt_digest,
            "probe_id": self.probe_id,
            "probe_spec_digest": self.probe_spec_digest,
            "before_result_digest": self.before_result_digest,
            "before_environment_digest": self.before_environment_digest,
            "failed_check_ids": list(self.failed_check_ids),
            "after_result": self.after_result.as_dict(),
            "after_environment_digest": self.after_environment_digest,
        }

    def _identity_payload(self) -> dict[str, Any]:
        return {
            **self._wire_payload(),
            "after_result": self.after_result.identity_dict(),
        }

    @property
    def digest(self) -> str:
        return canonical_digest(self._identity_payload())

    def as_dict(self) -> dict[str, Any]:
        return {**self._wire_payload(), "commit_digest": self.digest}

    @classmethod
    def create(
        cls,
        *,
        action: StopAction,
        running_receipt: TransactionReceipt,
        before_result: EnvironmentResult,
        before_result_ref: StopEvidenceRef,
        after_result: EnvironmentResult,
        after_result_ref: StopEvidenceRef,
    ) -> EnvironmentRecoveryCommit:
        """Earn a commit only from one exact running attempt and two parsed results."""

        if not isinstance(action, StopAction) or not isinstance(
            action.target, RecoverEnvironmentTarget
        ) or not isinstance(action.postcondition, EnvironmentReverified):
            raise ValueError("environment recovery commit requires a recover_environment action")
        if (
            not isinstance(running_receipt, TransactionReceipt)
            or running_receipt.phase != "running"
            or running_receipt.action_digest != action.digest
        ):
            raise ValueError(
                "environment recovery commit requires the semantically exact running receipt"
            )
        if running_receipt.adapter_id != action.target.recovery_adapter_id:
            raise ValueError(
                "environment recovery running receipt names another recovery adapter"
            )
        if not isinstance(before_result, EnvironmentResult) or before_result.ok:
            raise ValueError("environment recovery commit requires the exact failed before result")
        if not isinstance(after_result, EnvironmentResult) or not after_result.ok:
            raise ValueError("environment recovery commit requires a fully passing after result")
        target = action.target
        postcondition = action.postcondition
        before_failed_ids = tuple(
            check.check_id for check in before_result.checks if not check.passed
        )
        if (
            before_result.digest != target.environment.result_digest
            or before_result.probe_id != target.environment.probe_id
            or before_result.probe_spec is None
            or before_result.probe_spec.digest != target.environment.probe_spec_digest
            or before_failed_ids != target.failed_check_ids
            or before_result.environment_digest != running_receipt.authoritative_before_digest
            or before_result.digest != running_receipt.attempt_evidence_digest
        ):
            raise ValueError("before environment result does not bind the exact recovery target")
        if (
            not isinstance(before_result_ref, StopEvidenceRef)
            or before_result_ref.kind != "environment_result"
            or before_result_ref.record_schema != EnvironmentResult.SCHEMA
            or before_result_ref.record_digest != before_result.digest
            or before_result_ref.digest not in {evidence.digest for evidence in target.evidence}
        ):
            raise ValueError(
                "before environment result evidence is not the target's exact record identity"
            )
        after_checks = {check.check_id: check for check in after_result.checks}
        if (
            after_result.probe_id != postcondition.probe_id
            or after_result.probe_spec is None
            or after_result.probe_spec.digest != postcondition.probe_spec_digest
            or any(
                check_id not in after_checks or not after_checks[check_id].passed
                for check_id in postcondition.failed_check_ids
            )
        ):
            raise ValueError("after environment result does not satisfy the declared probe")
        if (
            not isinstance(after_result_ref, StopEvidenceRef)
            or after_result_ref.kind != "environment_result"
            or after_result_ref.record_schema != EnvironmentResult.SCHEMA
            or after_result_ref.record_digest != after_result.digest
            or after_result_ref.digest
            not in {evidence.digest for evidence in running_receipt.operation_refs}
        ):
            raise ValueError(
                "after environment result evidence is not a durably recorded receipt observation"
            )
        return cls(
            idempotency_key=running_receipt.idempotency_key,
            action_digest=action.digest,
            postcondition_digest=postcondition.digest,
            running_receipt_revision=running_receipt.revision,
            running_receipt_digest=running_receipt.digest,
            probe_id=postcondition.probe_id,
            probe_spec_digest=postcondition.probe_spec_digest,
            before_result_digest=before_result.digest,
            before_environment_digest=before_result.environment_digest,
            failed_check_ids=postcondition.failed_check_ids,
            after_result=after_result_ref,
            after_environment_digest=after_result.environment_digest,
        )

    def assert_matches(
        self,
        *,
        action: StopAction,
        running_receipt: TransactionReceipt,
        before_result: EnvironmentResult,
        before_result_ref: StopEvidenceRef,
        after_result: EnvironmentResult,
        after_result_ref: StopEvidenceRef,
    ) -> None:
        expected = self.create(
            action=action,
            running_receipt=running_receipt,
            before_result=before_result,
            before_result_ref=before_result_ref,
            after_result=after_result,
            after_result_ref=after_result_ref,
        )
        if self != expected:
            raise ValueError("environment recovery commit does not match the exact transaction")

    @classmethod
    def from_dict(cls, value: Any, where: str) -> EnvironmentRecoveryCommit:
        row = record(
            value,
            where,
            cls.SCHEMA,
            (
                "idempotency_key",
                "action_digest",
                "postcondition_digest",
                "running_receipt_revision",
                "running_receipt_digest",
                "probe_id",
                "probe_spec_digest",
                "before_result_digest",
                "before_environment_digest",
                "failed_check_ids",
                "after_result",
                "after_environment_digest",
                "commit_digest",
            ),
        )
        candidate = cls(
            idempotency_key=row["idempotency_key"],
            action_digest=row["action_digest"],
            postcondition_digest=row["postcondition_digest"],
            running_receipt_revision=row["running_receipt_revision"],
            running_receipt_digest=row["running_receipt_digest"],
            probe_id=row["probe_id"],
            probe_spec_digest=row["probe_spec_digest"],
            before_result_digest=row["before_result_digest"],
            before_environment_digest=row["before_environment_digest"],
            failed_check_ids=text_tuple_from_list(
                row["failed_check_ids"], f"{where}.failed_check_ids"
            ),
            after_result=StopEvidenceRef.from_dict(
                row["after_result"], f"{where}.after_result"
            ),
            after_environment_digest=row["after_environment_digest"],
        )
        require_canonical_digest(
            row["commit_digest"], candidate.digest, where, "commit_digest"
        )
        return candidate
