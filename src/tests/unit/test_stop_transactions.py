"""Strict typed stop-transaction contracts and receipt-bound evaluation."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import replace

import pytest

from vfx_harness.domain.stop_transaction_state import (
    BudgetStateAssertion,
    EnvironmentResultAssertion,
    EvidenceRecordAssertion,
    ResumeRecordAssertion,
    SelectedAuthorityAssertionV2,
    SelectedAuthorityBundle,
    SelectedAuthorityView,
    SelectedBundleAssertion,
    SelectedViewAssertion,
    StopEvidenceRef,
    UnitStateAssertion,
)
from vfx_harness.domain.stop_transactions import (
    TRANSACTION_RECEIPT_SCHEMA,
    ApplyRevisionCheckedReplanTarget,
    CheckpointedSessionAdvanced,
    EngineeringRouteCommitted,
    EnvironmentReverified,
    EscalateQuestionTarget,
    ExactUnitAttemptAdvanced,
    HumanDecisionCommitted,
    PostconditionEvaluation,
    PublishValidatedAmendmentTarget,
    RecoverEnvironmentTarget,
    ResumeCheckpointedSessionTarget,
    RetryExactUnitTarget,
    RevisionCheckedReplanCommitted,
    RouteEngineeringTarget,
    SelectedAuthorityAmendmentCommitted,
    StopAction,
    action_idempotency_key,
)

_GATE_POLICY = "structural-authority/runtime-falsification-v1"
_GATE_SCHEMA = "vfx-harness.plan-gate/v1"
_VALIDATION_SCOPE = "structural_authority"


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _record(
    label: str,
    *,
    kind: str = "stop_evidence",
    record_schema: str | None = None,
) -> StopEvidenceRef:
    return StopEvidenceRef(
        kind=kind,
        locator=f"runs/run-1/reports/{label}.json",
        sha256=_digest(f"{label}:bytes"),
        record_schema=record_schema or f"vfx-harness.{label}/v1",
        record_digest=_digest(f"{label}:record"),
    )


def _blob(label: str, kind: str) -> StopEvidenceRef:
    return StopEvidenceRef(
        kind=kind,
        locator=f"runs/run-1/checkpoints/{label}",
        sha256=_digest(label),
        record_schema=None,
        record_digest=None,
    )


def _bundle(label: str = "bundle") -> SelectedBundleAssertion:
    return SelectedBundleAssertion(_digest(label), _digest(f"{label}:selection"))


def _view(bundle: SelectedBundleAssertion, label: str = "view") -> SelectedViewAssertion:
    assert bundle.bundle_digest is not None
    return SelectedViewAssertion(
        bundle.bundle_digest,
        _digest(label),
        _digest(f"{label}:selection"),
    )


def _authority(
    label: str = "authority",
    *,
    source: str = "jit",
) -> SelectedAuthorityAssertionV2:
    bundle_digest = _digest(f"{label}:bundle")
    return SelectedAuthorityAssertionV2(
        "selected",
        SelectedAuthorityBundle(
            bundle_digest,
            "clean",
            _digest(f"{label}:bundle-manifest"),
        ),
        SelectedAuthorityView(
            source,
            bundle_digest if source == "bundle" else _digest(f"{label}:view"),
            _digest(f"{label}:view-manifest"),
        ),
    )


def _unit(*, status: str = "failed", checkpoint: str | None = "checkpoint") -> UnitStateAssertion:
    return UnitStateAssertion(
        layer_id="form",
        unit_id="hall",
        unit_digest=_digest("unit"),
        unit_plan_digest=_digest("unit-plan"),
        plan_digest=_digest("old-plan"),
        revision=3,
        status=status,
        state_digest=_digest("unit-state"),
        candidate_digest=_digest("candidate"),
        checkpoint_digest=None if checkpoint is None else _digest(checkpoint),
    )


def _budget() -> BudgetStateAssertion:
    return BudgetStateAssertion(
        "unit-retry",
        _digest("budget-policy"),
        1,
        2,
        _digest("budget-state"),
    )


def _assertion(label: str, kind: str) -> EvidenceRecordAssertion:
    return EvidenceRecordAssertion(kind, f"{kind}-{label}", _record(label))


def _retry_action() -> StopAction:
    state = _unit()
    target = RetryExactUnitTarget(state, _budget(), (_record("retry-evidence"),))
    return StopAction(
        target=target,
        postcondition=ExactUnitAttemptAdvanced(
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


def _all_actions() -> tuple[StopAction, ...]:
    retry = _retry_action()
    base_authority = _authority("old-authority")
    findings = (_assertion("plan-finding", "finding"),)
    publish_target = PublishValidatedAmendmentTarget(
        "layer_view",
        base_authority,
        "form",
        findings,
        "plan-authority",
        _GATE_POLICY,
        _GATE_SCHEMA,
        _VALIDATION_SCOPE,
    )
    publish = StopAction(
        publish_target,
        SelectedAuthorityAmendmentCommitted(
            "layer_view",
            base_authority.digest,
            "form",
            tuple(row.record_id for row in findings),
            "plan-authority",
            _GATE_POLICY,
            _GATE_SCHEMA,
            _VALIDATION_SCOPE,
            "jit",
        ),
    )

    target_bundle = _bundle("target-bundle")
    target_view = _view(target_bundle, "target-view")
    before = _unit(status="hypothesis_falsified")
    falsification = _assertion("falsification", "finding")
    replan_target = ApplyRevisionCheckedReplanTarget(
        "base-run",
        _digest("old-bundle"),
        target_bundle,
        target_view,
        "form",
        before,
        falsification,
        _digest("target-plan"),
        _digest("effects"),
        "replan-authority",
    )
    replan = StopAction(
        replan_target,
        RevisionCheckedReplanCommitted(
            "form",
            _digest("old-bundle"),
            target_bundle.bundle_digest,
            target_view.view_digest,
            _digest("target-plan"),
            before.revision,
            before.state_digest,
            falsification.record_id,
            _digest("effects"),
        ),
    )

    defect = _assertion("defect", "defect")
    route_target = RouteEngineeringTarget(
        _digest("cause"),
        _digest("attempt"),
        ("harness",),
        defect,
        (defect.evidence,),
        "engineering_handoff",
    )
    route = StopAction(
        route_target,
        EngineeringRouteCommitted(
            defect.evidence.record_digest,
            "engineering_handoff",
            ("harness",),
        ),
    )

    environment = EnvironmentResultAssertion(
        "preflight",
        _digest("probe-spec"),
        _digest("failed-environment"),
    )
    recover_target = RecoverEnvironmentTarget(
        environment,
        ("blender_executable",),
        "external_operator",
        (_record("environment", kind="environment_result"),),
    )
    recover = StopAction(
        recover_target,
        EnvironmentReverified(
            environment.probe_id,
            environment.probe_spec_digest,
            environment.result_digest,
            recover_target.failed_check_ids,
        ),
    )

    bundle = _bundle()
    view = _view(bundle)
    state = _unit(status="failed")
    checkpoint = _blob("checkpoint", "checkpoint")
    resume_record = ResumeRecordAssertion(
        "form",
        "hall",
        state.unit_digest,
        "sdk-session-1",
        checkpoint,
        _blob("journal.py", "journal"),
        8,
        4,
        _digest("ledger"),
    )
    resume_target = ResumeCheckpointedSessionTarget(
        bundle,
        view,
        state,
        resume_record,
        _budget(),
        (_record("resume-evidence"),),
    )
    resume = StopAction(
        resume_target,
        CheckpointedSessionAdvanced(
            state.layer_id,
            state.unit_id,
            state.unit_digest,
            resume_record.digest,
            state.state_digest,
            state.candidate_digest,
            state.checkpoint_digest,
        ),
    )

    question = _assertion("question", "question")
    escalate_target = EscalateQuestionTarget(
        question,
        _digest("question"),
        "human-producer",
        "vfx-harness.human-decision/v1",
        ("repair-form", "repair-camera"),
        (question.evidence,),
    )
    escalate = StopAction(
        escalate_target,
        HumanDecisionCommitted(
            escalate_target.question_digest,
            escalate_target.decision_authority_id,
            escalate_target.decision_schema,
            escalate_target.allowed_answer_ids,
        ),
    )
    return retry, publish, replan, route, recover, resume, escalate


def test_all_seven_transaction_actions_round_trip_with_derived_policy() -> None:
    actions = _all_actions()
    assert {action.transaction_id for action in actions} == {
        "retry_exact_unit",
        "publish_validated_amendment",
        "apply_revision_checked_replan",
        "route_engineering",
        "recover_environment",
        "resume_checkpointed_session",
        "escalate_question",
    }
    assert {action.dispatch_mode for action in actions} == {
        "automatic",
        "terminal_route",
        "external_recovery",
        "human_handoff",
    }
    for action in actions:
        assert StopAction.from_dict(action.as_dict(), "action") == action


def test_global_amendment_can_bind_an_absent_initial_selection() -> None:
    base = SelectedAuthorityAssertionV2("absent", None, None)
    finding = _assertion("initial-plan-finding", "finding")
    action = StopAction(
        PublishValidatedAmendmentTarget(
            "global_plan",
            base,
            None,
            (finding,),
            "plan-authority",
            _GATE_POLICY,
            _GATE_SCHEMA,
            _VALIDATION_SCOPE,
        ),
        SelectedAuthorityAmendmentCommitted(
            "global_plan",
            base.digest,
            None,
            (finding.record_id,),
            "plan-authority",
            _GATE_POLICY,
            _GATE_SCHEMA,
            _VALIDATION_SCOPE,
            "bundle",
        ),
    )

    assert StopAction.from_dict(action.as_dict(), "action") == action


@pytest.mark.parametrize("source", ["bundle", "jit"])
def test_layer_amendment_requires_selected_effective_authority(source: str) -> None:
    base = _authority(f"layer-{source}", source=source)
    finding = _assertion(f"layer-{source}", "finding")
    target = PublishValidatedAmendmentTarget(
        "layer_view",
        base,
        "form",
        (finding,),
        "plan-authority",
        _GATE_POLICY,
        _GATE_SCHEMA,
        _VALIDATION_SCOPE,
    )
    action = StopAction(
        target,
        SelectedAuthorityAmendmentCommitted(
            "layer_view",
            base.digest,
            "form",
            (finding.record_id,),
            "plan-authority",
            _GATE_POLICY,
            _GATE_SCHEMA,
            _VALIDATION_SCOPE,
            "jit",
        ),
    )

    assert action.preconditions == tuple(sorted((base, finding), key=lambda item: item.digest))
    assert StopAction.from_dict(action.as_dict(), "action") == action


def test_layer_amendment_refuses_absent_authority() -> None:
    with pytest.raises(ValueError, match="requires selected"):
        PublishValidatedAmendmentTarget(
            "layer_view",
            SelectedAuthorityAssertionV2("absent", None, None),
            "form",
            (_assertion("layer-absent", "finding"),),
            "plan-authority",
            _GATE_POLICY,
            _GATE_SCHEMA,
            _VALIDATION_SCOPE,
        )


@pytest.mark.parametrize(
    ("scope", "required_after_source"),
    [("global_plan", "jit"), ("layer_view", "bundle")],
)
def test_amendment_postcondition_refuses_wrong_required_after_source(
    scope: str,
    required_after_source: str,
) -> None:
    with pytest.raises(ValueError, match="required_after_source"):
        SelectedAuthorityAmendmentCommitted(
            scope,
            _digest("base-authority"),
            None if scope == "global_plan" else "form",
            ("finding-1",),
            "plan-authority",
            _GATE_POLICY,
            _GATE_SCHEMA,
            _VALIDATION_SCOPE,
            required_after_source,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("gate_policy_id", ""),
        ("gate_schema", "vfx-harness.plan-gate/v2"),
        ("validation_scope", "runtime_evidence"),
    ],
)
def test_amendment_target_refuses_unbound_gate_contract(field: str, value: str) -> None:
    kwargs = {
        "scope": "global_plan",
        "base_authority": SelectedAuthorityAssertionV2("absent", None, None),
        "layer_id": None,
        "findings": (_assertion("invalid-gate", "finding"),),
        "owner_authority_id": "plan-authority",
        "gate_policy_id": _GATE_POLICY,
        "gate_schema": _GATE_SCHEMA,
        "validation_scope": _VALIDATION_SCOPE,
    }
    kwargs[field] = value
    with pytest.raises(ValueError, match=field):
        PublishValidatedAmendmentTarget(**kwargs)


def test_amendment_postcondition_must_bind_owner_and_gate_contract_exactly() -> None:
    action = _all_actions()[1]
    assert isinstance(action.postcondition, SelectedAuthorityAmendmentCommitted)
    for field, value in (
        ("owner_authority_id", "other-authority"),
        ("gate_policy_id", "other-policy"),
    ):
        with pytest.raises(ValueError, match="exact transaction target"):
            StopAction(action.target, replace(action.postcondition, **{field: value}))


def test_amendment_v1_and_legacy_field_shapes_are_rejected() -> None:
    action = _all_actions()[1]

    stale_target_schema = deepcopy(action.as_dict())
    stale_target_schema["target"]["schema"] = "vfx-harness.stop-target.publish-validated-amendment/v1"
    with pytest.raises(ValueError, match="supported stop transaction target"):
        StopAction.from_dict(stale_target_schema, "action")

    stale_postcondition_schema = deepcopy(action.as_dict())
    stale_postcondition_schema["postcondition"]["schema"] = (
        "vfx-harness.stop-postcondition.selected-authority-amendment/v1"
    )
    with pytest.raises(ValueError, match="supported stop postcondition"):
        StopAction.from_dict(stale_postcondition_schema, "action")

    legacy_target_fields = deepcopy(action.as_dict())
    target = legacy_target_fields["target"]
    target.pop("base_authority")
    target.update(
        {
            "base_bundle": _bundle("legacy").as_dict(),
            "base_view": None,
            "changes_hard_constraint": False,
        }
    )
    with pytest.raises(ValueError, match="fields mismatch"):
        StopAction.from_dict(legacy_target_fields, "action")

    legacy_postcondition_fields = deepcopy(action.as_dict())
    postcondition = legacy_postcondition_fields["postcondition"]
    postcondition.pop("base_authority_digest")
    postcondition.pop("required_after_source")
    postcondition.update(
        {
            "base_bundle_digest": _digest("legacy-bundle"),
            "base_view_digest": _digest("legacy-view"),
        }
    )
    with pytest.raises(ValueError, match="fields mismatch"):
        StopAction.from_dict(legacy_postcondition_fields, "action")


def test_action_refuses_opaque_or_mismatched_postcondition() -> None:
    action = _retry_action()
    stale = deepcopy(action.as_dict())
    stale["postcondition"]["before_state_digest"] = _digest("substituted")
    with pytest.raises(ValueError, match=r"stale|bind"):
        StopAction.from_dict(stale, "action")

    with pytest.raises(ValueError, match="kinds do not match"):
        StopAction(
            action.target,
            EnvironmentReverified(
                "preflight",
                _digest("probe"),
                _digest("result"),
                ("configuration",),
            ),
        )


def test_evidence_refs_reject_paths_unknown_fields_and_untyped_records() -> None:
    with pytest.raises(ValueError, match="shot-relative"):
        StopEvidenceRef("run_report", "../escape.json", _digest("x"), "schema/v1", _digest("x"))
    with pytest.raises(ValueError, match="typed record"):
        StopEvidenceRef("run_report", "runs/r/report.json", _digest("x"), None, None)

    row = _record("strict").as_dict()
    row["command"] = "rm"
    with pytest.raises(ValueError, match="fields mismatch"):
        StopEvidenceRef.from_dict(row, "evidence")


def test_idempotency_key_binds_action_scope_not_human_detail() -> None:
    layer_action = _all_actions()[1]
    target = layer_action.target
    assert isinstance(target, PublishValidatedAmendmentTarget)
    global_target = PublishValidatedAmendmentTarget(
        "global_plan",
        target.base_authority,
        None,
        target.findings,
        target.owner_authority_id,
        target.gate_policy_id,
        target.gate_schema,
        target.validation_scope,
    )
    global_action = StopAction(
        global_target,
        SelectedAuthorityAmendmentCommitted(
            "global_plan",
            target.base_authority.digest,
            None,
            tuple(row.record_id for row in target.findings),
            target.owner_authority_id,
            target.gate_policy_id,
            target.gate_schema,
            target.validation_scope,
            "bundle",
        ),
    )
    before = _digest("before")
    attempt = _digest("attempt")
    assert action_idempotency_key(
        layer_action,
        authoritative_before_digest=before,
        attempt_evidence_digest=attempt,
    ) != action_idempotency_key(
        global_action,
        authoritative_before_digest=before,
        attempt_evidence_digest=attempt,
    )


def test_action_identity_excludes_regenerable_evidence_locator() -> None:
    action = _retry_action()
    original_evidence = action.target.evidence[0]
    republished_evidence = replace(
        original_evidence,
        locator="runs/run-2/reports/retry-evidence.json",
    )
    republished = StopAction(
        replace(action.target, evidence=(republished_evidence,)),
        action.postcondition,
    )

    assert original_evidence.as_dict() != republished_evidence.as_dict()
    assert original_evidence.digest == republished_evidence.digest
    assert action.target.digest == republished.target.digest
    assert action.precondition_digest == republished.precondition_digest
    assert action.digest == republished.digest
    assert action_idempotency_key(
        action,
        authoritative_before_digest=action.precondition_digest,
        attempt_evidence_digest=_digest("attempt"),
    ) == action_idempotency_key(
        republished,
        authoritative_before_digest=republished.precondition_digest,
        attempt_evidence_digest=_digest("attempt"),
    )


def test_same_authority_with_changed_exact_evidence_has_a_new_action_key() -> None:
    action = _retry_action()
    original_evidence = action.target.evidence[0]
    changed_evidence = replace(
        original_evidence,
        sha256=_digest("changed bytes"),
        record_digest=_digest("changed record"),
    )
    changed = StopAction(
        replace(action.target, evidence=(changed_evidence,)),
        action.postcondition,
    )
    authoritative_before = _digest("same authoritative domain state")
    attempt = _digest("same attempt envelope")

    assert action.precondition_digest != changed.precondition_digest
    assert action.digest != changed.digest
    assert action_idempotency_key(
        action,
        authoritative_before_digest=authoritative_before,
        attempt_evidence_digest=attempt,
    ) != action_idempotency_key(
        changed,
        authoritative_before_digest=authoritative_before,
        attempt_evidence_digest=attempt,
    )


def test_postcondition_evaluation_requires_receipt_and_exact_action_key() -> None:
    action = _retry_action()
    before = action.precondition_digest
    attempt = _digest("attempt")
    key = action_idempotency_key(
        action,
        authoritative_before_digest=before,
        attempt_evidence_digest=attempt,
    )
    with pytest.raises(ValueError, match="transaction receipt"):
        PostconditionEvaluation(
            action.digest,
            action.postcondition.digest,
            key,
            action.evaluator_id,
            before,
            _digest("after"),
            (_record("not-a-receipt"),),
            "satisfied",
        )

    with pytest.raises(ValueError, match="transaction-receipt/v1"):
        PostconditionEvaluation(
            action.digest,
            action.postcondition.digest,
            key,
            action.evaluator_id,
            before,
            _digest("after"),
            (_record("wrong-schema", kind="transaction_receipt"),),
            "satisfied",
        )

    receipt = _record(
        "receipt",
        kind="transaction_receipt",
        record_schema=TRANSACTION_RECEIPT_SCHEMA,
    )
    with pytest.raises(ValueError, match="exactly one transaction receipt"):
        PostconditionEvaluation(
            action.digest,
            action.postcondition.digest,
            key,
            action.evaluator_id,
            before,
            _digest("after"),
            (
                receipt,
                _record(
                    "second-receipt",
                    kind="transaction_receipt",
                    record_schema=TRANSACTION_RECEIPT_SCHEMA,
                ),
            ),
            "satisfied",
        )
    evaluation = PostconditionEvaluation(
        action.digest,
        action.postcondition.digest,
        key,
        action.evaluator_id,
        before,
        _digest("after"),
        (receipt,),
        "satisfied",
    )
    evaluation.assert_matches(
        action,
        authoritative_before_digest=before,
        attempt_evidence_digest=attempt,
    )
    assert PostconditionEvaluation.from_dict(evaluation.as_dict(), "evaluation") == evaluation
