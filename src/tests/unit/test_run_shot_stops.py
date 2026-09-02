"""The whole-shot driver consumes typed child stops, never exit-code meaning."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.run_owner_support import fail_run, owned_run
from vfx_harness.application import run_shot
from vfx_harness.application.inspect_run import collect
from vfx_harness.domain.stop_envelope_primitives import canonical_digest
from vfx_harness.domain.stop_envelopes import StopCause, StopEnvelope, StopIdentity
from vfx_harness.domain.stop_transaction_state import (
    EvidenceRecordAssertion,
    StopEvidenceRef,
)
from vfx_harness.domain.stop_transactions import (
    EscalateQuestionTarget,
    HumanDecisionCommitted,
    StopAction,
)
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration import run_owner_boundary


def test_passed_layer_skip_requires_current_terminal_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layer = SimpleNamespace(
        id="1",
        script="build/01_camera.py",
        stages=(SimpleNamespace(id="camera"), SimpleNamespace(id="form")),
    )
    shot = SimpleNamespace(folder=tmp_path)
    selected = object()

    monkeypatch.setattr(
        run_shot.layer_publication,
        "require_current_layer_publication",
        lambda *_args: (_ for _ in ()).throw(
            run_shot.layer_publication.LayerPublicationConflict(
                "terminal receipt is missing"
            )
        ),
    )

    assert run_shot._receipt_backed_passed_layers(
        shot,
        {"1": layer},
        selected,
    ) == set()

    calls = []

    def current_publication(folder, candidate, authority):
        calls.append((folder, candidate, authority))
        return SimpleNamespace(
            receipt=SimpleNamespace(layer_script_path=candidate.script),
        )

    monkeypatch.setattr(
        run_shot.layer_publication,
        "require_current_layer_publication",
        current_publication,
    )

    assert run_shot._receipt_backed_passed_layers(
        shot,
        {"1": layer},
        selected,
    ) == {"1"}
    assert calls == [(tmp_path, layer, selected)]


def test_selected_run_layers_preserves_selected_dag_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shot = SimpleNamespace(folder=tmp_path)
    selected = object()
    camera = SimpleNamespace(id="camera")
    form = SimpleNamespace(id="form")
    composite = SimpleNamespace(id="composite")
    observed = []
    monkeypatch.setattr(
        run_shot.authority_selection,
        "resolve_selected_authority",
        lambda folder: selected if folder == tmp_path else pytest.fail("wrong shot"),
    )

    def selected_chain(candidate_shot, *, selected_authority):
        observed.append((candidate_shot, selected_authority))
        return (camera, form, composite)

    monkeypatch.setattr(run_shot, "selected_layer_chain", selected_chain)

    authority, chain, layers = run_shot._selected_run_layers(shot)

    assert authority is selected
    assert chain == (camera, form, composite)
    assert list(layers) == ["camera", "form", "composite"]
    assert observed == [(shot, selected)]


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _child_stop(layout: run_artifacts.RunLayout) -> StopEnvelope:
    run_id = layout.run_id
    facts = _digest("acceptance facts")
    question_digest = _digest("acceptance question")
    allowed_answer_ids = ("abstain", "review_unit:L1:form")
    evidence_document = {
        "schema": "vfx-harness.acceptance-question/v1",
        "question_digest": question_digest,
        "allowed_answer_ids": list(allowed_answer_ids),
    }
    evidence_path = layout.write_report("acceptance-question", evidence_document)
    evidence = StopEvidenceRef(
        kind="stop_evidence",
        locator=evidence_path.relative_to(layout.shot).as_posix(),
        sha256=hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
        record_schema="vfx-harness.acceptance-question/v1",
        record_digest=canonical_digest(evidence_document),
    )
    question = EvidenceRecordAssertion(
        record_kind="question",
        record_id="acceptance-question-M1",
        evidence=evidence,
    )
    target = EscalateQuestionTarget(
        question_record=question,
        question_digest=question_digest,
        decision_authority_id="acceptance-review",
        decision_schema="vfx-harness.acceptance-decision/v1",
        allowed_answer_ids=allowed_answer_ids,
        evidence=(evidence,),
    )
    action = StopAction(
        target=target,
        postcondition=HumanDecisionCommitted(
            question_digest=question_digest,
            decision_authority_id=target.decision_authority_id,
            decision_schema=target.decision_schema,
            allowed_answer_ids=allowed_answer_ids,
        ),
    )
    return StopEnvelope(
        stage="acceptance",
        stop_class="human_decision_required",
        identity=StopIdentity(
            run_id=run_id,
            bundle_digest=_digest("bundle"),
            view_digest=_digest("view"),
            layer_id=None,
            unit_id=None,
            unit_plan_digest=None,
            unit_digest=None,
            candidate_digest=_digest("candidate"),
            checkpoint_digest=None,
            settings_digest=_digest("settings"),
            debt_state_digest=_digest("debt"),
        ),
        cause=StopCause(
            invariant_id="acceptance_moments_failed",
            finding_ids=("moment-M1",),
            owner_scope_ids=("acceptance",),
            normalized_facts_digest=facts,
        ),
        attempt_evidence_digest=_digest("attempt"),
        classification_evidence_digest=_digest("classification"),
        artifact_state_digest=_digest("artifact"),
        authoritative_before_digest=_digest("before"),
        actions=(action,),
        evidence_refs=(evidence,),
        budget_key="acceptance-decision",
        expected="Every required acceptance moment passes.",
        found="Acceptance moment M1 failed.",
        next_action="Resolve its exact repair authority.",
    )


def test_driver_preserves_child_selected_stop_envelope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    with owned_run(tmp_path, "driver-child-stop", command="run", dispatch_kind="driver") as (layout, lease):
        envelope = _child_stop(layout)
        layout.write_stop_envelope(envelope)

        with pytest.raises(SystemExit) as raised:
            run_shot._stop_after_stage(layout, lease, 9, "acceptance")

    assert raised.value.code == 9
    status = json.loads(layout.status.read_text(encoding="utf-8"))
    assert status["state"] == "failed"
    assert status["stop_envelope_digest"] == envelope.digest
    summary = json.loads((layout.reports / "summary.json").read_text(encoding="utf-8"))
    assert summary["stop_class"] == "human_decision_required"
    assert layout.read_terminal_stop() == envelope
    digest = collect(tmp_path, run_id=layout.run_id)
    assert digest["run"]["stop"]["stop_class"] == "human_decision_required"
    assert digest["run"]["stop"]["legal_transactions"] == ["escalate_question"]
    assert digest["run"]["stop"]["legal_actions"][0]["dispatch_mode"] == "human_handoff"
    assert digest["run"]["stop"]["evidence_refs"][0]["record_schema"] == ("vfx-harness.acceptance-question/v1")


def test_inspect_run_marks_a_stop_with_tampered_evidence_invalid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    with owned_run(tmp_path, "inspect-tampered-stop", command="run", dispatch_kind="driver") as (layout, lease):
        envelope = _child_stop(layout)
        fail_run(layout, lease, envelope, exit_code=9)
    evidence_path = layout.shot / envelope.evidence_refs[0].locator
    evidence_path.write_text('{"schema":"vfx-harness.acceptance-question/v1"}\n')

    digest = collect(tmp_path, run_id=layout.run_id)

    assert digest["run"]["stop"]["valid"] is False
    assert "SHA-256 mismatch" in digest["run"]["stop"]["error"]


def test_driver_does_not_infer_recovery_from_bare_child_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    with (
        owned_run(tmp_path, "driver-missing-stop", command="run", dispatch_kind="driver") as (layout, lease),
        pytest.raises(SystemExit) as raised,
    ):
        run_shot._stop_after_stage(layout, lease, 3, "layer-1-plan-gate")

    assert raised.value.code == 3
    envelope = layout.read_terminal_stop()
    assert envelope.stop_class == "harness_defect"
    assert envelope.cause.invariant_id == "terminal_boundary_requires_typed_stop"
    assert [action.transaction_id for action in envelope.actions] == ["route_engineering"]


def test_driver_terminalizes_a_recorded_signal_intent_without_a_stop_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    with owned_run(tmp_path, "driver-interrupted", command="run", dispatch_kind="driver") as (layout, lease):
        interrupted = run_owner_boundary.terminalize_cancellation(
            tmp_path,
            layout,
            lease,
            run_owner_boundary.RunCancellation("operator_interrupt", 2),
        )

    assert interrupted.code == 130
    status = json.loads(layout.status.read_text(encoding="utf-8"))
    assert (status["state"], status["exit_code"]) == ("interrupted", 130)
    assert status["stop_envelope"] is None
    assert not layout.stop_envelope.exists()
    with pytest.raises(ValueError, match="selects no stop envelope"):
        layout.read_terminal_stop()


def test_run_allocates_layout_then_stops_on_strict_preflight_before_any_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    shot = SimpleNamespace(folder=tmp_path, id="strict-preflight-shot")
    monkeypatch.setattr(run_shot, "load_shot", lambda _folder: shot)
    monkeypatch.setattr(
        run_shot,
        "preflight_probe",
        lambda _blender: {
            "ok": False,
            "auth": {
                "ok": False,
                "using": None,
                "problems": ["no selected credential"],
                "notes": [],
                "present": [],
                "decoys": [],
            },
            "configuration": {"ok": True, "problems": []},
            "blender": {
                "ok": True,
                "requested": "blender",
                "resolved": "/usr/bin/blender",
                "problems": [],
            },
            "blender_confinement": {
                "ok": True,
                "bwrap": "/usr/bin/bwrap",
                "libseccomp": "libseccomp.so.2",
                "worker_blender": "5.2.1 LTS",
                "problems": [],
            },
            "builder_execution_fence": {
                "ok": True,
                "mechanism": "sysv-sem-undo+descriptor-flock",
                "problems": [],
            },
        },
    )
    monkeypatch.setattr(
        run_shot,
        "_run",
        lambda *_args, **_kwargs: pytest.fail("no stage may run after failed strict preflight"),
    )
    monkeypatch.setattr(sys, "argv", ["vfx run", str(tmp_path), "--skip-render"])

    with pytest.raises(SystemExit) as raised:
        run_shot.main()

    assert raised.value.code == 1
    layout = run_artifacts.latest(tmp_path)
    assert layout is not None
    envelope = layout.read_terminal_stop()
    assert envelope.stop_class == "infrastructure_failure"
    assert [action.transaction_id for action in envelope.actions] == ["recover_environment"]
