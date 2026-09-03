"""Pure commit proof for a controller-dispatched layer rematerialization (ADR-0010).

A ``publish_validated_amendment`` stop names one layer whose selected view must be replaced
through the atomic authority-state transaction. The controller proves that transaction ran
by binding the exact running receipt, the authority digest the envelope observed before, and
the selected authority observed after publication. The record is content-addressed and
immutable; the terminal receipt names it as its commit marker.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from vfx_harness.domain.stop_amendment_transactions import (
    PublishValidatedAmendmentTarget,
    SelectedAuthorityAmendmentCommitted,
)
from vfx_harness.domain.stop_envelope_primitives import (
    canonical_digest,
    record,
    require_canonical_digest,
    require_digest,
    require_id,
    require_text_tuple,
)
from vfx_harness.domain.stop_transaction_state import SelectedAuthorityAssertionV2
from vfx_harness.domain.stop_transactions import StopAction
from vfx_harness.domain.transaction_receipts import TransactionReceipt


@dataclass(frozen=True, slots=True)
class RematerializationCommit:
    """One key-bound proof that a layer view was republished for an exact finding set."""

    SCHEMA: ClassVar[str] = "vfx-harness.rematerialization-commit/v1"

    idempotency_key: str
    action_digest: str
    postcondition_digest: str
    running_receipt_revision: int
    running_receipt_digest: str
    layer_id: str
    finding_ids: tuple[str, ...]
    before_authority_digest: str
    after_authority_digest: str
    after_bundle_digest: str
    after_view_source: str
    after_view_digest: str

    def __post_init__(self) -> None:
        for name in (
            "idempotency_key",
            "action_digest",
            "postcondition_digest",
            "running_receipt_digest",
            "before_authority_digest",
            "after_authority_digest",
            "after_bundle_digest",
            "after_view_digest",
        ):
            require_digest(getattr(self, name), f"RematerializationCommit.{name}")
        if (
            not isinstance(self.running_receipt_revision, int)
            or isinstance(self.running_receipt_revision, bool)
            or self.running_receipt_revision < 1
        ):
            raise ValueError("RematerializationCommit.running_receipt_revision must be positive")
        require_id(self.layer_id, "RematerializationCommit.layer_id")
        object.__setattr__(
            self,
            "finding_ids",
            require_text_tuple(self.finding_ids, "RematerializationCommit.finding_ids"),
        )
        if self.after_view_source != "jit":
            raise ValueError("a layer-view amendment commits only a JIT effective view")
        if self.after_authority_digest == self.before_authority_digest:
            raise ValueError("a rematerialization commit must change selected authority")

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "idempotency_key": self.idempotency_key,
            "action_digest": self.action_digest,
            "postcondition_digest": self.postcondition_digest,
            "running_receipt_revision": self.running_receipt_revision,
            "running_receipt_digest": self.running_receipt_digest,
            "layer_id": self.layer_id,
            "finding_ids": list(self.finding_ids),
            "before_authority_digest": self.before_authority_digest,
            "after_authority_digest": self.after_authority_digest,
            "after_bundle_digest": self.after_bundle_digest,
            "after_view_source": self.after_view_source,
            "after_view_digest": self.after_view_digest,
        }

    @property
    def digest(self) -> str:
        return canonical_digest(self._payload())

    def as_dict(self) -> dict[str, Any]:
        return {**self._payload(), "commit_digest": self.digest}

    @classmethod
    def create(
        cls,
        *,
        action: StopAction,
        running_receipt: TransactionReceipt,
        before_authority_digest: str,
        after_authority: SelectedAuthorityAssertionV2,
    ) -> RematerializationCommit:
        """Earn a commit only from one exact running attempt and the published authority."""

        target = action.target
        if not isinstance(target, PublishValidatedAmendmentTarget) or not isinstance(
            action.postcondition, SelectedAuthorityAmendmentCommitted
        ):
            raise ValueError("rematerialization commit requires a publish_validated_amendment action")
        if target.scope != "layer_view" or target.layer_id is None:
            raise ValueError("rematerialization commit requires a layer-view amendment")
        if (
            not isinstance(running_receipt, TransactionReceipt)
            or running_receipt.phase != "running"
            or running_receipt.action != action
        ):
            raise ValueError("rematerialization commit requires the exact running receipt")
        if running_receipt.authoritative_before_digest != before_authority_digest:
            raise ValueError("running receipt does not observe the same authority as the stop")
        if (
            after_authority.selection != "selected"
            or after_authority.bundle is None
            or after_authority.effective_view is None
        ):
            raise ValueError("published authority after rematerialization must be selected")
        return cls(
            idempotency_key=running_receipt.idempotency_key,
            action_digest=action.digest,
            postcondition_digest=action.postcondition.digest,
            running_receipt_revision=running_receipt.revision,
            running_receipt_digest=running_receipt.digest,
            layer_id=target.layer_id,
            finding_ids=tuple(item.record_id for item in target.findings),
            before_authority_digest=before_authority_digest,
            after_authority_digest=after_authority.digest,
            after_bundle_digest=after_authority.bundle.digest,
            after_view_source=after_authority.effective_view.source,
            after_view_digest=after_authority.effective_view.digest,
        )

    def assert_matches(
        self,
        *,
        action: StopAction,
        running_receipt: TransactionReceipt,
        before_authority_digest: str,
        after_authority: SelectedAuthorityAssertionV2,
    ) -> None:
        expected = RematerializationCommit.create(
            action=action,
            running_receipt=running_receipt,
            before_authority_digest=before_authority_digest,
            after_authority=after_authority,
        )
        if expected != self:
            raise ValueError("rematerialization commit does not bind the observed attempt and authority")

    @classmethod
    def from_dict(cls, value: Any, where: str) -> RematerializationCommit:
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
                "layer_id",
                "finding_ids",
                "before_authority_digest",
                "after_authority_digest",
                "after_bundle_digest",
                "after_view_source",
                "after_view_digest",
                "commit_digest",
            ),
        )
        finding_ids = row["finding_ids"]
        if not isinstance(finding_ids, list):
            raise ValueError(f"{where}.finding_ids must be a list")
        candidate = cls(
            idempotency_key=row["idempotency_key"],
            action_digest=row["action_digest"],
            postcondition_digest=row["postcondition_digest"],
            running_receipt_revision=row["running_receipt_revision"],
            running_receipt_digest=row["running_receipt_digest"],
            layer_id=row["layer_id"],
            finding_ids=tuple(finding_ids),
            before_authority_digest=row["before_authority_digest"],
            after_authority_digest=row["after_authority_digest"],
            after_bundle_digest=row["after_bundle_digest"],
            after_view_source=row["after_view_source"],
            after_view_digest=row["after_view_digest"],
        )
        require_canonical_digest(row["commit_digest"], candidate.digest, where, "commit_digest")
        return candidate


__all__ = ["RematerializationCommit"]
