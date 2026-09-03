"""The receipt-backed run controller dispatches exactly the amendment a stop names (ADR-0010).

Room run 20260903T100335Z-fa5dbb stopped with a typed ``publish_validated_amendment`` for
layer 2 and waited for an operator to type ``vfx plan --layer 2 --rematerialize --evidence``.
These tests pin the controller that performs that dispatch: the receipt chain, the commit
proof, the independent evaluation, convergence on identity, caps, ownership refusals, and
crash reconciliation between the commit and the ledger row.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.run_owner_support import owned_run
from vfx_harness.application import run_controller
from vfx_harness.domain.stop_amendment_transactions import (
    PublishValidatedAmendmentTarget,
    SelectedAuthorityAmendmentCommitted,
)
from vfx_harness.domain.stop_envelope_primitives import canonical_digest
from vfx_harness.domain.stop_envelopes import StopCause, StopEnvelope, StopIdentity
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
    StopAction,
)
from vfx_harness.domain.unit_outcomes import HypothesisFalsification
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration import controller_state, transaction_receipts

LAYER = "2"
UNIT = "streetlight_flicker"
OWNER = "layer-plan-authority"
GATE_POLICY = "structural-authority/runtime-falsification-v1"
GATE_SCHEMA = "vfx-harness.plan-gate/v1"
EVIDENCE_SCHEMA = "vfx-harness.builder-authority-stop-evidence/v1"
AUDIT_SCHEMA = "vfx-harness.builder-authority-stop-audit/v1"


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def authority(view_seed: str) -> SelectedAuthorityAssertionV2:
    return SelectedAuthorityAssertionV2(
        selection="selected",
        bundle=SelectedAuthorityBundle(
            digest=_digest("bundle"),
            outcome="clean",
            semantic_manifest_digest=_digest("bundle-manifest"),
        ),
        effective_view=SelectedAuthorityView(
            source="jit",
            digest=_digest(f"view-{view_seed}"),
            semantic_manifest_digest=_digest(f"view-manifest-{view_seed}"),
        ),
    )


def write_finding(
    shot: Path,
    *,
    name: str = "hf-test",
    fault_owner_units: tuple[str, ...] = (),
    decisions: list[dict] | None = None,
) -> Path:
    directory = shot / "state" / "hypothesis-falsifications"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.json"
    path.write_text(
        json.dumps(
            {
                "schema": "vfx-harness.hypothesis-falsification/v1",
                "record_id": name,
                "recorded_at": "2026-09-03T10:52:23+00:00",
                "layer": LAYER,
                "unit": UNIT,
                "identities": {
                    key: _digest(key)
                    for key in (
                        "bundle_hash",
                        "plan_hash",
                        "unit_hash",
                        "unit_plan_hash",
                        "candidate_hash",
                        "settings_hash",
                    )
                },
                "contract_ids": ["streetlight-flicker-schedule"],
                "observations": [
                    {
                        "classification": "unsatisfiable_in_scope",
                        "contract_ids": ["streetlight-flicker-schedule"],
                        "reason": "data.energy has no Light carrier",
                    }
                ],
                "decisions": decisions or [],
                "conflict": {
                    "kind": "contract",
                    "required_authority": "publish amended authority with a Light carrier",
                    "roles": ["exterior.lightctrl.streetlight_flicker"],
                    "controls": ["ground_streetlight_flicker"],
                },
                "evidence": [f"build/units/02/{UNIT}.py"],
                "affected": ["ground_island", UNIT],
                "fault_owner_units": list(fault_owner_units),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def amendment_envelope(
    layout: run_artifacts.RunLayout,
    *,
    finding_path: Path,
    view_seed: str = "before",
    fingerprint_seed: str = "facts",
    attempt_seed: str = "attempt",
) -> StopEnvelope:
    """A builder authority-defect stop shaped like the one run fa5dbb published."""

    base = authority(view_seed)
    document = {
        "schema": EVIDENCE_SCHEMA,
        "evidence_kind": "authority_defect",
        "finding_record": {"record_id": "hf-semantic-test", "seed": fingerprint_seed},
        "attempt": attempt_seed,
    }
    evidence_path = layout.write_report("builder-authority-stop-evidence", document)
    evidence = StopEvidenceRef(
        kind="stop_evidence",
        locator=layout.relative(evidence_path),
        sha256=hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
        record_schema=EVIDENCE_SCHEMA,
        record_digest=canonical_digest(document),
    )
    layout.write_report(
        "builder-authority-stop-audit",
        {
            "schema": AUDIT_SCHEMA,
            "run_id": layout.run_id,
            "evidence_ref": evidence.as_dict(),
            "audit_locators": {
                "source_finding": {
                    "artifact": layout.relative(finding_path),
                    "artifact_sha256": hashlib.sha256(finding_path.read_bytes()).hexdigest(),
                }
            },
        },
    )
    finding = EvidenceRecordAssertion(
        record_kind="finding",
        record_id="hf-semantic-test",
        evidence=evidence,
    )
    target = PublishValidatedAmendmentTarget(
        scope="layer_view",
        base_authority=base,
        layer_id=LAYER,
        findings=(finding,),
        owner_authority_id=OWNER,
        gate_policy_id=GATE_POLICY,
        gate_schema=GATE_SCHEMA,
        validation_scope="structural_authority",
    )
    action = StopAction(
        target=target,
        postcondition=SelectedAuthorityAmendmentCommitted(
            scope="layer_view",
            base_authority_digest=base.digest,
            layer_id=LAYER,
            finding_ids=("hf-semantic-test",),
            owner_authority_id=OWNER,
            gate_policy_id=GATE_POLICY,
            gate_schema=GATE_SCHEMA,
            validation_scope="structural_authority",
            required_after_source="jit",
        ),
    )
    return StopEnvelope(
        stage="builder",
        stop_class="authority_defect",
        identity=StopIdentity(
            run_id=layout.run_id,
            bundle_digest=base.bundle.digest,
            view_digest=base.effective_view.digest,
            layer_id=LAYER,
            unit_id=UNIT,
            unit_plan_digest=_digest("unit-plan"),
            unit_digest=_digest("unit"),
            candidate_digest=_digest("candidate"),
            checkpoint_digest=None,
            settings_digest=_digest("settings"),
            debt_state_digest=None,
        ),
        cause=StopCause(
            invariant_id="executable_hypothesis_requires_authority_change",
            finding_ids=("hf-cause-test",),
            owner_scope_ids=(f"{LAYER}:{UNIT}",),
            normalized_facts_digest=_digest(fingerprint_seed),
        ),
        attempt_evidence_digest=_digest(attempt_seed),
        classification_evidence_digest=_digest("classification"),
        artifact_state_digest=_digest("artifact"),
        authoritative_before_digest=base.digest,
        actions=(action,),
        evidence_refs=(evidence,),
        budget_key="builder-authority-amendment",
        expected="The selected unit authority can satisfy its contracts.",
        found=f"Executable finding proves a contract authority conflict for {LAYER}.{UNIT}.",
        next_action="Publish amended layer authority.",
    )


class Harness:
    """Fake authority, heads, and layer DAG around a real run layout and receipt store."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, layout) -> None:
        self.shot = SimpleNamespace(folder=tmp_path, id="controller-shot")
        self.layout = layout
        self.commands: list[list[str]] = []
        self.publish_on_run = True
        self.exit_code = 0
        self.published = False
        monkeypatch.setattr(
            run_controller.authority_selection,
            "resolve_selected_authority",
            lambda _shot: SimpleNamespace(assertion=authority("after" if self.published else "before")),
        )
        monkeypatch.setattr(
            run_controller.authority_selection_heads,
            "read_authority_selection_heads",
            lambda _shot: SimpleNamespace(jit=SimpleNamespace(materialized_layers=("1", LAYER))),
        )
        monkeypatch.setattr(
            run_controller.ledger,
            "load_layers",
            lambda _shot, selected_authority=None: {
                LAYER: SimpleNamespace(
                    stages=[SimpleNamespace(id=UNIT), SimpleNamespace(id="ground_island")]
                )
            },
        )

    def run_stage(self, command: list[str]) -> int:
        self.commands.append(list(command))
        if self.publish_on_run and self.exit_code == 0:
            self.published = True
        return self.exit_code

    def controller(self, **caps) -> run_controller.RunController:
        return run_controller.RunController(
            self.shot,
            self.layout,
            run_controller.ControllerCaps(
                max_dispatches=caps.get("max_dispatches", 6),
                max_replans_per_layer=caps.get("max_replans_per_layer", 2),
                max_usd=caps.get("max_usd"),
            ),
            run_stage=self.run_stage,
            blender="blender",
            python="python",
        )


def test_dispatch_rematerializes_the_layer_and_proves_the_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    with owned_run(tmp_path, "controller-run", command="run", dispatch_kind="driver") as (layout, _lease):
        harness = Harness(tmp_path, monkeypatch, layout)
        finding = write_finding(tmp_path)
        envelope = amendment_envelope(layout, finding_path=finding)
        layout.write_stop_envelope(envelope)

        result = harness.controller().dispatch(envelope)

        assert isinstance(result, run_controller.Dispatched), result
        assert result.layer_id == LAYER and result.resume_layer == LAYER
        [command] = harness.commands
        assert "vfx_harness.agents.planner" in command
        assert command[command.index("--layer") + 1] == LAYER
        assert "--rematerialize" in command
        assert command[command.index("--owner") + 1] == OWNER
        assert command[command.index("--evidence") + 1] == layout.relative(finding)
        assert "hf-cause-test" in command[command.index("--trigger") + 1]

        chain = transaction_receipts.transaction_receipt_chain(tmp_path, result.idempotency_key)
        assert [receipt.phase for receipt in chain] == ["prepared", "running", "terminal"]
        assert chain[-1].terminal_outcome == "committed"
        assert chain[-1].adapter_id == run_controller.ADAPTER_ID
        commit = controller_state.read_commit(tmp_path, result.idempotency_key)
        assert commit is not None and commit.layer_id == LAYER
        assert commit.before_authority_digest == envelope.authoritative_before_digest
        assert commit.after_authority_digest == authority("after").digest
        assert chain[-1].commit_marker == controller_state.commit_evidence_ref(tmp_path, commit)
        evaluation = controller_state.read_evaluation(tmp_path, result.idempotency_key)
        assert evaluation is not None and evaluation.satisfied
        assert evaluation.digest == result.evaluation_digest
        [attempt] = controller_state.read_attempts(tmp_path)
        assert attempt.cause_fingerprint == envelope.cause_fingerprint

        # The consumed envelope is archived, the run's slot is free for the next stage,
        # and the ledger row binds envelope, receipt, and evaluation by digest.
        assert not layout.stop_envelope.exists()
        [row] = run_controller.ledger_rows(layout)
        assert row["consumed_envelope_digest"] == envelope.digest
        archived = tmp_path / row["consumed_envelope"]
        assert StopEnvelope.from_dict(json.loads(archived.read_text()), "archived").digest == envelope.digest
        assert row["receipt_digest"] == chain[-1].digest
        assert row["evaluation_digest"] == evaluation.digest
        assert row["transaction_id"] == "publish_validated_amendment"


def test_failed_rematerialization_is_a_failed_receipt_not_a_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    with owned_run(tmp_path, "controller-fail", command="run", dispatch_kind="driver") as (layout, _lease):
        harness = Harness(tmp_path, monkeypatch, layout)
        harness.exit_code = 3
        envelope = amendment_envelope(layout, finding_path=write_finding(tmp_path))
        layout.write_stop_envelope(envelope)

        result = harness.controller().dispatch(envelope)

        assert isinstance(result, run_controller.DispatchRefusal)
        assert result.reason == "adapter_failed"
        key = transaction_receipts.transaction_receipt_chain
        [row_path] = sorted(layout.reports.glob("controller-adapter-failure-*.json"))
        assert json.loads(row_path.read_text())["exit_code"] == 3
        chains = list((run_artifacts.shot_state_dir(tmp_path)).rglob("*"))
        assert chains, "a receipt chain must exist for the failed attempt"
        assert not layout.stop_envelope.exists(), "the consumed envelope was archived"
        assert run_controller.ledger_rows(layout) == []
        assert controller_state.read_attempts(tmp_path) == ()
        del key

        # A second call with the same envelope does not re-run the child: the chain ended.
        again = harness.controller().dispatch(envelope)
        assert isinstance(again, run_controller.DispatchRefusal) and again.reason == "adapter_failed"
        assert len(harness.commands) == 1


def test_repeated_finding_converges_instead_of_dispatching_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    with owned_run(tmp_path, "controller-repeat", command="run", dispatch_kind="driver") as (layout, _lease):
        harness = Harness(tmp_path, monkeypatch, layout)
        finding = write_finding(tmp_path)
        first = amendment_envelope(layout, finding_path=finding)
        layout.write_stop_envelope(first)
        controller = harness.controller()
        assert isinstance(controller.dispatch(first), run_controller.Dispatched)

        # The replacement view was built and produced the same finding again.
        harness.published = False
        second = amendment_envelope(
            layout, finding_path=finding, view_seed="after", attempt_seed="attempt-2"
        )
        layout.write_stop_envelope(second)
        result = controller.dispatch(second)

        assert isinstance(result, run_controller.DispatchRefusal)
        assert result.reason == "repeated_finding"
        assert len(harness.commands) == 1
        assert layout.stop_envelope.exists(), "a refused envelope stays terminal"


def test_caps_refuse_before_any_spend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    with owned_run(tmp_path, "controller-caps", command="run", dispatch_kind="driver") as (layout, _lease):
        harness = Harness(tmp_path, monkeypatch, layout)
        finding = write_finding(tmp_path)
        first = amendment_envelope(layout, finding_path=finding)
        layout.write_stop_envelope(first)
        controller = harness.controller(max_dispatches=1)
        assert isinstance(controller.dispatch(first), run_controller.Dispatched)

        harness.published = False
        distinct = amendment_envelope(
            layout, finding_path=finding, view_seed="after", fingerprint_seed="other-facts"
        )
        layout.write_stop_envelope(distinct)
        result = controller.dispatch(distinct)
        assert isinstance(result, run_controller.DispatchRefusal)
        assert result.reason == "budget_exhausted" and "dispatch cap" in result.detail

        # Per-layer replan cap reads the shot's durable attempts, not this run's memory.
        fresh = harness.controller(max_dispatches=5, max_replans_per_layer=1)
        result = fresh.dispatch(distinct)
        assert isinstance(result, run_controller.DispatchRefusal)
        assert result.reason == "budget_exhausted" and "layer 2" in result.detail

        # The USD ceiling reads the run's cost log before dispatching.
        (layout.logs / "cost.jsonl").write_text(
            json.dumps({"run_id": layout.run_id, "session_id": "s1", "cost_usd": 4.5}) + "\n"
            + json.dumps({"run_id": layout.run_id, "session_id": "s1", "cost_usd": 6.0}) + "\n"
            + json.dumps({"run_id": layout.run_id, "session_id": "s2", "cost_usd": 1.0}) + "\n",
            encoding="utf-8",
        )
        assert run_controller.run_spend_usd(layout) == 7.0
        capped = harness.controller(max_dispatches=5, max_replans_per_layer=5, max_usd=7.0)
        result = capped.dispatch(distinct)
        assert isinstance(result, run_controller.DispatchRefusal)
        assert result.reason == "budget_exhausted" and "ceiling" in result.detail
        assert len(harness.commands) == 1


def test_out_of_layer_owner_and_hard_constraint_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    with owned_run(tmp_path, "controller-owner", command="run", dispatch_kind="driver") as (layout, _lease):
        harness = Harness(tmp_path, monkeypatch, layout)
        finding = write_finding(tmp_path, fault_owner_units=("camera_rig",))
        envelope = amendment_envelope(layout, finding_path=finding)
        layout.write_stop_envelope(envelope)

        result = harness.controller().dispatch(envelope)
        assert isinstance(result, run_controller.DispatchRefusal)
        assert result.reason == "out_of_layer_owner" and "camera_rig" in result.detail

        monkeypatch.setattr(HypothesisFalsification, "changes_hard_constraint", property(lambda self: True))
        in_layer = write_finding(tmp_path, name="hf-hard")
        layout.stop_envelope.unlink()  # the refused envelope stayed terminal; clear it for the next case
        envelope = amendment_envelope(layout, finding_path=in_layer, fingerprint_seed="hard")
        layout.write_stop_envelope(envelope)
        result = harness.controller().dispatch(envelope)
        assert isinstance(result, run_controller.DispatchRefusal)
        assert result.reason == "hard_constraint"
        assert harness.commands == []


def test_crash_between_commit_and_ledger_row_is_reconciled_without_a_second_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    with owned_run(tmp_path, "controller-crash", command="run", dispatch_kind="driver") as (layout, _lease):
        harness = Harness(tmp_path, monkeypatch, layout)
        envelope = amendment_envelope(layout, finding_path=write_finding(tmp_path))
        layout.write_stop_envelope(envelope)
        original = run_controller.evaluate_rematerialization
        crashes = {"left": 1}

        def crashing_evaluate(*args, **kwargs):
            if crashes["left"]:
                crashes["left"] -= 1
                raise RuntimeError("process died after the adapter commit")
            return original(*args, **kwargs)

        monkeypatch.setattr(run_controller, "evaluate_rematerialization", crashing_evaluate)
        with pytest.raises(RuntimeError, match="process died"):
            harness.controller().dispatch(envelope)
        assert run_controller.ledger_rows(layout) == []
        assert controller_state.read_attempts(tmp_path) == ()

        # The next controller (a new run's or this one's) finds the committed chain and
        # records it; the rematerialization is neither repeated nor lost.
        result = harness.controller().dispatch(envelope)
        assert isinstance(result, run_controller.Dispatched)
        assert len(harness.commands) == 1
        [row] = run_controller.ledger_rows(layout)
        assert row["idempotency_key"] == result.idempotency_key
        assert len(controller_state.read_attempts(tmp_path)) == 1


def test_non_dispatchable_actions_stay_terminal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    with owned_run(tmp_path, "controller-human", command="run", dispatch_kind="driver") as (layout, _lease):
        harness = Harness(tmp_path, monkeypatch, layout)
        question_digest = _digest("question")
        document = {"schema": "vfx-harness.acceptance-question/v1", "question_digest": question_digest}
        path = layout.write_report("acceptance-question", document)
        evidence = StopEvidenceRef(
            kind="stop_evidence",
            locator=layout.relative(path),
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            record_schema=document["schema"],
            record_digest=canonical_digest(document),
        )
        target = EscalateQuestionTarget(
            question_record=EvidenceRecordAssertion(record_kind="question", record_id="q-1", evidence=evidence),
            question_digest=question_digest,
            decision_authority_id="acceptance-review",
            decision_schema="vfx-harness.acceptance-decision/v1",
            allowed_answer_ids=("abstain", "approve"),
            evidence=(evidence,),
        )
        envelope = StopEnvelope(
            stage="acceptance",
            stop_class="human_decision_required",
            identity=StopIdentity(
                run_id=layout.run_id,
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
                normalized_facts_digest=_digest("facts"),
            ),
            attempt_evidence_digest=_digest("attempt"),
            classification_evidence_digest=_digest("classification"),
            artifact_state_digest=_digest("artifact"),
            authoritative_before_digest=_digest("before"),
            actions=(
                StopAction(
                    target=target,
                    postcondition=HumanDecisionCommitted(
                        question_digest=question_digest,
                        decision_authority_id=target.decision_authority_id,
                        decision_schema=target.decision_schema,
                        allowed_answer_ids=target.allowed_answer_ids,
                    ),
                ),
            ),
            evidence_refs=(evidence,),
            budget_key="acceptance-decision",
            expected="Every required acceptance moment passes.",
            found="Acceptance moment M1 failed.",
            next_action="Resolve its exact repair authority.",
        )
        result = harness.controller().dispatch(envelope)
        assert isinstance(result, run_controller.DispatchRefusal)
        assert result.reason == "not_dispatchable"
        assert harness.commands == []
