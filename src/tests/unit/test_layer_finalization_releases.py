"""Closed contracts for reviewed pre-terminal finalization release."""

from __future__ import annotations

import hashlib
import json

import pytest

from vfx_harness.domain.authority_head_records import (
    AuthoritySelectionTokenProjection,
)
from vfx_harness.domain.layer_finalization_releases import (
    UNSEALED_JUDGMENT_DISPOSITION,
    LayerFinalizationReleaseEvidence,
    LayerFinalizationReleaseReceipt,
    LayerFinalizationReleaseReference,
    LayerFinalizationReleaseRequest,
    canonical_layer_finalization_release_receipt_bytes,
    layer_finalization_release_claim_evidence,
    layer_finalization_review_evidence_locator,
)
from vfx_harness.domain.layer_finalizations import (
    LAYER_REPLAY_RECEIPT_SCHEMA,
    LayerFinalizationClaim,
    LayerFinalizationUnitInput,
)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _claim() -> LayerFinalizationClaim:
    return LayerFinalizationClaim.mint(
        attempt_revision=2,
        run_id="20260901T120000Z-release",
        layer_id="form",
        mode="singleton_passthrough",
        selection_token=AuthoritySelectionTokenProjection(
            3,
            _digest("plan pointer"),
            4,
            _digest("jit pointer"),
        ),
        plan_hash=_digest("form capsule"),
        layer_script_path="build/form.py",
        layer_script_sha256=_digest("form script"),
        unit_inputs=(
            LayerFinalizationUnitInput.mint(
                unit_id="form-unit",
                unit_digest=_digest("form unit"),
                completion_receipt_digest=_digest("form completion"),
                script_path="build/units/form/form-unit.py",
                script_sha256=_digest("form unit script"),
            ),
        ),
        predecessor_inputs=(),
        claimed_at="2026-09-01T12:00:00+00:00",
    )


def _review(locator: str) -> LayerFinalizationReleaseEvidence:
    sha256 = _digest(locator)
    return LayerFinalizationReleaseEvidence(
        kind="review_evidence",
        locator=layer_finalization_review_evidence_locator(sha256),
        sha256=sha256,
        source_locator=locator,
    )


def _request() -> LayerFinalizationReleaseRequest:
    return LayerFinalizationReleaseRequest.mint(
        claim=_claim(),
        reason="review proved the builder process exited",
        review_evidence=(
            _review("runs/release/evidence/process.json"),
            _review("runs/release/evidence/review.json"),
        ),
        replay_receipts=(
            LayerFinalizationReleaseEvidence(
                kind="layer_replay_receipt",
                locator=(
                    "runs/release/checkpoints/layer-finalizations/"
                    "replay.group-0.replay.json"
                ),
                sha256=_digest("replay bytes"),
                record_schema=LAYER_REPLAY_RECEIPT_SCHEMA,
                record_digest=_digest("replay receipt"),
                group_index=0,
                planned_group_count=1,
            ),
        ),
    )


def test_release_receipt_round_trip_is_closed_and_marks_judgment_unsealed() -> None:
    request = _request()
    receipt = LayerFinalizationReleaseReceipt.mint(
        request=request,
        released_at="2026-09-01T12:05:00+00:00",
    )
    payload = json.loads(canonical_layer_finalization_release_receipt_bytes(receipt))

    assert LayerFinalizationReleaseReceipt.from_dict(payload) == receipt
    assert request.release_id == f"lfr-{request.request_digest}"
    assert request.claim == _claim()
    assert request.judgment_disposition == UNSEALED_JUDGMENT_DISPOSITION


def test_release_request_requires_sorted_nonempty_file_evidence() -> None:
    with pytest.raises(ValueError, match="sorted by source locator"):
        LayerFinalizationReleaseRequest.mint(
            claim=_claim(),
            reason="reviewed release",
            review_evidence=(
                _review("runs/release/evidence/z.json"),
                _review("runs/release/evidence/a.json"),
            ),
            replay_receipts=(),
        )

    with pytest.raises(ValueError, match="non-empty typed review evidence"):
        LayerFinalizationReleaseRequest.mint(
            claim=_claim(),
            reason="reviewed release",
            review_evidence=(),
            replay_receipts=(),
        )


def test_release_receipt_rejects_tampered_request_and_receipt_identity() -> None:
    receipt = LayerFinalizationReleaseReceipt.mint(
        request=_request(),
        released_at="2026-09-01T12:05:00+00:00",
    )
    payload = receipt.as_dict()
    payload["request"]["reason"] = "different review"
    with pytest.raises(ValueError, match="release identity is stale"):
        LayerFinalizationReleaseReceipt.from_dict(payload)

    payload = receipt.as_dict()
    payload["receipt_digest"] = _digest("tampered receipt")
    with pytest.raises(ValueError, match="does not match its payload"):
        LayerFinalizationReleaseReceipt.from_dict(payload)


def test_release_reference_round_trips_the_exact_archive_evidence() -> None:
    receipt = LayerFinalizationReleaseReceipt.mint(
        request=_request(),
        released_at="2026-09-01T12:05:00+00:00",
    )
    reference = LayerFinalizationReleaseReference(
        locator=f"state/layer-finalization-releases/{receipt.request.release_id}.json",
        sha256=_digest("release bytes"),
        receipt_digest=receipt.receipt_digest,
    )

    assert (
        LayerFinalizationReleaseReference.from_archive_evidence(
            reference.as_archive_evidence()
        )
        == reference
    )
    evidence = layer_finalization_release_claim_evidence(receipt, reference)
    assert evidence[0] == reference.as_archive_evidence()
    assert evidence[1].startswith("layer-replay-receipt:0:")
    assert evidence[2:] == tuple(
        f"review-evidence:{row.sha256}:{row.locator}"
        for row in receipt.request.review_evidence
    )


def test_release_evidence_refuses_a_shot_root_locator() -> None:
    with pytest.raises(ValueError, match="canonical shot-relative path"):
        LayerFinalizationReleaseEvidence(
            kind="review_evidence",
            locator=layer_finalization_review_evidence_locator(_digest("root")),
            sha256=_digest("root"),
            source_locator=".",
        )


def test_release_request_requires_an_exact_contiguous_replay_prefix() -> None:
    replay = _request().replay_receipts[0]
    skipped = LayerFinalizationReleaseEvidence(
        kind="layer_replay_receipt",
        locator="runs/release/checkpoints/layer-finalizations/group-1.json",
        sha256=_digest("replay one bytes"),
        record_schema=LAYER_REPLAY_RECEIPT_SCHEMA,
        record_digest=_digest("replay one receipt"),
        group_index=1,
        planned_group_count=2,
    )

    with pytest.raises(ValueError, match="contiguous prefix"):
        LayerFinalizationReleaseRequest.mint(
            claim=_claim(),
            reason="reviewed release",
            review_evidence=(_review("runs/release/evidence/review.json"),),
            replay_receipts=(skipped,),
        )
    with pytest.raises(ValueError, match="disagree on planned group count"):
        LayerFinalizationReleaseRequest.mint(
            claim=_claim(),
            reason="reviewed release",
            review_evidence=(_review("runs/release/evidence/review.json"),),
            replay_receipts=(replay, skipped),
        )
