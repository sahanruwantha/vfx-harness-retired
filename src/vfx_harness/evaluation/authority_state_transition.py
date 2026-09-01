"""Independent deterministic evaluation of one HIR-0171 transition commit."""

from __future__ import annotations

import hashlib
from pathlib import Path

from vfx_harness.domain.authority_capsule_parsing import (
    parse_authority_capsule_set,
)
from vfx_harness.domain.authority_capsules import AuthorityCapsuleSet
from vfx_harness.domain.authority_head_records import (
    AuthoritySelectionTokenProjection,
)
from vfx_harness.domain.authority_state_records import (
    AuthorityStateMemberImage,
    AuthorityStateRecordRef,
    AuthorityStateTransitionCommit,
    AuthorityStateTransitionEvaluation,
    AuthorityStateTransitionIntent,
)
from vfx_harness.evaluation.authority_state_derivation import (
    verify_recompiled_authority_state_transition,
)
from vfx_harness.orchestration.authority_capsule_resolution import (
    capture_selected_authority_capsules,
)
from vfx_harness.orchestration.authority_layer_finalization_sources import (
    prepare_transition_preserved_finalization_sources,
    require_transition_preserved_finalization_sources,
)
from vfx_harness.orchestration.authority_selection import (
    resolve_selected_authority_from_heads,
)
from vfx_harness.orchestration.authority_selection_heads import (
    read_authority_selection_heads,
)
from vfx_harness.orchestration.authority_selection_transaction import (
    read_optional_pointer_bytes,
)
from vfx_harness.orchestration.authority_state_context import (
    ResolvedAuthorityStateContext,
    resolve_authority_state_context_from_ref,
)
from vfx_harness.orchestration.authority_state_live_members import (
    require_live_state_namespace,
)
from vfx_harness.orchestration.authority_state_store import (
    read_authority_state_bytes,
    read_authority_state_record,
)
from vfx_harness.orchestration.authority_unit_completion_sources import (
    prepare_transition_preserved_unit_sources,
    require_transition_preserved_unit_sources,
)
from vfx_harness.orchestration.unit_state_lock import read_state_file_bytes
from vfx_harness.orchestration.unit_state_serialization import (
    parse_work_unit_state_bytes,
)


def _finding(findings: list[str], message: str) -> None:
    text = " ".join(str(message).split())
    findings.append(text or "authority-state evaluator observed an empty failure")


def _verify_reference(shot: Path, reference: AuthorityStateRecordRef) -> None:
    stored = read_authority_state_bytes(
        shot,
        locator=reference.locator,
        sha256=reference.sha256,
    )
    if hashlib.sha256(stored.payload).hexdigest() != reference.sha256:
        raise ValueError(
            f"staged member {reference.locator!r} changed after preparation"
        )


def _verify_pointer_transition(shot: Path, transition) -> None:
    live = read_optional_pointer_bytes(shot, shot / transition.live_locator)
    expected = transition.after
    if expected is None:
        if live is not None:
            raise ValueError(
                f"removed {transition.pointer_kind} pointer remains selected"
            )
        return
    stored = read_authority_state_bytes(
        shot,
        locator=expected.locator,
        sha256=expected.sha256,
    )
    if live != stored.payload:
        raise ValueError(
            f"selected {transition.pointer_kind} pointer is not the intended bytes"
        )


def _load_capsule_set(
    shot: Path,
    reference: AuthorityStateRecordRef,
    *,
    where: str,
) -> AuthorityCapsuleSet:
    value, _stored = read_authority_state_record(
        shot,
        locator=reference.locator,
        sha256=reference.sha256,
    )
    capsules = parse_authority_capsule_set(value, where)
    if capsules.capsule_set_digest != reference.record_digest:
        raise ValueError(f"{where} does not match its immutable reference")
    return capsules


def _predecessor_context(
    shot: Path,
    intent: AuthorityStateTransitionIntent,
) -> ResolvedAuthorityStateContext | None:
    proposal = intent.proposal
    if proposal.predecessor_head_revision == 0:
        if (
            proposal.predecessor_head_ref is not None
            or proposal.predecessor_head_digest is not None
        ):
            raise ValueError("genesis transition invents a predecessor coordinator head")
        return None
    reference = proposal.predecessor_head_ref
    if reference is None:
        raise ValueError("non-genesis transition omits its predecessor coordinator head")
    context = resolve_authority_state_context_from_ref(shot, reference)
    if (
        context.head_ref != reference
        or context.head.revision != proposal.predecessor_head_revision
        or context.head.digest != proposal.predecessor_head_digest
        or context.head.selection_token != proposal.before_selection_token
    ):
        raise ValueError(
            "transition predecessor does not reopen the exact immutable coordinator head"
        )
    return context


def _recorded_state_documents(
    shot: Path,
    intent: AuthorityStateTransitionIntent,
    *,
    side: str,
) -> dict[str, dict]:
    staged = {
        (reference.locator, reference.sha256): reference
        for reference in intent.staged_members
    }
    states: dict[str, dict] = {}
    for member in intent.state_members:
        image = getattr(member, side)
        if image is None:
            continue
        reference = staged.get((image.locator, image.sha256))
        if (
            reference is None
            or reference.record_schema != "vfx-harness.work-unit-state/v1"
            or reference.record_digest != image.sha256
        ):
            raise ValueError(
                f"transition intent omits the exact staged {side} state for layer "
                f"{member.layer_id!r}"
            )
        stored = read_authority_state_bytes(
            shot,
            locator=image.locator,
            sha256=image.sha256,
        )
        state = parse_work_unit_state_bytes(
            stored.payload,
            f"authority-state recorded {side} member {member.layer_id!r}",
        )
        if state.get("layer") != member.layer_id:
            raise ValueError(
                f"authority-state recorded {side} member belongs to the wrong layer"
            )
        states[member.layer_id] = state
    return states


def _verify_state_members(
    shot: Path,
    intent: AuthorityStateTransitionIntent,
) -> dict[str, dict]:
    require_live_state_namespace(
        shot,
        required_layer_ids=(
            member.layer_id
            for member in intent.state_members
            if member.after is not None
        ),
        where="independent authority-state successor evaluation",
    )
    states = _recorded_state_documents(shot, intent, side="after")
    for member in intent.state_members:
        live = read_state_file_bytes(shot / member.live_locator)
        if member.after is None:
            if live is not None:
                raise ValueError(
                    f"removed work-unit state remains selected for layer {member.layer_id!r}"
                )
            continue
        staged = read_authority_state_bytes(
            shot,
            locator=member.after.locator,
            sha256=member.after.sha256,
        )
        if live != staged.payload:
            raise ValueError(
                f"work-unit state for layer {member.layer_id!r} is not the intended after bytes"
            )
    return states


def evaluate_authority_state_transition(
    shot_folder: str | Path,
    *,
    intent_ref: AuthorityStateRecordRef,
    intent: AuthorityStateTransitionIntent,
    commit_ref: AuthorityStateRecordRef,
    commit: AuthorityStateTransitionCommit,
    evaluated_at: str,
) -> AuthorityStateTransitionEvaluation:
    """Reopen every authoritative join and return a closed satisfied/failed receipt."""

    shot = Path(shot_folder).expanduser().absolute()
    findings: list[str] = []
    try:
        stored_intent, _stored = read_authority_state_record(
            shot,
            locator=intent_ref.locator,
            sha256=intent_ref.sha256,
        )
        if (
            AuthorityStateTransitionIntent.parse(stored_intent) != intent
            or intent_ref.record_digest != intent.digest
        ):
            raise ValueError("transition intent reference does not reopen exact intent")
        stored_commit, _stored = read_authority_state_record(
            shot,
            locator=commit_ref.locator,
            sha256=commit_ref.sha256,
        )
        if (
            AuthorityStateTransitionCommit.parse(stored_commit) != commit
            or commit_ref.record_digest != commit.digest
        ):
            raise ValueError("transition commit reference does not reopen exact commit")
        for reference in intent.staged_members:
            _verify_reference(shot, reference)
        _verify_reference(shot, intent.proposal.producer_ref)
        _verify_pointer_transition(shot, intent.proposal.plan_pointer)
        _verify_pointer_transition(shot, intent.proposal.jit_pointer)
        heads = read_authority_selection_heads(
            shot,
            allow_pending_authority_state=True,
        )
        if AuthoritySelectionTokenProjection(
            heads.token.plan_revision,
            heads.token.plan_pointer_sha256,
            heads.token.jit_revision,
            heads.token.jit_pointer_sha256,
        ) != commit.observed_selection_token:
            raise ValueError("live plan/JIT selection does not match transition commit")
        after_states = _verify_state_members(shot, intent)
        selected = resolve_selected_authority_from_heads(shot, heads)
        captured = capture_selected_authority_capsules(shot, selected)
        after_capsules = _load_capsule_set(
            shot,
            intent.proposal.capsule_set_ref,
            where="authority-state successor capsule set",
        )
        if captured.capsule_set != after_capsules:
            raise ValueError(
                "selected authority capsules do not match the gate-attested successor"
            )
        predecessor = _predecessor_context(shot, intent)
        before_capsules = None
        predecessor_images: dict[str, AuthorityStateMemberImage] = {}
        predecessor_revision = 0
        before_selection_token = intent.proposal.before_selection_token
        if predecessor is not None:
            before_capsules = _load_capsule_set(
                shot,
                predecessor.proposal.capsule_set_ref,
                where="authority-state predecessor capsule set",
            )
            predecessor_images = {
                image.layer_id: image
                for image in predecessor.commit.installed_states
            }
            predecessor_revision = predecessor.head.revision
            before_selection_token = predecessor.head.selection_token
        before_states = _recorded_state_documents(shot, intent, side="before")
        verify_recompiled_authority_state_transition(
            shot,
            intent=intent,
            before_capsules=before_capsules,
            after_capsules=after_capsules,
            predecessor_images=predecessor_images,
            predecessor_head_revision=predecessor_revision,
            before_selection_token=before_selection_token,
            before_states=before_states,
            after_states=after_states,
        )
        preserved_sources = prepare_transition_preserved_unit_sources(
            shot,
            capsules=after_capsules,
            states=after_states,
            effects=intent.proposal.effects,
        )
        captured.require_sources_unchanged()
        require_transition_preserved_unit_sources(
            preserved_sources,
            effects=intent.proposal.effects,
        )
        preserved_finalizations = prepare_transition_preserved_finalization_sources(
            shot,
            capsules=after_capsules,
            states=after_states,
            effects=intent.proposal.effects,
        )
        require_transition_preserved_finalization_sources(
            preserved_finalizations,
            effects=intent.proposal.effects,
            successor_state_sha256={
                member.layer_id: member.after.sha256
                for member in intent.state_members
                if member.after is not None
            },
        )
    except (OSError, TypeError, ValueError) as exc:
        _finding(findings, str(exc))
    return AuthorityStateTransitionEvaluation.mint(
        intent_ref=intent_ref,
        commit_ref=commit_ref,
        commit=commit,
        result="satisfied" if not findings else "failed",
        findings=findings,
        evaluated_at=evaluated_at,
    )


__all__ = ["evaluate_authority_state_transition"]
