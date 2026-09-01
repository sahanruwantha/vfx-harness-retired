"""Immutable receipts for accepted executable work-unit checkpoints."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vfx_harness.domain.stop_envelope_primitives import canonical_digest, require_digest
from vfx_harness.domain.unit_attempts import UnitAttemptClaim
from vfx_harness.domain.work_units import canonical_unit_script_path

COMPLETION_RECEIPT_SCHEMA = "vfx-harness.work-unit-completion-receipt/v1"
COMPLETION_RECEIPT_ARCHIVE_SCHEMA = (
    "vfx-harness.work-unit-completion-receipt-archive/v1"
)
_RECEIPT_FIELDS = frozenset(
    {
        "schema",
        "receipt_digest",
        "claim",
        "checkpoint_digest",
        "script_path",
        "script_hash",
        "evaluation_receipt_locator",
        "evaluation_receipt_sha256",
        "evaluation_receipt_digest",
        "passed_evidence",
        "completed_at",
    }
)
_ARCHIVE_FIELDS = frozenset(
    {"schema", "receipt", "disposition", "reason", "evidence", "at"}
)
_ARCHIVE_DISPOSITIONS = frozenset({"revoked", "superseded"})


def _text(value: object, where: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{where} must be a non-empty trimmed string")
    return value


def _audit_evidence(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{where} must be a non-empty list")
    rows = tuple(_text(item, f"{where}[{index}]") for index, item in enumerate(value))
    if len(rows) != len(set(rows)):
        raise ValueError(f"{where} contains duplicates")
    return rows


def normalize_passed_evidence(
    value: Iterable[tuple[str, str]],
    where: str = "work-unit completion passed_evidence",
) -> tuple[tuple[str, str], ...]:
    """Return one closed, deterministic evidence-key set."""

    if isinstance(value, (str, bytes, Mapping)):
        raise ValueError(f"{where} must be an iterable of (kind, id) pairs")
    rows: list[tuple[str, str]] = []
    for index, item in enumerate(value):
        if (
            not isinstance(item, (tuple, list))
            or len(item) != 2
        ):
            raise ValueError(f"{where}[{index}] must be a (kind, id) pair")
        rows.append(
            (
                _text(item[0], f"{where}[{index}].kind"),
                _text(item[1], f"{where}[{index}].id"),
            )
        )
    if not rows:
        raise ValueError(f"{where} must not be empty")
    if len(rows) != len(set(rows)):
        raise ValueError(f"{where} contains duplicates")
    return tuple(sorted(rows))


def _parse_passed_evidence(value: object, where: str) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, list):
        raise ValueError(f"{where} must be a list")
    pairs: list[tuple[str, str]] = []
    for index, item in enumerate(value):
        item_where = f"{where}[{index}]"
        if not isinstance(item, Mapping) or set(item) != {"kind", "id"}:
            raise ValueError(f"{item_where} must contain exactly kind and id")
        pairs.append((item.get("kind"), item.get("id")))
    return normalize_passed_evidence(pairs, where)


def _script_path(value: object, where: str) -> str:
    text = _text(value, where)
    path = Path(text)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != text:
        raise ValueError(f"{where} must be a canonical relative path")
    return text


@dataclass(frozen=True, slots=True)
class UnitCompletionReceipt:
    """The durable exact evidence identity of one accepted executable unit."""

    receipt_digest: str
    claim: UnitAttemptClaim
    checkpoint_digest: str
    script_path: str
    script_hash: str
    evaluation_receipt_locator: str
    evaluation_receipt_sha256: str
    evaluation_receipt_digest: str
    passed_evidence: tuple[tuple[str, str], ...]
    completed_at: str

    @classmethod
    def mint(
        cls,
        *,
        claim: UnitAttemptClaim,
        checkpoint: Mapping[str, Any],
        script_path: str,
        script_hash: str,
        evaluation_receipt_locator: str,
        evaluation_receipt_sha256: str,
        evaluation_receipt_digest: str,
        passed_evidence: Iterable[tuple[str, str]],
        completed_at: str,
    ) -> UnitCompletionReceipt:
        if not isinstance(claim, UnitAttemptClaim):
            raise ValueError("work-unit completion receipt requires a typed attempt claim")
        checkpoint_digest = canonical_digest(dict(checkpoint))
        script_path = _script_path(script_path, "work-unit completion script_path")
        script_hash = require_digest(script_hash, "work-unit completion script_hash")
        evaluation_receipt_locator = _script_path(
            evaluation_receipt_locator,
            "work-unit completion evaluation_receipt_locator",
        )
        evaluation_receipt_sha256 = require_digest(
            evaluation_receipt_sha256,
            "work-unit completion evaluation_receipt_sha256",
        )
        evaluation_receipt_digest = require_digest(
            evaluation_receipt_digest,
            "work-unit completion evaluation_receipt_digest",
        )
        evidence = normalize_passed_evidence(passed_evidence)
        completed_at = _text(completed_at, "work-unit completion completed_at")
        identity = {
            "schema": COMPLETION_RECEIPT_SCHEMA,
            "claim": claim.as_dict(),
            "checkpoint_digest": checkpoint_digest,
            "script_path": script_path,
            "script_hash": script_hash,
            "evaluation_receipt_locator": evaluation_receipt_locator,
            "evaluation_receipt_sha256": evaluation_receipt_sha256,
            "evaluation_receipt_digest": evaluation_receipt_digest,
            "passed_evidence": [
                {"kind": kind, "id": identifier} for kind, identifier in evidence
            ],
            "completed_at": completed_at,
        }
        return cls(
            receipt_digest=canonical_digest(identity),
            claim=claim,
            checkpoint_digest=checkpoint_digest,
            script_path=script_path,
            script_hash=script_hash,
            evaluation_receipt_locator=evaluation_receipt_locator,
            evaluation_receipt_sha256=evaluation_receipt_sha256,
            evaluation_receipt_digest=evaluation_receipt_digest,
            passed_evidence=evidence,
            completed_at=completed_at,
        )

    @classmethod
    def parse(
        cls,
        value: object,
        where: str = "work-unit completion receipt",
    ) -> UnitCompletionReceipt:
        if not isinstance(value, Mapping):
            raise ValueError(f"{where} must be an object")
        found = set(value)
        if found != _RECEIPT_FIELDS:
            raise ValueError(
                f"{where} fields mismatch; missing={sorted(_RECEIPT_FIELDS - found)}; "
                f"unexpected={sorted(found - _RECEIPT_FIELDS)}"
            )
        if value.get("schema") != COMPLETION_RECEIPT_SCHEMA:
            raise ValueError(f"{where}.schema must be {COMPLETION_RECEIPT_SCHEMA!r}")
        claim = UnitAttemptClaim.parse(value.get("claim"), f"{where}.claim")
        checkpoint_digest = require_digest(
            value.get("checkpoint_digest"),
            f"{where}.checkpoint_digest",
        )
        script_path = _script_path(value.get("script_path"), f"{where}.script_path")
        script_hash = require_digest(value.get("script_hash"), f"{where}.script_hash")
        evaluation_receipt_locator = _script_path(
            value.get("evaluation_receipt_locator"),
            f"{where}.evaluation_receipt_locator",
        )
        evaluation_receipt_sha256 = require_digest(
            value.get("evaluation_receipt_sha256"),
            f"{where}.evaluation_receipt_sha256",
        )
        evaluation_receipt_digest = require_digest(
            value.get("evaluation_receipt_digest"),
            f"{where}.evaluation_receipt_digest",
        )
        passed_evidence = _parse_passed_evidence(
            value.get("passed_evidence"),
            f"{where}.passed_evidence",
        )
        completed_at = _text(value.get("completed_at"), f"{where}.completed_at")
        receipt = cls(
            receipt_digest=require_digest(
                value.get("receipt_digest"),
                f"{where}.receipt_digest",
            ),
            claim=claim,
            checkpoint_digest=checkpoint_digest,
            script_path=script_path,
            script_hash=script_hash,
            evaluation_receipt_locator=evaluation_receipt_locator,
            evaluation_receipt_sha256=evaluation_receipt_sha256,
            evaluation_receipt_digest=evaluation_receipt_digest,
            passed_evidence=passed_evidence,
            completed_at=completed_at,
        )
        identity = receipt.as_dict()
        identity.pop("receipt_digest")
        if receipt.receipt_digest != canonical_digest(identity):
            raise ValueError(f"{where}.receipt_digest does not match its exact payload")
        return receipt

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": COMPLETION_RECEIPT_SCHEMA,
            "receipt_digest": self.receipt_digest,
            "claim": self.claim.as_dict(),
            "checkpoint_digest": self.checkpoint_digest,
            "script_path": self.script_path,
            "script_hash": self.script_hash,
            "evaluation_receipt_locator": self.evaluation_receipt_locator,
            "evaluation_receipt_sha256": self.evaluation_receipt_sha256,
            "evaluation_receipt_digest": self.evaluation_receipt_digest,
            "passed_evidence": [
                {"kind": kind, "id": identifier}
                for kind, identifier in self.passed_evidence
            ],
            "completed_at": self.completed_at,
        }


def validate_state_completion_contracts(value: Mapping[str, Any]) -> None:
    """Validate every current or archived receipt in a work-unit state document."""

    slots: list[tuple[str, Mapping[str, Any], bool]] = []
    units = value.get("units")
    if not isinstance(units, Mapping):
        raise ValueError("work-unit state.units must be an object")
    slots.extend((str(unit_id), slot, True) for unit_id, slot in units.items())
    superseded = value.get("superseded", [])
    if not isinstance(superseded, list):
        raise ValueError("work-unit state.superseded must be a list")
    slots.extend((str(slot.get("id")), slot, False) for slot in superseded)

    for unit_id, slot, current in slots:
        if not isinstance(slot, Mapping):
            raise ValueError(f"work-unit completion slot {unit_id!r} must be an object")
        receipt_raw = slot.get("completion_receipt")
        if current and slot.get("status") == "passed" and receipt_raw is None:
            raise ValueError(
                f"passed work-unit state for {unit_id!r} requires its completion receipt; "
                "revalidate or transactionally migrate the checkpoint"
            )
        if receipt_raw is not None:
            receipt = UnitCompletionReceipt.parse(
                receipt_raw,
                f"work-unit completion slot {unit_id!r}.completion_receipt",
            )
            if not current or slot.get("status") != "passed":
                raise ValueError(
                    f"current completion receipt for {unit_id!r} requires passed state"
                )
            if slot.get("active_attempt") is not None:
                raise ValueError(
                    f"current completion receipt for {unit_id!r} cannot coexist with an attempt"
                )
            if (
                receipt.claim.layer_id != str(value.get("layer"))
                or receipt.claim.unit_id != unit_id
                or receipt.claim.unit_digest != slot.get("unit_hash")
                or receipt.claim.plan_hash != value.get("plan_hash")
            ):
                raise ValueError(
                    f"current completion receipt for {unit_id!r} is stale for durable state"
                )
            if receipt.script_path != canonical_unit_script_path(
                str(value.get("layer")), unit_id
            ):
                raise ValueError(
                    f"current completion receipt for {unit_id!r} names a non-canonical script"
                )
            checkpoint = slot.get("checkpoint")
            if not isinstance(checkpoint, Mapping):
                raise ValueError(
                    f"current completion receipt for {unit_id!r} requires its checkpoint"
                )
            if canonical_digest(dict(checkpoint)) != receipt.checkpoint_digest:
                raise ValueError(
                    f"current completion receipt for {unit_id!r} checkpoint changed"
                )
            if checkpoint.get("script_hash") != receipt.script_hash:
                raise ValueError(
                    f"current completion receipt for {unit_id!r} script hash changed"
                )
            matching_archive = any(
                isinstance(row, Mapping)
                and row.get("disposition") == "completed"
                and isinstance(row.get("claim"), Mapping)
                and row["claim"].get("claim_id") == receipt.claim.claim_id
                for row in slot.get("attempt_history", [])
            )
            if not matching_archive:
                raise ValueError(
                    f"current completion receipt for {unit_id!r} lacks its completed claim"
                )

        history = slot.get("completion_receipt_history", [])
        if not isinstance(history, list):
            raise ValueError(
                f"work-unit completion slot {unit_id!r}.completion_receipt_history "
                "must be a list"
            )
        for index, row in enumerate(history):
            where = (
                f"work-unit completion slot {unit_id!r}."
                f"completion_receipt_history[{index}]"
            )
            if not isinstance(row, Mapping) or set(row) != _ARCHIVE_FIELDS:
                raise ValueError(f"{where} must have the exact receipt archive fields")
            if row.get("schema") != COMPLETION_RECEIPT_ARCHIVE_SCHEMA:
                raise ValueError(
                    f"{where}.schema must be {COMPLETION_RECEIPT_ARCHIVE_SCHEMA!r}"
                )
            UnitCompletionReceipt.parse(row.get("receipt"), f"{where}.receipt")
            if row.get("disposition") not in _ARCHIVE_DISPOSITIONS:
                raise ValueError(
                    f"{where}.disposition must be one of {sorted(_ARCHIVE_DISPOSITIONS)}"
                )
            _text(row.get("reason"), f"{where}.reason")
            _audit_evidence(row.get("evidence"), f"{where}.evidence")
            _text(row.get("at"), f"{where}.at")


def archive_completion_receipt(
    slot: dict[str, Any],
    *,
    disposition: str,
    reason: str,
    evidence: list[str],
    at: str,
) -> UnitCompletionReceipt | None:
    """Retire the current completion receipt without discarding its audit identity."""

    raw = slot.pop("completion_receipt", None)
    if raw is None:
        return None
    receipt = UnitCompletionReceipt.parse(raw)
    if disposition not in _ARCHIVE_DISPOSITIONS:
        raise ValueError(
            "completion receipt disposition must be revoked or superseded"
        )
    slot.setdefault("completion_receipt_history", []).append(
        {
            "schema": COMPLETION_RECEIPT_ARCHIVE_SCHEMA,
            "receipt": receipt.as_dict(),
            "disposition": disposition,
            "reason": _text(reason, "completion receipt archive reason"),
            "evidence": list(
                _audit_evidence(evidence, "completion receipt archive evidence")
            ),
            "at": _text(at, "completion receipt archive timestamp"),
        }
    )
    return receipt
