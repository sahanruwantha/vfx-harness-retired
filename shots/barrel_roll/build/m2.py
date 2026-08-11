# build/m2.py — barrel_roll milestone M2 (frame ~24): the inversion blackout.
# Runs AFTER build/m1.py in the same scene. Delta only:
#   - keys world tint + every emission (tower, crown, city, dome, volumes) from
#     full M1 values at f12 down to near-black by f21 (held black through the seam)
#   - a handful of hand-placed warm "seam lights" that fade in during the ramp and
#     survive as the only streaked lights at frame 24 (motion blur does the streaking)
#   - keys compositor bloom slightly up through the seam for the ref's halation
# Deterministic: fixed frames, fixed positions, no randomness.
import bpy

sc = bpy.context.scene
F_HOLD, F_BLACK = 12, 21          # hold full look until 12, fully black by 21


def key2(sock, v_hold, v_black):
    sock.default_value = v_hold
    sock.keyframe_insert('default_value', frame=F_HOLD)
    sock.default_value = v_black
    sock.keyframe_insert('default_value', frame=F_BLACK)


def dim_socket(nt, sock, tag, black=0.0):
    """Key a socket to black; if it's link-driven, insert a keyed MULTIPLY dimmer."""
    if sock.is_linked:
        src = sock.links[0].from_socket
        dim = nt.nodes.new('ShaderNodeMath')
        dim.operation = 'MULTIPLY'
        dim.name = 'bvfx_dim_' + tag
        nt.links.new(src, dim.inputs[0])
        nt.links.new(dim.outputs[0], sock)
        key2(dim.inputs[1], 1.0, black)
    else:
        key2(sock, sock.default_value, black * sock.default_value)


# ---------------------------------------------------------------- world -> black
wnt = sc.world.node_tree
bg = [n for n in wnt.nodes if n.type == 'BACKGROUND'][0]
vs = [n for n in wnt.nodes if n.type == 'VOLUME_SCATTER'][0]
key2(bg.inputs['Strength'], bg.inputs['Strength'].default_value, 0.0)
key2(vs.inputs['Density'], vs.inputs['Density'].default_value, 0.0)

# ---------------------------------------------------------------- emissives -> black
# window-grid materials (emission strength is link-driven -> dimmer node)
for mn in ('hero_tower_windows', 'towers_a_tpl_windows',
           'towers_b_tpl_windows', 'towers_c_tpl_windows'):
    nt = bpy.data.materials[mn].node_tree
    em = [n for n in nt.nodes if n.type == 'EMISSION'][0]
    dim_socket(nt, em.inputs['Strength'], mn)

# simple emission materials (unlinked strength -> key directly)
for mn in ('crown_grad', 'city_warm_mat', 'city_green_mat',
           'city_far_mat', 'cloud_dome_mat'):
    nt = bpy.data.materials[mn].node_tree
    em = [n for n in nt.nodes if n.type == 'EMISSION'][0]
    s = em.inputs['Strength']
    key2(s, s.default_value if s.default_value else 1.0, 0.0)

# volume materials: kill both glow and density at the seam
for mn in ('crown_aura_vol', 'wisp_L_vol', 'wisp_R_vol',
           'haze_vol', 'horizon_haze_vol', 'wisp_top_vol'):
    nt = bpy.data.materials[mn].node_tree
    pv = [n for n in nt.nodes if n.type == 'PRINCIPLED_VOLUME'][0]
    dim_socket(nt, pv.inputs['Emission Strength'], mn + '_em')
    dim_socket(nt, pv.inputs['Density'], mn + '_d')

# ---------------------------------------------------------------- seam lights
# The few streaked lights left alive at the seam (refs/M2_blackout.jpg).
# Hand-placed inside the frame-24 frustum; roll motion blur draws the streaks.
pts = [(150.0, 71.0, 25.0),
       (60.0, 171.0, 190.0),
       (-220.0, 171.0, 40.0),
       (280.0, 271.0, 140.0)]
me = bpy.data.meshes.new('seam_lights_pts')
me.from_pydata(pts, [], [])
inst = bpy.data.objects.new('seam_lights', me)
sc.collection.objects.link(inst)

bpy.ops.mesh.primitive_uv_sphere_add(radius=2.0, location=(0, 0, 0),
                                     segments=16, ring_count=8)
tpl = bpy.context.object
tpl.name = 'seam_lights_src'
mat = bvfx_emission('seam_lights_mat', (1.0, 0.78, 0.45), 30.0)
tpl.data.materials.append(mat)
tpl.parent = inst
inst.instance_type = 'VERTS'

# invisible at M1, fade in as the world dies
sem = [n for n in mat.node_tree.nodes if n.type == 'EMISSION'][0]
ss = sem.inputs['Strength']
ss.default_value = 0.0
ss.keyframe_insert('default_value', frame=12)
ss.default_value = 30.0
ss.keyframe_insert('default_value', frame=18)

# ---------------------------------------------------------------- seam bloom
# nudge glare up through the blackout so the surviving streaks halate like the ref
ng = sc.compositing_node_group
g = [n for n in ng.nodes if n.type == 'GLARE'][0]
gs = g.inputs['Strength']
gs.default_value = 0.7
gs.keyframe_insert('default_value', frame=12)
gs.default_value = 0.9
gs.keyframe_insert('default_value', frame=21)
