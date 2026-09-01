"""Independent comparison of prepared HIR-0171 state with pure derivation.

The intent, its effects, staged state bytes, and successor bindings are evidence to
check.  They never supply preservation or invalidation truth to this verifier.  Both
live commit/recovery evaluation and unpublished candidate preview call this leaf with
an independently resolved predecessor and typed successor capsule set.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from vfx_harness.domain.authority_capsules import AuthorityCapsuleSet
from vfx_harness.domain.authority_head_records import (
    AuthoritySelectionTokenProjection,
)
from vfx_harness.domain.authority_state_records import (
    AuthorityStateMemberImage,
    AuthorityStateTransitionIntent,
)
from vfx_harness.orchestration import authority_state_effects
from vfx_harness.orchestration.authority_state_effects import (
    AuthorityStateEffectsProjection,
)
from vfx_harness.orchestration.unit_state_lock import unit_state_path
from vfx_harness.orchestration.unit_state_serialization import (
    serialize_work_unit_state,
)


def _state_hash(value: Mapping[str, Any], where: str) -> str:
    try:
        return hashlib.sha256(serialize_work_unit_state(value)).hexdigest()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{where} is not canonical finite state: {exc}") from exc


def verify_recompiled_authority_state_transition(
    shot: Path,
    *,
    intent: AuthorityStateTransitionIntent,
    before_capsules: AuthorityCapsuleSet | None,
    after_capsules: AuthorityCapsuleSet,
    predecessor_images: Mapping[str, AuthorityStateMemberImage],
    predecessor_head_revision: int,
    before_selection_token: AuthoritySelectionTokenProjection,
    before_states: Mapping[str, Mapping[str, Any]],
    after_states: Mapping[str, Mapping[str, Any]],
) -> AuthorityStateEffectsProjection:
    """Recompile and compare every effect, state object, and binding exactly."""

    proposal = intent.proposal
    if (
        proposal.predecessor_head_revision != predecessor_head_revision
        or proposal.transition_revision != predecessor_head_revision + 1
        or proposal.before_selection_token != before_selection_token
    ):
        raise ValueError(
            "authority-state transition does not name the independently resolved "
            "immediate predecessor"
        )
    if proposal.capsule_set_ref.record_digest != after_capsules.capsule_set_digest:
        raise ValueError(
            "authority-state successor capsules differ from the transition reference"
        )
    if predecessor_head_revision == 0:
        if before_capsules is not None or predecessor_images or before_states:
            raise ValueError(
                "genesis authority-state transition invents predecessor capsules or state"
            )
    elif before_capsules is None:
        raise ValueError(
            "non-genesis authority-state transition omits predecessor capsules"
        )
    if set(before_states) != set(predecessor_images):
        raise ValueError(
            "authority-state before-state namespace differs from the immediate "
            "predecessor coordinator"
        )

    projection = authority_state_effects.compile_authority_state_effects(
        before_capsules=before_capsules,
        after_capsules=after_capsules,
        states=before_states,
        prior_bindings={
            layer_id: image.binding
            for layer_id, image in predecessor_images.items()
        },
        predecessor_head_revision=predecessor_head_revision,
        before_selection_token=before_selection_token,
        at=proposal.proposed_at,
    )
    if projection.effects != proposal.effects:
        raise ValueError(
            "authority-state effects differ from independent transition derivation"
        )

    projected_rows = projection.bind_after_authority(proposal)
    expected_layers = {row.layer_id for row, _binding in projected_rows}
    members = {member.layer_id: member for member in intent.state_members}
    if set(members) != expected_layers:
        raise ValueError(
            "authority-state member namespace differs from independent transition "
            "derivation"
        )
    expected_before_layers = {
        row.layer_id
        for row, _binding in projected_rows
        if row.before_state is not None
    }
    expected_after_layers = {
        row.layer_id
        for row, _binding in projected_rows
        if row.after_state is not None
    }
    if set(before_states) != expected_before_layers:
        raise ValueError(
            "authority-state predecessor states differ from independent transition "
            "derivation"
        )
    if set(after_states) != expected_after_layers:
        raise ValueError(
            "authority-state successor states differ from independent transition "
            "derivation"
        )

    for row, after_binding in projected_rows:
        member = members[row.layer_id]
        expected_locator = (
            unit_state_path(shot, row.layer_id).relative_to(shot).as_posix()
        )
        if member.live_locator != expected_locator:
            raise ValueError(
                "authority-state member has a non-canonical live locator for layer "
                f"{row.layer_id!r}"
            )
        if row.before_state is None:
            if member.before is not None:
                raise ValueError(
                    "authority-state transition invents predecessor state for layer "
                    f"{row.layer_id!r}"
                )
        else:
            prior_image = predecessor_images[row.layer_id]
            if (
                member.before is None
                or row.before_binding is None
                or member.before.binding != row.before_binding
                or before_states[row.layer_id] != row.before_state
                or member.before.sha256
                != _state_hash(
                    row.before_state,
                    f"authority-state before state {row.layer_id!r}",
                )
                or member.before.state_revision
                != int(row.before_state["revision"])
                or prior_image.locator != member.live_locator
                or prior_image.layer_id != row.layer_id
            ):
                raise ValueError(
                    "authority-state predecessor state or binding differs from "
                    f"independent derivation for layer {row.layer_id!r}"
                )
        if row.after_state is None:
            if member.after is not None or after_binding is not None:
                raise ValueError(
                    "authority-state transition invents successor state for layer "
                    f"{row.layer_id!r}"
                )
        elif (
            member.after is None
            or after_binding is None
            or member.after.binding != after_binding
            or after_states[row.layer_id] != row.after_state
            or member.after.sha256
            != _state_hash(
                row.after_state,
                f"authority-state after state {row.layer_id!r}",
            )
            or member.after.state_revision != int(row.after_state["revision"])
        ):
            raise ValueError(
                "authority-state successor state or binding differs from independent "
                f"derivation for layer {row.layer_id!r}"
            )
    return projection


__all__ = ["verify_recompiled_authority_state_transition"]
