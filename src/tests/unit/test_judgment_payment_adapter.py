"""Builder payment policy keeps pre-terminal observations side-effect free."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from types import SimpleNamespace

import pytest

from tests.unit.test_layer_finalizations import _replay as _fixture_layer_replay
from vfx_harness.agents.builder import judgment_payment
from vfx_harness.domain.judgment_debts import (
    JudgmentObservationRequest,
    JudgmentPaymentAttemptFailure,
    JudgmentPoint,
)
from vfx_harness.orchestration.judgment_observation import (
    ProvisionalJudgmentObservationCompilation,
)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _request() -> JudgmentObservationRequest:
    return JudgmentObservationRequest(
        definition_digest=_digest("definition"),
        activation_digest=_digest("activation"),
        payment_generation_digest=_digest("generation"),
        bundle_digest=_digest("bundle"),
        owner_view_digest=_digest("owner-view"),
        payer_view_digest=_digest("payer-view"),
        replay_receipt_digest=_digest("replay"),
        layer_replay_receipt_digest=_fixture_layer_replay().receipt_digest,
        parent_chain_digest=_digest("parents"),
        judge_point=JudgmentPoint(frame=10, ref="refs/hall.png"),
        observation_medium="workbench_solid",
        render_mode="solid",
        render_scale=0.5,
        reference_digest=_digest("reference"),
        reference_marker=None,
        observation_environment_digest=_digest("environment"),
        external_asset_provenance_digest=_digest("assets"),
        comparison_config_digest=_digest("comparison"),
        judge_config_digest=_digest("judge"),
    )


def _payment(
    tmp_path,
    *,
    policy: judgment_payment.JudgmentDebtPaymentPolicy | None = None,
) -> judgment_payment.JudgmentDebtPayment:
    layer_replay = _fixture_layer_replay()
    return judgment_payment.JudgmentDebtPayment(
        shot=SimpleNamespace(folder=tmp_path),
        decision={
            "definition_digest": _digest("definition"),
            "judge_points": ((10, "refs/hall.png"),),
            "subject_roles": ("hall",),
            "carrier_families": ("mesh",),
            "observation_medium": "workbench_solid",
        },
        session=SimpleNamespace(canonical_observation_environment=lambda **_kwargs: {"digest": _digest("env")}),
        axes=[("reference_match", "reference identity")],
        replay_receipt=layer_replay.observation.replay_prefix,
        layer_replay_receipt=layer_replay,
        policy=policy,
    )


def _no_signal_verdict() -> dict:
    return {
        "decided_by": "no_optical_signal",
        "signal": {"has_signal": False, "dynamic_range": 0.0},
    }


def _failure(
    request: JudgmentObservationRequest,
    label: str,
) -> JudgmentPaymentAttemptFailure:
    return JudgmentPaymentAttemptFailure.for_request(
        request,
        reason="no_optical_signal",
        signal_metrics_digest=_digest(f"{label}-signal"),
        candidate_capture_digest=_digest(f"{label}-capture"),
    )


def _projected_failure(
    request: JudgmentObservationRequest,
    failure: JudgmentPaymentAttemptFailure,
) -> dict:
    return {"request": request.as_dict(), "failure": failure.as_dict()}


def test_provisional_prepare_uses_pending_compiler_without_due_lookup(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    calls: list[str] = []

    def compile_provisional(*_args, **_kwargs):
        calls.append("provisional")
        return ProvisionalJudgmentObservationCompilation(request, "pending_not_due")

    monkeypatch.setattr(
        judgment_payment,
        "compile_provisional_judgment_observation_request",
        compile_provisional,
    )
    monkeypatch.setattr(
        judgment_payment,
        "compile_current_judgment_observation_request",
        lambda *_args, **_kwargs: pytest.fail("provisional payment used the due compiler"),
    )
    monkeypatch.setattr(
        judgment_payment,
        "current_payment_attempt_failure",
        lambda *_args, **_kwargs: pytest.fail("pending debt performed a due-state lookup"),
    )
    monkeypatch.setattr(judgment_payment, "critic_model", lambda: "bounded")
    payment = _payment(
        tmp_path,
        policy=judgment_payment.JudgmentDebtPaymentPolicy.layer_finalization(),
    )

    prepared = payment.prepare(
        frame=10,
        ref="refs/hall.png",
        render_mode="solid",
        render_scale=0.5,
    )

    assert calls == ["provisional"]
    assert prepared == judgment_payment.PreparedJudgmentObservation(request, None)


def test_default_prepare_uses_due_compiler_and_durable_suppression(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    prior = JudgmentPaymentAttemptFailure.for_request(
        request,
        reason="no_optical_signal",
        signal_metrics_digest=_digest("prior-signal"),
        candidate_capture_digest=_digest("prior-capture"),
    )
    calls: list[str] = []

    def compile_due(*_args, **_kwargs):
        calls.append("due")
        return request

    def find_prior(_folder, observed_request):
        calls.append("prior")
        assert observed_request == request
        return prior

    monkeypatch.setattr(
        judgment_payment,
        "compile_current_judgment_observation_request",
        compile_due,
    )
    monkeypatch.setattr(
        judgment_payment,
        "compile_provisional_judgment_observation_request",
        lambda *_args, **_kwargs: pytest.fail("default payment used provisional compiler"),
    )
    monkeypatch.setattr(judgment_payment, "current_payment_attempt_failure", find_prior)
    monkeypatch.setattr(judgment_payment, "critic_model", lambda: "bounded")
    payment = _payment(tmp_path)

    prepared = payment.prepare(
        frame=10,
        ref="refs/hall.png",
        render_mode="solid",
        render_scale=0.5,
    )

    assert calls == ["due", "prior"]
    assert prepared == judgment_payment.PreparedJudgmentObservation(request, prior)


def test_deferred_no_signal_does_not_publish_until_deterministic_flush(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    prepared = judgment_payment.PreparedJudgmentObservation(request, None)
    payment = _payment(
        tmp_path,
        policy=judgment_payment.JudgmentDebtPaymentPolicy.layer_finalization(),
    )
    monkeypatch.setattr(
        payment,
        "candidate_capture",
        lambda *_args, **_kwargs: {"capture_digest": _digest("capture")},
    )
    published: list[tuple[JudgmentObservationRequest, object]] = []

    def publish(_folder, observed_request, failure):
        published.append((observed_request, failure))
        return failure

    monkeypatch.setattr(judgment_payment, "record_payment_attempt_failure", publish)

    failure = payment.record_no_optical_signal(
        prepared,
        render_rel="runs/r/evidence/candidate.png",
        render_receipt={},
        verdict=_no_signal_verdict(),
    )

    assert published == []
    assert payment.deferred_payment_attempt_failures == (
        judgment_payment.DeferredJudgmentPaymentFailure(request, failure),
    )

    assert payment.flush_deferred_payment_attempt_failures() == (failure,)
    assert published == [(request, failure)]
    assert payment.deferred_payment_attempt_failures == ()


def test_default_payment_publishes_no_signal_immediately(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    prepared = judgment_payment.PreparedJudgmentObservation(request, None)
    payment = _payment(tmp_path)
    monkeypatch.setattr(
        payment,
        "candidate_capture",
        lambda *_args, **_kwargs: {"capture_digest": _digest("capture")},
    )
    published: list[tuple[JudgmentObservationRequest, object]] = []

    def publish(_folder, observed_request, failure):
        published.append((observed_request, failure))
        return failure

    monkeypatch.setattr(judgment_payment, "record_payment_attempt_failure", publish)

    failure = payment.record_no_optical_signal(
        prepared,
        render_rel="runs/r/evidence/candidate.png",
        render_receipt={},
        verdict=_no_signal_verdict(),
    )

    assert published == [(request, failure)]
    assert payment.deferred_payment_attempt_failures == ()


def test_reconcile_deferred_payment_failures_parses_before_ordered_idempotent_publish(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_request = _request()
    second_request = replace(
        first_request,
        parent_chain_digest=_digest("second-parent-chain"),
    )
    first_failure = _failure(first_request, "first")
    second_failure = _failure(second_request, "second")
    durable: dict[str, JudgmentPaymentAttemptFailure] = {}
    calls: list[str] = []

    def publish(_folder, request, failure):
        calls.append(request.digest)
        existing = durable.setdefault(request.digest, failure)
        assert existing == failure
        return existing

    monkeypatch.setattr(judgment_payment, "record_payment_attempt_failure", publish)
    projection = (
        _projected_failure(first_request, first_failure),
        _projected_failure(second_request, second_failure),
    )

    assert judgment_payment.reconcile_judgment_payment_attempt_failures(
        tmp_path,
        iter(projection),
    ) == (first_failure, second_failure)
    assert judgment_payment.reconcile_judgment_payment_attempt_failures(
        tmp_path,
        projection,
    ) == (first_failure, second_failure)
    assert calls == [
        first_request.digest,
        second_request.digest,
        first_request.digest,
        second_request.digest,
    ]


@pytest.mark.parametrize(
    "malformed",
    [
        None,
        "not-a-row",
        {},
        {"request": {}},
        {"request": {}, "failure": {}, "extra": True},
    ],
)
def test_reconcile_deferred_payment_failures_rejects_malformed_rows_before_publish(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    malformed,
) -> None:
    request = _request()
    failure = _failure(request, "valid")
    monkeypatch.setattr(
        judgment_payment,
        "record_payment_attempt_failure",
        lambda *_args, **_kwargs: pytest.fail("malformed projection was partially published"),
    )

    with pytest.raises(ValueError, match=r"payment_failures\[1\]"):
        judgment_payment.reconcile_judgment_payment_attempt_failures(
            tmp_path,
            (_projected_failure(request, failure), malformed),
        )


def test_reconcile_deferred_payment_failures_rejects_stale_serialized_digest(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    failure = _failure(request, "stale")
    row = _projected_failure(request, failure)
    row["failure"]["attempt_digest"] = _digest("forged-attempt")
    monkeypatch.setattr(
        judgment_payment,
        "record_payment_attempt_failure",
        lambda *_args, **_kwargs: pytest.fail("stale projection was published"),
    )

    with pytest.raises(ValueError, match="attempt_digest"):
        judgment_payment.reconcile_judgment_payment_attempt_failures(tmp_path, (row,))


@pytest.mark.parametrize(
    "rows",
    [None, "not-rows", {"request": {}, "failure": {}}, set(), frozenset()],
)
def test_reconcile_deferred_payment_failures_requires_an_ordered_iterable(
    tmp_path,
    rows,
) -> None:
    with pytest.raises(ValueError, match="ordered iterable"):
        judgment_payment.reconcile_judgment_payment_attempt_failures(tmp_path, rows)


def test_reconcile_deferred_payment_failures_rejects_duplicate_and_conflict_before_publish(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    first = _failure(request, "first")
    conflict = _failure(request, "conflict")
    monkeypatch.setattr(
        judgment_payment,
        "record_payment_attempt_failure",
        lambda *_args, **_kwargs: pytest.fail("duplicate projection was partially published"),
    )

    with pytest.raises(ValueError, match="duplicate payment failure"):
        judgment_payment.reconcile_judgment_payment_attempt_failures(
            tmp_path,
            (
                _projected_failure(request, first),
                _projected_failure(request, first),
            ),
        )
    with pytest.raises(ValueError, match="conflicting payment failure"):
        judgment_payment.reconcile_judgment_payment_attempt_failures(
            tmp_path,
            (
                _projected_failure(request, first),
                _projected_failure(request, conflict),
            ),
        )


def test_reconcile_deferred_payment_failures_rejects_request_failure_mismatch(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    other_request = replace(
        request,
        replay_receipt_digest=_digest("other-replay"),
    )
    failure = _failure(request, "mismatch")
    monkeypatch.setattr(
        judgment_payment,
        "record_payment_attempt_failure",
        lambda *_args, **_kwargs: pytest.fail("mismatched projection was published"),
    )

    with pytest.raises(ValueError, match="request_digest is stale"):
        judgment_payment.reconcile_judgment_payment_attempt_failures(
            tmp_path,
            (_projected_failure(other_request, failure),),
        )


def test_reconcile_deferred_payment_failures_rejects_non_exact_publisher_result(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    expected = _failure(request, "expected")
    unexpected = _failure(request, "unexpected")
    monkeypatch.setattr(
        judgment_payment,
        "record_payment_attempt_failure",
        lambda *_args, **_kwargs: unexpected,
    )

    with pytest.raises(ValueError, match="does not match its terminal projection"):
        judgment_payment.reconcile_judgment_payment_attempt_failures(
            tmp_path,
            (_projected_failure(request, expected),),
        )
