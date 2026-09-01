"""Current-authority checks for an immutable judgment-debt payment generation."""

from __future__ import annotations

from pathlib import Path

from vfx_harness.domain.judgment_debt_models import (
    JudgmentDebtActivation,
    JudgmentDebtDefinition,
)
from vfx_harness.domain.judgment_debt_payment_generations import (
    JudgmentDebtPaymentGeneration,
)
from vfx_harness.domain.judgment_debt_replay_receipts import ReplayPrefixReceipt
from vfx_harness.domain.work_units import dependency_ordered_units
from vfx_harness.infrastructure.trusted_files import (
    TrustedFileError,
    read_trusted_file,
)
from vfx_harness.orchestration.authority_selection import ResolvedSelectedAuthority
from vfx_harness.orchestration.authority_selection_heads import (
    read_authority_selection_heads,
)
from vfx_harness.orchestration.authority_selection_transaction import (
    authority_selection_lock,
    require_matching_authority_selection_token,
)
from vfx_harness.orchestration.authority_state_context import (
    resolve_current_authority_state,
)
from vfx_harness.orchestration.judgment_debt_replay_authority import (
    capture_selected_judgment_replay_prefix,
    require_payment_replay_prefix_current,
)
from vfx_harness.orchestration.unit_completion_state import (
    authorize_completed_units_for_layer,
)
from vfx_harness.orchestration.unit_state import unit_digest


def payment_generation_is_current(
    shot_folder: str | Path,
    selected_authority: ResolvedSelectedAuthority,
    definition: JudgmentDebtDefinition,
    activation: JudgmentDebtActivation,
    generation: JudgmentDebtPaymentGeneration,
    replay_prefix: ReplayPrefixReceipt,
) -> bool:
    """Authorize a paid replay only through current contiguous receipt lineage."""

    generation.assert_matches(definition, activation)
    if (
        generation.replay_prefix_digest != replay_prefix.digest
        or generation.replay_completions
        != tuple(
            sorted(replay_prefix.completion_bindings, key=lambda row: row.identity)
        )
        or not replay_prefix.layers
        or replay_prefix.layers[-1].layer_id != activation.payer_layer
    ):
        raise ValueError(
            "judgment debt payment generation does not bind its exact replay prefix"
        )
    shot = Path(shot_folder).expanduser().absolute()
    selected_prefix = capture_selected_judgment_replay_prefix(
        shot,
        selected_authority,
        activation.payer_layer,
    )
    require_payment_replay_prefix_current(
        shot,
        selected_authority,
        selected_prefix,
        replay_prefix,
    )
    head_refs = set()
    for selected, replay_layer in zip(
        selected_prefix.layers,
        replay_prefix.layers,
        strict=True,
    ):
        layer = selected.layer
        if (
            replay_layer.layer_generation_digest
            != selected.capsule.capsule_digest
            or replay_layer.predecessor_layer_digests
            != selected.capsule.predecessor_layer_digests
            or Path(layer.script).as_posix() != replay_layer.script_path
        ):
            return False
        selected_units = dependency_ordered_units(layer.stages)
        if tuple(unit.id for unit in selected_units) != tuple(
            unit.unit_id for unit in replay_layer.units
        ):
            return False
        expected_units = {unit.id: unit_digest(unit) for unit in selected_units}
        if any(
            expected_units.get(unit.unit_id) != unit.unit_digest
            for unit in replay_layer.units
        ):
            return False
        authorized = authorize_completed_units_for_layer(
            shot,
            replay_layer.layer_id,
            selected_units,
            expected_plan_hash=selected.capsule.capsule_digest,
            selected_authority=selected_authority,
        )
        expected_receipts = {
            unit.unit_id: unit.completion_receipt_digest
            for unit in replay_layer.units
        }
        if authorized.unit_ids != frozenset(expected_receipts):
            return False
        if any(
            authorized.receipt_digest(unit_id) != receipt_digest
            for unit_id, receipt_digest in expected_receipts.items()
        ):
            return False
        head_refs.add(authorized.authority_state_head_ref)

        try:
            layer_snapshot = read_trusted_file(
                shot,
                shot / replay_layer.script_path,
                f"current paid replay layer {replay_layer.layer_id}",
                require_nonempty=True,
            )
        except TrustedFileError as exc:
            raise ValueError(str(exc)) from exc
        if layer_snapshot.sha256 != replay_layer.script_sha256:
            raise ValueError(
                f"current paid replay layer {replay_layer.layer_id} bytes changed "
                "without invalidating its completion receipt set"
            )
        for dependency in replay_layer.dependencies:
            try:
                snapshot = read_trusted_file(
                    shot,
                    shot / dependency.path,
                    f"current paid replay layer {replay_layer.layer_id} "
                    f"{dependency.kind}",
                    require_nonempty=True,
                )
            except TrustedFileError as exc:
                raise ValueError(str(exc)) from exc
            if snapshot.sha256 != dependency.sha256:
                raise ValueError(
                    f"current paid replay layer {replay_layer.layer_id} "
                    f"{dependency.kind} bytes changed without receipt invalidation"
                )

    selected_prefix.require_sources_unchanged()
    if len(head_refs) != 1:
        raise ValueError(
            "judgment debt replay completions were authorized by different "
            "coordinator heads"
        )
    with authority_selection_lock(shot, exclusive=False):
        require_matching_authority_selection_token(
            selected_authority.selection_token,
            read_authority_selection_heads(shot).token,
        )
        context = resolve_current_authority_state(shot)
        if context is None or context.head_ref not in head_refs:
            raise ValueError(
                "authority-state head changed while judgment debt payment was authorized"
            )
    return True


__all__ = ["payment_generation_is_current"]
