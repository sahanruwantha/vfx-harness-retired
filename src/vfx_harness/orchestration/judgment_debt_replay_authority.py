"""Selected-DAG and layer-finalization authority for judgment replay prefixes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from vfx_harness.domain.authority_capsules import LayerAuthorityCapsule
from vfx_harness.domain.authority_head_records import parse_authority_selection_token
from vfx_harness.domain.judgment_debt_replay_receipts import ReplayPrefixReceipt
from vfx_harness.domain.layer_finalizations import (
    LayerFinalizationClaim,
    LayerFinalizationReceipt,
)
from vfx_harness.domain.work_units import (
    dependency_ordered_units,
    strict_topological_sparse_layer_ids,
)
from vfx_harness.orchestration import (
    authority_capsule_resolution,
    layer_finalization_state,
    layer_replay_receipts,
    unit_state,
)
from vfx_harness.orchestration.authority_selection import ResolvedSelectedAuthority
from vfx_harness.orchestration.ledger import Layer, load_layers_from_path


@dataclass(frozen=True, slots=True)
class SelectedJudgmentReplayLayer:
    """One executable layer and its exact selected semantic capsule."""

    layer: Layer
    capsule: LayerAuthorityCapsule


@dataclass(frozen=True, slots=True)
class CapturedSelectedJudgmentReplayPrefix:
    """Exact stable selected-layer prefix through one payer layer."""

    layers: tuple[SelectedJudgmentReplayLayer, ...]
    captured_capsules: authority_capsule_resolution.CapturedAuthorityCapsules

    def require_sources_unchanged(self) -> None:
        self.captured_capsules.require_sources_unchanged()


def capture_selected_judgment_replay_prefix(
    shot_folder: str | Path,
    selected_authority: ResolvedSelectedAuthority,
    payer_layer: str,
) -> CapturedSelectedJudgmentReplayPrefix:
    """Derive, rather than trust, the exact stable selected prefix through payer."""

    if not isinstance(selected_authority, ResolvedSelectedAuthority):
        raise ValueError(
            "judgment replay prefix requires exact typed selected authority"
        )
    payer = str(payer_layer).strip()
    if not payer:
        raise ValueError("judgment replay prefix payer layer must be non-empty")
    captured = authority_capsule_resolution.capture_selected_authority_capsules(
        shot_folder,
        selected_authority,
    )
    capsule_rows = captured.capsule_set.layers
    sparse_rows = tuple(row.projection["sparse_layer"] for row in capsule_rows)
    order = strict_topological_sparse_layer_ids(sparse_rows)
    capsule_order = tuple(row.layer_id for row in capsule_rows)
    if order != capsule_order:
        raise ValueError(
            "selected authority capsules do not preserve the stable topological "
            "layer order"
        )
    try:
        payer_index = order.index(payer)
    except ValueError as exc:
        raise ValueError(
            f"judgment replay payer layer {payer!r} is absent from selected authority"
        ) from exc
    try:
        layers_path = selected_authority.artifact_paths["layers.json"]
    except KeyError as exc:
        raise ValueError(
            "selected judgment replay authority omits layers.json"
        ) from exc
    selected_layers = load_layers_from_path(layers_path)
    if set(selected_layers) != set(order):
        raise ValueError(
            "selected executable layer view does not contain the exact global-DAG "
            "layer set"
        )
    prefix: list[SelectedJudgmentReplayLayer] = []
    for index, layer_id in enumerate(order[: payer_index + 1]):
        layer = selected_layers[layer_id]
        if layer.execution != "ready":
            raise ValueError(
                "judgment replay prefix contains an unmaterialized selected layer: "
                f"index={index}, layer={layer_id!r}"
            )
        prefix.append(SelectedJudgmentReplayLayer(layer, capsule_rows[index]))
    captured.require_sources_unchanged()
    return CapturedSelectedJudgmentReplayPrefix(tuple(prefix), captured)


def _claim_predecessor_identity(
    receipt: LayerFinalizationReceipt,
) -> tuple[str, str, str, str]:
    return (
        receipt.claim.layer_id,
        receipt.receipt_digest,
        receipt.layer_script_path,
        receipt.layer_script_sha256,
    )


def _observed_predecessor_identity(
    claim: LayerFinalizationClaim,
) -> tuple[tuple[str, str, str, str], ...]:
    return tuple(
        (
            row.layer_id,
            row.finalization_receipt_digest,
            row.script_path,
            row.script_sha256,
        )
        for row in claim.predecessor_inputs
    )


def _require_terminal_matches_selected_layer(
    receipt: LayerFinalizationReceipt,
    selected: SelectedJudgmentReplayLayer,
    predecessors: tuple[LayerFinalizationReceipt, ...],
    *,
    require_passed: bool,
) -> None:
    layer = selected.layer
    capsule = selected.capsule
    expected_units = tuple(
        (
            unit.id,
            unit_state.unit_digest(unit),
            unit.mutates.script_spans[0],
        )
        for unit in dependency_ordered_units(layer.stages)
    )
    observed_units = tuple(
        (row.unit_id, row.unit_digest, row.script_path)
        for row in receipt.claim.unit_inputs
    )
    if (
        receipt.claim.layer_id != str(layer.id)
        or receipt.claim.plan_hash != capsule.capsule_digest
        or receipt.layer_script_path != str(layer.script)
        or observed_units != expected_units
    ):
        raise ValueError(
            f"layer {layer.id} terminal receipt does not bind its exact selected "
            "layer capsule, script, and unit order"
        )
    expected_predecessors = tuple(
        _claim_predecessor_identity(row) for row in predecessors
    )
    if _observed_predecessor_identity(receipt.claim) != expected_predecessors:
        raise ValueError(
            f"layer {layer.id} terminal receipt does not bind the exact stable "
            "selected-layer predecessor prefix"
        )
    if require_passed and receipt.final_status != "passed":
        raise ValueError(
            f"judgment replay predecessor {layer.id} terminal status must be "
            f"'passed', found {receipt.final_status!r}"
        )


def _current_authorized_terminal(
    shot_folder: str | Path,
    selected_authority: ResolvedSelectedAuthority,
    selected: SelectedJudgmentReplayLayer,
    predecessors: tuple[LayerFinalizationReceipt, ...],
    *,
    require_passed: bool,
) -> LayerFinalizationReceipt:
    layer_id = str(selected.layer.id)
    receipt = layer_finalization_state.current_layer_finalization_receipt(
        shot_folder,
        layer_id,
    )
    if receipt is None:
        raise ValueError(
            f"judgment replay layer {layer_id} has no current terminal receipt"
        )
    layer_finalization_state.authorize_terminal_layer_finalization_mutation(
        shot_folder,
        receipt,
        dependency_ordered_units(selected.layer.stages),
        selected_authority,
    )
    _require_terminal_matches_selected_layer(
        receipt,
        selected,
        predecessors,
        require_passed=require_passed,
    )
    return receipt


def authorize_observation_replay_prefix(
    shot_folder: str | Path,
    selected_authority: ResolvedSelectedAuthority,
    selected_prefix: CapturedSelectedJudgmentReplayPrefix,
    payer_claim: LayerFinalizationClaim,
) -> tuple[LayerFinalizationReceipt, ...]:
    """Authorize predecessor terminals and the payer's active pre-terminal claim."""

    if not selected_prefix.layers:
        raise ValueError("judgment observation replay prefix must be non-empty")
    selected_projection = parse_authority_selection_token(
        selected_authority.selection_token.to_dict(),
        "judgment replay selected authority token",
    )
    payer = selected_prefix.layers[-1]
    if (
        payer_claim.layer_id != str(payer.layer.id)
        or payer_claim.plan_hash != payer.capsule.capsule_digest
        or payer_claim.selection_token != selected_projection
        or payer_claim.layer_script_path != str(payer.layer.script)
    ):
        raise ValueError(
            "judgment observation payer claim does not bind the exact selected payer"
        )
    predecessor_receipts: list[LayerFinalizationReceipt] = []
    for selected in selected_prefix.layers[:-1]:
        predecessor_receipts.append(
            _current_authorized_terminal(
                shot_folder,
                selected_authority,
                selected,
                tuple(predecessor_receipts),
                require_passed=True,
            )
        )
    if _observed_predecessor_identity(payer_claim) != tuple(
        _claim_predecessor_identity(row) for row in predecessor_receipts
    ):
        raise ValueError(
            "judgment observation payer claim does not bind the exact stable "
            "selected-layer predecessor prefix"
        )
    with layer_finalization_state.active_layer_finalization_guard(
        shot_folder,
        payer_claim,
        dependency_ordered_units(payer.layer.stages),
        selection_token=selected_authority.selection_token,
    ):
        pass
    selected_prefix.require_sources_unchanged()
    return tuple(predecessor_receipts)


def require_payment_replay_prefix_current(
    shot_folder: str | Path,
    selected_authority: ResolvedSelectedAuthority,
    selected_prefix: CapturedSelectedJudgmentReplayPrefix,
    replay_prefix: ReplayPrefixReceipt,
) -> tuple[LayerFinalizationReceipt, ...]:
    """Require exact terminal authority across the observation→payment phase split."""

    expected_ids = tuple(str(row.layer.id) for row in selected_prefix.layers)
    observed_ids = tuple(row.layer_id for row in replay_prefix.layers)
    if observed_ids != expected_ids:
        raise ValueError(
            "judgment replay receipt is not the exact stable selected-layer prefix: "
            f"expected={list(expected_ids)}, observed={list(observed_ids)}"
        )
    terminal_receipts: list[LayerFinalizationReceipt] = []
    for index, (selected, replay_layer) in enumerate(
        zip(selected_prefix.layers, replay_prefix.layers, strict=True)
    ):
        capsule = selected.capsule
        if (
            replay_layer.layer_generation_digest != capsule.capsule_digest
            or replay_layer.predecessor_layer_digests
            != capsule.predecessor_layer_digests
            or replay_layer.script_path != str(selected.layer.script)
        ):
            raise ValueError(
                f"judgment replay layer {replay_layer.layer_id} does not bind its "
                "exact selected capsule, dependencies, and script"
            )
        terminal = _current_authorized_terminal(
            shot_folder,
            selected_authority,
            selected,
            tuple(terminal_receipts),
            require_passed=index < len(selected_prefix.layers) - 1,
        )
        expected_units = tuple(
            (
                row.unit_id,
                row.unit_digest,
                row.completion_receipt_digest,
                row.script_path,
                row.script_sha256,
            )
            for row in terminal.claim.unit_inputs
        )
        observed_units = tuple(
            (
                row.unit_id,
                row.unit_digest,
                row.completion_receipt_digest,
                row.script_path,
                row.script_sha256,
            )
            for row in replay_layer.units
        )
        if observed_units != expected_units:
            raise ValueError(
                f"judgment replay layer {replay_layer.layer_id} unit rows do not "
                "match its exact dependency-ordered terminal claim inputs"
            )
        if index < len(selected_prefix.layers) - 1:
            if replay_layer.finalization_receipt_digest != terminal.receipt_digest:
                raise ValueError(
                    f"judgment replay predecessor {replay_layer.layer_id} terminal "
                    "receipt changed"
                )
        elif replay_layer.payer_claim_id != terminal.claim.claim_id:
            raise ValueError(
                "judgment replay payer terminal receipt belongs to another "
                "observation claim"
            )
        terminal_receipts.append(terminal)

    recorded_inputs = tuple(
        (
            row.script_path,
            row.script_sha256,
            row.dependencies,
        )
        for row in replay_prefix.layers
    )
    payer_terminal = terminal_receipts[-1]
    for index, binding in enumerate(
        payer_terminal.evaluation_receipt.replay_receipts
    ):
        stored_replay = layer_replay_receipts.load_layer_replay_receipt(
            shot_folder,
            binding.locator,
            expected_sha256=binding.sha256,
            expected_digest=binding.receipt.receipt_digest,
        )
        if stored_replay.receipt != binding.receipt:
            raise ValueError(
                f"payer terminal replay group {index} bytes disagree with evaluation"
            )
        executed_inputs = tuple(
            (
                row.script_path,
                row.script_sha256,
                row.dependencies,
            )
            for row in stored_replay.receipt.replay_inputs
        )
        if recorded_inputs != executed_inputs:
            raise ValueError(
                "judgment payment replay prefix does not match the exact executed "
                f"layer scripts and dependency bindings in payer group {index}"
            )
        if stored_replay.receipt.observation.replay_prefix != replay_prefix:
            raise ValueError(
                "judgment payment replay prefix differs from the exact payer "
                f"terminal replay identity in group {index}"
            )
    selected_prefix.require_sources_unchanged()
    return tuple(terminal_receipts)


__all__ = [
    "CapturedSelectedJudgmentReplayPrefix",
    "SelectedJudgmentReplayLayer",
    "authorize_observation_replay_prefix",
    "capture_selected_judgment_replay_prefix",
    "require_payment_replay_prefix_current",
]
