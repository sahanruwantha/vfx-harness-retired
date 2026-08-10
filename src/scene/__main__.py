"""Phase 0 smoke: launch the bridge, build a gray-box scene, render it, read the scene graph.

Proves the whole perception loop end to end against a real headless Blender:
``python -m scene``. Exits non-zero if any step fails.
"""

from __future__ import annotations

import sys

from scene.bridge import BlenderBridge, BridgeError

# A minimal "table with two chair stand-ins", lit, with an aimed camera — the gray-box a real
# 3D beat starts from before assets and lighting are refined.
GRAY_BOX = """
import bpy
from mathutils import Vector

# clear the default scene, data-API (no operator context needed in --background)
for obj in list(bpy.data.objects):
    bpy.data.objects.remove(obj, do_unlink=True)

bpy.ops.mesh.primitive_plane_add(size=12, location=(0, 0, 0))
bpy.context.active_object.name = "Ground"

bpy.ops.mesh.primitive_cube_add(size=2, location=(0, 0, 0.75))
table = bpy.context.active_object
table.name = "Table"
table.scale = (1.4, 0.8, 0.05)

bpy.ops.mesh.primitive_cube_add(size=1, location=(0, -1.1, 0.45))
chair_l = bpy.context.active_object
chair_l.name = "Chair.L"
chair_l.scale = (0.5, 0.5, 0.9)

bpy.ops.mesh.primitive_cube_add(size=1, location=(0, 1.1, 0.45))
chair_r = bpy.context.active_object
chair_r.name = "Chair.R"
chair_r.scale = (0.5, 0.5, 0.9)

bpy.ops.object.light_add(type='SUN', location=(4, -4, 8))
bpy.context.active_object.data.energy = 4.0

bpy.ops.object.camera_add(location=(6, -6, 4.2))
cam = bpy.context.active_object
cam.name = "ShotCam"
direction = Vector((0, 0, 0.9)) - cam.location
cam.rotation_euler = direction.to_track_quat('-Z', 'Y').to_euler()
bpy.context.scene.camera = cam

result = {"objects": [o.name for o in bpy.context.scene.objects]}
"""


def main() -> int:
    try:
        with BlenderBridge() as bridge:
            print(f"• work dir: {bridge.work_dir}")
            pong = bridge.ping()
            print(f"• ping → Blender {pong['blender']}")

            built = bridge.run_python(GRAY_BOX)
            if not built.get("ok"):
                print("✗ gray-box build failed:\n" + built.get("error", ""), file=sys.stderr)
                return 1
            print(f"• built gray-box: {built['result']['objects']}")

            graph = bridge.get_scene_graph()
            scene = graph["scene"]
            print(
                f"• scene graph: {scene['object_count']} objects, "
                f"camera={scene['active_camera']}, engine={scene['engine']}"
            )
            for obj in graph["objects"]:
                print(
                    f"    {obj['name']:<10} {obj['type']:<7} "
                    f"loc={obj['location']} bbox={obj['bound_min']}→{obj['bound_max']}"
                )

            render = bridge.render(name="graybox.png", resolution=[480, 270], samples=16)
            print(
                f"• rendered {render['resolution'][0]}×{render['resolution'][1]} "
                f"via {render['engine']} → {render['path']} ({render['bytes']} bytes)"
            )
            if render["bytes"] <= 0:
                print("✗ render produced no file", file=sys.stderr)
                return 1
        print("✓ Phase 0 bridge works: build → scene graph → render, all headless.")
        return 0
    except BridgeError as exc:
        print(f"✗ bridge error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
