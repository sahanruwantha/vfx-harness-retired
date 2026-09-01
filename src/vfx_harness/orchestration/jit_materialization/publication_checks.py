"""Exact joins between a gated materialization receipt and its re-preparation."""

from __future__ import annotations

import os
from pathlib import Path

from vfx_harness.domain.authority_state_records import (
    AuthorityStateTransitionIntent,
)
from vfx_harness.orchestration.authority_state_store import (
    read_authority_state_record,
)
from vfx_harness.orchestration.jit_materialization.errors import (
    MaterializationSelectionConflict,
)
from vfx_harness.orchestration.jit_materialization.schema import (
    MaterializationFinalization,
)
from vfx_harness.orchestration.jit_materialization.transition import (
    PreparedMaterializationPublication,
)


def attested_transition_intent(
    shot_folder: str | Path,
    finalization: MaterializationFinalization,
) -> AuthorityStateTransitionIntent | None:
    """Reopen the exact immutable intent named by a terminal finalization."""

    shot = Path(os.path.abspath(Path(shot_folder).expanduser()))
    reference = finalization.authority_transition_intent_ref
    if finalization.authority_transition_kind == "noop":
        if reference is not None:
            raise MaterializationSelectionConflict(
                "no-op materialization finalization unexpectedly names a transition intent"
            )
        return None
    if reference is None:
        raise MaterializationSelectionConflict(
            "committing materialization finalization omits its transition intent"
        )
    try:
        value, _stored = read_authority_state_record(
            shot,
            locator=reference.locator,
            sha256=reference.sha256,
        )
        intent = AuthorityStateTransitionIntent.parse(
            value,
            "materialization-finalized authority-state intent",
        )
    except (OSError, TypeError, ValueError) as exc:
        raise MaterializationSelectionConflict(
            f"materialization transition intent is unavailable: {exc}"
        ) from exc
    if intent.digest != reference.record_digest:
        raise MaterializationSelectionConflict(
            "materialization transition intent reference is stale"
        )
    return intent


def require_attested_publication(
    publication: PreparedMaterializationPublication,
    finalization: MaterializationFinalization,
) -> None:
    """Require a locked re-preparation to equal the gate-attested proposal."""

    if publication.pointer_sha256 != finalization.publication_jit_pointer_sha256:
        raise MaterializationSelectionConflict(
            "materialization publication pointer differs from terminal finalization"
        )
    if (
        publication.capsule_set.capsule_set_digest
        != finalization.authority_capsule_set_digest
    ):
        raise MaterializationSelectionConflict(
            "materialization authority capsules differ from terminal finalization"
        )
    if (
        publication.transition_kind != finalization.authority_transition_kind
        or publication.effects_digest != finalization.authority_effects_digest
    ):
        raise MaterializationSelectionConflict(
            "materialization state effects differ from terminal finalization"
        )
    observed_ref = (
        None if publication.transition is None else publication.transition.intent_ref
    )
    if observed_ref != finalization.authority_transition_intent_ref:
        raise MaterializationSelectionConflict(
            "materialization transition intent differs from terminal finalization"
        )
    if (
        publication.authority_state_head_ref
        != finalization.authority_state_head_ref
        or dict(publication.before_state_hashes)
        != finalization.before_state_hashes
        or dict(publication.after_state_hashes)
        != finalization.after_state_hashes
    ):
        raise MaterializationSelectionConflict(
            "materialization authority-state snapshot differs from terminal finalization"
        )


__all__ = ["attested_transition_intent", "require_attested_publication"]
