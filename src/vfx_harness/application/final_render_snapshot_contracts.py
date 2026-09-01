"""Immutable value contracts for a final-render replay snapshot."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from vfx_harness.infrastructure.trusted_files import (
    TrustedDirectoryBinding,
    TrustedFileBinding,
)

if TYPE_CHECKING:
    from vfx_harness.orchestration.authority_selection import ResolvedSelectedAuthority
    from vfx_harness.orchestration.ledger import Layer


class FinalRenderSnapshotError(ValueError):
    """The accepted render inputs cannot form or retain one exact snapshot."""


@dataclass(frozen=True, slots=True)
class _FileBinding:
    path: Path
    sha256: str | None
    trusted: TrustedFileBinding | None


@dataclass(frozen=True, slots=True)
class _ReplayBinding:
    source: _FileBinding
    snapshot: _FileBinding
    payload: bytes


@dataclass(frozen=True, slots=True)
class _ConstructionCapture:
    pointer_data: bytes
    glb_data: bytes
    sha256: str
    unit_digest: str
    view_count: int


@dataclass(frozen=True, slots=True)
class FinalRenderSnapshot:
    """One accepted outcome bound to immutable replay bytes and durable state."""

    selected_authority: ResolvedSelectedAuthority
    outcome_digest: str
    authority_digest: str
    chain_digest: str
    authored_inputs_digest: str
    decision_inputs_digest: str
    asset_tree_digest: str
    layers: tuple[Layer, ...]
    ledger: _FileBinding
    layer_outcomes: tuple[_FileBinding, ...]
    selected_artifacts: tuple[_FileBinding, ...]
    evidence_files: tuple[_FileBinding, ...]
    source_files: tuple[_FileBinding, ...]
    unit_state_digests: tuple[tuple[str, str], ...]
    replay: tuple[_ReplayBinding, ...]
    replay_dependencies: tuple[_FileBinding, ...]
    replay_root: Path
    replay_root_binding: TrustedDirectoryBinding
    assets_dir: Path
    manifest: Path

    @property
    def replay_scripts(self) -> tuple[Path, ...]:
        return tuple(binding.snapshot.path for binding in self.replay)

    @property
    def worker_file_bindings(self) -> tuple[TrustedFileBinding, ...]:
        """Exact snapshot members mounted into the confined worker by descriptor."""

        rows = (
            *(row.snapshot for row in self.replay),
            *self.replay_dependencies,
        )
        bindings: list[TrustedFileBinding] = []
        seen: set[Path] = set()
        for row in rows:
            if row.sha256 is None or row.trusted is None:
                raise FinalRenderSnapshotError(
                    f"worker-readable snapshot member has no trusted binding: {row.path}"
                )
            if row.path not in seen:
                bindings.append(row.trusted)
                seen.add(row.path)
        return tuple(bindings)
