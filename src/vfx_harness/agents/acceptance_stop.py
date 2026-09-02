"""Typed terminal authority for failed finished-chain acceptance.

Acceptance can prove that a selected moment failed, but its current diagnostic
routing identifies only a layer.  It therefore cannot authorize a unit retry or
an authority transaction.  This module seals the exact failed attempt and emits
only the human-decision transition until a revision-checked unit owner exists.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from vfx_harness.agents import acceptance_stop_evidence
from vfx_harness.domain.acceptance_outcomes import (
    AcceptanceMomentOutcome,
    AcceptanceOutcome,
)
from vfx_harness.domain.brief import Shot
from vfx_harness.domain.stop_envelope_primitives import canonical_digest, require_digest
from vfx_harness.domain.stop_envelopes import (
    StopCause,
    StopEnvelope,
    StopIdentity,
    classify_stop,
)
from vfx_harness.domain.stop_transaction_state import (
    EvidenceRecordAssertion,
    StopEvidenceRef,
)
from vfx_harness.domain.stop_transactions import (
    EscalateQuestionTarget,
    HumanDecisionCommitted,
    StopAction,
)
from vfx_harness.observability.run_artifacts import RunLayout
from vfx_harness.orchestration import layer_publication
from vfx_harness.orchestration.accepted_chain import (
    accepted_chain_digest,
    accepted_chain_rows,
    inside_shot,
    legacy_ledger_statuses,
    sha256_of,
)
from vfx_harness.orchestration.authority_selection import (
    ResolvedSelectedAuthority,
    SelectedAuthorityResolutionError,
    resolve_selected_authority,
)
from vfx_harness.orchestration.authority_selection_heads import (
    AuthoritySelectionHeadError,
    read_authority_selection_heads,
)
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionConflict,
    authority_selection_lock,
    require_matching_authority_selection_token,
)
from vfx_harness.orchestration.judgment_debt_state import (
    current_judgment_debt_state_digest_for_authority,
    require_judgment_debts_satisfied,
)
from vfx_harness.orchestration.ledger import Milestone, load_milestones
from vfx_harness.orchestration.plan_due import require_due_clear
from vfx_harness.orchestration.selected_layer_chain import selected_layer_chain


@dataclass(frozen=True, slots=True)
class AcceptanceAuthoritySnapshot:
    """Selected plan/view plus the exact accepted build read by acceptance."""

    bundle_digest: str
    view_digest: str
    judgment_debt_state_digest: str
    acceptance_artifact_sha256: str
    layers_artifact_sha256: str
    selected_moments: tuple[dict[str, Any], ...]
    chain: tuple[dict[str, Any], ...]

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": "vfx-harness.acceptance-authoritative-before/v2",
            "bundle_digest": self.bundle_digest,
            "view_digest": self.view_digest,
            "judgment_debt_state_digest": self.judgment_debt_state_digest,
            "acceptance_artifact_sha256": self.acceptance_artifact_sha256,
            "layers_artifact_sha256": self.layers_artifact_sha256,
            "selected_moments": list(self.selected_moments),
            "chain": list(self.chain),
        }

    @property
    def digest(self) -> str:
        return canonical_digest(self._payload())

    @property
    def chain_digest(self) -> str:
        return accepted_chain_digest(self.chain)


def _selected_moment_index(
    authority: AcceptanceAuthoritySnapshot,
) -> dict[str, Mapping[str, Any]]:
    expected_fields = {"id", "frame", "ref", "ref_sha256", "fingerprint"}
    selected: dict[str, Mapping[str, Any]] = {}
    for index, value in enumerate(authority.selected_moments):
        where = f"acceptance authority selected_moments[{index}]"
        if not isinstance(value, Mapping) or set(value) != expected_fields:
            raise ValueError(f"{where} must contain exactly {sorted(expected_fields)}")
        moment_id = value["id"]
        if not isinstance(moment_id, str) or not moment_id or moment_id != moment_id.strip():
            raise ValueError(f"{where}.id must be a non-empty trimmed string")
        if moment_id in selected:
            raise ValueError("acceptance authority contains duplicate selected moment ids")
        if not isinstance(value["frame"], int) or isinstance(value["frame"], bool):
            raise ValueError(f"{where}.frame must be an integer")
        if not isinstance(value["ref"], str) or not value["ref"].strip():
            raise ValueError(f"{where}.ref must be a non-empty string")
        require_digest(value["ref_sha256"], f"{where}.ref_sha256")
        canonical_digest(value["fingerprint"])
        selected[moment_id] = value
    if not selected:
        raise ValueError("acceptance authority must select at least one moment")
    return selected


def _semantic_selected_capture(
    row: Mapping[str, Any],
    selected: Mapping[str, Any],
) -> dict[str, Any]:
    semantic = _semantic_stop_capture(row)
    moment_id = semantic["moment_id"]
    if semantic["frame"] != selected["frame"]:
        raise ValueError(
            f"acceptance moment {moment_id} frame does not match selected authority"
        )
    if semantic["reference_sha256"] != selected["ref_sha256"]:
        raise ValueError(
            f"acceptance moment {moment_id} reference bytes do not match selected authority"
        )
    return semantic


def capture_acceptance_authority(
    shot: Shot,
    moments: Mapping[str, Milestone],
    selected_authority: ResolvedSelectedAuthority | None = None,
    *,
    verify_layer_publications: bool = True,
) -> AcceptanceAuthoritySnapshot:
    """Resolve one strict before-state and reject a mixed authority generation."""

    shot_root = shot.folder.resolve()
    if selected_authority is None:
        try:
            selected = resolve_selected_authority(shot_root)
        except SelectedAuthorityResolutionError as exc:
            raise ValueError(str(exc)) from exc
    else:
        selected = selected_authority
    if selected.plan is None or selected.assertion.effective_view is None:
        raise ValueError("acceptance requires selected plan authority")
    bundle_before = selected.plan.bundle
    view_before = selected.assertion.effective_view.digest
    try:
        acceptance_path = selected.artifact_paths["acceptance.json"]
        layers_path = selected.artifact_paths["layers.json"]
    except KeyError as exc:
        raise ValueError("selected acceptance authority omits a required artifact") from exc
    statuses = legacy_ledger_statuses(shot_root)

    layers = tuple(selected_layer_chain(
        shot,
        bundle=bundle_before,
        selected_authority=selected,
    ))
    publications: list[layer_publication.VerifiedLayerPublication] = []
    if verify_layer_publications:
        failures: list[str] = []
        for layer in layers:
            try:
                publications.append(
                    layer_publication.require_current_layer_publication(
                        shot_root,
                        layer,
                        selected,
                    )
                )
            except layer_publication.LayerPublicationConflict as exc:
                failures.append(f"layer {layer.id}: {exc}")
        if failures:
            raise ValueError(
                "acceptance requires current receipt-backed layer publications — "
                + "; ".join(failures)
            )

    chain = list(
        accepted_chain_rows(
            shot_root,
            layers,
            (
                {
                    str(layer.id): publication
                    for layer, publication in zip(layers, publications, strict=True)
                }
                if verify_layer_publications
                else {}
            ),
            ledger_statuses=statuses,
        )
    )

    if verify_layer_publications:
        observed_again: list[layer_publication.VerifiedLayerPublication] = []
        failures = []
        for layer in layers:
            try:
                observed_again.append(
                    layer_publication.require_current_layer_publication(
                        shot_root,
                        layer,
                        selected,
                    )
                )
            except layer_publication.LayerPublicationConflict as exc:
                failures.append(f"layer {layer.id}: {exc}")
        if failures:
            raise ValueError(
                "receipt-backed layer publication changed during acceptance authority "
                "capture — " + "; ".join(failures)
            )
        if tuple(observed_again) != tuple(publications):
            raise ValueError(
                "receipt-backed layer publication changed during acceptance authority "
                "capture"
            )

    selected_rows: list[dict[str, Any]] = []
    for moment_id, moment in moments.items():
        reference = inside_shot(
            shot_root,
            str(moment.ref),
            f"acceptance moment {moment_id} reference",
        )
        if not reference.is_file():
            raise ValueError(f"acceptance moment {moment_id} reference is missing")
        selected_rows.append(
            {
                "id": str(moment_id),
                "frame": int(moment.frame),
                "ref": str(moment.ref),
                "ref_sha256": sha256_of(reference),
                "fingerprint": moment.fingerprint,
            }
        )
    selected_moments = tuple(selected_rows)
    snapshot = AcceptanceAuthoritySnapshot(
        bundle_digest=bundle_before.content_hash,
        view_digest=view_before,
        judgment_debt_state_digest=current_judgment_debt_state_digest_for_authority(
            shot_root,
            selected,
        ),
        acceptance_artifact_sha256=sha256_of(acceptance_path),
        layers_artifact_sha256=sha256_of(layers_path),
        selected_moments=selected_moments,
        chain=tuple(chain),
    )

    try:
        with authority_selection_lock(shot_root, exclusive=False):
            heads = read_authority_selection_heads(shot_root)
            require_matching_authority_selection_token(
                selected.selection_token,
                heads.token,
            )
    except (AuthoritySelectionConflict, AuthoritySelectionHeadError) as exc:
        raise ValueError("selected acceptance authority changed while it was read") from exc
    if (
        current_judgment_debt_state_digest_for_authority(shot_root, selected)
        != snapshot.judgment_debt_state_digest
        or sha256_of(acceptance_path) != snapshot.acceptance_artifact_sha256
        or sha256_of(layers_path) != snapshot.layers_artifact_sha256
    ):
        raise ValueError("selected acceptance bundle/view changed while its before-state was read")
    return snapshot


def _capture_payload(
    shot: Shot,
    moment_id: str,
    result: Mapping[str, Any],
) -> dict[str, Any]:
    row = acceptance_stop_evidence.validate_moment_record(
        result,
        f"acceptance moment {moment_id}",
    )
    render_relative = row["render"]
    reference_relative = row["ref"]
    render = inside_shot(shot.folder.resolve(), render_relative, f"acceptance moment {moment_id} render")
    reference = inside_shot(shot.folder.resolve(), reference_relative, f"acceptance moment {moment_id} reference")
    if not render.is_file() or not reference.is_file():
        raise ValueError(f"acceptance moment {moment_id} evidence is missing")

    return {
        "schema": "vfx-harness.acceptance-moment-audit/v1",
        "moment_id": moment_id,
        "render_locator": render_relative,
        "reference_locator": reference_relative,
        "render_sha256": sha256_of(render),
        "reference_sha256": sha256_of(reference),
        "moment": dict(row),
    }


def _semantic_stop_capture(row: Mapping[str, Any]) -> dict[str, Any]:
    """Identity-bearing acceptance facts, excluding diagnostic prose and locators."""

    moment = row.get("moment")
    return acceptance_stop_evidence.semantic_capture(
        moment,
        moment_id=str(row.get("moment_id") or ""),
        render_sha256=str(row.get("render_sha256") or ""),
        reference_sha256=str(row.get("reference_sha256") or ""),
    )


def _failure_modes(row: Mapping[str, Any]) -> tuple[str, ...]:
    modes = []
    if row.get("critic_pass") is not True:
        modes.append(
            "no_optical_signal"
            if row.get("decided_by") == "no_optical_signal"
            else "critic"
        )
    if row.get("metric_readings") or row.get("metric_failures"):
        modes.append("blocking_metrics")
    if any(
        isinstance(evidence, Mapping)
        and evidence.get("authoritative") is True
        and evidence.get("pass") is not True
        for evidence in row.get("contract_evidence") or ()
    ):
        modes.append("authoritative_contracts")
    if not modes:
        modes.append("unclassified_acceptance_reading")
    return tuple(modes)


def _causal_failure(row: Mapping[str, Any]) -> dict[str, Any]:
    failed_contracts = sorted(
        str(evidence["id"])
        for evidence in row.get("contract_evidence") or ()
        if isinstance(evidence, Mapping)
        and evidence.get("authoritative") is True
        and evidence.get("pass") is not True
        and isinstance(evidence.get("id"), str)
    )
    metric_ids = sorted(
        {
            (
                str(reading.get("metric_id"))
                if isinstance(reading, Mapping)
                else str(reading).split(" ", 1)[0]
            )
            for reading in (row.get("metric_readings") or row.get("metric_failures") or ())
            if (
                isinstance(reading, Mapping)
                and isinstance(reading.get("metric_id"), str)
                and reading.get("metric_id")
            )
            or (not isinstance(reading, Mapping) and str(reading).strip())
        }
    )
    critic_axes = (
        sorted(str(axis) for axis in (row.get("scores") or {}))
        if row.get("critic_pass") is not True
        else []
    )
    return {
        "moment_id": str(row["moment_id"]),
        "frame": int(row["frame"]),
        "failure_modes": list(_failure_modes(row)),
        "failed_contract_ids": failed_contracts,
        "metric_ids": metric_ids,
        "critic_axis_ids": critic_axes,
    }


def acceptance_moment_captures(
    shot: Shot,
    authority: AcceptanceAuthoritySnapshot,
    results: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Return each moment's exact semantic capture in selected-moment order.

    The canonical digest of every capture is that moment's ``evidence_digest`` in the
    typed outcome, so a durable copy of the capture is the evidence record a ledger
    binding can re-verify without the acceptance session.
    """

    selected = _selected_moment_index(authority)
    expected_ids = tuple(selected)
    if tuple(results) != expected_ids:
        raise ValueError(
            "acceptance outcome does not cover the exact selected moment set; "
            f"expected={list(expected_ids)}, found={list(results)}"
        )
    return {
        moment_id: _semantic_selected_capture(
            _capture_payload(shot, moment_id, result),
            selected[moment_id],
        )
        for moment_id, result in results.items()
    }


def compile_acceptance_outcome(
    shot: Shot,
    authority: AcceptanceAuthoritySnapshot,
    results: Mapping[str, Mapping[str, Any]],
) -> AcceptanceOutcome:
    """Seal a complete acceptance attempt without treating failure as success."""

    moment_outcomes = [
        AcceptanceMomentOutcome(
            moment_id=moment_id,
            passed=semantic["pass"],
            decided_by=semantic["decided_by"],
            evidence_digest=canonical_digest(semantic),
        )
        for moment_id, semantic in acceptance_moment_captures(
            shot,
            authority,
            results,
        ).items()
    ]
    return AcceptanceOutcome(
        authority_digest=authority.digest,
        bundle_digest=authority.bundle_digest,
        view_digest=authority.view_digest,
        chain_digest=authority.chain_digest,
        moments=tuple(moment_outcomes),
    )


def require_current_accepted_outcome(
    shot: Shot,
    selected_authority: ResolvedSelectedAuthority | None = None,
) -> AcceptanceOutcome:
    """Require a passing full-chain outcome for the exact current authority.

    The ledger is not trusted merely because it contains ``passed == total``.  Every
    selected moment's evidence is re-hashed, and the selected plan/view plus accepted
    script chain must still equal the before-state that acceptance judged.
    """

    if selected_authority is None:
        try:
            selected_authority = resolve_selected_authority(shot.folder)
        except SelectedAuthorityResolutionError as exc:
            raise ValueError(str(exc)) from exc
    ledger_path = shot.folder / "shot.json"
    try:
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"final render requires a readable acceptance ledger: {ledger_path}") from exc
    acceptance = ledger.get("acceptance") if isinstance(ledger, dict) else None
    raw_outcome = acceptance.get("outcome") if isinstance(acceptance, dict) else None
    if raw_outcome is None:
        raise ValueError("final render requires a complete typed acceptance outcome")
    outcome = AcceptanceOutcome.from_dict(raw_outcome, "shot.json.acceptance.outcome")
    moments = load_milestones(shot, selected_authority)
    authority = capture_acceptance_authority(
        shot,
        moments,
        selected_authority,
    )
    if (
        outcome.authority_digest != authority.digest
        or outcome.bundle_digest != authority.bundle_digest
        or outcome.view_digest != authority.view_digest
        or outcome.chain_digest != authority.chain_digest
    ):
        raise ValueError(
            "final render acceptance is stale for the selected plan/view or accepted build chain"
        )
    selected = _selected_moment_index(authority)
    expected_ids = tuple(selected)
    if tuple(moment.moment_id for moment in outcome.moments) != expected_ids:
        raise ValueError("final render acceptance does not cover every selected moment")
    result_rows = acceptance.get("moments")
    if not isinstance(result_rows, dict) or tuple(result_rows) != expected_ids:
        raise ValueError("final render acceptance moment evidence is incomplete")
    for moment in outcome.moments:
        result = result_rows.get(moment.moment_id)
        if not isinstance(result, Mapping):
            raise ValueError(
                f"final render acceptance moment {moment.moment_id} has no evidence record"
            )
        semantic = _semantic_selected_capture(
            _capture_payload(shot, moment.moment_id, result),
            selected[moment.moment_id],
        )
        observed = canonical_digest(semantic)
        if (
            observed != moment.evidence_digest
            or moment.decided_by != semantic["decided_by"]
        ):
            raise ValueError(
                f"final render acceptance evidence changed for moment {moment.moment_id}"
            )
    if not outcome.passed:
        failed = ", ".join(
            moment.moment_id for moment in outcome.moments if not moment.passed
        )
        raise ValueError(f"final render requires passing acceptance; failed moments: {failed}")
    require_judgment_debts_satisfied(
        shot.folder,
        selected_authority,
    )
    require_due_clear(
        shot.folder,
        acceptance=True,
        expected_bundle_digest=authority.bundle_digest,
        selected_authority=selected_authority,
    )
    return outcome


def compile_acceptance_stop(
    shot: Shot,
    layout: RunLayout,
    authority: AcceptanceAuthoritySnapshot,
    results: Mapping[str, Mapping[str, Any]],
    axes: Sequence[tuple[str, str]],
) -> StopEnvelope:
    """Seal one failed acceptance attempt and return its only legal transition."""

    if layout.shot != shot.folder.resolve():
        raise ValueError("acceptance run layout belongs to another shot")
    selected = _selected_moment_index(authority)
    expected_ids = tuple(selected)
    if tuple(results) != expected_ids:
        raise ValueError(
            "acceptance results do not match the selected before-state moments; "
            f"expected={list(expected_ids)}, found={list(results)}"
        )
    raw_attempt_rows = tuple(
        _capture_payload(shot, str(moment_id), result)
        for moment_id, result in results.items()
    )
    attempt_rows = tuple(
        _semantic_selected_capture(row, selected[str(row["moment_id"])])
        for row in raw_attempt_rows
    )
    failed = tuple(row for row in attempt_rows if row.get("pass") is not True)
    if not failed:
        raise ValueError("a passing acceptance attempt cannot publish a stop envelope")

    failed_summary = tuple(_causal_failure(row) for row in failed)
    allowed_answer_ids = tuple(
        {
            "abstain",
            "review_authority_amendment",
            *(
                f"review_unit:{layer['layer_id']}:{unit['unit_id']}"
                for layer in authority.chain
                for unit in layer["units"]
            ),
        }
    )
    decision_authority_id = "acceptance-review"
    decision_schema = "vfx-harness.acceptance-repair-scope-decision/v1"
    question_payload = {
        "schema": "vfx-harness.acceptance-repair-scope-question/v1",
        "authority_digest": authority.digest,
        "failed": list(failed_summary),
        "decision_authority_id": decision_authority_id,
        "decision_schema": decision_schema,
        "allowed_answer_ids": sorted(allowed_answer_ids),
        "prompt": (
            "Select the exact current unit or authority scope to review for the failed "
            "acceptance moments, or abstain. This decision does not itself authorize mutation."
        ),
    }
    question_digest = canonical_digest(question_payload)
    question_id = f"acceptance-question-{question_digest[:20]}"
    attempt_payload = {
        "schema": "vfx-harness.acceptance-stop-evidence/v1",
        "bundle_digest": authority.bundle_digest,
        "view_digest": authority.view_digest,
        "authoritative_before_digest": authority.digest,
        "axes": [{"id": key, "description": description} for key, description in axes],
        "moments": list(attempt_rows),
    }
    attempt_evidence_digest = canonical_digest(attempt_payload)
    evidence_document = {
        **attempt_payload,
        "attempt_evidence_digest": attempt_evidence_digest,
        "question": {
            **question_payload,
            "question_id": question_id,
            "question_digest": question_digest,
        },
    }
    evidence_path = layout.write_report("acceptance-stop-evidence", evidence_document)
    try:
        observed_document = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("acceptance stop evidence failed readable publication") from exc
    if observed_document != evidence_document:
        raise RuntimeError("acceptance stop evidence changed during publication")
    evidence_record_digest = canonical_digest(evidence_document)
    evidence_ref = StopEvidenceRef(
        kind="stop_evidence",
        locator=evidence_path.relative_to(shot.folder.resolve()).as_posix(),
        sha256=sha256_of(evidence_path),
        record_schema="vfx-harness.acceptance-stop-evidence/v1",
        record_digest=evidence_record_digest,
    )
    audit_path = layout.write_report(
        "acceptance-stop-audit",
        {
            "schema": "vfx-harness.acceptance-stop-audit/v1",
            "run_id": layout.run_id,
            "evidence_ref": evidence_ref.as_dict(),
            "axes": [{"id": key, "description": description} for key, description in axes],
            "moments": list(raw_attempt_rows),
        },
    )
    if not audit_path.is_file():
        raise RuntimeError("acceptance stop audit did not publish")
    layout.terminal_metadata.update(
        {
            "acceptance_stop_evidence": "reports/acceptance-stop-evidence.json",
            "acceptance_stop_evidence_digest": evidence_record_digest,
            "acceptance_stop_audit": "reports/acceptance-stop-audit.json",
        }
    )
    normalized_facts_digest = canonical_digest(
        {
            "schema": "vfx-harness.acceptance-stop-cause/v1",
            "invariant_id": "selected_acceptance_moments_pass",
            "failed": list(failed_summary),
        }
    )
    classification_evidence_digest = canonical_digest(
        {
            "schema": "vfx-harness.acceptance-stop-classification/v1",
            "attempt_evidence_digest": attempt_evidence_digest,
            "failed": list(failed_summary),
        }
    )
    artifact_state_digest = canonical_digest(
        {
            "schema": "vfx-harness.acceptance-artifact-state/v1",
            "chain_digest": authority.chain_digest,
            "passed": len(attempt_rows) - len(failed),
            "total": len(attempt_rows),
            "failed_moment_ids": [str(row["moment_id"]) for row in failed],
        }
    )
    settings_digest = canonical_digest(
        {
            "schema": "vfx-harness.acceptance-settings/v1",
            "axes": [{"id": key, "description": description} for key, description in axes],
            "captures": [
                {
                    "moment_id": row["moment_id"],
                    "mode": row["render_capture"].get("mode"),
                    "scale": row["render_capture"].get("scale"),
                    "resolution": row["render_capture"].get("resolution"),
                    "render_state": row["render_capture"].get("render_state"),
                }
                for row in attempt_rows
            ],
        }
    )
    failed_text = ", ".join(
        f"{row['moment_id']}@f{row['frame']} ({'/'.join(row['failure_modes'])})" for row in failed_summary
    )
    question_record = EvidenceRecordAssertion(
        record_kind="question",
        record_id=question_id,
        evidence=evidence_ref,
    )
    target = EscalateQuestionTarget(
        question_record=question_record,
        question_digest=question_digest,
        decision_authority_id=decision_authority_id,
        decision_schema=decision_schema,
        allowed_answer_ids=allowed_answer_ids,
        evidence=(evidence_ref,),
    )
    action = StopAction(
        target=target,
        postcondition=HumanDecisionCommitted(
            question_digest=question_digest,
            decision_authority_id=decision_authority_id,
            decision_schema=decision_schema,
            allowed_answer_ids=allowed_answer_ids,
        ),
    )
    candidate = StopEnvelope(
        stage="acceptance",
        stop_class="human_decision_required",
        identity=StopIdentity(
            run_id=layout.run_id,
            bundle_digest=authority.bundle_digest,
            view_digest=authority.view_digest,
            layer_id=None,
            unit_id=None,
            unit_plan_digest=None,
            unit_digest=None,
            candidate_digest=authority.chain_digest,
            checkpoint_digest=None,
            settings_digest=settings_digest,
            debt_state_digest=None,
        ),
        cause=StopCause(
            invariant_id="selected_acceptance_moments_pass",
            finding_ids=tuple(
                "acceptance:" + canonical_digest(
                    {
                        "schema": "vfx-harness.acceptance-finding-id/v1",
                        "failure": row,
                    }
                )
                for row in failed_summary
            ),
            owner_scope_ids=("acceptance",),
            normalized_facts_digest=normalized_facts_digest,
        ),
        attempt_evidence_digest=attempt_evidence_digest,
        classification_evidence_digest=classification_evidence_digest,
        artifact_state_digest=artifact_state_digest,
        authoritative_before_digest=authority.digest,
        actions=(action,),
        evidence_refs=(evidence_ref,),
        budget_key="acceptance-human-decision",
        expected=f"All {len(attempt_rows)} selected acceptance moments pass their exact evidence.",
        found=f"{len(failed)} selected acceptance moment(s) failed: {failed_text}.",
        next_action=(
            "Choose the exact revision-checked unit or authority transaction for the failed "
            "moments; layer-level axis routing is diagnostic and authorizes no automatic "
            "retry or replan."
        ),
    )
    classified = classify_stop(candidate)
    if not isinstance(classified, StopEnvelope):
        raise ValueError("failed acceptance was misclassified as successful continuation")
    return classified
