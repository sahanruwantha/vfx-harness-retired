"""Strict content-addressed outcomes for the finished-chain acceptance boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from vfx_harness.domain.stop_envelope_primitives import (
    canonical_digest,
    list_value,
    record,
    require_canonical_digest,
    require_digest,
    require_id,
)

ACCEPTANCE_DECISION_KINDS = frozenset({"critic", "metrics", "no_optical_signal"})


@dataclass(frozen=True, slots=True)
class AcceptanceMomentOutcome:
    """One selected approval moment bound to its exact acceptance evidence."""

    SCHEMA: ClassVar[str] = "vfx-harness.acceptance-moment-outcome/v1"

    moment_id: str
    passed: bool
    decided_by: str
    evidence_digest: str

    def __post_init__(self) -> None:
        require_id(self.moment_id, "AcceptanceMomentOutcome.moment_id")
        if not isinstance(self.passed, bool):
            raise ValueError("AcceptanceMomentOutcome.passed must be a boolean")
        if self.decided_by not in ACCEPTANCE_DECISION_KINDS:
            raise ValueError(
                "AcceptanceMomentOutcome.decided_by must be one of "
                f"{sorted(ACCEPTANCE_DECISION_KINDS)}"
            )
        require_digest(self.evidence_digest, "AcceptanceMomentOutcome.evidence_digest")

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "moment_id": self.moment_id,
            "passed": self.passed,
            "decided_by": self.decided_by,
            "evidence_digest": self.evidence_digest,
        }

    @property
    def digest(self) -> str:
        return canonical_digest(self._payload())

    def as_dict(self) -> dict[str, Any]:
        return {**self._payload(), "moment_outcome_digest": self.digest}

    @classmethod
    def from_dict(cls, value: Any, where: str) -> AcceptanceMomentOutcome:
        row = record(
            value,
            where,
            cls.SCHEMA,
            (
                "moment_id",
                "passed",
                "decided_by",
                "evidence_digest",
                "moment_outcome_digest",
            ),
        )
        candidate = cls(
            moment_id=row["moment_id"],
            passed=row["passed"],
            decided_by=row["decided_by"],
            evidence_digest=row["evidence_digest"],
        )
        require_canonical_digest(
            row["moment_outcome_digest"],
            candidate.digest,
            where,
            "moment_outcome_digest",
        )
        return candidate


@dataclass(frozen=True, slots=True)
class AcceptanceOutcome:
    """Complete acceptance result for one exact selected authority and build chain."""

    SCHEMA: ClassVar[str] = "vfx-harness.acceptance-outcome/v1"

    authority_digest: str
    bundle_digest: str
    view_digest: str
    chain_digest: str
    moments: tuple[AcceptanceMomentOutcome, ...]

    def __post_init__(self) -> None:
        for name in (
            "authority_digest",
            "bundle_digest",
            "view_digest",
            "chain_digest",
        ):
            require_digest(getattr(self, name), f"AcceptanceOutcome.{name}")
        if not isinstance(self.moments, tuple) or not self.moments:
            raise ValueError("AcceptanceOutcome.moments must be a non-empty tuple")
        if any(not isinstance(moment, AcceptanceMomentOutcome) for moment in self.moments):
            raise ValueError(
                "AcceptanceOutcome.moments must contain AcceptanceMomentOutcome values"
            )
        ids = [moment.moment_id for moment in self.moments]
        if len(ids) != len(set(ids)):
            raise ValueError("AcceptanceOutcome.moments contains duplicate moment ids")

    @property
    def passed(self) -> bool:
        return all(moment.passed for moment in self.moments)

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "authority_digest": self.authority_digest,
            "bundle_digest": self.bundle_digest,
            "view_digest": self.view_digest,
            "chain_digest": self.chain_digest,
            "passed": self.passed,
            "total": len(self.moments),
            "moments": [moment.as_dict() for moment in self.moments],
        }

    @property
    def digest(self) -> str:
        return canonical_digest(self._payload())

    def as_dict(self) -> dict[str, Any]:
        return {**self._payload(), "outcome_digest": self.digest}

    @classmethod
    def from_dict(cls, value: Any, where: str) -> AcceptanceOutcome:
        row = record(
            value,
            where,
            cls.SCHEMA,
            (
                "authority_digest",
                "bundle_digest",
                "view_digest",
                "chain_digest",
                "passed",
                "total",
                "moments",
                "outcome_digest",
            ),
        )
        moments = tuple(
            AcceptanceMomentOutcome.from_dict(item, f"{where}.moments[{index}]")
            for index, item in enumerate(list_value(row["moments"], f"{where}.moments"))
        )
        candidate = cls(
            authority_digest=row["authority_digest"],
            bundle_digest=row["bundle_digest"],
            view_digest=row["view_digest"],
            chain_digest=row["chain_digest"],
            moments=moments,
        )
        if row["passed"] is not candidate.passed:
            raise ValueError(f"{where}.passed is inconsistent with its moment outcomes")
        if isinstance(row["total"], bool) or row["total"] != len(candidate.moments):
            raise ValueError(f"{where}.total is inconsistent with its moment outcomes")
        require_canonical_digest(row["outcome_digest"], candidate.digest, where, "outcome_digest")
        return candidate
