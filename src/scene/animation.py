"""Time — the animation layer. Keyframed Blender scenes rendered as a frame sequence.

Everything before this made ONE frame; the Hansa Silk Road beats are motion — a code-deletion
dissolve and a barrel-roll world-turn. Per the director's call these are built with Blender
KEYFRAME animation (fully directable, no AI video), rendered as a sequence, and judged by sampling
start/mid/end frames against the reference beat (the existing scene critic, which already accepts
multiple frames). See the breakdown's 03:03.5–03:05.5 beat.

:func:`barrel_roll_code` is Beat 2 as a single continuous ``oner``: a keyframed camera roll + dive
that passes through a blackout at full inversion, swapping a green world for a purple 'Silk Road 2.0'
world in the dark — the invisible-cut technique done as one unbroken move. :func:`sample_indices`
picks the frames the critic judges.
"""

from __future__ import annotations


def sample_indices(count: int, n: int = 3) -> list[int]:
    """Evenly-spaced frame indices (start … end) to hand the keyframe critic — always the ends."""
    if count <= 0:
        return []
    if count <= n:
        return list(range(count))
    return [int(i * (count - 1) / (n - 1) + 0.5) for i in range(n)]  # round half up (predictable)


def barrel_roll_code(*, frames: int = 48, roll_turns: float = 1.0, dive: float = 22.0) -> str:
    """A keyframed barrel-roll oner: green Silk-Road world → blackout at inversion → purple
    Silk-Road-2.0 world, all in one continuous camera move. ``frames`` long; ``roll_turns`` full
    counter-clockwise turns; ``dive`` forward travel. World colour + tower emission are keyed to
    near-black across the middle to hide the world-swap."""
    import math

    end = int(frames)
    mid = end // 2
    total_roll = -2.0 * math.pi * float(roll_turns)  # counter-clockwise
    return f"""
import bpy, math
from mathutils import Vector, Euler

for obj in list(bpy.data.objects):
    bpy.data.objects.remove(obj, do_unlink=True)
for blk in (bpy.data.materials, bpy.data.worlds):
    for b in list(blk):
        blk.remove(b)

scene = bpy.context.scene
scene.render.engine = 'BLENDER_EEVEE'
scene.frame_start, scene.frame_end = 1, {end}
scene.render.motion_blur_shutter = 1.0
try: scene.render.use_motion_blur = True
except Exception: pass

def emissive(name, color, strength):
    m = bpy.data.materials.new(name); m.use_nodes = True
    nt = m.node_tree; nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial"); em = nt.nodes.new("ShaderNodeEmission")
    em.inputs[0].default_value = (color[0], color[1], color[2], 1.0); em.inputs[1].default_value = strength
    nt.links.new(em.outputs[0], out.inputs[0]); return m, em

def tower(name, loc, color):
    mat, em = emissive(name + "_m", color, 6.0)
    bpy.ops.mesh.primitive_cube_add(size=1, location=loc)
    t = bpy.context.active_object; t.name = name; t.scale = (1.6, 1.6, 9.0)
    t.data.materials.append(mat)
    return t, em

def ground(name, loc, color):
    mat, _ = emissive(name + "_m", color, 3.0)
    bpy.ops.mesh.primitive_plane_add(size=400, location=loc)
    g = bpy.context.active_object; g.name = name; g.data.materials.append(mat)

# world A (green) around origin; world B (purple) further along +Y (the camera dives from A into B)
GREEN = (0.10, 1.0, 0.35); PURPLE = (0.55, 0.20, 0.95)
tA, emA = tower("towerA", (0, 0, 9), GREEN); ground("cityA", (0, 0, -1), (0.05, 0.30, 0.14))
tB, emB = tower("towerB", (0, {dive} + 14, 9), PURPLE); ground("cityB", (0, {dive} + 14, -1), (0.22, 0.10, 0.34))

# world background, keyed green -> black (blackout) -> purple
world = bpy.data.worlds.new("W"); scene.world = world; world.use_nodes = True
bg = world.node_tree.nodes["Background"]
def key_color(node_input, frame, value):
    node_input.default_value = value; node_input.keyframe_insert("default_value", frame=frame)
key_color(bg.inputs[0], 1, (0.02, 0.10, 0.06, 1)); key_color(bg.inputs[0], {mid}, (0.0, 0.0, 0.0, 1)); key_color(bg.inputs[0], {end}, (0.10, 0.04, 0.16, 1))
bg.inputs[1].default_value = 0.4; bg.inputs[1].keyframe_insert("default_value", frame=1)
bg.inputs[1].default_value = 0.02; bg.inputs[1].keyframe_insert("default_value", frame={mid})
bg.inputs[1].default_value = 0.4; bg.inputs[1].keyframe_insert("default_value", frame={end})

# emission dims to near-black at the inversion, hiding the swap
for em in (emA, emB):
    for f, s in ((1, 6.0), ({mid}, 0.2), ({end}, 6.0)):
        em.inputs[1].default_value = s; em.inputs[1].keyframe_insert("default_value", frame=f)

# camera: one continuous roll + forward dive from world A to world B
cam_data = bpy.data.cameras.new("cam"); cam_data.lens = 30
cam = bpy.data.objects.new("cam", cam_data); scene.collection.objects.link(cam); scene.camera = cam
for f in range(1, {end} + 1):
    t = (f - 1) / ({end} - 1)
    y = -10.0 + t * ({dive} + 24.0)     # dive forward through A, blackout, into B
    z = 6.0
    cam.location = (0.0, y, z)
    roll = t * ({total_roll})           # continuous counter-clockwise barrel roll
    look = Vector((0, y + 8, 8)) - Vector(cam.location)
    base = look.to_track_quat('-Z', 'Y').to_euler()
    cam.rotation_euler = Euler((base.x, base.y + roll, base.z))
    cam.keyframe_insert("location", frame=f)
    cam.keyframe_insert("rotation_euler", frame=f)

result = {{"ok": True, "frames": {end}}}
"""
