"""Strict terminal evaluation and v2 run-status selection matrix."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar, TypeAlias

from vfx_harness.domain.run_ids import require_run_id
from vfx_harness.domain.run_interruption_records import (
    INTERRUPTION_RECEIPT_LOCATOR,
    RunInterruptionReceipt,
)
from vfx_harness.domain.run_lifecycle_primitives import (
    chronological,
    exact_record,
    optional_pair,
    relative_locator,
    timestamp,
)
from vfx_harness.domain.run_owner_claims import RUN_OWNER_CLAIM_LOCATOR, RunOwnerClaim
from vfx_harness.domain.stop_envelope_primitives import (
    canonical_digest,
    require_canonical_digest,
    require_digest,
    require_id,
    require_text,
)
from vfx_harness.domain.stop_envelopes import StopEnvelope

INTERRUPTION_RECEIPT_EVALUATION_SCHEMA = "vfx-harness.interruption-receipt-evaluation/v1"
INTERRUPTION_RECEIPT_EVALUATION_LOCATOR = "reports/interruption-receipt-evaluation.json"
RUN_STATUS_SCHEMA = "vfx-harness.run-status/v2"
RUN_SUMMARY_SCHEMA = "vfx-harness.run-summary/v1"
RUN_SUMMARY_LOCATOR = "reports/summary.json"
STOP_ENVELOPE_LOCATOR = "reports/stop-envelope.json"
RUN_STATES = frozenset({"running", "passed", "dry-run", "failed", "interrupted"})
INTERRUPTION_EVALUATION_STATUSES = frozenset({"satisfied", "failed"})

_STATUS_FIELDS = frozenset(
    {
        "schema",
        "run_id",
        "state",
        "updated_at",
        "exit_code",
        "detail",
        "owner_claim",
        "owner_claim_digest",
        "summary",
        "summary_digest",
        "stop_envelope",
        "stop_envelope_digest",
        "interruption_receipt",
        "interruption_receipt_digest",
        "interruption_evaluation",
        "interruption_evaluation_digest",
        "status_digest",
    }
)


INTERRUPTION_EVALUATION_ISSUES = frozenset(
    {
        "archive_manifest_mismatch",
        "archive_manifest_unreadable",
        "archive_object_mismatch",
        "archive_object_missing",
        "archive_ref_mismatch",
        "authority_changed",
        "authority_observation_mismatch",
        "authority_observation_unreadable",
        "authority_ref_mismatch",
        "owner_claim_mismatch",
        "owner_claim_unverified",
        "owner_loss_mismatch",
        "owner_loss_ref_mismatch",
        "owner_loss_unreadable",
        "owner_ref_mismatch",
        "prior_status_snapshot_mismatch",
        "prior_status_snapshot_unreadable",
        "reconciler_manifest_mismatch",
        "source_identity_mismatch",
        "transcript_bytes_mismatch",
        "transcript_frontier_mismatch",
        "transcript_frontier_ref_mismatch",
        "transcript_frontier_unreadable",
    }
)


def _issue_ids(value: tuple[str, ...], where: str) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise ValueError(f"{where} must be a tuple")
    rows = tuple(require_id(item, f"{where}[{index}]") for index, item in enumerate(value))
    if rows != tuple(sorted(set(rows))):
        raise ValueError(f"{where} must be sorted and unique")
    unknown = sorted(set(rows) - INTERRUPTION_EVALUATION_ISSUES)
    if unknown:
        raise ValueError(f"{where} names issues outside the closed evaluator vocabulary: {unknown}")
    return rows


@dataclass(frozen=True, slots=True)
class InterruptionReceiptEvaluation:
    """Independent evaluation result binding every causal interruption record."""

    SCHEMA: ClassVar[str] = INTERRUPTION_RECEIPT_EVALUATION_SCHEMA

    run_id: str
    receipt_digest: str
    receipt_interrupted_at: str
    owner_claim_digest: str
    owner_ref_digest: str
    authority_observation_digest: str
    authority_ref_digest: str
    archive_manifest_digest: str
    archive_ref_digest: str
    transcript_frontier_digests: tuple[str, ...]
    transcript_frontier_ref_digests: tuple[str, ...]
    owner_loss_observation_digest: str | None
    owner_loss_ref_digest: str | None
    prior_running_status_evidence_digest: str | None
    prior_running_status_snapshot_ref_digest: str | None
    status: str
    issue_ids: tuple[str, ...]
    evaluated_at: str

    def __post_init__(self) -> None:
        require_run_id(self.run_id, "InterruptionReceiptEvaluation.run_id")
        require_digest(
            self.receipt_digest,
            "InterruptionReceiptEvaluation.receipt_digest",
        )
        timestamp(
            self.receipt_interrupted_at,
            "InterruptionReceiptEvaluation.receipt_interrupted_at",
        )
        require_digest(
            self.owner_claim_digest,
            "InterruptionReceiptEvaluation.owner_claim_digest",
        )
        require_digest(
            self.owner_ref_digest,
            "InterruptionReceiptEvaluation.owner_ref_digest",
        )
        require_digest(
            self.authority_observation_digest,
            "InterruptionReceiptEvaluation.authority_observation_digest",
        )
        require_digest(
            self.authority_ref_digest,
            "InterruptionReceiptEvaluation.authority_ref_digest",
        )
        require_digest(
            self.archive_manifest_digest,
            "InterruptionReceiptEvaluation.archive_manifest_digest",
        )
        require_digest(
            self.archive_ref_digest,
            "InterruptionReceiptEvaluation.archive_ref_digest",
        )
        if not isinstance(self.transcript_frontier_digests, tuple) or not isinstance(
            self.transcript_frontier_ref_digests,
            tuple,
        ):
            raise ValueError("interruption evaluation transcript frontier identities must be tuples")
        for index, digest in enumerate(self.transcript_frontier_digests):
            require_digest(
                digest,
                f"InterruptionReceiptEvaluation.transcript_frontier_digests[{index}]",
            )
        for index, digest in enumerate(self.transcript_frontier_ref_digests):
            require_digest(
                digest,
                f"InterruptionReceiptEvaluation.transcript_frontier_ref_digests[{index}]",
            )
        if (
            len(self.transcript_frontier_digests) != len(self.transcript_frontier_ref_digests)
            or len(set(self.transcript_frontier_digests)) != len(self.transcript_frontier_digests)
            or len(set(self.transcript_frontier_ref_digests)) != len(self.transcript_frontier_ref_digests)
        ):
            raise ValueError("interruption evaluation requires unique one-to-one frontier record and ref digests")
        if self.owner_loss_observation_digest is not None:
            require_digest(
                self.owner_loss_observation_digest,
                "InterruptionReceiptEvaluation.owner_loss_observation_digest",
            )
        if self.owner_loss_ref_digest is not None:
            require_digest(
                self.owner_loss_ref_digest,
                "InterruptionReceiptEvaluation.owner_loss_ref_digest",
            )
        if (self.owner_loss_observation_digest is None) is not (self.owner_loss_ref_digest is None):
            raise ValueError("interruption evaluation owner-loss record and ref digests must appear together")
        prior_status_pair = (
            self.prior_running_status_evidence_digest,
            self.prior_running_status_snapshot_ref_digest,
        )
        for index, value in enumerate(prior_status_pair):
            if value is not None:
                require_digest(
                    value,
                    (
                        "InterruptionReceiptEvaluation.prior_running_status_evidence_digest"
                        if index == 0
                        else "InterruptionReceiptEvaluation.prior_running_status_snapshot_ref_digest"
                    ),
                )
        if (prior_status_pair[0] is None) is not (prior_status_pair[1] is None):
            raise ValueError("interruption evaluation prior-status evidence and snapshot ref must appear together")
        if (self.owner_loss_observation_digest is None) is not (prior_status_pair[0] is None):
            raise ValueError("interruption evaluation requires prior running status exactly for owner loss")
        if self.status not in INTERRUPTION_EVALUATION_STATUSES:
            raise ValueError(
                f"InterruptionReceiptEvaluation.status must be one of {sorted(INTERRUPTION_EVALUATION_STATUSES)}"
            )
        issues = _issue_ids(
            self.issue_ids,
            "InterruptionReceiptEvaluation.issue_ids",
        )
        object.__setattr__(self, "issue_ids", issues)
        if self.status == "satisfied" and issues:
            raise ValueError("a satisfied interruption evaluation cannot contain issues")
        if self.status == "failed" and not issues:
            raise ValueError("a failed interruption evaluation must name at least one issue")
        timestamp(self.evaluated_at, "InterruptionReceiptEvaluation.evaluated_at")
        chronological(
            self.receipt_interrupted_at,
            self.evaluated_at,
            "interruption receipt/evaluation",
        )

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "run_id": self.run_id,
            "receipt_digest": self.receipt_digest,
            "receipt_interrupted_at": self.receipt_interrupted_at,
            "owner_claim_digest": self.owner_claim_digest,
            "owner_ref_digest": self.owner_ref_digest,
            "authority_observation_digest": self.authority_observation_digest,
            "authority_ref_digest": self.authority_ref_digest,
            "archive_manifest_digest": self.archive_manifest_digest,
            "archive_ref_digest": self.archive_ref_digest,
            "transcript_frontier_digests": list(self.transcript_frontier_digests),
            "transcript_frontier_ref_digests": list(self.transcript_frontier_ref_digests),
            "owner_loss_observation_digest": self.owner_loss_observation_digest,
            "owner_loss_ref_digest": self.owner_loss_ref_digest,
            "prior_running_status_evidence_digest": self.prior_running_status_evidence_digest,
            "prior_running_status_snapshot_ref_digest": self.prior_running_status_snapshot_ref_digest,
            "status": self.status,
            "issue_ids": list(self.issue_ids),
            "evaluated_at": self.evaluated_at,
        }

    @property
    def digest(self) -> str:
        return canonical_digest(self._payload())

    def as_dict(self) -> dict[str, Any]:
        return {**self._payload(), "evaluation_digest": self.digest}

    @classmethod
    def from_dict(
        cls,
        value: object,
        where: str = "interruption receipt evaluation",
        *,
        receipt: RunInterruptionReceipt | None = None,
    ) -> InterruptionReceiptEvaluation:
        fields = frozenset(
            {
                "run_id",
                "receipt_digest",
                "receipt_interrupted_at",
                "owner_claim_digest",
                "owner_ref_digest",
                "authority_observation_digest",
                "authority_ref_digest",
                "archive_manifest_digest",
                "archive_ref_digest",
                "transcript_frontier_digests",
                "transcript_frontier_ref_digests",
                "owner_loss_observation_digest",
                "owner_loss_ref_digest",
                "prior_running_status_evidence_digest",
                "prior_running_status_snapshot_ref_digest",
                "status",
                "issue_ids",
                "evaluated_at",
                "evaluation_digest",
            }
        )
        row = exact_record(value, where, cls.SCHEMA, fields)
        raw_frontiers = row["transcript_frontier_digests"]
        raw_frontier_refs = row["transcript_frontier_ref_digests"]
        raw_issues = row["issue_ids"]
        if not isinstance(raw_frontiers, list):
            raise ValueError(f"{where}.transcript_frontier_digests must be a list")
        if not isinstance(raw_frontier_refs, list):
            raise ValueError(f"{where}.transcript_frontier_ref_digests must be a list")
        if not isinstance(raw_issues, list):
            raise ValueError(f"{where}.issue_ids must be a list")
        candidate = cls(
            run_id=row["run_id"],
            receipt_digest=row["receipt_digest"],
            receipt_interrupted_at=row["receipt_interrupted_at"],
            owner_claim_digest=row["owner_claim_digest"],
            owner_ref_digest=row["owner_ref_digest"],
            authority_observation_digest=row["authority_observation_digest"],
            authority_ref_digest=row["authority_ref_digest"],
            archive_manifest_digest=row["archive_manifest_digest"],
            archive_ref_digest=row["archive_ref_digest"],
            transcript_frontier_digests=tuple(raw_frontiers),
            transcript_frontier_ref_digests=tuple(raw_frontier_refs),
            owner_loss_observation_digest=row["owner_loss_observation_digest"],
            owner_loss_ref_digest=row["owner_loss_ref_digest"],
            prior_running_status_evidence_digest=row["prior_running_status_evidence_digest"],
            prior_running_status_snapshot_ref_digest=row["prior_running_status_snapshot_ref_digest"],
            status=row["status"],
            issue_ids=tuple(raw_issues),
            evaluated_at=row["evaluated_at"],
        )
        require_canonical_digest(
            row["evaluation_digest"],
            candidate.digest,
            where,
            "evaluation_digest",
        )
        if receipt is not None:
            require_interruption_evaluation_binds_receipt(candidate, receipt)
        return candidate


def interruption_evaluation_receipt_binding(
    receipt: RunInterruptionReceipt,
) -> dict[str, Any]:
    """Return only the receipt-derived fields of an evaluation.

    This helper deliberately has no status, issues, or evaluation time.  It supports
    structural parsing and the future independent evaluator without allowing a caller
    to select a successful outcome through a domain factory.
    """

    if not isinstance(receipt, RunInterruptionReceipt):
        raise ValueError("interruption evaluation binding requires the exact typed receipt")
    return {
        "run_id": receipt.run_id,
        "receipt_digest": receipt.digest,
        "receipt_interrupted_at": receipt.interrupted_at,
        "owner_claim_digest": receipt.owner.digest,
        "owner_ref_digest": receipt.owner_ref.digest,
        "authority_observation_digest": receipt.authority.digest,
        "authority_ref_digest": receipt.authority_ref.digest,
        "archive_manifest_digest": receipt.archive.digest,
        "archive_ref_digest": receipt.archive_ref.digest,
        "transcript_frontier_digests": tuple(row.digest for row in receipt.transcript_frontiers),
        "transcript_frontier_ref_digests": tuple(row.digest for row in receipt.transcript_frontier_refs),
        "owner_loss_observation_digest": (None if receipt.owner_loss is None else receipt.owner_loss.digest),
        "owner_loss_ref_digest": (None if receipt.owner_loss_ref is None else receipt.owner_loss_ref.digest),
        "prior_running_status_evidence_digest": (
            None if receipt.owner_loss is None else receipt.owner_loss.prior_running_status.digest
        ),
        "prior_running_status_snapshot_ref_digest": (
            None if receipt.owner_loss is None else receipt.owner_loss.prior_running_status.status_snapshot_ref.digest
        ),
    }


def require_interruption_evaluation_binds_receipt(
    evaluation: InterruptionReceiptEvaluation,
    receipt: RunInterruptionReceipt,
) -> None:
    """Require exact receipt identity without deciding the evaluation outcome."""

    if not isinstance(evaluation, InterruptionReceiptEvaluation):
        raise ValueError("interruption evaluation binding requires a typed evaluation")
    expected = interruption_evaluation_receipt_binding(receipt)
    observed = {name: getattr(evaluation, name) for name in expected}
    if observed != expected:
        raise ValueError("interruption evaluation does not bind the exact receipt closure")


SelectedRunRecord: TypeAlias = RunOwnerClaim | StopEnvelope | RunInterruptionReceipt | Mapping[str, Any]


def _summary_digest(
    value: Mapping[str, Any],
    run_id: str,
    state: str,
    exit_code: int,
    command: str,
) -> str:
    if value.get("schema") != RUN_SUMMARY_SCHEMA:
        raise ValueError(f"run summary schema must be {RUN_SUMMARY_SCHEMA!r}")
    if value.get("run_id") != run_id:
        raise ValueError("run summary names another run")
    if value.get("state") != state:
        raise ValueError("run summary state does not match terminal run state")
    if value.get("command") != command:
        raise ValueError("run summary command does not match the root owner claim")
    summary_exit_code = value.get("exit_code")
    if not isinstance(summary_exit_code, int) or isinstance(summary_exit_code, bool):
        raise ValueError("run summary exit_code must be a non-Boolean integer")
    if summary_exit_code != exit_code:
        raise ValueError("run summary exit_code does not match terminal run state")
    return canonical_digest(value)


@dataclass(frozen=True, slots=True)
class RunStatusV2:
    """Strict status whose state selects only its permitted authoritative records."""

    SCHEMA: ClassVar[str] = RUN_STATUS_SCHEMA

    run_id: str
    state: str
    updated_at: str
    exit_code: int | None
    detail: str | None
    owner_claim: str | None
    owner_claim_digest: str | None
    summary: str | None
    summary_digest: str | None
    stop_envelope: str | None
    stop_envelope_digest: str | None
    interruption_receipt: str | None
    interruption_receipt_digest: str | None
    interruption_evaluation: str | None
    interruption_evaluation_digest: str | None

    def __post_init__(self) -> None:
        require_run_id(self.run_id, "RunStatusV2.run_id")
        if self.state not in RUN_STATES:
            raise ValueError(f"RunStatusV2.state must be one of {sorted(RUN_STATES)}")
        timestamp(self.updated_at, "RunStatusV2.updated_at")
        if self.detail is not None:
            require_text(self.detail, "RunStatusV2.detail")
        pairs = {
            "owner_claim": optional_pair(
                self.owner_claim,
                self.owner_claim_digest,
                "RunStatusV2.owner_claim",
            ),
            "summary": optional_pair(
                self.summary,
                self.summary_digest,
                "RunStatusV2.summary",
            ),
            "stop_envelope": optional_pair(
                self.stop_envelope,
                self.stop_envelope_digest,
                "RunStatusV2.stop_envelope",
            ),
            "interruption_receipt": optional_pair(
                self.interruption_receipt,
                self.interruption_receipt_digest,
                "RunStatusV2.interruption_receipt",
            ),
            "interruption_evaluation": optional_pair(
                self.interruption_evaluation,
                self.interruption_evaluation_digest,
                "RunStatusV2.interruption_evaluation",
            ),
        }
        expected = {
            "running": {"owner_claim"},
            "passed": {"owner_claim", "summary"},
            "dry-run": {"owner_claim", "summary"},
            "failed": {"owner_claim", "stop_envelope"},
            "interrupted": {
                "owner_claim",
                "interruption_receipt",
                "interruption_evaluation",
            },
        }[self.state]
        present = {name for name, pair in pairs.items() if pair[0] is not None}
        if present != expected:
            raise ValueError(f"run state {self.state!r} requires only {sorted(expected)}; found={sorted(present)}")
        if self.owner_claim != RUN_OWNER_CLAIM_LOCATOR:
            raise ValueError(f"run status owner claim locator must be {RUN_OWNER_CLAIM_LOCATOR!r}")
        if self.state == "running":
            if self.exit_code is not None:
                raise ValueError("running status cannot have an exit code")
        elif self.state in {"passed", "dry-run"}:
            if self.exit_code != 0 or isinstance(self.exit_code, bool):
                raise ValueError(f"{self.state} status requires exit code 0")
        elif self.state == "failed":
            if not isinstance(self.exit_code, int) or isinstance(self.exit_code, bool) or self.exit_code == 0:
                raise ValueError("failed status requires a non-zero integer exit code")
        elif self.exit_code is not None and (not isinstance(self.exit_code, int) or isinstance(self.exit_code, bool)):
            raise ValueError("interrupted status exit code must be an integer or null")
        if self.state in {"passed", "dry-run"} and self.summary != RUN_SUMMARY_LOCATOR:
            raise ValueError(f"{self.state} status must select the canonical run summary locator")
        if self.state == "failed" and self.stop_envelope != STOP_ENVELOPE_LOCATOR:
            raise ValueError("failed status must select the canonical stop-envelope locator")
        if self.state == "interrupted":
            if self.interruption_receipt != INTERRUPTION_RECEIPT_LOCATOR:
                raise ValueError("interrupted status must select the canonical interruption receipt locator")
            if self.interruption_evaluation != INTERRUPTION_RECEIPT_EVALUATION_LOCATOR:
                raise ValueError("interrupted status must select the canonical interruption evaluation locator")

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "run_id": self.run_id,
            "state": self.state,
            "updated_at": self.updated_at,
            "exit_code": self.exit_code,
            "detail": self.detail,
            "owner_claim": self.owner_claim,
            "owner_claim_digest": self.owner_claim_digest,
            "summary": self.summary,
            "summary_digest": self.summary_digest,
            "stop_envelope": self.stop_envelope,
            "stop_envelope_digest": self.stop_envelope_digest,
            "interruption_receipt": self.interruption_receipt,
            "interruption_receipt_digest": self.interruption_receipt_digest,
            "interruption_evaluation": self.interruption_evaluation,
            "interruption_evaluation_digest": self.interruption_evaluation_digest,
        }

    @property
    def digest(self) -> str:
        return canonical_digest(self._payload())

    def as_dict(self) -> dict[str, Any]:
        return {**self._payload(), "status_digest": self.digest}

    @classmethod
    def mint(
        cls,
        *,
        run_id: str,
        state: str,
        updated_at: str,
        record_locator: str,
        selected_record: SelectedRunRecord,
        exit_code: int | None = None,
        detail: str | None = None,
        owner: RunOwnerClaim | None = None,
        owner_locator: str | None = None,
        interruption_evaluation_locator: str | None = None,
        interruption_evaluation: InterruptionReceiptEvaluation | None = None,
    ) -> RunStatusV2:
        locator = relative_locator(record_locator, "run status record locator")
        timestamp(updated_at, "run status updated_at")
        pairs: dict[str, str | None] = {
            "owner_claim": None,
            "owner_claim_digest": None,
            "summary": None,
            "summary_digest": None,
            "stop_envelope": None,
            "stop_envelope_digest": None,
            "interruption_receipt": None,
            "interruption_receipt_digest": None,
            "interruption_evaluation": None,
            "interruption_evaluation_digest": None,
        }
        derived_exit = exit_code
        if state == "running":
            if not isinstance(selected_record, RunOwnerClaim):
                raise ValueError("running status requires the exact typed owner claim")
            if owner is not None or owner_locator is not None:
                raise ValueError("running status derives its owner from the selected owner claim")
            if exit_code is not None:
                raise ValueError("running status cannot accept a caller-provided exit code")
            owner = selected_record
            owner_locator = locator
            if owner.run_id != run_id:
                raise ValueError("run owner claim names another run")
            derived_exit = None
        elif state in {"passed", "dry-run"}:
            if not isinstance(selected_record, Mapping):
                raise ValueError(f"{state} status requires the exact run summary")
            if not isinstance(owner, RunOwnerClaim):
                raise ValueError(f"{state} status requires the exact typed root owner claim")
            if exit_code is not None and (exit_code != 0 or isinstance(exit_code, bool)):
                raise ValueError(f"{state} status cannot accept a caller-provided nonzero exit code")
            if locator != RUN_SUMMARY_LOCATOR:
                raise ValueError(f"{state} status must select the canonical run summary locator")
            pairs["summary"] = locator
            pairs["summary_digest"] = _summary_digest(
                selected_record,
                run_id,
                state,
                0,
                owner.command,
            )
            derived_exit = 0
        elif state == "failed":
            if not isinstance(selected_record, StopEnvelope):
                raise ValueError("failed status requires the exact typed StopEnvelope")
            if selected_record.identity.run_id != run_id:
                raise ValueError("failed status StopEnvelope names another run")
            if len(selected_record.actions) != 1:
                raise ValueError("failed status StopEnvelope must contain exactly one action")
            if locator != STOP_ENVELOPE_LOCATOR:
                raise ValueError("failed status must select the canonical stop-envelope locator")
            pairs["stop_envelope"] = locator
            pairs["stop_envelope_digest"] = selected_record.digest
        elif state == "interrupted":
            raise ValueError(
                "interrupted status publication is unavailable until the independent "
                "source-verifying evaluator and terminal selection boundary are implemented"
            )
        else:
            raise ValueError(f"run status state must be one of {sorted(RUN_STATES)}")
        if not isinstance(owner, RunOwnerClaim):
            raise ValueError(f"{state} status requires the exact typed root owner claim")
        if owner.run_id != run_id:
            raise ValueError("run owner claim names another run")
        chronological(
            owner.claimed_at,
            updated_at,
            "run owner/status selection",
        )
        selected_owner_locator = relative_locator(
            owner_locator,
            "run status owner claim locator",
        )
        if selected_owner_locator != RUN_OWNER_CLAIM_LOCATOR:
            raise ValueError(f"run status owner claim locator must be {RUN_OWNER_CLAIM_LOCATOR!r}")
        pairs["owner_claim"] = selected_owner_locator
        pairs["owner_claim_digest"] = owner.digest
        if state != "interrupted" and (
            interruption_evaluation is not None or interruption_evaluation_locator is not None
        ):
            raise ValueError("only interrupted status may select an interruption evaluation")
        return cls(
            run_id=run_id,
            state=state,
            updated_at=updated_at,
            exit_code=derived_exit,
            detail=detail,
            **pairs,
        )

    @classmethod
    def from_dict(
        cls,
        value: object,
        *,
        selected_record: SelectedRunRecord,
        owner: RunOwnerClaim | None = None,
        interruption_evaluation: InterruptionReceiptEvaluation | None = None,
        where: str = "run status",
    ) -> RunStatusV2:
        if not isinstance(value, Mapping):
            raise ValueError(f"{where} must be an object")
        found = set(value)
        if found != _STATUS_FIELDS:
            raise ValueError(
                f"{where} fields mismatch; missing={sorted(_STATUS_FIELDS - found)}; "
                f"unexpected={sorted(found - _STATUS_FIELDS)}"
            )
        if value.get("schema") != cls.SCHEMA:
            raise ValueError(f"{where}.schema must be {cls.SCHEMA!r}")
        state = value.get("state")
        if state == "interrupted":
            raise ValueError(
                f"{where} interrupted authority is unavailable until its independent "
                "source-verifying reader is implemented"
            )
        locator_field = {
            "running": "owner_claim",
            "passed": "summary",
            "dry-run": "summary",
            "failed": "stop_envelope",
            "interrupted": "interruption_receipt",
        }.get(state)
        if locator_field is None:
            raise ValueError(f"{where}.state must be one of {sorted(RUN_STATES)}")
        candidate = cls.mint(
            run_id=value.get("run_id"),
            state=state,
            updated_at=value.get("updated_at"),
            record_locator=value.get(locator_field),
            selected_record=selected_record,
            exit_code=value.get("exit_code"),
            detail=value.get("detail"),
            owner=owner,
            owner_locator=(None if state == "running" else value.get("owner_claim")),
            interruption_evaluation_locator=value.get("interruption_evaluation"),
            interruption_evaluation=interruption_evaluation,
        )
        if candidate.as_dict() != dict(value):
            require_canonical_digest(
                value.get("status_digest"),
                candidate.digest,
                where,
                "status_digest",
            )
            raise ValueError(f"{where} does not match its selected record and state matrix")
        return candidate
