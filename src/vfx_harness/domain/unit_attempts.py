"""Closed ownership tokens for one work-unit planning/build attempt."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from vfx_harness.domain.authority_head_records import (
    AUTHORITY_SELECTION_TOKEN_SCHEMA,
    AuthorityHeadRecordError,
    AuthoritySelectionTokenProjection,
    parse_authority_selection_token,
)
from vfx_harness.domain.run_ids import require_run_id
from vfx_harness.domain.stop_envelope_primitives import canonical_digest, require_digest

ATTEMPT_CLAIM_SCHEMA = "vfx-harness.work-unit-attempt-claim/v1"
ATTEMPT_ARCHIVE_SCHEMA = "vfx-harness.work-unit-attempt-archive/v1"
ATTEMPT_CHECKPOINT_ARCHIVE_SCHEMA = (
    "vfx-harness.work-unit-attempt-checkpoint-archive/v1"
)
ATTEMPT_PHASES = frozenset({"planning", "building"})
ATTEMPT_DISPOSITIONS = frozenset({"completed", "released", "revoked"})
_CLAIM_FIELDS = frozenset(
    {
        "schema",
        "claim_id",
        "attempt_revision",
        "run_id",
        "layer_id",
        "unit_id",
        "unit_digest",
        "plan_hash",
        "selection_token",
        "phase",
        "claimed_at",
        "updated_at",
    }
)
_ARCHIVE_FIELDS = frozenset(
    {"schema", "claim", "disposition", "reason", "evidence", "at"}
)
_CHECKPOINT_ARCHIVE_FIELDS = frozenset(
    {"schema", "checkpoint", "claim_id", "disposition", "reason", "evidence", "at"}
)


def _text(value: object, where: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{where} must be a non-empty trimmed string")
    return value


def _evidence(value: object, where: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{where} must be a non-empty list")
    rows = [_text(item, f"{where}[{index}]") for index, item in enumerate(value)]
    if len(rows) != len(set(rows)):
        raise ValueError(f"{where} contains duplicates")
    return rows


def _token_dict(token: AuthoritySelectionTokenProjection) -> dict[str, Any]:
    return {
        "schema": AUTHORITY_SELECTION_TOKEN_SCHEMA,
        "plan_revision": token.plan_revision,
        "plan_pointer_sha256": token.plan_pointer_sha256,
        "jit_revision": token.jit_revision,
        "jit_pointer_sha256": token.jit_pointer_sha256,
    }


@dataclass(frozen=True, slots=True)
class UnitAttemptClaim:
    """Exclusive durable ownership of one exact unit attempt."""

    claim_id: str
    attempt_revision: int
    run_id: str
    layer_id: str
    unit_id: str
    unit_digest: str
    plan_hash: str
    selection_token: AuthoritySelectionTokenProjection
    phase: str
    claimed_at: str
    updated_at: str

    @classmethod
    def mint(
        cls,
        *,
        attempt_revision: int,
        run_id: str,
        layer_id: str,
        unit_id: str,
        unit_digest: str,
        plan_hash: str,
        selection_token: Mapping[str, Any],
        phase: str,
        at: str,
    ) -> UnitAttemptClaim:
        if (
            not isinstance(attempt_revision, int)
            or isinstance(attempt_revision, bool)
            or attempt_revision <= 0
        ):
            raise ValueError("unit attempt revision must be a positive integer")
        run_id = require_run_id(run_id, "unit attempt run_id")
        layer_id = _text(layer_id, "unit attempt layer_id")
        unit_id = _text(unit_id, "unit attempt unit_id")
        unit_digest = require_digest(unit_digest, "unit attempt unit_digest")
        plan_hash = require_digest(plan_hash, "unit attempt plan_hash")
        if phase not in ATTEMPT_PHASES:
            raise ValueError(
                f"unit attempt phase must be one of {sorted(ATTEMPT_PHASES)}"
            )
        at = _text(at, "unit attempt timestamp")
        try:
            token = parse_authority_selection_token(
                selection_token,
                "unit attempt selection_token",
            )
        except AuthorityHeadRecordError as exc:
            raise ValueError(str(exc)) from exc
        identity = {
            "schema": ATTEMPT_CLAIM_SCHEMA,
            "attempt_revision": attempt_revision,
            "run_id": run_id,
            "layer_id": layer_id,
            "unit_id": unit_id,
            "unit_digest": unit_digest,
            "plan_hash": plan_hash,
            "selection_token": _token_dict(token),
        }
        return cls(
            claim_id=f"uca-{canonical_digest(identity)}",
            attempt_revision=attempt_revision,
            run_id=run_id,
            layer_id=layer_id,
            unit_id=unit_id,
            unit_digest=unit_digest,
            plan_hash=plan_hash,
            selection_token=token,
            phase=phase,
            claimed_at=at,
            updated_at=at,
        )

    @classmethod
    def parse(
        cls,
        value: object,
        where: str = "work-unit attempt claim",
    ) -> UnitAttemptClaim:
        if not isinstance(value, Mapping):
            raise ValueError(f"{where} must be an object")
        found = set(value)
        if found != _CLAIM_FIELDS:
            raise ValueError(
                f"{where} fields mismatch; missing={sorted(_CLAIM_FIELDS - found)}; "
                f"unexpected={sorted(found - _CLAIM_FIELDS)}"
            )
        if value.get("schema") != ATTEMPT_CLAIM_SCHEMA:
            raise ValueError(f"{where}.schema must be {ATTEMPT_CLAIM_SCHEMA!r}")
        revision = value.get("attempt_revision")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision <= 0:
            raise ValueError(f"{where}.attempt_revision must be a positive integer")
        try:
            token = parse_authority_selection_token(
                value.get("selection_token"),
                f"{where}.selection_token",
            )
        except AuthorityHeadRecordError as exc:
            raise ValueError(str(exc)) from exc
        phase = value.get("phase")
        if phase not in ATTEMPT_PHASES:
            raise ValueError(f"{where}.phase must be one of {sorted(ATTEMPT_PHASES)}")
        claim = cls(
            claim_id=_text(value.get("claim_id"), f"{where}.claim_id"),
            attempt_revision=revision,
            run_id=require_run_id(value.get("run_id"), f"{where}.run_id"),
            layer_id=_text(value.get("layer_id"), f"{where}.layer_id"),
            unit_id=_text(value.get("unit_id"), f"{where}.unit_id"),
            unit_digest=require_digest(value.get("unit_digest"), f"{where}.unit_digest"),
            plan_hash=require_digest(value.get("plan_hash"), f"{where}.plan_hash"),
            selection_token=token,
            phase=str(phase),
            claimed_at=_text(value.get("claimed_at"), f"{where}.claimed_at"),
            updated_at=_text(value.get("updated_at"), f"{where}.updated_at"),
        )
        expected = cls.mint(
            attempt_revision=claim.attempt_revision,
            run_id=claim.run_id,
            layer_id=claim.layer_id,
            unit_id=claim.unit_id,
            unit_digest=claim.unit_digest,
            plan_hash=claim.plan_hash,
            selection_token=_token_dict(claim.selection_token),
            phase=claim.phase,
            at=claim.claimed_at,
        ).claim_id
        if claim.claim_id != expected:
            raise ValueError(f"{where}.claim_id does not match its exact attempt identity")
        return claim

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": ATTEMPT_CLAIM_SCHEMA,
            "claim_id": self.claim_id,
            "attempt_revision": self.attempt_revision,
            "run_id": self.run_id,
            "layer_id": self.layer_id,
            "unit_id": self.unit_id,
            "unit_digest": self.unit_digest,
            "plan_hash": self.plan_hash,
            "selection_token": _token_dict(self.selection_token),
            "phase": self.phase,
            "claimed_at": self.claimed_at,
            "updated_at": self.updated_at,
        }

    def promoted(self, *, phase: str, at: str) -> UnitAttemptClaim:
        if self.phase != "planning" or phase != "building":
            raise ValueError(
                "unit attempt promotion requires the exact planning -> building phase"
            )
        return replace(self, phase=phase, updated_at=_text(at, "unit attempt timestamp"))


def require_attempt_matches_slot(
    slot: Mapping[str, Any],
    supplied: UnitAttemptClaim | Mapping[str, Any] | None,
    *,
    layer_id: str,
    unit_id: str,
    unit_digest: str,
    plan_hash: str,
) -> UnitAttemptClaim | None:
    """Require the caller's complete token whenever the slot has an active claim."""

    raw = slot.get("active_attempt")
    current = None if raw is None else UnitAttemptClaim.parse(raw)
    provided = (
        supplied
        if isinstance(supplied, UnitAttemptClaim)
        else None if supplied is None else UnitAttemptClaim.parse(supplied)
    )
    if current is None:
        if provided is not None:
            raise ValueError("work-unit attempt claim is stale; the unit is not actively owned")
        return None
    if provided is None:
        raise ValueError(
            f"work unit {unit_id} has active attempt {current.claim_id}; exact claim required"
        )
    if provided != current:
        raise ValueError(
            f"work-unit attempt claim changed; expected={current.claim_id}, "
            f"provided={provided.claim_id}"
        )
    expected = (
        str(layer_id),
        str(unit_id),
        require_digest(unit_digest, "active work-unit digest"),
        require_digest(plan_hash, "active work-unit plan hash"),
    )
    observed = (
        current.layer_id,
        current.unit_id,
        current.unit_digest,
        current.plan_hash,
    )
    if observed != expected:
        raise ValueError(
            "active work-unit attempt belongs to another layer, unit, or authority digest"
        )
    return current


def parse_attempt_archive(
    value: object,
    where: str = "work-unit attempt archive",
) -> UnitAttemptClaim:
    """Parse one closed immutable claim archive row."""

    if not isinstance(value, Mapping):
        raise ValueError(f"{where} must be an object")
    found = set(value)
    if found != _ARCHIVE_FIELDS:
        raise ValueError(
            f"{where} fields mismatch; missing={sorted(_ARCHIVE_FIELDS - found)}; "
            f"unexpected={sorted(found - _ARCHIVE_FIELDS)}"
        )
    if value.get("schema") != ATTEMPT_ARCHIVE_SCHEMA:
        raise ValueError(f"{where}.schema must be {ATTEMPT_ARCHIVE_SCHEMA!r}")
    claim = UnitAttemptClaim.parse(value.get("claim"), f"{where}.claim")
    disposition = value.get("disposition")
    if disposition not in ATTEMPT_DISPOSITIONS:
        raise ValueError(
            f"{where}.disposition must be one of {sorted(ATTEMPT_DISPOSITIONS)}"
        )
    _text(value.get("reason"), f"{where}.reason")
    _evidence(value.get("evidence"), f"{where}.evidence")
    _text(value.get("at"), f"{where}.at")
    return claim


def validate_slot_attempt_contract(
    slot: Mapping[str, Any],
    *,
    layer_id: str,
    unit_id: str,
    unit_digest: object,
    plan_hash: object,
) -> UnitAttemptClaim | None:
    """Strictly validate every present attempt field on one durable unit slot."""

    has_attempt_fields = any(
        field in slot for field in ("attempt_revision", "active_attempt", "attempt_history")
    )
    if not has_attempt_fields:
        return None
    revision = slot.get("attempt_revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision <= 0:
        raise ValueError("work-unit slot.attempt_revision must be a positive integer")
    history = slot.get("attempt_history", [])
    if not isinstance(history, list):
        raise ValueError("work-unit slot.attempt_history must be a list")
    archived = tuple(
        parse_attempt_archive(row, f"work-unit slot.attempt_history[{index}]")
        for index, row in enumerate(history)
    )
    raw_active = slot.get("active_attempt")
    active = None if raw_active is None else UnitAttemptClaim.parse(raw_active)
    claims = (*archived, *((active,) if active is not None else ()))
    revisions = [claim.attempt_revision for claim in claims]
    if not revisions or revisions != sorted(set(revisions)) or revisions[-1] != revision:
        raise ValueError(
            "work-unit slot attempt revisions must be unique, increasing, and end at "
            f"its recorded revision {revision}"
        )
    if len({claim.claim_id for claim in claims}) != len(claims):
        raise ValueError("work-unit attempt history contains duplicate claim identities")
    for claim in claims:
        if (claim.layer_id, claim.unit_id) != (str(layer_id), str(unit_id)):
            raise ValueError(
                "work-unit attempt history belongs to another layer or unit identity"
            )
    if active is None:
        return None
    expected_digest = require_digest(unit_digest, "active work-unit digest")
    expected_plan_hash = require_digest(plan_hash, "active work-unit plan hash")
    if active.unit_digest != expected_digest or active.plan_hash != expected_plan_hash:
        raise ValueError("active work-unit attempt does not match current durable identity")
    status = slot.get("status")
    legal_statuses = (
        {"planning"}
        if active.phase == "planning"
        else {"building", "frozen", "evaluating", "repairing"}
    )
    if status not in legal_statuses:
        raise ValueError(
            f"active {active.phase} attempt cannot own work-unit state {status!r}"
        )
    return active


def _slot_claims(slot: Mapping[str, Any]) -> tuple[UnitAttemptClaim, ...]:
    history = slot.get("attempt_history", [])
    archived = tuple(
        parse_attempt_archive(row, f"work-unit attempt_history[{index}]")
        for index, row in enumerate(history)
    )
    raw_active = slot.get("active_attempt")
    return (*archived, *((UnitAttemptClaim.parse(raw_active),) if raw_active is not None else ()))


def _validate_checkpoint_archives(slot: Mapping[str, Any], where: str) -> None:
    history = slot.get("attempt_checkpoint_history", [])
    if not isinstance(history, list):
        raise ValueError(f"{where}.attempt_checkpoint_history must be a list")
    for index, row in enumerate(history):
        item_where = f"{where}.attempt_checkpoint_history[{index}]"
        if not isinstance(row, Mapping):
            raise ValueError(f"{item_where} must be an object")
        found = set(row)
        if found != _CHECKPOINT_ARCHIVE_FIELDS:
            raise ValueError(
                f"{item_where} fields mismatch; "
                f"missing={sorted(_CHECKPOINT_ARCHIVE_FIELDS - found)}; "
                f"unexpected={sorted(found - _CHECKPOINT_ARCHIVE_FIELDS)}"
            )
        if row.get("schema") != ATTEMPT_CHECKPOINT_ARCHIVE_SCHEMA:
            raise ValueError(
                f"{item_where}.schema must be {ATTEMPT_CHECKPOINT_ARCHIVE_SCHEMA!r}"
            )
        if not isinstance(row.get("checkpoint"), Mapping):
            raise ValueError(f"{item_where}.checkpoint must be an object")
        claim_id = row.get("claim_id")
        if claim_id is not None:
            _text(claim_id, f"{item_where}.claim_id")
        if row.get("disposition") not in {"released", "revoked"}:
            raise ValueError(f"{item_where}.disposition must be released or revoked")
        _text(row.get("reason"), f"{item_where}.reason")
        _evidence(row.get("evidence"), f"{item_where}.evidence")
        _text(row.get("at"), f"{item_where}.at")


def validate_state_attempt_contracts(value: Mapping[str, Any]) -> None:
    """Fail a state read on any malformed live or archived nested attempt row."""

    units = value.get("units")
    if not isinstance(units, Mapping):
        raise ValueError("work-unit state.units must be an object")
    all_slots: list[tuple[str, Mapping[str, Any]]] = []
    active_by_unit: dict[str, UnitAttemptClaim] = {}
    for unit_id, slot in units.items():
        if not isinstance(slot, Mapping):
            raise ValueError(f"work-unit state.units[{unit_id!r}] must be an object")
        active = validate_slot_attempt_contract(
            slot,
            layer_id=str(value.get("layer")),
            unit_id=str(unit_id),
            unit_digest=slot.get("unit_hash"),
            plan_hash=value.get("plan_hash"),
        )
        _validate_checkpoint_archives(slot, f"work-unit state.units[{unit_id!r}]")
        all_slots.append((str(unit_id), slot))
        if active is not None:
            active_by_unit[str(unit_id)] = active
    superseded = value.get("superseded", [])
    if not isinstance(superseded, list):
        raise ValueError("work-unit state.superseded must be a list")
    for index, slot in enumerate(superseded):
        if not isinstance(slot, Mapping):
            raise ValueError(f"work-unit state.superseded[{index}] must be an object")
        validate_slot_attempt_contract(
            slot,
            layer_id=str(value.get("layer")),
            unit_id=str(slot.get("id")),
            unit_digest=slot.get("unit_hash"),
            plan_hash=value.get("plan_hash"),
        )
        _validate_checkpoint_archives(slot, f"work-unit state.superseded[{index}]")
        all_slots.append((str(slot.get("id")), slot))

    lineage = value.get("attempt_lineage", {})
    if not isinstance(lineage, Mapping):
        raise ValueError("work-unit state.attempt_lineage must be an object")
    parsed_lineage: dict[str, int] = {}
    for raw_unit_id, raw_revision in lineage.items():
        unit_id = _text(raw_unit_id, "work-unit attempt lineage unit id")
        if (
            not isinstance(raw_revision, int)
            or isinstance(raw_revision, bool)
            or raw_revision <= 0
        ):
            raise ValueError(
                f"work-unit attempt lineage for {unit_id} must be a positive integer"
            )
        parsed_lineage[unit_id] = raw_revision
    claims_by_unit: dict[str, list[UnitAttemptClaim]] = {}
    for unit_id, slot in all_slots:
        claims = _slot_claims(slot)
        if claims:
            claims_by_unit.setdefault(unit_id, []).extend(claims)
    if set(claims_by_unit) != set(parsed_lineage):
        raise ValueError(
            "work-unit attempt lineage IDs must exactly match durable claim history"
        )
    for unit_id, claims in claims_by_unit.items():
        revisions = [claim.attempt_revision for claim in claims]
        expected = list(range(1, parsed_lineage[unit_id] + 1))
        if sorted(revisions) != expected:
            raise ValueError(
                f"work-unit attempt lineage for {unit_id} must preserve revisions {expected}"
            )
        active = active_by_unit.get(unit_id)
        if active is not None and active.attempt_revision != parsed_lineage[unit_id]:
            raise ValueError(
                f"active work-unit attempt for {unit_id} is not the lineage head"
            )


def archive_attempt_checkpoint(
    slot: dict[str, Any],
    *,
    claim_id: str | None,
    disposition: str,
    reason: str,
    evidence: list[str],
    at: str,
) -> dict[str, Any] | None:
    """Remove one candidate checkpoint from executable state into typed audit."""

    checkpoint = slot.pop("checkpoint", None)
    if checkpoint is None:
        return None
    if not isinstance(checkpoint, Mapping):
        raise ValueError("work-unit checkpoint must be an object")
    if disposition not in {"released", "revoked"}:
        raise ValueError("checkpoint archive disposition must be released or revoked")
    if claim_id is not None:
        claim_id = _text(claim_id, "checkpoint archive claim_id")
    row = {
        "schema": ATTEMPT_CHECKPOINT_ARCHIVE_SCHEMA,
        "checkpoint": dict(checkpoint),
        "claim_id": claim_id,
        "disposition": disposition,
        "reason": _text(reason, "checkpoint archive reason"),
        "evidence": _evidence(evidence, "checkpoint archive evidence"),
        "at": _text(at, "checkpoint archive timestamp"),
    }
    slot.setdefault("attempt_checkpoint_history", []).append(row)
    return row


def archive_active_attempt(
    slot: dict[str, Any],
    *,
    disposition: str,
    reason: str,
    evidence: list[str],
    at: str,
    archive_checkpoint: bool = True,
) -> UnitAttemptClaim | None:
    """Move an active token into its immutable audit history."""

    if disposition not in ATTEMPT_DISPOSITIONS:
        raise ValueError(
            f"unit attempt disposition must be one of {sorted(ATTEMPT_DISPOSITIONS)}"
        )
    reason = _text(reason, "unit attempt archive reason")
    evidence = _evidence(evidence, "unit attempt archive evidence")
    at = _text(at, "unit attempt archive timestamp")
    raw = slot.get("active_attempt")
    if raw is None:
        return None
    claim = UnitAttemptClaim.parse(raw)
    if archive_checkpoint and disposition in {"released", "revoked"}:
        archive_attempt_checkpoint(
            slot,
            claim_id=claim.claim_id,
            disposition=disposition,
            reason=reason,
            evidence=evidence,
            at=at,
        )
    slot.pop("active_attempt")
    slot.setdefault("attempt_history", []).append(
        {
            "schema": ATTEMPT_ARCHIVE_SCHEMA,
            "claim": claim.as_dict(),
            "disposition": disposition,
            "reason": reason,
            "evidence": evidence,
            "at": at,
        }
    )
    return claim


def revoke_active_attempt(
    slot: dict[str, Any],
    *,
    reason: str,
    evidence: list[str],
    at: str,
    next_status: str,
) -> UnitAttemptClaim | None:
    """Archive current ownership and reopen its slot in the same state mutation."""

    before = slot.get("status")
    claim = archive_active_attempt(
        slot,
        disposition="revoked",
        reason=reason,
        evidence=evidence,
        at=at,
    )
    if claim is None:
        return None
    next_status = _text(next_status, "revoked unit attempt next_status")
    slot.setdefault("history", []).append(
        {
            "at": at,
            "from": before,
            "to": next_status,
            "reason": reason,
            "metadata": {
                "attempt_claim_id": claim.claim_id,
                "evidence": list(evidence),
            },
        }
    )
    slot["status"] = next_status
    slot["updated"] = at
    return claim
