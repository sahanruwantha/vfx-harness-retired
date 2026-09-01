"""Immutable evidence for the exact running status replaced by reconciliation.

This module deliberately does not import :mod:`vfx_harness.domain.run_status`.
Owner-loss reconciliation needs to validate the previous status while the status
module will eventually select interruption records that include this evidence.
Keeping the wire projection here dependency-light prevents that relationship from
becoming an import cycle.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

from vfx_harness.domain.run_ids import require_run_id
from vfx_harness.domain.run_lifecycle_primitives import (
    chronological,
    exact_record,
    relative_locator,
    timestamp,
)
from vfx_harness.domain.run_owner_claims import RUN_OWNER_CLAIM_LOCATOR, RunOwnerClaim
from vfx_harness.domain.run_record_refs import RunRecordRef
from vfx_harness.domain.stop_envelope_primitives import (
    canonical_digest,
    require_canonical_digest,
    require_digest,
    require_text,
)

PRIOR_RUNNING_STATUS_EVIDENCE_SCHEMA = "vfx-harness.prior-running-status-evidence/v1"
RUN_STATUS_V2_SCHEMA = "vfx-harness.run-status/v2"
RUNNING_STATUS_SOURCE_LOCATOR = "status.json"
RUNNING_STATUS_SNAPSHOT_DIRECTORY = "reports/prior-running-status"

_RUNNING_STATUS_FIELDS = frozenset(
    {
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


class _DuplicateJsonKey(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateJsonKey(key)
        value[key] = item
    return value


def _reject_non_finite(value: str) -> None:
    raise ValueError(f"non-finite JSON value {value!r} is forbidden")


def _decode_status_bytes(payload: bytes, where: str) -> Mapping[str, Any]:
    if not isinstance(payload, bytes):
        raise ValueError(f"{where} must be exact immutable bytes")
    try:
        text = payload.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite,
        )
    except _DuplicateJsonKey as exc:
        raise ValueError(f"{where} contains duplicate JSON key {exc.args[0]!r}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"{where} must contain one finite UTF-8 JSON object") from exc
    return exact_record(
        value,
        where,
        RUN_STATUS_V2_SCHEMA,
        _RUNNING_STATUS_FIELDS,
    )


@dataclass(frozen=True, slots=True)
class _RunningStatusProjection:
    run_id: str
    updated_at: str
    owner_claim_locator: str
    owner_claim_digest: str
    status_digest: str


def _running_status_projection(payload: bytes, where: str) -> _RunningStatusProjection:
    row = _decode_status_bytes(payload, where)
    run_id = require_run_id(row["run_id"], f"{where}.run_id")
    if row["state"] != "running":
        raise ValueError(f"{where}.state must be 'running'")
    updated_at = timestamp(row["updated_at"], f"{where}.updated_at")
    if row["exit_code"] is not None:
        raise ValueError(f"{where}.exit_code must be null while running")
    if row["detail"] is not None:
        require_text(row["detail"], f"{where}.detail")

    owner_claim_locator = relative_locator(
        row["owner_claim"],
        f"{where}.owner_claim",
    )
    owner_claim_digest = require_digest(
        row["owner_claim_digest"],
        f"{where}.owner_claim_digest",
    )
    terminal_fields = (
        "summary",
        "summary_digest",
        "stop_envelope",
        "stop_envelope_digest",
        "interruption_receipt",
        "interruption_receipt_digest",
        "interruption_evaluation",
        "interruption_evaluation_digest",
    )
    present = [field for field in terminal_fields if row[field] is not None]
    if present:
        raise ValueError(f"{where} running state permits only owner_claim authority; found terminal fields={present}")

    expected_digest = canonical_digest({key: value for key, value in row.items() if key != "status_digest"})
    require_canonical_digest(
        row["status_digest"],
        expected_digest,
        where,
        "status_digest",
    )
    return _RunningStatusProjection(
        run_id=run_id,
        updated_at=updated_at,
        owner_claim_locator=owner_claim_locator,
        owner_claim_digest=owner_claim_digest,
        status_digest=expected_digest,
    )


def running_status_snapshot_locator(status_sha256: str) -> str:
    """Return the sole content-addressed locator for copied running-status bytes."""

    digest = require_digest(
        status_sha256,
        "running status snapshot sha256",
    )
    return f"{RUNNING_STATUS_SNAPSHOT_DIRECTORY}/{digest}.json"


@dataclass(frozen=True, slots=True)
class PriorRunningStatusEvidence:
    """Source-bound proof of the exact running status seen before replacement.

    ``source_status_locator`` names the mutable status head that was observed.
    ``status_snapshot_ref`` names a create-only, content-addressed copy of those
    exact bytes.  Consumers must reopen that reference and call
    :meth:`require_source_bytes` before treating the evidence as satisfied.
    """

    SCHEMA: ClassVar[str] = PRIOR_RUNNING_STATUS_EVIDENCE_SCHEMA

    run_id: str
    source_status_locator: str
    status_snapshot_ref: RunRecordRef
    status_digest: str
    status_updated_at: str
    owner_claim_locator: str
    owner_claim_digest: str
    captured_at: str

    def __post_init__(self) -> None:
        require_run_id(self.run_id, "PriorRunningStatusEvidence.run_id")
        source = relative_locator(
            self.source_status_locator,
            "PriorRunningStatusEvidence.source_status_locator",
        )
        if source != RUNNING_STATUS_SOURCE_LOCATOR:
            raise ValueError("PriorRunningStatusEvidence.source_status_locator must be 'status.json'")
        if not isinstance(self.status_snapshot_ref, RunRecordRef):
            raise ValueError("PriorRunningStatusEvidence.status_snapshot_ref must be RunRecordRef")
        expected_snapshot_locator = running_status_snapshot_locator(self.status_snapshot_ref.sha256)
        if self.status_snapshot_ref.locator != expected_snapshot_locator:
            raise ValueError(
                "PriorRunningStatusEvidence.status_snapshot_ref must use the exact "
                f"content-addressed locator {expected_snapshot_locator!r}"
            )
        status_digest = require_digest(
            self.status_digest,
            "PriorRunningStatusEvidence.status_digest",
        )
        self.status_snapshot_ref.require_record(
            schema=RUN_STATUS_V2_SCHEMA,
            digest=status_digest,
            where="PriorRunningStatusEvidence.status_snapshot_ref",
        )
        owner_locator = relative_locator(
            self.owner_claim_locator,
            "PriorRunningStatusEvidence.owner_claim_locator",
        )
        if owner_locator != RUN_OWNER_CLAIM_LOCATOR:
            raise ValueError(
                "PriorRunningStatusEvidence.owner_claim_locator must name the canonical "
                f"owner claim {RUN_OWNER_CLAIM_LOCATOR!r}"
            )
        require_digest(
            self.owner_claim_digest,
            "PriorRunningStatusEvidence.owner_claim_digest",
        )
        timestamp(
            self.status_updated_at,
            "PriorRunningStatusEvidence.status_updated_at",
        )
        timestamp(self.captured_at, "PriorRunningStatusEvidence.captured_at")
        chronological(
            self.status_updated_at,
            self.captured_at,
            "prior running status/capture",
        )

    @classmethod
    def mint(
        cls,
        *,
        status_bytes: bytes,
        owner: RunOwnerClaim,
        captured_at: str,
    ) -> PriorRunningStatusEvidence:
        if not isinstance(owner, RunOwnerClaim):
            raise ValueError("prior running status evidence requires the exact typed owner claim")
        projection = _running_status_projection(
            status_bytes,
            "prior running status source",
        )
        if projection.run_id != owner.run_id:
            raise ValueError("prior running status source names another run")
        if projection.owner_claim_digest != owner.digest:
            raise ValueError("prior running status source does not select the exact owner claim")
        chronological(
            owner.claimed_at,
            projection.updated_at,
            "run owner/prior running status",
        )
        source_sha256 = hashlib.sha256(status_bytes).hexdigest()
        return cls(
            run_id=projection.run_id,
            source_status_locator=RUNNING_STATUS_SOURCE_LOCATOR,
            status_snapshot_ref=RunRecordRef(
                locator=running_status_snapshot_locator(source_sha256),
                sha256=source_sha256,
                record_schema=RUN_STATUS_V2_SCHEMA,
                record_digest=projection.status_digest,
            ),
            status_digest=projection.status_digest,
            status_updated_at=projection.updated_at,
            owner_claim_locator=projection.owner_claim_locator,
            owner_claim_digest=projection.owner_claim_digest,
            captured_at=captured_at,
        )

    def require_source_bytes(
        self,
        status_bytes: bytes,
        *,
        owner: RunOwnerClaim | None = None,
    ) -> None:
        """Reopen and verify the exact copied bytes and optional owner source."""

        if not isinstance(status_bytes, bytes):
            raise ValueError("prior running status source must be exact immutable bytes")
        observed_sha256 = hashlib.sha256(status_bytes).hexdigest()
        if observed_sha256 != self.status_snapshot_ref.sha256:
            raise ValueError("prior running status snapshot bytes do not match their exact source reference")
        projection = _running_status_projection(
            status_bytes,
            "prior running status snapshot",
        )
        observed = (
            projection.run_id,
            projection.updated_at,
            projection.owner_claim_locator,
            projection.owner_claim_digest,
            projection.status_digest,
        )
        expected = (
            self.run_id,
            self.status_updated_at,
            self.owner_claim_locator,
            self.owner_claim_digest,
            self.status_digest,
        )
        if observed != expected:
            raise ValueError("prior running status evidence does not bind the exact copied status record")
        if owner is not None:
            self.require_matches_owner(owner)

    def require_matches_owner(self, owner: RunOwnerClaim) -> None:
        if not isinstance(owner, RunOwnerClaim):
            raise ValueError("prior running status verification requires a typed owner claim")
        if (self.run_id, self.owner_claim_digest) != (owner.run_id, owner.digest):
            raise ValueError("prior running status evidence does not select the exact owner claim")
        chronological(
            owner.claimed_at,
            self.status_updated_at,
            "run owner/prior running status",
        )

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "run_id": self.run_id,
            "source_status_locator": self.source_status_locator,
            "status_snapshot_ref": self.status_snapshot_ref.as_dict(),
            "status_digest": self.status_digest,
            "status_updated_at": self.status_updated_at,
            "owner_claim_locator": self.owner_claim_locator,
            "owner_claim_digest": self.owner_claim_digest,
            "captured_at": self.captured_at,
        }

    @property
    def digest(self) -> str:
        return canonical_digest(self._payload())

    def as_dict(self) -> dict[str, Any]:
        return {**self._payload(), "evidence_digest": self.digest}

    @classmethod
    def from_dict(
        cls,
        value: object,
        where: str = "prior running status evidence",
        *,
        status_bytes: bytes | None = None,
        owner: RunOwnerClaim | None = None,
    ) -> PriorRunningStatusEvidence:
        fields = frozenset(
            {
                "run_id",
                "source_status_locator",
                "status_snapshot_ref",
                "status_digest",
                "status_updated_at",
                "owner_claim_locator",
                "owner_claim_digest",
                "captured_at",
                "evidence_digest",
            }
        )
        row = exact_record(value, where, cls.SCHEMA, fields)
        candidate = cls(
            run_id=row["run_id"],
            source_status_locator=row["source_status_locator"],
            status_snapshot_ref=RunRecordRef.from_dict(
                row["status_snapshot_ref"],
                f"{where}.status_snapshot_ref",
            ),
            status_digest=row["status_digest"],
            status_updated_at=row["status_updated_at"],
            owner_claim_locator=row["owner_claim_locator"],
            owner_claim_digest=row["owner_claim_digest"],
            captured_at=row["captured_at"],
        )
        require_canonical_digest(
            row["evidence_digest"],
            candidate.digest,
            where,
            "evidence_digest",
        )
        if status_bytes is not None:
            candidate.require_source_bytes(status_bytes, owner=owner)
        elif owner is not None:
            candidate.require_matches_owner(owner)
        return candidate
