"""Mint an exact selected-DAG replay prefix for qualitative judgment."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from vfx_harness.domain.judgment_debt_replay_receipts import (
    ReplayPrefixLayerReceipt,
    ReplayPrefixReceipt,
    ReplayPrefixUnitReceipt,
)
from vfx_harness.domain.layer_finalizations import LayerFinalizationClaim
from vfx_harness.domain.unit_completion_receipts import UnitCompletionReceipt
from vfx_harness.domain.unit_evaluation_receipts import ReplayDependencyBinding
from vfx_harness.domain.work_units import dependency_ordered_units
from vfx_harness.infrastructure.trusted_files import (
    TrustedFileBinding,
    TrustedFileError,
    read_trusted_file,
    require_trusted_file_unchanged,
)
from vfx_harness.orchestration import (
    judgment_debt_replay_authority,
    unit_state,
)
from vfx_harness.orchestration.authority_selection import ResolvedSelectedAuthority
from vfx_harness.orchestration.unit_completion_state import (
    authorize_completed_units_for_layer,
)
from vfx_harness.orchestration.unit_evaluation_receipts import (
    ExecutedReplayDependency,
    ExecutedReplayInput,
)


def _require_sha256(value: str, where: str) -> None:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"{where} must be a lowercase SHA-256 digest")


def require_replay_inputs_unchanged(
    shot_folder: str | Path,
    replay_inputs: Sequence[ExecutedReplayInput],
    *,
    where: str = "judgment-debt replay input",
) -> None:
    """Retain the exact executed layer-source lineage through debt publication."""

    if isinstance(replay_inputs, (str, bytes)) or not isinstance(
        replay_inputs,
        Sequence,
    ):
        raise ValueError("replay_inputs must be a sequence of ExecutedReplayInput")
    if not replay_inputs:
        raise ValueError("replay_inputs must be non-empty")
    shot = Path(shot_folder).expanduser().absolute()
    seen: set[str] = set()
    try:
        for index, replay_input in enumerate(replay_inputs):
            if not isinstance(replay_input, ExecutedReplayInput):
                raise ValueError(
                    f"replay_inputs[{index}] must be an ExecutedReplayInput"
                )
            locator = replay_input.script_path
            if (
                not isinstance(locator, str)
                or not locator
                or Path(locator).is_absolute()
                or ".." in Path(locator).parts
                or Path(locator).as_posix() != locator
            ):
                raise ValueError(
                    f"replay_inputs[{index}].script_path must be a canonical "
                    "shot-relative path"
                )
            if locator in seen:
                raise ValueError(f"replay_inputs repeats script path {locator!r}")
            seen.add(locator)
            _require_sha256(
                replay_input.script_sha256,
                f"replay_inputs[{index}].script_sha256",
            )
            binding = replay_input.source_binding
            if not isinstance(binding, TrustedFileBinding):
                raise ValueError(
                    f"replay_inputs[{index}].source_binding must be a trusted-file binding"
                )
            expected_path = shot / locator
            if (
                binding.root != shot
                or binding.path != expected_path
                or binding.relative != locator
            ):
                raise ValueError(
                    f"replay_inputs[{index}] is not bound to canonical path {locator!r}"
                )
            require_trusted_file_unchanged(binding, where)
            for dependency_index, dependency in enumerate(replay_input.dependencies):
                dependency_where = (
                    f"replay_inputs[{index}].dependencies[{dependency_index}]"
                )
                if not isinstance(dependency, ExecutedReplayDependency):
                    raise ValueError(
                        f"{dependency_where} must be an ExecutedReplayDependency"
                    )
                try:
                    dependency_row = ReplayDependencyBinding.mint(
                        kind=dependency.kind,
                        path=dependency.path,
                        sha256=dependency.sha256,
                        where=dependency_where,
                    )
                except ValueError as exc:
                    raise ValueError(str(exc)) from exc
                dependency_binding = dependency.source_binding
                if (
                    not isinstance(dependency_binding, TrustedFileBinding)
                    or dependency_binding.root != shot
                    or dependency_binding.path != shot / dependency_row.path
                    or dependency_binding.relative != dependency_row.path
                ):
                    raise ValueError(
                        f"{dependency_where} is not bound to its canonical path"
                    )
                require_trusted_file_unchanged(dependency_binding, where)
    except TrustedFileError as exc:
        raise ValueError(str(exc)) from exc


def _shot_relative_layer_script(shot: Path, path: str | Path) -> str:
    candidate = Path(path)
    if candidate.is_absolute():
        try:
            return candidate.resolve().relative_to(shot).as_posix()
        except ValueError as exc:
            raise ValueError(
                f"replayed layer script {candidate} escapes shot root {shot}"
            ) from exc
    return candidate.as_posix()


def replay_prefix_receipt(
    shot_folder: str | Path,
    *,
    replayed_layer_scripts: Sequence[str | Path],
    replay_inputs: Sequence[ExecutedReplayInput],
    selected_authority: ResolvedSelectedAuthority,
    finalization_claim: LayerFinalizationClaim | None = None,
) -> ReplayPrefixReceipt:
    """Issue the exact selected-DAG prefix consumed by one payer observation."""

    if isinstance(replayed_layer_scripts, (str, bytes)) or not isinstance(
        replayed_layer_scripts,
        Sequence,
    ):
        raise ValueError("replayed_layer_scripts must be a sequence of layer script paths")
    if not isinstance(finalization_claim, LayerFinalizationClaim):
        raise ValueError(
            "judgment-debt replay receipt requires an exact payer finalization claim"
        )
    shot = Path(shot_folder).expanduser().absolute()
    require_replay_inputs_unchanged(shot, replay_inputs)
    selected_prefix = (
        judgment_debt_replay_authority.capture_selected_judgment_replay_prefix(
            shot,
            selected_authority,
            finalization_claim.layer_id,
        )
    )
    expected_layers = selected_prefix.layers
    if (
        len(replayed_layer_scripts) != len(replay_inputs)
        or len(replay_inputs) != len(expected_layers)
    ):
        raise ValueError(
            "replayed layer scripts and inputs must be the exact stable selected-DAG "
            "prefix through the payer"
        )
    predecessor_receipts = (
        judgment_debt_replay_authority.authorize_observation_replay_prefix(
            shot,
            selected_authority,
            selected_prefix,
            finalization_claim,
        )
    )
    predecessor_by_layer = {
        receipt.claim.layer_id: receipt for receipt in predecessor_receipts
    }
    claim_inputs = {
        row.unit_id: row for row in finalization_claim.unit_inputs
    }
    replayed: list[ReplayPrefixLayerReceipt] = []
    unit_bindings: list[TrustedFileBinding] = []
    for index, selected in enumerate(expected_layers):
        layer = selected.layer
        capsule = selected.capsule
        relative = _shot_relative_layer_script(
            shot,
            replayed_layer_scripts[index],
        )
        if relative != str(layer.script):
            raise ValueError(
                "replayed layer order does not match the exact stable selected-DAG "
                f"prefix: index={index}, expected={layer.script!r}, found={relative!r}"
            )
        replay_input = replay_inputs[index]
        if replay_input.script_path != relative:
            raise ValueError(
                "executed replay input order does not match selected layer scripts: "
                f"expected {relative!r}, found {replay_input.script_path!r}"
            )
        try:
            layer_snapshot = read_trusted_file(
                shot,
                shot / relative,
                f"replayed layer {layer.id} causal input",
                require_nonempty=True,
            )
        except TrustedFileError as exc:
            raise ValueError(str(exc)) from exc
        if (
            layer_snapshot.binding != replay_input.source_binding
            or layer_snapshot.sha256 != replay_input.script_sha256
        ):
            raise ValueError(
                f"replayed layer {layer.id} current artifact does not match the exact "
                "bytes executed by the evaluator"
            )
        if (
            str(layer.id) == finalization_claim.layer_id
            and finalization_claim.layer_script_sha256
            != replay_input.script_sha256
        ):
            raise ValueError(
                "replayed payer layer bytes do not match the active finalization claim"
            )
        dependency_rows: list[ReplayDependencyBinding] = []
        for dependency_index, dependency in enumerate(replay_input.dependencies):
            dependency_row = ReplayDependencyBinding.mint(
                kind=dependency.kind,
                path=dependency.path,
                sha256=dependency.sha256,
                where=f"replayed layer {layer.id} dependency {dependency_index}",
            )
            try:
                dependency_snapshot = read_trusted_file(
                    shot,
                    shot / dependency_row.path,
                    f"replayed layer {layer.id} {dependency_row.kind}",
                    require_nonempty=True,
                )
            except TrustedFileError as exc:
                raise ValueError(str(exc)) from exc
            if (
                dependency_snapshot.binding != dependency.source_binding
                or dependency_snapshot.sha256 != dependency_row.sha256
            ):
                raise ValueError(
                    f"replayed layer {layer.id} {dependency_row.kind} does not "
                    "match the exact bytes consumed by the evaluator"
                )
            dependency_rows.append(dependency_row)
        state = unit_state.load(shot, str(layer.id))
        unit_state.validate_current(state, str(layer.id), layer.stages)
        if not state:
            raise ValueError(
                f"replayed layer {layer.id} has no durable work-unit state"
            )
        ordered_units = dependency_ordered_units(layer.stages)
        authorized = authorize_completed_units_for_layer(
            shot,
            str(layer.id),
            ordered_units,
            expected_plan_hash=capsule.capsule_digest,
            selected_authority=selected_authority,
        )
        if authorized.unit_ids != frozenset(unit.id for unit in ordered_units):
            raise ValueError(
                f"replayed layer {layer.id} does not have an exact complete "
                "coordinator-authorized unit receipt set"
            )
        units: list[ReplayPrefixUnitReceipt] = []
        for unit in ordered_units:
            row = (state.get("units") or {}).get(unit.id) or {}
            identity = f"{layer.id}:{unit.id}"
            expected_unit_digest = unit_state.unit_digest(unit)
            if (
                row.get("status") != "passed"
                or row.get("unit_hash") != expected_unit_digest
            ):
                raise ValueError(
                    f"replayed payer unit {identity} is not an exact current passed unit"
                )
            completion = UnitCompletionReceipt.parse(
                row.get("completion_receipt"),
                f"replayed payer unit {identity} completion receipt",
            )
            if authorized.receipt_digest(unit.id) != completion.receipt_digest:
                raise ValueError(
                    f"replayed payer unit {identity} has no current coordinator "
                    "authorization"
                )
            checkpoint = row.get("checkpoint")
            if not isinstance(checkpoint, dict):
                raise ValueError(
                    f"replayed payer unit {identity} has no accepted checkpoint"
                )
            if checkpoint.get("unit_hash") != expected_unit_digest:
                raise ValueError(
                    f"replayed payer unit {identity} checkpoint names a stale unit digest"
                )
            if len(unit.mutates.script_spans) != 1:
                raise ValueError(
                    f"replayed payer unit {identity} must own exactly one replay artifact"
                )
            artifact = shot / unit.mutates.script_spans[0]
            try:
                artifact_snapshot = read_trusted_file(
                    shot,
                    artifact,
                    f"replayed payer unit {identity} artifact",
                    require_nonempty=True,
                )
            except TrustedFileError as exc:
                raise ValueError(str(exc)) from exc
            script_hash = artifact_snapshot.sha256
            if checkpoint.get("script_hash") != script_hash:
                raise ValueError(
                    f"replayed payer unit {identity} artifact changed after its checkpoint"
                )
            if str(layer.id) == finalization_claim.layer_id:
                claim_input = claim_inputs.get(unit.id)
                if claim_input is None or (
                    claim_input.unit_digest,
                    claim_input.completion_receipt_digest,
                    claim_input.script_path,
                    claim_input.script_sha256,
                ) != (
                    expected_unit_digest,
                    completion.receipt_digest,
                    unit.mutates.script_spans[0],
                    script_hash,
                ):
                    raise ValueError(
                        f"replayed payer unit {identity} does not match the active "
                        "layer-finalization claim"
                    )
            unit_bindings.append(artifact_snapshot.binding)
            units.append(
                ReplayPrefixUnitReceipt(
                    layer_id=str(layer.id),
                    unit_id=str(unit.id),
                    unit_digest=expected_unit_digest,
                    checkpoint_unit_digest=str(checkpoint["unit_hash"]),
                    script_path=unit.mutates.script_spans[0],
                    script_sha256=script_hash,
                    checkpoint_script_sha256=str(checkpoint["script_hash"]),
                    completion_receipt_digest=completion.receipt_digest,
                )
            )
        predecessor = predecessor_by_layer.get(str(layer.id))
        if predecessor is not None:
            expected_terminal_units = tuple(
                (
                    row.unit_id,
                    row.unit_digest,
                    row.completion_receipt_digest,
                    row.script_path,
                    row.script_sha256,
                )
                for row in predecessor.claim.unit_inputs
            )
            observed_replay_units = tuple(
                (
                    row.unit_id,
                    row.unit_digest,
                    row.completion_receipt_digest,
                    row.script_path,
                    row.script_sha256,
                )
                for row in units
            )
            if observed_replay_units != expected_terminal_units:
                raise ValueError(
                    f"replayed predecessor layer {layer.id} units do not match its "
                    "exact dependency-ordered terminal claim inputs"
                )
        replayed.append(
            ReplayPrefixLayerReceipt(
                layer_id=str(layer.id),
                layer_generation_digest=capsule.capsule_digest,
                predecessor_layer_digests=capsule.predecessor_layer_digests,
                script_path=relative,
                script_sha256=replay_input.script_sha256,
                dependencies=tuple(dependency_rows),
                units=tuple(units),
                finalization_receipt_digest=(
                    None if predecessor is None else predecessor.receipt_digest
                ),
                payer_claim_id=(
                    finalization_claim.claim_id
                    if str(layer.id) == finalization_claim.layer_id
                    else None
                ),
            )
        )
    receipt = ReplayPrefixReceipt(tuple(replayed))
    require_replay_inputs_unchanged(shot, replay_inputs)
    try:
        for binding in unit_bindings:
            require_trusted_file_unchanged(
                binding,
                "judgment-debt payer unit causal input",
            )
    except TrustedFileError as exc:
        raise ValueError(str(exc)) from exc
    current_predecessors = (
        judgment_debt_replay_authority.authorize_observation_replay_prefix(
            shot,
            selected_authority,
            selected_prefix,
            finalization_claim,
        )
    )
    if current_predecessors != predecessor_receipts:
        raise ValueError(
            "judgment replay predecessor finalization authority changed during mint"
        )
    selected_prefix.require_sources_unchanged()
    return receipt


__all__ = ["replay_prefix_receipt", "require_replay_inputs_unchanged"]
