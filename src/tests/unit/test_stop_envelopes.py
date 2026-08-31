"""Strict stop-envelope integration and loop classifier contracts."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import replace

import pytest

from vfx_harness.domain.stop_envelopes import (
    EvidenceNotDue,
    PriorDispatchAttempt,
    StopCause,
    StopEnvelope,
    StopIdentity,
    classify_stop,
    repeated_dispatch_defect_document,
)
from vfx_harness.domain.stop_transaction_state import (
    BudgetStateAssertion,
    EvidenceRecordAssertion,
    StopEvidenceRef,
    UnitStateAssertion,
)
from vfx_harness.domain.stop_transactions import (
    TRANSACTION_RECEIPT_SCHEMA,
    EngineeringRouteCommitted,
    ExactUnitAttemptAdvanced,
    PostconditionEvaluation,
    RetryExactUnitTarget,
    RouteEngineeringTarget,
    StopAction,
    action_idempotency_key,
)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _evidence(label: str, *, kind: str = "stop_evidence") -> StopEvidenceRef:
    return StopEvidenceRef(
        kind,
        f"runs/run-1/reports/{label}.json",
        _digest(f"{label}:bytes"),
        TRANSACTION_RECEIPT_SCHEMA if kind == "transaction_receipt" else f"vfx-harness.{label}/v1",
        _digest(f"{label}:record"),
    )


def _identity(*, run_id: str = "run-1") -> StopIdentity:
    return StopIdentity(
        run_id=run_id,
        bundle_digest=_digest("bundle"),
        view_digest=_digest("view"),
        layer_id="form",
        unit_id="hall",
        unit_plan_digest=_digest("unit-plan"),
        unit_digest=_digest("unit"),
        candidate_digest=_digest("candidate"),
        checkpoint_digest=_digest("checkpoint"),
        settings_digest=_digest("settings"),
        debt_state_digest=_digest("debt-state"),
    )


def _cause() -> StopCause:
    return StopCause(
        invariant_id="canonical_contract_failed",
        finding_ids=("finding-b", "finding-a"),
        owner_scope_ids=("form:hall",),
        normalized_facts_digest=_digest("normalized facts"),
    )


def _retry_action(*, evidence_label: str = "attempt") -> StopAction:
    state = UnitStateAssertion(
        "form",
        "hall",
        _digest("unit"),
        _digest("unit-plan"),
        _digest("plan"),
        2,
        "failed",
        _digest("unit-state"),
        _digest("candidate"),
        _digest("checkpoint"),
    )
    target = RetryExactUnitTarget(
        state,
        BudgetStateAssertion(
            "unit-retry",
            _digest("budget-policy"),
            1,
            1,
            _digest("budget-state"),
        ),
        (_evidence(evidence_label),),
    )
    return StopAction(
        target,
        ExactUnitAttemptAdvanced(
            state.layer_id,
            state.unit_id,
            state.unit_digest,
            state.unit_plan_digest,
            state.revision,
            state.state_digest,
            state.candidate_digest,
            state.checkpoint_digest,
        ),
    )


def _local_stop(
    *,
    run_id: str = "run-1",
    evidence_label: str = "attempt",
    authoritative_before: str = "authoritative before",
) -> StopEnvelope:
    action = _retry_action(evidence_label=evidence_label)
    return StopEnvelope(
        stage="builder",
        stop_class="local_implementation_miss",
        identity=_identity(run_id=run_id),
        cause=_cause(),
        attempt_evidence_digest=_digest("attempt evidence"),
        classification_evidence_digest=_digest("classification evidence"),
        artifact_state_digest=_digest("artifact state"),
        authoritative_before_digest=_digest(authoritative_before),
        actions=(action,),
        evidence_refs=action.target.evidence,
        budget_key="unit-retry",
        expected="The exact unit satisfies its declared contract.",
        found="The canonical reading violates the bound contract.",
        next_action="Retry the exact current unit within its remaining budget.",
    )


def _evaluation(
    envelope: StopEnvelope,
    *,
    result: str,
    after: str = "same",
) -> PostconditionEvaluation:
    action = envelope.actions[0]
    return PostconditionEvaluation(
        action_digest=action.digest,
        postcondition_digest=action.postcondition.digest,
        idempotency_key=action_idempotency_key(
            action,
            authoritative_before_digest=envelope.authoritative_before_digest,
            attempt_evidence_digest=envelope.attempt_evidence_digest,
        ),
        evaluator_id=action.evaluator_id,
        authoritative_before_digest=envelope.authoritative_before_digest,
        authoritative_after_digest=(envelope.authoritative_before_digest if after == "same" else _digest(after)),
        observed_records=(_evidence("receipt", kind="transaction_receipt"),),
        result=result,
    )


def test_stop_envelope_round_trips_with_derived_retry_and_sealed_before_state() -> None:
    envelope = _local_stop()
    assert envelope.retryable is True
    assert envelope.authoritative_before_digest == _digest("authoritative before")
    assert envelope.authoritative_before_digest != envelope.actions[0].precondition_digest
    assert StopEnvelope.from_dict(envelope.as_dict(), "envelope") == envelope
    assert envelope.cause.finding_ids == ("finding-a", "finding-b")

    changed_run = _local_stop(run_id="run-2")
    assert changed_run.cause_fingerprint == envelope.cause_fingerprint
    assert changed_run.digest != envelope.digest


def test_stop_envelope_rejects_stale_unknown_multiple_actions_and_wrong_stage() -> None:
    envelope = _local_stop()
    stale = deepcopy(envelope.as_dict())
    stale["retryable"] = False
    with pytest.raises(ValueError, match="retryable is stale"):
        StopEnvelope.from_dict(stale, "envelope")

    unknown = deepcopy(envelope.as_dict())
    unknown["unexpected"] = True
    with pytest.raises(ValueError, match="fields mismatch"):
        StopEnvelope.from_dict(unknown, "envelope")

    with pytest.raises(ValueError, match="exactly one"):
        StopEnvelope(
            stage=envelope.stage,
            stop_class=envelope.stop_class,
            identity=envelope.identity,
            cause=envelope.cause,
            attempt_evidence_digest=envelope.attempt_evidence_digest,
            classification_evidence_digest=envelope.classification_evidence_digest,
            artifact_state_digest=envelope.artifact_state_digest,
            authoritative_before_digest=envelope.authoritative_before_digest,
            actions=(envelope.actions[0], envelope.actions[0]),
            evidence_refs=envelope.evidence_refs,
            budget_key=envelope.budget_key,
            expected=envelope.expected,
            found=envelope.found,
            next_action=envelope.next_action,
        )

    with pytest.raises(ValueError, match="illegal at stop stage"):
        StopEnvelope(
            stage="plan_gate",
            stop_class=envelope.stop_class,
            identity=envelope.identity,
            cause=envelope.cause,
            attempt_evidence_digest=envelope.attempt_evidence_digest,
            classification_evidence_digest=envelope.classification_evidence_digest,
            artifact_state_digest=envelope.artifact_state_digest,
            authoritative_before_digest=envelope.authoritative_before_digest,
            actions=envelope.actions,
            evidence_refs=envelope.evidence_refs,
            budget_key=envelope.budget_key,
            expected=envelope.expected,
            found=envelope.found,
            next_action=envelope.next_action,
        )


def test_unit_identity_is_all_or_none() -> None:
    with pytest.raises(ValueError, match="must appear together"):
        StopEnvelope(
            stage="builder",
            stop_class="local_implementation_miss",
            identity=StopIdentity(
                "run-1",
                _digest("bundle"),
                _digest("view"),
                "form",
                "hall",
                None,
                _digest("unit"),
                _digest("candidate"),
                _digest("checkpoint"),
                _digest("settings"),
                None,
            ),
            cause=_cause(),
            attempt_evidence_digest=_digest("attempt"),
            classification_evidence_digest=_digest("classification"),
            artifact_state_digest=_digest("state"),
            authoritative_before_digest=_digest("authority"),
            actions=(_retry_action(),),
            evidence_refs=_retry_action().target.evidence,
            budget_key="unit-retry",
            expected="Expected.",
            found="Found.",
            next_action="Retry.",
        )


def test_evidence_not_due_is_a_tagged_continuation_not_stop() -> None:
    progress = EvidenceNotDue(
        definition_digest=_digest("definition"),
        debt_state_digest=_digest("pending debt state"),
        provider_activation_digest=_digest("provider activation"),
    )
    assert classify_stop(progress) is progress
    assert EvidenceNotDue.from_dict(progress.as_dict(), "progress") == progress
    assert "stop_class" not in progress.as_dict()


def test_unsatisfied_repetition_stops_before_unimplemented_receipt_routing() -> None:
    original = _local_stop()
    prior = PriorDispatchAttempt(
        cause_fingerprint=original.cause_fingerprint,
        authoritative_before_digest=original.authoritative_before_digest,
        attempt_evidence_digest=original.attempt_evidence_digest,
        action=original.actions[0],
        evaluation=_evaluation(original, result="unsatisfied"),
    )
    assert PriorDispatchAttempt.from_dict(prior.as_dict(), "prior") == prior

    document = repeated_dispatch_defect_document(original, prior)
    assert document["schema"] == "vfx-harness.repeated-dispatch-defect/v1"
    assert document["receipt_evidence_digests"] == [
        prior.evaluation.observed_records[0].digest
    ]

    with pytest.raises(ValueError, match="routing remains blocked"):
        classify_stop(original, (prior,))

    changed_classification = replace(
        original,
        classification_evidence_digest=_digest("changed classification"),
        artifact_state_digest=_digest("changed artifact state"),
    )
    changed_stage = replace(original, stage="composition")
    assert repeated_dispatch_defect_document(changed_classification, prior) != document
    assert repeated_dispatch_defect_document(changed_stage, prior) != document


def test_same_authority_and_cause_loops_when_exact_attempt_evidence_changes() -> None:
    original = _local_stop(evidence_label="attempt-a")
    changed_evidence = _local_stop(evidence_label="attempt-b")
    assert changed_evidence.authoritative_before_digest == original.authoritative_before_digest
    assert changed_evidence.actions[0].precondition_digest != original.actions[0].precondition_digest
    assert changed_evidence.actions[0].digest != original.actions[0].digest
    prior = PriorDispatchAttempt(
        cause_fingerprint=original.cause_fingerprint,
        authoritative_before_digest=original.authoritative_before_digest,
        attempt_evidence_digest=original.attempt_evidence_digest,
        action=original.actions[0],
        evaluation=_evaluation(original, result="unsatisfied"),
    )

    with pytest.raises(ValueError, match="routing remains blocked"):
        classify_stop(changed_evidence, (prior,))

    advanced_authority = _local_stop(
        evidence_label="attempt-b",
        authoritative_before="advanced authority",
    )
    assert classify_stop(advanced_authority, (prior,)) is advanced_authority


def test_satisfied_typed_evaluation_does_not_trigger_loop() -> None:
    original = _local_stop()
    prior = PriorDispatchAttempt(
        cause_fingerprint=original.cause_fingerprint,
        authoritative_before_digest=original.authoritative_before_digest,
        attempt_evidence_digest=original.attempt_evidence_digest,
        action=original.actions[0],
        evaluation=_evaluation(original, result="satisfied", after="advanced"),
    )
    assert classify_stop(original, (prior,)) is original


def test_classifier_refuses_untyped_input_and_bare_boolean_history() -> None:
    with pytest.raises(ValueError, match="typed StopEnvelope or EvidenceNotDue"):
        classify_stop({"status": "contract_gap"})
    with pytest.raises(ValueError, match="PriorDispatchAttempt"):
        classify_stop(_local_stop(), ("failed",))

    with pytest.raises(TypeError):
        PriorDispatchAttempt(  # type: ignore[call-arg]
            cause_fingerprint=_digest("cause"),
            authoritative_before_digest=_digest("before"),
            postcondition_satisfied=False,
        )


def test_route_action_must_bind_envelope_cause_and_attempt() -> None:
    evidence = _evidence("defect")
    defect = EvidenceRecordAssertion("defect", "defect-1", evidence)
    target = RouteEngineeringTarget(
        _digest("wrong-cause"),
        _digest("attempt"),
        ("harness",),
        defect,
        (evidence,),
        "engineering_handoff",
    )
    action = StopAction(
        target,
        EngineeringRouteCommitted(
            evidence.record_digest,
            target.sink_id,
            target.owner_scope_ids,
        ),
    )
    with pytest.raises(ValueError, match="exact stop cause"):
        StopEnvelope(
            stage="infrastructure",
            stop_class="harness_defect",
            identity=StopIdentity(
                "run-1",
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
            ),
            cause=StopCause(
                "missing_typed_stop",
                ("finding-1",),
                ("observability",),
                _digest("facts"),
            ),
            attempt_evidence_digest=_digest("attempt"),
            classification_evidence_digest=_digest("classification"),
            artifact_state_digest=_digest("state"),
            authoritative_before_digest=_digest("authority"),
            actions=(action,),
            evidence_refs=(evidence,),
            budget_key="engineering-route",
            expected="A typed stop exists.",
            found="It does not.",
            next_action="Route to engineering.",
        )
