"""Short-lock integration for generated work-unit construction."""

from __future__ import annotations

from vfx_harness.agents.builder.attempt_guard import UnitAttemptGuard
from vfx_harness.orchestration import generate_construction


def resolve_unit_construction(
    shot,
    layer_id: str,
    unit,
    unit_hash: str,
    attempt_guard: UnitAttemptGuard,
):
    """Stage large construction I/O unlocked and bind it under a short guard."""

    route = getattr(getattr(unit, "construction", None), "route", "procedural")
    if route not in {"generate", "retrieve"}:
        return None

    attempt_guard.check(f"start construction reuse validation for unit {unit.id}")
    reuse = generate_construction.prepare_promoted_construction_reuse(
        shot.folder,
        layer_id,
        unit.id,
        unit_hash,
    )
    if reuse is not None:
        return attempt_guard.publish(
            f"bind existing construction for unit {unit.id}",
            lambda: generate_construction.commit_promoted_construction_reuse(reuse),
        )

    attempt_guard.check(f"start external construction staging for unit {unit.id}")
    staged = generate_construction.stage_generate_unit(
        shot.folder,
        layer_id,
        unit,
        run_id=attempt_guard.claim.run_id,
        claim_id=attempt_guard.claim.claim_id,
    )
    attempt_guard.check(f"start construction CAS staging for unit {unit.id}")
    prepared = generate_construction.prepare_generate_unit_promotion(
        shot.folder,
        layer_id,
        unit,
        staged,
        expected_run_id=attempt_guard.claim.run_id,
        expected_claim_id=attempt_guard.claim.claim_id,
    )
    return attempt_guard.publish(
        f"bind construction pointer for unit {unit.id}",
        lambda: generate_construction.commit_generate_unit_promotion(
            shot.folder,
            layer_id,
            unit,
            prepared,
            expected_run_id=attempt_guard.claim.run_id,
            expected_claim_id=attempt_guard.claim.claim_id,
        ),
    )
