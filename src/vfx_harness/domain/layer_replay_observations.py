"""Closed pre-judgment observations for one actual layer replay group."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import vfx_harness.domain.layer_replay_observation_values as replay_values
from vfx_harness.domain import evidence_authority
from vfx_harness.domain.judgment_debt_replay_receipts import ReplayPrefixReceipt
from vfx_harness.domain.layer_finalization_claims import (
    LayerFinalizationClaim,
    _relative_path,
    _text,
)
from vfx_harness.domain.layer_finalization_values import semantic_digest
from vfx_harness.domain.layer_replay_group_plans import (
    LAYER_REPLAY_CLAIM_AUTHORITIES as LAYER_REPLAY_CLAIM_AUTHORITIES,
)
from vfx_harness.domain.layer_replay_group_plans import (
    LAYER_REPLAY_EVIDENCE_KINDS as LAYER_REPLAY_EVIDENCE_KINDS,
)
from vfx_harness.domain.layer_replay_group_plans import (
    LayerReplayClaimRequirement as LayerReplayClaimRequirement,
)
from vfx_harness.domain.layer_replay_group_plans import (
    LayerReplayEvaluationGroupPlan as LayerReplayEvaluationGroupPlan,
)
from vfx_harness.domain.stop_envelope_primitives import canonical_digest, require_digest

if TYPE_CHECKING:
    from vfx_harness.domain.unit_evaluation_receipts import ReplayInputBinding

LAYER_REPLAY_OBSERVATION_SCHEMA = "vfx-harness.layer-replay-observation/v1"
_POINT_FIELDS = frozenset(
    {
        "frame",
        "ref",
        "ref_sha256",
        "evidence",
        "evidence_digest",
        "missing_evidence_ids",
        "failed_evidence_ids",
        "deterministic_status",
        "render",
        "render_sha256",
        "render_capture",
    }
)
LAYER_REPLAY_DETERMINISTIC_STATUSES = frozenset({"passed", "failed", "missing"})



@dataclass(frozen=True, slots=True)
class LayerReplayPointObservation:
    """Actual deterministic and render evidence captured before a critic call."""

    frame: int
    ref: str
    ref_sha256: str
    evidence: tuple[dict[str, Any], ...]
    evidence_digest: str
    missing_evidence_ids: tuple[str, ...]
    failed_evidence_ids: tuple[str, ...]
    deterministic_status: str
    render: str | None
    render_sha256: str | None
    render_capture: dict[str, Any] | None

    @classmethod
    def mint(
        cls,
        *,
        plan: LayerReplayEvaluationGroupPlan,
        frame: int,
        ref: object,
        ref_sha256: object,
        evidence: object,
        render: object = None,
        render_sha256: object = None,
        render_capture: object = None,
    ) -> LayerReplayPointObservation:
        if not isinstance(plan, LayerReplayEvaluationGroupPlan):
            raise ValueError("layer replay point requires a typed group plan")
        if not isinstance(frame, int) or isinstance(frame, bool) or frame < 1:
            raise ValueError("layer replay point frame must be a positive integer")
        point = (frame, str(ref))
        if point not in plan.judge_points:
            raise ValueError("layer replay point is not declared by its group plan")
        rows = replay_values.evidence_rows(evidence, "layer replay point evidence")
        by_id = {str(row["id"]): row for row in rows}
        required_ids = plan.executable_evidence_ids(frame)
        missing = tuple(sorted(set(required_ids) - set(by_id)))
        # The plan's claims already bound these ids, so the question is whether each was
        # produced as a real reading -- not whether it could veto unbound. Demanding
        # autonomy of a bound id excluded every builder-paid image row, which is the only
        # mechanism that can discharge a required image contract (HIR-0205, HIR-0210).
        for evidence_id in required_ids:
            row = by_id.get(evidence_id)
            if row is not None and not evidence_authority.is_recorded_evidence(row):
                raise ValueError(
                    "required executable evidence is not a typed measurement: "
                    f"{evidence_id} carries source "
                    f"{str(row.get('source') or '(none)')!r}"
                )
        failed = tuple(
            sorted(
                str(row["id"])
                for row in rows
                # A failing bound row is a failure whether or not it may veto unbound;
                # filtering on autonomy let a failed image contract read as passed.
                if evidence_authority.is_recorded_evidence(row) and row["pass"] is False
            )
        )
        status = "missing" if missing else "failed" if failed else "passed"
        evidence_digest = canonical_digest({"evidence": list(rows)})
        ref_path = _relative_path(ref, "layer replay point ref")
        ref_digest = require_digest(ref_sha256, "layer replay point ref_sha256")
        if plan.evidence_kind == "executable_only":
            if (
                render is not None
                or render_sha256 is not None
                or render_capture is not None
            ):
                raise ValueError("executable-only replay point forbids render evidence")
            render_path = None
            render_digest = None
            capture = None
        else:
            render_path = _relative_path(render, "layer replay point render")
            render_digest = require_digest(
                render_sha256,
                "layer replay point render_sha256",
            )
            capture = replay_values.render_capture(
                render_capture,
                "layer replay point render_capture",
                frame=frame,
                mode=str(plan.render_mode),
                scale=float(plan.render_scale),
            )
            if capture["png_sha256"] != render_digest:
                raise ValueError(
                    "layer replay point render SHA does not match its capture receipt"
                )
        return cls(
            frame=frame,
            ref=ref_path,
            ref_sha256=ref_digest,
            evidence=rows,
            evidence_digest=evidence_digest,
            missing_evidence_ids=missing,
            failed_evidence_ids=failed,
            deterministic_status=status,
            render=render_path,
            render_sha256=render_digest,
            render_capture=capture,
        )

    @classmethod
    def from_dict(
        cls,
        value: object,
        where: str,
        *,
        plan: LayerReplayEvaluationGroupPlan,
    ) -> LayerReplayPointObservation:
        if not isinstance(value, Mapping) or set(value) != _POINT_FIELDS:
            raise ValueError(f"{where} has unsupported layer replay point shape")
        candidate = cls.mint(
            plan=plan,
            frame=value["frame"],
            ref=value["ref"],
            ref_sha256=value["ref_sha256"],
            evidence=value["evidence"],
            render=value["render"],
            render_sha256=value["render_sha256"],
            render_capture=value["render_capture"],
        )
        expected = {
            "evidence_digest": candidate.evidence_digest,
            "missing_evidence_ids": list(candidate.missing_evidence_ids),
            "failed_evidence_ids": list(candidate.failed_evidence_ids),
            "deterministic_status": candidate.deterministic_status,
        }
        if any(value[key] != observed for key, observed in expected.items()):
            raise ValueError(f"{where} deterministic evidence projection is stale")
        return candidate

    def as_dict(self) -> dict[str, Any]:
        return {
            "frame": self.frame,
            "ref": self.ref,
            "ref_sha256": self.ref_sha256,
            "evidence": list(self.evidence),
            "evidence_digest": self.evidence_digest,
            "missing_evidence_ids": list(self.missing_evidence_ids),
            "failed_evidence_ids": list(self.failed_evidence_ids),
            "deterministic_status": self.deterministic_status,
            "render": self.render,
            "render_sha256": self.render_sha256,
            "render_capture": self.render_capture,
        }


@dataclass(frozen=True, slots=True)
class LayerReplayObservation:
    """Strict replay prefix and actual inputs for one pre-judgment group."""

    replay_prefix: ReplayPrefixReceipt
    plan: LayerReplayEvaluationGroupPlan
    points: tuple[LayerReplayPointObservation, ...]
    auxiliary_captures: tuple[dict[str, Any], ...] = ()
    execution_status: str = "passed"
    execution_failure: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.replay_prefix, ReplayPrefixReceipt):
            raise ValueError("layer replay observation requires ReplayPrefixReceipt")
        if not isinstance(self.plan, LayerReplayEvaluationGroupPlan):
            raise ValueError("layer replay observation requires a typed group plan")
        if not isinstance(self.points, tuple) or any(
            not isinstance(row, LayerReplayPointObservation) for row in self.points
        ):
            raise ValueError("layer replay observation points must be typed")
        if self.execution_status not in {"passed", "failed"}:
            raise ValueError("layer replay execution_status must be passed or failed")
        if self.execution_status == "passed":
            if self.execution_failure is not None:
                raise ValueError("passed layer replay forbids an execution failure")
            if tuple((row.frame, row.ref) for row in self.points) != self.plan.judge_points:
                raise ValueError(
                    "layer replay observation points must exactly match planned judge order"
                )
        else:
            if self.points or self.auxiliary_captures:
                raise ValueError(
                    "failed layer replay forbids unexecuted point and auxiliary evidence"
                )
            if self.execution_failure is None:
                raise ValueError("failed layer replay requires typed execution failure")
            object.__setattr__(
                self,
                "execution_failure",
                replay_values.execution_failure(
                    self.execution_failure,
                    "layer replay observation execution_failure",
                ),
            )
        for index, point in enumerate(self.points):
            rebuilt = LayerReplayPointObservation.mint(
                plan=self.plan,
                frame=point.frame,
                ref=point.ref,
                ref_sha256=point.ref_sha256,
                evidence=point.evidence,
                render=point.render,
                render_sha256=point.render_sha256,
                render_capture=point.render_capture,
            )
            if rebuilt != point:
                raise ValueError(
                    "layer replay observation point contains non-derived fields: "
                    f"index={index}"
                )
        object.__setattr__(
            self,
            "auxiliary_captures",
            replay_values.auxiliary_captures(
                self.auxiliary_captures,
                "layer replay observation auxiliary_captures",
            ),
        )

    @property
    def group_index(self) -> int:
        return self.plan.group_index

    @classmethod
    def failed(
        cls,
        *,
        replay_prefix: ReplayPrefixReceipt,
        plan: LayerReplayEvaluationGroupPlan,
        stage: object,
        message: object,
    ) -> LayerReplayObservation:
        stage_text = _text(stage, "layer replay execution failure stage")
        message_text = _text(message, "layer replay execution failure message")
        failure = {
            "stage": stage_text,
            "message": message_text,
            "failure_digest": canonical_digest(
                {"stage": stage_text, "message": message_text}
            ),
        }
        return cls(
            replay_prefix=replay_prefix,
            plan=plan,
            points=(),
            execution_status="failed",
            execution_failure=failure,
        )

    @property
    def digest(self) -> str:
        return semantic_digest(
            {
                "schema": LAYER_REPLAY_OBSERVATION_SCHEMA,
                "replay_prefix": self.replay_prefix.as_dict(),
                "plan": self.plan.as_dict(),
                "points": [row.as_dict() for row in self.points],
                "auxiliary_captures": list(self.auxiliary_captures),
                "execution_status": self.execution_status,
                "execution_failure": self.execution_failure,
            }
        )

    def assert_matches_claim(
        self,
        claim: LayerFinalizationClaim,
        replay_inputs: tuple[ReplayInputBinding, ...],
    ) -> None:
        if not isinstance(claim, LayerFinalizationClaim):
            raise ValueError("layer replay observation requires a finalization claim")
        layers = self.replay_prefix.layers
        payer = layers[-1]
        if (
            payer.layer_id,
            payer.payer_claim_id,
            payer.layer_generation_digest,
            payer.script_path,
            payer.script_sha256,
        ) != (
            claim.layer_id,
            claim.claim_id,
            claim.plan_hash,
            claim.layer_script_path,
            claim.layer_script_sha256,
        ):
            raise ValueError("layer replay observation payer does not match its claim")
        predecessor_rows = tuple(
            (
                row.layer_id,
                row.finalization_receipt_digest,
                row.script_path,
                row.script_sha256,
            )
            for row in claim.predecessor_inputs
        )
        observed_predecessors = tuple(
            (
                row.layer_id,
                row.finalization_receipt_digest,
                row.script_path,
                row.script_sha256,
            )
            for row in layers[:-1]
        )
        if observed_predecessors != predecessor_rows:
            raise ValueError(
                "layer replay observation predecessor prefix does not match its claim"
            )
        observed_inputs = tuple(
            (row.script_path, row.script_sha256) for row in replay_inputs
        )
        prefix_inputs = tuple((row.script_path, row.script_sha256) for row in layers)
        if observed_inputs != prefix_inputs:
            raise ValueError(
                "layer replay observation prefix does not match executed replay inputs"
            )
        observed_units = tuple(
            (
                row.unit_id,
                row.unit_digest,
                row.completion_receipt_digest,
                row.script_path,
                row.script_sha256,
            )
            for row in payer.units
        )
        expected_units = tuple(
            (
                row.unit_id,
                row.unit_digest,
                row.completion_receipt_digest,
                row.script_path,
                row.script_sha256,
            )
            for row in claim.unit_inputs
        )
        if observed_units != expected_units:
            raise ValueError("layer replay observation units do not match its claim")

    @classmethod
    def from_dict(cls, value: object, where: str) -> LayerReplayObservation:
        expected = {
            "schema",
            "replay_prefix",
            "plan",
            "points",
            "auxiliary_captures",
            "execution_status",
            "execution_failure",
            "observation_digest",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError(f"{where} has unsupported layer replay observation shape")
        if value["schema"] != LAYER_REPLAY_OBSERVATION_SCHEMA:
            raise ValueError(
                f"{where}.schema must be {LAYER_REPLAY_OBSERVATION_SCHEMA!r}"
            )
        raw_points = value["points"]
        if not isinstance(raw_points, list):
            raise ValueError(f"{where}.points must be a list")
        plan = LayerReplayEvaluationGroupPlan.from_dict(
            value["plan"],
            f"{where}.plan",
        )
        candidate = cls(
            replay_prefix=ReplayPrefixReceipt.from_dict(
                value["replay_prefix"],
                f"{where}.replay_prefix",
            ),
            plan=plan,
            points=tuple(
                LayerReplayPointObservation.from_dict(
                    row,
                    f"{where}.points[{index}]",
                    plan=plan,
                )
                for index, row in enumerate(raw_points)
            ),
            auxiliary_captures=replay_values.auxiliary_captures(
                value["auxiliary_captures"],
                f"{where}.auxiliary_captures",
            ),
            execution_status=value["execution_status"],
            execution_failure=value["execution_failure"],
        )
        if require_digest(
            value["observation_digest"],
            f"{where}.observation_digest",
        ) != candidate.digest:
            raise ValueError(f"{where}.observation_digest does not match its payload")
        return candidate

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": LAYER_REPLAY_OBSERVATION_SCHEMA,
            "replay_prefix": self.replay_prefix.as_dict(),
            "plan": self.plan.as_dict(),
            "points": [row.as_dict() for row in self.points],
            "auxiliary_captures": list(self.auxiliary_captures),
            "execution_status": self.execution_status,
            "execution_failure": self.execution_failure,
            "observation_digest": self.digest,
        }


def canonical_layer_replay_observation_bytes(
    observation: LayerReplayObservation,
) -> bytes:
    if not isinstance(observation, LayerReplayObservation):
        raise ValueError("observation must be a typed layer replay observation")
    return (
        json.dumps(
            observation.as_dict(),
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


__all__ = [
    "LAYER_REPLAY_CLAIM_AUTHORITIES",
    "LAYER_REPLAY_DETERMINISTIC_STATUSES",
    "LAYER_REPLAY_EVIDENCE_KINDS",
    "LAYER_REPLAY_OBSERVATION_SCHEMA",
    "LayerReplayClaimRequirement",
    "LayerReplayEvaluationGroupPlan",
    "LayerReplayObservation",
    "LayerReplayPointObservation",
    "canonical_layer_replay_observation_bytes",
]
