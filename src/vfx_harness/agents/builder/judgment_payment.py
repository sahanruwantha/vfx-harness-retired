"""Builder-side adapter for exactly-once qualitative-debt observations."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vfx_harness.agents.builder.models import critic_model
from vfx_harness.domain.judgment_debts import (
    JudgmentObservationRequest,
    JudgmentPaymentAttemptFailure,
)
from vfx_harness.orchestration.judgment_debt_state import ReplayPrefixReceipt
from vfx_harness.orchestration.judgment_observation import (
    compile_current_judgment_observation_request,
)
from vfx_harness.orchestration.judgment_payment_attempts import (
    current_payment_attempt_failure,
    record_payment_attempt_failure,
)


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _implementation_digest(filename: str) -> str:
    path = Path(__file__).with_name(filename)
    if not path.is_file():
        raise ValueError(f"judgment implementation input is missing: {path}")
    return _sha256(path)


@dataclass(frozen=True, slots=True)
class PreparedJudgmentObservation:
    request: JudgmentObservationRequest
    prior_failure: JudgmentPaymentAttemptFailure | None


class JudgmentDebtPayment:
    """One exact debt activation evaluated against one cumulative replay receipt."""

    def __init__(
        self,
        *,
        shot,
        decision: dict,
        session,
        axes: list[tuple[str, str]],
        replay_receipt: ReplayPrefixReceipt,
    ) -> None:
        self.shot = shot
        self.decision = decision
        self.session = session
        self.axes = tuple((str(key), str(description)) for key, description in axes)
        self.replay_receipt = replay_receipt
        self._points = {
            (int(frame), str(ref))
            for frame, ref in tuple(decision.get("judge_points") or ())
        }

    def prepare(
        self,
        *,
        frame: int,
        ref: str,
        render_mode: str,
        render_scale: float,
    ) -> PreparedJudgmentObservation | None:
        """Compile and query the exact request before rendering its judge point."""
        if (int(frame), str(ref)) not in self._points:
            return None
        environment = self.session.canonical_observation_environment(
            frame=int(frame),
            subject_roles=tuple(self.decision["subject_roles"]),
            carrier_families=tuple(self.decision["carrier_families"]),
            observation_medium=str(self.decision["observation_medium"]),
        )
        request = compile_current_judgment_observation_request(
            self.shot.folder,
            str(self.decision["definition_digest"]),
            replay_receipt=self.replay_receipt,
            frame=int(frame),
            ref=str(ref),
            render_mode=render_mode,
            render_scale=render_scale,
            observation_environment=environment,
            comparison_config={
                "schema": "vfx-harness.judgment-comparison-config/v1",
                "render_mode": render_mode,
                "render_scale": float(render_scale),
                "optical_signal_contract": "vfx-harness.optical-signal/v1",
                "optical_signal_implementation": _implementation_digest("critic_focus.py"),
                "render_implementation": _implementation_digest("evidence.py"),
            },
            judge_config={
                "schema": "vfx-harness.judgment-judge-config/v1",
                "model": critic_model(),
                "axes": [
                    {"key": key, "description": description}
                    for key, description in self.axes
                ],
                "critic_implementation": _implementation_digest("critic.py"),
                "verdict_implementation": _implementation_digest("verdicts.py"),
            },
        )
        return PreparedJudgmentObservation(
            request=request,
            prior_failure=current_payment_attempt_failure(
                self.shot.folder,
                request,
            ),
        )

    def cached_verdict(
        self,
        prepared: PreparedJudgmentObservation,
    ) -> dict[str, Any]:
        """Re-emit a typed stop without pretending to make another observation."""
        failure = prepared.prior_failure
        if failure is None:
            raise ValueError("cached judgment verdict requires a prior payment failure")
        failure.assert_matches_request(prepared.request)
        scored_axes = [key for key, _description in self.axes]
        return {
            "scores": dict.fromkeys(scored_axes, 1),
            "mean": 1.0,
            "pass": False,
            "issues": [
                "unchanged qualitative-payment observation already produced no optical "
                "signal; repair authoritative inputs before another raster or critic call"
            ],
            "scored_axes": scored_axes,
            "na_axes": [],
            "observations": [],
            "decided_by": "unchanged_payment_attempt",
            "judge_conflict": False,
            "contract_gap": False,
            "payment_attempt": {
                "request_digest": prepared.request.digest,
                "attempt_digest": failure.digest,
                "reason": failure.reason,
                "candidate_capture_digest": failure.candidate_capture_digest,
                "signal_metrics_digest": failure.signal_metrics_digest,
                "suppressed": True,
            },
            "judgment_observation": {
                "request": prepared.request.as_dict(),
                "candidate_capture": None,
                "reused_attempt": failure.as_dict(),
            },
        }

    def candidate_capture(
        self,
        prepared: PreparedJudgmentObservation,
        *,
        render_rel: str,
        render_receipt: dict[str, Any],
    ) -> dict[str, Any]:
        """Validate the worker render receipt against its sealed pre-render request."""
        expected = {
            "schema",
            "frame",
            "mode",
            "scale",
            "resolution",
            "render_state",
            "warnings",
            "png_sha256",
            "capture_digest",
        }
        if not isinstance(render_receipt, dict) or set(render_receipt) != expected:
            raise ValueError("judgment candidate has no strict canonical render receipt")
        if render_receipt.get("schema") != "vfx-harness.canonical-render-capture/v1":
            raise ValueError("judgment candidate render receipt has an unsupported schema")
        request = prepared.request
        if (
            render_receipt.get("frame") != request.judge_point.frame
            or render_receipt.get("mode") != request.render_mode
            or float(render_receipt.get("scale", 0.0)) != request.render_scale
        ):
            raise ValueError("judgment candidate render receipt does not match its request")
        payload = dict(render_receipt)
        found_digest = payload.pop("capture_digest")
        if found_digest != _canonical_digest(payload):
            raise ValueError("judgment candidate render receipt digest is stale")
        candidate = (Path(self.shot.folder) / render_rel).resolve()
        shot = Path(self.shot.folder).resolve()
        try:
            candidate.relative_to(shot)
        except ValueError as exc:
            raise ValueError("judgment candidate capture escapes the shot root") from exc
        if not candidate.is_file() or _sha256(candidate) != render_receipt["png_sha256"]:
            raise ValueError("judgment candidate capture bytes do not match their render receipt")
        return dict(render_receipt)

    def record_no_optical_signal(
        self,
        prepared: PreparedJudgmentObservation,
        *,
        render_rel: str,
        render_receipt: dict[str, Any],
        verdict: dict[str, Any],
    ) -> JudgmentPaymentAttemptFailure:
        """Persist one failed attempt after a real candidate signal probe."""
        if prepared.prior_failure is not None:
            raise ValueError("a suppressed judgment observation cannot record a new failure")
        if verdict.get("decided_by") != "no_optical_signal":
            raise ValueError("only no_optical_signal is a legal v1 payment-attempt failure")
        signal = verdict.get("signal")
        if not isinstance(signal, dict):
            raise ValueError("no_optical_signal verdict is missing typed signal metrics")
        capture = self.candidate_capture(
            prepared,
            render_rel=render_rel,
            render_receipt=render_receipt,
        )
        failure = JudgmentPaymentAttemptFailure.for_request(
            prepared.request,
            reason="no_optical_signal",
            signal_metrics_digest=_canonical_digest(
                {
                    "schema": "vfx-harness.optical-signal-evidence/v1",
                    "metrics": signal,
                }
            ),
            candidate_capture_digest=str(capture["capture_digest"]),
        )
        return record_payment_attempt_failure(
            self.shot.folder,
            prepared.request,
            failure,
        )
