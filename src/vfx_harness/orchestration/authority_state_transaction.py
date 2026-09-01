"""Atomic WAL-backed commit of plan/JIT selection plus work-unit state."""

from __future__ import annotations

import hashlib
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path

from vfx_harness.domain.authority_head_records import (
    AuthoritySelectionTokenProjection,
    canonical_json_bytes,
)
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
from vfx_harness.orchestration.authority_layer_finalization_sources import (
    PreservedLayerFinalizationSourceConflict,
    require_transition_preserved_finalization_sources,
)
from vfx_harness.orchestration.authority_selection_heads import (
    read_authority_selection_heads,
)
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionToken,
    authority_selection_lock,
    durable_remove_pointer,
    durable_replace_pointer_bytes,
    durably_ensure_real_directory,
    require_matching_authority_selection_token,
)
from vfx_harness.orchestration.authority_state_context import (
    install_record_ref,
    resolve_current_authority_state,
)
from vfx_harness.orchestration.authority_state_live_members import (
    AuthorityStateLiveMemberError,
    require_live_state_namespace,
)
from vfx_harness.orchestration.authority_state_preparation import (
    PreparedAuthorityStateTransition,
)
from vfx_harness.orchestration.authority_state_store import (
    decode_pointer_bytes,
    read_authority_state_bytes,
    read_authority_state_record,
    read_current_bytes,
    read_pending_bytes,
    remove_pending,
    replace_current_bytes,
    replace_pending_bytes,
)
from vfx_harness.orchestration.authority_unit_completion_sources import (
    PreservedUnitCompletionSourceConflict,
    require_transition_preserved_unit_sources,
)
from vfx_harness.orchestration.unit_state_lock import (
    read_state_file_bytes,
    remove_state_file,
    unit_state_lock,
    write_state_file_bytes,
)


class AuthorityStateTransitionConflict(ValueError):
    """Prepared transition is stale or its bounded commit failed closed."""


def _authority_state_write_boundary(
    event: str,
    *,
    identity: str | None = None,
) -> None:
    """Stable no-op observation seam at each WAL-visible write boundary.

    Tests inject process death here so recovery is exercised after the exact write,
    rather than approximating ordering by replacing an unrelated helper.
    """


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _token(projection: AuthoritySelectionTokenProjection) -> AuthoritySelectionToken:
    return AuthoritySelectionToken(
        projection.plan_revision,
        projection.plan_pointer_sha256,
        projection.jit_revision,
        projection.jit_pointer_sha256,
    )


def _load_intent(
    shot: Path,
    prepared: PreparedAuthorityStateTransition,
) -> AuthorityStateTransitionIntent:
    value, _stored = read_authority_state_record(
        shot,
        locator=prepared.intent_ref.locator,
        sha256=prepared.intent_ref.sha256,
    )
    intent = AuthorityStateTransitionIntent.parse(value)
    if intent != prepared.intent or intent.digest != prepared.intent_ref.record_digest:
        raise AuthorityStateTransitionConflict(
            "prepared authority-state intent changed before commit"
        )
    return intent


def _require_predecessor_head(shot: Path, intent: AuthorityStateTransitionIntent) -> None:
    proposal = intent.proposal
    context = resolve_current_authority_state(shot, allow_absent=True)
    if proposal.predecessor_head_revision == 0:
        if context is not None:
            raise AuthorityStateTransitionConflict(
                "genesis transition observed an existing coordinator head"
            )
        return
    if context is None:
        raise AuthorityStateTransitionConflict(
            "authority-state predecessor head disappeared before commit"
        )
    if (
        context.head.revision != proposal.predecessor_head_revision
        or context.head.digest != proposal.predecessor_head_digest
        or context.head_ref != proposal.predecessor_head_ref
    ):
        raise AuthorityStateTransitionConflict(
            "authority-state predecessor head changed before commit"
        )


def _staged_bytes(shot: Path, image) -> bytes:
    stored = read_authority_state_bytes(
        shot,
        locator=image.locator,
        sha256=image.sha256,
    )
    return stored.payload


def _require_before_members(shot: Path, intent: AuthorityStateTransitionIntent) -> None:
    for member in intent.state_members:
        live = read_state_file_bytes(shot / member.live_locator)
        if member.before is None:
            if live is not None:
                raise AuthorityStateTransitionConflict(
                    f"new layer state {member.layer_id!r} appeared after finalization"
                )
            continue
        if live is None or hashlib.sha256(live).hexdigest() != member.before.sha256:
            raise AuthorityStateTransitionConflict(
                f"work-unit state for layer {member.layer_id!r} changed after finalization"
            )


def _install_after_members(shot: Path, intent: AuthorityStateTransitionIntent) -> None:
    for member in intent.state_members:
        path = shot / member.live_locator
        if member.after is None:
            remove_state_file(path)
        else:
            write_state_file_bytes(path, _staged_bytes(shot, member.after))
        _authority_state_write_boundary(
            "after_state_replacement",
            identity=member.layer_id,
        )


def _replace_pointer(shot: Path, transition) -> None:
    path = shot / transition.live_locator
    before = transition.before
    after = transition.after
    if before == after:
        return
    if after is None:
        durable_remove_pointer(shot, path)
        _authority_state_write_boundary(
            f"after_{transition.pointer_kind}_pointer_replacement"
        )
        return
    if before is None:
        durably_ensure_real_directory(shot, path.parent)
    durable_replace_pointer_bytes(shot, path, _staged_bytes(shot, after))
    _authority_state_write_boundary(
        f"after_{transition.pointer_kind}_pointer_replacement"
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


def _require_pending_exact(
    shot: Path,
    pending: AuthorityStatePendingPointer,
) -> None:
    payload = read_pending_bytes(shot)
    if payload is None:
        raise AuthorityStateTransitionConflict(
            "authority-state WAL disappeared during commit"
        )
    if AuthorityStatePendingPointer.parse(
        decode_pointer_bytes(payload, "authority-state pending pointer")
    ) != pending:
        raise AuthorityStateTransitionConflict(
            "authority-state WAL changed during commit"
        )


def commit_prepared_authority_state_transition_locked(
    shot_folder: str | Path,
    prepared: PreparedAuthorityStateTransition,
    *,
    committed_at: str | None = None,
) -> AuthorityStateCoordinatorHead:
    """Roll one prepared transition forward while selection exclusive is held."""

    shot = Path(shot_folder).expanduser().absolute()
    committed_at = committed_at or _now()
    intent = _load_intent(shot, prepared)
    proposal = intent.proposal
    if read_pending_bytes(shot) is not None:
        raise AuthorityStateTransitionConflict(
            "another authority-state transition is pending; recover it first"
        )
    heads = read_authority_selection_heads(shot)
    require_matching_authority_selection_token(
        _token(proposal.before_selection_token),
        heads.token,
    )
    _require_predecessor_head(shot, intent)
    state_ids = tuple(member.layer_id for member in intent.state_members)
    with ExitStack() as stack:
        for layer_id in state_ids:
            stack.enter_context(unit_state_lock(shot, layer_id, exclusive=True))
        try:
            require_live_state_namespace(
                shot,
                required_layer_ids=(
                    member.layer_id
                    for member in intent.state_members
                    if member.before is not None
                ),
                where="authority-state pre-WAL predecessor",
            )
        except AuthorityStateLiveMemberError as exc:
            raise AuthorityStateTransitionConflict(str(exc)) from exc
        _require_before_members(shot, intent)
        try:
            require_transition_preserved_unit_sources(
                prepared.preserved_unit_sources,
                effects=proposal.effects,
            )
        except PreservedUnitCompletionSourceConflict as exc:
            raise AuthorityStateTransitionConflict(
                "preserved completion source closure changed before authority-state "
                f"commit: {exc}"
            ) from exc
        try:
            require_transition_preserved_finalization_sources(
                prepared.preserved_finalization_sources,
                effects=proposal.effects,
                successor_state_sha256={
                    member.layer_id: member.after.sha256
                    for member in intent.state_members
                    if member.after is not None
                },
            )
        except PreservedLayerFinalizationSourceConflict as exc:
            raise AuthorityStateTransitionConflict(
                "preserved terminal publication source closure changed before "
                f"authority-state commit: {exc}"
            ) from exc
        pending = AuthorityStatePendingPointer.mint(
            intent_ref=prepared.intent_ref,
            intent=intent,
            selected_at=committed_at,
        )
        _authority_state_write_boundary("before_pending_publication")
        replace_pending_bytes(shot, canonical_json_bytes(pending.as_dict()))
        _require_pending_exact(shot, pending)
        _authority_state_write_boundary("after_pending_publication")
        try:
            _install_after_members(shot, intent)
            _authority_state_write_boundary("after_all_state_replacements")
            try:
                require_live_state_namespace(
                    shot,
                    required_layer_ids=(
                        member.layer_id
                        for member in intent.state_members
                        if member.after is not None
                    ),
                    where="authority-state installed successor",
                )
            except AuthorityStateLiveMemberError as exc:
                raise AuthorityStateTransitionConflict(str(exc)) from exc
            _replace_pointer(shot, proposal.plan_pointer)
            _replace_pointer(shot, proposal.jit_pointer)
            observed = read_authority_selection_heads(
                shot,
                allow_pending_authority_state=True,
            )
            require_matching_authority_selection_token(
                _token(proposal.after_selection_token),
                observed.token,
            )
            commit = AuthorityStateTransitionCommit.mint(
                intent_ref=prepared.intent_ref,
                intent=intent,
                observed_selection_token=proposal.after_selection_token,
                installed_states=_installed_states(intent),
                committed_at=committed_at,
            )
            commit_ref = install_record_ref(shot, commit)
            _authority_state_write_boundary("after_commit_receipt_publication")
            evaluation = evaluate_authority_state_transition(
                shot,
                intent_ref=prepared.intent_ref,
                intent=intent,
                commit_ref=commit_ref,
                commit=commit,
                evaluated_at=committed_at,
            )
            _authority_state_write_boundary("after_independent_evaluation")
            evaluation_ref = install_record_ref(shot, evaluation)
            _authority_state_write_boundary("after_evaluation_receipt_publication")
            if evaluation.result != "satisfied":
                raise AuthorityStateTransitionConflict(
                    "independent authority-state evaluation failed: "
                    + "; ".join(evaluation.findings)
                )
            head = AuthorityStateCoordinatorHead.mint(
                commit_ref=commit_ref,
                commit=commit,
                evaluation_ref=evaluation_ref,
                evaluation=evaluation,
            )
            install_record_ref(shot, head)
            _authority_state_write_boundary(
                "after_coordinator_head_publication"
            )
            replace_current_bytes(shot, canonical_json_bytes(head.as_dict()))
            selected_head = read_current_bytes(shot)
            if selected_head != canonical_json_bytes(head.as_dict()):
                raise AuthorityStateTransitionConflict(
                    "authority-state current head replacement did not select the successor"
                )
            _authority_state_write_boundary("after_current_head_replacement")
            _require_pending_exact(shot, pending)
            _authority_state_write_boundary("before_pending_removal")
            remove_pending(shot)
            if read_pending_bytes(shot) is not None:
                raise AuthorityStateTransitionConflict(
                    "authority-state WAL remained selected after commit"
                )
            _authority_state_write_boundary("after_pending_removal")
            resolved = resolve_current_authority_state(shot)
            if resolved is None or resolved.head != head:
                raise AuthorityStateTransitionConflict(
                    "committed authority-state head did not resolve independently"
                )
            return head
        except BaseException:
            # Pending is the durable recovery authority.  Never roll back a plausible
            # subset or erase the WAL after the visibility boundary has begun.
            raise


def commit_prepared_authority_state_transition(
    shot_folder: str | Path,
    prepared: PreparedAuthorityStateTransition,
    *,
    committed_at: str | None = None,
) -> AuthorityStateCoordinatorHead:
    """Acquire selection exclusive and commit one exact prepared intent."""

    with authority_selection_lock(shot_folder, exclusive=True):
        return commit_prepared_authority_state_transition_locked(
            shot_folder,
            prepared,
            committed_at=committed_at,
        )


__all__ = [
    "AuthorityStateTransitionConflict",
    "commit_prepared_authority_state_transition",
    "commit_prepared_authority_state_transition_locked",
]
