"""Pure immutable transaction receipts and monotone phase transitions.

The durable store lives outside ``domain``.  This module defines only the strict
content-addressed records that such a store may publish.  A receipt embeds the exact
``StopAction`` and recomputes its idempotency key; a caller-supplied key can therefore
never substitute another action or authoritative attempt.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise
from typing import Any, ClassVar

from vfx_harness.domain.stop_envelope_primitives import (
    canonical_digest,
    list_value,
    record,
    require_canonical_digest,
    require_digest,
    require_id,
)
from vfx_harness.domain.stop_transaction_state import (
    StopEvidenceRef,
    _evidence_tuple,
    _finish,
    _positive_int,
    _row,
    _StrictRecord,
)
from vfx_harness.domain.stop_transactions import (
    TRANSACTION_RECEIPT_SCHEMA,
    StopAction,
    action_idempotency_key,
)

TRANSACTION_RECEIPT_PHASES = frozenset({"prepared", "running", "terminal"})
TRANSACTION_TERMINAL_OUTCOMES = frozenset({"committed", "failed", "indeterminate"})
RECOVERY_DISPOSITIONS = frozenset(
    {
        "repeatable_read_only",
        "resume_checkpointed",
        "query_external",
        "reconcile_commit_only",
        "halt_on_uncertainty",
    }
)

# Recovery may become more restrictive as execution learns that work has started, but
# it may never become safer by assertion.  Resume and external-query paths are
# deliberately incomparable: changing between them would invent a recovery mechanism.
_RECOVERY_SUCCESSORS = {
    "repeatable_read_only": frozenset(
        {"repeatable_read_only", "reconcile_commit_only", "halt_on_uncertainty"}
    ),
    "resume_checkpointed": frozenset(
        {"resume_checkpointed", "reconcile_commit_only", "halt_on_uncertainty"}
    ),
    "query_external": frozenset(
        {"query_external", "reconcile_commit_only", "halt_on_uncertainty"}
    ),
    "reconcile_commit_only": frozenset(
        {"reconcile_commit_only", "halt_on_uncertainty"}
    ),
    "halt_on_uncertainty": frozenset({"halt_on_uncertainty"}),
}


def _non_negative_int(value: Any, where: str) -> int:
    return _positive_int(value, where, allow_zero=True)


@dataclass(frozen=True, slots=True)
class TransactionSpend(_StrictRecord):
    """Cumulative integer-only resource use for one transaction attempt."""

    SCHEMA: ClassVar[str] = "vfx-harness.transaction-spend/v1"
    DIGEST_FIELD: ClassVar[str] = "spend_digest"

    model_input_tokens: int
    model_output_tokens: int
    model_turns: int
    external_requests: int
    cost_microusd: int
    elapsed_milliseconds: int

    def __post_init__(self) -> None:
        for name in (
            "model_input_tokens",
            "model_output_tokens",
            "model_turns",
            "external_requests",
            "cost_microusd",
            "elapsed_milliseconds",
        ):
            _non_negative_int(getattr(self, name), f"TransactionSpend.{name}")

    @classmethod
    def zero(cls) -> TransactionSpend:
        return cls(0, 0, 0, 0, 0, 0)

    def is_zero(self) -> bool:
        return all(
            getattr(self, name) == 0
            for name in (
                "model_input_tokens",
                "model_output_tokens",
                "model_turns",
                "external_requests",
                "cost_microusd",
                "elapsed_milliseconds",
            )
        )

    def is_monotone_from(self, prior: TransactionSpend) -> bool:
        if not isinstance(prior, TransactionSpend):
            raise ValueError("prior spend must be TransactionSpend")
        return all(
            getattr(self, name) >= getattr(prior, name)
            for name in (
                "model_input_tokens",
                "model_output_tokens",
                "model_turns",
                "external_requests",
                "cost_microusd",
                "elapsed_milliseconds",
            )
        )

    @classmethod
    def from_dict(cls, value: Any, where: str) -> TransactionSpend:
        row = _row(cls, value, where)
        candidate = cls(
            model_input_tokens=row["model_input_tokens"],
            model_output_tokens=row["model_output_tokens"],
            model_turns=row["model_turns"],
            external_requests=row["external_requests"],
            cost_microusd=row["cost_microusd"],
            elapsed_milliseconds=row["elapsed_milliseconds"],
        )
        return _finish(candidate, row, where)


def _refs(value: Any, where: str) -> tuple[StopEvidenceRef, ...]:
    return tuple(
        StopEvidenceRef.from_dict(item, f"{where}[{index}]")
        for index, item in enumerate(list_value(value, where))
    )


def _optional_ref(value: Any, where: str) -> StopEvidenceRef | None:
    return None if value is None else StopEvidenceRef.from_dict(value, where)


def _validate_non_receipt_refs(refs: tuple[StopEvidenceRef, ...], where: str) -> None:
    if any(ref.kind == "transaction_receipt" for ref in refs):
        raise ValueError(f"{where} cannot contain a transaction receipt and create a receipt cycle")


def _merge_refs(
    current: tuple[StopEvidenceRef, ...],
    additional: tuple[StopEvidenceRef, ...],
    where: str,
) -> tuple[StopEvidenceRef, ...]:
    combined: dict[str, StopEvidenceRef] = {ref.digest: ref for ref in current}
    for ref in additional:
        if not isinstance(ref, StopEvidenceRef):
            raise ValueError(f"{where} must contain StopEvidenceRef values")
        combined.setdefault(ref.digest, ref)
    return _evidence_tuple(tuple(combined.values()), where, allow_empty=True)


@dataclass(frozen=True, slots=True)
class TransactionReceipt:
    """One immutable revision in a key-addressable transaction receipt chain."""

    SCHEMA: ClassVar[str] = TRANSACTION_RECEIPT_SCHEMA

    idempotency_key: str
    revision: int
    action: StopAction
    adapter_id: str
    authoritative_before_digest: str
    attempt_evidence_digest: str
    phase: str
    predecessor_receipt_digest: str | None
    recovery_disposition: str
    operation_refs: tuple[StopEvidenceRef, ...]
    spend: TransactionSpend
    terminal_outcome: str | None
    authoritative_after_digest: str | None
    commit_marker: StopEvidenceRef | None
    result_evidence: tuple[StopEvidenceRef, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.action, StopAction):
            raise ValueError("TransactionReceipt.action must be an exact StopAction")
        require_digest(self.idempotency_key, "TransactionReceipt.idempotency_key")
        expected_key = action_idempotency_key(
            self.action,
            authoritative_before_digest=self.authoritative_before_digest,
            attempt_evidence_digest=self.attempt_evidence_digest,
        )
        if self.idempotency_key != expected_key:
            raise ValueError("TransactionReceipt.idempotency_key does not match its exact action attempt")
        _positive_int(self.revision, "TransactionReceipt.revision")
        require_id(self.adapter_id, "TransactionReceipt.adapter_id")
        require_digest(
            self.authoritative_before_digest,
            "TransactionReceipt.authoritative_before_digest",
        )
        require_digest(
            self.attempt_evidence_digest,
            "TransactionReceipt.attempt_evidence_digest",
        )
        if self.phase not in TRANSACTION_RECEIPT_PHASES:
            raise ValueError(
                "TransactionReceipt.phase must be one of "
                f"{sorted(TRANSACTION_RECEIPT_PHASES)}"
            )
        if self.predecessor_receipt_digest is not None:
            require_digest(
                self.predecessor_receipt_digest,
                "TransactionReceipt.predecessor_receipt_digest",
            )
        if self.recovery_disposition not in RECOVERY_DISPOSITIONS:
            raise ValueError(
                "TransactionReceipt.recovery_disposition must be one of "
                f"{sorted(RECOVERY_DISPOSITIONS)}"
            )
        operation_refs = _evidence_tuple(
            self.operation_refs,
            "TransactionReceipt.operation_refs",
            allow_empty=True,
        )
        _validate_non_receipt_refs(operation_refs, "TransactionReceipt.operation_refs")
        object.__setattr__(self, "operation_refs", operation_refs)
        if not isinstance(self.spend, TransactionSpend):
            raise ValueError("TransactionReceipt.spend must be TransactionSpend")
        result_evidence = _evidence_tuple(
            self.result_evidence,
            "TransactionReceipt.result_evidence",
            allow_empty=True,
        )
        _validate_non_receipt_refs(result_evidence, "TransactionReceipt.result_evidence")
        object.__setattr__(self, "result_evidence", result_evidence)

        if self.phase == "prepared":
            if self.revision != 1 or self.predecessor_receipt_digest is not None:
                raise ValueError("a prepared receipt must be revision 1 with no predecessor")
            if operation_refs or not self.spend.is_zero():
                raise ValueError("a prepared receipt cannot claim execution references or spend")
            self._require_nonterminal_fields()
        elif self.phase == "running":
            if self.revision < 2 or self.predecessor_receipt_digest is None:
                raise ValueError("a running receipt requires revision >= 2 and a predecessor")
            self._require_nonterminal_fields()
        else:
            if self.revision < 3 or self.predecessor_receipt_digest is None:
                raise ValueError("a terminal receipt requires revision >= 3 and a predecessor")
            if self.terminal_outcome not in TRANSACTION_TERMINAL_OUTCOMES:
                raise ValueError(
                    "a terminal receipt outcome must be one of "
                    f"{sorted(TRANSACTION_TERMINAL_OUTCOMES)}"
                )
            if self.authoritative_after_digest is None:
                raise ValueError("a terminal receipt requires authoritative_after_digest")
            require_digest(
                self.authoritative_after_digest,
                "TransactionReceipt.authoritative_after_digest",
            )
            if not result_evidence:
                raise ValueError("a terminal receipt requires typed result evidence")
            if self.terminal_outcome == "committed":
                if self.commit_marker is None or self.commit_marker.kind != "authority_record":
                    raise ValueError("a committed receipt requires an exact authority-record commit marker")
                if self.authoritative_after_digest == self.authoritative_before_digest:
                    raise ValueError("a committed receipt must change authoritative domain state")
            elif self.commit_marker is not None:
                raise ValueError("failed or indeterminate receipts cannot claim a commit marker")

    def _require_nonterminal_fields(self) -> None:
        if (
            self.terminal_outcome is not None
            or self.authoritative_after_digest is not None
            or self.commit_marker is not None
            or self.result_evidence
        ):
            raise ValueError("prepared or running receipts cannot claim terminal fields")

    @property
    def transaction_id(self) -> str:
        return self.action.transaction_id

    @property
    def action_digest(self) -> str:
        return self.action.digest

    @property
    def precondition_digest(self) -> str:
        return self.action.precondition_digest

    @property
    def postcondition_digest(self) -> str:
        return self.action.postcondition.digest

    @property
    def evaluator_id(self) -> str:
        return self.action.evaluator_id

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "idempotency_key": self.idempotency_key,
            "revision": self.revision,
            "transaction_id": self.transaction_id,
            "action": self.action.as_dict(),
            "action_digest": self.action_digest,
            "precondition_digest": self.precondition_digest,
            "postcondition_digest": self.postcondition_digest,
            "evaluator_id": self.evaluator_id,
            "adapter_id": self.adapter_id,
            "authoritative_before_digest": self.authoritative_before_digest,
            "attempt_evidence_digest": self.attempt_evidence_digest,
            "phase": self.phase,
            "predecessor_receipt_digest": self.predecessor_receipt_digest,
            "recovery_disposition": self.recovery_disposition,
            "operation_refs": [ref.as_dict() for ref in self.operation_refs],
            "spend": self.spend.as_dict(),
            "terminal_outcome": self.terminal_outcome,
            "authoritative_after_digest": self.authoritative_after_digest,
            "commit_marker": None if self.commit_marker is None else self.commit_marker.as_dict(),
            "result_evidence": [ref.as_dict() for ref in self.result_evidence],
        }

    @property
    def digest(self) -> str:
        return canonical_digest(self._payload())

    def as_dict(self) -> dict[str, Any]:
        return {**self._payload(), "receipt_digest": self.digest}

    def evidence_ref(self, *, locator: str, sha256: str) -> StopEvidenceRef:
        """Return the exact reference a durable store may publish for these bytes."""

        return StopEvidenceRef(
            kind="transaction_receipt",
            locator=locator,
            sha256=sha256,
            record_schema=self.SCHEMA,
            record_digest=self.digest,
        )

    def same_attempt_as(self, other: TransactionReceipt) -> bool:
        if not isinstance(other, TransactionReceipt):
            return False
        return (
            self.idempotency_key,
            self.action_digest,
            self.adapter_id,
            self.authoritative_before_digest,
            self.attempt_evidence_digest,
        ) == (
            other.idempotency_key,
            other.action_digest,
            other.adapter_id,
            other.authoritative_before_digest,
            other.attempt_evidence_digest,
        )

    def assert_successor(self, prior: TransactionReceipt) -> None:
        """Validate this record as the one legal next immutable revision."""

        if not isinstance(prior, TransactionReceipt):
            raise ValueError("receipt predecessor must be TransactionReceipt")
        if prior.phase == "terminal":
            raise ValueError("a terminal transaction receipt is immutable")
        if not self.same_attempt_as(prior):
            raise ValueError("receipt successor does not bind the same exact transaction attempt")
        if self.revision != prior.revision + 1 or self.predecessor_receipt_digest != prior.digest:
            raise ValueError("receipt successor revision/predecessor is not contiguous")
        allowed_phases = {"running"} if prior.phase == "prepared" else {"running", "terminal"}
        if self.phase not in allowed_phases:
            raise ValueError(
                f"receipt phase cannot advance from {prior.phase!r} to {self.phase!r}"
            )
        prior_refs = {ref.digest for ref in prior.operation_refs}
        current_refs = {ref.digest for ref in self.operation_refs}
        if not prior_refs.issubset(current_refs):
            raise ValueError("receipt successor cannot remove an operation reference")
        if not self.spend.is_monotone_from(prior.spend):
            raise ValueError("receipt successor cumulative spend cannot decrease")
        if self.recovery_disposition not in _RECOVERY_SUCCESSORS[prior.recovery_disposition]:
            raise ValueError("receipt successor cannot claim a safer or unrelated recovery disposition")
        if prior.phase == "running" and self.phase == "running":
            progressed = (
                current_refs != prior_refs
                or self.spend != prior.spend
                or self.recovery_disposition != prior.recovery_disposition
            )
            if not progressed:
                raise ValueError("a running receipt revision must add execution identity, spend, or restriction")

    @classmethod
    def prepare(
        cls,
        action: StopAction,
        *,
        adapter_id: str,
        authoritative_before_digest: str,
        attempt_evidence_digest: str,
        recovery_disposition: str,
    ) -> TransactionReceipt:
        key = action_idempotency_key(
            action,
            authoritative_before_digest=authoritative_before_digest,
            attempt_evidence_digest=attempt_evidence_digest,
        )
        return cls(
            idempotency_key=key,
            revision=1,
            action=action,
            adapter_id=adapter_id,
            authoritative_before_digest=authoritative_before_digest,
            attempt_evidence_digest=attempt_evidence_digest,
            phase="prepared",
            predecessor_receipt_digest=None,
            recovery_disposition=recovery_disposition,
            operation_refs=(),
            spend=TransactionSpend.zero(),
            terminal_outcome=None,
            authoritative_after_digest=None,
            commit_marker=None,
            result_evidence=(),
        )

    def running_successor(
        self,
        *,
        additional_operation_refs: tuple[StopEvidenceRef, ...] = (),
        cumulative_spend: TransactionSpend | None = None,
        recovery_disposition: str | None = None,
    ) -> TransactionReceipt:
        candidate = TransactionReceipt(
            idempotency_key=self.idempotency_key,
            revision=self.revision + 1,
            action=self.action,
            adapter_id=self.adapter_id,
            authoritative_before_digest=self.authoritative_before_digest,
            attempt_evidence_digest=self.attempt_evidence_digest,
            phase="running",
            predecessor_receipt_digest=self.digest,
            recovery_disposition=(
                self.recovery_disposition
                if recovery_disposition is None
                else recovery_disposition
            ),
            operation_refs=_merge_refs(
                self.operation_refs,
                additional_operation_refs,
                "TransactionReceipt.operation_refs",
            ),
            spend=cumulative_spend or self.spend,
            terminal_outcome=None,
            authoritative_after_digest=None,
            commit_marker=None,
            result_evidence=(),
        )
        candidate.assert_successor(self)
        return candidate

    def terminal_successor(
        self,
        *,
        terminal_outcome: str,
        authoritative_after_digest: str,
        result_evidence: tuple[StopEvidenceRef, ...],
        commit_marker: StopEvidenceRef | None = None,
        additional_operation_refs: tuple[StopEvidenceRef, ...] = (),
        cumulative_spend: TransactionSpend | None = None,
        recovery_disposition: str | None = None,
    ) -> TransactionReceipt:
        candidate = TransactionReceipt(
            idempotency_key=self.idempotency_key,
            revision=self.revision + 1,
            action=self.action,
            adapter_id=self.adapter_id,
            authoritative_before_digest=self.authoritative_before_digest,
            attempt_evidence_digest=self.attempt_evidence_digest,
            phase="terminal",
            predecessor_receipt_digest=self.digest,
            recovery_disposition=(
                self.recovery_disposition
                if recovery_disposition is None
                else recovery_disposition
            ),
            operation_refs=_merge_refs(
                self.operation_refs,
                additional_operation_refs,
                "TransactionReceipt.operation_refs",
            ),
            spend=cumulative_spend or self.spend,
            terminal_outcome=terminal_outcome,
            authoritative_after_digest=authoritative_after_digest,
            commit_marker=commit_marker,
            result_evidence=result_evidence,
        )
        candidate.assert_successor(self)
        return candidate

    @classmethod
    def from_dict(cls, value: Any, where: str) -> TransactionReceipt:
        names = (
            "idempotency_key",
            "revision",
            "transaction_id",
            "action",
            "action_digest",
            "precondition_digest",
            "postcondition_digest",
            "evaluator_id",
            "adapter_id",
            "authoritative_before_digest",
            "attempt_evidence_digest",
            "phase",
            "predecessor_receipt_digest",
            "recovery_disposition",
            "operation_refs",
            "spend",
            "terminal_outcome",
            "authoritative_after_digest",
            "commit_marker",
            "result_evidence",
            "receipt_digest",
        )
        row = record(value, where, cls.SCHEMA, names)
        candidate = cls(
            idempotency_key=row["idempotency_key"],
            revision=row["revision"],
            action=StopAction.from_dict(row["action"], f"{where}.action"),
            adapter_id=row["adapter_id"],
            authoritative_before_digest=row["authoritative_before_digest"],
            attempt_evidence_digest=row["attempt_evidence_digest"],
            phase=row["phase"],
            predecessor_receipt_digest=row["predecessor_receipt_digest"],
            recovery_disposition=row["recovery_disposition"],
            operation_refs=_refs(row["operation_refs"], f"{where}.operation_refs"),
            spend=TransactionSpend.from_dict(row["spend"], f"{where}.spend"),
            terminal_outcome=row["terminal_outcome"],
            authoritative_after_digest=row["authoritative_after_digest"],
            commit_marker=_optional_ref(row["commit_marker"], f"{where}.commit_marker"),
            result_evidence=_refs(row["result_evidence"], f"{where}.result_evidence"),
        )
        derived = {
            "transaction_id": candidate.transaction_id,
            "action_digest": candidate.action_digest,
            "precondition_digest": candidate.precondition_digest,
            "postcondition_digest": candidate.postcondition_digest,
            "evaluator_id": candidate.evaluator_id,
        }
        for name, expected in derived.items():
            if row[name] != expected:
                raise ValueError(f"{where}.{name} does not match the embedded action")
        require_canonical_digest(
            row["receipt_digest"],
            candidate.digest,
            where,
            "receipt_digest",
        )
        return candidate


def validate_receipt_chain(receipts: tuple[TransactionReceipt, ...]) -> TransactionReceipt:
    """Validate one complete prepared-first chain and return its selected head."""

    if not isinstance(receipts, tuple) or not receipts:
        raise ValueError("transaction receipt chain must be a non-empty tuple")
    if any(not isinstance(receipt, TransactionReceipt) for receipt in receipts):
        raise ValueError("transaction receipt chain must contain TransactionReceipt values")
    if receipts[0].phase != "prepared" or receipts[0].revision != 1:
        raise ValueError("transaction receipt chain must begin with prepared revision 1")
    for prior, current in pairwise(receipts):
        current.assert_successor(prior)
    return receipts[-1]
