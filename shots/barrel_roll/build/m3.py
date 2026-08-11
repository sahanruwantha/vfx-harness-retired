# build/m3.py — barrel_roll milestone M3 (frame 32): emerging inverted, the
# Silk Road 2.0 tower re-lit, the purple world revealed.
# Runs AFTER build/m1.py and build/m2.py in the same scene. Delta only:
#   - camera roll retimed: M2's f24 pose pinned, 215deg at f32 (emerging
#     inverted, world still partly overhead), 226deg at f36 (slow emergence,
#     slight shutter smear like the ref), then unwinding fast to 360 at f48
#   - small rig yaw pan (0 -> 0.03rad -> 0) keeping the tower central
#   - motion blur forced on: shutter 0.5 + 32 real time-steps (EEVEE's default
#     1-step post-process smear leaves tiny emissive dots sharp)
#   - green hero + crown hidden at the seam; the committed sr2_tower asset (baked
#     purple facade + 'Silk Road 2.0' sign) swapped in, emission ramped up f25-31
#   - world tint, cloud dome, city carpets, lesser towers and volumes re-keyed
#     from green to the purple colourway during the blackout (colours swap at
#     f21-23 while everything is black; strengths ramp back f25-31)
#   - seam lights fade out f24-28; bloom settles f26-32
# Deterministic: fixed frames/values, seeded scatter only.
import bpy, math

sc = bpy.context.scene


def key2(sock, f0, v0, f1, v1):
    sock.default_value = v0
    sock.keyframe_insert('default_value', frame=f0)
    sock.default_value = v1
    sock.keyframe_insert('default_value', frame=f1)


def emission_of(mat_name):
    nt = bpy.data.materials[mat_name].node_tree
    return nt, [n for n in nt.nodes if n.type == 'EMISSION'][0]


# ---------------------------------------------------------------- camera: retimed roll
cam = bpy.data.objects['cam']
rig = bpy.data.objects['cam_rig']

sc.frame_set(24)
r24 = cam.rotation_euler[2]                 # M2's exact interpolated pose (~174.26deg)
cam.rotation_euler[2] = r24
cam.keyframe_insert('rotation_euler', index=2, frame=24)
cam.rotation_euler[2] = math.radians(215.0) # emerging inverted: world partly overhead
cam.keyframe_insert('rotation_euler', index=2, frame=32)
cam.rotation_euler[2] = math.radians(226.0) # slow through the reveal, then unwind fast
cam.keyframe_insert('rotation_euler', index=2, frame=36)

ad = cam.animation_data
fcs = []
for layer in ad.action.layers:
    for strip in layer.strips:
        cb = strip.channelbag(ad.action_slot)
        if cb:
            fcs += list(cb.fcurves)
for fc in fcs:
    if fc.data_path == 'rotation_euler' and fc.array_index == 2:
        for kp in fc.keyframe_points:
            kp.interpolation = 'BEZIER'
            kp.handle_left_type = 'AUTO_CLAMPED'
            kp.handle_right_type = 'AUTO_CLAMPED'

# slight yaw pan, returns to 0 by f48 so M4 framing is untouched
rig.rotation_euler[2] = 0.0
rig.keyframe_insert('rotation_euler', index=2, frame=24)
rig.rotation_euler[2] = 0.03
rig.keyframe_insert('rotation_euler', index=2, frame=32)
rig.rotation_euler[2] = 0.0
rig.keyframe_insert('rotation_euler', index=2, frame=48)

# weighty, real motion blur on the roll/dive
sc.render.use_motion_blur = True
sc.render.motion_blur_shutter = 0.5
sc.eevee.motion_blur_steps = 32

# ---------------------------------------------------------------- seam swap: green hero out
for n in ('hero_tower', 'crown'):
    o = bpy.data.objects[n]
    o.hide_render = False
    o.keyframe_insert('hide_render', frame=20)
    o.hide_render = True
    o.keyframe_insert('hide_render', frame=21)

# ---------------------------------------------------------------- purple hero in
names = bvfx_import_asset('sr2_tower')
sr2 = bpy.data.objects[names[0]]
sr2.name = 'sr2_tower'
sr2.location = (25.0, 11.0, 50.0)
sr2.scale = (29.0, 29.0, 50.0)              # slim silhouette, 100 tall (import scale is 50)
sr2.hide_render = True
sr2.keyframe_insert('hide_render', frame=20)
sr2.hide_render = False
sr2.keyframe_insert('hide_render', frame=21)

# baked albedo -> crushed emission (dark circuit-board facade, glowing windows + sign)
mt = sr2.data.materials[0].node_tree
img = [n for n in mt.nodes if n.type == 'TEX_IMAGE' and n.image and 'Image_0' in n.image.name][0]
outn = [n for n in mt.nodes if n.type == 'OUTPUT_MATERIAL' and n.is_active_output][0]
gam = mt.nodes.new('ShaderNodeGamma')
gam.inputs['Gamma'].default_value = 3.0
em = mt.nodes.new('ShaderNodeEmission')
em.name = 'sr2_em'
mt.links.new(img.outputs['Color'], gam.inputs['Color'])
mt.links.new(gam.outputs['Color'], em.inputs['Color'])
mt.links.new(em.outputs[0], outn.inputs['Surface'])
s = em.inputs['Strength']
s.default_value = 0.0
s.keyframe_insert('default_value', frame=25)
s.default_value = 1.5
s.keyframe_insert('default_value', frame=27)
s.default_value = 7.5
s.keyframe_insert('default_value', frame=31)

# ---------------------------------------------------------------- world -> purple
wnt = sc.world.node_tree
bg = [n for n in wnt.nodes if n.type == 'BACKGROUND'][0]
vs = [n for n in wnt.nodes if n.type == 'VOLUME_SCATTER'][0]
key2(bg.inputs['Color'], 21, (0.010, 0.045, 0.018, 1), 23, (0.018, 0.012, 0.055, 1))
key2(bg.inputs['Strength'], 25, 0.0, 31, 0.25)
key2(vs.inputs['Color'], 21, (0.05, 0.20, 0.09, 1), 23, (0.10, 0.06, 0.28, 1))
key2(vs.inputs['Density'], 25, 0.0, 31, 0.0003)

# ---------------------------------------------------------------- cloud dome -> violet billows
cd = bpy.data.materials['cloud_dome_mat'].node_tree
rp = [n for n in cd.nodes if n.type == 'VALTORGB'][0].color_ramp
e0, e1 = rp.elements[0], rp.elements[1]
e0.color = (0.002, 0.005, 0.004, 1); e0.keyframe_insert('color', frame=21)
e1.color = (0.030, 0.052, 0.046, 1); e1.keyframe_insert('color', frame=21)
e0.color = (0.003, 0.002, 0.009, 1); e0.keyframe_insert('color', frame=23)
e1.color = (0.034, 0.026, 0.075, 1); e1.keyframe_insert('color', frame=23)
cem = [n for n in cd.nodes if n.type == 'EMISSION'][0]
key2(cem.inputs['Strength'], 25, 0.0, 31, 1.5)

# ---------------------------------------------------------------- city carpets re-lit
_, cge = emission_of('city_green_mat')
key2(cge.inputs['Color'], 21, (0.45, 1.0, 0.55, 1), 23, (0.55, 0.60, 1.0, 1))
key2(cge.inputs['Strength'], 25, 0.0, 31, 9.0)
for mn, tgt in (('city_warm_mat', 11.0), ('city_far_mat', 30.0)):
    _, e = emission_of(mn)
    key2(e.inputs['Strength'], 25, 0.0, 31, tgt)

# extra dense near-camera carpet for the purple era
bvfx_scatter_emissive(count=12000, area=900.0, z_range=(0.4, 2.0),
                      color=(1.0, 0.82, 0.55), strength=0.0, seed=47, dot=0.55,
                      name='city_p')
cp = bpy.data.objects['city_p']
cp.location = (0.0, -60.0, 0.0)
cp.hide_render = True
cp.keyframe_insert('hide_render', frame=20)
cp.hide_render = False
cp.keyframe_insert('hide_render', frame=21)
_, cpe = emission_of('city_p_mat')
key2(cpe.inputs['Strength'], 25, 0.0, 31, 7.0)

# ---------------------------------------------------------------- lesser towers -> cool violet
for mn in ('towers_a_tpl_windows', 'towers_b_tpl_windows', 'towers_c_tpl_windows'):
    nt, e = emission_of(mn)
    key2(e.inputs['Color'], 21, (0.9, 0.85, 0.55, 1), 23, (0.62, 0.66, 1.0, 1))
    key2(nt.nodes['bvfx_dim_' + mn].inputs[1], 25, 0.0, 31, 0.85)

# ---------------------------------------------------------------- wisps/haze -> purple, re-lit
VOLS = {
    #                 scatter color old       scatter color new      emission color new    em    dens
    'wisp_L_vol':      ((0.05, 0.10, 0.07, 1), (0.09, 0.06, 0.17, 1), (0.32, 0.22, 0.60, 1), 0.18, 0.45),
    'wisp_R_vol':      ((0.05, 0.10, 0.07, 1), (0.09, 0.06, 0.17, 1), (0.32, 0.22, 0.60, 1), 0.18, 0.45),
    'haze_vol':        ((0.06, 0.13, 0.09, 1), (0.08, 0.05, 0.16, 1), (0.30, 0.20, 0.55, 1), 0.08, 0.50),
    'horizon_haze_vol':((0.10, 0.10, 0.07, 1), (0.11, 0.08, 0.16, 1), (0.45, 0.32, 0.60, 1), 0.45, 0.80),
    'wisp_top_vol':    ((0.06, 0.14, 0.09, 1), (0.08, 0.06, 0.17, 1), (0.30, 0.22, 0.58, 1), 0.24, 0.60),
}
for mn, (c_old, c_new, ec_new, emf, dnf) in VOLS.items():
    nt = bpy.data.materials[mn].node_tree
    pv = [n for n in nt.nodes if n.type == 'PRINCIPLED_VOLUME'][0]
    key2(pv.inputs['Color'], 21, c_old, 23, c_new)
    key2(pv.inputs['Emission Color'], 21, c_old, 23, ec_new)
    key2(nt.nodes['bvfx_dim_' + mn + '_em'].inputs[1], 25, 0.0, 31, emf)
    key2(nt.nodes['bvfx_dim_' + mn + '_d'].inputs[1], 25, 0.0, 31, dnf)

# ---------------------------------------------------------------- seam lights out, bloom settles
_, sle = emission_of('seam_lights_mat')
key2(sle.inputs['Strength'], 24, 30.0, 28, 0.0)

ng = sc.compositing_node_group
g = [n for n in ng.nodes if n.type == 'GLARE'][0]
key2(g.inputs['Strength'], 26, 0.9, 32, 0.62)

sc.frame_set(32)
