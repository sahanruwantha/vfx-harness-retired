"""Harness-owned generate-construction: plates, identity, Meshy, promotion.

The builder does not choose a generator. A ``generate`` unit is prepared before
the live session: minted ``refobs-*`` crops → Higgsfield plates → identity card
→ Meshy multi-image → hash-verified bytes under ``build/construction/``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from vfx_harness.assets import plates as _plates
from vfx_harness.assets.adapter import MeshyBackend
from vfx_harness.domain.refobs import (
    PROMOTED_CONSTRUCTION_SCHEMA,
    PROMOTED_PATH_RULE,
    UNREGISTERED_WITNESS_RULE,
    construction_pointer_relpath,
    legal_promoted_relpath,
    promoted_glb_relpath,
)
from vfx_harness.infrastructure.trusted_files import (
    TrustedFileBinding,
    TrustedFileError,
    TrustedFileNotFound,
    read_trusted_file,
    require_trusted_file_unchanged,
)
from vfx_harness.observability.log import log
from vfx_harness.observability.run_artifacts import active as active_run
from vfx_harness.orchestration.authority_selection_transaction import (
    durable_replace_file_bytes,
)
from vfx_harness.orchestration.construction_replay import (
    CONSTRUCTION_PIN as CONSTRUCTION_PIN,
)
from vfx_harness.orchestration.construction_replay import (
    ConstructionReplayDependency as ConstructionReplayDependency,
)
from vfx_harness.orchestration.construction_replay import (
    ConstructionReplayError as GenerateConstructionError,
)
from vfx_harness.orchestration.construction_replay import (
    PreparedConstructionReplayInput as PreparedConstructionReplayInput,
)
from vfx_harness.orchestration.construction_replay import (
    pin_prepared_construction_replay as pin_prepared_construction_replay,
)
from vfx_harness.orchestration.construction_replay import (
    prepare_construction_replay_input as prepare_construction_replay_input,
)
from vfx_harness.orchestration.construction_replay import (
    require_prepared_construction_replay_current as require_prepared_construction_replay_current,
)
from vfx_harness.orchestration.refobs import load_witness_crop, missing_witness_ids
from vfx_harness.orchestration.unit_state import unit_digest as digest_of

_GLB_MAGIC = b"glTF"
_EXTRA_CAMERAS = ("three_quarter", "left", "right")
_SAFE_BINDING = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,191}")
_DIRECTORY_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
FINAL_RENDER_CONSTRUCTION_POINTER_SCHEMA = (
    "vfx-harness.final-render-construction-pointer/v1"
)

IsolateFn = Callable[..., Path]
OrbitFn = Callable[..., Path]
IdentityFn = Callable[[str | Path, str | Path], tuple[str, ...]]
ToGlbFn = Callable[..., dict]


def ensure_construction_read_namespace(shot_folder: str | Path) -> Path:
    """Create only the construction CAS namespace under the outer builder fence."""

    shot = Path(shot_folder).expanduser().absolute()
    shot_descriptor: int | None = None
    build_descriptor: int | None = None
    construction_descriptor: int | None = None
    try:
        shot_descriptor = os.open(shot, _DIRECTORY_OPEN_FLAGS)
        build_descriptor = os.open(
            "build",
            _DIRECTORY_OPEN_FLAGS,
            dir_fd=shot_descriptor,
        )
        created = False
        try:
            os.mkdir("construction", mode=0o755, dir_fd=build_descriptor)
            created = True
        except FileExistsError:
            pass
        construction_descriptor = os.open(
            "construction",
            _DIRECTORY_OPEN_FLAGS,
            dir_fd=build_descriptor,
        )
        if not stat.S_ISDIR(os.fstat(construction_descriptor).st_mode):
            raise GenerateConstructionError(
                "construction namespace must be a real non-symlink directory"
            )
        if created:
            os.fsync(build_descriptor)
        return shot / "build" / "construction"
    except OSError as exc:
        raise GenerateConstructionError(
            "construction namespace requires real non-symlink "
            "shot/build/construction directories"
        ) from exc
    finally:
        for descriptor in (
            construction_descriptor,
            build_descriptor,
            shot_descriptor,
        ):
            if descriptor is not None:
                os.close(descriptor)


@dataclass(frozen=True)
class PromotedConstruction:
    glb_relpath: str
    sha256: str
    unit_digest: str
    view_count: int
    reused: bool = False
    snapshot_path: str | None = None


@dataclass(frozen=True)
class StagedConstruction:
    """Claim-neutral scratch result; it carries no publication authority."""

    candidate_path: Path
    sha256: str
    unit_digest: str
    witness_digests: tuple[tuple[str, str], ...]
    view_count: int
    run_id: str
    claim_id: str


@dataclass(frozen=True)
class ConstructionBinding:
    """Fully verified construction whose guarded acceptance needs only stat calls."""

    promoted: PromotedConstruction
    pointer_path: Path
    pointer_identity: TrustedFileBinding | None
    glb_path: Path
    glb_identity: TrustedFileBinding
    witness_files: tuple[tuple[str, Path, TrustedFileBinding], ...]


@dataclass(frozen=True)
class PreparedConstructionPublication:
    """Inert CAS bytes plus the bounded pointer transaction that can bind them."""

    binding: ConstructionBinding
    pointer_bytes: bytes
    run_id: str
    claim_id: str


def _read_real_file(
    trusted_root: str | Path,
    path: Path,
) -> tuple[bytes, TrustedFileBinding]:
    try:
        observed = read_trusted_file(
            trusted_root,
            path,
            "construction input",
            require_nonempty=True,
        )
    except TrustedFileError as exc:
        raise GenerateConstructionError(
            f"construction input must be a real regular file: {path}"
        ) from exc
    return observed.payload, observed.binding


def _require_identity(path: Path, expected: TrustedFileBinding) -> None:
    if Path(path).absolute() != expected.path:
        raise GenerateConstructionError(
            f"construction binding belongs to another path: {expected.path} != {path}"
        )
    try:
        require_trusted_file_unchanged(expected, "construction binding")
    except TrustedFileError as exc:
        raise GenerateConstructionError(
            f"construction binding changed before guarded publication: {path}"
        ) from exc


def _qualify_glb(trusted_root: str | Path, path: Path) -> str:
    payload, _file_identity = _read_real_file(trusted_root, path)
    header = payload[:4]
    if header != _GLB_MAGIC:
        raise GenerateConstructionError(
            f"generated mesh {path} is not a GLB (magic {header!r}). "
            "Preview images cannot promote."
        )
    return hashlib.sha256(payload).hexdigest()


def _pointer_path(shot_folder: Path, layer_id: str, unit_id: str) -> Path:
    return shot_folder / construction_pointer_relpath(layer_id, unit_id)


def prepare_promoted_construction_reuse(
    shot_folder: str | Path, layer_id: str, unit_id: str, expected_digest: str
) -> ConstructionBinding | None:
    """Hash and validate an existing construction without holding attempt locks."""

    shot = Path(shot_folder).expanduser().absolute()
    pointer = _pointer_path(shot, layer_id, unit_id)
    try:
        pointer.lstat()
    except FileNotFoundError:
        return None
    pointer_bytes, pointer_identity = _read_real_file(shot, pointer)
    try:
        payload = json.loads(pointer_bytes)
    except json.JSONDecodeError:
        return None
    if payload.get("schema") != PROMOTED_CONSTRUCTION_SCHEMA:
        return None
    if str(payload.get("unit_digest") or "") != expected_digest:
        return None
    rel = str(payload.get("glb") or "")
    digest = str(payload.get("sha256") or "")
    if not legal_promoted_relpath(rel):
        raise GenerateConstructionError(
            f"construction pointer {pointer} names illegal path {rel!r}. "
            + PROMOTED_PATH_RULE
        )
    glb = shot / rel
    glb_bytes, glb_identity = _read_real_file(shot, glb)
    if glb_bytes[:4] != _GLB_MAGIC:
        raise GenerateConstructionError(
            f"promoted construction {rel} is not a GLB"
        )
    actual = hashlib.sha256(glb_bytes).hexdigest()
    if actual != digest:
        raise GenerateConstructionError(
            f"promoted construction hash mismatch for {rel}: pointer {digest} file {actual}"
        )
    witnesses = payload.get("witnesses")
    witness_digests = payload.get("witness_digests")
    if (
        not isinstance(witnesses, list)
        or not witnesses
        or not isinstance(witness_digests, dict)
        or set(witness_digests) != {str(value) for value in witnesses}
    ):
        raise GenerateConstructionError(
            f"construction pointer {pointer} lacks exact witness byte digests"
        )
    witness_files: list[tuple[str, Path, TrustedFileBinding]] = []
    for witness in witnesses:
        expected_witness = str(witness_digests[str(witness)])
        crop = load_witness_crop(shot_folder, str(witness))
        crop_bytes, crop_identity = _read_real_file(shot, crop)
        observed_witness = hashlib.sha256(crop_bytes).hexdigest()
        if observed_witness != expected_witness:
            raise GenerateConstructionError(
                f"construction witness {witness} changed after promotion: expected "
                f"{expected_witness}, found {observed_witness}"
            )
        witness_files.append((str(witness), crop, crop_identity))
    return ConstructionBinding(
        promoted=PromotedConstruction(
            glb_relpath=rel,
            sha256=digest,
            unit_digest=expected_digest,
            view_count=int(payload.get("view_count") or 0),
            reused=True,
        ),
        pointer_path=pointer,
        pointer_identity=pointer_identity,
        glb_path=glb,
        glb_identity=glb_identity,
        witness_files=tuple(witness_files),
    )


def commit_promoted_construction_reuse(
    binding: ConstructionBinding,
) -> PromotedConstruction:
    """Accept a verified reuse binding through only bounded identity checks."""

    if binding.pointer_identity is None:
        raise GenerateConstructionError(
            "existing construction reuse requires an exact pointer identity"
        )
    _require_identity(binding.pointer_path, binding.pointer_identity)
    _require_identity(binding.glb_path, binding.glb_identity)
    for _witness, crop, identity in binding.witness_files:
        _require_identity(crop, identity)
    return binding.promoted


def load_promoted_construction(
    shot_folder: str | Path, layer_id: str, unit_id: str, expected_digest: str
) -> PromotedConstruction | None:
    """Standalone fully validated reuse adapter.

    Builder integration uses the explicit prepare/commit pair so expensive reads stay
    outside its short attempt-publication lock.
    """

    binding = prepare_promoted_construction_reuse(
        shot_folder,
        layer_id,
        unit_id,
        expected_digest,
    )
    return None if binding is None else commit_promoted_construction_reuse(binding)


def prepare_plates(
    parents: Sequence[Path],
    plate_dir: Path,
    *,
    isolate_relight: IsolateFn = _plates.isolate_relight,
    isolate_cutout: IsolateFn = _plates.isolate_cutout,
    orbit_view: OrbitFn = _plates.orbit_view,
    identity_gaps: IdentityFn = _plates.plate_identity_gaps,
    extra_cameras: Sequence[str] = _EXTRA_CAMERAS,
) -> list[Path]:
    """Identity-gated isolate + bounded orbits. Never returns a failed plate."""
    if not parents:
        raise GenerateConstructionError(
            "generate construction has no parent crop. " + UNREGISTERED_WITNESS_RULE
        )
    plate_dir.mkdir(parents=True, exist_ok=True)
    kept: list[Path] = []
    for index, parent in enumerate(parents):
        relight = plate_dir / f"relight-{index}.png"
        isolate_relight(parent, relight)
        gaps = identity_gaps(parent, relight)
        source = relight
        if gaps:
            cutout = plate_dir / f"cutout-{index}.png"
            isolate_cutout(parent, cutout)
            cut_gaps = identity_gaps(parent, cutout)
            if cut_gaps:
                raise GenerateConstructionError(
                    "isolate identity failed for both relight and cutout: "
                    + "; ".join(gaps + cut_gaps)
                )
            source = cutout
        kept.append(source)
        for camera in extra_cameras:
            if len(kept) >= 4:
                break
            orbit = plate_dir / f"orbit-{index}-{camera}.png"
            orbit_view(source, orbit, camera=camera)
            orbit_gaps = identity_gaps(source, orbit)
            if orbit_gaps:
                log(
                    f"orbit_view {camera} failed identity and was not sent to Meshy: "
                    + "; ".join(orbit_gaps),
                    1,
                )
                continue
            kept.append(orbit)
        if len(kept) >= 4:
            break
    if not kept:
        raise GenerateConstructionError(
            "no identity-gated plates survived; Meshy was not called"
        )
    return kept[:4]


def stage_generate_unit(
    shot_folder: str | Path,
    layer_id: str,
    unit,
    *,
    isolate_relight: IsolateFn | None = None,
    isolate_cutout: IsolateFn | None = None,
    orbit_view: OrbitFn | None = None,
    identity_gaps: IdentityFn | None = None,
    to_glb: ToGlbFn | None = None,
    extra_cameras: Sequence[str] = _EXTRA_CAMERAS,
    run_id: str | None = None,
    claim_id: str,
) -> StagedConstruction:
    """Resolve witnesses, prepare plates, and generate only into run scratch.

    This operation may call external providers and deliberately holds no unit-state
    lock.  Its caller must revalidate the exact attempt before promoting the returned
    immutable candidate.
    """
    route = getattr(getattr(unit, "construction", None), "route", "procedural")
    if route == "retrieve":
        raise GenerateConstructionError(
            "construction route 'retrieve' is not wired; stage generate with minted "
            "refobs-* witnesses or procedural mesh"
        )
    if route != "generate":
        raise GenerateConstructionError(
            f"prepare_generate_unit is only for generate units; got {route!r}"
        )
    witnesses = tuple(getattr(unit.construction, "witnesses", ()) or ())
    expected = digest_of(unit)
    missing = missing_witness_ids(shot_folder, witnesses)
    if missing:
        raise GenerateConstructionError(
            f"generate unit {unit.id} names unregistered witnesses {list(missing)}. "
            + UNREGISTERED_WITNESS_RULE
        )
    shot = Path(shot_folder).expanduser().absolute()
    parents = [load_witness_crop(shot_folder, token) for token in witnesses]
    witness_digests = tuple(
        (
            str(token),
            hashlib.sha256(_read_real_file(shot, parent)[0]).hexdigest(),
        )
        for token, parent in zip(witnesses, parents, strict=True)
    )
    layout = active_run(shot_folder)
    if layout is None:
        raise GenerateConstructionError(
            "construction staging requires the exact active run scratch tree"
        )
    bound_run_id = str(run_id or layout.run_id).strip()
    bound_claim_id = str(claim_id).strip()
    if (
        not _SAFE_BINDING.fullmatch(bound_run_id)
        or not _SAFE_BINDING.fullmatch(bound_claim_id)
    ):
        raise GenerateConstructionError(
            "construction staging requires exact run_id and claim_id bindings"
        )
    if layout.run_id != bound_run_id:
        raise GenerateConstructionError(
            f"construction run binding {bound_run_id!r} does not match active run "
            f"{layout.run_id!r}"
        )
    staging_root = (
        layout.scratch
        / "construction"
        / str(layer_id)
        / str(unit.id)
        / bound_claim_id
    )
    plate_dir = staging_root / "plates"
    candidate_out = staging_root / "candidate.glb"
    plates = prepare_plates(
        parents,
        plate_dir,
        isolate_relight=isolate_relight or _plates.isolate_relight,
        isolate_cutout=isolate_cutout or _plates.isolate_cutout,
        orbit_view=orbit_view or _plates.orbit_view,
        identity_gaps=identity_gaps or _plates.plate_identity_gaps,
        extra_cameras=extra_cameras,
    )
    generate = to_glb or MeshyBackend().to_glb
    generate(plates, candidate_out)
    digest = _qualify_glb(shot, candidate_out)
    return StagedConstruction(
        candidate_path=Path(candidate_out),
        sha256=digest,
        unit_digest=expected,
        witness_digests=witness_digests,
        view_count=len(plates),
        run_id=bound_run_id,
        claim_id=bound_claim_id,
    )


def prepare_generate_unit_promotion(
    shot_folder: str | Path,
    layer_id: str,
    unit,
    staged: StagedConstruction,
    *,
    expected_run_id: str,
    expected_claim_id: str,
) -> PreparedConstructionPublication:
    """Validate large inputs and publish inert content-addressed bytes unlocked."""

    expected = digest_of(unit)
    layout = active_run(shot_folder)
    if layout is None or layout.run_id != staged.run_id:
        raise GenerateConstructionError(
            "staged construction no longer belongs to the exact active run"
        )
    expected_staging_root = (
        layout.scratch
        / "construction"
        / str(layer_id)
        / str(unit.id)
        / staged.claim_id
    ).absolute()
    candidate_path = staged.candidate_path.expanduser().absolute()
    try:
        candidate_path.relative_to(expected_staging_root)
    except ValueError as exc:
        raise GenerateConstructionError(
            "staged construction candidate escaped its exact run/claim scratch tree"
        ) from exc
    if staged.run_id != str(expected_run_id) or staged.claim_id != str(
        expected_claim_id
    ):
        raise GenerateConstructionError(
            "staged construction run/claim binding changed before promotion: "
            f"staged {staged.run_id}/{staged.claim_id}, expected "
            f"{expected_run_id}/{expected_claim_id}"
        )
    if staged.unit_digest != expected:
        raise GenerateConstructionError(
            f"staged construction belongs to unit digest {staged.unit_digest}, "
            f"current {unit.id} is {expected}"
        )
    candidate_bytes, _candidate_identity = _read_real_file(
        Path(shot_folder).expanduser().absolute(),
        candidate_path,
    )
    if candidate_bytes[:4] != _GLB_MAGIC:
        raise GenerateConstructionError(
            f"staged construction {staged.candidate_path} is not a GLB"
        )
    actual = hashlib.sha256(candidate_bytes).hexdigest()
    if actual != staged.sha256:
        raise GenerateConstructionError(
            f"staged construction changed before promotion: expected {staged.sha256}, "
            f"found {actual}"
        )
    shot = Path(shot_folder).expanduser().absolute()
    witness_files: list[tuple[str, Path, TrustedFileBinding]] = []
    for witness, expected_witness_digest in staged.witness_digests:
        crop = load_witness_crop(shot_folder, witness)
        crop_bytes, crop_identity = _read_real_file(shot, crop)
        observed_witness_digest = hashlib.sha256(crop_bytes).hexdigest()
        if observed_witness_digest != expected_witness_digest:
            raise GenerateConstructionError(
                f"construction witness {witness} changed before promotion: expected "
                f"{expected_witness_digest}, found {observed_witness_digest}"
            )
        witness_files.append((witness, crop, crop_identity))
    digest = actual
    rel = promoted_glb_relpath(digest)
    if not legal_promoted_relpath(rel):
        raise GenerateConstructionError(f"illegal promotion path {rel}. " + PROMOTED_PATH_RULE)
    durable_replace_file_bytes(
        shot_folder,
        rel,
        candidate_bytes,
    )
    glb_path = shot / rel
    published_bytes, glb_identity = _read_real_file(shot, glb_path)
    if hashlib.sha256(published_bytes).hexdigest() != digest:
        raise GenerateConstructionError(
            f"content-addressed construction publication changed at {rel}"
        )
    pointer = {
        "schema": PROMOTED_CONSTRUCTION_SCHEMA,
        "unit_id": str(unit.id),
        "layer_id": str(layer_id),
        "unit_digest": expected,
        "sha256": digest,
        "glb": rel,
        "witnesses": [witness for witness, _digest in staged.witness_digests],
        "witness_digests": dict(staged.witness_digests),
        "run_id": staged.run_id,
        "claim_id": staged.claim_id,
        "view_count": staged.view_count,
        "generator": "meshy",
    }
    pointer_path = Path(shot_folder) / construction_pointer_relpath(
        str(layer_id),
        str(unit.id),
    )
    try:
        pointer_snapshot = read_trusted_file(
            shot,
            pointer_path,
            "construction pointer",
            require_nonempty=True,
        )
    except TrustedFileNotFound:
        pointer_identity = None
    except TrustedFileError as exc:
        raise GenerateConstructionError(
            f"construction pointer must be a real regular file: {pointer_path}"
        ) from exc
    else:
        pointer_identity = pointer_snapshot.binding
    promoted = PromotedConstruction(
        glb_relpath=rel,
        sha256=digest,
        unit_digest=expected,
        view_count=staged.view_count,
        reused=False,
    )
    return PreparedConstructionPublication(
        binding=ConstructionBinding(
            promoted=promoted,
            pointer_path=pointer_path,
            pointer_identity=pointer_identity,
            glb_path=glb_path,
            glb_identity=glb_identity,
            witness_files=tuple(witness_files),
        ),
        pointer_bytes=(json.dumps(pointer, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        ),
        run_id=staged.run_id,
        claim_id=staged.claim_id,
    )


def commit_generate_unit_promotion(
    shot_folder: str | Path,
    layer_id: str,
    unit,
    prepared: PreparedConstructionPublication,
    *,
    expected_run_id: str,
    expected_claim_id: str,
) -> PromotedConstruction:
    """Bind one inert CAS object with a small guarded pointer transaction."""

    layout = active_run(shot_folder)
    binding = prepared.binding
    if layout is None or layout.run_id != prepared.run_id:
        raise GenerateConstructionError(
            "prepared construction no longer belongs to the exact active run"
        )
    if (
        prepared.run_id != str(expected_run_id)
        or prepared.claim_id != str(expected_claim_id)
    ):
        raise GenerateConstructionError(
            "prepared construction run/claim binding changed before pointer commit"
        )
    expected = digest_of(unit)
    if binding.promoted.unit_digest != expected:
        raise GenerateConstructionError(
            f"prepared construction belongs to another unit generation: "
            f"{binding.promoted.unit_digest} != {expected}"
        )
    expected_pointer = _pointer_path(Path(shot_folder), str(layer_id), str(unit.id))
    if binding.pointer_path != expected_pointer:
        raise GenerateConstructionError(
            "prepared construction pointer belongs to another layer or unit"
        )
    if binding.pointer_identity is None:
        if binding.pointer_path.exists() or binding.pointer_path.is_symlink():
            raise GenerateConstructionError(
                "construction pointer appeared before guarded publication"
            )
    else:
        _require_identity(binding.pointer_path, binding.pointer_identity)
    _require_identity(binding.glb_path, binding.glb_identity)
    for _witness, crop, identity in binding.witness_files:
        _require_identity(crop, identity)
    durable_replace_file_bytes(
        shot_folder,
        construction_pointer_relpath(str(layer_id), str(unit.id)),
        prepared.pointer_bytes,
    )
    log(
        f"generate construction promoted {binding.promoted.glb_relpath} "
        f"({binding.promoted.view_count} views) for {unit.id}",
        1,
    )
    return binding.promoted


def promote_generate_unit(
    shot_folder: str | Path,
    layer_id: str,
    unit,
    staged: StagedConstruction,
    *,
    expected_run_id: str,
    expected_claim_id: str,
) -> PromotedConstruction:
    """Standalone adapter around explicit unlocked prepare and guarded-sized commit."""

    prepared = prepare_generate_unit_promotion(
        shot_folder,
        layer_id,
        unit,
        staged,
        expected_run_id=expected_run_id,
        expected_claim_id=expected_claim_id,
    )
    return commit_generate_unit_promotion(
        shot_folder,
        layer_id,
        unit,
        prepared,
        expected_run_id=expected_run_id,
        expected_claim_id=expected_claim_id,
    )


def pin_construction_import(session, promoted: PromotedConstruction | None) -> None:
    """Pin or clear the worker construction import for this unit."""
    setter = getattr(session, "pin_construction_replay", None)
    if setter is not None:
        setter(None)
    artifacts = getattr(session, "artifacts", None)
    if not artifacts:
        return
    pin = Path(artifacts) / CONSTRUCTION_PIN
    if promoted is None:
        pin.unlink(missing_ok=True)
        return
    payload = {"glb": promoted.glb_relpath, "sha256": promoted.sha256}
    if promoted.snapshot_path is not None:
        payload["snapshot_glb"] = promoted.snapshot_path
    pin.write_text(
        json.dumps(payload, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )


def _final_render_snapshot_root(shot: Path, pointer: Path) -> Path:
    layout = active_run(shot)
    if layout is None:
        raise GenerateConstructionError(
            "final-render construction replay requires the active producing run"
        )
    scratch = layout.scratch.resolve()
    current = pointer.resolve().parent
    while current.parent != scratch:
        if current == scratch or scratch not in current.parents:
            raise GenerateConstructionError(
                "final-render construction pointer is outside active run scratch"
            )
        current = current.parent
    if not current.name.startswith("final-render-chain-"):
        raise GenerateConstructionError(
            "final-render construction pointer is outside an immutable replay snapshot"
        )
    return current


def _pin_final_render_snapshot(session, pointer: Path, payload: dict) -> None:
    cwd = getattr(session, "cwd", None)
    if not cwd:
        raise GenerateConstructionError(
            "final-render construction replay requires the shot working directory"
        )
    shot = Path(cwd).resolve()
    snapshot_root = _final_render_snapshot_root(shot, pointer)
    rel = str(payload.get("glb") or "")
    digest = str(payload.get("sha256") or "")
    if not legal_promoted_relpath(rel):
        raise GenerateConstructionError(
            f"final-render construction pointer {pointer} names illegal path {rel!r}. "
            + PROMOTED_PATH_RULE
        )
    glb = (snapshot_root / rel).absolute()
    try:
        glb.relative_to(snapshot_root)
    except ValueError as exc:
        raise GenerateConstructionError(
            "final-render construction snapshot escapes its immutable replay root"
        ) from exc
    actual = _qualify_glb(snapshot_root, glb)
    if actual != digest:
        raise GenerateConstructionError(
            f"final-render construction snapshot hash mismatch: pointer {digest} file {actual}"
        )
    pin_construction_import(
        session,
        PromotedConstruction(
            glb_relpath=rel,
            sha256=digest,
            unit_digest=str(payload.get("unit_digest") or ""),
            view_count=int(payload.get("view_count") or 0),
            reused=True,
            snapshot_path=str(glb),
        ),
    )


def pin_for_script(session, script_path: str | Path) -> None:
    """Pin generate-construction bytes for an identity-derived unit script, or clear."""
    pointer = Path(script_path).with_suffix(".construction.json")
    cwd = getattr(session, "cwd", None)
    if cwd:
        shot = Path(cwd).expanduser().absolute()
    elif len(pointer.parts) >= 4 and pointer.parts[-3] == "units" and pointer.parts[-4] == "build":
        shot = pointer.parents[3].expanduser().absolute()
    else:
        pin_construction_import(session, None)
        return
    pointer = pointer.expanduser().absolute() if pointer.is_absolute() else shot / pointer
    if not pointer.exists() and not pointer.is_symlink():
        pin_construction_import(session, None)
        return
    pointer_bytes, _pointer_binding = _read_real_file(shot, pointer)
    try:
        payload = json.loads(pointer_bytes)
    except json.JSONDecodeError:
        pin_construction_import(session, None)
        return
    if payload.get("schema") == FINAL_RENDER_CONSTRUCTION_POINTER_SCHEMA:
        _pin_final_render_snapshot(session, pointer, payload)
        return
    if payload.get("schema") != PROMOTED_CONSTRUCTION_SCHEMA:
        pin_construction_import(session, None)
        return
    rel = str(payload.get("glb") or "")
    digest = str(payload.get("sha256") or "")
    if not legal_promoted_relpath(rel):
        raise GenerateConstructionError(
            f"construction pointer {pointer} names illegal path {rel!r}. "
            + PROMOTED_PATH_RULE
        )
    actual = _qualify_glb(shot, shot / rel)
    if actual != digest:
        raise GenerateConstructionError(
            f"promoted construction hash mismatch for {rel}: pointer {digest} file {actual}"
        )
    pin_construction_import(
        session,
        PromotedConstruction(
            glb_relpath=rel,
            sha256=digest,
            unit_digest=str(payload.get("unit_digest") or ""),
            view_count=int(payload.get("view_count") or 0),
            reused=True,
        ),
    )
