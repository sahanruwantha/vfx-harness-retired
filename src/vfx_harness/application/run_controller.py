"""Receipt-backed run controller: dispatch the one transaction a stop envelope names.

`vfx run` used to stop at the first unaccepted boundary and hand a closed stop envelope to
an operator, who read the record and typed the command it named (`vfx plan --layer N
--rematerialize --evidence <finding>`, then `vfx run --from N`). Every one of those actions
was mechanical and fully determined by the typed record (ADR-0010). The controller performs
exactly that dispatch, and nothing inferred: it consumes the envelope's single typed action,
runs the same adapter the operator command uses, proves the commit through an independent
evaluator, records a controller ledger row under the run, and lets the driver continue from
the earliest legal layer.

Only `publish_validated_amendment` on a layer view is dispatchable here. A finding that
changes a hard constraint is already an `escalate_question`, an out-of-layer fault owner is
refused, an identical finding dispatched before converges instead of repeating, and every
cap exhaustion is a typed refusal. Refusals leave the envelope where it was: the driver then
selects it as the run's terminal stop exactly as before.
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vfx_harness.domain.controller_commits import RematerializationCommit
from vfx_harness.domain.stop_amendment_transactions import PublishValidatedAmendmentTarget
from vfx_harness.domain.stop_envelope_primitives import canonical_digest
from vfx_harness.domain.stop_envelopes import (
    PriorDispatchAttempt,
    StopEnvelope,
    classify_stop,
    repeated_dispatch_defect_document,
)
from vfx_harness.domain.stop_transaction_state import StopEvidenceRef
from vfx_harness.domain.stop_transactions import (
    PostconditionEvaluation,
    StopAction,
    action_idempotency_key,
)
from vfx_harness.domain.transaction_receipts import TransactionReceipt
from vfx_harness.domain.unit_outcomes import (
    HypothesisFalsification,
    load_hypothesis_falsification,
)
from vfx_harness.infrastructure.config import Settings
from vfx_harness.observability import run_artifacts
from vfx_harness.observability.log import log
from vfx_harness.orchestration import (
    authority_selection,
    authority_selection_heads,
    controller_state,
    ledger,
    transaction_receipts,
)

ADAPTER_ID = "run_controller"
DISPATCHABLE_TRANSACTIONS = frozenset({"publish_validated_amendment"})
LEDGER_SCHEMA = "vfx-harness.controller-dispatch/v1"
CONSUMED_STOP_REPORT = "controller-consumed-stop-{index:02d}"
DISPATCH_REPORT = "controller-dispatch-{index:02d}"
REPEATED_DEFECT_REPORT = "controller-repeated-dispatch-{index:02d}"
ADAPTER_FAILURE_REPORT = "controller-adapter-failure-{index:02d}"
ADAPTER_FAILURE_SCHEMA = "vfx-harness.controller-adapter-failure/v1"
BUILDER_STOP_AUDIT_REPORT = "builder-authority-stop-audit"
REFUSAL_REASONS = frozenset(
    {
        "not_dispatchable",
        "out_of_layer_owner",
        "hard_constraint",
        "repeated_finding",
        "budget_exhausted",
        "evidence_unavailable",
        "adapter_failed",
    }
)


@dataclass(frozen=True, slots=True)
class ControllerCaps:
    """Spend bounds the controller enforces after identity-based convergence."""

    max_dispatches: int
    max_replans_per_layer: int
    max_usd: float | None

    def __post_init__(self) -> None:
        if int(self.max_dispatches) < 1 or int(self.max_replans_per_layer) < 1:
            raise ValueError("controller caps must allow at least one dispatch")
        if self.max_usd is not None and float(self.max_usd) <= 0.0:
            raise ValueError("controller USD cap must be positive")

    @classmethod
    def from_settings(cls, settings: Settings) -> ControllerCaps:
        return cls(
            max_dispatches=settings.run_max_dispatches,
            max_replans_per_layer=settings.run_max_replans_per_layer,
            max_usd=settings.run_max_usd,
        )


@dataclass(frozen=True, slots=True)
class DispatchRefusal:
    """Why the envelope stays terminal; ``reason`` is one closed id."""

    reason: str
    detail: str

    def __post_init__(self) -> None:
        if self.reason not in REFUSAL_REASONS:
            raise ValueError(f"unknown controller refusal reason {self.reason!r}")


@dataclass(frozen=True, slots=True)
class Dispatched:
    """One committed, evaluated, ledger-recorded transaction the driver may continue from."""

    index: int
    transaction_id: str
    layer_id: str
    idempotency_key: str
    receipt_digest: str
    evaluation_digest: str
    resume_layer: str
    ledger_report: str


def run_spend_usd(layout: run_artifacts.RunLayout) -> float:
    """Model spend recorded for this run so far, one maximum snapshot per session."""

    path = layout.logs / "cost.jsonl"
    if not path.is_file():
        return 0.0
    sessions: dict[str, float] = {}
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict) or row.get("run_id") not in (None, layout.run_id):
            continue
        key = str(row.get("session_id") or f"legacy-row-{index}")
        cost = float(row.get("cost_usd") or 0.0)
        sessions[key] = max(sessions.get(key, 0.0), cost)
    return sum(sessions.values())


def ledger_rows(layout: run_artifacts.RunLayout) -> list[dict[str, Any]]:
    """Every controller dispatch row this run recorded, in dispatch order."""

    rows: list[dict[str, Any]] = []
    if not layout.reports.is_dir():
        return rows
    for path in sorted(layout.reports.glob("controller-dispatch-*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"controller ledger row {path.name} is unreadable: {exc}") from exc
        if not isinstance(value, dict) or value.get("schema") != LEDGER_SCHEMA:
            raise ValueError(f"controller ledger row {path.name} has an unsupported schema")
        rows.append(value)
    return rows


class RunController:
    """Dispatch receipt-backed transactions for one owned run."""

    def __init__(
        self,
        shot,
        layout: run_artifacts.RunLayout,
        caps: ControllerCaps,
        *,
        run_stage: Callable[[list[str]], int],
        blender: str,
        python: str | None = None,
    ) -> None:
        self.shot = shot
        self.layout = layout
        self.caps = caps
        self._run_stage = run_stage
        self.blender = blender
        self.python = python or sys.executable
        self.rows: list[dict[str, Any]] = ledger_rows(layout)

    # -- decision ---------------------------------------------------------------------

    def dispatch(self, envelope: StopEnvelope) -> Dispatched | DispatchRefusal:
        action = envelope.actions[0]
        if action.transaction_id not in DISPATCHABLE_TRANSACTIONS:
            return DispatchRefusal(
                "not_dispatchable",
                f"{action.transaction_id} has no receipt-backed controller adapter; the envelope "
                "remains the run's terminal stop",
            )
        target = action.target
        if not isinstance(target, PublishValidatedAmendmentTarget):
            return DispatchRefusal("not_dispatchable", "amendment action carries an unexpected target")
        if target.scope != "layer_view" or target.layer_id is None:
            return DispatchRefusal(
                "not_dispatchable",
                "global-plan amendments stay a reviewed operator transaction (ADR-0010)",
            )
        layer_id = target.layer_id
        refusal = self._cap_refusal(layer_id)
        if refusal is not None:
            return refusal
        attempts = controller_state.read_attempts(self.shot.folder)
        refusal = self._convergence_refusal(envelope, attempts)
        if refusal is not None:
            return refusal
        try:
            finding, evidence = self._finding_evidence(envelope)
        except ValueError as exc:
            return DispatchRefusal("evidence_unavailable", str(exc))
        if finding is not None:
            refusal = self._ownership_refusal(finding, layer_id)
            if refusal is not None:
                return refusal
        key = action_idempotency_key(
            action,
            authoritative_before_digest=envelope.authoritative_before_digest,
            attempt_evidence_digest=envelope.attempt_evidence_digest,
        )
        with controller_state.controller_lock(self.shot.folder, key):
            return self._dispatch_locked(envelope, action, target, key, evidence)

    def _cap_refusal(self, layer_id: str) -> DispatchRefusal | None:
        if len(self.rows) >= self.caps.max_dispatches:
            return DispatchRefusal(
                "budget_exhausted",
                f"run dispatch cap {self.caps.max_dispatches} reached",
            )
        attempts = controller_state.read_attempts(self.shot.folder)
        same_layer = sum(
            1
            for attempt in attempts
            if isinstance(attempt.action.target, PublishValidatedAmendmentTarget)
            and attempt.action.target.layer_id == layer_id
        )
        if same_layer >= self.caps.max_replans_per_layer:
            return DispatchRefusal(
                "budget_exhausted",
                f"layer {layer_id} already rematerialized {same_layer} time(s) by the controller; "
                f"cap {self.caps.max_replans_per_layer}",
            )
        if self.caps.max_usd is not None:
            spend = run_spend_usd(self.layout)
            if spend >= self.caps.max_usd:
                return DispatchRefusal(
                    "budget_exhausted",
                    f"run spend ${spend:.2f} reached the ${self.caps.max_usd:.2f} ceiling",
                )
        return None

    def _convergence_refusal(
        self,
        envelope: StopEnvelope,
        attempts: Sequence[PriorDispatchAttempt],
    ) -> DispatchRefusal | None:
        try:
            classify_stop(envelope, attempts)
        except ValueError as exc:
            prior = next(
                attempt
                for attempt in attempts
                if attempt.cause_fingerprint == envelope.cause_fingerprint
                and attempt.authoritative_before_digest == envelope.authoritative_before_digest
                and not attempt.evaluation.satisfied
            )
            document = repeated_dispatch_defect_document(envelope, prior)
            self.layout.write_report(REPEATED_DEFECT_REPORT.format(index=len(self.rows) + 1), document)
            return DispatchRefusal("repeated_finding", f"{exc}; defect {document['defect_id']}")
        repeated = [
            attempt for attempt in attempts if attempt.cause_fingerprint == envelope.cause_fingerprint
        ]
        if repeated:
            return DispatchRefusal(
                "repeated_finding",
                "a finding with this exact cause fingerprint was already dispatched "
                f"({repeated[0].evaluation.idempotency_key[:16]}); the replacement authority "
                "re-authored the same defect, so the run converges instead of paying again",
            )
        return None

    def _finding_evidence(
        self,
        envelope: StopEnvelope,
    ) -> tuple[HypothesisFalsification | None, tuple[str, ...]]:
        """The exact evidence locators the rematerialization must cite."""

        if envelope.stage in {"materialization", "plan_gate"}:
            # Neither stage cites a hypothesis falsification: the deterministic gate's own
            # blocking findings are the evidence, and ownership was already settled when
            # the stop chose its scope.  Only a builder stop carries a finding to check.
            return None, tuple(item.locator for item in envelope.evidence_refs)
        audit_path = self.layout.reports / f"{BUILDER_STOP_AUDIT_REPORT}.json"
        try:
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"builder authority stop audit {audit_path.name} is unreadable: {exc}"
            ) from exc
        evidence_ref = audit.get("evidence_ref") if isinstance(audit, dict) else None
        cited = {item.record_digest for item in envelope.evidence_refs}
        if not isinstance(evidence_ref, dict) or evidence_ref.get("record_digest") not in cited:
            raise ValueError("builder authority stop audit does not describe this envelope's evidence")
        source = (audit.get("audit_locators") or {}).get("source_finding") or {}
        artifact = source.get("artifact")
        expected_sha = source.get("artifact_sha256")
        if not isinstance(artifact, str) or not isinstance(expected_sha, str):
            raise ValueError("builder authority stop audit names no source finding artifact")
        shot_root = Path(self.shot.folder).resolve()
        path = (shot_root / artifact).resolve()
        path.relative_to(shot_root)
        if not path.is_file():
            raise ValueError(f"source finding {artifact} is missing")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha:
            raise ValueError(f"source finding {artifact} changed since the stop was compiled")
        finding = load_hypothesis_falsification(path)
        return finding, (artifact,)

    def _ownership_refusal(
        self,
        finding: HypothesisFalsification,
        layer_id: str,
    ) -> DispatchRefusal | None:
        if finding.layer != layer_id:
            return DispatchRefusal(
                "out_of_layer_owner",
                f"finding names layer {finding.layer} but the amendment targets layer {layer_id}",
            )
        if finding.changes_hard_constraint:
            return DispatchRefusal(
                "hard_constraint",
                "the finding changes a hard constraint; that decision is a human's",
            )
        selected = authority_selection.resolve_selected_authority(self.shot.folder)
        layers = ledger.load_layers(self.shot, selected_authority=selected)
        layer = layers.get(layer_id)
        if layer is None:
            return DispatchRefusal("out_of_layer_owner", f"selected DAG has no layer {layer_id}")
        stage_ids = {unit.id for unit in layer.stages}
        outside = sorted(set(finding.fault_owner_units) - stage_ids)
        if outside:
            return DispatchRefusal(
                "out_of_layer_owner",
                f"fault owners {', '.join(outside)} lie outside layer {layer_id}; the reviewed "
                "replacement must change their owning capsules (HIR-0154)",
            )
        return None

    # -- transaction -----------------------------------------------------------------

    def _dispatch_locked(
        self,
        envelope: StopEnvelope,
        action: StopAction,
        target: PublishValidatedAmendmentTarget,
        key: str,
        evidence: tuple[str, ...],
    ) -> Dispatched | DispatchRefusal:
        shot_root = self.shot.folder
        transaction_receipts.reconcile_transaction_receipt_crash_orphan(shot_root, key)
        chain = transaction_receipts.transaction_receipt_chain(shot_root, key)
        prepared = TransactionReceipt.prepare(
            action,
            adapter_id=ADAPTER_ID,
            authoritative_before_digest=envelope.authoritative_before_digest,
            attempt_evidence_digest=envelope.attempt_evidence_digest,
            recovery_disposition="query_external",
        )
        if chain:
            current = chain[-1]
            if not current.same_attempt_as(prepared):
                return DispatchRefusal(
                    "adapter_failed",
                    "the receipt chain for this key binds another attempt; refusing to reuse it",
                )
            if current.phase == "terminal":
                if current.terminal_outcome != "committed":
                    return DispatchRefusal(
                        "adapter_failed",
                        f"a prior controller attempt for this stop ended {current.terminal_outcome}",
                    )
                return self._record_committed(envelope, action, target, key, chain)
        else:
            current = transaction_receipts.publish_transaction_receipt(shot_root, prepared)
        if current.phase == "prepared":
            current = transaction_receipts.publish_transaction_receipt(
                shot_root, current.running_successor()
            )
        if current.phase != "running":
            return DispatchRefusal("adapter_failed", "controller receipt has an unsupported phase")
        commit = controller_state.read_commit(shot_root, key)
        if commit is None:
            self._consume_envelope(envelope)
            trigger = (
                f"controller dispatch of {', '.join(envelope.cause.finding_ids)}: {envelope.found}"
            )[:400]
            command = [
                self.python,
                "-m",
                "vfx_harness.agents.planner",
                str(shot_root),
                "--layer",
                target.layer_id,
                "--rematerialize",
                "--owner",
                target.owner_authority_id,
                "--trigger",
                trigger,
                "--blender",
                self.blender,
            ]
            for locator in evidence:
                command.extend(["--evidence", locator])
            log(f"──── controller · publish_validated_amendment · layer {target.layer_id} ────")
            rc = self._run_stage(command)
            after = authority_selection.resolve_selected_authority(shot_root)
            if rc != 0 or not self._published(after, envelope, target.layer_id):
                failure_ref = self._publish_adapter_failure(
                    envelope, target.layer_id, rc, after.assertion.digest
                )
                transaction_receipts.publish_transaction_receipt(
                    shot_root,
                    current.terminal_successor(
                        terminal_outcome="failed",
                        authoritative_after_digest=after.assertion.digest,
                        result_evidence=(failure_ref,),
                        recovery_disposition="halt_on_uncertainty",
                    ),
                )
                return DispatchRefusal(
                    "adapter_failed",
                    f"rematerialization of layer {target.layer_id} exited {rc} without "
                    "publishing a replacement view",
                )
            commit = RematerializationCommit.create(
                action=action,
                running_receipt=current,
                before_authority_digest=envelope.authoritative_before_digest,
                after_authority=after.assertion,
            )
            commit_ref = controller_state.publish_commit(shot_root, commit)
        else:
            commit_ref = controller_state.commit_evidence_ref(shot_root, commit)
        terminal = transaction_receipts.publish_transaction_receipt(
            shot_root,
            current.terminal_successor(
                terminal_outcome="committed",
                authoritative_after_digest=commit.after_authority_digest,
                result_evidence=(commit_ref,),
                commit_marker=commit_ref,
                recovery_disposition="reconcile_commit_only",
            ),
        )
        chain = transaction_receipts.transaction_receipt_chain(shot_root, key)
        if chain[-1] != terminal:
            raise RuntimeError("controller terminal receipt is not the selected head")
        return self._record_committed(envelope, action, target, key, chain)

    def _published(self, after, envelope: StopEnvelope, layer_id: str) -> bool:
        assertion = after.assertion
        if (
            assertion.selection != "selected"
            or assertion.effective_view is None
            or assertion.effective_view.source != "jit"
            or assertion.digest == envelope.authoritative_before_digest
        ):
            return False
        heads = authority_selection_heads.read_authority_selection_heads(self.shot.folder)
        return heads.jit is not None and layer_id in heads.jit.materialized_layers

    def _publish_adapter_failure(
        self,
        envelope: StopEnvelope,
        layer_id: str,
        exit_code: int,
        authority_after_digest: str,
    ):
        """Typed result evidence for a failed dispatch; the child's own stop stays terminal."""

        index = len(self.rows) + 1
        document = {
            "schema": ADAPTER_FAILURE_SCHEMA,
            "run_id": self.layout.run_id,
            "consumed_envelope_digest": envelope.digest,
            "layer_id": layer_id,
            "exit_code": int(exit_code),
            "authority_after_digest": authority_after_digest,
        }
        path = self.layout.write_report(ADAPTER_FAILURE_REPORT.format(index=index), document)
        return StopEvidenceRef(
            kind="run_report",
            locator=self.layout.relative(path),
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            record_schema=ADAPTER_FAILURE_SCHEMA,
            record_digest=canonical_digest(document),
        )

    def _consume_envelope(self, envelope: StopEnvelope) -> None:
        """Archive the dispatched envelope so the next stage can publish its own."""

        index = len(self.rows) + 1
        current = self.layout.read_stop_envelope()
        if current.digest != envelope.digest:
            raise RuntimeError("the run's stop envelope changed before the controller consumed it")
        self.layout.write_report(CONSUMED_STOP_REPORT.format(index=index), envelope.as_dict())
        self.layout.stop_envelope.unlink()

    # -- proof and ledger ----------------------------------------------------------------

    def _record_committed(
        self,
        envelope: StopEnvelope,
        action: StopAction,
        target: PublishValidatedAmendmentTarget,
        key: str,
        chain: tuple[TransactionReceipt, ...],
    ) -> Dispatched:
        evaluation = evaluate_rematerialization(
            self.shot.folder,
            envelope=envelope,
            chain=chain,
        )
        index = len(self.rows) + 1
        archived = self.layout.reports / f"{CONSUMED_STOP_REPORT.format(index=index)}.json"
        if not archived.is_file():
            # A crash between the adapter commit and the ledger row leaves the consumed
            # envelope unarchived only when the child never ran; archive it now.
            self.layout.write_report(CONSUMED_STOP_REPORT.format(index=index), envelope.as_dict())
            if self.layout.stop_envelope.exists():
                self.layout.stop_envelope.unlink()
        row = {
            "schema": LEDGER_SCHEMA,
            "index": index,
            "run_id": self.layout.run_id,
            "stage": envelope.stage,
            "stop_class": envelope.stop_class,
            "cause_fingerprint": envelope.cause_fingerprint,
            "consumed_envelope_digest": envelope.digest,
            "consumed_envelope": self.layout.relative(archived),
            "consumed_envelope_sha256": hashlib.sha256(archived.read_bytes()).hexdigest(),
            "transaction_id": action.transaction_id,
            "layer_id": target.layer_id,
            "idempotency_key": key,
            "adapter_id": ADAPTER_ID,
            "receipt_digest": chain[-1].digest,
            "evaluation_digest": evaluation.digest,
            "evaluation_result": evaluation.result,
            "resume_layer": target.layer_id,
            "spend_usd_at_dispatch": round(run_spend_usd(self.layout), 6),
        }
        path = self.layout.write_report(DISPATCH_REPORT.format(index=index), row)
        self.rows.append(row)
        return Dispatched(
            index=index,
            transaction_id=action.transaction_id,
            layer_id=target.layer_id,
            idempotency_key=key,
            receipt_digest=chain[-1].digest,
            evaluation_digest=evaluation.digest,
            resume_layer=target.layer_id,
            ledger_report=self.layout.relative(path),
        )


def evaluate_rematerialization(
    shot_folder: str | Path,
    *,
    envelope: StopEnvelope,
    chain: tuple[TransactionReceipt, ...],
) -> PostconditionEvaluation:
    """Independently prove the committed chain against the stored commit and live authority."""

    shot = Path(shot_folder).resolve()
    action = envelope.actions[0]
    terminal = chain[-1]
    if terminal.phase != "terminal" or terminal.terminal_outcome != "committed":
        raise ValueError("controller evaluation requires a committed terminal receipt")
    key = terminal.idempotency_key
    commit = controller_state.read_commit(shot, key)
    if commit is None:
        raise ValueError("committed controller receipt has no rematerialization commit")
    try:
        running = next(receipt for receipt in chain if receipt.digest == commit.running_receipt_digest)
    except StopIteration as exc:
        raise ValueError("rematerialization commit names a receipt outside its chain") from exc
    if running.phase != "running" or running.revision != commit.running_receipt_revision:
        raise ValueError("rematerialization commit does not name an exact running receipt")
    after = authority_selection.resolve_selected_authority(shot)
    if after.assertion.digest != commit.after_authority_digest:
        # Authority moved again after the commit (a later layer materialized). The commit
        # still proves this transition: re-derive it from the stored after-state digest.
        if commit.after_authority_digest == envelope.authoritative_before_digest:
            raise ValueError("rematerialization commit did not change selected authority")
    else:
        commit.assert_matches(
            action=action,
            running_receipt=running,
            before_authority_digest=envelope.authoritative_before_digest,
            after_authority=after.assertion,
        )
    commit_ref = controller_state.commit_evidence_ref(shot, commit)
    if (
        terminal.predecessor_receipt_digest != running.digest
        or terminal.commit_marker != commit_ref
        or terminal.authoritative_after_digest != commit.after_authority_digest
        or terminal.result_evidence != (commit_ref,)
    ):
        raise ValueError("terminal controller receipt does not bind its exact commit")
    receipt_ref = transaction_receipts.transaction_receipt_evidence_ref(shot, terminal)
    evaluation = PostconditionEvaluation(
        action_digest=action.digest,
        postcondition_digest=action.postcondition.digest,
        idempotency_key=key,
        evaluator_id=action.evaluator_id,
        authoritative_before_digest=envelope.authoritative_before_digest,
        authoritative_after_digest=commit.after_authority_digest,
        observed_records=(receipt_ref, commit_ref),
        result="satisfied",
    )
    evaluation.assert_matches(
        action,
        authoritative_before_digest=envelope.authoritative_before_digest,
        attempt_evidence_digest=envelope.attempt_evidence_digest,
    )
    stored = controller_state.read_evaluation(shot, key)
    if stored is not None and stored != evaluation:
        raise ValueError("stored controller postcondition evaluation conflicts with current proof")
    controller_state.publish_evaluation(shot, evaluation)
    attempt = PriorDispatchAttempt(
        cause_fingerprint=envelope.cause_fingerprint,
        authoritative_before_digest=envelope.authoritative_before_digest,
        attempt_evidence_digest=envelope.attempt_evidence_digest,
        action=action,
        evaluation=evaluation,
    )
    controller_state.publish_attempt(shot, attempt)
    return evaluation


__all__ = [
    "ADAPTER_ID",
    "DISPATCHABLE_TRANSACTIONS",
    "LEDGER_SCHEMA",
    "REFUSAL_REASONS",
    "ControllerCaps",
    "DispatchRefusal",
    "Dispatched",
    "RunController",
    "evaluate_rematerialization",
    "ledger_rows",
    "run_spend_usd",
]
