"""Hybrid shots — a directable 3D subject composited over a photoreal plate backplate.

The compositor's answer to the 3D-vs-plate tension: don't make one tool do everything. The plate
(Higgsfield) carries the atmosphere geometry can't reach; the *subject* is a Tripo mesh (image→3D,
directable — you place, frame, and light it); the 3D subject renders with a transparent film and is
composited OVER the plate via an Alpha-Over in the compositor. Best of both: photoreal atmosphere +
a subject you control.

Two pure code-gens the harness/staged loop drive over the bridge: :func:`hybrid_stage_code` (a
plate-ready stage — transparent film, a shadow-catch ground, a hero camera, key light, NO visible
backdrop geometry) and :func:`plate_backdrop_code` (wire the plate under the 3D render). Blender 5.x
node-group compositor (see ``scene.compositor``).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path


def hybrid_stage_code(
    target_height: float, *, key_color=(0.7, 1.0, 0.8), key_energy: float = 3.0, shadow_ground: bool = True
) -> str:
    """Clear the scene and set a plate-ready stage: transparent film (so the plate shows through), a
    key light, and a low hero camera framed for a subject of ``target_height`` sitting centered at the
    origin. No visible backdrop — the plate is the world. ``shadow_ground`` adds a shadow-catcher
    plane so the subject grounds into the plate (skipped where the renderer lacks shadow catching)."""
    h = float(target_height)
    cam_y = round(-(2.2 * h + 2.0) * 0.8, 3)
    cam_z = round(h * 0.45, 3)
    look_z = round(h * 0.5, 3)
    ground = (
        """
# shadow-catcher ground so the subject grounds into the plate; removed if the renderer can't catch
bpy.ops.mesh.primitive_plane_add(size=200, location=(0, 0, 0))
_g = bpy.context.active_object
_g.name = "ShadowGround"
if hasattr(_g, "is_shadow_catcher"):
    _g.is_shadow_catcher = True
else:
    bpy.data.objects.remove(_g, do_unlink=True)
"""
        if shadow_ground
        else ""
    )
    return f"""
import bpy
from mathutils import Vector

for obj in list(bpy.data.objects):
    bpy.data.objects.remove(obj, do_unlink=True)

scene = bpy.context.scene
scene.render.engine = 'BLENDER_EEVEE'
scene.render.film_transparent = True  # background is alpha → the plate shows through the composite
{ground}
bpy.ops.object.light_add(type='SUN', location=(4, -3, 8))
key = bpy.context.active_object.data
key.energy = {key_energy}
key.color = {key_color}

bpy.ops.object.camera_add(location=(0, {cam_y}, {cam_z}))
cam = bpy.context.active_object
cam.name = "ShotCam"
cam.data.lens = 35
direction = Vector((0, 0, {look_z})) - cam.location
cam.rotation_euler = direction.to_track_quat('-Z', 'Y').to_euler()
scene.camera = cam

result = {{"ok": True, "target_height": {h}}}
"""


def plate_backdrop_code(plate_path: str | Path, *, distance: float = 100.0) -> str:
    """Place the plate as a camera-locked emissive backdrop plane behind the subject.

    Real geometry, not the compositor — far more reliable than Blender 5.x's reworked node-group
    compositor. The plane is parented to the camera, sized to fill its field of view at ``distance``,
    and emits the plate image (unlit, so it shows at full brightness). The 3D subject renders in
    front of it. No ``film_transparent``, no compositor."""
    path = str(plate_path)
    return f"""
import bpy
import math
from mathutils import Matrix
scene = bpy.context.scene
scene.render.film_transparent = False
cam = scene.camera
image = bpy.data.images.load({path!r}, check_existing=True)

# an unlit emissive material carrying the plate (shows at full brightness regardless of lighting)
mat = bpy.data.materials.new("Backdrop")
mat.use_nodes = True
tree = mat.node_tree
tree.nodes.clear()
out = tree.nodes.new("ShaderNodeOutputMaterial")
emit = tree.nodes.new("ShaderNodeEmission")
tex = tree.nodes.new("ShaderNodeTexImage")
tex.image = image
emit.inputs["Strength"].default_value = 1.0
tree.links.new(tex.outputs["Color"], emit.inputs["Color"])
tree.links.new(emit.outputs[0], out.inputs["Surface"])

# a plane parented to the camera, filling its FOV at `distance` (camera-locked billboard)
D = {distance}
fov = cam.data.angle
aspect = scene.render.resolution_x / max(1, scene.render.resolution_y)
half_w = D * math.tan(fov / 2.0)
half_h = half_w / aspect
bpy.ops.mesh.primitive_plane_add(size=2.0)  # spans -1..1
plane = bpy.context.active_object
plane.name = "Backdrop"
plane.data.materials.append(mat)
plane.parent = cam
plane.matrix_parent_inverse = Matrix.Identity(4)
plane.location = (0.0, 0.0, -D)  # camera-local: straight ahead (camera looks down -Z)
plane.scale = (half_w * 1.05, half_h * 1.05, 1.0)
result = {{"backdrop": True, "plate": {path!r}}}
"""


# --- hybrid realizer: plate backdrop + a desk-built directable subject over it -----------

# Prepare a hybrid stage: clear the scene, set EEVEE + a neutral view transform, and place a low
# hero camera. plate_backdrop_code (run next) parents the plate plane to this camera.
_HYBRID_CAMERA_CODE = """
import bpy, math
for _o in list(bpy.data.objects):
    bpy.data.objects.remove(_o, do_unlink=True)
scene = bpy.context.scene
try: scene.render.engine = 'BLENDER_EEVEE'
except Exception: pass
try:
    scene.view_settings.view_transform = 'AgX'; scene.view_settings.exposure = 0.0; scene.view_settings.look = 'None'
except Exception: pass
cam_d = bpy.data.cameras.new('Cam'); cam_d.lens = 35
cam = bpy.data.objects.new('Cam', cam_d)
bpy.context.collection.objects.link(cam); scene.camera = cam
cam.location = (0.0, -9.0, 2.2); cam.rotation_euler = (math.radians(84.0), 0.0, 0.0)
result = {'camera': cam.name}
"""


def hybrid_subject_brief(subject: str) -> str:
    """The desk brief for the subject half of a hybrid shot — build only the foreground, keep the plate."""
    return (
        f"Build the directable 3D SUBJECT for this shot: {subject.strip()}.\n"
        "An atmosphere PLATE is ALREADY the world backdrop — a camera-locked emissive plane named "
        "'Backdrop' behind everything, showing the reference environment/sky — and a hero CAMERA is "
        "set. Build ONLY the foreground subject (near the origin) and light it to sit believably IN "
        "FRONT of the plate: match the plate's light direction, colour and mood so the subject and the "
        "plate read as ONE shot. You may reframe the camera, but do NOT delete the camera or the "
        "'Backdrop' plane, and do NOT build your own sky/background — the plate IS the background."
    )


def make_hybrid_realizer(
    *,
    bridge,
    out_dir: str | Path,
    plate_generator: Callable[..., Awaitable] | None = None,
    build: Callable[..., Awaitable] | None = None,
    distance: float = 100.0,
    resolution: tuple[int, int] = (768, 432),
    samples: int = 64,
    max_rounds: int = 3,
    refs_subdir: str = "refs",
):
    """A ``render_3d`` leaf that realizes a beat as a HYBRID: an AI atmosphere plate as the backdrop
    (the look geometry can't reach) with a desk-built, directable 3D subject composited in front of it
    via a real camera-locked backdrop plane (not the flaky 5.x compositor).

    Reuses :func:`scene.plate.make_plate_generator` for the plate and :func:`scene.harness.build_shot`
    (with its ``prepare`` hook) for the subject; both injectable for testing. Caller owns the bridge.
    """
    from develop.ledger import Clip
    from scene.critic import load_reference_images
    from scene.harness import build_shot
    from scene.plate import make_plate_generator

    _build = build or build_shot
    _plate = plate_generator or make_plate_generator(out_dir=out_dir)

    async def render(entry):
        beat_dir = Path(out_dir) / entry.id
        beat_dir.mkdir(parents=True, exist_ok=True)
        subject = entry.intent.subject or entry.intent.heading

        plate_clip = await _plate(entry)
        plate_path = getattr(plate_clip, "fetched_path", None)
        if not plate_path:
            gap = getattr(plate_clip, "acquisition_gap", None) or "unknown"
            return Clip(licence="KNOWN", acquisition_gap=f"hybrid: plate unavailable for {subject!r}: {gap}")

        references = load_reference_images(beat_dir / refs_subdir)

        def prepare() -> None:
            bridge.run_python(_HYBRID_CAMERA_CODE)
            bridge.run_python(plate_backdrop_code(plate_path, distance=distance))

        result = await _build(
            bridge=bridge, brief=hybrid_subject_brief(subject), reference_images=references,
            subject=subject, out_dir=beat_dir, prepare=prepare, max_rounds=max_rounds,
            resolution=resolution, samples=samples,
        )
        best = result.best
        if best is None or best.render_path is None or best.render_frame is None:
            return Clip(licence="KNOWN", acquisition_gap=f"hybrid subject build failed for {subject!r}")
        return Clip(
            fetched_path=Path(best.render_path),
            licence="KNOWN",
            frames=(best.render_frame,),
            render_meta={"kind": "hybrid", "plate": str(plate_path), "subject": subject, "score": f"{best.score:.2f}"},
        )

    return render
