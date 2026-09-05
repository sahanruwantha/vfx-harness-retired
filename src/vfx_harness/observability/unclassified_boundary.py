"""The unclassified-boundary stop: what a boundary that published no typed stop says.

Extracted from ``run_artifacts`` when that module crossed the 900-line budget. The
budget is not arbitrary here: this is one cohesive behaviour -- snapshot the authority,
label the exception, publish the audit and the defect, and compile the one envelope
whose only action is ``route_engineering`` -- and it had grown four times in one day
(HIR-0214 identity, HIR-0226 prose, HIR-0231 record shape).

``RunLayout`` is imported only for typing so the dependency runs one way:
``run_artifacts`` imports this module and never the reverse.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from vfx_harness.domain.stop_envelope_primitives import canonical_digest
from vfx_harness.domain.stop_envelopes import StopCause, StopEnvelope, StopIdentity
from vfx_harness.domain.stop_transaction_state import (
    EvidenceRecordAssertion,
    StopEvidenceRef,
)
from vfx_harness.domain.stop_transactions import (
    EngineeringRouteCommitted,
    RouteEngineeringTarget,
    StopAction,
)
from vfx_harness.observability import unclassified_authority

if TYPE_CHECKING:
    from vfx_harness.observability.run_artifacts import RunLayout


def _unclassified_authoritative_state(layout: RunLayout, command: str) -> dict[str, Any]:
    """Return only semantic selected authority and durable accepted state."""

    state, _audit = unclassified_authority.snapshot(
        layout.shot,
        layout.manifest,
        boundary=command,
    )
    return state


def _exception_label(exc: BaseException, *, limit: int = 240) -> str:
    """`module.QualName: message`, whitespace-normalised and bounded.

    One value for the audit record and the operator-facing prose, so the envelope
    cannot describe an exception differently from the file beside it (HIR-0226).
    """
    qualified = f"{type(exc).__module__}.{type(exc).__qualname__}"
    message = " ".join(str(exc).split())
    if len(message) > limit:
        message = message[: limit - 1].rstrip() + "\u2026"
    return f"{qualified}: {message}" if message else qualified


def _unclassified_stop_envelope(
    layout: RunLayout,
    command: str,
    exc: BaseException,
    *,
    code: int,
    terminal_cause: str,
) -> StopEnvelope:
    """Fail closed when an owning boundary returned no typed classification.

    This classifies only the harness invariant that the envelope is missing. It does
    not infer retry, replan, or recovery authority from the exception, exit code, or
    prose that exposed the omission.
    """
    authoritative_state, authority_audit = unclassified_authority.snapshot(
        layout.shot,
        layout.manifest,
        boundary=command,
    )
    authoritative_before_digest = canonical_digest(authoritative_state)
    terminal_cause_id = unclassified_authority.closed_terminal_cause(terminal_cause)
    # Identity and authority are two questions. The stop authorizes nothing either way,
    # which is what the constant facts were protecting; but a constant identity meant
    # every unclassified boundary in every shot shared one fingerprint and one finding
    # id. Five genuinely different causes collided on one value -- an unhandled mint
    # ValueError, a stale finalization claim, a builder truncation, an operator's
    # SIGTERM, and an unclaimable-state conflict -- and the controller refuses a cause
    # fingerprint already dispatched in the shot, so the second real defect would be
    # suppressed as a repeat of the first. The cause is closed vocabulary and the
    # exception is its type only, so neither admits free-form prose (HIR-0214).
    normalized_facts = {
        "schema": "vfx-harness.unclassified-boundary-facts/v2",
        "boundary": command,
        "invariant": "terminal_boundary_requires_typed_stop",
        "terminal_cause": terminal_cause_id,
        "exception_type": f"{type(exc).__module__}.{type(exc).__qualname__}",
        "operator_initiated": terminal_cause_id in unclassified_authority.OPERATOR_CAUSES,
    }
    classification_digest = canonical_digest(normalized_facts)
    attempt_digest = canonical_digest(
        {
            "schema": "vfx-harness.unclassified-boundary-attempt/v1",
            "boundary": command,
            "exception_type": f"{type(exc).__module__}.{type(exc).__qualname__}",
            "legacy_terminal_cause": terminal_cause_id,
            "exit_code": code,
            "authoritative_before_digest": authoritative_before_digest,
        }
    )
    artifact_state_digest = canonical_digest(
        {
            "schema": "vfx-harness.unclassified-boundary-state/v1",
            "authority": authoritative_state,
        }
    )
    defect_document = {
        "schema": "vfx-harness.unclassified-boundary-defect/v1",
        "boundary": command,
        "invariant": "terminal_boundary_requires_typed_stop",
        "exception_type": f"{type(exc).__module__}.{type(exc).__qualname__}",
        "legacy_terminal_cause": terminal_cause_id,
        "exit_code": code,
        "authoritative_state": authoritative_state,
        "attempt_evidence_digest": attempt_digest,
        "classification_evidence_digest": classification_digest,
        "artifact_state_digest": artifact_state_digest,
    }
    # The authority snapshot is ~270KB and the two fields that answer "what happened"
    # are ~95 bytes. Reports are written with sorted keys, so `authority_sources` sorted
    # first by accident of the letter 'a' and buried the cause behind a quarter of a
    # megabyte -- a reader opening this file with a line-limited read never reached it.
    # HIR-0226 pointed at this file; leaving the dump inside it left a second lookup
    # within it. Same rule one level down: the record holds the cause, the bulk gets a
    # locator (HIR-0231).
    sources_path = layout.write_report(
        "unclassified-boundary-authority",
        {
            "schema": "vfx-harness.unclassified-boundary-authority/v1",
            "run_id": layout.run_id,
            "boundary": command,
            "authority_sources": authority_audit,
        },
    )
    audit_path = layout.write_report(
        "unclassified-boundary-audit",
        {
            "schema": "vfx-harness.unclassified-boundary-audit/v2",
            "run_id": layout.run_id,
            "boundary": command,
            "exception_type": f"{type(exc).__module__}.{type(exc).__qualname__}",
            "exception_message": str(exc),
            "legacy_terminal_cause": terminal_cause,
            "exit_code": code,
            "authority_sources_report": sources_path.relative_to(layout.root).as_posix(),
        },
    )
    audit_locator = audit_path.relative_to(layout.root).as_posix()
    layout.terminal_metadata.update(
        {
            "unclassified_boundary_audit": audit_locator,
        }
    )
    defect_path = layout.write_report("unclassified-boundary-defect", defect_document)
    try:
        observed = json.loads(defect_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as read_exc:
        raise RuntimeError("unclassified-boundary defect evidence failed read-back") from read_exc
    defect_digest = canonical_digest(defect_document)
    if observed != defect_document or canonical_digest(observed) != defect_digest:
        raise RuntimeError("unclassified-boundary defect evidence changed during publication")
    authority_after, _audit_after = unclassified_authority.snapshot(
        layout.shot,
        layout.manifest,
        boundary=command,
    )
    if authority_after != authoritative_state:
        raise RuntimeError(
            "selected authority changed while unclassified-boundary evidence was published"
        )
    evidence = StopEvidenceRef(
        kind="stop_evidence",
        locator=defect_path.relative_to(layout.shot).as_posix(),
        sha256=hashlib.sha256(defect_path.read_bytes()).hexdigest(),
        record_schema=defect_document["schema"],
        record_digest=defect_digest,
    )
    cause = StopCause(
        invariant_id="terminal_boundary_requires_typed_stop",
        finding_ids=(f"unclassified-boundary-{classification_digest[:20]}",),
        owner_scope_ids=("observability",),
        normalized_facts_digest=classification_digest,
    )
    defect = EvidenceRecordAssertion(
        record_kind="defect",
        record_id=f"unclassified-boundary-{defect_digest[:20]}",
        evidence=evidence,
    )
    target = RouteEngineeringTarget(
        cause_fingerprint=cause.fingerprint_for("harness_defect"),
        attempt_evidence_digest=attempt_digest,
        owner_scope_ids=cause.owner_scope_ids,
        defect_record=defect,
        evidence=(evidence,),
        sink_id="engineering_handoff",
    )
    action = StopAction(
        target=target,
        postcondition=EngineeringRouteCommitted(
            defect_packet_digest=defect_digest,
            sink_id=target.sink_id,
            owner_scope_ids=target.owner_scope_ids,
        ),
    )
    return StopEnvelope(
        stage="infrastructure",
        stop_class="harness_defect",
        identity=StopIdentity(
            run_id=layout.run_id,
            bundle_digest=None,
            view_digest=None,
            layer_id=None,
            unit_id=None,
            unit_plan_digest=None,
            unit_digest=None,
            candidate_digest=None,
            checkpoint_digest=None,
            settings_digest=None,
            debt_state_digest=None,
        ),
        cause=cause,
        attempt_evidence_digest=attempt_digest,
        classification_evidence_digest=classification_digest,
        artifact_state_digest=artifact_state_digest,
        authoritative_before_digest=authoritative_before_digest,
        actions=(action,),
        evidence_refs=(evidence,),
        budget_key="unclassified-boundary",
        expected="Every unaccepted run boundary publishes a typed stop before returning.",
        # The exception is already recorded verbatim in the audit beside this envelope.
        # Naming only the boundary here discarded it at the one surface an operator
        # reads, because `detail` is f"{stop_class}: {found} {next_action}". Four
        # distinct causes were lost this way in one day, including a
        # LayerFinalizationConflict whose own message names the `vfx finalizations
        # release` transaction that recovers it (HIR-0226).
        found=(
            f"The {command!r} boundary returned without typed stop authority; it raised "
            f"{_exception_label(exc)}"
        ),
        next_action=(
            "Read that exception first -- it is the cause, and several of these name "
            f"their own owning boundary or recovery transaction. {audit_locator} holds "
            "it verbatim. Then route the boundary and exact attempt evidence to "
            "engineering."
        ),
    )
