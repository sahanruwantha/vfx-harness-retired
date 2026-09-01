"""Builder-side adapter for exactly-once qualitative-debt observations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from vfx_harness.agents.builder.models import critic_model
from vfx_harness.domain.judgment_debt_replay_receipts import ReplayPrefixReceipt
from vfx_harness.domain.judgment_debts import (
    JudgmentObservationRequest,
    JudgmentPaymentAttemptFailure,
)
from vfx_harness.domain.layer_finalizations import LayerReplayReceipt
from vfx_harness.orchestration.judgment_observation import (
    compile_current_judgment_observation_request,
    compile_provisional_judgment_observation_request,
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


@dataclass(frozen=True, slots=True)
class JudgmentDebtPaymentPolicy:
    """Select lifecycle reads and durable failure-publication timing."""

    observation: Literal["due", "provisional"] = "due"
    failure_publication: Literal["immediate", "deferred"] = "immediate"

    def __post_init__(self) -> None:
        if self.observation not in ("due", "provisional"):
            raise ValueError("judgment payment observation must be due or provisional")
        if self.failure_publication not in ("immediate", "deferred"):
            raise ValueError("judgment payment failure publication must be immediate or deferred")

    @classmethod
    def layer_finalization(cls) -> JudgmentDebtPaymentPolicy:
        return cls(observation="provisional", failure_publication="deferred")


@dataclass(frozen=True, slots=True)
class DeferredJudgmentPaymentFailure:
    """One in-memory failure waiting for its terminal transaction to publish."""

    request: JudgmentObservationRequest
    failure: JudgmentPaymentAttemptFailure

    def __post_init__(self) -> None:
        if not isinstance(self.request, JudgmentObservationRequest):
            raise ValueError("DeferredJudgmentPaymentFailure.request must be a JudgmentObservationRequest")
        if not isinstance(self.failure, JudgmentPaymentAttemptFailure):
            raise ValueError("DeferredJudgmentPaymentFailure.failure must be a JudgmentPaymentAttemptFailure")
        self.failure.assert_matches_request(self.request)


def reconcile_judgment_payment_attempt_failures(
    shot_folder: str | Path,
    rows: Iterable[Mapping[str, Any]],
) -> tuple[JudgmentPaymentAttemptFailure, ...]:
    """Publish exact terminal-receipt payment failures without a live adapter.

    Every row is parsed and cross-checked before the first durable write.  This
    preserves input order while ensuring that a malformed, duplicate, or
    internally conflicting terminal projection cannot cause a partial replay.
    The underlying append operation is idempotent for an exact request/failure
    pair and fails closed if durable authority already contains another result.
    """
    if isinstance(rows, (str, bytes, Mapping, set, frozenset)):
        raise ValueError("deferred payment-attempt failures must be an ordered iterable of objects")
    try:
        projected_rows = tuple(rows)
    except TypeError as exc:
        raise ValueError("deferred payment-attempt failures must be an ordered iterable of objects") from exc

    parsed: list[tuple[JudgmentObservationRequest, JudgmentPaymentAttemptFailure]] = []
    by_request: dict[str, JudgmentPaymentAttemptFailure] = {}
    by_attempt: dict[str, str] = {}
    for index, value in enumerate(projected_rows):
        where = f"payment_failures[{index}]"
        if not isinstance(value, Mapping) or set(value) != {"request", "failure"}:
            raise ValueError(f"{where} must contain exactly request and failure")
        try:
            request = JudgmentObservationRequest.from_dict(
                value["request"],
                f"{where}.request",
            )
            failure = JudgmentPaymentAttemptFailure.from_dict(
                value["failure"],
                f"{where}.failure",
            )
            failure.assert_matches_request(request)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{where} is not an exact payment-attempt failure: {exc}") from exc

        prior = by_request.get(request.digest)
        if prior is not None:
            qualifier = "duplicate" if prior == failure else "conflicting"
            raise ValueError(f"{where} is a {qualifier} payment failure for request {request.digest}")
        prior_request = by_attempt.get(failure.digest)
        if prior_request is not None:
            raise ValueError(
                f"{where} reuses payment attempt {failure.digest} for requests {prior_request} and {request.digest}"
            )
        by_request[request.digest] = failure
        by_attempt[failure.digest] = request.digest
        parsed.append((request, failure))

    published: list[JudgmentPaymentAttemptFailure] = []
    for request, failure in parsed:
        recorded = record_payment_attempt_failure(shot_folder, request, failure)
        if recorded != failure:
            raise ValueError("published payment-attempt failure does not match its terminal projection")
        published.append(recorded)
    return tuple(published)


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
        layer_replay_receipt: LayerReplayReceipt,
        policy: JudgmentDebtPaymentPolicy | None = None,
    ) -> None:
        self.shot = shot
        self.decision = decision
        self.session = session
        self.axes = tuple((str(key), str(description)) for key, description in axes)
        self.replay_receipt = replay_receipt
        if not isinstance(layer_replay_receipt, LayerReplayReceipt):
            raise ValueError(
                "judgment payment requires a typed layer replay receipt"
            )
        if layer_replay_receipt.observation.replay_prefix != replay_receipt:
            raise ValueError(
                "judgment payment replay prefix does not match its stored layer receipt"
            )
        self.layer_replay_receipt = layer_replay_receipt
        self.policy = JudgmentDebtPaymentPolicy() if policy is None else policy
        if not isinstance(self.policy, JudgmentDebtPaymentPolicy):
            raise ValueError("policy must be a JudgmentDebtPaymentPolicy")
        self._deferred_failures: dict[str, DeferredJudgmentPaymentFailure] = {}
        self._points = {(int(frame), str(ref)) for frame, ref in tuple(decision.get("judge_points") or ())}

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
        kwargs = {
            "replay_receipt": self.replay_receipt,
            "layer_replay_receipt_digest": self.layer_replay_receipt.receipt_digest,
            "frame": int(frame),
            "ref": str(ref),
            "render_mode": render_mode,
            "render_scale": render_scale,
            "observation_environment": environment,
            "comparison_config": {
                "schema": "vfx-harness.judgment-comparison-config/v1",
                "render_mode": render_mode,
                "render_scale": float(render_scale),
                "optical_signal_contract": "vfx-harness.optical-signal/v1",
                "optical_signal_implementation": _implementation_digest("critic_focus.py"),
                "render_implementation": _implementation_digest("evidence.py"),
            },
            "judge_config": {
                "schema": "vfx-harness.judgment-judge-config/v1",
                "model": critic_model(),
                "axes": [{"key": key, "description": description} for key, description in self.axes],
                "critic_implementation": _implementation_digest("critic.py"),
                "verdict_implementation": _implementation_digest("verdicts.py"),
            },
        }
        lifecycle = "due"
        if self.policy.observation == "provisional":
            compilation = compile_provisional_judgment_observation_request(
                self.shot.folder,
                str(self.decision["definition_digest"]),
                **kwargs,
            )
            request = compilation.request
            lifecycle = compilation.lifecycle
        else:
            request = compile_current_judgment_observation_request(
                self.shot.folder,
                str(self.decision["definition_digest"]),
                **kwargs,
            )
        deferred = self._deferred_failures.get(request.digest)
        prior_failure = None if deferred is None else deferred.failure
        if prior_failure is None and lifecycle == "due":
            prior_failure = current_payment_attempt_failure(
                self.shot.folder,
                request,
            )
        return PreparedJudgmentObservation(
            request=request,
            prior_failure=prior_failure,
        )

    @property
    def deferred_payment_attempt_failures(
        self,
    ) -> tuple[DeferredJudgmentPaymentFailure, ...]:
        """Return pending publications in canonical request-digest order."""
        return tuple(self._deferred_failures[key] for key in sorted(self._deferred_failures))

    def flush_deferred_payment_attempt_failures(
        self,
    ) -> tuple[JudgmentPaymentAttemptFailure, ...]:
        """Publish retained failures after the owning terminal receipt exists.

        Durable payment-attempt authority independently requires the debt to be due,
        so calling this before the finalization transaction activates debt fails closed.
        Each successful row is removed only after its append is confirmed; a partial
        failure can therefore be retried without repeating already-published rows.
        """
        published: list[JudgmentPaymentAttemptFailure] = []
        for request_digest in sorted(self._deferred_failures):
            deferred = self._deferred_failures[request_digest]
            failure = record_payment_attempt_failure(
                self.shot.folder,
                deferred.request,
                deferred.failure,
            )
            if failure != deferred.failure:
                raise ValueError("published payment-attempt failure does not match the retained failure")
            published.append(failure)
            del self._deferred_failures[request_digest]
        return tuple(published)

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
        if self.policy.failure_publication == "immediate":
            return record_payment_attempt_failure(
                self.shot.folder,
                prepared.request,
                failure,
            )
        deferred = DeferredJudgmentPaymentFailure(prepared.request, failure)
        existing = self._deferred_failures.get(prepared.request.digest)
        if existing is not None and existing != deferred:
            raise ValueError("deferred payment-attempt failure conflicts with the retained exact request")
        self._deferred_failures[prepared.request.digest] = deferred
        return failure
