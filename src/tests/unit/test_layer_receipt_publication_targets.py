from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from threading import Thread

import pytest

import vfx_harness.orchestration.layer_evaluation_receipts as evaluation_receipt_publication
import vfx_harness.orchestration.layer_replay_receipts as replay_receipt_publication
import vfx_harness.orchestration.shot_authority_capture as shot_authority_capture
from tests.unit.test_layer_finalization_state import (
    _claim,
    _claim_guard,
    _layer,
    _lower_boundary_receipt_authority,  # noqa: F401  # imported pytest fixture
    _pass_layer_units,
    _publish_artifact,
    _publish_replay,
)
from vfx_harness.domain.layer_finalizations import (
    LayerEvaluationReceipt,
    LayerReplayReceiptBinding,
    canonical_layer_evaluation_receipt_bytes,
    canonical_layer_replay_receipt_bytes,
)
from vfx_harness.orchestration.layer_evaluation_receipts import (
    LayerEvaluationReceiptConflict,
    commit_layer_evaluation_receipt,
    discard_layer_evaluation_receipt,
    prepare_layer_evaluation_receipt,
)
from vfx_harness.orchestration.layer_replay_receipts import (
    LayerReplayReceiptConflict,
    commit_layer_replay_receipt,
    discard_layer_replay_receipt,
    prepare_layer_replay_receipt,
)


def _active_replay(folder: Path):
    layer = _layer()
    _pass_layer_units(folder, layer)
    guard = _claim_guard(folder, layer, _claim(folder, layer))
    _path, script_sha256 = _publish_artifact(folder, guard)
    replay, stored = _publish_replay(folder, guard, script_sha256)
    return guard, replay, stored


def _passing_evaluation(replay, stored) -> LayerEvaluationReceipt:
    point = replay.observation.points[0]
    canonical = (
        {
            "frame": point.frame,
            "ref": point.ref,
            "verdict": {
                "evidence_kind": "executable_only",
                "pass": True,
                "issues": [],
                "evidence": list(point.evidence),
                "evidence_failures": [],
                "missing_evidence": [],
                "decided_by": "unit_executable_evidence",
                "layer_replay_receipt_digest": replay.receipt_digest,
            },
        },
    )
    return LayerEvaluationReceipt.mint(
        replay_receipts=(
            LayerReplayReceiptBinding.mint(
                locator=stored.locator,
                sha256=stored.sha256,
                receipt=replay,
            ),
        ),
        evaluation_groups=(
            {
                "group_index": 0,
                "result": "passed",
                "requirement_ids": [],
                "debt_id": None,
                "definition_digest": None,
                "activation_digest": None,
                "canonical_start": 0,
                "canonical_end": 1,
                "payment_failures": [],
            },
        ),
        canonical=canonical,
        created_at="2026-09-01T10:01:30+00:00",
    )


def test_replay_noop_retains_exact_authority_destination_and_refuses_cross_shot(
    tmp_path: Path,
) -> None:
    guard, replay, stored = _active_replay(tmp_path)
    prepared = prepare_layer_replay_receipt(
        tmp_path / "unused" / "..",
        replay,
        guard,
    )
    assert prepared.destination == tmp_path / stored.locator
    with pytest.raises(AttributeError):
        _ = prepared.publication
    with pytest.raises(AttributeError):
        _ = prepared.shot
    with pytest.raises(TypeError, match="dataclass instances"):
        replace(prepared, shot=tmp_path)

    foreign = tmp_path / "foreign-shot"
    foreign.mkdir()

    with pytest.raises(LayerReplayReceiptConflict, match="another shot root"):
        commit_layer_replay_receipt(foreign, prepared, guard)

    assert commit_layer_replay_receipt(tmp_path, prepared, guard).receipt == replay
    assert (tmp_path / stored.locator).read_bytes() == canonical_layer_replay_receipt_bytes(replay)


def test_replay_preparation_hides_physical_publication_and_policy(
    tmp_path: Path,
) -> None:
    guard, replay, _stored = _active_replay(tmp_path)
    prepared = prepare_layer_replay_receipt(tmp_path, replay, guard)
    for hidden in ("publication", "authority_binding", "existing_binding"):
        with pytest.raises(AttributeError):
            getattr(prepared, hidden)
    with pytest.raises(TypeError, match="dataclass instances"):
        replace(prepared, publication=object())
    discard_layer_replay_receipt(prepared)


def test_evaluation_noop_retains_exact_destination_and_refuses_forged_binding(
    tmp_path: Path,
) -> None:
    guard, replay, stored_replay = _active_replay(tmp_path)
    evaluation = _passing_evaluation(replay, stored_replay)
    first = prepare_layer_evaluation_receipt(tmp_path, evaluation, guard)
    stored_evaluation = commit_layer_evaluation_receipt(tmp_path, first, guard)
    identical = prepare_layer_evaluation_receipt(tmp_path, evaluation, guard)
    assert identical.destination == tmp_path / stored_evaluation.locator
    with pytest.raises(AttributeError):
        _ = identical.publication
    with pytest.raises(AttributeError):
        _ = identical.existing_binding
    with pytest.raises(TypeError, match="dataclass instances"):
        replace(identical, existing_binding=None)
    assert commit_layer_evaluation_receipt(tmp_path, identical, guard).receipt == evaluation

    assert (tmp_path / stored_evaluation.locator).read_bytes() == (canonical_layer_evaluation_receipt_bytes(evaluation))


def test_receipt_success_retires_each_typed_preparation(tmp_path: Path) -> None:
    guard, replay, stored_replay = _active_replay(tmp_path)
    replay_prepared = prepare_layer_replay_receipt(tmp_path, replay, guard)
    assert commit_layer_replay_receipt(
        tmp_path,
        replay_prepared,
        guard,
    ).receipt == replay
    with pytest.raises(LayerReplayReceiptConflict, match="unregistered, expired"):
        _ = replay_prepared.sha256
    with pytest.raises(LayerReplayReceiptConflict, match="unregistered, expired"):
        discard_layer_replay_receipt(replay_prepared)

    evaluation = _passing_evaluation(replay, stored_replay)
    evaluation_prepared = prepare_layer_evaluation_receipt(
        tmp_path,
        evaluation,
        guard,
    )
    assert commit_layer_evaluation_receipt(
        tmp_path,
        evaluation_prepared,
        guard,
    ).receipt == evaluation
    with pytest.raises(
        LayerEvaluationReceiptConflict,
        match="unregistered, expired",
    ):
        _ = evaluation_prepared.sha256
    with pytest.raises(
        LayerEvaluationReceiptConflict,
        match="unregistered, expired",
    ):
        discard_layer_evaluation_receipt(evaluation_prepared)


def test_receipt_preparations_refuse_foreign_thread_access(tmp_path: Path) -> None:
    guard, replay, stored_replay = _active_replay(tmp_path)
    replay_prepared = prepare_layer_replay_receipt(tmp_path, replay, guard)
    evaluation = _passing_evaluation(replay, stored_replay)
    evaluation_prepared = prepare_layer_evaluation_receipt(
        tmp_path,
        evaluation,
        guard,
    )
    failures: list[BaseException] = []

    def read_from_foreign_thread() -> None:
        for prepared in (replay_prepared, evaluation_prepared):
            try:
                _ = prepared.sha256
            except BaseException as exc:  # asserted below
                failures.append(exc)

    worker = Thread(target=read_from_foreign_thread)
    worker.start()
    worker.join(5)

    assert not worker.is_alive()
    assert len(failures) == 2
    assert all("another process or thread" in str(error) for error in failures)
    discard_layer_replay_receipt(replay_prepared)
    discard_layer_evaluation_receipt(evaluation_prepared)


def test_evaluation_commit_refuses_coherent_cross_shot_target(
    tmp_path: Path,
) -> None:
    guard, replay, stored_replay = _active_replay(tmp_path)
    evaluation = _passing_evaluation(replay, stored_replay)
    prepared = prepare_layer_evaluation_receipt(tmp_path, evaluation, guard)
    foreign = tmp_path / "foreign-shot"
    foreign.mkdir()

    try:
        with pytest.raises(
            LayerEvaluationReceiptConflict,
            match="another shot root",
        ):
            commit_layer_evaluation_receipt(foreign, prepared, guard)
        assert not prepared.destination.exists()
    finally:
        discard_layer_evaluation_receipt(prepared)


def test_replay_prepare_refuses_structurally_forged_typed_receipt(
    tmp_path: Path,
) -> None:
    guard, replay, _stored = _active_replay(tmp_path)
    forged = replace(replay, receipt_digest="0" * 64)

    with pytest.raises(
        LayerReplayReceiptConflict,
        match="receipt_digest does not match",
    ):
        prepare_layer_replay_receipt(tmp_path, forged, guard)


def test_evaluation_prepare_refuses_structurally_forged_typed_receipt(
    tmp_path: Path,
) -> None:
    guard, replay, stored_replay = _active_replay(tmp_path)
    evaluation = _passing_evaluation(replay, stored_replay)
    forged = replace(evaluation, final_status="failed")

    with pytest.raises(
        LayerEvaluationReceiptConflict,
        match="final_status is not mechanically derived",
    ):
        prepare_layer_evaluation_receipt(tmp_path, forged, guard)


@pytest.mark.parametrize("mutation", ["mutated", "deleted"])
def test_replay_commit_refuses_causal_source_drift(
    tmp_path: Path,
    mutation: str,
) -> None:
    guard, replay, _stored = _active_replay(tmp_path)
    prepared = prepare_layer_replay_receipt(tmp_path, replay, guard)
    source = tmp_path / replay.observation.points[0].ref
    if mutation == "mutated":
        source.write_bytes(b"changed replay causal source")
    else:
        source.unlink()

    try:
        with pytest.raises(LayerReplayReceiptConflict, match=r"changed|missing|not found"):
            commit_layer_replay_receipt(tmp_path, prepared, guard)
    finally:
        discard_layer_replay_receipt(prepared)


@pytest.mark.parametrize("mutation", ["replay-receipt", "reference"])
def test_evaluation_commit_refuses_causal_source_drift(
    tmp_path: Path,
    mutation: str,
) -> None:
    guard, replay, stored_replay = _active_replay(tmp_path)
    evaluation = _passing_evaluation(replay, stored_replay)
    prepared = prepare_layer_evaluation_receipt(tmp_path, evaluation, guard)
    source = (
        tmp_path / stored_replay.locator
        if mutation == "replay-receipt"
        else tmp_path / replay.observation.points[0].ref
    )
    source.write_bytes(b"changed evaluation causal source")

    try:
        with pytest.raises(LayerEvaluationReceiptConflict, match=r"changed|digest"):
            commit_layer_evaluation_receipt(tmp_path, prepared, guard)
    finally:
        discard_layer_evaluation_receipt(prepared)


def test_replay_post_commit_readback_failure_remains_cleanup_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard, replay, stored = _active_replay(tmp_path)
    destination = tmp_path / stored.locator
    destination.unlink()
    prepared = prepare_layer_replay_receipt(tmp_path, replay, guard)
    failure = RuntimeError("injected replay readback failure")

    def fail_readback(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(
        replay_receipt_publication,
        "load_layer_replay_receipt",
        fail_readback,
    )

    with pytest.raises(RuntimeError, match="injected replay readback") as observed:
        commit_layer_replay_receipt(tmp_path, prepared, guard)

    assert observed.value is failure
    assert destination.read_bytes() == canonical_layer_replay_receipt_bytes(replay)
    discard_layer_replay_receipt(prepared)


def test_evaluation_post_commit_readback_failure_remains_cleanup_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard, replay, stored_replay = _active_replay(tmp_path)
    evaluation = _passing_evaluation(replay, stored_replay)
    prepared = prepare_layer_evaluation_receipt(tmp_path, evaluation, guard)
    destination = prepared.destination
    failure = RuntimeError("injected evaluation readback failure")

    def fail_readback(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(
        evaluation_receipt_publication,
        "load_layer_evaluation_receipt",
        fail_readback,
    )

    with pytest.raises(
        RuntimeError,
        match="injected evaluation readback",
    ) as observed:
        commit_layer_evaluation_receipt(tmp_path, prepared, guard)

    assert observed.value is failure
    assert destination.read_bytes() == canonical_layer_evaluation_receipt_bytes(
        evaluation
    )
    discard_layer_evaluation_receipt(prepared)


def test_replay_noop_acquires_one_writer_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard, replay, _stored = _active_replay(tmp_path)
    prepared = prepare_layer_replay_receipt(tmp_path, replay, guard)
    publish_prepared = guard.publish_prepared
    calls = 0

    def counted_publish(_self, operation, transaction_binding, mutation):
        nonlocal calls
        calls += 1
        assert transaction_binding is prepared
        return publish_prepared(operation, transaction_binding, mutation)

    monkeypatch.setattr(type(guard), "publish_prepared", counted_publish)
    stored = commit_layer_replay_receipt(
        tmp_path,
        prepared,
        guard,
    )

    assert stored.receipt == replay
    assert calls == 1


def test_evaluation_noop_acquires_one_writer_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard, replay, stored_replay = _active_replay(tmp_path)
    evaluation = _passing_evaluation(replay, stored_replay)
    prepared = prepare_layer_evaluation_receipt(tmp_path, evaluation, guard)
    commit_layer_evaluation_receipt(tmp_path, prepared, guard)
    identical = prepare_layer_evaluation_receipt(tmp_path, evaluation, guard)
    publish_prepared = guard.publish_prepared
    calls = 0

    def counted_publish(_self, operation, transaction_binding, mutation):
        nonlocal calls
        calls += 1
        assert transaction_binding is identical
        return publish_prepared(operation, transaction_binding, mutation)

    monkeypatch.setattr(type(guard), "publish_prepared", counted_publish)
    stored = commit_layer_evaluation_receipt(
        tmp_path,
        identical,
        guard,
    )

    assert stored.receipt == evaluation
    assert calls == 1


def test_replay_noop_refuses_guard_that_skips_prepared_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard, replay, stored = _active_replay(tmp_path)
    prepared = prepare_layer_replay_receipt(tmp_path, replay, guard)
    destination = tmp_path / stored.locator
    original = destination.read_bytes()

    def skip_prepared_mutation(
        _self,
        _operation,
        transaction_binding,
        _mutation,
    ) -> None:
        assert transaction_binding is prepared

    monkeypatch.setattr(
        type(guard),
        "publish_prepared",
        skip_prepared_mutation,
    )

    with pytest.raises(
        LayerReplayReceiptConflict,
        match="returned without completing its exact prepared mutation",
    ):
        commit_layer_replay_receipt(tmp_path, prepared, guard)

    assert destination.read_bytes() == original
    discard_layer_replay_receipt(prepared)


def test_evaluation_noop_refuses_guard_that_skips_prepared_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard, replay, stored_replay = _active_replay(tmp_path)
    evaluation = _passing_evaluation(replay, stored_replay)
    initial = prepare_layer_evaluation_receipt(tmp_path, evaluation, guard)
    stored = commit_layer_evaluation_receipt(tmp_path, initial, guard)
    prepared = prepare_layer_evaluation_receipt(tmp_path, evaluation, guard)
    destination = tmp_path / stored.locator
    original = destination.read_bytes()

    def skip_prepared_mutation(
        _self,
        _operation,
        transaction_binding,
        _mutation,
    ) -> None:
        assert transaction_binding is prepared

    monkeypatch.setattr(
        type(guard),
        "publish_prepared",
        skip_prepared_mutation,
    )

    with pytest.raises(
        LayerEvaluationReceiptConflict,
        match="returned without completing its exact prepared mutation",
    ):
        commit_layer_evaluation_receipt(tmp_path, prepared, guard)

    assert destination.read_bytes() == original
    discard_layer_evaluation_receipt(prepared)


def test_replay_final_sink_refuses_a_foreign_writer_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard, replay, _stored = _active_replay(tmp_path)
    prepared = prepare_layer_replay_receipt(tmp_path, replay, guard)
    foreign = tmp_path / "foreign-shot"
    foreign.mkdir()

    def inject_foreign_capability(
        _self,
        _operation,
        transaction_binding,
        mutation,
    ):
        assert transaction_binding is prepared
        with shot_authority_capture.shot_authority_writer_fence(foreign) as capability:
            return mutation(capability)

    monkeypatch.setattr(type(guard), "publish_prepared", inject_foreign_capability)

    try:
        with pytest.raises(
            LayerReplayReceiptConflict,
            match="exact typed finalization authorization",
        ):
            commit_layer_replay_receipt(tmp_path, prepared, guard)
    finally:
        discard_layer_replay_receipt(prepared)


def test_evaluation_final_sink_refuses_a_foreign_writer_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard, replay, stored_replay = _active_replay(tmp_path)
    evaluation = _passing_evaluation(replay, stored_replay)
    prepared = prepare_layer_evaluation_receipt(tmp_path, evaluation, guard)
    foreign = tmp_path / "foreign-shot"
    foreign.mkdir()

    def inject_foreign_capability(
        _self,
        _operation,
        transaction_binding,
        mutation,
    ):
        assert transaction_binding is prepared
        with shot_authority_capture.shot_authority_writer_fence(foreign) as capability:
            return mutation(capability)

    monkeypatch.setattr(type(guard), "publish_prepared", inject_foreign_capability)

    try:
        with pytest.raises(
            LayerEvaluationReceiptConflict,
            match="exact typed finalization authorization",
        ):
            commit_layer_evaluation_receipt(
                tmp_path,
                prepared,
                guard,
            )
    finally:
        discard_layer_evaluation_receipt(prepared)


def test_replay_noop_existing_binding_audit_is_opaque(
    tmp_path: Path,
) -> None:
    guard, replay, _stored = _active_replay(tmp_path)
    prepared = prepare_layer_replay_receipt(tmp_path, replay, guard)
    with pytest.raises(AttributeError):
        _ = prepared.existing_binding
    with pytest.raises(TypeError, match="dataclass instances"):
        replace(prepared, existing_binding=None)
    discard_layer_replay_receipt(prepared)


def test_evaluation_noop_existing_binding_audit_is_opaque(
    tmp_path: Path,
) -> None:
    guard, replay, stored_replay = _active_replay(tmp_path)
    evaluation = _passing_evaluation(replay, stored_replay)
    initial = prepare_layer_evaluation_receipt(tmp_path, evaluation, guard)
    commit_layer_evaluation_receipt(tmp_path, initial, guard)
    prepared = prepare_layer_evaluation_receipt(tmp_path, evaluation, guard)
    with pytest.raises(AttributeError):
        _ = prepared.existing_binding
    with pytest.raises(TypeError, match="dataclass instances"):
        replace(prepared, existing_binding=None)
    discard_layer_evaluation_receipt(prepared)


def test_replay_noop_reopens_bytes_before_writer_acquisition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard, replay, stored = _active_replay(tmp_path)
    prepared = prepare_layer_replay_receipt(tmp_path, replay, guard)
    (tmp_path / stored.locator).write_bytes(b"changed replay receipt bytes\n")

    def unexpected_publish(*_args, **_kwargs):
        raise AssertionError("changed no-op reached writer acquisition")

    monkeypatch.setattr(type(guard), "publish_prepared", unexpected_publish)

    with pytest.raises(
        LayerReplayReceiptConflict,
        match="file digest changed",
    ):
        commit_layer_replay_receipt(tmp_path, prepared, guard)


def test_evaluation_noop_reopens_bytes_before_writer_acquisition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard, replay, stored_replay = _active_replay(tmp_path)
    evaluation = _passing_evaluation(replay, stored_replay)
    initial = prepare_layer_evaluation_receipt(tmp_path, evaluation, guard)
    stored = commit_layer_evaluation_receipt(tmp_path, initial, guard)
    prepared = prepare_layer_evaluation_receipt(tmp_path, evaluation, guard)
    (tmp_path / stored.locator).write_bytes(b"changed evaluation receipt bytes\n")

    def unexpected_publish(*_args, **_kwargs):
        raise AssertionError("changed no-op reached writer acquisition")

    monkeypatch.setattr(type(guard), "publish_prepared", unexpected_publish)

    with pytest.raises(
        LayerEvaluationReceiptConflict,
        match="file digest changed",
    ):
        commit_layer_evaluation_receipt(tmp_path, prepared, guard)


def test_replay_noop_rechecks_rederived_binding_under_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard, replay, stored = _active_replay(tmp_path)
    prepared = prepare_layer_replay_receipt(tmp_path, replay, guard)
    destination = tmp_path / stored.locator
    replacement = destination.with_name("same-replay-bytes.json")
    replacement.write_bytes(destination.read_bytes())

    publish_prepared = guard.publish_prepared

    def rebind_before_publish(_self, operation, transaction_binding, mutation):
        assert transaction_binding is prepared

        def rebind_then_publish(capability):
            replacement.replace(destination)
            return mutation(capability)

        return publish_prepared(
            operation,
            transaction_binding,
            rebind_then_publish,
        )

    monkeypatch.setattr(type(guard), "publish_prepared", rebind_before_publish)

    with pytest.raises(
        LayerReplayReceiptConflict,
        match=r"trusted (?:path|file).*changed|rebound",
    ):
        commit_layer_replay_receipt(tmp_path, prepared, guard)


def test_evaluation_noop_rechecks_rederived_binding_under_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard, replay, stored_replay = _active_replay(tmp_path)
    evaluation = _passing_evaluation(replay, stored_replay)
    initial = prepare_layer_evaluation_receipt(tmp_path, evaluation, guard)
    stored = commit_layer_evaluation_receipt(tmp_path, initial, guard)
    prepared = prepare_layer_evaluation_receipt(tmp_path, evaluation, guard)
    destination = tmp_path / stored.locator
    replacement = destination.with_name("same-evaluation-bytes.json")
    replacement.write_bytes(destination.read_bytes())

    publish_prepared = guard.publish_prepared

    def rebind_before_publish(_self, operation, transaction_binding, mutation):
        assert transaction_binding is prepared

        def rebind_then_publish(capability):
            replacement.replace(destination)
            return mutation(capability)

        return publish_prepared(
            operation,
            transaction_binding,
            rebind_then_publish,
        )

    monkeypatch.setattr(type(guard), "publish_prepared", rebind_before_publish)

    with pytest.raises(
        LayerEvaluationReceiptConflict,
        match=r"trusted (?:path|file).*changed|rebound",
    ):
        commit_layer_evaluation_receipt(tmp_path, prepared, guard)


def test_replay_replacement_requires_staged_payload_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard, replay, stored = _active_replay(tmp_path)
    (tmp_path / stored.locator).unlink()
    prepared = prepare_layer_replay_receipt(tmp_path, replay, guard)
    monkeypatch.setattr(
        replay_receipt_publication,
        "verify_prepared_file_payload",
        lambda *_args, **_kwargs: None,
    )

    try:
        with pytest.raises(
            LayerReplayReceiptConflict,
            match="requires held staged-payload verification",
        ):
            commit_layer_replay_receipt(tmp_path, prepared, guard)
    finally:
        discard_layer_replay_receipt(prepared)


def test_evaluation_replacement_requires_staged_payload_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard, replay, stored_replay = _active_replay(tmp_path)
    evaluation = _passing_evaluation(replay, stored_replay)
    prepared = prepare_layer_evaluation_receipt(tmp_path, evaluation, guard)
    monkeypatch.setattr(
        evaluation_receipt_publication,
        "verify_prepared_file_payload",
        lambda *_args, **_kwargs: None,
    )

    try:
        with pytest.raises(
            LayerEvaluationReceiptConflict,
            match="requires held staged-payload verification",
        ):
            commit_layer_evaluation_receipt(tmp_path, prepared, guard)
    finally:
        discard_layer_evaluation_receipt(prepared)
