"""Builder falsification publishes exact typed stop authority, never guessed repair."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest

import vfx_harness.agents.builder.cli as builder_cli
import vfx_harness.agents.builder.layer as builder_layer
import vfx_harness.agents.builder.stops as builder_stops
import vfx_harness.agents.builder.unit_failure as builder_unit_failure
from tests.architecture.test_staged_architecture import _unit
from tests.unit_attempt_fixtures import (
    synthetic_completion_authorization_for_receipts,
)
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
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionToken,
)
from vfx_harness.orchestration.hypothesis_falsification_projection import (
    FalsificationProjectionPending,
)
from vfx_harness.orchestration.ledger import Layer
from vfx_harness.orchestration.unit_state import (
    initialize,
    record_hypothesis_falsification,
)
from vfx_harness.orchestration.unit_state import load as load_unit_state
from vfx_harness.orchestration.unit_state_claims import (
    claim_ready_unit_for_build,
    claim_ready_unit_for_planning,
)
from vfx_harness.orchestration.unit_state_lock import STATE_DIR


@pytest.fixture(autouse=True)
def _typed_mocked_completion_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep this mocked builder-boundary suite on the typed receipt API."""

    def authorize(
        folder,
        layer_id,
        _units,
        *,
        expected_plan_hash,
        selected_authority,
    ):
        state = builder_layer.load_unit_state(folder, str(layer_id))
        receipts: dict[str, str] = {}
        for unit_id, slot in (state.get("units") or {}).items():
            if not isinstance(slot, dict) or slot.get("status") != "passed":
                continue
            receipt = builder_layer.UnitCompletionReceipt.parse(
                slot.get("completion_receipt"),
                f"mocked builder state {layer_id}.{unit_id}.completion_receipt",
            )
            receipts[str(unit_id)] = receipt.receipt_digest
        if not receipts:
            return None
        return synthetic_completion_authorization_for_receipts(
            str(layer_id),
            expected_plan_hash,
            receipts,
            selection_token=selected_authority.selection_token,
        )

    monkeypatch.setattr(
        builder_layer,
        "authorize_completed_units_for_layer",
        authorize,
    )


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
        / "state"
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
    # Unit state and findings carry the selected layer CAPSULE digest (HIR-0171), which
    # is never the byte hash of layers.json; the fixture pins that distinction.
    plan_hash = hashlib.sha256(b"selected layer capsule").hexdigest()
    assert plan_hash != _sha256(layers_path)
    monkeypatch.setattr(
        builder_stops.authority_capsule_resolution,
        "selected_layer_capsule_digest",
        lambda _folder, layer_id, _selected: plan_hash if str(layer_id) == "1" else "other",
    )
    unit_plan = tmp_path / unit.plan
    unit_plan.parent.mkdir(parents=True, exist_ok=True)
    unit_plan.write_text("# exact gated unit plan\n", encoding="utf-8")
    evidence = tmp_path / "evidence" / "failed-contract.json"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text('{"id":"bbox-f36","pass":false}\n', encoding="utf-8")

    initialize(tmp_path, "1", (unit,), plan_hash=plan_hash)
    state_token = AuthoritySelectionToken(0, None, 0, None)
    planning_claim = claim_ready_unit_for_planning(
        tmp_path,
        "1",
        unit.id,
        (unit,),
        expected_plan_hash=plan_hash,
        eligible_passed=set(),
        completion_authorization=None,
        run_id="builder-stop-fixture",
        selection_token=state_token,
        reason="fixture planning claim",
    )
    build_claim = claim_ready_unit_for_build(
        tmp_path,
        "1",
        unit.id,
        (unit,),
        planning_claim,
        expected_plan_hash=plan_hash,
        eligible_passed=set(),
        completion_authorization=None,
        run_id="builder-stop-fixture",
        selection_token=state_token,
        reason="fixture build claim",
    )
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
        attempt=build_claim,
        selection_token=state_token,
    )

    bundle = SimpleNamespace(content_hash=bundle_digest)
    view_digest = hashlib.sha256(b"selected view").hexdigest()
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
        selection_token=AuthoritySelectionToken(
            plan_revision=1,
            plan_pointer_sha256=hashlib.sha256(b"plan pointer").hexdigest(),
            jit_revision=1,
            jit_pointer_sha256=hashlib.sha256(b"jit pointer").hexdigest(),
        ),
        plan=SimpleNamespace(revision=1, bundle=bundle),
        artifact_paths={"layers.json": layers_path},
    )
    monkeypatch.setattr(
        builder_stops,
        "resolve_selected_authority",
        lambda _folder: selected_authority,
    )
    monkeypatch.setattr(
        builder_stops.ledger_runtime,
        "load_layers",
        lambda _shot, *, selected_authority=None: {"1": layer},
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
        / "state/hypothesis-falsifications"
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


def test_stop_compiler_reconciles_missing_falsification_projection_from_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shot, layout, finding, _unit_record = _fixture(tmp_path, monkeypatch)
    projection = (
        tmp_path
        / "state"
        / "hypothesis-falsifications"
        / f"{finding['record_id']}.json"
    )
    projection.unlink()

    envelope = builder_stops.compile_hypothesis_falsification_stop(
        shot,
        layout,
        finding,
    )

    assert envelope.stop_class == "authority_defect"
    assert json.loads(projection.read_text(encoding="utf-8")) == finding


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
    selected = builder_stops.resolve_selected_authority(tmp_path)
    assert selected.assertion.bundle is not None
    monkeypatch.setattr(
        builder_stops,
        "resolve_selected_authority",
        lambda _folder: replace(
            selected,
            assertion=replace(
                selected.assertion,
                bundle=replace(
                    selected.assertion.bundle,
                    digest=hashlib.sha256(b"replacement bundle").hexdigest(),
                ),
            ),
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
        script="build/01_proxy.py",
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

    events: list[str] = []
    selected_publications: list[str] = []

    async def build_unit(*args, **kwargs):
        assert args[1].id == "1@proxy"
        assert "publish_layer" not in kwargs
        events.append("build")
        return _Ledger()

    monkeypatch.setattr(
        builder_layer,
        "selected_layer_capsule_digest",
        lambda *_args: finding["identities"]["plan_hash"],
    )
    monkeypatch.setattr(builder_layer, "_prior_layer_paths", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(builder_layer, "load_unit_state", lambda *_args: load_unit_state(tmp_path, "1"))
    monkeypatch.setattr(builder_layer, "load_layers", lambda *_args, **_kwargs: {"1": layer})
    monkeypatch.setattr(builder_layer, "plan_strips", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(builder_layer, "ready_from_durable_state", lambda *_args, **_kwargs: (unit,))
    monkeypatch.setattr(builder_layer, "require_due_clear", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        builder_layer,
        "commit_selected_authority",
        lambda _folder, _selected, *, operation, mutation: (
            selected_publications.append(operation),
            mutation(),
        )[1],
    )
    monkeypatch.setattr(builder_layer, "transition", lambda *_args, **_kwargs: None)
    planning_claim = SimpleNamespace(phase="planning")
    building_claim = SimpleNamespace(phase="building")

    class _Guard:
        claim = planning_claim

        def check(self, _operation):
            return self.claim

        def publish(self, _operation, mutation):
            return mutation()

        def promoted(self, claim):
            self.claim = claim
            return self

    monkeypatch.setattr(
        builder_layer.UnitAttemptGuard,
        "bind",
        lambda *_args, **_kwargs: _Guard(),
    )
    monkeypatch.setattr(
        builder_layer,
        "claim_ready_unit_for_planning",
        lambda *_args, **_kwargs: events.append("claim-planning") or planning_claim,
    )
    monkeypatch.setattr(
        builder_layer,
        "claim_ready_unit_for_build",
        lambda *_args, **_kwargs: events.append("claim-building") or building_claim,
    )
    monkeypatch.setattr(
        builder_unit_failure,
        "block_dependents",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(builder_layer, "work_unit_plan_path", lambda *_args, **_kwargs: unit_plan)
    monkeypatch.setattr(
        builder_layer,
        "validate_work_unit_plan_authority",
        lambda *_args, **_kwargs: events.append("validate-plan"),
    )
    monkeypatch.setattr(builder_layer, "_plan_layer_excerpt", lambda *_args, **_kwargs: "unit plan")
    monkeypatch.setattr(builder_layer, "load_milestones", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(builder_layer, "write_layer_context", lambda *_args, **_kwargs: context)
    monkeypatch.setattr(
        builder_layer,
        "builder_package",
        lambda: SimpleNamespace(build_unit=build_unit),
    )
    def projection_pending(*_args, **_kwargs):
        raise FalsificationProjectionPending(finding)

    monkeypatch.setattr(
        builder_unit_failure,
        "_record_contract_gap_falsification",
        projection_pending,
    )
    monkeypatch.setattr(
        builder_unit_failure,
        "fail_unit_attempt",
        lambda *_args, **_kwargs: pytest.fail(
            "a committed falsification must not fail its archived attempt"
        ),
    )

    async def run() -> None:
        with pytest.raises(BuildAuthorityDefect) as caught:
            await builder_layer.build_layer(shot, layer, session=None, verbose=False)
        assert caught.value.finding.record_id == finding["record_id"]
        assert caught.value.stage == "builder"
        assert caught.value.exit_code == 7

    anyio.run(run)
    assert events == ["claim-planning", "validate-plan", "claim-building", "build"]
    assert selected_publications == [
        "block dependants of unit 1.proxy",
    ]
    assert not any(
        operation.startswith(("claim unit ", "promote unit "))
        for operation in selected_publications
    )


def test_build_layer_refuses_unbound_legacy_resume_before_fence_or_spend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    touched: list[str] = []

    def unexpected_fence(*_args, **_kwargs):
        touched.append("fence")
        raise AssertionError("resume reached the execution fence")

    def unexpected_claim(*_args, **_kwargs):
        touched.append("claim")
        raise AssertionError("resume reached a work-unit claim")

    monkeypatch.setattr(builder_layer, "builder_execution_fence", unexpected_fence)
    monkeypatch.setattr(
        builder_layer,
        "claim_ready_unit_for_planning",
        unexpected_claim,
    )
    monkeypatch.setattr(
        builder_layer,
        "builder_package",
        lambda: touched.append("builder") or SimpleNamespace(),
    )

    async def run() -> None:
        with pytest.raises(ValueError, match="not bound to an exact work-unit attempt"):
            await builder_layer.build_layer(
                SimpleNamespace(folder=tmp_path),
                layer=None,
                session=None,
                resume_ok=True,
            )

    anyio.run(run)
    assert touched == []
    assert not (tmp_path / "state/builder-execution/fence.lock").exists()


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
    layer = Layer(
        id="1",
        script="build/01_proxy.py",
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

    monkeypatch.setattr(
        builder_layer,
        "selected_layer_capsule_digest",
        lambda *_args: "0" * 64,
    )
    monkeypatch.setattr(builder_layer, "_prior_layer_paths", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(builder_layer, "load_layers", lambda *_args, **_kwargs: {"1": layer})
    monkeypatch.setattr(builder_layer, "plan_strips", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(builder_layer, "ready_from_durable_state", lambda *_args, **_kwargs: (unit,))
    monkeypatch.setattr(builder_layer, "require_due_clear", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        builder_unit_failure,
        "block_dependents",
        lambda *_args, **_kwargs: blocked.append(unit.id),
    )
    monkeypatch.setattr(builder_layer, "work_unit_plan_path", lambda *_args, **_kwargs: unit_plan)
    monkeypatch.setattr(builder_layer, "validate_work_unit_plan_authority", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(builder_layer, "_plan_layer_excerpt", lambda *_args, **_kwargs: "unit plan")
    monkeypatch.setattr(builder_layer, "load_milestones", lambda *_args, **_kwargs: {})
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
    assert state["units"][unit.id]["active_attempt"]["phase"] == "building"
    assert blocked == []


def test_build_layer_refuses_layer_script_aliasing_a_unit_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unit = _unit("proxy")
    unit_script = tmp_path / unit.mutates.script_spans[0]
    unit_script.parent.mkdir(parents=True, exist_ok=True)
    unit_script.write_text("# accepted unit\n", encoding="utf-8")
    layer = Layer(
        id="1",
        script=unit.mutates.script_spans[0],
        title="invalid aliased layer",
        judges=((unit.evaluation.primary_judge, unit.evaluation.judges[0].ref),),
        reads="proxy",
        owns=(unit.evaluation.claims[0].axis,),
        primary_judge=unit.evaluation.primary_judge,
        stages=(unit,),
    )
    capsule_reads: list[str] = []
    monkeypatch.setattr(
        builder_layer,
        "load_layers",
        lambda *_args, **_kwargs: {"1": layer},
    )
    monkeypatch.setattr(
        builder_layer,
        "selected_layer_capsule_digest",
        lambda *_args, **_kwargs: capsule_reads.append("capsule") or "a" * 64,
    )

    async def run() -> None:
        with pytest.raises(
            ValueError,
            match=r"reserves .* for the composed layer artifact",
        ):
            await builder_layer.build_layer(
                SimpleNamespace(folder=tmp_path),
                layer,
                session=None,
                verbose=False,
            )

    anyio.run(run)
    assert capsule_reads == []
    assert unit_script.read_text(encoding="utf-8") == "# accepted unit\n"


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
    events: list[str] = []
    terminal_statuses: list[str] = []
    claim = SimpleNamespace(
        mode="single_unit",
        claim_id="lfc-proxy",
        layer_script_path=layer.script,
        layer_script_sha256="a" * 64,
        predecessor_inputs=(),
    )
    replay_receipt = SimpleNamespace(
        receipt_digest="b" * 64,
        layer_script_sha256="a" * 64,
        created_at="2026-09-01T00:00:00+00:00",
        claim=claim,
    )
    stored_replay = SimpleNamespace(
        receipt=replay_receipt,
        locator="runs/test/checkpoints/layer-finalizations/lfc-proxy.json",
        sha256="c" * 64,
    )
    terminal_receipt = SimpleNamespace(
        receipt_digest="d" * 64,
        completed_at="2026-09-01T00:00:00+00:00",
        layer_script_path=layer.script,
        layer_script_sha256="a" * 64,
        claim=claim,
        projection={"revalidation": {"schema": "fixture-revalidation"}},
    )

    class _Guard:
        def __init__(self, authority) -> None:
            self.claim = claim
            self.authority = authority

        @property
        def label(self) -> str:
            return "fixture finalization guard"

        @property
        def authority_binding(self) -> dict:
            return {"fixture": True}

        def check(self, _operation):
            return self.authority

        def publish(self, _operation, mutation):
            return mutation()

    class _Ledger:
        def __init__(self, _shot, *_args, **_kwargs) -> None:
            self.slot: dict = {}

        def _slot(self, _milestone) -> dict:
            return self.slot

        def begin(self, _milestone) -> None:
            self.slot["attempt"] = 1

        def save(self) -> None:
            events.append("ledger_projection")

    async def axes(*_args):
        return [(unit.evaluation.claims[0].axis, "proxy axis")]

    async def verify(
        *_args,
        on_replay_ready,
        on_observation_ready,
        **_kwargs,
    ) -> str:
        replay_context = on_replay_ready(())
        sealed, payment = on_observation_ready(
            (),
            (
                {
                    "frame": unit.evaluation.primary_judge,
                    "ref": unit.evaluation.judges[0].ref,
                    "render": "runs/test/evidence/renders/composed.png",
                    "render_capture": {"png_sha256": "f" * 64},
                    "evidence": (),
                    "motion_evidence": None,
                },
            ),
            replay_context,
        )
        assert sealed is replay_receipt
        assert payment is None
        return "contract_gap"

    async def ablate(*_args, **_kwargs) -> dict:
        return {"ok": False, "note": "not run for contract gap"}

    monkeypatch.setattr(
        builder_layer,
        "selected_layer_capsule_digest",
        lambda *_args: finding["identities"]["plan_hash"],
    )
    monkeypatch.setattr(builder_layer, "_prior_layer_paths", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        builder_layer,
        "load_unit_state",
        lambda *_args: {
            "units": {
                unit.id: {
                    "status": "passed",
                    "completion_receipt": {"fixture": True},
                }
            }
        },
    )
    monkeypatch.setattr(
        builder_layer.UnitCompletionReceipt,
        "parse",
        lambda *_args, **_kwargs: SimpleNamespace(receipt_digest="e" * 64),
    )
    monkeypatch.setattr(builder_layer, "resolve_completed_unit", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(builder_layer, "load_layers", lambda *_args, **_kwargs: {"1": layer})
    monkeypatch.setattr(builder_layer, "plan_strips", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(builder_layer, "AuthorityBoundLedger", _Ledger)
    monkeypatch.setattr(builder_layer, "ensure_axes", axes)
    monkeypatch.setattr(
        builder_layer,
        "_load_provisional_decisions",
        lambda *_args, **_kwargs: ({"id": "R-proxy"},),
    )
    monkeypatch.setattr(
        builder_layer,
        "_composition_judge_unit",
        lambda *_args: composition_unit,
    )
    monkeypatch.setattr(builder_layer, "_verify_script", verify)
    monkeypatch.setattr(
        builder_layer,
        "_prepare_composed_contract_gap_falsification",
        lambda *_args, **_kwargs: SimpleNamespace(payload=finding),
    )
    monkeypatch.setattr(
        builder_layer,
        "current_layer_finalization_receipt",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        builder_layer,
        "claim_layer_finalization",
        lambda *_args, layer_script_sha256, **_kwargs: (
            setattr(claim, "layer_script_sha256", layer_script_sha256) or claim
        ),
    )
    claim_guard = _Guard(claim)
    monkeypatch.setattr(
        builder_layer.LayerFinalizationClaimGuard,
        "bind",
        lambda *_args, **_kwargs: claim_guard,
    )
    monkeypatch.setattr(
        builder_layer,
        "prepare_layer_artifact",
        lambda *_args, **_kwargs: SimpleNamespace(sha256=claim.layer_script_sha256),
    )
    monkeypatch.setattr(builder_layer, "commit_layer_artifact", lambda *_args: None)
    monkeypatch.setattr(builder_layer, "discard_layer_artifact", lambda *_args: None)
    replay_prefix = SimpleNamespace(
        unit_digests=(("1:proxy", finding["identities"]["unit_hash"]),),
        as_dict=lambda: {"schema": "fixture-replay-prefix"},
    )
    monkeypatch.setattr(
        builder_layer,
        "replay_prefix_receipt",
        lambda *_args, **_kwargs: replay_prefix,
    )
    monkeypatch.setattr(
        builder_layer,
        "prepare_replay_inputs",
        lambda *_args, **_kwargs: ((), ()),
    )
    monkeypatch.setattr(
        builder_layer,
        "require_replay_inputs_unchanged",
        lambda *_args, **_kwargs: None,
    )
    def mint_replay(**kwargs):
        replay_receipt.observation = kwargs["observation"]
        return replay_receipt

    monkeypatch.setattr(
        builder_layer,
        "LayerReplayPointObservation",
        SimpleNamespace(mint=lambda **kwargs: SimpleNamespace(**kwargs)),
    )
    monkeypatch.setattr(
        builder_layer,
        "LayerReplayObservation",
        lambda **kwargs: SimpleNamespace(execution_status="passed", **kwargs),
    )
    monkeypatch.setattr(
        builder_layer,
        "LayerReplayReceipt",
        SimpleNamespace(mint=mint_replay),
    )
    monkeypatch.setattr(
        builder_layer,
        "prepare_layer_replay_receipt",
        lambda *_args, **_kwargs: SimpleNamespace(receipt=replay_receipt),
    )
    monkeypatch.setattr(
        builder_layer,
        "commit_layer_replay_receipt",
        lambda *_args, **_kwargs: events.append("replay_receipt") or stored_replay,
    )
    monkeypatch.setattr(
        builder_layer,
        "discard_layer_replay_receipt",
        lambda *_args, **_kwargs: None,
    )

    evaluation_receipt = SimpleNamespace(
        receipt_digest="1" * 64,
        final_status="contract_gap",
        claim=claim,
    )

    def mint_evaluation(**kwargs):
        groups = kwargs["evaluation_groups"]
        assert len(kwargs["replay_receipts"]) == 1
        assert [row["result"] for row in groups] == ["contract_gap"]
        evaluation_receipt.evaluation_groups = groups
        evaluation_receipt.canonical = kwargs["canonical"]
        return evaluation_receipt

    monkeypatch.setattr(
        builder_layer,
        "LayerReplayReceiptBinding",
        SimpleNamespace(mint=lambda **kwargs: SimpleNamespace(**kwargs)),
    )
    monkeypatch.setattr(
        builder_layer,
        "LayerEvaluationReceipt",
        SimpleNamespace(mint=mint_evaluation),
    )
    monkeypatch.setattr(
        builder_layer,
        "prepare_layer_evaluation_receipt",
        lambda *_args, **_kwargs: SimpleNamespace(receipt=evaluation_receipt),
    )
    monkeypatch.setattr(
        builder_layer,
        "commit_layer_evaluation_receipt",
        lambda *_args, **_kwargs: events.append("evaluation_receipt")
        or SimpleNamespace(
            receipt=evaluation_receipt,
            locator="runs/test/checkpoints/layer-finalizations/lfc-proxy/evaluation.json",
            sha256="2" * 64,
        ),
    )
    monkeypatch.setattr(
        builder_layer,
        "discard_layer_evaluation_receipt",
        lambda *_args, **_kwargs: None,
    )

    def mint_terminal(**kwargs):
        terminal_statuses.append(kwargs["evaluation_receipt"].final_status)
        terminal_receipt.projection = kwargs["projection"]
        return terminal_receipt

    monkeypatch.setattr(
        builder_layer,
        "LayerFinalizationReceipt",
        SimpleNamespace(mint=mint_terminal),
    )
    monkeypatch.setattr(
        builder_layer,
        "complete_layer_finalization",
        lambda *_args, **_kwargs: events.append("terminal_receipt"),
    )
    prepared_revalidation = SimpleNamespace(
        source_sha256="e" * 64,
        result={"kept": 0, "dropped": []},
    )
    monkeypatch.setattr(
        builder_layer,
        "prepare_layer_revalidation",
        lambda *_args, **_kwargs: prepared_revalidation,
    )
    monkeypatch.setattr(
        builder_layer,
        "layer_revalidation_projection",
        lambda _prepared: terminal_receipt.projection["revalidation"],
    )
    monkeypatch.setattr(
        builder_layer,
        "build_layer_outcome_projection",
        lambda *_args, **_kwargs: SimpleNamespace(
            as_dict=lambda: {"schema": "fixture-outcome-projection"}
        ),
    )
    monkeypatch.setattr(
        builder_layer,
        "discard_layer_revalidation",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        builder_layer,
        "builder_package",
        lambda: SimpleNamespace(_ablate=ablate),
    )
    def reconcile(*_args, **_kwargs):
        assert "terminal_receipt" in events
        assert terminal_receipt.projection["finding"] == finding
        events.extend(
            [
                "revalidation_projection",
                "finding_projection",
                "outcome_projection",
                "ledger_projection",
            ]
        )
        return SimpleNamespace(
            revalidation={"kept": 0, "dropped": []},
            finding=finding,
            outcome=tmp_path / "runs/test/reports/layers/1.json",
            ledger=SimpleNamespace(),
        )

    monkeypatch.setattr(builder_layer, "reconcile_layer_finalization", reconcile)
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
    assert terminal_statuses == ["contract_gap"]
    assert events == [
        "replay_receipt",
        "evaluation_receipt",
        "terminal_receipt",
        "revalidation_projection",
        "finding_projection",
        "outcome_projection",
        "ledger_projection",
    ]
    assert unit_script.read_text(encoding="utf-8") == "# accepted unit\n"


def test_existing_terminal_receipt_reconciles_before_builder_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unit = _unit("proxy", script_span="build/units/1/proxy.py")
    layer = Layer(
        id="1",
        script="build/layer_1.py",
        title="receipt restart",
        judges=((unit.evaluation.primary_judge, unit.evaluation.judges[0].ref),),
        reads="fixture",
        owns=(unit.evaluation.claims[0].axis,),
        primary_judge=unit.evaluation.primary_judge,
        stages=(unit,),
    )
    shot = SimpleNamespace(folder=tmp_path)
    selected = ResolvedSelectedAuthority(
        assertion=SelectedAuthorityAssertionV2("absent", None, None),
        pointer_observation=AuthorityPointerObservation(None, None),
        selection_token=AuthoritySelectionToken(0, None, 0, None),
        plan=None,
        artifact_paths={},
    )
    terminal_receipt = SimpleNamespace(final_status="passed")
    terminal_ledger = SimpleNamespace(name="reconciled-ledger")
    events: list[str] = []

    monkeypatch.setattr(builder_layer, "load_layers", lambda *_args, **_kwargs: {"1": layer})
    monkeypatch.setattr(
        builder_layer,
        "selected_layer_capsule_digest",
        lambda *_args: "a" * 64,
    )
    monkeypatch.setattr(
        builder_layer,
        "commit_selected_authority",
        lambda _folder, _selected, *, mutation, **_kwargs: mutation(),
    )
    monkeypatch.setattr(builder_layer, "_prior_layer_paths", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        builder_layer,
        "load_unit_state",
        lambda *_args: {
            "units": {
                unit.id: {
                    "status": "passed",
                    "completion_receipt": {"fixture": True},
                }
            }
        },
    )
    monkeypatch.setattr(
        builder_layer.UnitCompletionReceipt,
        "parse",
        lambda *_args, **_kwargs: SimpleNamespace(receipt_digest="e" * 64),
    )
    monkeypatch.setattr(
        builder_layer,
        "resolve_completed_unit",
        lambda *_args, **_kwargs: events.append("unit_receipt_verified"),
    )
    monkeypatch.setattr(builder_layer, "plan_strips", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        builder_layer,
        "current_layer_finalization_receipt",
        lambda *_args, **_kwargs: terminal_receipt,
    )

    def reconcile(*_args, **_kwargs):
        events.append("terminal_reconciled")
        return SimpleNamespace(
            revalidation={"kept": 0, "dropped": []},
            finding=None,
            outcome=tmp_path / "plans/outcomes/layer-31.json",
            ledger=terminal_ledger,
        )

    monkeypatch.setattr(builder_layer, "reconcile_layer_finalization", reconcile)
    monkeypatch.setattr(
        builder_layer,
        "claim_layer_finalization",
        lambda *_args, **_kwargs: pytest.fail("restart must not claim or execute"),
    )
    monkeypatch.setattr(
        builder_layer,
        "ensure_axes",
        lambda *_args, **_kwargs: pytest.fail("restart must not load axes or judge"),
    )

    async def run():
        return await builder_layer.build_layer(
            shot,
            layer,
            session=None,
            verbose=False,
            selected_authority=selected,
        )

    assert anyio.run(run) is terminal_ledger
    assert events == ["unit_receipt_verified", "terminal_reconciled"]


def test_build_layer_resolves_once_and_rejects_a_hybrid_layer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unit = _unit("proxy")
    authoritative = Layer(
        id="1",
        script=unit.mutates.script_spans[0],
        title="authoritative",
        judges=((unit.evaluation.primary_judge, unit.evaluation.judges[0].ref),),
        reads="proxy",
        owns=(unit.evaluation.claims[0].axis,),
        primary_judge=unit.evaluation.primary_judge,
        stages=(unit,),
    )
    stale = replace(authoritative, title="stale caller layer")
    selected = ResolvedSelectedAuthority(
        assertion=SelectedAuthorityAssertionV2("absent", None, None),
        pointer_observation=AuthorityPointerObservation(None, None),
        selection_token=AuthoritySelectionToken(0, None, 0, None),
        plan=None,
        artifact_paths={},
    )
    resolutions = 0

    def resolve(_folder):
        nonlocal resolutions
        resolutions += 1
        return selected

    monkeypatch.setattr(builder_layer, "resolve_selected_authority", resolve)
    monkeypatch.setattr(
        builder_layer,
        "load_layers",
        lambda *_args, **_kwargs: {"1": authoritative},
    )

    async def run() -> None:
        with pytest.raises(ValueError, match="does not exactly match"):
            await builder_layer.build_layer(
                SimpleNamespace(folder=tmp_path),
                stale,
                session=None,
                verbose=False,
            )

    anyio.run(run)
    assert resolutions == 1


def test_build_layer_aba_token_change_refuses_first_durable_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unit = _unit("proxy")
    layer = Layer(
        id="1",
        script="build/01_proxy.py",
        title="proxy",
        judges=((unit.evaluation.primary_judge, unit.evaluation.judges[0].ref),),
        reads="proxy",
        owns=(unit.evaluation.claims[0].axis,),
        primary_judge=unit.evaluation.primary_judge,
        stages=(unit,),
    )
    layers_path = tmp_path / "selected" / "layers.json"
    layers_path.parent.mkdir(parents=True)
    layers_path.write_text('{"schema":5,"layers":[]}\n', encoding="utf-8")
    selected = ResolvedSelectedAuthority(
        assertion=SelectedAuthorityAssertionV2(
            "selected",
            SelectedAuthorityBundle("a" * 64, "clean", "b" * 64),
            SelectedAuthorityView("bundle", "a" * 64, "c" * 64),
        ),
        pointer_observation=AuthorityPointerObservation("d" * 64, None),
        selection_token=AuthoritySelectionToken(1, "d" * 64, 0, None),
        plan=SimpleNamespace(bundle=SimpleNamespace(content_hash="a" * 64)),
        artifact_paths={"layers.json": layers_path},
    )
    initialize(tmp_path, "1", (unit,), plan_hash="e" * 64)
    state_before = load_unit_state(tmp_path, "1")
    claim_calls: list[str] = []
    claim_for_planning = builder_layer.claim_ready_unit_for_planning

    def claim_with_probe(*args, **kwargs):
        claim_calls.append(str(args[2]))
        return claim_for_planning(*args, **kwargs)

    monkeypatch.setattr(
        builder_layer,
        "load_layers",
        lambda *_args, **_kwargs: {"1": layer},
    )
    monkeypatch.setattr(
        builder_layer,
        "selected_layer_capsule_digest",
        lambda *_args: "e" * 64,
    )
    monkeypatch.setattr(builder_layer, "_prior_layer_paths", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(builder_layer, "plan_strips", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(builder_layer, "require_due_clear", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        builder_layer,
        "claim_ready_unit_for_planning",
        claim_with_probe,
    )

    async def run() -> None:
        with pytest.raises(ValueError, match="authority selection changed"):
            await builder_layer.build_layer(
                SimpleNamespace(folder=tmp_path),
                layer,
                session=None,
                verbose=False,
                selected_authority=selected,
            )

    anyio.run(run)
    assert claim_calls == [unit.id]
    assert load_unit_state(tmp_path, "1") == state_before


def test_falsification_of_a_superseded_layer_capsule_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shot, layout, finding, _unit_record = _fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(
        builder_stops.authority_capsule_resolution,
        "selected_layer_capsule_digest",
        lambda _folder, _layer_id, _selected: hashlib.sha256(b"replacement capsule").hexdigest(),
    )
    with pytest.raises(ValueError, match="superseded selected layer capsule"):
        builder_stops.compile_hypothesis_falsification_stop(shot, layout, finding)
