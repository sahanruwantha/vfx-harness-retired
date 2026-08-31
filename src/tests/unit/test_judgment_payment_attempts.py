"""Strict durable suppression of current qualitative-payment failures."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from vfx_harness.domain.judgment_debts import (
    JudgmentDebtActivation,
    JudgmentDebtDefinition,
    JudgmentDebtSeed,
    JudgmentDebtState,
    JudgmentObservationRequest,
    JudgmentPaymentAttemptFailure,
    JudgmentPoint,
    JudgmentProvider,
    activate_judgment_debt,
    compile_judgment_debt,
)
from vfx_harness.orchestration import judgment_payment_attempts


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


BUNDLE_DIGEST = _digest("selected-bundle")
PAYER_DIGEST = _digest("payer-unit")


def _due_authority() -> tuple[JudgmentDebtDefinition, JudgmentDebtActivation, JudgmentDebtState]:
    definition = compile_judgment_debt(
        JudgmentDebtSeed(
            requirement_id="R-hall",
            statement="The hall reads as the reference.",
            decision_strength="approved_start",
            claim_kind="atomic",
            property="reference_identity",
            owner_layer="camera",
            fault_owner="form",
            subject_roles=("hall",),
            axes=("reference_match",),
            judge_points=(JudgmentPoint(frame=1, ref="refs/hall.png"),),
            observation_medium="workbench_solid",
            lifecycle="persistent",
            bundle_digest=BUNDLE_DIGEST,
            carrier_families=("mesh",),
        ),
        (JudgmentProvider("hall-mesh", "form", "mesh", ("hall.mass",)),),
        layer_dependencies={"camera": (), "form": ("camera",)},
        layer_order=("camera", "form"),
    )
    activation = JudgmentDebtActivation.for_definition(
        definition,
        payer_unit_digests=(("form:hall", PAYER_DIGEST),),
    )
    due = activate_judgment_debt(
        definition,
        JudgmentDebtState.pending(definition),
        activation=activation,
        layer_id=activation.payer_layer,
        replayed_unit_digests=activation.payer_unit_digests,
    )
    return definition, activation, due


def _request(
    definition: JudgmentDebtDefinition, activation: JudgmentDebtActivation
) -> JudgmentObservationRequest:
    point = definition.seed.judge_points[0]
    return JudgmentObservationRequest(
        definition_digest=definition.digest,
        activation_digest=activation.digest,
        payment_generation_digest=_digest("payment-generation"),
        bundle_digest=BUNDLE_DIGEST,
        owner_view_digest=_digest("owner-view"),
        payer_view_digest=_digest("payer-view"),
        replay_receipt_digest=_digest("replay-receipt"),
        parent_chain_digest=_digest("parent-chain"),
        judge_point=point,
        observation_medium="workbench_solid",
        render_mode="solid",
        render_scale=0.5,
        reference_digest=None,
        reference_marker="no-reference",
        observation_environment_digest=_digest("observation-environment"),
        external_asset_provenance_digest=_digest("asset-provenance"),
        comparison_config_digest=_digest("comparison-config"),
        judge_config_digest=_digest("judge-config"),
    )


def _failure(request: JudgmentObservationRequest, *, label: str = "first") -> JudgmentPaymentAttemptFailure:
    return JudgmentPaymentAttemptFailure.for_request(
        request,
        reason="no_optical_signal",
        signal_metrics_digest=_digest(f"signal-metrics-{label}"),
        candidate_capture_digest=_digest(f"candidate-capture-{label}"),
    )


def _patch_current_due(
    monkeypatch: pytest.MonkeyPatch,
    definition: JudgmentDebtDefinition,
    activation: JudgmentDebtActivation,
    state: JudgmentDebtState,
) -> None:
    monkeypatch.setattr(
        judgment_payment_attempts.plan_authority,
        "resolve_current",
        lambda _shot: SimpleNamespace(content_hash=BUNDLE_DIGEST),
    )
    monkeypatch.setattr(
        judgment_payment_attempts.judgment_debt_state,
        "current_judgment_debt_states",
        lambda _shot: ((definition, activation, state),),
    )


def _append_raw(shot: Path, value: dict) -> None:
    path = shot / "state" / judgment_payment_attempts.EVENTS
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def test_records_exact_current_due_failure_once_and_returns_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition, activation, due = _due_authority()
    _patch_current_due(monkeypatch, definition, activation, due)
    request = _request(definition, activation)
    failure = _failure(request)

    assert judgment_payment_attempts.current_payment_attempt_failure(tmp_path, request) is None
    assert judgment_payment_attempts.record_payment_attempt_failure(tmp_path, request, failure) == failure
    assert judgment_payment_attempts.current_payment_attempt_failure(tmp_path, request) == failure
    assert judgment_payment_attempts.record_payment_attempt_failure(tmp_path, request, failure) == failure

    rows = (tmp_path / "state" / judgment_payment_attempts.EVENTS).read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1
    row = json.loads(rows[0])
    assert set(row) == {
        "schema",
        "bundle_digest",
        "debt_id",
        "definition_digest",
        "activation_digest",
        "request",
        "failure",
    }
    assert row["bundle_digest"] == BUNDLE_DIGEST
    assert row["definition_digest"] == definition.digest
    assert row["activation_digest"] == activation.digest
    assert row["request"] == request.as_dict()
    assert row["failure"] == failure.as_dict()


def test_rejects_conflicting_failure_for_the_same_current_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition, activation, due = _due_authority()
    _patch_current_due(monkeypatch, definition, activation, due)
    request = _request(definition, activation)
    judgment_payment_attempts.record_payment_attempt_failure(tmp_path, request, _failure(request))

    with pytest.raises(ValueError, match="conflicts with the existing exact request record"):
        judgment_payment_attempts.record_payment_attempt_failure(
            tmp_path, request, _failure(request, label="conflict")
        )


def test_ignores_valid_superseded_bundle_or_activation_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition, activation, due = _due_authority()
    _patch_current_due(monkeypatch, definition, activation, due)
    request = _request(definition, activation)
    failure = _failure(request)
    old_bundle = _digest("superseded-bundle")
    old_activation = _digest("superseded-activation")
    old_bundle_request = replace(request, bundle_digest=old_bundle)
    old_activation_request = replace(request, activation_digest=old_activation)
    _append_raw(
        tmp_path,
        judgment_payment_attempts._PaymentAttemptEvent(
            bundle_digest=old_bundle,
            debt_id=definition.debt_id,
            definition_digest=definition.digest,
            activation_digest=activation.digest,
            request=old_bundle_request,
            failure=JudgmentPaymentAttemptFailure.for_request(
                old_bundle_request,
                reason="no_optical_signal",
                signal_metrics_digest=_digest("old-bundle-signal"),
                candidate_capture_digest=_digest("old-bundle-capture"),
            ),
        ).as_dict(),
    )
    path = tmp_path / "state" / judgment_payment_attempts.EVENTS
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                judgment_payment_attempts._PaymentAttemptEvent(
                    bundle_digest=BUNDLE_DIGEST,
                    debt_id=definition.debt_id,
                    definition_digest=definition.digest,
                    activation_digest=old_activation,
                    request=old_activation_request,
                    failure=JudgmentPaymentAttemptFailure.for_request(
                        old_activation_request,
                        reason="no_optical_signal",
                        signal_metrics_digest=_digest("old-activation-signal"),
                        candidate_capture_digest=_digest("old-activation-capture"),
                    ),
                ).as_dict(),
                sort_keys=True,
            )
            + "\n"
        )

    assert judgment_payment_attempts.current_payment_attempt_failure(tmp_path, request) is None
    assert judgment_payment_attempts.record_payment_attempt_failure(tmp_path, request, failure) == failure
    assert len(path.read_text(encoding="utf-8").splitlines()) == 3


def test_refuses_malformed_history_and_non_due_requests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition, activation, due = _due_authority()
    _patch_current_due(monkeypatch, definition, activation, due)
    request = _request(definition, activation)
    _append_raw(tmp_path, {"schema": judgment_payment_attempts.EVENT_SCHEMA})

    with pytest.raises(ValueError, match="unsupported judgment-payment-attempt event shape"):
        judgment_payment_attempts.current_payment_attempt_failure(tmp_path, request)

    with pytest.raises(ValueError, match="is not the selected bundle"):
        judgment_payment_attempts.current_payment_attempt_failure(
            tmp_path / "stale-bundle",
            replace(request, bundle_digest=_digest("stale-bundle")),
        )
    with pytest.raises(ValueError, match="activation_digest is stale"):
        judgment_payment_attempts.current_payment_attempt_failure(
            tmp_path / "stale-activation",
            replace(request, activation_digest=_digest("stale-activation")),
        )

    _patch_current_due(
        monkeypatch,
        definition,
        activation,
        JudgmentDebtState.pending(definition),
    )
    with pytest.raises(ValueError, match="requires due state, not pending_not_due"):
        judgment_payment_attempts.current_payment_attempt_failure(tmp_path / "other", request)
