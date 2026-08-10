"""The command server that runs *inside* Blender: ``blender --background --python server.py``.

This is the only code with ``bpy`` in scope. It binds a localhost TCP socket, writes the chosen
port to a ready-file (so the client knows it is up without parsing stdout), and serves framed
requests (:mod:`scene.protocol`) until a ``shutdown`` command. Every command is a small,
synchronous handler over ``bpy`` — ``run_python`` is the general power tool; the rest are
convenience verbs with legible results.

Never imported by the project venv (it would need ``bpy``); it is launched by
:class:`scene.bridge.BlenderBridge`. It inserts the ``src/`` dir on ``sys.path`` so it can reuse
the shared protocol framing rather than redefining it.
"""

from __future__ import annotations

import base64
import contextlib
import io
import os
import socket
import sys
import traceback
from pathlib import Path

import bpy  # noqa: E402 — only available inside Blender
from mathutils import Vector  # noqa: E402

# Import the shared framing as a BARE SIBLING module — never through the ``scene`` package, whose
# __init__ pulls client-only deps (claude_agent_sdk) that Blender's bundled Python does not have.
# The server's entire dependency surface is bpy + stdlib + this one self-contained module.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from protocol import recv_message, send_message  # noqa: E402


# --- command handlers ---------------------------------------------------------------


def _vec(v) -> list[float]:
    return [round(float(x), 6) for x in v]


def cmd_ping(_params: dict, _render_dir: str) -> dict:
    return {"pong": True, "blender": bpy.app.version_string}


def cmd_run_python(params: dict, _render_dir: str) -> dict:
    """Execute arbitrary ``bpy`` code. Captures stdout and an optional ``result`` variable.

    Domain-level ``ok`` (did the code raise?) lives *inside* this result; the transport envelope
    is separate. A raising snippet returns ``ok=False`` with a traceback — not a transport error.
    """
    code = params.get("code", "")
    namespace: dict = {"bpy": bpy, "Vector": Vector}
    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer):
            exec(code, namespace)  # noqa: S102 — arbitrary bpy is the point; localhost only
    except Exception:
        return {"ok": False, "stdout": buffer.getvalue(), "error": traceback.format_exc()}
    result = namespace.get("result")
    return {"ok": True, "stdout": buffer.getvalue(), "result": _jsonable(result)}


def _jsonable(value: object) -> object:
    import json

    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return repr(value)


def cmd_get_scene_graph(_params: dict, _render_dir: str) -> dict:
    """The scene as JSON: every object's transform, world bounding box, materials, and — for
    lights and cameras — their datablock. This is the symbolic perception channel."""
    scene = bpy.context.scene
    objects = []
    for obj in scene.objects:
        wm = obj.matrix_world
        corners = [wm @ Vector(c) for c in obj.bound_box]
        xs = [c.x for c in corners]
        ys = [c.y for c in corners]
        zs = [c.z for c in corners]
        entry = {
            "name": obj.name,
            "type": obj.type,
            "location": _vec(obj.location),
            "rotation_euler": _vec(obj.rotation_euler),
            "scale": _vec(obj.scale),
            "dimensions": _vec(obj.dimensions),
            "bound_min": [round(min(xs), 6), round(min(ys), 6), round(min(zs), 6)],
            "bound_max": [round(max(xs), 6), round(max(ys), 6), round(max(zs), 6)],
            "materials": [s.material.name for s in obj.material_slots if s.material],
        }
        if obj.type == "LIGHT":
            light = obj.data
            entry["light"] = {"type": light.type, "energy": light.energy, "color": _vec(light.color)}
        if obj.type == "CAMERA":
            cam = obj.data
            entry["camera"] = {
                "lens_mm": round(cam.lens, 3),
                "sensor_width": round(cam.sensor_width, 3),
                "is_active": obj is scene.camera,
            }
        objects.append(entry)
    render = scene.render
    return {
        "scene": {
            "name": scene.name,
            "engine": render.engine,
            "active_camera": scene.camera.name if scene.camera else None,
            "resolution": [render.resolution_x, render.resolution_y],
            "frame": scene.frame_current,
            "object_count": len(objects),
        },
        "objects": objects,
    }


def _enable_gpu() -> str | None:
    """Enable a Cycles GPU backend (OPTIX preferred on RTX, then CUDA/HIP/METAL/ONEAPI). Returns the
    backend chosen, or None if no GPU device is available (Cycles then stays on CPU)."""
    try:
        prefs = bpy.context.preferences.addons["cycles"].preferences
    except (KeyError, AttributeError):
        return None
    for backend in ("OPTIX", "CUDA", "HIP", "METAL", "ONEAPI"):
        try:
            prefs.compute_device_type = backend
        except (TypeError, AttributeError):
            continue
        with contextlib.suppress(Exception):
            prefs.get_devices()
        gpus = [d for d in prefs.devices if getattr(d, "type", "") == backend]
        if gpus:
            for d in prefs.devices:  # enable the GPUs of this backend (+ CPU for a hybrid pass)
                d.use = getattr(d, "type", "") in (backend, "CPU")
            return backend
    return None


def _apply_quality(scene, engine_used: str, params: dict) -> str | None:
    """Per-engine render quality: Cycles samples + denoise + GPU (path-traced/photoreal), else EEVEE
    TAA. Returns the Cycles device string ('GPU:OPTIX' / 'CPU') for the caller to report, or None."""
    samples = params.get("samples")
    if engine_used == "CYCLES":
        if samples is not None:
            with contextlib.suppress(AttributeError):
                scene.cycles.samples = int(samples)
        if params.get("denoise", True):
            with contextlib.suppress(AttributeError):
                scene.cycles.use_denoising = True
        backend = _enable_gpu() if params.get("gpu", True) else None
        with contextlib.suppress(AttributeError):
            scene.cycles.device = "GPU" if backend else "CPU"
        return f"GPU:{backend}" if backend else "CPU"
    if samples is not None:
        with contextlib.suppress(AttributeError):
            scene.eevee.taa_render_samples = int(samples)
    return None


def cmd_render_view(params: dict, render_dir: str) -> dict:
    """Render through the active (or named) camera to a PNG. This is the visual perception channel.

    Returns the path; with ``return_base64`` it also inlines the image so a vision model can look.
    ``engine`` selects the renderer ('CYCLES' for the photoreal final, EEVEE for fast previews).
    """
    scene = bpy.context.scene
    cam_name = params.get("camera")
    if cam_name:
        if cam_name not in bpy.data.objects:
            raise ValueError(f"no such camera object: {cam_name!r}")
        scene.camera = bpy.data.objects[cam_name]
    if scene.camera is None:
        raise ValueError("scene has no active camera; add one and set scene.camera before rendering")

    resolution = params.get("resolution")
    if resolution:
        scene.render.resolution_x, scene.render.resolution_y = int(resolution[0]), int(resolution[1])
    scene.render.resolution_percentage = 100

    engine = params.get("engine", "BLENDER_EEVEE_NEXT")
    with contextlib.suppress(TypeError):  # unknown engine id → keep the current one
        scene.render.engine = engine
    engine_used = scene.render.engine

    cycles_device = _apply_quality(scene, engine_used, params)

    fmt = str(params.get("format", "PNG")).upper()
    ext = ".jpg" if fmt == "JPEG" else ".png"
    name = params.get("name", "render" + ext)
    path = params.get("path") or os.path.join(render_dir, name)
    if not path.lower().endswith(ext):
        path += ext
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(AttributeError, TypeError):
        scene.render.image_settings.media_type = "IMAGE"  # reset from a prior MULTI_LAYER passes render
    scene.render.image_settings.file_format = fmt
    scene.render.use_file_extension = False
    scene.render.filepath = path
    bpy.ops.render.render(write_still=True)

    result = {
        "path": path,
        "resolution": [scene.render.resolution_x, scene.render.resolution_y],
        "engine": engine_used,
        "camera": scene.camera.name,
        "format": fmt,
        "cycles_device": cycles_device,
        "bytes": os.path.getsize(path) if os.path.exists(path) else 0,
    }
    if params.get("return_base64"):
        with open(path, "rb") as fh:
            result["image_b64"] = base64.b64encode(fh.read()).decode("ascii")
    return result


def cmd_render_passes(params: dict, render_dir: str) -> dict:
    """Render AOVs to a multilayer OpenEXR — the passes compositing works from (beauty, emission, Z,
    mist, normal, cryptomatte). Path-traced by default (Cycles). Real finishing input, not a preview:
    returns the EXR path and the passes actually enabled (some depend on the engine/build)."""
    scene = bpy.context.scene
    cam_name = params.get("camera")
    if cam_name:
        if cam_name not in bpy.data.objects:
            raise ValueError(f"no such camera object: {cam_name!r}")
        scene.camera = bpy.data.objects[cam_name]
    if scene.camera is None:
        raise ValueError("scene has no active camera; add one and set scene.camera before rendering")

    resolution = params.get("resolution")
    if resolution:
        scene.render.resolution_x, scene.render.resolution_y = int(resolution[0]), int(resolution[1])
    scene.render.resolution_percentage = 100
    engine = params.get("engine", "CYCLES")
    with contextlib.suppress(TypeError):
        scene.render.engine = engine
    engine_used = scene.render.engine
    _apply_quality(scene, engine_used, params)

    view_layer = bpy.context.view_layer
    requested = [str(p).lower() for p in (params.get("passes") or ["z", "mist", "normal", "emit", "cryptomatte"])]
    enabled = ["combined"]

    def _enable(attr: str, name: str) -> None:
        if name in requested:
            with contextlib.suppress(AttributeError):
                setattr(view_layer, attr, True)
                enabled.append(name)

    _enable("use_pass_z", "z")
    _enable("use_pass_mist", "mist")
    _enable("use_pass_normal", "normal")
    _enable("use_pass_emit", "emit")
    _enable("use_pass_diffuse_color", "diffuse")
    if "cryptomatte" in requested:
        with contextlib.suppress(AttributeError):
            view_layer.use_pass_cryptomatte_object = True
            enabled.append("cryptomatte")

    path = params.get("path") or os.path.join(render_dir, "passes.exr")
    if not path.lower().endswith(".exr"):
        path += ".exr"
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    img = scene.render.image_settings
    # 5.x: file_format is gated by image_settings.media_type — OPEN_EXR_MULTILAYER is only assignable
    # once media_type='MULTI_LAYER' (a prior JPEG/IMAGE render leaves it filtered out). Set the media
    # type first; fall back to single-layer OPEN_EXR (combined + Z only) if multilayer is unavailable.
    with contextlib.suppress(AttributeError, TypeError):
        img.media_type = "MULTI_LAYER"
    try:
        img.file_format = "OPEN_EXR_MULTILAYER"
        fmt_used = "OPEN_EXR_MULTILAYER"
    except TypeError:
        with contextlib.suppress(AttributeError, TypeError):
            img.media_type = "IMAGE"
        img.file_format = "OPEN_EXR"
        fmt_used = "OPEN_EXR"
    with contextlib.suppress(TypeError):
        img.color_depth = "32"
    scene.render.use_file_extension = False
    scene.render.filepath = path
    bpy.ops.render.render(write_still=True)
    return {
        "path": path,
        "passes": enabled,
        "format": fmt_used,
        "multilayer": fmt_used == "OPEN_EXR_MULTILAYER",
        "engine": engine_used,
        "resolution": [scene.render.resolution_x, scene.render.resolution_y],
        "bytes": os.path.getsize(path) if os.path.exists(path) else 0,
    }


def cmd_render_sequence(params: dict, render_dir: str) -> dict:
    """Render a range of frames (the timeline), one image per frame — the animation primitive.

    Sets ``scene.frame_start/end`` and steps through ``frame_set`` so keyframed animation and
    frame-driven materials play. Returns the written paths so the client can sample start/mid/end
    for the keyframe critic. Optionally inlines base64 for the frame indices in ``sample``."""
    scene = bpy.context.scene
    start = int(params.get("start", scene.frame_start))
    end = int(params.get("end", scene.frame_end))
    step = max(1, int(params.get("step", 1)))
    prefix = params.get("prefix", "seq")
    out_dir = params.get("dir") or os.path.join(render_dir, prefix)
    os.makedirs(out_dir, exist_ok=True)

    resolution = params.get("resolution")
    if resolution:
        scene.render.resolution_x, scene.render.resolution_y = int(resolution[0]), int(resolution[1])
    scene.render.resolution_percentage = 100
    engine = params.get("engine", "BLENDER_EEVEE")
    with contextlib.suppress(TypeError):
        scene.render.engine = engine
    _apply_quality(scene, scene.render.engine, params)
    scene.render.image_settings.file_format = "JPEG"
    scene.render.use_file_extension = False
    scene.frame_start, scene.frame_end = start, end

    paths = []
    for frame in range(start, end + 1, step):
        scene.frame_set(frame)
        path = os.path.join(out_dir, f"{prefix}_{frame:04d}.jpg")
        scene.render.filepath = path
        bpy.ops.render.render(write_still=True)
        paths.append(path)

    sample = params.get("sample") or []
    frames_b64 = {}
    for idx in sample:
        if 0 <= idx < len(paths) and os.path.exists(paths[idx]):
            with open(paths[idx], "rb") as fh:
                frames_b64[str(idx)] = base64.b64encode(fh.read()).decode("ascii")
    return {"paths": paths, "count": len(paths), "start": start, "end": end, "dir": out_dir, "frames_b64": frames_b64}


def cmd_save_blend(params: dict, render_dir: str) -> dict:
    """Save the current scene to a ``.blend`` — the checkpoint the refine loop reverts to."""
    path = params.get("path") or os.path.join(render_dir, "scene.blend")
    if not path.endswith(".blend"):
        path += ".blend"
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=path)
    return {"path": path, "bytes": os.path.getsize(path) if os.path.exists(path) else 0}


def cmd_open_blend(params: dict, _render_dir: str) -> dict:
    """Reopen a saved ``.blend`` — the panel restores a base scene before each atmosphere variant."""
    path = params["path"]
    if not os.path.exists(path):
        raise ValueError(f"no such .blend to open: {path!r}")
    bpy.ops.wm.open_mainfile(filepath=path)
    return {"opened": path, "objects": len(bpy.context.scene.objects)}


def cmd_image_stats(params: dict, _render_dir: str) -> dict:
    """A cheap palette/contrast signature of an image: mean RGB, luma mean/std, an 8-bin luma
    histogram. The panel scores atmosphere variants against the reference's signature — no LLM."""
    import numpy as np

    path = params["path"]
    image = bpy.data.images.load(path, check_existing=False)
    try:
        width, height = image.size
        flat = np.empty(len(image.pixels), dtype=np.float32)
        image.pixels.foreach_get(flat)
        rgb = flat.reshape(-1, 4)[:, :3]
        mean = rgb.mean(axis=0)
        luma = 0.2126 * rgb[:, 0] + 0.7152 * rgb[:, 1] + 0.0722 * rgb[:, 2]
        hist, _ = np.histogram(luma, bins=8, range=(0.0, 1.0))
        hist = hist / max(1, hist.sum())
        return {
            "mean_rgb": [round(float(mean[0]), 5), round(float(mean[1]), 5), round(float(mean[2]), 5)],
            "luma_mean": round(float(luma.mean()), 5),
            "luma_std": round(float(luma.std()), 5),
            "hist8": [round(float(x), 5) for x in hist],
            "size": [width, height],
        }
    finally:
        bpy.data.images.remove(image)


def cmd_import_glb(params: dict, _render_dir: str) -> dict:
    """Import a GLB (e.g. a Tripo mesh) and report the objects it added, so ``normalize_asset``
    can operate on exactly them rather than the whole scene."""
    path = params.get("path")
    if not path or not os.path.exists(path):
        raise ValueError(f"no such GLB to import: {path!r}")
    before = {o.name for o in bpy.data.objects}
    bpy.ops.import_scene.gltf(filepath=path)
    imported = [o for o in bpy.data.objects if o.name not in before]
    roots = [o.name for o in imported if o.parent is None or o.parent.name in before]
    mesh_count = sum(1 for o in imported if o.type == "MESH")
    return {"objects": [o.name for o in imported], "roots": roots, "mesh_count": mesh_count}


def _world_bbox(objs) -> tuple | None:
    """Combined world-space AABB over the mesh objects in *objs*, or None if there is none."""
    pts = [obj.matrix_world @ Vector(c) for obj in objs if obj.type == "MESH" for c in obj.bound_box]
    if not pts:
        return None
    lo = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
    hi = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
    return lo, hi


def cmd_normalize_asset(params: dict, _render_dir: str) -> dict:
    """Rescale + reseat imported geometry to real-world size: raw meshes arrive at arbitrary scale,
    off-centre and floating. Uniformly scale to ``target_height`` (metres, Z), centre on X/Y, and
    drop onto the ground (min Z = 0), via a controller empty so a multi-object asset moves as one."""
    names = params.get("names") or [o.name for o in bpy.context.selected_objects]
    roots = [bpy.data.objects[n] for n in names if n in bpy.data.objects]
    if not roots:
        raise ValueError("normalize_asset: no such objects to normalize")

    def descendants(obj):
        yield obj
        for child in obj.children:
            yield from descendants(child)

    seen: dict[str, object] = {}
    for root in roots:
        for obj in descendants(root):
            seen.setdefault(obj.name, obj)
    members = list(seen.values())

    box = _world_bbox(members)
    if box is None:
        raise ValueError("normalize_asset: no mesh geometry to measure")
    lo, hi = box
    target = float(params.get("target_height", 1.0))
    factor = target / max((hi - lo).z, 1e-6)

    ctrl = bpy.data.objects.new("AssetCtrl", None)
    bpy.context.scene.collection.objects.link(ctrl)
    ctrl.empty_display_size = 0.2
    for root in roots:
        if root.parent is None:
            root.parent = ctrl
            root.matrix_parent_inverse = ctrl.matrix_world.inverted()
    ctrl.scale = (factor, factor, factor)
    bpy.context.view_layer.update()

    lo2, hi2 = _world_bbox(members)
    ctrl.location = (
        ctrl.location.x - (lo2.x + hi2.x) / 2,
        ctrl.location.y - (lo2.y + hi2.y) / 2,
        ctrl.location.z - lo2.z,
    )
    bpy.context.view_layer.update()

    lo3, hi3 = _world_bbox(members)
    return {
        "controller": ctrl.name,
        "scale_factor": round(factor, 6),
        "bound_min": [round(lo3.x, 4), round(lo3.y, 4), round(lo3.z, 4)],
        "bound_max": [round(hi3.x, 4), round(hi3.y, 4), round(hi3.z, 4)],
    }


HANDLERS = {
    "ping": cmd_ping,
    "run_python": cmd_run_python,
    "get_scene_graph": cmd_get_scene_graph,
    "render_view": cmd_render_view,
    "render_passes": cmd_render_passes,
    "render_sequence": cmd_render_sequence,
    "save_blend": cmd_save_blend,
    "open_blend": cmd_open_blend,
    "image_stats": cmd_image_stats,
    "import_glb": cmd_import_glb,
    "normalize_asset": cmd_normalize_asset,
}


# --- serve loop ---------------------------------------------------------------------


def _write_ready(ready_file: str, port: int) -> None:
    """Publish the bound port atomically so the client never reads a half-written file."""
    import json

    tmp = f"{ready_file}.tmp"
    with open(tmp, "w") as fh:
        json.dump({"port": port, "pid": os.getpid()}, fh)
    os.replace(tmp, ready_file)


def serve(port: int, ready_file: str, render_dir: str) -> None:
    os.makedirs(render_dir, exist_ok=True)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(1)
    _write_ready(ready_file, srv.getsockname()[1])

    running = True
    while running:
        conn, _addr = srv.accept()
        with conn:
            while True:
                try:
                    request = recv_message(conn)
                except ConnectionError:
                    break
                if request is None:
                    break  # client disconnected without shutdown → wait for a reconnect
                command = request.get("command")
                params = request.get("params") or {}
                if command == "shutdown":
                    send_message(conn, {"ok": True, "result": {"bye": True}})
                    running = False
                    break
                handler = HANDLERS.get(command)
                if handler is None:
                    send_message(conn, {"ok": False, "error": f"unknown command: {command!r}"})
                    continue
                try:
                    result = handler(params, render_dir)
                    send_message(conn, {"ok": True, "result": result})
                except Exception:
                    send_message(conn, {"ok": False, "error": traceback.format_exc()})
    srv.close()


def _parse_args(argv: list[str]) -> dict:
    """Blender passes script args after ``--``; parse ``--key value`` pairs from them."""
    tail = argv[argv.index("--") + 1:] if "--" in argv else []
    opts: dict[str, str] = {}
    i = 0
    while i < len(tail):
        if tail[i].startswith("--"):
            key = tail[i][2:]
            value = tail[i + 1] if i + 1 < len(tail) else ""
            opts[key] = value
            i += 2
        else:
            i += 1
    return opts


def main() -> None:
    opts = _parse_args(sys.argv)
    serve(
        port=int(opts.get("port", "0")),
        ready_file=opts["ready-file"],
        render_dir=opts["render-dir"],
    )


if __name__ == "__main__":
    main()
