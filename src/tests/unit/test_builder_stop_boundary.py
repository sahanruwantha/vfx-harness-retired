"""Builder falsification publishes exact typed stop authority, never guessed repair."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest

import vfx_harness.agents.builder.cli as builder_cli
import vfx_harness.agents.builder.layer as builder_layer
import vfx_harness.agents.builder.stops as builder_stops
from tests.architecture.test_staged_architecture import _unit
from vfx_harness.agents.builder.models import BuildAuthorityDefect
from vfx_harness.domain.stop_envelope_primitives import canonical_digest
from vfx_harness.domain.stop_transaction_state import (
    EvidenceRecordAssertion,
    SelectedAuthorityAssertionV2,
    SelectedAuthorityBundle,
    SelectedAuthorityView,
    StopEvidenceRef,
)
from vfx_harness.domain.stop_transactions import (
    EscalateQuestionTarget,
    HumanDecisionCommitted,
    PublishValidatedAmendmentTarget,
    SelectedAuthorityAmendmentCommitted,
)
from vfx_harness.domain.unit_outcomes import HYPOTHESIS_FALSIFICATION_SCHEMA
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration.authority_selection import (
    AuthorityPointerObservation,
    ResolvedSelectedAuthority,
)
from vfx_harness.orchestration.ledger import Layer
from vfx_harness.orchestration.unit_state import (
    STATE_DIR,
    initialize,
    record_hypothesis_falsification,
    transition,
)
from vfx_harness.orchestration.unit_state import load as load_unit_state


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _replace_current_finding(
    root: Path,
    finding: dict,
    *,
    record_id: str,
    recorded_at: str,
    evidence_relative: str,
    observation_value: float | None = None,
    decision_strength: str | None = None,
) -> dict:
    """Replace only test authority needed to model a newly sealed finding record."""

    variant = deepcopy(finding)
    source = root / str(finding["evidence"][0])
    target = root / evidence_relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source.read_bytes())
    variant["record_id"] = record_id
    variant["recorded_at"] = recorded_at
    variant["evidence"] = [evidence_relative]
    if observation_value is not None:
        variant["observations"][0]["value"] = observation_value
    if decision_strength is not None:
        variant["decisions"][0]["strength"] = decision_strength

    state_path = root / STATE_DIR / "layer_1.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["units"][variant["unit"]]["falsification"] = variant
    state["falsifications"][-1] = variant
    state_path.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    artifact = (
        root
        / STATE_DIR
        / "hypothesis-falsifications"
        / f"{variant['record_id']}.json"
    )
    artifact.write_text(
        json.dumps(variant, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return variant


def _fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[SimpleNamespace, run_artifacts.RunLayout, dict, object]:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    unit = _unit("proxy")
    layer = SimpleNamespace(id="1", stages=(unit,))
    shot = SimpleNamespace(folder=tmp_path, id="typed-builder-stop")

    layers_path = tmp_path / "selected" / "layers.json"
    layers_path.parent.mkdir(parents=True, exist_ok=True)
    layers_path.write_text('{"schema":5,"layers":[]}\n', encoding="utf-8")
    plan_hash = _sha256(layers_path)
    unit_plan = tmp_path / unit.plan
    unit_plan.parent.mkdir(parents=True, exist_ok=True)
    unit_plan.write_text("# exact gated unit plan\n", encoding="utf-8")
    evidence = tmp_path / "evidence" / "failed-contract.json"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text('{"id":"bbox-f36","pass":false}\n', encoding="utf-8")

    initialize(tmp_path, "1", (unit,), plan_hash=plan_hash)
    transition(tmp_path, "1", unit.id, "planning", reason="ready")
    transition(tmp_path, "1", unit.id, "building", reason="started")
    bundle_digest = hashlib.sha256(b"selected bundle").hexdigest()
    finding = record_hypothesis_falsification(
        tmp_path,
        "1",
        unit,
        (unit,),
        bundle_hash=bundle_digest,
        unit_plan_hash=_sha256(unit_plan),
        candidate_hash=hashlib.sha256(b"candidate").hexdigest(),
        settings_hash=hashlib.sha256(b"settings").hexdigest(),
        contract_ids=["bbox-f36"],
        observations=[
            {
                "contract_id": "bbox-f36",
                "pass": False,
                "value": -10.0,
                "target": [0.95, 1.3],
            }
        ],
        decisions=[{"id": "A-camera", "strength": "approved_start"}],
        conflict={
            "kind": "decision",
            "required_authority": "change the approved camera start",
            "roles": ["camera", "proxy"],
            "controls": ["camera_spine"],
        },
        evidence=["evidence/failed-contract.json"],
    )

    bundle = SimpleNamespace(content_hash=bundle_digest)
    view_digest = hashlib.sha256(b"selected view").hexdigest()
    monkeypatch.setattr(
        builder_stops.plan_authority,
        "resolve_current",
        lambda _folder: bundle,
    )
    monkeypatch.setattr(
        builder_stops.plan_authority,
        "selected_artifact_path",
        lambda _folder, name: layers_path
        if name == "layers.json"
        else pytest.fail(f"unexpected selected artifact {name}"),
    )
    monkeypatch.setattr(
        builder_stops.judgment_observation,
        "selected_view_digest",
        lambda _folder, _bundle: view_digest,
    )
    selected_authority = ResolvedSelectedAuthority(
        assertion=SelectedAuthorityAssertionV2(
            "selected",
            SelectedAuthorityBundle(
                bundle_digest,
                "clean_with_deferred",
                hashlib.sha256(b"bundle manifest").hexdigest(),
            ),
            SelectedAuthorityView(
                "jit",
                view_digest,
                hashlib.sha256(b"view manifest").hexdigest(),
            ),
        ),
        pointer_observation=AuthorityPointerObservation(
            hashlib.sha256(b"plan pointer").hexdigest(),
            hashlib.sha256(b"jit pointer").hexdigest(),
        ),
    )
    monkeypatch.setattr(
        builder_stops,
        "resolve_selected_authority",
        lambda _folder: selected_authority,
    )
    monkeypatch.setattr(
        builder_stops.ledger_runtime,
        "load_layers",
        lambda _shot: {"1": layer},
    )
    monkeypatch.setattr(
        builder_stops.layer_plans,
        "work_unit_plan_path",
        lambda _folder, _unit: unit_plan,
    )
    layout = run_artifacts.create(tmp_path, "builder-authority-stop")
    return shot, layout, finding, unit


def test_exact_current_falsification_compiles_one_amendment_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shot, layout, finding, _unit_record = _fixture(tmp_path, monkeypatch)

    envelope = builder_stops.compile_hypothesis_falsification_stop(
        shot,
        layout,
        finding,
    )

    assert envelope.stage == "builder"
    assert envelope.stop_class == "authority_defect"
    assert envelope.retryable is False
    assert envelope.identity.bundle_digest == finding["identities"]["bundle_hash"]
    assert envelope.identity.unit_plan_digest == finding["identities"]["unit_plan_hash"]
    assert envelope.identity.unit_digest == finding["identities"]["unit_hash"]
    assert envelope.identity.candidate_digest == finding["identities"]["candidate_hash"]
    assert envelope.identity.checkpoint_digest is not None
    assert envelope.identity.settings_digest == finding["identities"]["settings_hash"]
    assert [action.transaction_id for action in envelope.actions] == [
        "publish_validated_amendment"
    ]
    assert "apply_revision_checked_replan" not in {
        action.transaction_id for action in envelope.actions
    }
    assert len(envelope.evidence_refs) == 1
    evidence_ref = envelope.evidence_refs[0]
    assert isinstance(evidence_ref, StopEvidenceRef)
    assert evidence_ref.kind == "stop_evidence"
    assert evidence_ref.record_schema == (
        "vfx-harness.builder-authority-stop-evidence/v1"
    )

    action = envelope.actions[0]
    assert isinstance(action.target, PublishValidatedAmendmentTarget)
    target = action.target
    assert target.scope == "layer_view"
    assert target.layer_id == finding["layer"]
    assert target.owner_authority_id == "layer-plan-authority"
    assert target.base_authority.selection == "selected"
    assert target.base_authority.bundle is not None
    assert target.base_authority.bundle.digest == finding["identities"]["bundle_hash"]
    assert target.base_authority.effective_view is not None
    assert target.base_authority.effective_view.digest == envelope.identity.view_digest
    assert target.base_authority.digest == envelope.authoritative_before_digest
    assert len(target.findings) == 1
    finding_assertion = target.findings[0]
    finding_identity = builder_stops._finding_identity_payload(finding)
    finding_identity_digest = canonical_digest(finding_identity)
    finding_identity_id = f"hf-semantic-{finding_identity_digest[:20]}"
    assert isinstance(finding_assertion, EvidenceRecordAssertion)
    assert finding_assertion.record_kind == "finding"
    assert finding_assertion.record_id == finding_identity_id
    assert finding_assertion.evidence == evidence_ref
    assert isinstance(action.postcondition, SelectedAuthorityAmendmentCommitted)
    assert action.postcondition.scope == "layer_view"
    assert action.postcondition.base_authority_digest == envelope.authoritative_before_digest
    assert action.postcondition.required_after_source == "jit"
    assert action.postcondition.layer_id == finding["layer"]
    assert action.postcondition.finding_ids == (finding_identity_id,)
    assert action.postcondition.gate_policy_id == (
        "structural-authority/runtime-falsification-v1"
    )
    serialized_action = action.as_dict()
    assert "required_authority_digest" not in json.dumps(serialized_action)
    assert "progress_postcondition_digest" not in json.dumps(serialized_action)

    report = json.loads(
        (layout.reports / "builder-authority-stop-evidence.json").read_text(
            encoding="utf-8"
        )
    )
    assert "run_id" not in report
    assert report["finding_record"] == {
        "schema": "vfx-harness.builder-falsification-identity/v1",
        "record_id": finding_identity_id,
        "record_digest": finding_identity_digest,
        "payload": finding_identity,
    }
    assert report["source_finding_identity"] == {
        "wire_schema": HYPOTHESIS_FALSIFICATION_SCHEMA,
        "semantic_schema": "vfx-harness.builder-falsification-identity/v1",
        "semantic_digest": finding_identity_digest,
    }
    assert report["evidence_sha256"] == [
        _sha256(tmp_path / "evidence/failed-contract.json")
    ]
    assert evidence_ref.record_digest == canonical_digest(report)
    assert evidence_ref.sha256 == _sha256(
        layout.reports / "builder-authority-stop-evidence.json"
    )
    audit = json.loads(
        (layout.reports / "builder-authority-stop-audit.json").read_text(
            encoding="utf-8"
        )
    )
    assert audit["run_id"] == layout.run_id
    source_finding = audit["audit_locators"]["source_finding"]
    assert source_finding["schema"] == HYPOTHESIS_FALSIFICATION_SCHEMA
    assert source_finding["record_id"] == finding["record_id"]
    assert source_finding["record_digest"] == canonical_digest(finding)
    assert source_finding["payload"] == finding
    assert source_finding["artifact_sha256"] == _sha256(
        tmp_path
        / "state/work-units/hypothesis-falsifications"
        / f"{finding['record_id']}.json"
    )
    assert audit["audit_locators"]["evidence"] == [
        {
            "path": "evidence/failed-contract.json",
            "sha256": _sha256(tmp_path / "evidence/failed-contract.json"),
        }
    ]
    assert report["attempt_evidence_digest"] == envelope.attempt_evidence_digest

    composition_layout = run_artifacts.create(tmp_path, "composition-authority-stop")
    composition = builder_stops.compile_hypothesis_falsification_stop(
        shot,
        composition_layout,
        finding,
        stage="composition",
    )
    assert composition.stage == "composition"
    assert composition.cause_fingerprint == envelope.cause_fingerprint


def test_hard_constraint_falsification_requires_a_typed_human_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shot, layout, finding, _unit_record = _fixture(tmp_path, monkeypatch)
    hard = _replace_current_finding(
        tmp_path,
        finding,
        record_id="hf-hard-constraint",
        recorded_at="2026-08-31T02:00:00+00:00",
        evidence_relative="evidence/hard-constraint.json",
        decision_strength="hard_constraint",
    )

    envelope = builder_stops.compile_hypothesis_falsification_stop(
        shot,
        layout,
        hard,
    )

    assert envelope.stop_class == "human_decision_required"
    assert envelope.cause.invariant_id == (
        "hard_constraint_amendment_requires_human_decision"
    )
    assert envelope.budget_key == "builder-hard-constraint-decision"
    assert [action.transaction_id for action in envelope.actions] == [
        "escalate_question"
    ]
    action = envelope.actions[0]
    assert isinstance(action.target, EscalateQuestionTarget)
    assert isinstance(action.postcondition, HumanDecisionCommitted)
    assert action.target.allowed_answer_ids == (
        "approve-hard-constraint-amendment",
        "reject-hard-constraint-amendment",
    )
    assert action.target.question_record.record_kind == "question"
    assert action.target.question_record.evidence == envelope.evidence_refs[0]
    assert action.postcondition.question_digest == action.target.question_digest

    report = json.loads(
        (layout.reports / "builder-authority-stop-evidence.json").read_text(
            encoding="utf-8"
        )
    )
    assert report["evidence_kind"] == "human_decision_required"
    question = report["decision_question"]
    assert question["record_id"] == action.target.question_record.record_id
    assert question["question_digest"] == canonical_digest(question["payload"])


def test_builder_stop_rejects_stale_or_untyped_finding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shot, layout, finding, _unit_record = _fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(
        builder_stops.plan_authority,
        "resolve_current",
        lambda _folder: SimpleNamespace(
            content_hash=hashlib.sha256(b"replacement bundle").hexdigest()
        ),
    )

    with pytest.raises(ValueError, match="superseded bundle authority"):
        builder_stops.compile_hypothesis_falsification_stop(shot, layout, finding)

    with pytest.raises(ValueError, match="current closed hypothesis-falsification shape"):
        builder_stops.compile_hypothesis_falsification_stop(
            shot,
            layout,
            {"status": "contract_gap"},
        )


def test_action_identity_ignores_finding_audit_metadata_but_tracks_semantics_and_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shot, first_layout, finding, _unit_record = _fixture(tmp_path, monkeypatch)
    first = builder_stops.compile_hypothesis_falsification_stop(
        shot,
        first_layout,
        finding,
    )

    second_layout = run_artifacts.create(tmp_path, "builder-authority-stop-restart")
    second = builder_stops.compile_hypothesis_falsification_stop(
        shot,
        second_layout,
        finding,
    )
    assert second.identity.run_id != first.identity.run_id
    assert second.attempt_evidence_digest == first.attempt_evidence_digest
    assert second.evidence_refs[0].locator != first.evidence_refs[0].locator
    assert second.evidence_refs[0].digest == first.evidence_refs[0].digest
    assert second.actions[0].digest == first.actions[0].digest

    audit_variant = _replace_current_finding(
        tmp_path,
        finding,
        record_id="hf-another-run-audit-id",
        recorded_at="2099-01-01T00:00:00+00:00",
        evidence_relative="runs/another-run/evidence/failed-contract.json",
    )
    assert builder_stops._finding_identity_payload(audit_variant) == (
        builder_stops._finding_identity_payload(finding)
    )
    audit_layout = run_artifacts.create(tmp_path, "builder-authority-stop-audit-variant")
    audit_changed = builder_stops.compile_hypothesis_falsification_stop(
        shot,
        audit_layout,
        audit_variant,
    )
    assert audit_changed.authoritative_before_digest == first.authoritative_before_digest
    assert audit_changed.attempt_evidence_digest == first.attempt_evidence_digest
    assert audit_changed.evidence_refs[0].digest == first.evidence_refs[0].digest
    assert audit_changed.actions[0].digest == first.actions[0].digest

    semantic_variant = _replace_current_finding(
        tmp_path,
        audit_variant,
        record_id="hf-semantic-change",
        recorded_at="2099-01-02T00:00:00+00:00",
        evidence_relative="runs/semantic-run/evidence/failed-contract.json",
        observation_value=-12.0,
    )
    semantic_layout = run_artifacts.create(
        tmp_path,
        "builder-authority-stop-semantic-variant",
    )
    semantic_changed = builder_stops.compile_hypothesis_falsification_stop(
        shot,
        semantic_layout,
        semantic_variant,
    )
    assert semantic_changed.attempt_evidence_digest != audit_changed.attempt_evidence_digest
    assert semantic_changed.evidence_refs[0].digest != audit_changed.evidence_refs[0].digest
    assert semantic_changed.actions[0].digest != audit_changed.actions[0].digest

    (tmp_path / semantic_variant["evidence"][0]).write_text(
        '{"id":"bbox-f36","pass":false,"value":-11}\n',
        encoding="utf-8",
    )
    changed_layout = run_artifacts.create(tmp_path, "builder-authority-stop-new-evidence")
    changed = builder_stops.compile_hypothesis_falsification_stop(
        shot,
        changed_layout,
        semantic_variant,
    )
    assert changed.attempt_evidence_digest != semantic_changed.attempt_evidence_digest
    assert changed.evidence_refs[0].digest != semantic_changed.evidence_refs[0].digest
    assert changed.actions[0].digest != semantic_changed.actions[0].digest


def test_public_builder_exit_keeps_legacy_code_and_detail_with_typed_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shot, _layout, finding, _unit_record = _fixture(tmp_path, monkeypatch)
    envelope = builder_stops.compile_hypothesis_falsification_stop(
        shot,
        run_artifacts.active(tmp_path),
        finding,
    )
    monkeypatch.setattr(
        builder_cli.stop_runtime,
        "compile_hypothesis_falsification_stop",
        lambda *_args, **_kwargs: envelope,
    )
    failure = BuildAuthorityDefect(
        finding,
        stage="builder",
        exit_code=7,
        legacy_detail="layer 1 did not accept every work unit: proxy=hypothesis_falsified",
    )

    stopped = builder_cli._authority_defect_exit(shot, failure)

    assert isinstance(stopped, run_artifacts.TypedStop)
    assert stopped.code == 7
    assert str(stopped).startswith("INCOMPLETE CHAIN — layer 1")
    assert stopped.stop_envelope is envelope
    assert stopped.terminal_cause == "authority_defect"


def test_layer_runtime_carries_only_a_sealed_falsification_to_public_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shot, _layout, finding, unit = _fixture(tmp_path, monkeypatch)
    layer = Layer(
        id="1",
        script=unit.mutates.script_spans[0],
        title="proxy",
        judges=((unit.evaluation.primary_judge, unit.evaluation.judges[0].ref),),
        reads="proxy",
        owns=(unit.evaluation.claims[0].axis,),
        primary_judge=unit.evaluation.primary_judge,
        stages=(unit,),
    )
    context = tmp_path / "generated-context.md"
    context.write_text("context\n", encoding="utf-8")
    unit_plan = tmp_path / unit.plan

    class _Ledger:
        @staticmethod
        def status(_milestone) -> str:
            return "contract_gap"

    async def build_unit(*_args, **_kwargs):
        return _Ledger()

    monkeypatch.setattr(builder_layer, "active_plan_hash", lambda _folder: finding["identities"]["plan_hash"])
    monkeypatch.setattr(builder_layer, "initialize", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(builder_layer, "_prior_layer_paths", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(builder_layer, "load_unit_state", lambda *_args: load_unit_state(tmp_path, "1"))
    monkeypatch.setattr(builder_layer, "load_layers", lambda _shot: {"1": layer})
    monkeypatch.setattr(builder_layer, "plan_strips", lambda _shot: {})
    monkeypatch.setattr(builder_layer, "ready_from_durable_state", lambda *_args, **_kwargs: (unit,))
    monkeypatch.setattr(builder_layer, "require_due_clear", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(builder_layer, "transition", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(builder_layer, "block_dependents", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(builder_layer, "work_unit_plan_path", lambda *_args: unit_plan)
    monkeypatch.setattr(builder_layer, "validate_work_unit_plan_authority", lambda *_args: None)
    monkeypatch.setattr(builder_layer, "_plan_layer_excerpt", lambda *_args: "unit plan")
    monkeypatch.setattr(builder_layer, "load_milestones", lambda _shot: {})
    monkeypatch.setattr(builder_layer, "write_layer_context", lambda *_args, **_kwargs: context)
    monkeypatch.setattr(
        builder_layer,
        "builder_package",
        lambda: SimpleNamespace(build_unit=build_unit),
    )
    monkeypatch.setattr(
        builder_layer,
        "_record_contract_gap_falsification",
        lambda *_args: finding,
    )

    async def run() -> None:
        with pytest.raises(BuildAuthorityDefect) as caught:
            await builder_layer.build_layer(shot, layer, session=None, verbose=False)
        assert caught.value.finding.record_id == finding["record_id"]
        assert caught.value.stage == "builder"
        assert caught.value.exit_code == 7

    anyio.run(run)


def test_unclassified_builder_exception_preserves_in_flight_unit_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raised runtime boundary is not executable evidence that the unit failed."""
    unit = _unit("proxy")
    shot = SimpleNamespace(folder=tmp_path, id="builder-boundary-failure")
    unit_plan = tmp_path / unit.plan
    unit_plan.parent.mkdir(parents=True, exist_ok=True)
    unit_plan.write_text("# exact gated unit plan\n", encoding="utf-8")
    initialize(tmp_path, "1", (unit,), plan_hash="0" * 64)
    transition(tmp_path, "1", unit.id, "planning", reason="ready")
    transition(tmp_path, "1", unit.id, "building", reason="started")
    layer = Layer(
        id="1",
        script=unit.mutates.script_spans[0],
        title="proxy",
        judges=((unit.evaluation.primary_judge, unit.evaluation.judges[0].ref),),
        reads="proxy",
        owns=(unit.evaluation.claims[0].axis,),
        primary_judge=unit.evaluation.primary_judge,
        stages=(unit,),
    )
    context = tmp_path / "generated-context.md"
    context.write_text("context\n", encoding="utf-8")
    blocked: list[str] = []

    async def build_unit(*_args, **_kwargs):
        raise RuntimeError("unclassified builder boundary failure")

    monkeypatch.setattr(builder_layer, "active_plan_hash", lambda _folder: "0" * 64)
    monkeypatch.setattr(builder_layer, "initialize", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(builder_layer, "_prior_layer_paths", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(builder_layer, "load_layers", lambda _shot: {"1": layer})
    monkeypatch.setattr(builder_layer, "plan_strips", lambda _shot: {})
    monkeypatch.setattr(builder_layer, "ready_from_durable_state", lambda *_args, **_kwargs: (unit,))
    monkeypatch.setattr(builder_layer, "require_due_clear", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        builder_layer,
        "block_dependents",
        lambda *_args, **_kwargs: blocked.append(unit.id),
    )
    monkeypatch.setattr(builder_layer, "work_unit_plan_path", lambda *_args: unit_plan)
    monkeypatch.setattr(builder_layer, "validate_work_unit_plan_authority", lambda *_args: None)
    monkeypatch.setattr(builder_layer, "_plan_layer_excerpt", lambda *_args: "unit plan")
    monkeypatch.setattr(builder_layer, "load_milestones", lambda _shot: {})
    monkeypatch.setattr(builder_layer, "write_layer_context", lambda *_args, **_kwargs: context)
    monkeypatch.setattr(
        builder_layer,
        "builder_package",
        lambda: SimpleNamespace(build_unit=build_unit),
    )

    async def run() -> None:
        with pytest.raises(RuntimeError, match="unclassified builder boundary failure"):
            await builder_layer.build_layer(shot, layer, session=None, verbose=False)

    anyio.run(run)

    state = load_unit_state(tmp_path, "1")
    assert state["units"][unit.id]["status"] == "building"
    assert blocked == []


def test_composition_runtime_preserves_outcome_then_carries_sealed_finding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shot, _layout, finding, unit = _fixture(tmp_path, monkeypatch)
    unit_script = tmp_path / unit.mutates.script_spans[0]
    unit_script.parent.mkdir(parents=True, exist_ok=True)
    unit_script.write_text("# accepted unit\n", encoding="utf-8")
    layer = Layer(
        id="1",
        script="build/01_composed.py",
        title="composed proxy",
        judges=((unit.evaluation.primary_judge, unit.evaluation.judges[0].ref),),
        reads="proxy",
        owns=(unit.evaluation.claims[0].axis,),
        primary_judge=unit.evaluation.primary_judge,
        stages=(unit,),
    )
    composition_unit = SimpleNamespace(
        provisional_decisions=({"id": "R-proxy"},),
        provisional_requirement_ids=("R-proxy",),
        evaluation=unit.evaluation,
    )
    recorded_outcomes: list[str] = []

    class _Ledger:
        def __init__(self, _shot) -> None:
            self.slot: dict = {}

        def _slot(self, _milestone) -> dict:
            return self.slot

        def begin(self, _milestone) -> None:
            self.slot["attempt"] = 1

        def mark(self, _milestone, status: str, best=None) -> None:
            recorded_outcomes.append(status)

    async def axes(*_args):
        return [(unit.evaluation.claims[0].axis, "proxy axis")]

    async def verify(*_args, **_kwargs) -> str:
        return "contract_gap"

    monkeypatch.setattr(builder_layer, "active_plan_hash", lambda _folder: finding["identities"]["plan_hash"])
    monkeypatch.setattr(builder_layer, "initialize", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(builder_layer, "_prior_layer_paths", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        builder_layer,
        "load_unit_state",
        lambda *_args: {"units": {unit.id: {"status": "passed"}}},
    )
    monkeypatch.setattr(builder_layer, "load_layers", lambda _shot: {"1": layer})
    monkeypatch.setattr(builder_layer, "plan_strips", lambda _shot: {})
    monkeypatch.setattr(builder_layer, "Ledger", _Ledger)
    monkeypatch.setattr(builder_layer, "ensure_axes", axes)
    monkeypatch.setattr(
        builder_layer,
        "_load_provisional_decisions",
        lambda *_args: ({"id": "R-proxy"},),
    )
    monkeypatch.setattr(
        builder_layer,
        "_composition_judge_unit",
        lambda *_args: composition_unit,
    )
    monkeypatch.setattr(builder_layer, "_verify_script", verify)
    monkeypatch.setattr(
        builder_layer,
        "_record_composed_contract_gap_falsification",
        lambda *_args: finding,
    )
    monkeypatch.setattr(builder_layer, "write_layer_outcome", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(builder_layer, "_blender_version", lambda _session: "test")
    monkeypatch.setattr(builder_layer.costlog, "bind", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(builder_layer.costlog, "unbind", lambda: None)
    monkeypatch.setattr(builder_layer.transcript, "bind", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(builder_layer.transcript, "unbind", lambda: None)

    async def run() -> None:
        with pytest.raises(BuildAuthorityDefect) as caught:
            await builder_layer.build_layer(shot, layer, session=None, verbose=False)
        assert caught.value.finding.record_id == finding["record_id"]
        assert caught.value.stage == "composition"
        assert caught.value.exit_code == 9

    anyio.run(run)
    assert recorded_outcomes == ["contract_gap"]
