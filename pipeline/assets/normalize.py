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
        # Does the MESH still look like the plate it was reconstructed FROM? Nothing
        # checked, and the cost of not checking is paid three layers later: a critic
        # demanding "setbacks and a stepped podium" at every frame of every attempt, with
        # no way to tell whether the mesh lacks them or the render is merely hiding them.
        # Answering that took me a manual look at two PNGs; it should be a field.
        try:
            plate = next((shot.folder / v for v in meta.get("views") or []), None)
            prev = shot.folder / meta["preview"] if meta.get("preview") else None
            if plate and prev and plate.is_file() and prev.is_file():
                meta["fidelity"] = compare_to_plate(plate, prev)
                f = meta["fidelity"]
                log(f"fidelity vs plate: flare {f['mesh_flare']}x vs {f['plate_flare']}x "
                    f"→ {f['verdict']}", 2)
                if f["verdict"] != "consistent":
                    log(f"! {f['note']}", 2)
        except Exception as e:
            meta["fidelity_error"] = str(e)[:200]
            log(f"! fidelity check unavailable: {str(e)[:80]}", 2)
        # And the asset's LOOK, which the silhouette check above cannot see. Rendered
        # front-on WITH materials so it is comparable to the plate; the 3/4 clay preview
        # is a shape check and shows no facade at all.
        log("facade: front-on textured render", 2)
        try:
            meta["facade_preview"] = str(
                Path(_facade_render(model, adir / "facade.png", target_height, blender))
                .relative_to(shot.folder))
            plate = next((shot.folder / v for v in meta.get("views") or []), None)
            fpng = shot.folder / meta["facade_preview"]
            if plate and plate.is_file() and fpng.is_file():
                meta["facade"] = facade_vs_plate(plate, fpng)
                fa = meta["facade"]
                log(f"facade vs plate: outer/core {fa.get('mesh_outer_core')} vs "
                    f"{fa.get('plate_outer_core')} · L1 {fa.get('profile_l1')} "
                    f"→ {fa['verdict']}", 2)
                if fa["verdict"] not in ("consistent",):
                    log(f"! {fa['note']}", 2)
        except Exception as e:
            meta["facade_error"] = str(e)[:200]
            log(f"! facade check unavailable: {str(e)[:80]}", 2)
    meta_path.write_text(json.dumps(meta, indent=2) + "\n")
    return meta


def _silhouette_profile(path: Path, rows: int = 24) -> list[float]:
    """Fraction of SUBJECT pixels per horizontal band, subject = anything materially
    darker or lighter than the corner background. Works across a dark shaded design plate
    and a grey clay preview, because it measures SHAPE, not colour."""
    from PIL import Image
    im = Image.open(path).convert("L")
    w, h = im.size
    px = im.load()
    bg = sum((px[1, 1], px[w - 2, 1], px[1, h - 2], px[w - 2, h - 2])) / 4
    out = []
    for r in range(rows):
        y0, y1 = int(r * h / rows), int((r + 1) * h / rows)
        ys, xs = range(y0, y1, 2), range(0, w, 2)
        sub = sum(1 for y in ys for x in xs if abs(px[x, y] - bg) > 28)
        out.append(sub / max(len(list(ys)) * len(list(xs)), 1))
    return out


def compare_to_plate(plate: Path, preview: Path) -> dict:
    """Compare the normalized mesh's silhouette against its source plate.

    Ratios only, never absolute fill: the preview is framed by our own camera and the
    plate by whatever produced it, so absolute widths are not comparable — but the
    PROPORTION of base flare to shaft width is, and that is exactly the structure a
    reconstruction tends to lose (a stepped podium collapsing into a plain extrusion).
    """
    a, b = _silhouette_profile(plate), _silhouette_profile(preview)

    def flare(p: list[float]) -> float:
        body = sorted(p[len(p) // 4: len(p) * 2 // 3])
        shaft = body[len(body) // 2] if body else 0.0        # median of the middle band
        base = max(p[len(p) * 2 // 3:] or [0.0])             # widest row low down
        return round(base / shaft, 2) if shaft > 0.01 else 0.0

    pf, mf = flare(a), flare(b)
    rel = abs(mf - pf) / pf if pf > 0.01 else 0.0
    if pf <= 1.05:
        verdict, note = "no-base-in-plate", "the plate shows no base flare to compare"
    elif rel <= 0.25:
        verdict, note = "consistent", ""
    else:
        lost = mf < pf
        verdict = "mesh-lost-structure" if lost else "mesh-added-structure"
        note = (f"the mesh's base flare is {mf}x its shaft where the plate shows {pf}x — "
                f"the reconstruction {'DROPPED' if lost else 'invented'} base massing the "
                f"design has. Anything scored on silhouette will be judged against a shape "
                f"the mesh cannot produce.")
    return {"plate_flare": pf, "mesh_flare": mf, "rel_diff": round(rel, 2),
            "verdict": verdict, "note": note,
            "plate_profile": [round(v, 3) for v in a],
            "mesh_profile": [round(v, 3) for v in b]}


def _facade_render(model: Path, out: Path, height: float, blender: str) -> str:
    """FRONT-ON, TEXTURED render of the normalized mesh — the asset's LOOK, not its shape.

    The 3/4 Workbench preview below is a clay render with materials switched OFF. That is
    right for a silhouette check and useless for anything else — and it was the ONLY image
    of an asset anyone ever saw. The consequence was expensive: sr2_tower ships three
    2048x2048 maps reproducing its design plate almost exactly (dense window cells in
    strips, a dark recessed core, ribbed piers, a stepped podium, a sign with glowing
    letters), none of which is visible in clay, so a build layer spent five attempts and
    $78 reconstructing a facade the asset already had.

    Front-on and evenly lit, to match how an isolation plate is framed: a facade profile
    is only comparable between matched ANGLE and matched LIGHTING.
    """
    from ..blender.session import BlenderSession
    s = BlenderSession(blender=blender, blend_file=None).start()
    try:
        s.run("import bpy\nbpy.ops.wm.read_factory_settings(use_empty=True)\n")
        s.run(f"import bpy\nbpy.ops.import_scene.gltf(filepath={str(model)!r})\n")
        s.run(
            "import bpy, math, mathutils\n"
            "sc=bpy.context.scene\n"
            "sc.render.resolution_x, sc.render.resolution_y = 720, 1080\n"
            # Bright even world, no key: we want the TEXTURE's own values the way a plate
            # shows them. A raking key would measure the lighting rather than the asset.
            "w=bpy.data.worlds.new('W'); sc.world=w; w.use_nodes=True\n"
            "nt=w.node_tree; nt.nodes.clear()\n"
            "o=nt.nodes.new('ShaderNodeOutputWorld'); bg=nt.nodes.new('ShaderNodeBackground')\n"
            "bg.inputs['Color'].default_value=(1,1,1,1); bg.inputs['Strength'].default_value=1.8\n"
            "nt.links.new(bg.outputs[0], o.inputs['Surface'])\n"
            "cam_d=bpy.data.cameras.new('Cam'); cam=bpy.data.objects.new('Cam',cam_d)\n"
            "sc.collection.objects.link(cam); sc.camera=cam; cam_d.lens=85\n"
            # Frame from the REAL world bbox. Assuming base-at-z=0 framed the top third of
            # the tower in an earlier rig and measured empty backdrop for two runs.
            "obs=[ob for ob in sc.objects if ob.type=='MESH']\n"
            "bb=[ob.matrix_world @ mathutils.Vector(c) for ob in obs for c in ob.bound_box]\n"
            "zmin=min(v.z for v in bb); zmax=max(v.z for v in bb); cz=0.5*(zmin+zmax)\n"
            "H=max(zmax-zmin, 1e-6)\n"
            "fov=2*math.atan(cam_d.sensor_width/(2*cam_d.lens))\n"
            "dist=(H*1.12)/(2*math.tan(fov/2))\n"
            "cam.location=(0.0,-dist,cz); cam.rotation_euler=(math.radians(90),0,0)\n"
        )
        src = s.render(frame=1, mode="eevee", scale=1.0)
        shutil.copyfile(src, out)
        return str(out)
    finally:
        s.close()


def facade_vs_plate(plate: Path, facade_png: Path) -> dict:
    """Does the mesh's FACADE match the plate, not just its outline?

    compare_to_plate measures the silhouette, and a silhouette statistic cannot see a
    facade: sr2_tower passed it at 1.73x base flare against the plate's 1.85x while the
    facade the pipeline actually rendered had the WRONG POLARITY (outer/core 1.91 where
    the plate reads 3.37). The gate was correct and the asset cleared it while being wrong
    in the way that mattered.

    Same rule as #37, one level up: A GATE MUST MEASURE THE PROPERTY THE ARTIFACT IS FOR.
    An asset is for its look, not its outline.
    """
    from ..facade import compare_profiles, facade_profile
    ref = facade_profile(plate)
    got = facade_profile(facade_png)
    if ref.get("warning") or got.get("warning"):
        return {"verdict": "unmeasurable",
                "note": ref.get("warning") or got.get("warning")}
    cmp = compare_profiles(got, ref)
    l1 = cmp["l1"]
    pf, mf = ref["outer_core_ratio"], got["outer_core_ratio"]

    # THE RATIO IS PRIMARY, not L1. My first version of this gate made L1 the test at a
    # 0.25 threshold and it passed the very defect it was written for: the procedural
    # facade scores outer/core 1.91 against the plate's 3.37 — plainly wrong — with an L1
    # of 0.242, just inside the bar. L1 measures overall profile distance and is sensitive
    # to lighting and exposure; the outer/core RATIO measures the one structural property
    # that keeps being built backwards. Judge that, and treat L1 as corroboration.
    #
    # Asymmetric on purpose. Falling SHORT of the plate's separation is the failure (a
    # fused or inverted facade); EXCEEDING it is not — the correct asset measures 4.48
    # against the plate's 3.37 simply because a render can separate strips from core more
    # cleanly than a photograph does.
    floor = 0.70 * pf
    if pf < 1.2:
        verdict, note = "no-structure-in-plate", ("the plate shows no outer/core "
                                                  "separation to compare")
    elif mf < floor:
        verdict = "facade-polarity-lost"
        note = (f"the plate reads outer/core {pf} — bright OUTER bands against a darker "
                f"recessed core — and the mesh renders {mf}, only {mf/pf:.0%} of it. The "
                f"facade's structure is fused or inverted. Any layer judged on 'does the "
                f"hero read' will fail on this and CANNOT fix it by shading, because the "
                f"structure itself is wrong. Check nothing downstream is overwriting the "
                f"asset's own material.")
    elif l1 > 0.30:
        verdict = "facade-differs"
        note = (f"outer/core is in range ({mf} vs {pf}) but the profile shape differs "
                f"(L1 {l1}): the bright bands may be in the wrong PLACE across the shaft, "
                f"or the cell density is off.")
    else:
        verdict, note = "consistent", ""
    return {"plate_outer_core": pf, "mesh_outer_core": mf, "profile_l1": l1,
            "ratio_gap": cmp["ratio_gap"], "verdict": verdict, "note": note,
            "plate_profile": ref["profile"], "mesh_profile": got["profile"]}


def _preview_render(model: Path, out: Path, height: float, blender: str) -> str:
    """Quick 3/4 Workbench render of the normalized mesh so the agent can eyeball it.

    SHAPE ONLY — Workbench solid shading ignores materials entirely. Use _facade_render
    for the asset's look, and see the note there on what a clay-only preview cost."""
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
