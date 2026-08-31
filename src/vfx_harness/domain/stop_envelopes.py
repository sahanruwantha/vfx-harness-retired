"""Pure sealed stop envelopes and loop-safe classification results.

Every terminal envelope carries one exact typed transaction. Human-readable detail is
diagnostic only; authority, evidence, preconditions, and progress are closed records.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar, TypeAlias

from vfx_harness.domain.stop_envelope_primitives import (
    canonical_digest,
    list_value,
    record,
    require_canonical_digest,
    require_digest,
    require_id,
    require_optional_digest,
    require_optional_id,
    require_sequence,
    require_text,
    require_text_tuple,
    text_tuple_from_list,
)
from vfx_harness.domain.stop_transaction_state import StopEvidenceRef, _evidence_tuple
from vfx_harness.domain.stop_transactions import (
    TRANSACTION_IDS,
    ApplyRevisionCheckedReplanTarget,
    EscalateQuestionTarget,
    PostconditionEvaluation,
    PublishValidatedAmendmentTarget,
    RecoverEnvironmentTarget,
    ResumeCheckpointedSessionTarget,
    RetryExactUnitTarget,
    RouteEngineeringTarget,
    StopAction,
    StopTransactionTarget,
)

STOP_CLASSES = frozenset(
    {
        "local_implementation_miss",
        "authority_defect",
        "harness_defect",
        "infrastructure_failure",
        "human_decision_required",
    }
)
STOP_STAGES = frozenset(
    {
        "planning",
        "plan_gate",
        "materialization",
        "builder",
        "composition",
        "acceptance",
        "infrastructure",
    }
)
STOP_TRANSACTION_IDS = TRANSACTION_IDS

_ACTION_CLASS = {
    "retry_exact_unit": "local_implementation_miss",
    "publish_validated_amendment": "authority_defect",
    "apply_revision_checked_replan": "authority_defect",
    "route_engineering": "harness_defect",
    "recover_environment": "infrastructure_failure",
    "resume_checkpointed_session": "infrastructure_failure",
    "escalate_question": "human_decision_required",
}
_ACTION_STAGES = {
    "retry_exact_unit": frozenset({"builder", "composition", "acceptance"}),
    "publish_validated_amendment": frozenset({"plan_gate", "materialization", "builder", "composition", "acceptance"}),
    "apply_revision_checked_replan": frozenset({"materialization", "builder", "composition", "acceptance"}),
    "route_engineering": STOP_STAGES,
    "recover_environment": STOP_STAGES,
    "resume_checkpointed_session": frozenset({"builder"}),
    "escalate_question": STOP_STAGES,
}


@dataclass(frozen=True, slots=True)
class StopIdentity:
    """Exact authoritative identities that applied at an unaccepted boundary."""

    SCHEMA: ClassVar[str] = "vfx-harness.stop-identity/v1"

    run_id: str
    bundle_digest: str | None
    view_digest: str | None
    layer_id: str | None
    unit_id: str | None
    unit_plan_digest: str | None
    unit_digest: str | None
    candidate_digest: str | None
    checkpoint_digest: str | None
    settings_digest: str | None
    debt_state_digest: str | None

    def __post_init__(self) -> None:
        require_id(self.run_id, "StopIdentity.run_id")
        for name in (
            "bundle_digest",
            "view_digest",
            "unit_plan_digest",
            "unit_digest",
            "candidate_digest",
            "checkpoint_digest",
            "settings_digest",
            "debt_state_digest",
        ):
            require_optional_digest(getattr(self, name), f"StopIdentity.{name}")
        require_optional_id(self.layer_id, "StopIdentity.layer_id")
        require_optional_id(self.unit_id, "StopIdentity.unit_id")

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "run_id": self.run_id,
            "bundle_digest": self.bundle_digest,
            "view_digest": self.view_digest,
            "layer_id": self.layer_id,
            "unit_id": self.unit_id,
            "unit_plan_digest": self.unit_plan_digest,
            "unit_digest": self.unit_digest,
            "candidate_digest": self.candidate_digest,
            "checkpoint_digest": self.checkpoint_digest,
            "settings_digest": self.settings_digest,
            "debt_state_digest": self.debt_state_digest,
        }

    @property
    def digest(self) -> str:
        return canonical_digest(self._payload())

    def as_dict(self) -> dict[str, Any]:
        return {**self._payload(), "identity_digest": self.digest}

    @classmethod
    def from_dict(cls, value: Any, where: str) -> StopIdentity:
        names = (
            "run_id",
            "bundle_digest",
            "view_digest",
            "layer_id",
            "unit_id",
            "unit_plan_digest",
            "unit_digest",
            "candidate_digest",
            "checkpoint_digest",
            "settings_digest",
            "debt_state_digest",
            "identity_digest",
        )
        row = record(value, where, cls.SCHEMA, names)
        candidate = cls(**{name: row[name] for name in names if name != "identity_digest"})
        require_canonical_digest(row["identity_digest"], candidate.digest, where, "identity_digest")
        return candidate


@dataclass(frozen=True, slots=True)
class StopCause:
    """Stable causal facts, deliberately excluding run-local attempt identity."""

    SCHEMA: ClassVar[str] = "vfx-harness.stop-cause/v1"

    invariant_id: str
    finding_ids: tuple[str, ...]
    owner_scope_ids: tuple[str, ...]
    normalized_facts_digest: str

    def __post_init__(self) -> None:
        require_id(self.invariant_id, "StopCause.invariant_id")
        object.__setattr__(
            self,
            "finding_ids",
            require_text_tuple(self.finding_ids, "StopCause.finding_ids"),
        )
        object.__setattr__(
            self,
            "owner_scope_ids",
            require_text_tuple(self.owner_scope_ids, "StopCause.owner_scope_ids"),
        )
        require_digest(self.normalized_facts_digest, "StopCause.normalized_facts_digest")

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "invariant_id": self.invariant_id,
            "finding_ids": list(self.finding_ids),
            "owner_scope_ids": list(self.owner_scope_ids),
            "normalized_facts_digest": self.normalized_facts_digest,
        }

    def fingerprint_for(self, stop_class: str) -> str:
        if stop_class not in STOP_CLASSES:
            raise ValueError(f"stop_class must be one of {sorted(STOP_CLASSES)}")
        return canonical_digest(
            {
                "schema": "vfx-harness.stop-cause-fingerprint/v1",
                "stop_class": stop_class,
                "cause": self._payload(),
            }
        )

    def as_dict(self) -> dict[str, Any]:
        return {**self._payload(), "cause_digest": canonical_digest(self._payload())}

    @classmethod
    def from_dict(cls, value: Any, where: str) -> StopCause:
        row = record(
            value,
            where,
            cls.SCHEMA,
            (
                "invariant_id",
                "finding_ids",
                "owner_scope_ids",
                "normalized_facts_digest",
                "cause_digest",
            ),
        )
        candidate = cls(
            invariant_id=row["invariant_id"],
            finding_ids=text_tuple_from_list(row["finding_ids"], f"{where}.finding_ids"),
            owner_scope_ids=text_tuple_from_list(
                row["owner_scope_ids"],
                f"{where}.owner_scope_ids",
            ),
            normalized_facts_digest=row["normalized_facts_digest"],
        )
        require_canonical_digest(
            row["cause_digest"],
            canonical_digest(candidate._payload()),
            where,
            "cause_digest",
        )
        return candidate


def _actions(value: Any, where: str) -> tuple[StopAction, ...]:
    require_sequence(value, where)
    actions = tuple(value)
    if len(actions) != 1 or not isinstance(actions[0], StopAction):
        raise ValueError(f"{where} must contain exactly one typed StopAction")
    return actions


def _validate_identity_for_stage(stage: str, identity: StopIdentity) -> None:
    if identity.view_digest is not None and identity.bundle_digest is None:
        raise ValueError("a selected view identity requires its selected bundle identity")
    unit_fields = (identity.unit_id, identity.unit_digest, identity.unit_plan_digest)
    if any(item is not None for item in unit_fields) and not all(item is not None for item in unit_fields):
        raise ValueError("unit id, unit digest, and unit-plan digest must appear together")
    if identity.unit_id is not None and identity.layer_id is None:
        raise ValueError("a unit identity requires its owning layer identity")
    if identity.checkpoint_digest is not None and identity.unit_id is None:
        raise ValueError("a checkpoint identity requires its exact unit identity")
    if stage == "plan_gate" and identity.candidate_digest is None:
        raise ValueError("plan_gate stop requires the rejected candidate identity")
    if stage in {"builder", "composition", "acceptance"} and (
        identity.bundle_digest is None or identity.view_digest is None
    ):
        raise ValueError(f"{stage} stop requires selected bundle and view identities")
    if stage in {"materialization", "builder", "composition"} and identity.layer_id is None:
        raise ValueError(f"{stage} stop requires a layer identity")
    if stage == "materialization" and identity.bundle_digest is None:
        raise ValueError("materialization stop requires a selected bundle identity")
    if stage == "builder" and identity.unit_id is None:
        raise ValueError("builder stop requires exact unit, digest, and unit-plan identities")
    if stage == "acceptance" and (identity.candidate_digest is None or identity.settings_digest is None):
        raise ValueError("acceptance stop requires exact chain candidate and settings identities")


def _target_evidence(target: StopTransactionTarget) -> tuple[StopEvidenceRef, ...]:
    if isinstance(target, RetryExactUnitTarget):
        return target.evidence
    if isinstance(target, PublishValidatedAmendmentTarget):
        return tuple(item.evidence for item in target.findings)
    if isinstance(target, ApplyRevisionCheckedReplanTarget):
        return (target.falsification.evidence,)
    if isinstance(target, RouteEngineeringTarget):
        return (target.defect_record.evidence, *target.evidence)
    if isinstance(target, RecoverEnvironmentTarget):
        return target.evidence
    if isinstance(target, ResumeCheckpointedSessionTarget):
        return (
            target.resume_record.checkpoint,
            target.resume_record.journal,
            *target.evidence,
        )
    if isinstance(target, EscalateQuestionTarget):
        return (target.question_record.evidence, *target.evidence)
    raise ValueError("unsupported stop transaction target")


def _validate_action_identity(
    stage: str,
    identity: StopIdentity,
    action: StopAction,
    budget_key: str,
    authoritative_before_digest: str,
) -> None:
    if stage not in _ACTION_STAGES[action.transaction_id]:
        raise ValueError(f"transaction {action.transaction_id!r} is illegal at stop stage {stage!r}")
    target = action.target
    if isinstance(target, RetryExactUnitTarget):
        state = target.unit_state
        expected = (
            identity.layer_id,
            identity.unit_id,
            identity.unit_digest,
            identity.unit_plan_digest,
            identity.candidate_digest,
            identity.checkpoint_digest,
        )
        found = (
            state.layer_id,
            state.unit_id,
            state.unit_digest,
            state.unit_plan_digest,
            state.candidate_digest,
            state.checkpoint_digest,
        )
        if expected != found:
            raise ValueError("retry target does not match the exact stop identity")
        if target.budget_state.budget_key != budget_key:
            raise ValueError("retry target budget does not match StopEnvelope.budget_key")
    elif isinstance(target, PublishValidatedAmendmentTarget):
        base_bundle = target.base_authority.bundle
        base_view = target.base_authority.effective_view
        if (
            (None if base_bundle is None else base_bundle.digest)
            != identity.bundle_digest
            or (
                target.scope == "layer_view"
                and (None if base_view is None else base_view.digest)
                != identity.view_digest
            )
            or (target.layer_id is not None and target.layer_id != identity.layer_id)
            or target.base_authority.digest != authoritative_before_digest
        ):
            raise ValueError("amendment target does not match selected stop authority")
        if identity.candidate_digest is None:
            raise ValueError("validated amendment requires the exact rejected candidate")
    elif isinstance(target, ApplyRevisionCheckedReplanTarget):
        target_view = None if target.target_view is None else target.target_view.view_digest
        state = target.before_unit_state
        if (
            target.target_bundle.bundle_digest != identity.bundle_digest
            or target_view != identity.view_digest
            or target.layer_id != identity.layer_id
            or state.unit_id != identity.unit_id
            or state.unit_digest != identity.unit_digest
            or state.unit_plan_digest != identity.unit_plan_digest
        ):
            raise ValueError("revision-checked replan target does not match the stop identity")
    elif isinstance(target, ResumeCheckpointedSessionTarget):
        state = target.unit_state
        if (
            target.bundle.bundle_digest != identity.bundle_digest
            or target.view.view_digest != identity.view_digest
            or state.layer_id != identity.layer_id
            or state.unit_id != identity.unit_id
            or state.unit_digest != identity.unit_digest
            or state.unit_plan_digest != identity.unit_plan_digest
            or state.checkpoint_digest != identity.checkpoint_digest
        ):
            raise ValueError("resume target does not match the exact stop identity")
        if target.budget_state.budget_key != budget_key:
            raise ValueError("resume target budget does not match StopEnvelope.budget_key")


@dataclass(frozen=True, slots=True)
class StopEnvelope:
    """One run-scoped, unaccepted boundary with no inferred repair authority."""

    SCHEMA: ClassVar[str] = "vfx-harness.stop-envelope/v1"

    stage: str
    stop_class: str
    identity: StopIdentity
    cause: StopCause
    attempt_evidence_digest: str
    classification_evidence_digest: str
    artifact_state_digest: str
    authoritative_before_digest: str
    actions: tuple[StopAction, ...]
    evidence_refs: tuple[StopEvidenceRef, ...]
    budget_key: str
    expected: str
    found: str
    next_action: str
    cause_fingerprint: str = field(init=False)
    retryable: bool = field(init=False)

    def __post_init__(self) -> None:
        if self.stage not in STOP_STAGES:
            raise ValueError(f"StopEnvelope.stage must be one of {sorted(STOP_STAGES)}")
        if self.stop_class not in STOP_CLASSES:
            raise ValueError(f"StopEnvelope.stop_class must be one of {sorted(STOP_CLASSES)}")
        if not isinstance(self.identity, StopIdentity) or not isinstance(self.cause, StopCause):
            raise ValueError("StopEnvelope requires StopIdentity and StopCause")
        _validate_identity_for_stage(self.stage, self.identity)
        for name in (
            "attempt_evidence_digest",
            "classification_evidence_digest",
            "artifact_state_digest",
            "authoritative_before_digest",
        ):
            require_digest(getattr(self, name), f"StopEnvelope.{name}")
        actions = _actions(self.actions, "StopEnvelope.actions")
        object.__setattr__(self, "actions", actions)
        action = actions[0]
        expected_class = _ACTION_CLASS[action.transaction_id]
        if self.stop_class != expected_class:
            raise ValueError(f"transaction {action.transaction_id!r} is illegal for {self.stop_class!r}")
        require_id(self.budget_key, "StopEnvelope.budget_key")
        _validate_action_identity(
            self.stage,
            self.identity,
            action,
            self.budget_key,
            self.authoritative_before_digest,
        )
        evidence = _evidence_tuple(self.evidence_refs, "StopEnvelope.evidence_refs")
        object.__setattr__(self, "evidence_refs", evidence)
        if not any(item.record_digest is not None for item in evidence):
            raise ValueError("StopEnvelope must cite at least one typed evidence record")
        available = {item.digest for item in evidence}
        missing = sorted(item.digest for item in _target_evidence(action.target) if item.digest not in available)
        if missing:
            raise ValueError("StopEnvelope evidence_refs do not contain every transaction target reference")
        fingerprint = self.cause.fingerprint_for(self.stop_class)
        if isinstance(action.target, RouteEngineeringTarget) and (
            action.target.cause_fingerprint != fingerprint
            or action.target.attempt_evidence_digest != self.attempt_evidence_digest
            or action.target.owner_scope_ids != self.cause.owner_scope_ids
        ):
            raise ValueError("engineering route target does not bind the exact stop cause")
        for name in ("expected", "found", "next_action"):
            require_text(getattr(self, name), f"StopEnvelope.{name}")
        object.__setattr__(self, "cause_fingerprint", fingerprint)
        object.__setattr__(
            self,
            "retryable",
            action.transaction_id == "retry_exact_unit",
        )

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "stage": self.stage,
            "stop_class": self.stop_class,
            "retryable": self.retryable,
            "identity": self.identity.as_dict(),
            "cause": self.cause.as_dict(),
            "cause_fingerprint": self.cause_fingerprint,
            "attempt_evidence_digest": self.attempt_evidence_digest,
            "classification_evidence_digest": self.classification_evidence_digest,
            "artifact_state_digest": self.artifact_state_digest,
            "authoritative_before_digest": self.authoritative_before_digest,
            "actions": [self.actions[0].as_dict()],
            "evidence_refs": [item.as_dict() for item in self.evidence_refs],
            "budget_key": self.budget_key,
            "expected": self.expected,
            "found": self.found,
            "next_action": self.next_action,
        }

    @property
    def digest(self) -> str:
        return canonical_digest(self._payload())

    def as_dict(self) -> dict[str, Any]:
        return {**self._payload(), "envelope_digest": self.digest}

    @classmethod
    def from_dict(cls, value: Any, where: str) -> StopEnvelope:
        names = (
            "stage",
            "stop_class",
            "retryable",
            "identity",
            "cause",
            "cause_fingerprint",
            "attempt_evidence_digest",
            "classification_evidence_digest",
            "artifact_state_digest",
            "authoritative_before_digest",
            "actions",
            "evidence_refs",
            "budget_key",
            "expected",
            "found",
            "next_action",
            "envelope_digest",
        )
        row = record(value, where, cls.SCHEMA, names)
        actions = tuple(
            StopAction.from_dict(item, f"{where}.actions[{index}]")
            for index, item in enumerate(list_value(row["actions"], f"{where}.actions"))
        )
        evidence = tuple(
            StopEvidenceRef.from_dict(item, f"{where}.evidence_refs[{index}]")
            for index, item in enumerate(list_value(row["evidence_refs"], f"{where}.evidence_refs"))
        )
        candidate = cls(
            stage=row["stage"],
            stop_class=row["stop_class"],
            identity=StopIdentity.from_dict(row["identity"], f"{where}.identity"),
            cause=StopCause.from_dict(row["cause"], f"{where}.cause"),
            attempt_evidence_digest=row["attempt_evidence_digest"],
            classification_evidence_digest=row["classification_evidence_digest"],
            artifact_state_digest=row["artifact_state_digest"],
            authoritative_before_digest=row["authoritative_before_digest"],
            actions=actions,
            evidence_refs=evidence,
            budget_key=row["budget_key"],
            expected=row["expected"],
            found=row["found"],
            next_action=row["next_action"],
        )
        if row["retryable"] is not candidate.retryable:
            raise ValueError(f"{where}.retryable is stale")
        require_canonical_digest(
            row["cause_fingerprint"],
            candidate.cause_fingerprint,
            where,
            "cause_fingerprint",
        )
        require_canonical_digest(
            row["envelope_digest"],
            candidate.digest,
            where,
            "envelope_digest",
        )
        return candidate


@dataclass(frozen=True, slots=True)
class EvidenceNotDue:
    """Successful continuation: a debt remains pending until its typed provider is due."""

    SCHEMA: ClassVar[str] = "vfx-harness.evidence-not-due/v1"

    definition_digest: str
    debt_state_digest: str
    provider_activation_digest: str

    def __post_init__(self) -> None:
        for name in ("definition_digest", "debt_state_digest", "provider_activation_digest"):
            require_digest(getattr(self, name), f"EvidenceNotDue.{name}")

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "definition_digest": self.definition_digest,
            "debt_state_digest": self.debt_state_digest,
            "provider_activation_digest": self.provider_activation_digest,
        }

    @property
    def progress_digest(self) -> str:
        return canonical_digest(self._payload())

    def as_dict(self) -> dict[str, Any]:
        return {**self._payload(), "progress_digest": self.progress_digest}

    @classmethod
    def from_dict(cls, value: Any, where: str) -> EvidenceNotDue:
        row = record(
            value,
            where,
            cls.SCHEMA,
            (
                "definition_digest",
                "debt_state_digest",
                "provider_activation_digest",
                "progress_digest",
            ),
        )
        candidate = cls(
            row["definition_digest"],
            row["debt_state_digest"],
            row["provider_activation_digest"],
        )
        require_canonical_digest(
            row["progress_digest"],
            candidate.progress_digest,
            where,
            "progress_digest",
        )
        return candidate


@dataclass(frozen=True, slots=True)
class PriorDispatchAttempt:
    """One receipt-backed prior transaction and its typed postcondition evaluation."""

    SCHEMA: ClassVar[str] = "vfx-harness.prior-dispatch-attempt/v1"

    cause_fingerprint: str
    authoritative_before_digest: str
    attempt_evidence_digest: str
    action: StopAction
    evaluation: PostconditionEvaluation

    def __post_init__(self) -> None:
        require_digest(self.cause_fingerprint, "PriorDispatchAttempt.cause_fingerprint")
        require_digest(
            self.authoritative_before_digest,
            "PriorDispatchAttempt.authoritative_before_digest",
        )
        require_digest(
            self.attempt_evidence_digest,
            "PriorDispatchAttempt.attempt_evidence_digest",
        )
        if not isinstance(self.action, StopAction):
            raise ValueError("PriorDispatchAttempt.action must be a StopAction")
        if not isinstance(self.evaluation, PostconditionEvaluation):
            raise ValueError("PriorDispatchAttempt.evaluation must be a PostconditionEvaluation")
        self.evaluation.assert_matches(
            self.action,
            authoritative_before_digest=self.authoritative_before_digest,
            attempt_evidence_digest=self.attempt_evidence_digest,
        )

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "cause_fingerprint": self.cause_fingerprint,
            "authoritative_before_digest": self.authoritative_before_digest,
            "attempt_evidence_digest": self.attempt_evidence_digest,
            "action": self.action.as_dict(),
            "evaluation": self.evaluation.as_dict(),
        }

    @property
    def digest(self) -> str:
        return canonical_digest(self._payload())

    def as_dict(self) -> dict[str, Any]:
        return {**self._payload(), "attempt_digest": self.digest}

    @classmethod
    def from_dict(cls, value: Any, where: str) -> PriorDispatchAttempt:
        row = record(
            value,
            where,
            cls.SCHEMA,
            (
                "cause_fingerprint",
                "authoritative_before_digest",
                "attempt_evidence_digest",
                "action",
                "evaluation",
                "attempt_digest",
            ),
        )
        candidate = cls(
            cause_fingerprint=row["cause_fingerprint"],
            authoritative_before_digest=row["authoritative_before_digest"],
            attempt_evidence_digest=row["attempt_evidence_digest"],
            action=StopAction.from_dict(row["action"], f"{where}.action"),
            evaluation=PostconditionEvaluation.from_dict(
                row["evaluation"],
                f"{where}.evaluation",
            ),
        )
        require_canonical_digest(
            row["attempt_digest"],
            candidate.digest,
            where,
            "attempt_digest",
        )
        return candidate


StopClassification: TypeAlias = StopEnvelope | EvidenceNotDue


REPEATED_DISPATCH_DEFECT_SCHEMA = "vfx-harness.repeated-dispatch-defect/v1"


def repeated_dispatch_defect_document(
    candidate: StopEnvelope,
    prior: PriorDispatchAttempt,
) -> dict[str, Any]:
    """Return the exact content a caller must publish before routing a loop.

    The pure classifier cannot manufacture evidence.  A future controller must
    publish this document, seal its reference, and pass that typed assertion back
    to :func:`classify_stop`.
    """

    if not isinstance(candidate, StopEnvelope) or not isinstance(prior, PriorDispatchAttempt):
        raise ValueError("repeated-dispatch evidence requires a typed stop and prior attempt")
    if (
        prior.cause_fingerprint != candidate.cause_fingerprint
        or prior.authoritative_before_digest != candidate.authoritative_before_digest
        or prior.evaluation.satisfied
    ):
        raise ValueError("prior attempt is not an unsatisfied repetition of this stop")
    stable_identity = {
        "schema": "vfx-harness.dispatch-stop-identity/v1",
        "stage": candidate.stage,
        "stop_class": candidate.stop_class,
        "identity": {
            key: value
            for key, value in candidate.identity._payload().items()
            if key not in {"schema", "run_id"}
        },
        "cause_fingerprint": candidate.cause_fingerprint,
        "attempt_evidence_digest": candidate.attempt_evidence_digest,
        "classification_evidence_digest": candidate.classification_evidence_digest,
        "artifact_state_digest": candidate.artifact_state_digest,
        "authoritative_before_digest": candidate.authoritative_before_digest,
        "action_digest": candidate.actions[0].digest,
        "evidence_ref_digests": sorted(item.digest for item in candidate.evidence_refs),
        "budget_key": candidate.budget_key,
    }
    core = {
        "schema": REPEATED_DISPATCH_DEFECT_SCHEMA,
        "cause_fingerprint": candidate.cause_fingerprint,
        "authoritative_before_digest": candidate.authoritative_before_digest,
        "current_stop_digest": canonical_digest(stable_identity),
        "current_attempt_evidence_digest": candidate.attempt_evidence_digest,
        "current_action_digest": candidate.actions[0].digest,
        "prior_attempt_digest": prior.digest,
        "prior_evaluation_digest": prior.evaluation.digest,
        "receipt_evidence_digests": sorted(
            item.digest
            for item in prior.evaluation.observed_records
            if item.kind == "transaction_receipt"
        ),
    }
    defect_id = "controller-loop-" + canonical_digest(core)[:20]
    return {**core, "defect_id": defect_id}


def classify_stop(
    candidate: Any,
    prior_attempts: Sequence[PriorDispatchAttempt] = (),
) -> StopClassification:
    """Return typed continuation or a receipt-backed loop-safe terminal envelope."""

    require_sequence(prior_attempts, "prior_attempts")
    attempts = tuple(prior_attempts)
    if any(not isinstance(attempt, PriorDispatchAttempt) for attempt in attempts):
        raise ValueError("prior_attempts must contain PriorDispatchAttempt values")
    if isinstance(candidate, EvidenceNotDue):
        return candidate
    if not isinstance(candidate, StopEnvelope):
        raise ValueError("stop classification requires a typed StopEnvelope or EvidenceNotDue")
    repeated = any(
        attempt.cause_fingerprint == candidate.cause_fingerprint
        and attempt.authoritative_before_digest == candidate.authoritative_before_digest
        and not attempt.evaluation.satisfied
        for attempt in attempts
    )
    if repeated:
        raise ValueError(
            "unchanged repeated stop detected; routing remains blocked until a verified "
            "transaction-receipt and repeated-dispatch-defect consumer exists"
        )
    return candidate
