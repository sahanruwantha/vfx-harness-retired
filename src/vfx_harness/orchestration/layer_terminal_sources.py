"""External source closure for one immutable layer terminal receipt."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from vfx_harness.domain.layer_finalizations import LayerFinalizationReceipt
from vfx_harness.infrastructure.trusted_files import (
    TrustedFileBinding,
    TrustedFileSnapshot,
    require_trusted_file_unchanged,
)
from vfx_harness.orchestration import plan_bundle_integrity
from vfx_harness.orchestration.layer_evaluation_receipts import (
    StoredLayerEvaluationReceipt,
    load_layer_evaluation_receipt,
)
from vfx_harness.orchestration.layer_replay_receipts import (
    StoredLayerReplayReceipt,
    load_layer_replay_receipt,
)


class LayerTerminalSourceConflict(ValueError):
    """A terminal receipt's immutable or content-addressed source changed."""


@dataclass(frozen=True, slots=True)
class CapturedTerminalLayerSources:
    """Descriptor-bound transitive source closure of one terminal receipt."""

    stored_evaluation: StoredLayerEvaluationReceipt
    stored_replays: tuple[StoredLayerReplayReceipt, ...]
    layer_script: TrustedFileSnapshot
    replay_source_bindings: tuple[TrustedFileBinding, ...]
    observation_source_bindings: tuple[TrustedFileBinding, ...]


def _snapshot(
    shot: Path,
    locator: str,
    expected_sha256: str,
    where: str,
) -> TrustedFileSnapshot:
    snapshot = plan_bundle_integrity.read_real_file_snapshot(
        shot,
        shot / locator,
        where,
    )
    if snapshot.sha256 != expected_sha256:
        raise LayerTerminalSourceConflict(f"{where} bytes changed: {locator}")
    return snapshot


def capture_terminal_layer_sources(
    folder: str | Path,
    receipt: LayerFinalizationReceipt,
) -> CapturedTerminalLayerSources:
    """Capture the evaluation, every group receipt, and every named causal byte."""

    if not isinstance(receipt, LayerFinalizationReceipt):
        raise LayerTerminalSourceConflict(
            "terminal source capture requires a typed finalization receipt"
        )
    shot = Path(folder).expanduser().absolute()
    stored_evaluation = load_layer_evaluation_receipt(
        shot,
        receipt.evaluation_receipt_locator,
        expected_sha256=receipt.evaluation_receipt_sha256,
        expected_digest=receipt.evaluation_receipt_digest,
    )
    receipt.assert_matches_evaluation(stored_evaluation.receipt)
    stored_replays: list[StoredLayerReplayReceipt] = []
    replay_sources: list[TrustedFileBinding] = []
    observation_sources: list[TrustedFileBinding] = []
    seen_replay_sources: set[tuple[str, str]] = set()
    seen_observation_sources: set[tuple[str, str]] = set()
    for group_index, binding in enumerate(stored_evaluation.receipt.replay_receipts):
        stored = load_layer_replay_receipt(
            shot,
            binding.locator,
            expected_sha256=binding.sha256,
            expected_digest=binding.receipt.receipt_digest,
        )
        if stored.receipt != binding.receipt:
            raise LayerTerminalSourceConflict(
                f"layer replay group {group_index} bytes disagree with evaluation"
            )
        stored_replays.append(stored)
        for input_index, replay_input in enumerate(stored.receipt.replay_inputs):
            source_key = (replay_input.script_path, replay_input.script_sha256)
            if source_key not in seen_replay_sources:
                replay_sources.append(
                    _snapshot(
                        shot,
                        replay_input.script_path,
                        replay_input.script_sha256,
                        f"terminal layer replay group {group_index} input {input_index}",
                    ).binding
                )
                seen_replay_sources.add(source_key)
            for dependency_index, dependency in enumerate(replay_input.dependencies):
                dependency_key = (dependency.path, dependency.sha256)
                if dependency_key in seen_replay_sources:
                    continue
                replay_sources.append(
                    _snapshot(
                        shot,
                        dependency.path,
                        dependency.sha256,
                        (
                            f"terminal layer replay group {group_index} input "
                            f"{input_index} dependency {dependency_index}"
                        ),
                    ).binding
                )
                seen_replay_sources.add(dependency_key)
        for point_index, point in enumerate(stored.receipt.observation.points):
            ref_key = (point.ref, point.ref_sha256)
            if ref_key not in seen_observation_sources:
                observation_sources.append(
                    _snapshot(
                        shot,
                        point.ref,
                        point.ref_sha256,
                        (
                            f"terminal layer replay group {group_index} point "
                            f"{point_index} reference"
                        ),
                    ).binding
                )
                seen_observation_sources.add(ref_key)
            if point.render is None:
                continue
            assert point.render_sha256 is not None
            render_key = (point.render, point.render_sha256)
            if render_key in seen_observation_sources:
                continue
            observation_sources.append(
                _snapshot(
                    shot,
                    point.render,
                    point.render_sha256,
                    (
                        f"terminal layer replay group {group_index} point "
                        f"{point_index} render"
                    ),
                ).binding
            )
            seen_observation_sources.add(render_key)
        for capture_index, auxiliary in enumerate(
            stored.receipt.observation.auxiliary_captures
        ):
            auxiliary_key = (auxiliary["locator"], auxiliary["sha256"])
            if auxiliary_key in seen_observation_sources:
                continue
            observation_sources.append(
                _snapshot(
                    shot,
                    auxiliary["locator"],
                    auxiliary["sha256"],
                    (
                        f"terminal layer replay group {group_index} auxiliary "
                        f"capture {capture_index}"
                    ),
                ).binding
            )
            seen_observation_sources.add(auxiliary_key)
    layer_script = _snapshot(
        shot,
        receipt.layer_script_path,
        receipt.layer_script_sha256,
        "terminal layer finalization script",
    )
    return CapturedTerminalLayerSources(
        stored_evaluation=stored_evaluation,
        stored_replays=tuple(stored_replays),
        layer_script=layer_script,
        replay_source_bindings=tuple(replay_sources),
        observation_source_bindings=tuple(observation_sources),
    )


def require_terminal_layer_sources_unchanged(
    captured: CapturedTerminalLayerSources,
) -> None:
    if not isinstance(captured, CapturedTerminalLayerSources):
        raise LayerTerminalSourceConflict(
            "terminal layer source guard requires typed captured sources"
        )
    require_trusted_file_unchanged(
        captured.stored_evaluation.source_binding,
        "terminal layer evaluation receipt",
    )
    for replay in captured.stored_replays:
        require_trusted_file_unchanged(
            replay.source_binding,
            "terminal layer replay receipt",
        )
    require_trusted_file_unchanged(
        captured.layer_script.binding,
        "terminal layer finalization script",
    )
    for source in (
        *captured.replay_source_bindings,
        *captured.observation_source_bindings,
    ):
        require_trusted_file_unchanged(source, "terminal layer causal input")


__all__ = [
    "CapturedTerminalLayerSources",
    "LayerTerminalSourceConflict",
    "capture_terminal_layer_sources",
    "require_terminal_layer_sources_unchanged",
]
