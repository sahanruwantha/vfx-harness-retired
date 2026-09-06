"""Atomic ownership transactions for paid work-unit attempts.

Callers hold the selected-authority shared lock before entering these operations.
The decorator then acquires the layer's exclusive state lock, preserving the global
selection -> unit-state order while readiness and claim publication share one read/write.
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import wraps
from pathlib import Path
from typing import Any

from vfx_harness.domain.stop_envelope_primitives import require_digest
from vfx_harness.domain.unit_attempts import (
    UnitAttemptClaim,
    archive_active_attempt,
    archive_attempt_checkpoint,
    require_attempt_matches_slot,
)
from vfx_harness.domain.unit_completion_receipts import (
    UnitCompletionReceipt,
)
from vfx_harness.domain.work_units import (
    WorkUnit,
    canonical_unit_script_path,
    ready_units,
    validate_unit_dag,
)
from vfx_harness.orchestration import unit_state
from vfx_harness.orchestration.authority_selection_heads import (
    read_authority_selection_heads,
)
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionToken,
    authority_selection_lock,
    require_matching_authority_selection_token,
)
from vfx_harness.orchestration.unit_attempt_state_guards import (
    UnitAttemptConflict as UnitAttemptConflict,
)
from vfx_harness.orchestration.unit_attempt_state_guards import (
    _selection_projection,
    _slot,
    require_active_unit_attempt_in_state,
)
from vfx_harness.orchestration.unit_attempt_state_guards import (
    active_unit_attempt_guard as active_unit_attempt_guard,
)
from vfx_harness.orchestration.unit_attempt_state_guards import (
    require_active_unit_attempt as require_active_unit_attempt,
)
from vfx_harness.orchestration.unit_completion_authority_guard import (
    require_current_unit_completion_authorization,
)
from vfx_harness.orchestration.unit_completion_authorizations import (
    AuthorizedUnitCompletionSet,
)
from vfx_harness.orchestration.unit_completion_state import (
    completed_unit_attempt_guard as completed_unit_attempt_guard,
)
from vfx_harness.orchestration.unit_evaluation_receipts import (
    PreparedUnitEvaluationCompletion,
    UnitEvaluationConflict,
    prepare_unit_evaluation_completion,
    require_prepared_unit_evaluation_completion,
)
from vfx_harness.orchestration.unit_state_lifecycle import (
    PLANNING_CLAIMABLE_STATES as PLANNING_CLAIMABLE_STATES,
)
from vfx_harness.orchestration.unit_state_lifecycle import (
    UNCLAIMED_RETRY_STATES as UNCLAIMED_RETRY_STATES,
)
from vfx_harness.orchestration.unit_state_lock import (
    serialized_state_mutation,
    unit_state_path,
)

ATTEMPT_RELEASE_STATES = frozenset({"retryable", "blocked", "failed"})



def _selected_state_mutation(mutation):
    """Enforce selection-SH -> state-EX ordering around one public mutation."""

    @wraps(mutation)
    def guarded(folder, layer_id, *args, **kwargs):
        selection_token = kwargs.get("selection_token")
        _selection_projection(selection_token)
        with authority_selection_lock(folder, exclusive=False):
            observed = read_authority_selection_heads(folder).token
            require_matching_authority_selection_token(selection_token, observed)
            return mutation(folder, layer_id, *args, **kwargs)

    return guarded


def _text(value: object, where: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise UnitAttemptConflict(f"{where} must be a non-empty trimmed string")
    return value


def _evidence(value: object, where: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise UnitAttemptConflict(f"{where} must be a non-empty list")
    rows = [_text(item, f"{where}[{index}]") for index, item in enumerate(value)]
    if len(rows) != len(set(rows)):
        raise UnitAttemptConflict(f"{where} contains duplicates")
    return rows


def _write(path: Path, value: dict[str, Any]) -> None:
    unit_state._write(path, value)


def _load_exact_state(
    folder: str | Path,
    layer_id: str,
    units: tuple[WorkUnit, ...],
    *,
    expected_plan_hash: str,
) -> dict[str, Any]:
    validate_unit_dag(units, f"layer {layer_id} work units")
    expected_plan_hash = require_digest(
        expected_plan_hash,
        "work-unit claim expected_plan_hash",
    )
    value = unit_state.load(folder, layer_id)
    if not value:
        raise UnitAttemptConflict("work-unit state is not initialized")
    unit_state.validate_current(value, layer_id, units)
    if int(value.get("digest_schema", 0)) != unit_state.DIGEST_SCHEMA:
        raise UnitAttemptConflict(
            "work-unit claim requires current digest schema; publish a validated "
            "authority replacement or amendment"
        )
    if value.get("plan_hash") != expected_plan_hash:
        raise UnitAttemptConflict(
            "work-unit claim plan identity changed; re-resolve selected authority"
        )
    return value


def _require_ready(
    value: Mapping[str, Any],
    unit_id: str,
    units: tuple[WorkUnit, ...],
    *,
    eligible_passed: set[str] | frozenset[str] | None,
    completion_authorization: AuthorizedUnitCompletionSet | None,
) -> WorkUnit:
    by_id = {unit.id: unit for unit in units}
    unit = by_id.get(unit_id)
    if unit is None:
        raise UnitAttemptConflict(f"unknown work unit {unit_id!r}")
    passed = {
        str(uid)
        for uid, row in (value.get("units") or {}).items()
        if isinstance(row, Mapping) and row.get("status") == "passed"
    }
    if eligible_passed is not None:
        eligible = {str(uid) for uid in eligible_passed}
        unknown = eligible - set(by_id)
        if unknown:
            raise UnitAttemptConflict(
                "eligible passed set names unknown work unit(s): "
                + ", ".join(sorted(unknown))
            )
        passed &= eligible
    sealed = unit_state.authorized_passed_unit_ids(
        value,
        units,
        completion_authorization=completion_authorization,
    ) & passed
    ready_ids = {
        candidate.id
        for candidate in ready_units(units, passed, sealed_producers=sealed)
    }
    if unit_id not in ready_ids:
        missing = sorted(set(unit.depends_on) - sealed)
        detail = f"; unsealed producers={missing}" if missing else ""
        raise UnitAttemptConflict(
            f"work unit {unit_id} is no longer dependency-ready{detail}"
        )
    return unit


def _next_attempt_revision(value: dict[str, Any], unit_id: str) -> int:
    lineage = value.setdefault("attempt_lineage", {})
    if not isinstance(lineage, dict):
        raise UnitAttemptConflict("work-unit state.attempt_lineage must be an object")
    revision = lineage.get(unit_id, 0)
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise UnitAttemptConflict(
            f"work-unit attempt lineage for {unit_id} must be a non-negative integer"
        )
    return revision + 1


def _append_transition(
    slot: dict[str, Any],
    *,
    before: str,
    after: str,
    reason: str,
    claim: UnitAttemptClaim,
    at: str,
    evidence: list[str] | None = None,
) -> None:
    metadata: dict[str, Any] = {
        "attempt_claim_id": claim.claim_id,
        "attempt_revision": claim.attempt_revision,
        "attempt_run_id": claim.run_id,
    }
    if evidence is not None:
        metadata["evidence"] = list(evidence)
    slot.setdefault("history", []).append(
        {
            "at": at,
            "from": before,
            "to": after,
            "reason": reason,
            "metadata": metadata,
        }
    )
    slot["status"] = after
    slot["updated"] = at


@_selected_state_mutation
@serialized_state_mutation(unit_state_path)
def claim_ready_unit_for_planning(
    folder: str | Path,
    layer_id: str,
    unit_id: str,
    units: tuple[WorkUnit, ...],
    *,
    expected_plan_hash: str,
    eligible_passed: set[str] | frozenset[str] | None,
    completion_authorization: AuthorizedUnitCompletionSet | None,
    run_id: str,
    selection_token: AuthoritySelectionToken,
    reason: str,
) -> UnitAttemptClaim:
    """Own one ready pending/blocked/retryable unit before planning spend."""

    reason = _text(reason, "work-unit planning claim reason")
    projection = _selection_projection(selection_token)
    if (
        completion_authorization is not None
        and completion_authorization.selection_token != projection
    ):
        raise UnitAttemptConflict(
            "work-unit completion authorization belongs to another selection"
        )
    value = _load_exact_state(
        folder,
        layer_id,
        units,
        expected_plan_hash=expected_plan_hash,
    )
    if completion_authorization is not None:
        try:
            require_current_unit_completion_authorization(
                folder,
                completion_authorization,
                selection_token=selection_token,
            )
        except ValueError as exc:
            raise UnitAttemptConflict(str(exc)) from exc
    slot = _slot(value, unit_id)
    before = str(slot.get("status"))
    if before not in PLANNING_CLAIMABLE_STATES:
        raise UnitAttemptConflict(
            f"cannot claim work unit {unit_id} for planning from state {before!r}; "
            f"legal states are {sorted(PLANNING_CLAIMABLE_STATES)}"
        )
    if slot.get("active_attempt") is not None:
        raise UnitAttemptConflict(f"work unit {unit_id} already has an active attempt")
    unit = _require_ready(
        value,
        unit_id,
        units,
        eligible_passed=eligible_passed,
        completion_authorization=completion_authorization,
    )
    at = unit_state._now()
    claim = UnitAttemptClaim.mint(
        attempt_revision=_next_attempt_revision(value, unit.id),
        run_id=run_id,
        layer_id=str(layer_id),
        unit_id=unit.id,
        unit_digest=unit_state.unit_digest(unit),
        plan_hash=expected_plan_hash,
        selection_token=selection_token.to_dict(),
        phase="planning",
        at=at,
    )
    if claim.selection_token != projection:  # pragma: no cover - parser invariant
        raise UnitAttemptConflict("work-unit claim selection token changed while minting")
    slot["attempt_revision"] = claim.attempt_revision
    slot["active_attempt"] = claim.as_dict()
    value["attempt_lineage"][unit.id] = claim.attempt_revision
    _append_transition(
        slot,
        before=before,
        after="planning",
        reason=reason,
        claim=claim,
        at=at,
    )
    value["updated"] = at
    _write(unit_state_path(folder, layer_id), value)
    return claim


@_selected_state_mutation
@serialized_state_mutation(unit_state_path)
def claim_ready_unit_for_build(
    folder: str | Path,
    layer_id: str,
    unit_id: str,
    units: tuple[WorkUnit, ...],
    prior_claim: UnitAttemptClaim,
    *,
    expected_plan_hash: str,
    eligible_passed: set[str] | frozenset[str] | None,
    completion_authorization: AuthorizedUnitCompletionSet | None,
    run_id: str,
    selection_token: AuthoritySelectionToken,
    reason: str,
) -> UnitAttemptClaim:
    """Promote the exact ready planning claim before builder execution spend."""

    reason = _text(reason, "work-unit build claim reason")
    projection = _selection_projection(selection_token)
    if (
        completion_authorization is not None
        and completion_authorization.selection_token != projection
    ):
        raise UnitAttemptConflict(
            "work-unit completion authorization belongs to another selection"
        )
    value = _load_exact_state(
        folder,
        layer_id,
        units,
        expected_plan_hash=expected_plan_hash,
    )
    if completion_authorization is not None:
        try:
            require_current_unit_completion_authorization(
                folder,
                completion_authorization,
                selection_token=selection_token,
            )
        except ValueError as exc:
            raise UnitAttemptConflict(str(exc)) from exc
    slot = _slot(value, unit_id)
    if slot.get("status") != "planning":
        raise UnitAttemptConflict(
            f"cannot promote work unit {unit_id} for build from state "
            f"{slot.get('status')!r}; an exact planning claim is required"
        )
    unit = _require_ready(
        value,
        unit_id,
        units,
        eligible_passed=eligible_passed,
        completion_authorization=completion_authorization,
    )
    try:
        current = require_attempt_matches_slot(
            slot,
            prior_claim,
            layer_id=str(layer_id),
            unit_id=unit.id,
            unit_digest=unit_state.unit_digest(unit),
            plan_hash=expected_plan_hash,
        )
    except ValueError as exc:
        raise UnitAttemptConflict(str(exc)) from exc
    if current is None or current.phase != "planning":
        raise UnitAttemptConflict(
            f"work unit {unit_id} does not have the exact active planning claim"
        )
    projection = _selection_projection(selection_token)
    if current.selection_token != projection or current.run_id != _text(
        run_id,
        "work-unit build claim run_id",
    ):
        raise UnitAttemptConflict(
            "work-unit planning claim belongs to another run or authority selection"
        )
    at = unit_state._now()
    promoted = current.promoted(phase="building", at=at)
    slot["active_attempt"] = promoted.as_dict()
    _append_transition(
        slot,
        before="planning",
        after="building",
        reason=reason,
        claim=promoted,
        at=at,
    )
    value["updated"] = at
    _write(unit_state_path(folder, layer_id), value)
    return promoted


@_selected_state_mutation
@serialized_state_mutation(unit_state_path)
def fail_unit_attempt(
    folder: str | Path,
    layer_id: str,
    unit_id: str,
    units: tuple[WorkUnit, ...],
    claim: UnitAttemptClaim,
    *,
    expected_plan_hash: str,
    selection_token: AuthoritySelectionToken,
    reason: str,
    evidence: list[str],
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Complete the exact active attempt as a typed executable failure."""

    reason = _text(reason, "work-unit attempt failure reason")
    evidence = _evidence(evidence, "work-unit attempt failure evidence")
    value = _load_exact_state(
        folder,
        layer_id,
        units,
        expected_plan_hash=expected_plan_hash,
    )
    current = require_active_unit_attempt_in_state(
        value,
        layer_id,
        unit_id,
        units,
        claim,
        expected_plan_hash=expected_plan_hash,
        selection_token=selection_token,
    )
    slot = _slot(value, unit_id)
    before = str(slot.get("status"))
    if "failed" not in unit_state._TRANSITIONS.get(before, set()):
        raise UnitAttemptConflict(
            f"cannot fail work unit {unit_id} from state {before!r}"
        )
    reserved = {"attempt_claim_id", "attempt_revision", "attempt_run_id", "evidence"}
    extra = {} if metadata is None else dict(metadata)
    overlap = reserved & set(extra)
    if overlap:
        raise UnitAttemptConflict(
            "work-unit failure metadata cannot replace reserved field(s): "
            + ", ".join(sorted(overlap))
        )
    at = unit_state._now()
    archive_active_attempt(
        slot,
        disposition="completed",
        reason=reason,
        evidence=evidence,
        at=at,
    )
    archive_attempt_checkpoint(
        slot,
        claim_id=current.claim_id,
        disposition="revoked",
        reason=reason,
        evidence=evidence,
        at=at,
    )
    event_metadata = {
        **extra,
        "attempt_claim_id": current.claim_id,
        "attempt_revision": current.attempt_revision,
        "attempt_run_id": current.run_id,
        "evidence": evidence,
    }
    slot.setdefault("history", []).append(
        {
            "at": at,
            "from": before,
            "to": "failed",
            "reason": reason,
            "metadata": event_metadata,
        }
    )
    slot["status"] = "failed"
    slot["updated"] = at
    value["updated"] = at
    _write(unit_state_path(folder, layer_id), value)
    return value


def _completion_checkpoint(
    slot: Mapping[str, Any],
    unit: WorkUnit,
) -> Mapping[str, Any]:
    required_fields = {
        "at",
        "candidate_hash",
        "settings_hash",
        "script_hash",
        "input_hash",
        "protected_contract_ids",
        "unit_hash",
    }
    checkpoint = slot.get("checkpoint")
    if not isinstance(checkpoint, Mapping) or not required_fields <= set(checkpoint):
        raise UnitAttemptConflict(
            f"cannot complete work unit {unit.id}; its exact frozen checkpoint is missing"
        )
    if checkpoint.get("unit_hash") != unit_state.unit_digest(unit):
        raise UnitAttemptConflict(
            f"cannot complete work unit {unit.id}; checkpoint unit identity changed"
        )
    return checkpoint


@_selected_state_mutation
@serialized_state_mutation(unit_state_path)
def _complete_unit_attempt(
    folder: str | Path,
    layer_id: str,
    unit_id: str,
    units: tuple[WorkUnit, ...],
    claim: UnitAttemptClaim,
    *,
    expected_plan_hash: str,
    selection_token: AuthoritySelectionToken,
    reason: str,
    evidence: list[str],
    prepared_evaluation: PreparedUnitEvaluationCompletion,
) -> UnitCompletionReceipt:
    """Accept one exact evaluated checkpoint and complete its owning attempt."""

    reason = _text(reason, "work-unit attempt completion reason")
    evidence = _evidence(evidence, "work-unit attempt completion evidence")
    value = _load_exact_state(
        folder,
        layer_id,
        units,
        expected_plan_hash=expected_plan_hash,
    )
    current = require_active_unit_attempt_in_state(
        value,
        layer_id,
        unit_id,
        units,
        claim,
        expected_plan_hash=expected_plan_hash,
        selection_token=selection_token,
    )
    slot = _slot(value, unit_id)
    if slot.get("status") != "evaluating":
        raise UnitAttemptConflict(
            f"cannot complete work unit {unit_id} from state {slot.get('status')!r}; "
            "an exact evaluated checkpoint is required"
        )
    unit = next(candidate for candidate in units if candidate.id == unit_id)
    checkpoint = _completion_checkpoint(slot, unit)
    script_relative = Path(canonical_unit_script_path(str(layer_id), unit_id))
    try:
        evaluated = require_prepared_unit_evaluation_completion(
            prepared_evaluation,
            unit,
            checkpoint,
            layer_id=str(layer_id),
            claim=current,
        )
    except UnitEvaluationConflict as exc:
        raise UnitAttemptConflict(
            f"cannot complete work unit {unit_id}; independent canonical evaluation "
            f"is not authoritative: {exc}"
        ) from exc
    at = unit_state._now()
    receipt = UnitCompletionReceipt.mint(
        claim=current,
        checkpoint=checkpoint,
        script_path=script_relative.as_posix(),
        script_hash=prepared_evaluation.script_sha256,
        evaluation_receipt_locator=prepared_evaluation.stored.locator,
        evaluation_receipt_sha256=prepared_evaluation.stored.sha256,
        evaluation_receipt_digest=evaluated.receipt_digest,
        passed_evidence=evaluated.passed_evidence,
        completed_at=at,
    )
    archive_active_attempt(
        slot,
        disposition="completed",
        reason=reason,
        evidence=evidence,
        at=at,
    )
    _append_transition(
        slot,
        before="evaluating",
        after="passed",
        reason=reason,
        claim=current,
        at=at,
        evidence=evidence,
    )
    slot["completion_receipt"] = receipt.as_dict()
    value["updated"] = at
    _write(unit_state_path(folder, layer_id), value)
    return receipt


def complete_unit_attempt(
    folder: str | Path,
    layer_id: str,
    unit_id: str,
    units: tuple[WorkUnit, ...],
    claim: UnitAttemptClaim,
    *,
    expected_plan_hash: str,
    selection_token: AuthoritySelectionToken,
    reason: str,
    evidence: list[str],
) -> UnitCompletionReceipt:
    """Prepare evaluator bytes unlocked, then accept them in one short state CAS."""

    reason = _text(reason, "work-unit attempt completion reason")
    evidence = _evidence(evidence, "work-unit attempt completion evidence")
    unit = next((candidate for candidate in units if candidate.id == unit_id), None)
    if unit is None:
        raise UnitAttemptConflict(f"unknown work unit {unit_id!r}")
    with active_unit_attempt_guard(
        folder,
        layer_id,
        unit_id,
        units,
        claim,
        expected_plan_hash=expected_plan_hash,
        selection_token=selection_token,
    ):
        state = unit_state.load(folder, layer_id)
        slot = _slot(state, unit_id)
        if slot.get("status") != "evaluating":
            raise UnitAttemptConflict(
                f"cannot complete work unit {unit_id} from state {slot.get('status')!r}; "
                "an exact evaluated checkpoint is required"
            )
        _completion_checkpoint(slot, unit)
    try:
        prepared = prepare_unit_evaluation_completion(
            folder,
            str(layer_id),
            unit,
            claim,
        )
    except UnitEvaluationConflict as exc:
        raise UnitAttemptConflict(
            f"cannot complete work unit {unit_id}; independent canonical evaluation "
            f"is not authoritative: {exc}"
        ) from exc
    return _complete_unit_attempt(
        folder,
        layer_id,
        unit_id,
        units,
        claim,
        expected_plan_hash=expected_plan_hash,
        selection_token=selection_token,
        reason=reason,
        evidence=evidence,
        prepared_evaluation=prepared,
    )


# Architecture checks follow ``__wrapped__`` to the serialized private CAS. The public
# boundary deliberately prepares large evaluator bytes before entering that mutation.
complete_unit_attempt.__wrapped__ = _complete_unit_attempt  # type: ignore[attr-defined]


@_selected_state_mutation
@serialized_state_mutation(unit_state_path)
def release_unit_attempt(
    folder: str | Path,
    layer_id: str,
    unit_id: str,
    units: tuple[WorkUnit, ...],
    claim: UnitAttemptClaim,
    *,
    expected_plan_hash: str,
    selection_token: AuthoritySelectionToken,
    reason: str,
    evidence: list[str],
    next_status: str = "retryable",
) -> dict[str, Any]:
    """Conditionally abandon one exact live claim into a reviewed restart state."""

    reason = _text(reason, "work-unit attempt release reason")
    evidence = _evidence(evidence, "work-unit attempt release evidence")
    if next_status not in ATTEMPT_RELEASE_STATES:
        raise UnitAttemptConflict(
            f"attempt release state must be one of {sorted(ATTEMPT_RELEASE_STATES)}"
        )
    value = _load_exact_state(
        folder,
        layer_id,
        units,
        expected_plan_hash=expected_plan_hash,
    )
    slot = _slot(value, unit_id)
    unit = next((candidate for candidate in units if candidate.id == unit_id), None)
    if unit is None:
        raise UnitAttemptConflict(f"unknown work unit {unit_id!r}")
    try:
        current = require_attempt_matches_slot(
            slot,
            claim,
            layer_id=str(layer_id),
            unit_id=unit.id,
            unit_digest=unit_state.unit_digest(unit),
            plan_hash=expected_plan_hash,
        )
    except ValueError as exc:
        raise UnitAttemptConflict(str(exc)) from exc
    if current is None:
        raise UnitAttemptConflict(f"work unit {unit_id} has no active attempt to release")
    if current.selection_token != _selection_projection(selection_token):
        raise UnitAttemptConflict(
            "work-unit attempt belongs to another authority selection"
        )
    before = str(slot.get("status"))
    if next_status not in unit_state._TRANSITIONS.get(before, set()):
        raise UnitAttemptConflict(
            f"cannot release work unit {unit_id}: {before} -> {next_status}"
        )
    at = unit_state._now()
    archive_active_attempt(
        slot,
        disposition="released",
        reason=reason,
        evidence=evidence,
        at=at,
    )
    _append_transition(
        slot,
        before=before,
        after=next_status,
        reason=reason,
        claim=current,
        at=at,
        evidence=evidence,
    )
    value["updated"] = at
    _write(unit_state_path(folder, layer_id), value)
    return value


@_selected_state_mutation
@serialized_state_mutation(unit_state_path)
def release_unclaimed_unit_for_retry(
    folder: str | Path,
    layer_id: str,
    unit_id: str,
    units: tuple[WorkUnit, ...],
    *,
    expected_plan_hash: str,
    selection_token: AuthoritySelectionToken,
    reason: str,
    evidence: list[str],
) -> dict[str, Any]:
    """Review one historical naked in-flight row into a fresh planning source."""

    reason = _text(reason, "unclaimed work-unit retry reason")
    evidence = _evidence(evidence, "unclaimed work-unit retry evidence")
    _selection_projection(selection_token)
    value = _load_exact_state(
        folder,
        layer_id,
        units,
        expected_plan_hash=expected_plan_hash,
    )
    slot = _slot(value, unit_id)
    if "active_attempt" in slot:
        raise UnitAttemptConflict(
            f"work unit {unit_id} has an active attempt; release its exact claim instead"
        )
    before = str(slot.get("status"))
    if before not in UNCLAIMED_RETRY_STATES:
        raise UnitAttemptConflict(
            f"cannot release unclaimed work unit {unit_id} from state {before!r}; "
            f"legal states are {sorted(UNCLAIMED_RETRY_STATES)}"
        )
    at = unit_state._now()
    archive_attempt_checkpoint(
        slot,
        claim_id=None,
        disposition="released",
        reason=reason,
        evidence=evidence,
        at=at,
    )
    slot.setdefault("history", []).append(
        {
            "at": at,
            "from": before,
            "to": "retryable",
            "reason": reason,
            "metadata": {
                "evidence": evidence,
                "reviewed_unclaimed_retry": True,
                "selection_token": selection_token.to_dict(),
            },
        }
    )
    slot["status"] = "retryable"
    slot["updated"] = at
    value["updated"] = at
    _write(unit_state_path(folder, layer_id), value)
    return value
