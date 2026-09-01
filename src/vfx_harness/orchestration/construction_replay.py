"""Descriptor-bound construction inputs for deterministic artifact replay."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from vfx_harness.domain.refobs import (
    PROMOTED_CONSTRUCTION_SCHEMA,
    PROMOTED_PATH_RULE,
    legal_promoted_relpath,
)
from vfx_harness.infrastructure.trusted_files import (
    TrustedFileBinding,
    TrustedFileError,
    TrustedFileNotFound,
    read_trusted_file,
    require_trusted_file_unchanged,
)

CONSTRUCTION_PIN = "construction_import.json"
_GLB_MAGIC = b"glTF"


class ConstructionReplayError(RuntimeError):
    """Construction replay authority is absent, malformed, or stale."""


@dataclass(frozen=True, slots=True)
class ConstructionReplayDependency:
    """One descriptor-read construction input consumed during artifact replay."""

    kind: str
    path: str
    sha256: str
    binding: TrustedFileBinding

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "path": self.path, "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class PreparedConstructionReplayInput:
    """Exact pointer and GLB bytes selected before an artifact reaches Blender."""

    glb_relpath: str
    glb_sha256: str
    unit_digest: str
    view_count: int
    dependencies: tuple[ConstructionReplayDependency, ...]
    glb_payload: bytes


def prepare_construction_replay_input(
    shot_folder: str | Path,
    script_path: str | Path,
) -> PreparedConstructionReplayInput | None:
    """Capture a script's optional construction pointer and promoted GLB exactly."""

    shot = Path(shot_folder).expanduser().absolute()
    supplied = Path(script_path).expanduser()
    script = supplied.absolute() if supplied.is_absolute() else shot / supplied
    try:
        script.relative_to(shot)
    except ValueError as exc:
        raise ConstructionReplayError(
            f"construction replay script escapes shot root {shot}: {script}"
        ) from exc
    pointer = script.with_suffix(".construction.json")
    try:
        pointer_snapshot = read_trusted_file(
            shot,
            pointer,
            "construction replay pointer",
            require_nonempty=True,
        )
    except TrustedFileNotFound:
        return None
    except TrustedFileError as exc:
        raise ConstructionReplayError(str(exc)) from exc
    try:
        payload = json.loads(pointer_snapshot.payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConstructionReplayError(
            f"construction replay pointer is invalid JSON: {pointer}"
        ) from exc
    if (
        not isinstance(payload, Mapping)
        or payload.get("schema") != PROMOTED_CONSTRUCTION_SCHEMA
    ):
        raise ConstructionReplayError(
            f"construction replay pointer must use {PROMOTED_CONSTRUCTION_SCHEMA}: "
            f"{pointer}"
        )
    rel = str(payload.get("glb") or "")
    claimed_digest = str(payload.get("sha256") or "")
    if not legal_promoted_relpath(rel):
        raise ConstructionReplayError(
            f"construction pointer {pointer} names illegal path {rel!r}. "
            + PROMOTED_PATH_RULE
        )
    try:
        glb_snapshot = read_trusted_file(
            shot,
            shot / rel,
            "construction replay GLB",
            require_nonempty=True,
        )
    except TrustedFileError as exc:
        raise ConstructionReplayError(str(exc)) from exc
    if glb_snapshot.payload[:4] != _GLB_MAGIC:
        raise ConstructionReplayError(f"promoted construction {rel} is not a GLB")
    if glb_snapshot.sha256 != claimed_digest:
        raise ConstructionReplayError(
            f"promoted construction hash mismatch for {rel}: pointer "
            f"{claimed_digest} file {glb_snapshot.sha256}"
        )
    try:
        view_count = int(payload.get("view_count") or 0)
    except (TypeError, ValueError) as exc:
        raise ConstructionReplayError(
            f"construction replay pointer has invalid view_count: {pointer}"
        ) from exc
    prepared = PreparedConstructionReplayInput(
        glb_relpath=rel,
        glb_sha256=glb_snapshot.sha256,
        unit_digest=str(payload.get("unit_digest") or ""),
        view_count=view_count,
        dependencies=(
            ConstructionReplayDependency(
                kind="construction_pointer",
                path=pointer.relative_to(shot).as_posix(),
                sha256=pointer_snapshot.sha256,
                binding=pointer_snapshot.binding,
            ),
            ConstructionReplayDependency(
                kind="construction_glb",
                path=rel,
                sha256=glb_snapshot.sha256,
                binding=glb_snapshot.binding,
            ),
        ),
        glb_payload=glb_snapshot.payload,
    )
    require_prepared_construction_replay_current(prepared)
    return prepared


def require_prepared_construction_replay_current(
    prepared: PreparedConstructionReplayInput,
) -> None:
    """Reject any pointer, ancestor, or GLB rebinding after preparation."""

    if not isinstance(prepared, PreparedConstructionReplayInput):
        raise ConstructionReplayError(
            "construction replay requires a typed prepared input"
        )
    try:
        for dependency in prepared.dependencies:
            require_trusted_file_unchanged(
                dependency.binding,
                f"{dependency.kind} replay input",
            )
    except TrustedFileError as exc:
        raise ConstructionReplayError(str(exc)) from exc


def _clear_replay_pin(session) -> None:
    setter = getattr(session, "pin_construction_replay", None)
    if setter is not None:
        setter(None)
    artifacts = getattr(session, "artifacts", None)
    if artifacts:
        (Path(artifacts) / CONSTRUCTION_PIN).unlink(missing_ok=True)


def pin_prepared_construction_replay(
    session,
    prepared: PreparedConstructionReplayInput | None,
) -> None:
    """Load captured GLB bytes into worker memory; absence deterministically clears."""

    if prepared is None:
        _clear_replay_pin(session)
        return
    require_prepared_construction_replay_current(prepared)
    setter = getattr(session, "pin_construction_replay", None)
    artifacts = getattr(session, "artifacts", None)
    if setter is None or artifacts is None:
        raise ConstructionReplayError(
            "prepared construction replay requires a typed Blender session"
        )
    replay_dir = Path(artifacts) / "construction-replay"
    replay_dir.mkdir(parents=True, exist_ok=True)
    snapshot = replay_dir / f"{prepared.glb_sha256}.glb"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(snapshot, flags, 0o600)
    except FileExistsError:
        try:
            observed = read_trusted_file(
                replay_dir,
                snapshot,
                "prepared construction replay snapshot",
                require_nonempty=True,
            )
        except TrustedFileError as exc:
            raise ConstructionReplayError(str(exc)) from exc
        if observed.sha256 != prepared.glb_sha256:
            raise ConstructionReplayError(
                f"construction replay snapshot conflicts with captured bytes: {snapshot}"
            ) from None
    else:
        try:
            remaining = memoryview(prepared.glb_payload)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise ConstructionReplayError(
                        f"construction replay snapshot short write: {snapshot}"
                    )
                remaining = remaining[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        directory_descriptor = os.open(
            replay_dir,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    setter(
        {
            "glb": prepared.glb_relpath,
            "sha256": prepared.glb_sha256,
            "snapshot": str(snapshot.absolute()),
        }
    )
    require_prepared_construction_replay_current(prepared)
