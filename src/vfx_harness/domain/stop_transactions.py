"""Pure, closed stop transactions whose digests authenticate typed payloads."""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any, ClassVar, TypeAlias

from vfx_harness.domain.stop_envelope_primitives import (
    canonical_digest,
    list_value,
    record,
    require_canonical_digest,
    require_digest,
    require_id,
    require_optional_digest,
    require_text,
    require_text_tuple,
    text_tuple_from_list,
)
from vfx_harness.domain.stop_transaction_state import (
    BudgetStateAssertion,
    EnvironmentResultAssertion,
    EvidenceRecordAssertion,
    ResumeRecordAssertion,
    SelectedBundleAssertion,
    SelectedViewAssertion,
    StopEvidenceRef,
    StopStateAssertion,
    UnitStateAssertion,
    _assertions,
    _evidence_tuple,
    _finish,
    _positive_int,
    _row,
    _StrictRecord,
    state_assertion_from_dict,
)

TRANSACTION_RECEIPT_SCHEMA = "vfx-harness.transaction-receipt/v1"
AUTHORITY_SCOPES = frozenset({"global_plan", "layer_view"})
ENGINEERING_SINK_IDS = frozenset({"engineering_handoff"})
RECOVERY_ADAPTER_IDS = frozenset({"external_operator"})


@dataclass(frozen=True, slots=True)
class RetryExactUnitTarget(_StrictRecord):
    SCHEMA: ClassVar[str] = "vfx-harness.stop-target.retry-exact-unit/v1"
    DIGEST_FIELD: ClassVar[str] = "target_digest"
    unit_state: UnitStateAssertion
    budget_state: BudgetStateAssertion
    evidence: tuple[StopEvidenceRef, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.unit_state, UnitStateAssertion) or self.unit_state.status != "failed":
            raise ValueError("RetryExactUnitTarget requires an exact failed unit state")
        if not isinstance(self.budget_state, BudgetStateAssertion) or self.budget_state.remaining_attempts < 1:
            raise ValueError("RetryExactUnitTarget requires remaining typed retry budget")
        object.__setattr__(self, "evidence", _evidence_tuple(self.evidence, "RetryExactUnitTarget.evidence"))


@dataclass(frozen=True, slots=True)
class PublishValidatedAmendmentTarget(_StrictRecord):
    SCHEMA: ClassVar[str] = "vfx-harness.stop-target.publish-validated-amendment/v1"
    DIGEST_FIELD: ClassVar[str] = "target_digest"
    scope: str
    base_bundle: SelectedBundleAssertion
    base_view: SelectedViewAssertion | None
    layer_id: str | None
    findings: tuple[EvidenceRecordAssertion, ...]
    owner_authority_id: str
    changes_hard_constraint: bool

    def __post_init__(self) -> None:
        if self.scope not in AUTHORITY_SCOPES:
            raise ValueError(f"PublishValidatedAmendmentTarget.scope must be one of {sorted(AUTHORITY_SCOPES)}")
        if not isinstance(self.base_bundle, SelectedBundleAssertion):
            raise ValueError("PublishValidatedAmendmentTarget requires selected bundle state")
        if self.base_view is not None and (
            not isinstance(self.base_view, SelectedViewAssertion)
            or self.base_view.bundle_digest != self.base_bundle.bundle_digest
        ):
            raise ValueError("amendment base view must belong to its base bundle")
        if self.scope == "layer_view":
            require_id(self.layer_id, "PublishValidatedAmendmentTarget.layer_id")
            if self.base_bundle.bundle_digest is None:
                raise ValueError("layer-view amendment requires a selected base bundle")
        elif self.layer_id is not None:
            raise ValueError("global-plan amendment cannot name a layer target")
        if not isinstance(self.findings, tuple) or not self.findings:
            raise ValueError("PublishValidatedAmendmentTarget.findings must be non-empty")
        if any(
            not isinstance(item, EvidenceRecordAssertion) or item.record_kind != "finding" for item in self.findings
        ):
            raise ValueError("PublishValidatedAmendmentTarget.findings must be finding assertions")
        if len({item.digest for item in self.findings}) != len(self.findings):
            raise ValueError("PublishValidatedAmendmentTarget.findings contains duplicates")
        object.__setattr__(self, "findings", tuple(sorted(self.findings, key=lambda item: item.digest)))
        require_id(self.owner_authority_id, "PublishValidatedAmendmentTarget.owner_authority_id")
        if self.changes_hard_constraint is not False:
            raise ValueError("automatic validated amendment cannot change a hard constraint")


@dataclass(frozen=True, slots=True)
class ApplyRevisionCheckedReplanTarget(_StrictRecord):
    SCHEMA: ClassVar[str] = "vfx-harness.stop-target.apply-revision-checked-replan/v1"
    DIGEST_FIELD: ClassVar[str] = "target_digest"
    base_bundle_run_id: str
    base_bundle_digest: str | None
    target_bundle: SelectedBundleAssertion
    target_view: SelectedViewAssertion | None
    layer_id: str
    before_unit_state: UnitStateAssertion
    falsification: EvidenceRecordAssertion
    target_plan_digest: str
    expected_effects_digest: str
    owner_authority_id: str

    def __post_init__(self) -> None:
        require_id(self.base_bundle_run_id, "ApplyRevisionCheckedReplanTarget.base_bundle_run_id")
        require_digest(self.base_bundle_digest, "ApplyRevisionCheckedReplanTarget.base_bundle_digest")
        if not isinstance(self.target_bundle, SelectedBundleAssertion):
            raise ValueError("ApplyRevisionCheckedReplanTarget requires selected target bundle state")
        if self.target_bundle.bundle_digest is None:
            raise ValueError("replan target bundle must be a selected immutable generation")
        if self.target_view is not None and self.target_view.bundle_digest != self.target_bundle.bundle_digest:
            raise ValueError("replan target view must belong to its target bundle")
        require_id(self.layer_id, "ApplyRevisionCheckedReplanTarget.layer_id")
        if (
            not isinstance(self.before_unit_state, UnitStateAssertion)
            or self.before_unit_state.layer_id != self.layer_id
        ):
            raise ValueError("replan before-state must belong to the target layer")
        if not isinstance(self.falsification, EvidenceRecordAssertion) or self.falsification.record_kind != "finding":
            raise ValueError("replan requires one exact falsification finding")
        require_digest(self.target_plan_digest, "ApplyRevisionCheckedReplanTarget.target_plan_digest")
        require_digest(self.expected_effects_digest, "ApplyRevisionCheckedReplanTarget.expected_effects_digest")
        require_id(self.owner_authority_id, "ApplyRevisionCheckedReplanTarget.owner_authority_id")
        authority_changed = (
            self.base_bundle_digest != self.target_bundle.bundle_digest
            or self.before_unit_state.plan_digest != self.target_plan_digest
            or self.target_view is not None
        )
        if not authority_changed:
            raise ValueError("revision-checked replan requires already-selected amended authority")


@dataclass(frozen=True, slots=True)
class RouteEngineeringTarget(_StrictRecord):
    SCHEMA: ClassVar[str] = "vfx-harness.stop-target.route-engineering/v1"
    DIGEST_FIELD: ClassVar[str] = "target_digest"
    cause_fingerprint: str
    attempt_evidence_digest: str
    owner_scope_ids: tuple[str, ...]
    defect_record: EvidenceRecordAssertion
    evidence: tuple[StopEvidenceRef, ...]
    sink_id: str

    def __post_init__(self) -> None:
        require_digest(self.cause_fingerprint, "RouteEngineeringTarget.cause_fingerprint")
        require_digest(self.attempt_evidence_digest, "RouteEngineeringTarget.attempt_evidence_digest")
        object.__setattr__(
            self, "owner_scope_ids", require_text_tuple(self.owner_scope_ids, "RouteEngineeringTarget.owner_scope_ids")
        )
        if not isinstance(self.defect_record, EvidenceRecordAssertion) or self.defect_record.record_kind != "defect":
            raise ValueError("RouteEngineeringTarget.defect_record must be a defect assertion")
        object.__setattr__(self, "evidence", _evidence_tuple(self.evidence, "RouteEngineeringTarget.evidence"))
        if self.sink_id not in ENGINEERING_SINK_IDS:
            raise ValueError(f"RouteEngineeringTarget.sink_id must be one of {sorted(ENGINEERING_SINK_IDS)}")


@dataclass(frozen=True, slots=True)
class RecoverEnvironmentTarget(_StrictRecord):
    SCHEMA: ClassVar[str] = "vfx-harness.stop-target.recover-environment/v1"
    DIGEST_FIELD: ClassVar[str] = "target_digest"
    environment: EnvironmentResultAssertion
    failed_check_ids: tuple[str, ...]
    recovery_adapter_id: str
    evidence: tuple[StopEvidenceRef, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.environment, EnvironmentResultAssertion):
            raise ValueError("RecoverEnvironmentTarget requires typed environment state")
        object.__setattr__(
            self,
            "failed_check_ids",
            require_text_tuple(self.failed_check_ids, "RecoverEnvironmentTarget.failed_check_ids"),
        )
        if self.recovery_adapter_id not in RECOVERY_ADAPTER_IDS:
            raise ValueError(
                f"RecoverEnvironmentTarget.recovery_adapter_id must be one of {sorted(RECOVERY_ADAPTER_IDS)}"
            )
        object.__setattr__(self, "evidence", _evidence_tuple(self.evidence, "RecoverEnvironmentTarget.evidence"))


@dataclass(frozen=True, slots=True)
class ResumeCheckpointedSessionTarget(_StrictRecord):
    SCHEMA: ClassVar[str] = "vfx-harness.stop-target.resume-checkpointed-session/v1"
    DIGEST_FIELD: ClassVar[str] = "target_digest"
    bundle: SelectedBundleAssertion
    view: SelectedViewAssertion
    unit_state: UnitStateAssertion
    resume_record: ResumeRecordAssertion
    budget_state: BudgetStateAssertion
    evidence: tuple[StopEvidenceRef, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.bundle, SelectedBundleAssertion) or not isinstance(self.view, SelectedViewAssertion):
            raise ValueError("resume target requires selected bundle and view state")
        if self.bundle.bundle_digest is None:
            raise ValueError("resume target bundle must be selected")
        if self.view.bundle_digest != self.bundle.bundle_digest:
            raise ValueError("resume target view must belong to its bundle")
        if not isinstance(self.unit_state, UnitStateAssertion) or not isinstance(
            self.resume_record, ResumeRecordAssertion
        ):
            raise ValueError("resume target requires exact unit and resume state")
        if (
            self.resume_record.layer_id != self.unit_state.layer_id
            or self.resume_record.unit_id != self.unit_state.unit_id
            or self.resume_record.unit_digest != self.unit_state.unit_digest
            or self.resume_record.checkpoint.sha256 != self.unit_state.checkpoint_digest
        ):
            raise ValueError("resume record does not match the exact unit checkpoint identity")
        if not isinstance(self.budget_state, BudgetStateAssertion) or self.budget_state.remaining_attempts < 1:
            raise ValueError("resume target requires remaining typed budget")
        object.__setattr__(self, "evidence", _evidence_tuple(self.evidence, "ResumeCheckpointedSessionTarget.evidence"))


@dataclass(frozen=True, slots=True)
class EscalateQuestionTarget(_StrictRecord):
    SCHEMA: ClassVar[str] = "vfx-harness.stop-target.escalate-question/v1"
    DIGEST_FIELD: ClassVar[str] = "target_digest"
    question_record: EvidenceRecordAssertion
    question_digest: str
    decision_authority_id: str
    decision_schema: str
    allowed_answer_ids: tuple[str, ...]
    evidence: tuple[StopEvidenceRef, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.question_record, EvidenceRecordAssertion)
            or self.question_record.record_kind != "question"
        ):
            raise ValueError("EscalateQuestionTarget.question_record must be a question assertion")
        require_digest(self.question_digest, "EscalateQuestionTarget.question_digest")
        require_id(self.decision_authority_id, "EscalateQuestionTarget.decision_authority_id")
        require_text(self.decision_schema, "EscalateQuestionTarget.decision_schema")
        object.__setattr__(
            self,
            "allowed_answer_ids",
            require_text_tuple(self.allowed_answer_ids, "EscalateQuestionTarget.allowed_answer_ids"),
        )
        object.__setattr__(self, "evidence", _evidence_tuple(self.evidence, "EscalateQuestionTarget.evidence"))


StopTransactionTarget: TypeAlias = (
    RetryExactUnitTarget
    | PublishValidatedAmendmentTarget
    | ApplyRevisionCheckedReplanTarget
    | RouteEngineeringTarget
    | RecoverEnvironmentTarget
    | ResumeCheckpointedSessionTarget
    | EscalateQuestionTarget
)
_TARGET_TYPES = {
    cls.SCHEMA: cls
    for cls in (
        RetryExactUnitTarget,
        PublishValidatedAmendmentTarget,
        ApplyRevisionCheckedReplanTarget,
        RouteEngineeringTarget,
        RecoverEnvironmentTarget,
        ResumeCheckpointedSessionTarget,
        EscalateQuestionTarget,
    )
}


def _parse_evidence_list(value: Any, where: str) -> tuple[StopEvidenceRef, ...]:
    return tuple(
        StopEvidenceRef.from_dict(item, f"{where}[{index}]") for index, item in enumerate(list_value(value, where))
    )


def _parse_assertion_list(value: Any, where: str) -> tuple[StopStateAssertion, ...]:
    return tuple(
        state_assertion_from_dict(item, f"{where}[{index}]") for index, item in enumerate(list_value(value, where))
    )


def target_from_dict(value: Any, where: str) -> StopTransactionTarget:
    if not isinstance(value, dict) or value.get("schema") not in _TARGET_TYPES:
        raise ValueError(f"{where}.schema must name a supported stop transaction target")
    cls = _TARGET_TYPES[value["schema"]]
    row = _row(cls, value, where)
    kwargs = {item.name: row[item.name] for item in fields(cls)}
    if cls is RetryExactUnitTarget:
        kwargs["unit_state"] = state_assertion_from_dict(row["unit_state"], f"{where}.unit_state")
        kwargs["budget_state"] = state_assertion_from_dict(row["budget_state"], f"{where}.budget_state")
        kwargs["evidence"] = _parse_evidence_list(row["evidence"], f"{where}.evidence")
    elif cls is PublishValidatedAmendmentTarget:
        kwargs["base_bundle"] = state_assertion_from_dict(row["base_bundle"], f"{where}.base_bundle")
        kwargs["base_view"] = (
            None if row["base_view"] is None else state_assertion_from_dict(row["base_view"], f"{where}.base_view")
        )
        kwargs["findings"] = _parse_assertion_list(row["findings"], f"{where}.findings")
    elif cls is ApplyRevisionCheckedReplanTarget:
        for name in ("target_bundle", "target_view", "before_unit_state", "falsification"):
            kwargs[name] = None if row[name] is None else state_assertion_from_dict(row[name], f"{where}.{name}")
    elif cls is RouteEngineeringTarget:
        kwargs["owner_scope_ids"] = text_tuple_from_list(row["owner_scope_ids"], f"{where}.owner_scope_ids")
        kwargs["defect_record"] = state_assertion_from_dict(row["defect_record"], f"{where}.defect_record")
        kwargs["evidence"] = _parse_evidence_list(row["evidence"], f"{where}.evidence")
    elif cls is RecoverEnvironmentTarget:
        kwargs["environment"] = state_assertion_from_dict(row["environment"], f"{where}.environment")
        kwargs["failed_check_ids"] = text_tuple_from_list(row["failed_check_ids"], f"{where}.failed_check_ids")
        kwargs["evidence"] = _parse_evidence_list(row["evidence"], f"{where}.evidence")
    elif cls is ResumeCheckpointedSessionTarget:
        for name in ("bundle", "view", "unit_state", "resume_record", "budget_state"):
            kwargs[name] = state_assertion_from_dict(row[name], f"{where}.{name}")
        kwargs["evidence"] = _parse_evidence_list(row["evidence"], f"{where}.evidence")
    else:
        kwargs["question_record"] = state_assertion_from_dict(row["question_record"], f"{where}.question_record")
        kwargs["allowed_answer_ids"] = text_tuple_from_list(row["allowed_answer_ids"], f"{where}.allowed_answer_ids")
        kwargs["evidence"] = _parse_evidence_list(row["evidence"], f"{where}.evidence")
    return _finish(cls(**kwargs), row, where)


@dataclass(frozen=True, slots=True)
class SelectedAuthorityAmendmentCommitted(_StrictRecord):
    SCHEMA: ClassVar[str] = "vfx-harness.stop-postcondition.selected-authority-amendment/v1"
    DIGEST_FIELD: ClassVar[str] = "postcondition_digest"
    scope: str
    base_bundle_digest: str
    base_view_digest: str | None
    layer_id: str | None
    finding_ids: tuple[str, ...]
    gate_policy_id: str

    def __post_init__(self) -> None:
        if self.scope not in AUTHORITY_SCOPES:
            raise ValueError(f"authority amendment scope must be one of {sorted(AUTHORITY_SCOPES)}")
        require_optional_digest(
            self.base_bundle_digest,
            "SelectedAuthorityAmendmentCommitted.base_bundle_digest",
        )
        require_optional_digest(self.base_view_digest, "SelectedAuthorityAmendmentCommitted.base_view_digest")
        if self.scope == "layer_view":
            require_id(self.layer_id, "SelectedAuthorityAmendmentCommitted.layer_id")
        elif self.layer_id is not None:
            raise ValueError("global authority postcondition cannot name a layer")
        object.__setattr__(
            self, "finding_ids", require_text_tuple(self.finding_ids, "SelectedAuthorityAmendmentCommitted.finding_ids")
        )
        require_text(
            self.gate_policy_id,
            "SelectedAuthorityAmendmentCommitted.gate_policy_id",
        )


@dataclass(frozen=True, slots=True)
class RevisionCheckedReplanCommitted(_StrictRecord):
    SCHEMA: ClassVar[str] = "vfx-harness.stop-postcondition.revision-checked-replan/v1"
    DIGEST_FIELD: ClassVar[str] = "postcondition_digest"
    layer_id: str
    base_bundle_digest: str
    target_bundle_digest: str
    target_view_digest: str | None
    target_plan_digest: str
    before_state_revision: int
    before_state_digest: str
    falsification_id: str
    expected_effects_digest: str

    def __post_init__(self) -> None:
        require_id(self.layer_id, "RevisionCheckedReplanCommitted.layer_id")
        for name in (
            "base_bundle_digest",
            "target_bundle_digest",
            "target_plan_digest",
            "before_state_digest",
            "expected_effects_digest",
        ):
            require_digest(getattr(self, name), f"RevisionCheckedReplanCommitted.{name}")
        require_optional_digest(self.target_view_digest, "RevisionCheckedReplanCommitted.target_view_digest")
        _positive_int(self.before_state_revision, "RevisionCheckedReplanCommitted.before_state_revision")
        require_id(self.falsification_id, "RevisionCheckedReplanCommitted.falsification_id")


@dataclass(frozen=True, slots=True)
class ExactUnitAttemptAdvanced(_StrictRecord):
    SCHEMA: ClassVar[str] = "vfx-harness.stop-postcondition.exact-unit-attempt-advanced/v1"
    DIGEST_FIELD: ClassVar[str] = "postcondition_digest"
    layer_id: str
    unit_id: str
    unit_digest: str
    unit_plan_digest: str
    before_state_revision: int
    before_state_digest: str
    before_candidate_digest: str | None
    before_checkpoint_digest: str | None

    def __post_init__(self) -> None:
        require_id(self.layer_id, "ExactUnitAttemptAdvanced.layer_id")
        require_id(self.unit_id, "ExactUnitAttemptAdvanced.unit_id")
        for name in ("unit_digest", "unit_plan_digest", "before_state_digest"):
            require_digest(getattr(self, name), f"ExactUnitAttemptAdvanced.{name}")
        _positive_int(self.before_state_revision, "ExactUnitAttemptAdvanced.before_state_revision")
        require_optional_digest(self.before_candidate_digest, "ExactUnitAttemptAdvanced.before_candidate_digest")
        require_optional_digest(self.before_checkpoint_digest, "ExactUnitAttemptAdvanced.before_checkpoint_digest")


@dataclass(frozen=True, slots=True)
class EnvironmentReverified(_StrictRecord):
    SCHEMA: ClassVar[str] = "vfx-harness.stop-postcondition.environment-reverified/v1"
    DIGEST_FIELD: ClassVar[str] = "postcondition_digest"
    probe_id: str
    probe_spec_digest: str
    before_result_digest: str
    failed_check_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        require_id(self.probe_id, "EnvironmentReverified.probe_id")
        require_digest(self.probe_spec_digest, "EnvironmentReverified.probe_spec_digest")
        require_digest(self.before_result_digest, "EnvironmentReverified.before_result_digest")
        object.__setattr__(
            self,
            "failed_check_ids",
            require_text_tuple(self.failed_check_ids, "EnvironmentReverified.failed_check_ids"),
        )


@dataclass(frozen=True, slots=True)
class CheckpointedSessionAdvanced(_StrictRecord):
    SCHEMA: ClassVar[str] = "vfx-harness.stop-postcondition.checkpointed-session-advanced/v1"
    DIGEST_FIELD: ClassVar[str] = "postcondition_digest"
    layer_id: str
    unit_id: str
    unit_digest: str
    resume_record_digest: str
    before_state_digest: str
    before_candidate_digest: str | None
    before_checkpoint_digest: str | None

    def __post_init__(self) -> None:
        require_id(self.layer_id, "CheckpointedSessionAdvanced.layer_id")
        require_id(self.unit_id, "CheckpointedSessionAdvanced.unit_id")
        for name in ("unit_digest", "resume_record_digest", "before_state_digest"):
            require_digest(getattr(self, name), f"CheckpointedSessionAdvanced.{name}")
        require_optional_digest(self.before_candidate_digest, "CheckpointedSessionAdvanced.before_candidate_digest")
        require_optional_digest(self.before_checkpoint_digest, "CheckpointedSessionAdvanced.before_checkpoint_digest")


@dataclass(frozen=True, slots=True)
class HumanDecisionCommitted(_StrictRecord):
    SCHEMA: ClassVar[str] = "vfx-harness.stop-postcondition.human-decision-committed/v1"
    DIGEST_FIELD: ClassVar[str] = "postcondition_digest"
    question_digest: str
    decision_authority_id: str
    decision_schema: str
    allowed_answer_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        require_digest(self.question_digest, "HumanDecisionCommitted.question_digest")
        require_id(self.decision_authority_id, "HumanDecisionCommitted.decision_authority_id")
        require_text(self.decision_schema, "HumanDecisionCommitted.decision_schema")
        object.__setattr__(
            self,
            "allowed_answer_ids",
            require_text_tuple(self.allowed_answer_ids, "HumanDecisionCommitted.allowed_answer_ids"),
        )


@dataclass(frozen=True, slots=True)
class EngineeringRouteCommitted(_StrictRecord):
    SCHEMA: ClassVar[str] = "vfx-harness.stop-postcondition.engineering-route-committed/v1"
    DIGEST_FIELD: ClassVar[str] = "postcondition_digest"
    defect_packet_digest: str
    sink_id: str
    owner_scope_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        require_digest(self.defect_packet_digest, "EngineeringRouteCommitted.defect_packet_digest")
        if self.sink_id not in ENGINEERING_SINK_IDS:
            raise ValueError(f"EngineeringRouteCommitted.sink_id must be one of {sorted(ENGINEERING_SINK_IDS)}")
        object.__setattr__(
            self,
            "owner_scope_ids",
            require_text_tuple(self.owner_scope_ids, "EngineeringRouteCommitted.owner_scope_ids"),
        )


StopPostcondition: TypeAlias = (
    SelectedAuthorityAmendmentCommitted
    | RevisionCheckedReplanCommitted
    | ExactUnitAttemptAdvanced
    | EnvironmentReverified
    | CheckpointedSessionAdvanced
    | HumanDecisionCommitted
    | EngineeringRouteCommitted
)
_POSTCONDITION_TYPES = {
    cls.SCHEMA: cls
    for cls in (
        SelectedAuthorityAmendmentCommitted,
        RevisionCheckedReplanCommitted,
        ExactUnitAttemptAdvanced,
        EnvironmentReverified,
        CheckpointedSessionAdvanced,
        HumanDecisionCommitted,
        EngineeringRouteCommitted,
    )
}


def postcondition_from_dict(value: Any, where: str) -> StopPostcondition:
    if not isinstance(value, dict) or value.get("schema") not in _POSTCONDITION_TYPES:
        raise ValueError(f"{where}.schema must name a supported stop postcondition")
    cls = _POSTCONDITION_TYPES[value["schema"]]
    row = _row(cls, value, where)
    kwargs = {item.name: row[item.name] for item in fields(cls)}
    for name in ("finding_ids", "failed_check_ids", "allowed_answer_ids", "owner_scope_ids"):
        if name in kwargs:
            kwargs[name] = text_tuple_from_list(row[name], f"{where}.{name}")
    return _finish(cls(**kwargs), row, where)


_TARGET_META = {
    RetryExactUnitTarget: ("retry_exact_unit", "automatic"),
    PublishValidatedAmendmentTarget: ("publish_validated_amendment", "automatic"),
    ApplyRevisionCheckedReplanTarget: ("apply_revision_checked_replan", "automatic"),
    RouteEngineeringTarget: ("route_engineering", "terminal_route"),
    RecoverEnvironmentTarget: ("recover_environment", "external_recovery"),
    ResumeCheckpointedSessionTarget: ("resume_checkpointed_session", "automatic"),
    EscalateQuestionTarget: ("escalate_question", "human_handoff"),
}
TRANSACTION_IDS = frozenset(value[0] for value in _TARGET_META.values())
_EVALUATOR_IDS = {
    SelectedAuthorityAmendmentCommitted: "selected_authority_amendment",
    RevisionCheckedReplanCommitted: "revision_checked_replan",
    ExactUnitAttemptAdvanced: "exact_unit_attempt",
    EnvironmentReverified: "environment_reverification",
    CheckpointedSessionAdvanced: "checkpointed_session",
    HumanDecisionCommitted: "human_decision",
    EngineeringRouteCommitted: "engineering_route",
}


def target_preconditions(target: StopTransactionTarget) -> tuple[StopStateAssertion, ...]:
    if isinstance(target, RetryExactUnitTarget):
        values = (target.unit_state, target.budget_state)
    elif isinstance(target, PublishValidatedAmendmentTarget):
        values = (target.base_bundle, *((target.base_view,) if target.base_view else ()), *target.findings)
    elif isinstance(target, ApplyRevisionCheckedReplanTarget):
        values = (
            target.target_bundle,
            *((target.target_view,) if target.target_view else ()),
            target.before_unit_state,
            target.falsification,
        )
    elif isinstance(target, RouteEngineeringTarget):
        values = (target.defect_record,)
    elif isinstance(target, RecoverEnvironmentTarget):
        values = (target.environment,)
    elif isinstance(target, ResumeCheckpointedSessionTarget):
        values = (target.bundle, target.view, target.unit_state, target.resume_record, target.budget_state)
    elif isinstance(target, EscalateQuestionTarget):
        values = (target.question_record,)
    else:
        raise ValueError("unsupported stop transaction target")
    return _assertions(tuple(values), "StopAction.preconditions")


def _validate_target_postcondition(target: StopTransactionTarget, postcondition: StopPostcondition) -> None:
    if isinstance(target, RetryExactUnitTarget) and isinstance(postcondition, ExactUnitAttemptAdvanced):
        state = target.unit_state
        expected = (
            state.layer_id,
            state.unit_id,
            state.unit_digest,
            state.unit_plan_digest,
            state.revision,
            state.state_digest,
            state.candidate_digest,
            state.checkpoint_digest,
        )
        found = (
            postcondition.layer_id,
            postcondition.unit_id,
            postcondition.unit_digest,
            postcondition.unit_plan_digest,
            postcondition.before_state_revision,
            postcondition.before_state_digest,
            postcondition.before_candidate_digest,
            postcondition.before_checkpoint_digest,
        )
    elif isinstance(target, PublishValidatedAmendmentTarget) and isinstance(
        postcondition, SelectedAuthorityAmendmentCommitted
    ):
        expected = (
            target.scope,
            target.base_bundle.bundle_digest,
            None if target.base_view is None else target.base_view.view_digest,
            target.layer_id,
            tuple(sorted(item.record_id for item in target.findings)),
        )
        found = (
            postcondition.scope,
            postcondition.base_bundle_digest,
            postcondition.base_view_digest,
            postcondition.layer_id,
            postcondition.finding_ids,
        )
    elif isinstance(target, ApplyRevisionCheckedReplanTarget) and isinstance(
        postcondition, RevisionCheckedReplanCommitted
    ):
        expected = (
            target.layer_id,
            target.base_bundle_digest,
            target.target_bundle.bundle_digest,
            None if target.target_view is None else target.target_view.view_digest,
            target.target_plan_digest,
            target.before_unit_state.revision,
            target.before_unit_state.state_digest,
            target.falsification.record_id,
            target.expected_effects_digest,
        )
        found = (
            postcondition.layer_id,
            postcondition.base_bundle_digest,
            postcondition.target_bundle_digest,
            postcondition.target_view_digest,
            postcondition.target_plan_digest,
            postcondition.before_state_revision,
            postcondition.before_state_digest,
            postcondition.falsification_id,
            postcondition.expected_effects_digest,
        )
    elif isinstance(target, RouteEngineeringTarget) and isinstance(postcondition, EngineeringRouteCommitted):
        expected = (target.defect_record.evidence.record_digest, target.sink_id, target.owner_scope_ids)
        found = (postcondition.defect_packet_digest, postcondition.sink_id, postcondition.owner_scope_ids)
    elif isinstance(target, RecoverEnvironmentTarget) and isinstance(postcondition, EnvironmentReverified):
        expected = (
            target.environment.probe_id,
            target.environment.probe_spec_digest,
            target.environment.result_digest,
            target.failed_check_ids,
        )
        found = (
            postcondition.probe_id,
            postcondition.probe_spec_digest,
            postcondition.before_result_digest,
            postcondition.failed_check_ids,
        )
    elif isinstance(target, ResumeCheckpointedSessionTarget) and isinstance(postcondition, CheckpointedSessionAdvanced):
        state = target.unit_state
        expected = (
            state.layer_id,
            state.unit_id,
            state.unit_digest,
            target.resume_record.digest,
            state.state_digest,
            state.candidate_digest,
            state.checkpoint_digest,
        )
        found = (
            postcondition.layer_id,
            postcondition.unit_id,
            postcondition.unit_digest,
            postcondition.resume_record_digest,
            postcondition.before_state_digest,
            postcondition.before_candidate_digest,
            postcondition.before_checkpoint_digest,
        )
    elif isinstance(target, EscalateQuestionTarget) and isinstance(postcondition, HumanDecisionCommitted):
        expected = (
            target.question_digest,
            target.decision_authority_id,
            target.decision_schema,
            target.allowed_answer_ids,
        )
        found = (
            postcondition.question_digest,
            postcondition.decision_authority_id,
            postcondition.decision_schema,
            postcondition.allowed_answer_ids,
        )
    else:
        raise ValueError("StopAction target and postcondition kinds do not match")
    if expected != found:
        raise ValueError("StopAction postcondition does not bind its exact transaction target")


@dataclass(frozen=True, slots=True)
class StopAction:
    SCHEMA: ClassVar[str] = "vfx-harness.stop-action/v1"

    target: StopTransactionTarget
    postcondition: StopPostcondition

    def __post_init__(self) -> None:
        if type(self.target) not in _TARGET_META:
            raise ValueError("StopAction.target must be a supported transaction target")
        if type(self.postcondition) not in _EVALUATOR_IDS:
            raise ValueError("StopAction.postcondition must be a supported typed postcondition")
        _validate_target_postcondition(self.target, self.postcondition)

    @property
    def transaction_id(self) -> str:
        return _TARGET_META[type(self.target)][0]

    @property
    def dispatch_mode(self) -> str:
        return _TARGET_META[type(self.target)][1]

    @property
    def preconditions(self) -> tuple[StopStateAssertion, ...]:
        return target_preconditions(self.target)

    @property
    def precondition_digest(self) -> str:
        return canonical_digest(
            {
                "schema": "vfx-harness.stop-preconditions/v1",
                "target_digest": self.target.digest,
                "assertions": [item.identity_dict() for item in self.preconditions],
            }
        )

    @property
    def evaluator_id(self) -> str:
        return _EVALUATOR_IDS[type(self.postcondition)]

    def _wire_payload(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "transaction_id": self.transaction_id,
            "dispatch_mode": self.dispatch_mode,
            "target": self.target.as_dict(),
            "preconditions": [item.as_dict() for item in self.preconditions],
            "precondition_digest": self.precondition_digest,
            "postcondition": self.postcondition.as_dict(),
            "receipt_schema": TRANSACTION_RECEIPT_SCHEMA,
        }

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "transaction_id": self.transaction_id,
            "dispatch_mode": self.dispatch_mode,
            "target": self.target.identity_dict(),
            "preconditions": [item.identity_dict() for item in self.preconditions],
            "precondition_digest": self.precondition_digest,
            "postcondition": self.postcondition.identity_dict(),
            "receipt_schema": TRANSACTION_RECEIPT_SCHEMA,
        }

    @property
    def digest(self) -> str:
        return canonical_digest(self._identity_payload())

    def as_dict(self) -> dict[str, Any]:
        return {**self._wire_payload(), "action_digest": self.digest}

    @classmethod
    def from_dict(cls, value: Any, where: str) -> StopAction:
        row = record(
            value,
            where,
            cls.SCHEMA,
            (
                "transaction_id",
                "dispatch_mode",
                "target",
                "preconditions",
                "precondition_digest",
                "postcondition",
                "receipt_schema",
                "action_digest",
            ),
        )
        candidate = cls(
            target_from_dict(row["target"], f"{where}.target"),
            postcondition_from_dict(row["postcondition"], f"{where}.postcondition"),
        )
        if row["transaction_id"] != candidate.transaction_id:
            raise ValueError(f"{where}.transaction_id does not match its target")
        if row["dispatch_mode"] != candidate.dispatch_mode:
            raise ValueError(f"{where}.dispatch_mode does not match its transaction")
        if row["receipt_schema"] != TRANSACTION_RECEIPT_SCHEMA:
            raise ValueError(f"{where}.receipt_schema must be {TRANSACTION_RECEIPT_SCHEMA!r}")
        parsed = _assertions(
            _parse_assertion_list(row["preconditions"], f"{where}.preconditions"), f"{where}.preconditions"
        )
        if parsed != candidate.preconditions:
            raise ValueError(f"{where}.preconditions do not match its target")
        require_canonical_digest(
            row["precondition_digest"], candidate.precondition_digest, where, "precondition_digest"
        )
        require_canonical_digest(row["action_digest"], candidate.digest, where, "action_digest")
        return candidate


def action_idempotency_key(
    action: StopAction,
    *,
    authoritative_before_digest: str,
    attempt_evidence_digest: str,
) -> str:
    if not isinstance(action, StopAction):
        raise ValueError("idempotency key requires a typed StopAction")
    require_digest(authoritative_before_digest, "authoritative_before_digest")
    require_digest(attempt_evidence_digest, "attempt_evidence_digest")
    return canonical_digest(
        {
            "schema": "vfx-harness.transaction-idempotency-key/v1",
            "receipt_schema": TRANSACTION_RECEIPT_SCHEMA,
            "action_digest": action.digest,
            "authoritative_before_digest": authoritative_before_digest,
            "attempt_evidence_digest": attempt_evidence_digest,
        }
    )


@dataclass(frozen=True, slots=True)
class PostconditionEvaluation(_StrictRecord):
    SCHEMA: ClassVar[str] = "vfx-harness.postcondition-evaluation/v1"
    DIGEST_FIELD: ClassVar[str] = "evaluation_digest"
    action_digest: str
    postcondition_digest: str
    idempotency_key: str
    evaluator_id: str
    authoritative_before_digest: str
    authoritative_after_digest: str
    observed_records: tuple[StopEvidenceRef, ...]
    result: str

    def __post_init__(self) -> None:
        for name in (
            "action_digest",
            "postcondition_digest",
            "idempotency_key",
            "authoritative_before_digest",
            "authoritative_after_digest",
        ):
            require_digest(getattr(self, name), f"PostconditionEvaluation.{name}")
        require_id(self.evaluator_id, "PostconditionEvaluation.evaluator_id")
        object.__setattr__(self, "observed_records", _evidence_tuple(
            self.observed_records, "PostconditionEvaluation.observed_records"
        ))
        receipts = tuple(item for item in self.observed_records if item.kind == "transaction_receipt")
        if len(receipts) != 1 or receipts[0].record_schema != TRANSACTION_RECEIPT_SCHEMA:
            raise ValueError("PostconditionEvaluation must cite exactly one transaction receipt "
                             f"using {TRANSACTION_RECEIPT_SCHEMA!r}")
        if self.result not in {"satisfied", "unsatisfied"}:
            raise ValueError("PostconditionEvaluation.result must be 'satisfied' or 'unsatisfied'")
        if self.result == "satisfied" and self.authoritative_after_digest == self.authoritative_before_digest:
            raise ValueError("a satisfied progress postcondition must change authoritative domain state")

    @property
    def satisfied(self) -> bool:
        return self.result == "satisfied"

    def assert_matches(
        self,
        action: StopAction,
        *,
        authoritative_before_digest: str,
        attempt_evidence_digest: str,
    ) -> None:
        expected_key = action_idempotency_key(
            action,
            authoritative_before_digest=authoritative_before_digest,
            attempt_evidence_digest=attempt_evidence_digest,
        )
        if (
            self.action_digest != action.digest
            or self.postcondition_digest != action.postcondition.digest
            or self.evaluator_id != action.evaluator_id
            or self.authoritative_before_digest != authoritative_before_digest
            or self.idempotency_key != expected_key
        ):
            raise ValueError("postcondition evaluation does not bind the exact action attempt")

    @classmethod
    def from_dict(cls, value: Any, where: str) -> PostconditionEvaluation:
        row = _row(cls, value, where)
        candidate = cls(
            action_digest=row["action_digest"],
            postcondition_digest=row["postcondition_digest"],
            idempotency_key=row["idempotency_key"],
            evaluator_id=row["evaluator_id"],
            authoritative_before_digest=row["authoritative_before_digest"],
            authoritative_after_digest=row["authoritative_after_digest"],
            observed_records=_parse_evidence_list(row["observed_records"], f"{where}.observed_records"),
            result=row["result"],
        )
        return _finish(candidate, row, where)
