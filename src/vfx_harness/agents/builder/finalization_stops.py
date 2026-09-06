"""A failed terminal finalization publishes a stop that names its prerequisite.

`layer_publication` refuses when a layer's terminal receipt is not `passed`. HIR-0247 made
that refusal *say* which judgments failed, at which frames, by which decider, and the debt
and requirement they were paying. The envelope around it was still the unclassified
boundary: `harness_defect`, routed to engineering, for a failure the receipt describes
completely.

This compiles the other half. A finalization whose failures are all **evidence that could
not be produced** -- a black plate, an uncovered judge frame, a contract gap -- has a
nameable prerequisite and a nameable owner, and no amount of rebuilding supplies either.
That is a human decision on authority, so it stops as one (HIR-0248).

A finalization that failed because the work was **judged and did not pass** is deliberately
not compiled here. That is the layer failing on its merits, the existing exit already says
so, and inventing a dispatchable transaction for it would manufacture the
`local_implementation_miss` authority AGENTS.md says no producer proves.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from vfx_harness.domain.layer_finalization_diagnosis import (
    EVIDENCE_UNAVAILABLE_DECIDERS,
    describe_failed_finalization,
)
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
from vfx_harness.domain.verdict_deciders import CONTRACT_GAP_DECIDERS
from vfx_harness.observability.run_artifacts import RunLayout

FINALIZATION_STOP_EVIDENCE_SCHEMA = "vfx-harness.finalization-failure-stop-evidence/v1"
FINALIZATION_DECISION_AUTHORITY = "operator.evidence-prerequisite"
FINALIZATION_DECISION_SCHEMA = "vfx-harness.evidence-prerequisite-decision/v1"
# The answers are derived from the deciders present, not fixed. A remedy set that does not
# contain the remedy is a misclassification wearing the right words: an operator handed
# three options that all miss has to reject them and describe a fourth. `no_optical_signal`
# means the layer cannot produce this kind of evidence at all, and move/add-provider/
# withdraw genuinely enumerates it. A contract gap means the layer *can* produce it and no
# claim was authored to ask for it, which none of those three describes (HIR-0248).
_UNPRODUCIBLE_ANSWER_IDS = (
    "add_the_missing_provider_before_this_layer",
    "move_judgment_to_a_layer_that_can_produce_it",
    "withdraw_the_judgment",
)
_CONTRACT_GAP_ANSWER_IDS = (
    "author_a_required_claim_covering_the_frame",
    "escalate_a_vocabulary_gap_and_close_the_requirement_by_decision",
    "withdraw_the_judgment",
)


def answer_ids_for(deciders: Sequence[str]) -> tuple[str, ...]:
    """The remedies that actually apply to the deciders that failed."""
    answers: set[str] = set()
    for decider in deciders:
        if decider in CONTRACT_GAP_DECIDERS:
            answers.update(_CONTRACT_GAP_ANSWER_IDS)
        else:
            answers.update(_UNPRODUCIBLE_ANSWER_IDS)
    return tuple(sorted(answers))


def unresolved_prerequisites(
    canonical: Sequence[Mapping[str, Any]],
    groups: Sequence[Mapping[str, Any]] = (),
) -> tuple[dict[str, Any], ...]:
    """Every failing judgment whose evidence could not be produced, with its owner.

    Empty when the layer failed on judgments that were actually made: those are the work,
    not a missing prerequisite, and they route differently.
    """
    owners: dict[int, Mapping[str, Any]] = {}
    for group in groups:
        if not isinstance(group, Mapping):
            continue
        start, end = group.get("canonical_start"), group.get("canonical_end")
        if isinstance(start, int) and isinstance(end, int):
            for index in range(start, end):
                owners[index] = group
    rows: list[dict[str, Any]] = []
    for index, row in enumerate(canonical):
        if not isinstance(row, Mapping):
            continue
        verdict = row.get("verdict")
        verdict = verdict if isinstance(verdict, Mapping) else {}
        if verdict.get("pass"):
            continue
        decided_by = str(verdict.get("decided_by") or "undeclared")
        if decided_by not in EVIDENCE_UNAVAILABLE_DECIDERS:
            continue
        group = owners.get(index) or {}
        requirements = group.get("requirement_ids")
        rows.append(
            {
                "canonical_index": index,
                "frame": row.get("frame"),
                "decided_by": decided_by,
                "group_index": group.get("group_index"),
                "debt_id": group.get("debt_id"),
                "requirement_ids": sorted(
                    str(item)
                    for item in (requirements or ())
                    if isinstance(requirements, Sequence)
                    and not isinstance(requirements, str)
                    and str(item)
                ),
            }
        )
    return tuple(rows)


def judged_failures(
    canonical: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """Failing rows whose evidence existed: the layer's own merits, judged and not passed.

    These do not route this stop, and they are not omitted from it either. A layer can fail
    one group on a plate it could not produce and another on a judgment that was made, and
    an envelope naming only the first tells the operator that resolving the prerequisite
    unblocks the layer. It does not.
    """
    rows: list[dict[str, Any]] = []
    for index, row in enumerate(canonical):
        if not isinstance(row, Mapping):
            continue
        verdict = row.get("verdict")
        verdict = verdict if isinstance(verdict, Mapping) else {}
        if verdict.get("pass"):
            continue
        decided_by = str(verdict.get("decided_by") or "undeclared")
        if decided_by in EVIDENCE_UNAVAILABLE_DECIDERS:
            continue
        rows.append(
            {"canonical_index": index, "frame": row.get("frame"), "decided_by": decided_by}
        )
    return tuple(rows)


def _owner_scope_ids(layer_id: str, rows: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    scopes = {f"layer:{layer_id}"}
    for row in rows:
        for requirement in row.get("requirement_ids") or ():
            scopes.add(f"requirement:{requirement}")
        if row.get("debt_id"):
            scopes.add(f"debt:{str(row['debt_id'])[:20]}")
    return tuple(sorted(scopes))


_ANSWER_PROSE = {
    "add_the_missing_provider_before_this_layer": "add the missing provider before this layer",
    "author_a_required_claim_covering_the_frame": (
        "author a required claim whose moments reach that frame, on a unit that judges it"
    ),
    "escalate_a_vocabulary_gap_and_close_the_requirement_by_decision": (
        "escalate a vocabulary gap and close the requirement with an approved_start decision"
    ),
    "move_judgment_to_a_layer_that_can_produce_it": (
        "move the judgment to a layer that can produce it"
    ),
    "withdraw_the_judgment": "withdraw the judgment",
}


def _next_action(
    layer_id: str,
    frames: str,
    answer_ids: Sequence[str],
    also_judged: Sequence[Mapping[str, Any]],
) -> str:
    options = "; ".join(_ANSWER_PROSE[answer] for answer in answer_ids)
    action = (
        f"Decide how layer {layer_id} settles what it judges at {frames}: {options}. "
        "Rebuilding cannot supply a prerequisite the layer has no way to produce."
    )
    if also_judged:
        outstanding = ", ".join(f"f{row['frame']}" for row in also_judged)
        action += (
            f" This is not the layer's only blocker: {len(also_judged)} further judgment(s) "
            f"at {outstanding} were made on evidence that existed and did not pass, so "
            "resolving this prerequisite does not accept the layer on its own."
        )
    return action


def compile_finalization_failure_stop(
    layout: RunLayout,
    *,
    layer_id: str,
    bundle_digest: str,
    view_digest: str,
    source_path: Path,
    receipt: Mapping[str, Any],
) -> StopEnvelope | None:
    """Compile a typed stop from the terminal receipt, or None when it does not apply.

    Returns None when the receipt passed, or when every failure was a judgment that was
    actually made -- a stop naming a missing prerequisite would be false there.

    The receipt is re-read from the durable work-unit state it lives in and compared with
    what was passed in, so the envelope is source-backed rather than compiled from an
    in-memory summary somebody else assembled.
    """
    if str(receipt.get("final_status") or "") == "passed":
        return None
    canonical = receipt.get("canonical")
    groups = receipt.get("evaluation_groups")
    canonical = canonical if isinstance(canonical, Sequence) else ()
    groups = groups if isinstance(groups, Sequence) else ()
    rows = unresolved_prerequisites(canonical, groups)
    if not rows:
        return None
    also_judged = judged_failures(canonical)
    answer_ids = answer_ids_for([str(row["decided_by"]) for row in rows])

    path = Path(source_path)
    try:
        payload = path.read_bytes()
        document = json.loads(payload)
        slot = document.get("layer_finalization") if isinstance(document, Mapping) else None
        observed = slot.get("terminal_receipt") if isinstance(slot, Mapping) else None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"durable work-unit state at {path} is unreadable, so this stop cannot be "
            "source-backed"
        ) from exc
    if observed != dict(receipt):
        raise RuntimeError(
            "terminal finalization receipt changed while its typed stop was compiled"
        )
    receipt_sha256 = hashlib.sha256(payload).hexdigest()
    receipt_digest = canonical_digest(observed)

    diagnosis = describe_failed_finalization(canonical, groups)
    facts = {
        "schema": "vfx-harness.finalization-failure-cause/v1",
        "invariant_id": "unresolved_evidence_prerequisite_blocks_finalization",
        "layer_id": str(layer_id),
        "final_status": str(receipt.get("final_status") or ""),
        "unresolved": list(rows),
        "also_failed_on_judgments_made": list(also_judged),
    }
    normalized_facts_digest = canonical_digest(facts)
    question_payload = {
        "schema": FINALIZATION_DECISION_SCHEMA,
        "layer_id": str(layer_id),
        "unresolved": list(rows),
        "also_failed_on_judgments_made": list(also_judged),
        "decision_authority_id": FINALIZATION_DECISION_AUTHORITY,
        "allowed_answer_ids": list(answer_ids),
    }
    question_digest = canonical_digest(question_payload)

    document = {
        "schema": FINALIZATION_STOP_EVIDENCE_SCHEMA,
        "run_id": layout.run_id,
        "layer_id": str(layer_id),
        "receipt": {
            "source_locator": str(source_path),
            "sha256": receipt_sha256,
            "record_digest": receipt_digest,
            "final_status": receipt.get("final_status"),
        },
        "unresolved": list(rows),
        "also_failed_on_judgments_made": list(also_judged),
        "diagnosis": diagnosis,
        "question": question_payload,
    }
    record_digest = canonical_digest(document)
    report = layout.write_report("finalization-failure-stop-evidence", document)
    written = report.read_bytes()
    if json.loads(written) != document:
        raise RuntimeError("finalization failure stop evidence failed exact read-back")
    evidence_record = StopEvidenceRef(
        kind="stop_evidence",
        locator=layout.relative(report),
        sha256=hashlib.sha256(written).hexdigest(),
        record_schema=FINALIZATION_STOP_EVIDENCE_SCHEMA,
        record_digest=record_digest,
    )
    action = StopAction(
        target=EscalateQuestionTarget(
            question_record=EvidenceRecordAssertion(
                record_kind="question",
                record_id=f"evidence-prerequisite-{question_digest[:20]}",
                evidence=evidence_record,
            ),
            question_digest=question_digest,
            decision_authority_id=FINALIZATION_DECISION_AUTHORITY,
            decision_schema=FINALIZATION_DECISION_SCHEMA,
            allowed_answer_ids=answer_ids,
            evidence=(evidence_record,),
        ),
        postcondition=HumanDecisionCommitted(
            question_digest=question_digest,
            decision_authority_id=FINALIZATION_DECISION_AUTHORITY,
            decision_schema=FINALIZATION_DECISION_SCHEMA,
            allowed_answer_ids=answer_ids,
        ),
    )
    frames = ", ".join(f"f{row['frame']}" for row in rows)
    return StopEnvelope(
        stage="composition",
        stop_class="human_decision_required",
        identity=StopIdentity(
            run_id=layout.run_id,
            bundle_digest=bundle_digest,
            view_digest=view_digest,
            layer_id=str(layer_id),
            unit_id=None,
            unit_plan_digest=None,
            unit_digest=None,
            candidate_digest=None,
            # No checkpoint digest: the domain requires a unit identity beside one, and
            # this stop is layer-scoped. The receipt's identity travels as the attempt
            # and authoritative-before digests instead.
            checkpoint_digest=None,
            settings_digest=None,
            debt_state_digest=None,
        ),
        cause=StopCause(
            invariant_id="unresolved_evidence_prerequisite_blocks_finalization",
            finding_ids=(f"finalization-prereq-{normalized_facts_digest[:20]}",),
            owner_scope_ids=_owner_scope_ids(str(layer_id), rows),
            normalized_facts_digest=normalized_facts_digest,
        ),
        attempt_evidence_digest=receipt_digest,
        classification_evidence_digest=canonical_digest(
            {
                "schema": "vfx-harness.finalization-failure-classification/v1",
                "receipt_digest": receipt_digest,
                "unresolved_count": len(rows),
                "deciders": sorted({str(row["decided_by"]) for row in rows}),
            }
        ),
        artifact_state_digest=record_digest,
        authoritative_before_digest=receipt_digest,
        actions=(action,),
        evidence_refs=(evidence_record,),
        budget_key="finalization-evidence-prerequisite",
        expected=(
            f"Layer {layer_id}'s terminal finalization judges every declared point on "
            "evidence its own replay can produce."
        ),
        found=diagnosis,
        next_action=_next_action(str(layer_id), frames, answer_ids, also_judged),
    )


__all__ = [
    "FINALIZATION_DECISION_AUTHORITY",
    "FINALIZATION_DECISION_SCHEMA",
    "FINALIZATION_STOP_EVIDENCE_SCHEMA",
    "answer_ids_for",
    "compile_finalization_failure_stop",
    "judged_failures",
    "unresolved_prerequisites",
]
