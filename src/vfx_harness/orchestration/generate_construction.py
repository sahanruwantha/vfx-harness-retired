"""Harness-owned generate-construction: plates, identity, Meshy, promotion.

The builder does not choose a generator. A ``generate`` unit is prepared before
the live session: minted ``refobs-*`` crops → Higgsfield plates → identity card
→ Meshy multi-image → hash-verified bytes under ``build/construction/``.
"""

from __future__ import annotations

import hashlib
import json
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
from vfx_harness.observability.log import log
from vfx_harness.observability.provenance import atomic_write
from vfx_harness.observability.run_artifacts import active as active_run
from vfx_harness.orchestration.refobs import load_witness_crop, missing_witness_ids
from vfx_harness.orchestration.unit_state import unit_digest as digest_of

_GLB_MAGIC = b"glTF"
_EXTRA_CAMERAS = ("three_quarter", "left", "right")
CONSTRUCTION_PIN = "construction_import.json"
FINAL_RENDER_CONSTRUCTION_POINTER_SCHEMA = (
    "vfx-harness.final-render-construction-pointer/v1"
)

IsolateFn = Callable[..., Path]
OrbitFn = Callable[..., Path]
IdentityFn = Callable[[str | Path, str | Path], tuple[str, ...]]
ToGlbFn = Callable[..., dict]


class GenerateConstructionError(RuntimeError):
    """Typed generate-construction stop. Meshy must not have been called."""


@dataclass(frozen=True)
class PromotedConstruction:
    glb_relpath: str
    sha256: str
    unit_digest: str
    view_count: int
    reused: bool = False
    snapshot_path: str | None = None


def _qualify_glb(path: Path) -> str:
    if not path.is_file() or path.stat().st_size == 0:
        raise GenerateConstructionError(
            f"generated mesh {path} is missing or empty; Meshy thumbnails are not acceptance"
        )
    header = path.read_bytes()[:4]
    if header != _GLB_MAGIC:
        raise GenerateConstructionError(
            f"generated mesh {path} is not a GLB (magic {header!r}). "
            "Preview images cannot promote."
        )
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _pointer_path(shot_folder: Path, layer_id: str, unit_id: str) -> Path:
    return shot_folder / construction_pointer_relpath(layer_id, unit_id)


def load_promoted_construction(
    shot_folder: str | Path, layer_id: str, unit_id: str, expected_digest: str
) -> PromotedConstruction | None:
    pointer = _pointer_path(Path(shot_folder), layer_id, unit_id)
    if not pointer.is_file():
        return None
    try:
        payload = json.loads(pointer.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
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
    glb = Path(shot_folder) / rel
    actual = _qualify_glb(glb)
    if actual != digest:
        raise GenerateConstructionError(
            f"promoted construction hash mismatch for {rel}: pointer {digest} file {actual}"
        )
    return PromotedConstruction(
        glb_relpath=rel,
        sha256=digest,
        unit_digest=expected_digest,
        view_count=int(payload.get("view_count") or 0),
        reused=True,
    )


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


def prepare_generate_unit(
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
    plate_dir: Path | None = None,
    candidate_out: Path | None = None,
) -> PromotedConstruction:
    """Resolve witnesses, prepare plates, generate, and promote under build/."""
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
    existing = load_promoted_construction(shot_folder, str(layer_id), str(unit.id), expected)
    if existing is not None:
        log(f"generate construction reused {existing.glb_relpath} for {unit.id}", 1)
        return existing
    missing = missing_witness_ids(shot_folder, witnesses)
    if missing:
        raise GenerateConstructionError(
            f"generate unit {unit.id} names unregistered witnesses {list(missing)}. "
            + UNREGISTERED_WITNESS_RULE
        )
    parents = [load_witness_crop(shot_folder, token) for token in witnesses]
    layout = active_run(shot_folder)
    if plate_dir is None:
        if layout is None:
            raise GenerateConstructionError(
                "generate construction needs an active run to hold candidate plates"
            )
        plate_dir = layout.scratch / "construction" / str(layer_id) / str(unit.id) / "plates"
    if candidate_out is None:
        if layout is None:
            raise GenerateConstructionError(
                "generate construction needs an active run to hold the candidate GLB"
            )
        candidate_out = (
            layout.scratch / "construction" / str(layer_id) / str(unit.id) / "candidate.glb"
        )
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
    digest = _qualify_glb(candidate_out)
    rel = promoted_glb_relpath(digest)
    if not legal_promoted_relpath(rel):
        raise GenerateConstructionError(f"illegal promotion path {rel}. " + PROMOTED_PATH_RULE)
    dest = Path(shot_folder) / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(candidate_out.read_bytes())
    pointer = {
        "schema": PROMOTED_CONSTRUCTION_SCHEMA,
        "unit_id": str(unit.id),
        "layer_id": str(layer_id),
        "unit_digest": expected,
        "sha256": digest,
        "glb": rel,
        "witnesses": list(witnesses),
        "view_count": len(plates),
        "generator": "meshy",
    }
    atomic_write(
        _pointer_path(Path(shot_folder), str(layer_id), str(unit.id)),
        json.dumps(pointer, indent=2, sort_keys=True) + "\n",
    )
    log(f"generate construction promoted {rel} ({len(plates)} views) for {unit.id}", 1)
    return PromotedConstruction(
        glb_relpath=rel,
        sha256=digest,
        unit_digest=expected,
        view_count=len(plates),
        reused=False,
    )


def pin_construction_import(session, promoted: PromotedConstruction | None) -> None:
    """Pin or clear the worker construction import for this unit."""
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
    glb = (snapshot_root / rel).resolve()
    try:
        glb.relative_to(snapshot_root)
    except ValueError as exc:
        raise GenerateConstructionError(
            "final-render construction snapshot escapes its immutable replay root"
        ) from exc
    actual = _qualify_glb(glb)
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
    if not pointer.is_file():
        pin_construction_import(session, None)
        return
    try:
        payload = json.loads(pointer.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
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
    cwd = getattr(session, "cwd", None)
    if cwd:
        shot = Path(cwd)
    elif len(pointer.parts) >= 4 and pointer.parts[-3] == "units" and pointer.parts[-4] == "build":
        shot = pointer.parents[3]
    else:
        pin_construction_import(session, None)
        return
    actual = _qualify_glb(shot / rel)
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
