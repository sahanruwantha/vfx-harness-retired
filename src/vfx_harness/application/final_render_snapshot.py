"""Immutable accepted-chain inputs for final media replay and publication."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vfx_harness.agents import acceptance_stop
from vfx_harness.domain.acceptance_outcomes import AcceptanceOutcome
from vfx_harness.domain.brief import Shot
from vfx_harness.domain.refobs import PROMOTED_CONSTRUCTION_SCHEMA
from vfx_harness.domain.stop_envelope_primitives import canonical_digest, require_digest
from vfx_harness.infrastructure.trusted_files import (
    TrustedDirectoryBinding,
    TrustedFileBinding,
    TrustedFileError,
    TrustedFileNotFound,
    bind_trusted_directory,
    read_trusted_file,
    require_trusted_directory_unchanged,
    require_trusted_file_unchanged,
)
from vfx_harness.orchestration import unit_state
from vfx_harness.orchestration.authority_selection import ResolvedSelectedAuthority
from vfx_harness.orchestration.generate_construction import (
    FINAL_RENDER_CONSTRUCTION_POINTER_SCHEMA,
)
from vfx_harness.orchestration.ledger import ledger_lock, load_milestones
from vfx_harness.orchestration.plan_inputs import (
    authored_input_bytes,
    decision_input_bytes,
)
from vfx_harness.orchestration.selected_layer_chain import selected_layer_chain
from vfx_harness.orchestration.unit_state_lock import unit_state_lock, unit_state_path

if TYPE_CHECKING:
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


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _binding(
    root: Path,
    path: Path,
    *,
    required: bool,
    where: str,
) -> tuple[_FileBinding, bytes | None]:
    """Read one exact leaf and retain its complete named ancestor lineage."""

    try:
        snapshot = read_trusted_file(
            root,
            path,
            where,
            require_nonempty=False,
        )
    except TrustedFileNotFound:
        if not required:
            return _FileBinding(path=path, sha256=None, trusted=None), None
        raise FinalRenderSnapshotError(f"{where} is missing: {path}") from None
    except TrustedFileError as exc:
        raise FinalRenderSnapshotError(str(exc)) from exc
    return (
        _FileBinding(
            path=snapshot.binding.path,
            sha256=snapshot.sha256,
            trusted=snapshot.binding,
        ),
        snapshot.payload,
    )


def _inside(root: Path, relative: object, where: str) -> tuple[str, Path]:
    if not isinstance(relative, str) or not relative or relative != relative.strip():
        raise FinalRenderSnapshotError(f"{where} must be a non-empty relative path")
    rel = Path(relative)
    if rel.is_absolute() or any(part in {"", ".", ".."} for part in rel.parts):
        raise FinalRenderSnapshotError(f"{where} must be a normalized in-shot path: {relative!r}")
    path = root.joinpath(*rel.parts)
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise FinalRenderSnapshotError(f"{where} escapes the shot root: {relative!r}") from exc
    if resolved != path:
        raise FinalRenderSnapshotError(f"{where} contains a symlink component: {relative!r}")
    return rel.as_posix(), path


def _require_outcome_matches_authority(
    outcome: AcceptanceOutcome,
    authority: acceptance_stop.AcceptanceAuthoritySnapshot,
) -> None:
    if (
        outcome.authority_digest != authority.digest
        or outcome.bundle_digest != authority.bundle_digest
        or outcome.view_digest != authority.view_digest
        or outcome.chain_digest != authority.chain_digest
    ):
        raise FinalRenderSnapshotError(
            "accepted outcome changed before its final-render replay snapshot was captured"
        )


def _expected_chain_files(
    root: Path,
    authority: acceptance_stop.AcceptanceAuthoritySnapshot,
) -> tuple[dict[str, str], tuple[str, ...]]:
    expected: dict[str, str] = {}
    replay: list[str] = []
    for layer_index, raw_layer in enumerate(authority.chain):
        where = f"acceptance chain[{layer_index}]"
        if not isinstance(raw_layer, Mapping):
            raise FinalRenderSnapshotError(f"{where} must be an object")
        if raw_layer.get("status") != "passed":
            raise FinalRenderSnapshotError(f"{where} is not an accepted layer checkpoint")
        relative, _path = _inside(root, raw_layer.get("script"), f"{where}.script")
        script_digest = require_digest(raw_layer.get("script_sha256"), f"{where}.script_sha256")
        if relative in expected and expected[relative] != script_digest:
            raise FinalRenderSnapshotError(f"{where}.script has conflicting accepted digests")
        expected[relative] = script_digest
        replay.append(relative)
        units = raw_layer.get("units")
        if not isinstance(units, list):
            raise FinalRenderSnapshotError(f"{where}.units must be a list")
        for unit_index, raw_unit in enumerate(units):
            unit_where = f"{where}.units[{unit_index}]"
            if not isinstance(raw_unit, Mapping):
                raise FinalRenderSnapshotError(f"{unit_where} must be an object")
            unit_relative, _unit_path = _inside(
                root,
                raw_unit.get("script"),
                f"{unit_where}.script",
            )
            unit_script_digest = require_digest(
                raw_unit.get("script_sha256"),
                f"{unit_where}.script_sha256",
            )
            if unit_relative in expected and expected[unit_relative] != unit_script_digest:
                raise FinalRenderSnapshotError(
                    f"{unit_where}.script has conflicting accepted digests"
                )
            expected[unit_relative] = unit_script_digest
    if len(replay) != len(set(replay)):
        raise FinalRenderSnapshotError("acceptance chain contains duplicate replay scripts")
    return expected, tuple(replay)


def _capture_source_files(
    root: Path,
    expected: Mapping[str, str],
) -> tuple[
    tuple[_FileBinding, ...],
    dict[str, bytes],
    tuple[_FileBinding, ...],
    dict[str, _ConstructionCapture],
]:
    bindings: list[_FileBinding] = []
    data_by_relative: dict[str, bytes] = {}
    dependencies: dict[Path, _FileBinding] = {}
    construction: dict[str, _ConstructionCapture] = {}
    for relative, expected_digest in expected.items():
        _relative, path = _inside(root, relative, f"accepted script {relative}")
        binding, data = _binding(
            root,
            path,
            required=True,
            where=f"accepted script {relative}",
        )
        assert data is not None
        if binding.sha256 != expected_digest:
            raise FinalRenderSnapshotError(
                f"accepted script {relative} changed before snapshot; "
                f"expected={expected_digest}, found={binding.sha256}"
            )
        bindings.append(binding)
        data_by_relative[relative] = data

        pointer = path.with_suffix(".construction.json")
        pointer_binding, pointer_data = _binding(
            root,
            pointer,
            required=False,
            where=f"construction pointer for {relative}",
        )
        dependencies[pointer] = pointer_binding
        if pointer_data is None:
            continue
        try:
            pointer_row = json.loads(pointer_data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FinalRenderSnapshotError(
                f"construction pointer for {relative} is invalid JSON"
            ) from exc
        if (
            not isinstance(pointer_row, Mapping)
            or pointer_row.get("schema") != PROMOTED_CONSTRUCTION_SCHEMA
            or not pointer_row.get("glb")
        ):
            raise FinalRenderSnapshotError(
                f"construction pointer for {relative} does not name its promoted GLB"
            )
        _glb_relative, glb = _inside(
            root,
            pointer_row.get("glb"),
            f"construction GLB for {relative}",
        )
        glb_binding, _glb_data = _binding(
            root,
            glb,
            required=True,
            where=f"construction GLB for {relative}",
        )
        claimed = pointer_row.get("sha256")
        require_digest(claimed, f"construction pointer for {relative}.sha256")
        if glb_binding.sha256 != claimed:
            raise FinalRenderSnapshotError(
                f"construction GLB for {relative} does not match its pointer digest"
            )
        dependencies[glb] = glb_binding
        assert _glb_data is not None
        construction[relative] = _ConstructionCapture(
            pointer_data=pointer_data,
            glb_data=_glb_data,
            sha256=claimed,
            unit_digest=str(pointer_row.get("unit_digest") or ""),
            view_count=int(pointer_row.get("view_count") or 0),
        )
    return (
        tuple(bindings),
        data_by_relative,
        tuple(dependencies.values()),
        construction,
    )


def _capture_evidence_files(root: Path, ledger_data: bytes) -> tuple[_FileBinding, ...]:
    try:
        ledger = json.loads(ledger_data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FinalRenderSnapshotError("final-render ledger is invalid JSON") from exc
    acceptance = ledger.get("acceptance") if isinstance(ledger, Mapping) else None
    moments = acceptance.get("moments") if isinstance(acceptance, Mapping) else None
    if not isinstance(moments, Mapping) or not moments:
        raise FinalRenderSnapshotError("final-render ledger has no acceptance moment evidence")
    paths: dict[Path, _FileBinding] = {}
    for moment_id, raw in moments.items():
        if not isinstance(raw, Mapping):
            raise FinalRenderSnapshotError(
                f"acceptance moment {moment_id} evidence must be an object"
            )
        for field in ("render", "ref"):
            _relative, path = _inside(
                root,
                raw.get(field),
                f"acceptance moment {moment_id}.{field}",
            )
            binding, _data = _binding(
                root,
                path,
                required=True,
                where=f"acceptance moment {moment_id}.{field}",
            )
            paths[path] = binding
    return tuple(paths.values())


def _tree_entries(
    root: Path,
    relative: str,
    where: str,
) -> tuple[bool, tuple[Path, ...], tuple[Path, ...]]:
    directory = root / relative
    if not directory.exists() and not directory.is_symlink():
        return False, (), ()
    if directory.is_symlink() or not directory.is_dir():
        raise FinalRenderSnapshotError(f"{where} must be a real directory: {directory}")
    directories: list[Path] = []
    files: list[Path] = []
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise FinalRenderSnapshotError(f"{where} contains a symlink: {path}")
        if path.is_dir():
            directories.append(path)
            continue
        if not path.is_file():
            raise FinalRenderSnapshotError(f"{where} contains a non-regular file: {path}")
        files.append(path)
    return True, tuple(directories), tuple(files)


def _tree_identity(
    root: Path,
    relative: str,
    where: str,
) -> tuple[str, tuple[_FileBinding, ...], bool, tuple[Path, ...]]:
    exists, directories, files = _tree_entries(root, relative, where)
    bindings = tuple(
        _binding(root, path, required=True, where=f"{where} file")[0]
        for path in files
    )
    digest = canonical_digest(
        {
            "schema": "vfx-harness.final-render-file-tree/v1",
            "root": relative,
            "exists": exists,
            "directories": [
                path.relative_to(root).as_posix()
                for path in directories
            ],
            "files": [
                {
                    "path": row.path.relative_to(root).as_posix(),
                    "sha256": row.sha256,
                }
                for row in bindings
            ],
        }
    )
    return digest, bindings, exists, directories


def _snapshot_tree(
    root: Path,
    snapshot_root: Path,
    relative: str,
    where: str,
) -> tuple[str, tuple[_FileBinding, ...], tuple[_FileBinding, ...]]:
    digest, source, exists, directories = _tree_identity(root, relative, where)
    target_root = snapshot_root / relative
    if exists:
        target_root.mkdir(parents=True, exist_ok=True)
    for directory in directories:
        (snapshot_root / directory.relative_to(root)).mkdir(parents=True, exist_ok=True)
    snapshots: list[_FileBinding] = []
    for binding in source:
        copied, data = _binding(
            root,
            binding.path,
            required=True,
            where=f"{where} file",
        )
        assert data is not None
        if copied.trusted != binding.trusted or copied.sha256 != binding.sha256:
            raise FinalRenderSnapshotError(f"{where} changed while its snapshot was copied")
        target = snapshot_root / binding.path.relative_to(root)
        snapshots.append(_write_snapshot_file(root, target, data))
    return digest, source, tuple(snapshots)


def _authored_inputs_digest(inputs: Mapping[str, bytes]) -> str:
    return canonical_digest(
        {
            "schema": "vfx-harness.final-render-authored-inputs/v1",
            "inputs": {
                relative: _digest(data)
                for relative, data in sorted(inputs.items())
            },
        }
    )


def _snapshot_authored_inputs(
    root: Path,
    snapshot_root: Path,
) -> tuple[str, tuple[_FileBinding, ...]]:
    inputs = authored_input_bytes(root)
    snapshots: list[_FileBinding] = []
    for relative, data in sorted(inputs.items()):
        snapshots.append(_write_snapshot_file(root, snapshot_root / relative, data))
    return _authored_inputs_digest(inputs), tuple(snapshots)


def _decision_inputs_digest(root: Path) -> str:
    return canonical_digest(
        {
            "schema": "vfx-harness.final-render-decision-inputs/v1",
            "inputs": [
                {"path": relative, "size": len(data), "sha256": _digest(data)}
                for relative, data in sorted(decision_input_bytes(root).items())
            ],
        }
    )


def _unit_state_projection(
    shot: Shot,
    layers: Sequence[Layer],
    expected_scripts: Mapping[str, str],
) -> tuple[tuple[str, str], ...]:
    rows: list[tuple[str, str]] = []
    for layer in layers:
        state = unit_state.load(shot.folder, str(layer.id))
        unit_state.validate_current(state, str(layer.id), layer.stages)
        units = state.get("units") if isinstance(state, Mapping) else None
        if not isinstance(units, Mapping):
            raise FinalRenderSnapshotError(
                f"layer {layer.id} has no durable work-unit checkpoint state"
            )
        for unit in layer.stages:
            raw = units.get(unit.id)
            if not isinstance(raw, Mapping) or raw.get("status") != "passed":
                raise FinalRenderSnapshotError(
                    f"layer {layer.id} unit {unit.id} has no current passed checkpoint"
                )
            checkpoint = raw.get("checkpoint")
            if not isinstance(checkpoint, Mapping):
                raise FinalRenderSnapshotError(
                    f"layer {layer.id} unit {unit.id} has no immutable checkpoint"
                )
            expected_unit_digest = unit_state.unit_digest(unit)
            if checkpoint.get("unit_hash") != expected_unit_digest:
                raise FinalRenderSnapshotError(
                    f"layer {layer.id} unit {unit.id} checkpoint names another unit digest"
                )
            relative = unit.mutates.script_spans[0]
            expected_script_digest = expected_scripts.get(relative)
            if checkpoint.get("script_hash") != expected_script_digest:
                raise FinalRenderSnapshotError(
                    f"layer {layer.id} unit {unit.id} checkpoint names another script digest"
                )
        rows.append(
            (
                str(layer.id),
                canonical_digest(
                    {
                        "schema": "vfx-harness.final-render-unit-state/v1",
                        "layer_id": str(layer.id),
                        "state": state,
                    }
                ),
            )
        )
    return tuple(rows)


def _write_snapshot_file(root: Path, path: Path, data: bytes) -> _FileBinding:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags, 0o400)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    binding, observed = _binding(
        root,
        path,
        required=True,
        where="immutable final-render snapshot member",
    )
    if observed != data:
        raise FinalRenderSnapshotError(
            f"immutable final-render snapshot member changed after write: {path}"
        )
    return binding


def _snapshot_replay_files(
    root: Path,
    snapshot_root: Path,
    replay_relatives: Sequence[str],
    data_by_relative: Mapping[str, bytes],
    construction: Mapping[str, _ConstructionCapture],
) -> tuple[tuple[_ReplayBinding, ...], tuple[_FileBinding, ...]]:
    replay: list[_ReplayBinding] = []
    dependencies: dict[Path, _FileBinding] = {}
    for relative in replay_relatives:
        _relative, source = _inside(root, relative, f"replay script {relative}")
        source_data = data_by_relative[relative]
        source_binding, observed_source = _binding(
            root,
            source,
            required=True,
            where=f"replay source {relative}",
        )
        if observed_source != source_data:
            raise FinalRenderSnapshotError(
                f"replay source {relative} changed before snapshot publication"
            )
        snapshot_path = snapshot_root / relative
        snapshot_binding = _write_snapshot_file(root, snapshot_path, source_data)

        captured_construction = construction.get(relative)
        if captured_construction is not None:
            glb_relative = f"build/construction/{captured_construction.sha256}.glb"
            glb_snapshot = snapshot_root / glb_relative
            if glb_snapshot not in dependencies:
                dependencies[glb_snapshot] = _write_snapshot_file(
                    root,
                    glb_snapshot,
                    captured_construction.glb_data,
                )
            pointer_payload = (
                json.dumps(
                    {
                        "schema": FINAL_RENDER_CONSTRUCTION_POINTER_SCHEMA,
                        "source_pointer_sha256": _digest(
                            captured_construction.pointer_data
                        ),
                        "glb": glb_relative,
                        "sha256": captured_construction.sha256,
                        "unit_digest": captured_construction.unit_digest,
                        "view_count": captured_construction.view_count,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8")
            pointer_snapshot = snapshot_path.with_suffix(".construction.json")
            dependencies[pointer_snapshot] = _write_snapshot_file(
                root,
                pointer_snapshot,
                pointer_payload,
            )
        replay.append(
            _ReplayBinding(
                source=source_binding,
                snapshot=snapshot_binding,
                payload=source_data,
            )
        )
    return tuple(replay), tuple(dependencies.values())


def _assert_binding_current(binding: _FileBinding, where: str) -> None:
    if binding.sha256 is None:
        try:
            current = read_trusted_file(binding.path.parent, binding.path, where)
        except TrustedFileNotFound:
            return
        except TrustedFileError as exc:
            raise FinalRenderSnapshotError(str(exc)) from exc
        raise FinalRenderSnapshotError(
            f"{where} changed during final render; expected absence, "
            f"found={current.sha256}"
        )
    if binding.trusted is None:
        raise FinalRenderSnapshotError(f"{where} has no trusted filesystem binding")
    try:
        require_trusted_file_unchanged(binding.trusted, where)
    except TrustedFileError as exc:
        raise FinalRenderSnapshotError(
            f"{where} changed during final render; expected={binding.sha256}"
        ) from exc


def _manifest_payload(snapshot: FinalRenderSnapshot, root: Path) -> dict[str, Any]:
    def locator(path: Path) -> str:
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            return str(path)

    return {
        "schema": "vfx-harness.final-render-replay-snapshot/v1",
        "selection_token": snapshot.selected_authority.selection_token.to_dict(),
        "outcome_digest": snapshot.outcome_digest,
        "authority_digest": snapshot.authority_digest,
        "chain_digest": snapshot.chain_digest,
        "authored_inputs_digest": snapshot.authored_inputs_digest,
        "decision_inputs_digest": snapshot.decision_inputs_digest,
        "asset_tree_digest": snapshot.asset_tree_digest,
        "ledger": {"path": locator(snapshot.ledger.path), "sha256": snapshot.ledger.sha256},
        "selected_artifacts": [
            {"path": locator(row.path), "sha256": row.sha256}
            for row in snapshot.selected_artifacts
        ],
        "evidence_files": [
            {"path": locator(row.path), "sha256": row.sha256}
            for row in snapshot.evidence_files
        ],
        "source_files": [
            {"path": locator(row.path), "sha256": row.sha256}
            for row in snapshot.source_files
        ],
        "unit_state_digests": dict(snapshot.unit_state_digests),
        "replay": [
            {
                "source": locator(row.source.path),
                "source_sha256": row.source.sha256,
                "snapshot": locator(row.snapshot.path),
                "snapshot_sha256": row.snapshot.sha256,
            }
            for row in snapshot.replay
        ],
        "replay_dependencies": [
            {"path": locator(row.path), "sha256": row.sha256}
            for row in snapshot.replay_dependencies
        ],
        "replay_root": locator(snapshot.replay_root),
        "assets_dir": locator(snapshot.assets_dir),
    }


def capture_final_render_snapshot(
    shot: Shot,
    selected_authority: ResolvedSelectedAuthority,
    outcome: AcceptanceOutcome,
    scripts: Sequence[Path],
    *,
    scratch: Path,
) -> FinalRenderSnapshot:
    """Copy exact accepted replay bytes and bind every declared mutable input."""

    root = shot.folder.resolve()
    authority = acceptance_stop.capture_acceptance_authority(
        shot,
        load_milestones(shot, selected_authority),
        selected_authority,
    )
    _require_outcome_matches_authority(outcome, authority)
    expected_scripts, replay_relatives = _expected_chain_files(root, authority)
    supplied = tuple(path.resolve().relative_to(root).as_posix() for path in scripts)
    if supplied != replay_relatives:
        raise FinalRenderSnapshotError(
            "selected replay chain disagrees with the exact chain accepted by the outcome"
        )
    source_files, source_data, dependencies, construction = _capture_source_files(
        root,
        expected_scripts,
    )

    layers = selected_layer_chain(
        shot,
        selected_authority=selected_authority,
        expected_bundle_digest=outcome.bundle_digest,
    )
    if tuple(str(layer.id) for layer in layers) != tuple(
        str(row["layer_id"]) for row in authority.chain
    ):
        raise FinalRenderSnapshotError("selected layer order changed during replay snapshot capture")
    unit_states = _unit_state_projection(shot, layers, expected_scripts)

    ledger_binding, ledger_data = _binding(
        root,
        root / "shot.json",
        required=True,
        where="final-render acceptance ledger",
    )
    assert ledger_data is not None
    evidence_files = _capture_evidence_files(root, ledger_data)
    selected_artifacts = tuple(
        _binding(
            root,
            path,
            required=True,
            where=f"selected artifact {name}",
        )[0]
        for name, path in sorted(selected_authority.artifact_paths.items())
    )

    snapshot_root = Path(tempfile.mkdtemp(prefix="final-render-chain-", dir=scratch))
    asset_tree_digest, asset_sources, asset_snapshots = _snapshot_tree(
        root,
        snapshot_root,
        "assets",
        "shot asset tree",
    )
    authored_inputs_digest, authored_snapshots = _snapshot_authored_inputs(
        root,
        snapshot_root,
    )
    replay, replay_dependencies = _snapshot_replay_files(
        root,
        snapshot_root,
        replay_relatives,
        source_data,
        construction,
    )
    try:
        replay_root_binding = bind_trusted_directory(
            root,
            snapshot_root,
            "final-render replay root",
        )
    except TrustedFileError as exc:
        raise FinalRenderSnapshotError(str(exc)) from exc
    provisional = FinalRenderSnapshot(
        selected_authority=selected_authority,
        outcome_digest=outcome.digest,
        authority_digest=authority.digest,
        chain_digest=authority.chain_digest,
        authored_inputs_digest=authored_inputs_digest,
        decision_inputs_digest=_decision_inputs_digest(root),
        asset_tree_digest=asset_tree_digest,
        layers=layers,
        ledger=ledger_binding,
        selected_artifacts=selected_artifacts,
        evidence_files=(*evidence_files, *dependencies, *asset_sources),
        source_files=source_files,
        unit_state_digests=unit_states,
        replay=replay,
        replay_dependencies=(
            *replay_dependencies,
            *asset_snapshots,
            *authored_snapshots,
        ),
        replay_root=snapshot_root,
        replay_root_binding=replay_root_binding,
        assets_dir=snapshot_root / "assets",
        manifest=snapshot_root / "manifest.json",
    )
    payload = json.dumps(
        _manifest_payload(provisional, root),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ).encode("utf-8") + b"\n"
    _write_snapshot_file(root, provisional.manifest, payload)
    require_snapshot_inputs_current(shot, provisional)
    return provisional


def require_snapshot_inputs_current(shot: Shot, snapshot: FinalRenderSnapshot) -> None:
    """Revalidate every mutable byte/state projection bound by the replay snapshot."""

    try:
        require_trusted_directory_unchanged(
            snapshot.replay_root_binding,
            "final-render replay root",
        )
    except TrustedFileError as exc:
        raise FinalRenderSnapshotError(str(exc)) from exc
    current_authored_inputs_digest = _authored_inputs_digest(
        authored_input_bytes(shot.folder.resolve())
    )
    if current_authored_inputs_digest != snapshot.authored_inputs_digest:
        raise FinalRenderSnapshotError("authored plan inputs changed during final render")
    if _decision_inputs_digest(shot.folder.resolve()) != snapshot.decision_inputs_digest:
        raise FinalRenderSnapshotError("planning decision inputs changed during final render")
    current_asset_tree_digest, _asset_bindings, _exists, _directories = _tree_identity(
        shot.folder.resolve(),
        "assets",
        "shot asset tree",
    )
    if current_asset_tree_digest != snapshot.asset_tree_digest:
        raise FinalRenderSnapshotError("shot asset tree changed during final render")
    _assert_binding_current(snapshot.ledger, "final-render acceptance ledger")
    for index, binding in enumerate(snapshot.selected_artifacts):
        _assert_binding_current(binding, f"selected artifact {index}")
    for index, binding in enumerate(snapshot.evidence_files):
        _assert_binding_current(binding, f"acceptance evidence dependency {index}")
    for index, binding in enumerate(snapshot.source_files):
        _assert_binding_current(binding, f"accepted source script {index}")
    for index, binding in enumerate(snapshot.replay):
        _assert_binding_current(binding.snapshot, f"immutable replay script {index}")
    for index, binding in enumerate(snapshot.replay_dependencies):
        _assert_binding_current(binding, f"immutable replay dependency {index}")
    expected_scripts: dict[str, str] = {}
    root = shot.folder.resolve()
    for binding in snapshot.source_files:
        if binding.sha256 is None:
            raise FinalRenderSnapshotError("accepted source script binding is absent")
        expected_scripts[binding.path.relative_to(root).as_posix()] = binding.sha256
    observed_states = _unit_state_projection(
        shot,
        snapshot.layers,
        expected_scripts,
    )
    if observed_states != snapshot.unit_state_digests:
        raise FinalRenderSnapshotError(
            "durable work-unit checkpoint state changed during final render"
        )
    try:
        require_trusted_directory_unchanged(
            snapshot.replay_root_binding,
            "final-render replay root",
        )
    except TrustedFileError as exc:
        raise FinalRenderSnapshotError(str(exc)) from exc


@contextmanager
def final_render_state_locks(
    shot: Shot,
    snapshot: FinalRenderSnapshot,
) -> Iterator[None]:
    """Hold all mutable accepted-build locks through final validation and rename."""

    with ExitStack() as stack:
        layer_ids = sorted(
            (layer_id for layer_id, _digest_value in snapshot.unit_state_digests),
            key=lambda layer_id: str(unit_state_path(shot.folder, layer_id).absolute()),
        )
        for layer_id in layer_ids:
            stack.enter_context(unit_state_lock(shot.folder, layer_id, exclusive=False))
        stack.enter_context(ledger_lock(shot.folder / "shot.json", exclusive=False))
        yield
