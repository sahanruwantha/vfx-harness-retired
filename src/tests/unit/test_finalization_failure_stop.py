"""A failed finalization stops on its own receipt, and only when a prerequisite is missing.

HIR-0247 made the refusal describe what failed. This covers the half that decides what
that description *authorizes*: a judgment that could not be made is a human decision on
authority, and a judgment that was made and scored badly is the layer failing on its
merits. Compiling a stop for the second would invent dispatch authority no producer
proves, so these tests pin the distinction from both sides (HIR-0248).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from vfx_harness.agents.builder.cli import _layer_verdict_exit
from vfx_harness.agents.builder.finalization_stops import (
    FINALIZATION_STOP_EVIDENCE_SCHEMA,
    answer_ids_for,
    compile_finalization_failure_stop,
    judged_failures,
    unresolved_prerequisites,
)
from vfx_harness.agents.builder.models import LayerVerdictFailed
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration.layer_publication import LayerFinalizationNotPassed

DIGEST_A = hashlib.sha256(b"bundle").hexdigest()
DIGEST_B = hashlib.sha256(b"view").hexdigest()


def _row(frame: int, decided_by: str, *, passed: bool) -> dict:
    return {"frame": frame, "verdict": {"pass": passed, "decided_by": decided_by}}


def _receipt(rows: list[dict], *, status: str = "failed") -> dict:
    """A finalization receipt shaped like the one hansa_silk_road layer 2 actually minted."""
    return {
        "schema": "vfx-harness.layer-finalization-receipt/v2",
        "final_status": status,
        "canonical": rows,
        "evaluation_groups": [
            {
                "group_index": 2,
                "canonical_start": 0,
                "canonical_end": len(rows),
                "debt_id": "jd-06fa28eac2ac05f79",
                "requirement_ids": ["R50"],
            }
        ],
    }


def _state(tmp_path: Path, receipt: dict) -> Path:
    path = tmp_path / "layer_2.json"
    path.write_text(
        json.dumps({"layer_finalization": {"terminal_receipt": receipt}}), encoding="utf-8"
    )
    return path


def _compile(tmp_path: Path, receipt: dict, *, source: Path | None = None):
    layout = run_artifacts.create(tmp_path / "shot", "probe")
    return compile_finalization_failure_stop(
        layout,
        layer_id="2",
        bundle_digest=DIGEST_A,
        view_digest=DIGEST_B,
        source_path=source or _state(tmp_path, receipt),
        receipt=receipt,
    )


def test_unavailable_evidence_stops_as_a_human_decision(tmp_path):
    receipt = _receipt(
        [_row(frame, "no_optical_signal", passed=False) for frame in (1, 51, 151)]
    )
    envelope = _compile(tmp_path, receipt)

    assert envelope is not None
    assert envelope.stop_class == "human_decision_required"
    assert envelope.stage == "composition"
    # The owner is nameable from the receipt alone: the layer, the requirement the group
    # was paying, and the debt it was paying it against.
    assert envelope.cause.owner_scope_ids == (
        "debt:jd-06fa28eac2ac05f79",
        "layer:2",
        "requirement:R50",
    )
    assert "f1, f51, f151" in envelope.next_action
    target = envelope.actions[0].target
    assert set(target.allowed_answer_ids) == {
        "add_the_missing_provider_before_this_layer",
        "move_judgment_to_a_layer_that_can_produce_it",
        "withdraw_the_judgment",
    }


def test_a_negative_critic_verdict_compiles_no_stop(tmp_path):
    """The layer was judged and did not pass. That is the work, not a prerequisite."""
    receipt = _receipt([_row(frame, "critic", passed=False) for frame in (1, 51, 151)])

    assert unresolved_prerequisites(receipt["canonical"], receipt["evaluation_groups"]) == ()
    assert _compile(tmp_path, receipt) is None


def test_failed_executable_evidence_compiles_no_stop(tmp_path):
    """Measured and failed is also work: the frame was judged on evidence that existed."""
    receipt = _receipt([_row(1, "unit_executable_evidence", passed=False)])

    assert _compile(tmp_path, receipt) is None


def test_a_contract_gap_offers_the_remedy_a_contract_gap_actually_has(tmp_path):
    """An uncovered judge frame is repaired by authoring a claim, not by adding a provider.

    The layer *can* produce the evidence; nobody asked for it. Offering "add the missing
    provider before this layer" there hands the operator three options that all miss, and a
    remedy set that does not contain the remedy is a misclassification wearing the right
    words.
    """
    receipt = _receipt([_row(1, "uncovered_judge_frame", passed=False)])
    envelope = _compile(tmp_path, receipt)

    assert envelope is not None
    assert set(envelope.actions[0].target.allowed_answer_ids) == {
        "author_a_required_claim_covering_the_frame",
        "escalate_a_vocabulary_gap_and_close_the_requirement_by_decision",
        "withdraw_the_judgment",
    }
    assert "add the missing provider" not in envelope.next_action
    assert "author a required claim" in envelope.next_action


def test_answers_are_derived_per_decider_and_a_mixed_receipt_offers_both():
    assert answer_ids_for(["no_optical_signal"]) == (
        "add_the_missing_provider_before_this_layer",
        "move_judgment_to_a_layer_that_can_produce_it",
        "withdraw_the_judgment",
    )
    both = answer_ids_for(["no_optical_signal", "uncovered_judge_frame"])
    assert "author_a_required_claim_covering_the_frame" in both
    assert "add_the_missing_provider_before_this_layer" in both


def test_a_contract_gap_is_a_missing_prerequisite(tmp_path):
    """An uncovered judge frame could not be settled at all, whatever the work looked like."""
    receipt = _receipt([_row(1, "uncovered_judge_frame", passed=False)])
    envelope = _compile(tmp_path, receipt)

    assert envelope is not None
    assert envelope.stop_class == "human_decision_required"


def test_a_mixed_failure_stops_on_the_prerequisite_half(tmp_path):
    receipt = _receipt(
        [
            _row(1, "critic", passed=False),
            _row(51, "no_optical_signal", passed=False),
            _row(101, "critic", passed=True),
        ]
    )
    rows = unresolved_prerequisites(receipt["canonical"], receipt["evaluation_groups"])

    assert [row["frame"] for row in rows] == [51]
    envelope = _compile(tmp_path, receipt)
    assert envelope is not None
    # A layer can fail one group on a plate it could not produce and another on a judgment
    # that was made. Naming only the first tells the operator that resolving the
    # prerequisite accepts the layer, and it does not.
    assert [row["frame"] for row in judged_failures(receipt["canonical"])] == [1]
    assert "not the layer's only blocker" in envelope.next_action
    assert "f1" in envelope.next_action.split("only blocker")[1]


def test_a_pure_prerequisite_failure_claims_no_other_blocker(tmp_path):
    receipt = _receipt([_row(1, "no_optical_signal", passed=False)])
    envelope = _compile(tmp_path, receipt)

    assert envelope is not None
    assert "only blocker" not in envelope.next_action


def test_a_passed_receipt_compiles_no_stop(tmp_path):
    receipt = _receipt([_row(1, "critic", passed=True)], status="passed")

    assert _compile(tmp_path, receipt) is None


def test_the_stop_refuses_a_receipt_that_changed_under_it(tmp_path):
    """Source-backed means source-backed: the envelope is compiled from bytes on disk."""
    receipt = _receipt([_row(1, "no_optical_signal", passed=False)])
    source = _state(tmp_path, receipt)
    source.write_text(
        json.dumps(
            {"layer_finalization": {"terminal_receipt": _receipt([_row(1, "critic", passed=True)])}}
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="changed while its typed stop"):
        _compile(tmp_path, receipt, source=source)


def test_the_stop_publishes_evidence_that_reads_back(tmp_path):
    receipt = _receipt([_row(1, "no_optical_signal", passed=False)])
    layout = run_artifacts.create(tmp_path / "shot", "probe")
    envelope = compile_finalization_failure_stop(
        layout,
        layer_id="2",
        bundle_digest=DIGEST_A,
        view_digest=DIGEST_B,
        source_path=_state(tmp_path, receipt),
        receipt=receipt,
    )

    assert envelope is not None
    ref = envelope.evidence_refs[0]
    published = (tmp_path / "shot" / ref.locator).read_bytes()
    assert hashlib.sha256(published).hexdigest() == ref.sha256
    document = json.loads(published)
    assert document["schema"] == FINALIZATION_STOP_EVIDENCE_SCHEMA
    assert document["receipt"]["final_status"] == "failed"
    assert [row["frame"] for row in document["unresolved"]] == [1]


def _failure(tmp_path: Path, receipt: dict) -> LayerVerdictFailed:
    conflict = LayerFinalizationNotPassed(
        "layer 2 terminal finalization is 'failed', not 'passed'",
        layer_id="2",
        receipt=SimpleNamespace(as_dict=lambda: receipt),
        source_path=_state(tmp_path, receipt),
    )
    return LayerVerdictFailed("layer 2 verdict", finalization=conflict)


def _selected():
    return SimpleNamespace(
        assertion=SimpleNamespace(
            bundle=SimpleNamespace(digest=DIGEST_A),
            effective_view=SimpleNamespace(digest=DIGEST_B),
        )
    )


def test_the_boundary_exits_with_a_typed_stop_for_a_missing_prerequisite(tmp_path):
    shot = SimpleNamespace(folder=str(tmp_path / "shot"))
    run_artifacts.create(Path(shot.folder), "probe")
    receipt = _receipt([_row(1, "no_optical_signal", passed=False)])

    exit_value = _layer_verdict_exit(shot, _selected(), _failure(tmp_path, receipt))

    assert isinstance(exit_value, run_artifacts.TypedStop)
    assert exit_value.code == 9
    assert exit_value.stop_envelope.stop_class == "human_decision_required"
    assert exit_value.terminal_cause == "typed_stop_selected"


def test_the_boundary_exits_plainly_for_a_negative_verdict(tmp_path):
    """No envelope here, and the absence is the point: nothing is dispatchable."""
    shot = SimpleNamespace(folder=str(tmp_path / "shot"))
    run_artifacts.create(Path(shot.folder), "probe")
    receipt = _receipt([_row(1, "critic", passed=False)])

    exit_value = _layer_verdict_exit(shot, _selected(), _failure(tmp_path, receipt))

    assert isinstance(exit_value, run_artifacts.RequestedExit)
    assert not isinstance(exit_value, run_artifacts.TypedStop)
    assert exit_value.terminal_cause == "gate_rejected"


def test_only_a_dispatchable_transaction_may_spend_a_cause_fingerprint():
    """A human escalation must not burn the fingerprint a real dispatch would need later.

    That holds today only because `PriorDispatchAttempt` is constructed on one code path.
    It is asserted rather than described, because the invariant is invisible at the line
    that depends on it and a second caller would remove it silently.
    """
    from vfx_harness.application import run_controller

    source = Path(run_controller.__file__).read_text(encoding="utf-8")
    guard = "controller published a dispatch attempt for"
    assert source.count("PriorDispatchAttempt(") == 1, (
        "a second construction site needs its own guard, or this invariant is gone"
    )
    before, _, after = source.partition("PriorDispatchAttempt(")
    assert guard in before and guard not in after, (
        "the dispatchability guard must run before the attempt is constructed"
    )
    assert frozenset(
        {"publish_validated_amendment"}
    ) == run_controller.DISPATCHABLE_TRANSACTIONS
