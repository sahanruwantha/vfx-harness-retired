"""Asset prep — freeze a committed, normalized `model.glb` the build stage imports.

`normalize_glb` is the deterministic host driver (runs `_normalize_bpy.py` in a
throwaway headless Blender, isolated from the build session). `prepare_asset`
orchestrates get-raw → normalize → cache, writing `assets/<name>/` in the shot.

Cache layout (per asset):
    assets/<name>/
        concept.png   generated/source image (optional)
        raw.glb       image→3D output (non-deterministic, cached)
        model.glb     normalized, deterministic — the build input
        meta.json     bbox, tris, backend, spec hash, …
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from ..brief import load_shot
from ..log import log
from .adapter import get_backend
from .images import get_image_backend

_NB = Path(__file__).with_name("_normalize_bpy.py")


def normalize_glb(raw: Path, out: Path, *, target_height: float = 100.0, up: str = "Z",
                  blender: str = "blender", meta_path: Path | None = None) -> dict:
    raw, out = Path(raw), Path(out)
    if not raw.is_file():
        raise FileNotFoundError(f"raw GLB not found: {raw}")
    out.parent.mkdir(parents=True, exist_ok=True)
    meta_path = Path(meta_path) if meta_path else out.with_name("meta.json")

    argv = [blender, "--background", "--factory-startup", "--python", str(_NB), "--",
            "--in", str(raw), "--out", str(out), "--meta", str(meta_path),
            "--height", str(target_height), "--up", up]
    proc = subprocess.run(argv, capture_output=True, text=True)
    if proc.returncode != 0 or not out.is_file():
        raise RuntimeError(f"normalize failed (rc={proc.returncode}):\n{proc.stderr[-2000:]}")
    return json.loads(meta_path.read_text())


def _spec_hash(name: str, backend: str, target_height: float,
               sources: list[Path], isolate: bool) -> str:
    h = hashlib.sha256()
    h.update(f"{name}|{backend}|{target_height}|isolate={isolate}".encode())
    for s in sources:
        s = Path(s)
        if s.is_file():
            h.update(s.read_bytes())
    return h.hexdigest()[:12]


def _isolate_views(references: list[Path], adir: Path, mode: str,
                   subject: str, prompt: str | None) -> list[Path]:
    """Isolate each reference view into a clean subject image.

    mode='regen'  → image→image: regenerate a sharp white-bg product shot (best for 3D)
    mode='cutout' → background removal only (keeps low-res pixels; weak on dark crops)
    """
    iso_dir = adir / "isolated"
    iso_dir.mkdir(parents=True, exist_ok=True)
    img = get_image_backend()
    out: list[Path] = []
    for i, ref in enumerate(references):
        dst = iso_dir / f"view_{i}.png"
        if mode == "regen":
            img.isolate_regen(ref, dst, subject=subject, prompt=prompt)
        else:
            log(f"isolate view {i + 1}/{len(references)} (cutout): {Path(ref).name}", 2)
            img.remove_background(ref, dst, subject=subject)
        out.append(dst)
    return out


def _gen_with_retry(backend, images: list[Path], raw_dst: Path, retries: int) -> dict:
    last = None
    for attempt in range(1, retries + 2):  # retries=2 → up to 3 tries (gen fails transiently)
        try:
            if attempt > 1:
                log(f"retry {attempt}/{retries + 1} (previous attempt failed)", 2)
            return backend.to_glb(images, raw_dst)
        except Exception as e:  # transient server-side failure — retry
            last = e
            log(f"image→3D attempt {attempt} failed: {str(e)[:120]}", 2)
    raise RuntimeError(f"image→3D failed after {retries + 1} tries: {last}")


def prepare_asset(shot, name: str, *, references: list[str | Path] | None = None,
                  raw: str | Path | None = None, isolate: bool | str = "regen",
                  isolate_prompt: str | None = None, subject: str | None = None,
                  backend: str = "meshy", target_height: float = 100.0,
                  face_limit: int | None = 250000, retries: int = 2,
                  blender: str = "blender", preview: bool = True,
                  force: bool = False) -> dict:
    """Produce assets/<name>/model.glb (+meta, +preview) from the shot's own refs.

    `references` = one or more views from refs/ → ISOLATE the subject → image→3D →
    normalize. Isolation modes: 'regen' (default; image→image regenerates a clean
    white-bg product shot — best for 3D from dark/low-res crops), 'cutout' (plain
    background removal), or False/'none'. `raw=<glb>` skips generation. `model.glb`
    is the deterministic build input; regenerating it is an explicit act.
    """
    mode = ("none" if isolate in (False, "none", None)
            else "regen" if isolate in (True, "regen") else str(isolate))
    subject = subject or f"the {name.replace('_', ' ')}"
    adir = shot.folder / "assets" / name
    model, meta_path = adir / "model.glb", adir / "meta.json"
    # resolve reference views against the shot folder (accept absolute paths too)
    refs = [Path(r) if Path(r).is_absolute() else shot.folder / r for r in (references or [])]
    sources = refs if refs else ([Path(raw)] if raw else [])
    spec = _spec_hash(name, "manual" if raw else f"{backend}:{mode}:{face_limit}:{isolate_prompt}",
                      target_height, sources, mode)

    log(f"prepare_asset '{name}': {len(refs)} ref(s), isolate={mode}, "
        f"backend={backend}, face_limit={face_limit}", 1)

    if model.is_file() and meta_path.is_file() and not force:
        meta = json.loads(meta_path.read_text())
        if meta.get("spec_hash") == spec:
            log(f"cache hit → {meta.get('model')} (tris={meta.get('tris')})", 2)
            return meta  # cache hit

    adir.mkdir(parents=True, exist_ok=True)
    raw_dst = adir / "raw.glb"

    if raw:
        shutil.copyfile(raw, raw_dst)
        backend_used, views = "manual", []
    else:
        if not refs:
            raise ValueError("prepare_asset needs references=[...] or raw=<glb>")
        views = _isolate_views(refs, adir, mode, subject, isolate_prompt) if mode != "none" else refs
        be = get_backend(backend)
        poly_param = getattr(be, "poly_param", None)  # each engine names its cap differently
        if face_limit is not None and poly_param:
            be.params[poly_param] = str(face_limit)
        _gen_with_retry(be, views, raw_dst, retries)
        backend_used = backend

    log("normalize: GLB → model.glb (base@origin, scaled)", 2)
    meta = normalize_glb(raw_dst, model, target_height=target_height, blender=blender,
                         meta_path=meta_path)
    meta.update(name=name, backend=backend_used, spec_hash=spec,
                references=[str(r) for r in refs],
                views=[str(v.relative_to(shot.folder)) for v in views] if mode != "none" and not raw else [],
                model=str(model.relative_to(shot.folder)),
                prepared_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    log(f"normalized: tris={meta.get('tris')} bbox={meta.get('bbox_dims')} "
        f"base_z={meta.get('base_z')}", 2)
    if preview:
        log("preview: 3/4 solid render", 2)
        try:
            meta["preview"] = _preview_render(model, adir / "preview.png",
                                              target_height, blender)
            meta["preview"] = str(Path(meta["preview"]).relative_to(shot.folder))
        except Exception as e:  # preview is a nicety, not a layer
            meta["preview_error"] = str(e)[:200]
    meta_path.write_text(json.dumps(meta, indent=2) + "\n")
    return meta


def _preview_render(model: Path, out: Path, height: float, blender: str) -> str:
    """Quick 3/4 Workbench render of the normalized mesh so the agent can eyeball it."""
    from ..blender.session import BlenderSession
    s = BlenderSession(blender=blender, blend_file=None).start()
    try:
        s.run("import bpy\nbpy.ops.wm.read_factory_settings(use_empty=True)\n")
        s.run(f"import bpy\nbpy.ops.import_scene.gltf(filepath={str(model)!r})\n")
        s.run(
            "import bpy, math, mathutils\n"
            "sc=bpy.context.scene\n"
            "cam_d=bpy.data.cameras.new('Cam'); cam=bpy.data.objects.new('Cam',cam_d)\n"
            "sc.collection.objects.link(cam); sc.camera=cam; cam_d.lens=45\n"
            f"h={height}\n"
            "cam.location=(h*1.6,-h*1.8,h*0.75)\n"
            "d=mathutils.Vector((0,0,h*0.45))-cam.location\n"
            "cam.rotation_euler=d.to_track_quat('-Z','Y').to_euler()\n"
            "sun_d=bpy.data.lights.new('Sun','SUN'); sun_d.energy=3.0\n"
            "sun=bpy.data.objects.new('Sun',sun_d); sc.collection.objects.link(sun)\n"
            "sun.rotation_euler=(math.radians(55),0,math.radians(35))\n"
        )
        src = s.render(frame=1, mode="solid", scale=0.5)
        shutil.copyfile(src, out)
        return str(out)
    finally:
        s.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Prepare a normalized shot asset from refs.")
    ap.add_argument("folder", help="shot folder (contains brief.md)")
    ap.add_argument("name", help="asset name, e.g. tower")
    ap.add_argument("--reference", "-r", action="append", dest="references",
                    help="reference view from refs/ (use one clean front view)")
    ap.add_argument("--raw", help="pre-made GLB to normalize (skips generation)")
    ap.add_argument("--isolate", default="regen", choices=["regen", "cutout", "none"],
                    help="isolation mode (default regen: clean white-bg redraw)")
    ap.add_argument("--subject", help="short description of the asset (for regen)")
    ap.add_argument("--prompt", help="full isolate prompt (overrides --subject default)")
    ap.add_argument("--backend", default="meshy", choices=["meshy"])
    ap.add_argument("--height", type=float, default=100.0, help="target height (units)")
    ap.add_argument("--face-limit", type=int, default=250000, help="cap tris (decimate)")
    ap.add_argument("--blender", default="blender")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    shot = load_shot(args.folder)
    meta = prepare_asset(shot, args.name, references=args.references, raw=args.raw,
                         isolate=args.isolate, subject=args.subject,
                         isolate_prompt=args.prompt, backend=args.backend,
                         target_height=args.height, face_limit=args.face_limit,
                         blender=args.blender, force=args.force)
    print(json.dumps(meta, indent=2))


if __name__ == "__main__":
    main()
