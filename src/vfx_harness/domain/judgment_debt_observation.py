"""Pure, sealed identities for qualitative-debt observation attempts.

An observation request is deliberately pre-renderable: it seals the authority,
replay, environment, and comparison inputs before a candidate image exists.  That
makes it the durable exactly-once suppression key for both successful payments and
typed payment-attempt failures.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

from vfx_harness.domain.judgment_debt_models import (
    OBSERVATION_MEDIA,
    JudgmentDebtActivation,
    JudgmentDebtDefinition,
    JudgmentPoint,
    _canonical_digest,
    _record,
    _require_canonical_digest,
    _require_digest,
)

JUDGMENT_OBSERVATION_RENDER_MODES = frozenset({"solid", "eevee"})
JUDGMENT_PAYMENT_ATTEMPT_FAILURE_REASONS = frozenset({"no_optical_signal"})
NO_REFERENCE_MARKER = "no-reference"


def _require_render_scale(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{where} must be a number in (0, 1]")
    scale = float(value)
    if not math.isfinite(scale) or not 0.0 < scale <= 1.0:
        raise ValueError(f"{where} must be a number in (0, 1]")
    return scale


@dataclass(frozen=True, slots=True)
class JudgmentObservationRequest:
    """One sealed, pre-render qualitative observation request.

    The request is the suppression identity, not a candidate-inclusive result.  A
    changed replay, authority generation, environment, reference, or judge config
    creates a new digest and therefore a new legal payment attempt.
    """

    SCHEMA: ClassVar[str] = "vfx-harness.judgment-observation-request/v1"

    definition_digest: str
    activation_digest: str
    payment_generation_digest: str
    bundle_digest: str
    owner_view_digest: str
    payer_view_digest: str
    replay_receipt_digest: str
    parent_chain_digest: str
    judge_point: JudgmentPoint
    observation_medium: str
    render_mode: str
    render_scale: float
    reference_digest: str | None
    reference_marker: str | None
    observation_environment_digest: str
    external_asset_provenance_digest: str
    comparison_config_digest: str
    judge_config_digest: str

    def __post_init__(self) -> None:
        for name in (
            "definition_digest",
            "activation_digest",
            "payment_generation_digest",
            "bundle_digest",
            "owner_view_digest",
            "payer_view_digest",
            "replay_receipt_digest",
            "parent_chain_digest",
            "observation_environment_digest",
            "external_asset_provenance_digest",
            "comparison_config_digest",
            "judge_config_digest",
        ):
            _require_digest(getattr(self, name), f"JudgmentObservationRequest.{name}")
        if not isinstance(self.judge_point, JudgmentPoint):
            raise ValueError("JudgmentObservationRequest.judge_point must be a JudgmentPoint")
        if self.observation_medium not in OBSERVATION_MEDIA:
            raise ValueError("JudgmentObservationRequest.observation_medium must be workbench_solid or eevee")
        if self.render_mode not in JUDGMENT_OBSERVATION_RENDER_MODES:
            raise ValueError("JudgmentObservationRequest.render_mode must be solid or eevee")
        expected_mode = "solid" if self.observation_medium == "workbench_solid" else "eevee"
        if self.render_mode != expected_mode:
            raise ValueError(
                "JudgmentObservationRequest.render_mode must match the observation medium "
                f"({self.observation_medium!r} requires {expected_mode!r})"
            )
        object.__setattr__(
            self,
            "render_scale",
            _require_render_scale(self.render_scale, "JudgmentObservationRequest.render_scale"),
        )
        if self.reference_digest is None:
            if self.reference_marker != NO_REFERENCE_MARKER:
                raise ValueError(
                    "JudgmentObservationRequest without reference_digest requires "
                    f"reference_marker={NO_REFERENCE_MARKER!r}"
                )
        else:
            _require_digest(self.reference_digest, "JudgmentObservationRequest.reference_digest")
            if self.reference_marker is not None:
                raise ValueError("JudgmentObservationRequest reference_digest forbids reference_marker")

    def assert_matches(
        self,
        definition: JudgmentDebtDefinition,
        activation: JudgmentDebtActivation,
    ) -> None:
        """Fail closed unless this request is for the exact current debt activation."""
        if not isinstance(definition, JudgmentDebtDefinition):
            raise ValueError("definition must be a JudgmentDebtDefinition")
        if not isinstance(activation, JudgmentDebtActivation):
            raise ValueError("activation must be a JudgmentDebtActivation")
        activation.assert_matches(definition)
        if self.definition_digest != definition.digest:
            raise ValueError("JudgmentObservationRequest.definition_digest is stale for the definition")
        if self.activation_digest != activation.digest:
            raise ValueError("JudgmentObservationRequest.activation_digest is stale for the activation")
        if self.bundle_digest != definition.seed.bundle_digest:
            raise ValueError("JudgmentObservationRequest.bundle_digest is stale for the definition")
        if self.judge_point not in definition.seed.judge_points:
            raise ValueError("JudgmentObservationRequest.judge_point is not declared by the debt definition")
        if self.observation_medium != definition.seed.observation_medium:
            raise ValueError("JudgmentObservationRequest.observation_medium is stale for the debt definition")

    def _payload(self) -> dict[str, Any]:
        reference: dict[str, str] = (
            {"digest": self.reference_digest}
            if self.reference_digest is not None
            else {"marker": NO_REFERENCE_MARKER}
        )
        return {
            "schema": self.SCHEMA,
            "definition_digest": self.definition_digest,
            "activation_digest": self.activation_digest,
            "payment_generation_digest": self.payment_generation_digest,
            "bundle_digest": self.bundle_digest,
            "owner_view_digest": self.owner_view_digest,
            "payer_view_digest": self.payer_view_digest,
            "replay_receipt_digest": self.replay_receipt_digest,
            "parent_chain_digest": self.parent_chain_digest,
            "judge_point": self.judge_point.as_dict(),
            "observation_medium": self.observation_medium,
            "render_mode": self.render_mode,
            "render_scale": self.render_scale,
            "reference": reference,
            "observation_environment_digest": self.observation_environment_digest,
            "external_asset_provenance_digest": self.external_asset_provenance_digest,
            "comparison_config_digest": self.comparison_config_digest,
            "judge_config_digest": self.judge_config_digest,
        }

    @property
    def digest(self) -> str:
        return _canonical_digest(self._payload())

    @property
    def request_digest(self) -> str:
        return self.digest

    def as_dict(self) -> dict[str, Any]:
        return {**self._payload(), "request_digest": self.digest}

    @classmethod
    def from_dict(cls, value: Any, where: str) -> JudgmentObservationRequest:
        fields = (
            "definition_digest",
            "activation_digest",
            "payment_generation_digest",
            "bundle_digest",
            "owner_view_digest",
            "payer_view_digest",
            "replay_receipt_digest",
            "parent_chain_digest",
            "judge_point",
            "observation_medium",
            "render_mode",
            "render_scale",
            "reference",
            "observation_environment_digest",
            "external_asset_provenance_digest",
            "comparison_config_digest",
            "judge_config_digest",
            "request_digest",
        )
        row = _record(value, where, cls.SCHEMA, fields)
        raw_reference = row["reference"]
        if not isinstance(raw_reference, Mapping):
            raise ValueError(f"{where}.reference must be an object")
        reference = (
            _record(raw_reference, f"{where}.reference", None, ("digest",))
            if "digest" in raw_reference
            else _record(raw_reference, f"{where}.reference", None, ("marker",))
        )
        candidate = cls(
            definition_digest=row["definition_digest"],
            activation_digest=row["activation_digest"],
            payment_generation_digest=row["payment_generation_digest"],
            bundle_digest=row["bundle_digest"],
            owner_view_digest=row["owner_view_digest"],
            payer_view_digest=row["payer_view_digest"],
            replay_receipt_digest=row["replay_receipt_digest"],
            parent_chain_digest=row["parent_chain_digest"],
            judge_point=JudgmentPoint.from_dict(row["judge_point"], f"{where}.judge_point"),
            observation_medium=row["observation_medium"],
            render_mode=row["render_mode"],
            render_scale=row["render_scale"],
            reference_digest=reference.get("digest"),
            reference_marker=reference.get("marker"),
            observation_environment_digest=row["observation_environment_digest"],
            external_asset_provenance_digest=row["external_asset_provenance_digest"],
            comparison_config_digest=row["comparison_config_digest"],
            judge_config_digest=row["judge_config_digest"],
        )
        _require_canonical_digest(row["request_digest"], candidate.digest, where, "request_digest")
        return candidate


@dataclass(frozen=True, slots=True)
class JudgmentPaymentAttemptFailure:
    """A typed non-terminal payment failure for one sealed observation request."""

    SCHEMA: ClassVar[str] = "vfx-harness.judgment-payment-attempt-failure/v1"

    request_digest: str
    reason: str
    frame: int
    signal_metrics_digest: str
    candidate_capture_digest: str

    def __post_init__(self) -> None:
        _require_digest(self.request_digest, "JudgmentPaymentAttemptFailure.request_digest")
        if self.reason not in JUDGMENT_PAYMENT_ATTEMPT_FAILURE_REASONS:
            raise ValueError("JudgmentPaymentAttemptFailure.reason must be no_optical_signal")
        if isinstance(self.frame, bool) or not isinstance(self.frame, int) or self.frame < 1:
            raise ValueError("JudgmentPaymentAttemptFailure.frame must be a positive integer")
        _require_digest(self.signal_metrics_digest, "JudgmentPaymentAttemptFailure.signal_metrics_digest")
        _require_digest(self.candidate_capture_digest, "JudgmentPaymentAttemptFailure.candidate_capture_digest")

    @classmethod
    def for_request(
        cls,
        request: JudgmentObservationRequest,
        *,
        reason: str,
        signal_metrics_digest: str,
        candidate_capture_digest: str,
    ) -> JudgmentPaymentAttemptFailure:
        if not isinstance(request, JudgmentObservationRequest):
            raise ValueError("request must be a JudgmentObservationRequest")
        return cls(
            request_digest=request.digest,
            reason=reason,
            frame=request.judge_point.frame,
            signal_metrics_digest=signal_metrics_digest,
            candidate_capture_digest=candidate_capture_digest,
        )

    def assert_matches_request(self, request: JudgmentObservationRequest) -> None:
        if not isinstance(request, JudgmentObservationRequest):
            raise ValueError("request must be a JudgmentObservationRequest")
        if self.request_digest != request.digest:
            raise ValueError("JudgmentPaymentAttemptFailure.request_digest is stale for the observation request")
        if self.frame != request.judge_point.frame:
            raise ValueError("JudgmentPaymentAttemptFailure.frame does not match the observation request")

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "request_digest": self.request_digest,
            "reason": self.reason,
            "frame": self.frame,
            "signal_metrics_digest": self.signal_metrics_digest,
            "candidate_capture_digest": self.candidate_capture_digest,
        }

    @property
    def digest(self) -> str:
        return _canonical_digest(self._payload())

    @property
    def attempt_digest(self) -> str:
        return self.digest

    def as_dict(self) -> dict[str, Any]:
        return {**self._payload(), "attempt_digest": self.digest}

    @classmethod
    def from_dict(cls, value: Any, where: str) -> JudgmentPaymentAttemptFailure:
        row = _record(
            value,
            where,
            cls.SCHEMA,
            (
                "request_digest",
                "reason",
                "frame",
                "signal_metrics_digest",
                "candidate_capture_digest",
                "attempt_digest",
            ),
        )
        candidate = cls(
            request_digest=row["request_digest"],
            reason=row["reason"],
            frame=row["frame"],
            signal_metrics_digest=row["signal_metrics_digest"],
            candidate_capture_digest=row["candidate_capture_digest"],
        )
        _require_canonical_digest(row["attempt_digest"], candidate.digest, where, "attempt_digest")
        return candidate
