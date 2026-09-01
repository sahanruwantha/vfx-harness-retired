"""Typed descriptor-bound causal inputs for canonical work-unit replay."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from vfx_harness.domain.unit_evaluation_receipts import (
    ReplayDependencyBinding,
    ReplayInputBinding,
)
from vfx_harness.infrastructure.trusted_files import (
    TrustedFileBinding,
    TrustedFileError,
    require_trusted_file_unchanged,
)
from vfx_harness.orchestration import plan_bundle_integrity


class ReplayInputConflict(ValueError):
    """Executed replay sources do not close against canonical current authority."""


@dataclass(frozen=True, slots=True)
class ExecutedReplayDependency:
    """One descriptor-read non-script source consumed by artifact replay."""

    kind: str
    path: str
    sha256: str
    source_binding: TrustedFileBinding


@dataclass(frozen=True, slots=True)
class ExecutedReplayInput:
    """One canonical locator plus the exact descriptor-read source sent to Blender."""

    script_path: str
    script_sha256: str
    source_binding: TrustedFileBinding
    dependencies: tuple[ExecutedReplayDependency, ...] = ()


def _require_source_unchanged(binding: TrustedFileBinding) -> None:
    try:
        require_trusted_file_unchanged(binding, "unit evaluation causal input")
    except TrustedFileError as exc:
        raise ReplayInputConflict(
            "unit evaluation causal input changed before publication: "
            f"{binding.path} ({exc})"
        ) from exc


def _read_source_snapshot(shot: Path, path: Path, where: str):
    try:
        return plan_bundle_integrity.read_real_file_snapshot(shot, path, where)
    except plan_bundle_integrity.PlanPublicationError as exc:
        raise ReplayInputConflict(str(exc)) from exc


def prepare_replay_inputs(
    shot: Path,
    values: Sequence[ExecutedReplayInput],
) -> tuple[
    tuple[ReplayInputBinding, ...],
    tuple[TrustedFileBinding, ...],
]:
    """Bind executed sources to the canonical paths that completion will replay."""

    if not isinstance(values, (tuple, list)) or not values:
        raise ReplayInputConflict(
            "unit evaluation requires a non-empty ordered executed replay-input closure"
        )
    receipt_rows: list[ReplayInputBinding] = []
    identities: list[TrustedFileBinding] = []
    seen_paths: set[str] = set()
    for index, value in enumerate(values):
        where = f"unit evaluation replay_inputs[{index}]"
        if not isinstance(value, ExecutedReplayInput):
            raise ReplayInputConflict(f"{where} must be a typed executed replay input")
        try:
            dependencies: list[ReplayDependencyBinding] = []
            dependency_identities: list[TrustedFileBinding] = []
            if not isinstance(value.dependencies, tuple):
                raise ReplayInputConflict(
                    f"{where}.dependencies must be an ordered tuple"
                )
            for dependency_index, dependency in enumerate(value.dependencies):
                dependency_where = f"{where}.dependencies[{dependency_index}]"
                if not isinstance(dependency, ExecutedReplayDependency):
                    raise ReplayInputConflict(
                        f"{dependency_where} must be a typed executed replay dependency"
                    )
                dependency_row = ReplayDependencyBinding.mint(
                    kind=dependency.kind,
                    path=dependency.path,
                    sha256=dependency.sha256,
                    where=dependency_where,
                )
                if (
                    not isinstance(dependency.source_binding, TrustedFileBinding)
                    or dependency.source_binding.root != shot
                ):
                    raise ReplayInputConflict(
                        f"{dependency_where} source binding must belong to the exact shot root"
                    )
                _require_source_unchanged(dependency.source_binding)
                dependency_snapshot = _read_source_snapshot(
                    shot,
                    shot / dependency_row.path,
                    f"{dependency_where} canonical replay dependency",
                )
                if dependency_snapshot.sha256 != dependency_row.sha256:
                    raise ReplayInputConflict(
                        f"{dependency_where} canonical bytes differ from the exact "
                        "source consumed by Blender"
                    )
                dependencies.append(dependency_row)
                dependency_identities.extend(
                    (dependency.source_binding, dependency_snapshot.binding)
                )
            row = ReplayInputBinding.mint(
                script_path=value.script_path,
                script_sha256=value.script_sha256,
                dependencies=tuple(dependencies),
                where=where,
            )
        except ValueError as exc:
            raise ReplayInputConflict(str(exc)) from exc
        if row.script_path in seen_paths:
            raise ReplayInputConflict(
                f"unit evaluation replay closure repeats {row.script_path!r}"
            )
        seen_paths.add(row.script_path)
        if (
            not isinstance(value.source_binding, TrustedFileBinding)
            or value.source_binding.root != shot
        ):
            raise ReplayInputConflict(
                f"{where} source binding must belong to the exact shot root"
            )
        _require_source_unchanged(value.source_binding)
        canonical = _read_source_snapshot(
            shot,
            shot / row.script_path,
            f"{where} canonical replay script",
        )
        if canonical.sha256 != row.script_sha256:
            raise ReplayInputConflict(
                f"{where} canonical bytes differ from the exact source sent to Blender"
            )
        receipt_rows.append(row)
        identities.extend(
            (
                value.source_binding,
                canonical.binding,
                *dependency_identities,
            )
        )
    return tuple(receipt_rows), tuple(dict.fromkeys(identities))
