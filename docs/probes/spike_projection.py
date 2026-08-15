"""SPIKE: can a generated city plate be camera-projected onto layer 1's proxy city,
and how far does the parallax hold?

Projected FROM the f100 camera. f100 is therefore the free case — it should look like the
plate. f440 is the stress test: 74 units closer and 35 degrees of yaw arc away from the
projector, which is where a projection either holds or shears apart.
"""
import sys; sys.path.insert(0, ".")
from pathlib import Path
from pipeline.blender.session import BlenderSession
from pipeline.brief import load_shot
from pipeline.build_agent import _RESET, _preamble

shot = load_shot("shots/barrel_roll")
OUT = Path("/tmp/claude-1000/-home-sahan-Desktop-bambi-vfx/"
           "9428c845-d22d-4014-93d3-84155fc1c6eb/scratchpad/proj")
OUT.mkdir(parents=True, exist_ok=True)
PLATE = str((shot.folder / "refs/layer3/f100_city_L3.png").resolve())

s = BlenderSession(blender="blender", blend_file=None,
                   assets_dir=shot.folder / "assets", cwd=shot.folder).start()
try:
    s.run(_RESET); s.run(_preamble(shot))
    s.run((shot.folder / "build/01_layout.py").read_text(encoding="utf-8"))
    print("RESULT layer-1 scene built")

    s.run(f'''
import bpy
sc = bpy.context.scene
cam = sc.camera
sc.frame_set(100)
bpy.context.view_layer.update()

# A locked-off duplicate of the camera AT f100 — the projector.
pcam_data = cam.data.copy()
proj = bpy.data.objects.new("projector", pcam_data)
sc.collection.objects.link(proj)
proj.matrix_world = cam.matrix_world.copy()   # frozen; the render camera moves on

city = bpy.data.objects["proxy_city"]
me = city.data
if not me.uv_layers:
    me.uv_layers.new(name="proj")

mod = city.modifiers.new("cityproj", "UV_PROJECT")
mod.uv_layer = me.uv_layers[0].name
mod.projector_count = 1
mod.projectors[0].object = proj
mod.aspect_x = sc.render.resolution_x
mod.aspect_y = sc.render.resolution_y

mat = bpy.data.materials.new("city_projected"); mat.use_nodes = True
nt = mat.node_tree; nt.nodes.clear()
out = nt.nodes.new("ShaderNodeOutputMaterial")
em  = nt.nodes.new("ShaderNodeEmission")
tex = nt.nodes.new("ShaderNodeTexImage")
tex.image = bpy.data.images.load(r"{PLATE}")
tex.extension = "CLIP"                 # outside the frustum shows nothing, not a smear
uv  = nt.nodes.new("ShaderNodeUVMap"); uv.uv_map = me.uv_layers[0].name
nt.links.new(uv.outputs["UV"], tex.inputs["Vector"])
nt.links.new(tex.outputs["Color"], em.inputs["Color"])
em.inputs["Strength"].default_value = 1.0
nt.links.new(em.outputs[0], out.inputs["Surface"])
me.materials.clear(); me.materials.append(mat)
RESULT = "projection wired: " + str(len(me.polygons)) + " proxy faces"
''')
    for f in (100, 440):
        p = s.render(frame=f, mode="eevee", scale=0.5)
        import shutil; shutil.copyfile(p, OUT / f"proj_f{f}.png")
        print(f"RESULT rendered f{f} -> {OUT / f'proj_f{f}.png'}")
finally:
    s.close()
