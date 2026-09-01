"""Deterministic roll-forward recovery for a selected authority-state WAL."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from vfx_harness.domain.authority_head_records import canonical_json_bytes
from vfx_harness.domain.authority_state_records import (
    AuthorityStateCoordinatorHead,
    AuthorityStateMemberImage,
    AuthorityStatePendingPointer,
    AuthorityStateTransitionCommit,
    AuthorityStateTransitionIntent,
)
from vfx_harness.evaluation.authority_state_transition import (
    evaluate_authority_state_transition,
)
from vfx_harness.orchestration.authority_selection_heads import (
    read_authority_selection_heads,
)
from vfx_harness.orchestration.authority_selection_transaction import (
    authority_selection_lock,
    durable_remove_pointer,
    durable_replace_pointer_bytes,
    durably_ensure_real_directory,
    read_optional_pointer_bytes,
)
from vfx_harness.orchestration.authority_state_context import (
    ResolvedAuthorityStateContext,
    install_record_ref,
    resolve_current_authority_state,
)
from vfx_harness.orchestration.authority_state_live_members import (
    AuthorityStateLiveMemberError,
    require_live_state_namespace,
)
from vfx_harness.orchestration.authority_state_live_validation import (
    AuthorityStateLiveValidationError,
    require_live_authority_state_generation,
)
from vfx_harness.orchestration.authority_state_store import (
    decode_pointer_bytes,
    read_authority_state_bytes,
    read_authority_state_record,
    read_current_bytes,
    read_pending_bytes,
    remove_pending,
    replace_current_bytes,
)
from vfx_harness.orchestration.unit_state_lock import (
    read_state_file_bytes,
    remove_state_file,
    unit_state_lock,
    write_state_file_bytes,
)


class AuthorityStateRecoveryError(ValueError):
    """A pending transition cannot be rolled forward without guessing."""


@dataclass(frozen=True, slots=True)
class AuthorityStateRecoveryObservation:
    """One lock-consistent observation of a recovered or already-current head."""

    disposition: str
    pending: AuthorityStatePendingPointer | None
    context: ResolvedAuthorityStateContext

    def __post_init__(self) -> None:
        if self.disposition not in {"recovered", "already_current"}:
            raise AuthorityStateRecoveryError(
                "authority-state recovery observation has an unsupported disposition"
            )
        if (self.disposition == "recovered") is not (self.pending is not None):
            raise AuthorityStateRecoveryError(
                "authority-state recovery disposition disagrees with pending evidence"
            )
        if (
            self.pending is not None
            and self.pending.intent_ref != self.context.commit.intent_ref
        ):
            raise AuthorityStateRecoveryError(
                "recovered authority-state head does not commit the selected pending intent"
            )


def _stored_payload(shot: Path, image) -> bytes:
    return read_authority_state_bytes(
        shot,
        locator=image.locator,
        sha256=image.sha256,
    ).payload


def _side(
    shot: Path,
    live: bytes | None,
    before,
    after,
    *,
    where: str,
) -> str:
    before_payload = None if before is None else _stored_payload(shot, before)
    after_payload = None if after is None else _stored_payload(shot, after)
    matches_before = live == before_payload
    matches_after = live == after_payload
    if not matches_before and not matches_after:
        raise AuthorityStateRecoveryError(
            f"{where} matches neither recorded before nor after identity"
        )
    if matches_after:
        return "after"
    return "before"

def _install_pointer_after(shot: Path, transition) -> None:
    if transition.after is None:
        durable_remove_pointer(shot, shot / transition.live_locator)
        return
    if transition.before is None:
        durably_ensure_real_directory(shot, shot / Path(transition.live_locator).parent)
    durable_replace_pointer_bytes(
        shot,
        shot / transition.live_locator,
        _stored_payload(shot, transition.after),
    )


def _installed_states(
    intent: AuthorityStateTransitionIntent,
) -> tuple[AuthorityStateMemberImage, ...]:
    return tuple(
        AuthorityStateMemberImage.mint(
            layer_id=member.layer_id,
            locator=member.live_locator,
            sha256=member.after.sha256,
            state_revision=member.after.state_revision,
            binding=member.after.binding,
        )
        for member in intent.state_members
        if member.after is not None
    )


def _load_pending_intent(
    shot: Path,
) -> tuple[AuthorityStatePendingPointer, AuthorityStateTransitionIntent]:
    payload = read_pending_bytes(shot)
    if payload is None:
        raise AuthorityStateRecoveryError("authority-state transition is not pending")
    try:
        pending = AuthorityStatePendingPointer.parse(
            decode_pointer_bytes(payload, "authority-state pending pointer")
        )
        value, _stored = read_authority_state_record(
            shot,
            locator=pending.intent_ref.locator,
            sha256=pending.intent_ref.sha256,
        )
        intent = AuthorityStateTransitionIntent.parse(
            value,
            "pending authority-state transition intent",
        )
    except (OSError, TypeError, ValueError) as exc:
        raise AuthorityStateRecoveryError(str(exc)) from exc
    if (
        intent.digest != pending.intent_ref.record_digest
        or intent.transaction_id != pending.transaction_id
        or intent.transition_revision != pending.transition_revision
    ):
        raise AuthorityStateRecoveryError(
            "pending pointer does not close on its exact transition intent"
        )
    return pending, intent


def _current_side(shot: Path, intent: AuthorityStateTransitionIntent) -> str:
    context = resolve_current_authority_state(
        shot,
        allow_absent=True,
        verify_live_selection=False,
    )
    proposal = intent.proposal
    if context is None:
        if proposal.predecessor_head_revision == 0:
            return "before"
        raise AuthorityStateRecoveryError(
            "pending transition predecessor coordinator head is missing"
        )
    if (
        context.head.revision == proposal.predecessor_head_revision
        and context.head_ref == proposal.predecessor_head_ref
        and context.head.digest == proposal.predecessor_head_digest
    ):
        return "before"
    if (
        context.head.revision == proposal.transition_revision
        and context.head.predecessor_head_ref == proposal.predecessor_head_ref
        and context.head.predecessor_head_digest == proposal.predecessor_head_digest
        and context.head.selection_token == proposal.after_selection_token
    ):
        return "after"
    raise AuthorityStateRecoveryError(
        "selected coordinator head matches neither pending predecessor nor successor"
    )


def _require_staged_members(shot: Path, intent: AuthorityStateTransitionIntent) -> None:
    for reference in intent.staged_members:
        read_authority_state_bytes(
            shot,
            locator=reference.locator,
            sha256=reference.sha256,
        )


def _recover_pending_authority_state_locked(
    shot: Path,
) -> AuthorityStateRecoveryObservation:
    """Recover one transition while the caller holds selection exclusive."""

    if read_pending_bytes(shot) is None:
        try:
            current = resolve_current_authority_state(shot)
            if current is not None:
                require_live_authority_state_generation(shot, current)
        except AuthorityStateLiveValidationError as exc:
            raise AuthorityStateRecoveryError(
                f"current authority-state live generation is invalid: {exc}"
            ) from exc
        if current is None:  # pragma: no cover - strict resolver never returns it
            raise AuthorityStateRecoveryError(
                "no pending transition or current coordinator head exists"
            )
        return AuthorityStateRecoveryObservation(
            disposition="already_current",
            pending=None,
            context=current,
        )
    pending, intent = _load_pending_intent(shot)
    _require_staged_members(shot, intent)
    head_side = _current_side(shot, intent)
    member_ids = tuple(member.layer_id for member in intent.state_members)
    with ExitStack() as stack:
        for layer_id in member_ids:
            stack.enter_context(unit_state_lock(shot, layer_id, exclusive=True))
        try:
            require_live_state_namespace(
                shot,
                required_layer_ids=(
                    member.layer_id
                    for member in intent.state_members
                    if member.before is not None and member.after is not None
                ),
                allowed_layer_ids=member_ids,
                where="pending authority-state transition",
            )
        except AuthorityStateLiveMemberError as exc:
            raise AuthorityStateRecoveryError(str(exc)) from exc
        observed_sides: list[str] = []
        for member in intent.state_members:
            live = read_state_file_bytes(shot / member.live_locator)
            observed_sides.append(
                _side(
                    shot,
                    live,
                    member.before,
                    member.after,
                    where=f"work-unit state for layer {member.layer_id!r}",
                )
            )
        for transition in (
            intent.proposal.plan_pointer,
            intent.proposal.jit_pointer,
        ):
            live = read_optional_pointer_bytes(
                shot,
                shot / transition.live_locator,
            )
            observed_sides.append(
                _side(
                    shot,
                    live,
                    transition.before,
                    transition.after,
                    where=f"{transition.pointer_kind} pointer",
                )
            )
        if head_side == "after" and any(side != "after" for side in observed_sides):
            raise AuthorityStateRecoveryError(
                "successor coordinator head is selected while a live member is not after"
            )
        for member in intent.state_members:
            path = shot / member.live_locator
            if member.after is None:
                remove_state_file(path)
            else:
                write_state_file_bytes(path, _stored_payload(shot, member.after))
        try:
            require_live_state_namespace(
                shot,
                required_layer_ids=(
                    member.layer_id
                    for member in intent.state_members
                    if member.after is not None
                ),
                where="recovered authority-state successor",
            )
        except AuthorityStateLiveMemberError as exc:
            raise AuthorityStateRecoveryError(str(exc)) from exc
        _install_pointer_after(shot, intent.proposal.plan_pointer)
        _install_pointer_after(shot, intent.proposal.jit_pointer)
        heads = read_authority_selection_heads(
            shot,
            allow_pending_authority_state=True,
        )
        observed = type(intent.proposal.after_selection_token)(
            heads.token.plan_revision,
            heads.token.plan_pointer_sha256,
            heads.token.jit_revision,
            heads.token.jit_pointer_sha256,
        )
        if observed != intent.proposal.after_selection_token:
            raise AuthorityStateRecoveryError(
                "recovered plan/JIT heads do not match the intended successor"
            )
        commit = AuthorityStateTransitionCommit.mint(
            intent_ref=pending.intent_ref,
            intent=intent,
            observed_selection_token=observed,
            installed_states=_installed_states(intent),
            committed_at=pending.selected_at,
        )
        commit_ref = install_record_ref(shot, commit)
        evaluation = evaluate_authority_state_transition(
            shot,
            intent_ref=pending.intent_ref,
            intent=intent,
            commit_ref=commit_ref,
            commit=commit,
            evaluated_at=pending.selected_at,
        )
        evaluation_ref = install_record_ref(shot, evaluation)
        if evaluation.result != "satisfied":
            raise AuthorityStateRecoveryError(
                "recovered authority-state transition failed independent evaluation: "
                + "; ".join(evaluation.findings)
            )
        head = AuthorityStateCoordinatorHead.mint(
            commit_ref=commit_ref,
            commit=commit,
            evaluation_ref=evaluation_ref,
            evaluation=evaluation,
        )
        install_record_ref(shot, head)
        selected_head = read_current_bytes(shot)
        if head_side == "after":
            if selected_head != canonical_json_bytes(head.as_dict()):
                raise AuthorityStateRecoveryError(
                    "selected successor coordinator differs from recovered records"
                )
        else:
            replace_current_bytes(shot, canonical_json_bytes(head.as_dict()))
        expected_pending = canonical_json_bytes(pending.as_dict())
        if read_pending_bytes(shot) != expected_pending:
            raise AuthorityStateRecoveryError(
                "authority-state pending pointer changed during recovery"
            )
        remove_pending(shot)
        resolved = resolve_current_authority_state(shot)
        if resolved is None or resolved.head != head:
            raise AuthorityStateRecoveryError(
                "recovered authority-state successor did not resolve"
            )
        return AuthorityStateRecoveryObservation(
            disposition="recovered",
            pending=pending,
            context=resolved,
        )


def recover_pending_authority_state(
    shot_folder: str | Path,
) -> AuthorityStateRecoveryObservation:
    """Return typed lock-consistent evidence for recovery or an exact no-op."""

    shot = Path(shot_folder).expanduser().absolute()
    try:
        with authority_selection_lock(shot, exclusive=True):
            return _recover_pending_authority_state_locked(shot)
    except AuthorityStateRecoveryError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise AuthorityStateRecoveryError(
            f"authority-state recovery failed closed: {exc}"
        ) from exc


def recover_pending_authority_state_transition(
    shot_folder: str | Path,
) -> AuthorityStateCoordinatorHead:
    """Roll one pending transition to its sole exact successor, idempotently."""

    return recover_pending_authority_state(shot_folder).context.head


__all__ = [
    "AuthorityStateRecoveryError",
    "AuthorityStateRecoveryObservation",
    "recover_pending_authority_state",
    "recover_pending_authority_state_transition",
]
