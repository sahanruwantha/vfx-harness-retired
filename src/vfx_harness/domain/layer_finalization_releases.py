"""Typed reviewed release of one orphaned pre-terminal layer claim."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from vfx_harness.domain.layer_finalizations import (
    LAYER_REPLAY_RECEIPT_SCHEMA,
    LayerFinalizationClaim,
)
from vfx_harness.domain.stop_envelope_primitives import require_digest

LAYER_FINALIZATION_RELEASE_REQUEST_SCHEMA = (
    "vfx-harness.layer-finalization-release-request/v2"
)
LAYER_FINALIZATION_RELEASE_RECEIPT_SCHEMA = (
    "vfx-harness.layer-finalization-release-receipt/v2"
)
LAYER_FINALIZATION_RELEASE_EVIDENCE_SCHEMA = (
    "vfx-harness.layer-finalization-release-evidence/v2"
)
LAYER_FINALIZATION_RELEASE_OUTCOME = "released_for_fresh_claim"
UNSEALED_JUDGMENT_DISPOSITION = "unsealed_not_reusable"
LAYER_FINALIZATION_RELEASE_REFERENCE_PREFIX = "layer-finalization-release"
LAYER_FINALIZATION_REVIEW_EVIDENCE_DIRECTORY = Path(
    "state/layer-finalization-releases/evidence"
)

_EVIDENCE_KINDS = frozenset({"review_evidence", "layer_replay_receipt"})


def _text(value: object, where: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{where} must be a non-empty trimmed string")
    return value


def _relative_path(value: object, where: str) -> str:
    text = _text(value, where)
    path = Path(text)
    if (
        path == Path(".")
        or path.is_absolute()
        or ".." in path.parts
        or path.as_posix() != text
    ):
        raise ValueError(f"{where} must be a canonical shot-relative path")
    return text


def _semantic_digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def layer_finalization_review_evidence_locator(sha256: object) -> str:
    """Return the sole immutable locator for reviewed evidence bytes."""

    digest = require_digest(
        sha256,
        "layer finalization review evidence SHA-256",
    )
    return (LAYER_FINALIZATION_REVIEW_EVIDENCE_DIRECTORY / digest).as_posix()


@dataclass(frozen=True, slots=True)
class LayerFinalizationReleaseEvidence:
    """One descriptor-read file reference retained by the reviewed release."""

    SCHEMA: ClassVar[str] = LAYER_FINALIZATION_RELEASE_EVIDENCE_SCHEMA

    kind: str
    locator: str
    sha256: str
    source_locator: str | None = None
    record_schema: str | None = None
    record_digest: str | None = None
    group_index: int | None = None
    planned_group_count: int | None = None

    def __post_init__(self) -> None:
        if self.kind not in _EVIDENCE_KINDS:
            raise ValueError(
                "layer finalization release evidence kind must be one of "
                f"{sorted(_EVIDENCE_KINDS)}"
            )
        _relative_path(self.locator, "layer finalization release evidence locator")
        require_digest(
            self.sha256,
            "layer finalization release evidence SHA-256",
        )
        if self.kind == "layer_replay_receipt":
            if self.source_locator is not None:
                raise ValueError(
                    "layer replay release evidence cannot name a mutable source locator"
                )
            if self.record_schema != LAYER_REPLAY_RECEIPT_SCHEMA:
                raise ValueError(
                    "layer replay release evidence must name the replay receipt schema"
                )
            require_digest(
                self.record_digest,
                "layer replay release evidence record digest",
            )
            if (
                not isinstance(self.group_index, int)
                or isinstance(self.group_index, bool)
                or self.group_index < 0
            ):
                raise ValueError(
                    "layer replay release evidence group_index must be a "
                    "non-negative integer"
                )
            if (
                not isinstance(self.planned_group_count, int)
                or isinstance(self.planned_group_count, bool)
                or self.planned_group_count < 1
                or self.group_index >= self.planned_group_count
            ):
                raise ValueError(
                    "layer replay release evidence planned_group_count must "
                    "contain group_index"
                )
        else:
            _relative_path(
                self.source_locator,
                "layer finalization release review source locator",
            )
            if self.locator != layer_finalization_review_evidence_locator(
                self.sha256
            ):
                raise ValueError(
                    "release review evidence locator must be the exact content-addressed "
                    "snapshot path"
                )
            if (
                self.record_schema is not None
                or self.record_digest is not None
                or self.group_index is not None
                or self.planned_group_count is not None
            ):
                raise ValueError(
                    "ordinary release review evidence cannot claim a typed record identity"
                )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "kind": self.kind,
            "locator": self.locator,
            "sha256": self.sha256,
            "source_locator": self.source_locator,
            "record_schema": self.record_schema,
            "record_digest": self.record_digest,
            "group_index": self.group_index,
            "planned_group_count": self.planned_group_count,
        }

    @classmethod
    def from_dict(
        cls,
        value: object,
        where: str = "layer finalization release evidence",
    ) -> LayerFinalizationReleaseEvidence:
        fields = {
            "schema",
            "kind",
            "locator",
            "sha256",
            "source_locator",
            "record_schema",
            "record_digest",
            "group_index",
            "planned_group_count",
        }
        if not isinstance(value, Mapping) or set(value) != fields:
            raise ValueError(f"{where} must contain exactly {sorted(fields)}")
        if value.get("schema") != cls.SCHEMA:
            raise ValueError(f"{where}.schema must be {cls.SCHEMA!r}")
        return cls(
            kind=value.get("kind"),
            locator=value.get("locator"),
            sha256=value.get("sha256"),
            source_locator=value.get("source_locator"),
            record_schema=value.get("record_schema"),
            record_digest=value.get("record_digest"),
            group_index=value.get("group_index"),
            planned_group_count=value.get("planned_group_count"),
        )


def _review_evidence(
    value: Iterable[LayerFinalizationReleaseEvidence],
) -> tuple[LayerFinalizationReleaseEvidence, ...]:
    if isinstance(value, (str, bytes, Mapping, set, frozenset)):
        raise ValueError(
            "layer finalization release review evidence must be an ordered iterable"
        )
    rows = tuple(value)
    if not rows or any(
        not isinstance(row, LayerFinalizationReleaseEvidence)
        or row.kind != "review_evidence"
        for row in rows
    ):
        raise ValueError(
            "layer finalization release requires non-empty typed review evidence"
        )
    source_locators = tuple(row.source_locator for row in rows)
    if len(source_locators) != len(set(source_locators)):
        raise ValueError(
            "layer finalization release review evidence contains duplicate source locators"
        )
    if source_locators != tuple(sorted(source_locators)):
        raise ValueError(
            "layer finalization release review evidence must be sorted by source locator"
        )
    return rows


def _replay_receipts(
    value: Iterable[LayerFinalizationReleaseEvidence],
) -> tuple[LayerFinalizationReleaseEvidence, ...]:
    if isinstance(value, (str, bytes, Mapping, set, frozenset)):
        raise ValueError(
            "layer finalization release replay receipts must be an ordered iterable"
        )
    rows = tuple(value)
    if any(
        not isinstance(row, LayerFinalizationReleaseEvidence)
        or row.kind != "layer_replay_receipt"
        for row in rows
    ):
        raise ValueError(
            "layer finalization release replay receipts must be typed replay evidence"
        )
    indices = tuple(row.group_index for row in rows)
    if indices != tuple(range(len(rows))):
        raise ValueError(
            "layer finalization release replay receipts must be the exact ordered "
            "contiguous prefix starting at group zero"
        )
    counts = {row.planned_group_count for row in rows}
    if len(counts) > 1:
        raise ValueError(
            "layer finalization release replay receipts disagree on planned group count"
        )
    if rows and len(rows) > rows[0].planned_group_count:
        raise ValueError(
            "layer finalization release replay receipt prefix exceeds its plan"
        )
    locators = tuple(row.locator for row in rows)
    if len(locators) != len(set(locators)):
        raise ValueError(
            "layer finalization release replay receipts contain duplicate locators"
        )
    return rows


@dataclass(frozen=True, slots=True)
class LayerFinalizationReleaseRequest:
    """Deterministic reviewed authority to release one exact active claim."""

    SCHEMA: ClassVar[str] = LAYER_FINALIZATION_RELEASE_REQUEST_SCHEMA

    release_id: str
    request_digest: str
    claim: LayerFinalizationClaim
    reason: str
    review_evidence: tuple[LayerFinalizationReleaseEvidence, ...]
    replay_receipts: tuple[LayerFinalizationReleaseEvidence, ...]
    judgment_disposition: str

    @classmethod
    def mint(
        cls,
        *,
        claim: LayerFinalizationClaim,
        reason: object,
        review_evidence: Iterable[LayerFinalizationReleaseEvidence],
        replay_receipts: Iterable[LayerFinalizationReleaseEvidence],
    ) -> LayerFinalizationReleaseRequest:
        if not isinstance(claim, LayerFinalizationClaim):
            raise ValueError(
                "layer finalization release request requires the exact typed claim"
            )
        reviewed_reason = _text(reason, "layer finalization release reason")
        evidence = _review_evidence(review_evidence)
        replays = _replay_receipts(replay_receipts)
        identity = {
            "schema": cls.SCHEMA,
            "claim": claim.as_dict(),
            "reason": reviewed_reason,
            "review_evidence": [row.as_dict() for row in evidence],
            "replay_receipts": [row.as_dict() for row in replays],
            "judgment_disposition": UNSEALED_JUDGMENT_DISPOSITION,
        }
        digest = _semantic_digest(identity)
        return cls(
            release_id=f"lfr-{digest}",
            request_digest=digest,
            claim=claim,
            reason=reviewed_reason,
            review_evidence=evidence,
            replay_receipts=replays,
            judgment_disposition=UNSEALED_JUDGMENT_DISPOSITION,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "release_id": self.release_id,
            "request_digest": self.request_digest,
            "claim": self.claim.as_dict(),
            "reason": self.reason,
            "review_evidence": [row.as_dict() for row in self.review_evidence],
            "replay_receipts": [row.as_dict() for row in self.replay_receipts],
            "judgment_disposition": self.judgment_disposition,
        }

    @classmethod
    def from_dict(
        cls,
        value: object,
        where: str = "layer finalization release request",
    ) -> LayerFinalizationReleaseRequest:
        fields = {
            "schema",
            "release_id",
            "request_digest",
            "claim",
            "reason",
            "review_evidence",
            "replay_receipts",
            "judgment_disposition",
        }
        if not isinstance(value, Mapping) or set(value) != fields:
            raise ValueError(f"{where} must contain exactly {sorted(fields)}")
        if value.get("schema") != cls.SCHEMA:
            raise ValueError(f"{where}.schema must be {cls.SCHEMA!r}")
        raw_evidence = value.get("review_evidence")
        if not isinstance(raw_evidence, list):
            raise ValueError(f"{where}.review_evidence must be a list")
        raw_replays = value.get("replay_receipts")
        if not isinstance(raw_replays, list):
            raise ValueError(f"{where}.replay_receipts must be a list")
        request = cls.mint(
            claim=LayerFinalizationClaim.parse(
                value.get("claim"),
                f"{where}.claim",
            ),
            reason=value.get("reason"),
            review_evidence=tuple(
                LayerFinalizationReleaseEvidence.from_dict(
                    row,
                    f"{where}.review_evidence[{index}]",
                )
                for index, row in enumerate(raw_evidence)
            ),
            replay_receipts=tuple(
                LayerFinalizationReleaseEvidence.from_dict(
                    row,
                    f"{where}.replay_receipts[{index}]",
                )
                for index, row in enumerate(raw_replays)
            ),
        )
        if value.get("judgment_disposition") != UNSEALED_JUDGMENT_DISPOSITION:
            raise ValueError(
                f"{where}.judgment_disposition must be "
                f"{UNSEALED_JUDGMENT_DISPOSITION!r}"
            )
        if (
            value.get("release_id") != request.release_id
            or value.get("request_digest") != request.request_digest
        ):
            raise ValueError(f"{where} release identity is stale")
        return request


@dataclass(frozen=True, slots=True)
class LayerFinalizationReleaseReceipt:
    """Immutable proof that the exact pre-terminal claim was released."""

    SCHEMA: ClassVar[str] = LAYER_FINALIZATION_RELEASE_RECEIPT_SCHEMA

    receipt_digest: str
    request: LayerFinalizationReleaseRequest
    outcome: str
    released_at: str

    @classmethod
    def mint(
        cls,
        *,
        request: LayerFinalizationReleaseRequest,
        released_at: object,
    ) -> LayerFinalizationReleaseReceipt:
        if not isinstance(request, LayerFinalizationReleaseRequest):
            raise ValueError(
                "layer finalization release receipt requires a typed request"
            )
        at = _text(released_at, "layer finalization release receipt released_at")
        identity = {
            "schema": cls.SCHEMA,
            "request": request.as_dict(),
            "outcome": LAYER_FINALIZATION_RELEASE_OUTCOME,
            "released_at": at,
        }
        return cls(
            receipt_digest=_semantic_digest(identity),
            request=request,
            outcome=LAYER_FINALIZATION_RELEASE_OUTCOME,
            released_at=at,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "receipt_digest": self.receipt_digest,
            "request": self.request.as_dict(),
            "outcome": self.outcome,
            "released_at": self.released_at,
        }

    @classmethod
    def from_dict(
        cls,
        value: object,
        where: str = "layer finalization release receipt",
    ) -> LayerFinalizationReleaseReceipt:
        fields = {
            "schema",
            "receipt_digest",
            "request",
            "outcome",
            "released_at",
        }
        if not isinstance(value, Mapping) or set(value) != fields:
            raise ValueError(f"{where} must contain exactly {sorted(fields)}")
        if value.get("schema") != cls.SCHEMA:
            raise ValueError(f"{where}.schema must be {cls.SCHEMA!r}")
        if value.get("outcome") != LAYER_FINALIZATION_RELEASE_OUTCOME:
            raise ValueError(
                f"{where}.outcome must be {LAYER_FINALIZATION_RELEASE_OUTCOME!r}"
            )
        receipt = cls.mint(
            request=LayerFinalizationReleaseRequest.from_dict(
                value.get("request"),
                f"{where}.request",
            ),
            released_at=value.get("released_at"),
        )
        if value.get("receipt_digest") != receipt.receipt_digest:
            raise ValueError(f"{where}.receipt_digest does not match its payload")
        return receipt


@dataclass(frozen=True, slots=True)
class LayerFinalizationReleaseReference:
    """Typed content-addressed locator retained by finalization state."""

    locator: str
    sha256: str
    receipt_digest: str

    def __post_init__(self) -> None:
        _relative_path(
            self.locator,
            "layer finalization release receipt locator",
        )
        require_digest(
            self.sha256,
            "layer finalization release receipt file SHA-256",
        )
        require_digest(
            self.receipt_digest,
            "layer finalization release receipt semantic digest",
        )

    def as_archive_evidence(self) -> str:
        """Encode the reference for the immutable claim-history evidence list."""

        return ":".join(
            (
                LAYER_FINALIZATION_RELEASE_REFERENCE_PREFIX,
                self.receipt_digest,
                self.sha256,
                self.locator,
            )
        )

    @classmethod
    def from_archive_evidence(
        cls,
        value: object,
    ) -> LayerFinalizationReleaseReference:
        """Parse one exact release receipt reference from claim history."""

        if not isinstance(value, str):
            raise ValueError(
                "layer finalization release archive reference must be a string"
            )
        parts = value.split(":", 3)
        if (
            len(parts) != 4
            or parts[0] != LAYER_FINALIZATION_RELEASE_REFERENCE_PREFIX
        ):
            raise ValueError(
                "layer finalization release archive reference is malformed"
            )
        return cls(
            receipt_digest=parts[1],
            sha256=parts[2],
            locator=parts[3],
        )


def layer_finalization_release_claim_evidence(
    receipt: LayerFinalizationReleaseReceipt,
    reference: LayerFinalizationReleaseReference,
) -> tuple[str, ...]:
    """Derive the sole claim-history evidence closure for a release receipt."""

    if not isinstance(receipt, LayerFinalizationReleaseReceipt) or not isinstance(
        reference,
        LayerFinalizationReleaseReference,
    ):
        raise ValueError("release claim evidence requires typed receipt inputs")
    if reference.receipt_digest != receipt.receipt_digest:
        raise ValueError("release reference does not name the exact receipt")
    rows = [reference.as_archive_evidence()]
    for replay in receipt.request.replay_receipts:
        rows.append(
            f"layer-replay-receipt:{replay.group_index}:{replay.record_digest}:"
            f"{replay.sha256}:{replay.locator}"
        )
    rows.extend(
        f"review-evidence:{row.sha256}:{row.locator}"
        for row in receipt.request.review_evidence
    )
    return tuple(rows)


def canonical_layer_finalization_release_receipt_bytes(
    receipt: LayerFinalizationReleaseReceipt,
) -> bytes:
    """Return the sole durable encoding for a release receipt."""

    if not isinstance(receipt, LayerFinalizationReleaseReceipt):
        raise ValueError("release receipt must be typed")
    return (
        json.dumps(
            receipt.as_dict(),
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


__all__ = [
    "LAYER_FINALIZATION_RELEASE_EVIDENCE_SCHEMA",
    "LAYER_FINALIZATION_RELEASE_OUTCOME",
    "LAYER_FINALIZATION_RELEASE_RECEIPT_SCHEMA",
    "LAYER_FINALIZATION_RELEASE_REFERENCE_PREFIX",
    "LAYER_FINALIZATION_RELEASE_REQUEST_SCHEMA",
    "LAYER_FINALIZATION_REVIEW_EVIDENCE_DIRECTORY",
    "UNSEALED_JUDGMENT_DISPOSITION",
    "LayerFinalizationReleaseEvidence",
    "LayerFinalizationReleaseReceipt",
    "LayerFinalizationReleaseReference",
    "LayerFinalizationReleaseRequest",
    "canonical_layer_finalization_release_receipt_bytes",
    "layer_finalization_release_claim_evidence",
    "layer_finalization_review_evidence_locator",
]
