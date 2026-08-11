# build/m1.py — barrel_roll milestone M1 (frame 1): upright, full green world,
# hero tower at full emission, low aerial dive beginning.
# Assumes: empty scene; engine/frame range/fps/motion blur already set by harness.
# Deterministic: fixed seeds everywhere.
import bpy, bmesh, random, math

sc = bpy.context.scene

# ---------------------------------------------------------------- hero tower
names = bvfx_import_asset('sr2_tower')
hero = bpy.data.objects[names[0]]
hero.name = 'hero_tower'
bvfx_emissive_windows(hero, window_color=(0.10, 1.0, 0.20), strength=12.0,
                      density=24, aspect=1.2, mortar=0.28, body_glow=0.08)

# --- break the window grid: per-cell random on/off + intensity + structural bands ---
_wnt = hero.active_material.node_tree
_nodes, _links = _wnt.nodes, _wnt.links
_sep = [n for n in _nodes if n.type == 'SEPXYZ'][0]
_m10 = _wnt.nodes['Math.010']     # window mask (0/1)
_m11 = _wnt.nodes['Math.011']     # mask*strength + body_glow -> Emission.Strength

def _math(op, v0=None, v1=None):
    n = _nodes.new('ShaderNodeMath'); n.operation = op
    if v0 is not None: n.inputs[0].default_value = v0
    if v1 is not None: n.inputs[1].default_value = v1
    return n

_sx = _math('MULTIPLY', None, 24.925); _links.new(_sep.outputs['X'], _sx.inputs[0])
_fx = _math('FLOOR');                  _links.new(_sx.outputs[0], _fx.inputs[0])
_sz = _math('MULTIPLY', None, 14.4);   _links.new(_sep.outputs['Z'], _sz.inputs[0])
_fz = _math('FLOOR');                  _links.new(_sz.outputs[0], _fz.inputs[0])
_comb = _nodes.new('ShaderNodeCombineXYZ')
_links.new(_fx.outputs[0], _comb.inputs['X']); _links.new(_fz.outputs[0], _comb.inputs['Y'])
_comb.inputs['Z'].default_value = 0.7
_wn = _nodes.new('ShaderNodeTexWhiteNoise'); _wn.noise_dimensions = '3D'
_links.new(_comb.outputs[0], _wn.inputs['Vector'])
_on = _math('GREATER_THAN', None, 0.30); _links.new(_wn.outputs['Value'], _on.inputs[0])
_vm = _math('MULTIPLY', None, 1.05);     _links.new(_wn.outputs['Value'], _vm.inputs[0])
_va = _math('ADD', None, 0.30);          _links.new(_vm.outputs[0], _va.inputs[0])
_var = _math('MULTIPLY'); _links.new(_on.outputs[0], _var.inputs[0]); _links.new(_va.outputs[0], _var.inputs[1])
_rm = _math('MODULO', None, 6.0);        _links.new(_fz.outputs[0], _rm.inputs[0])
_bd = _math('GREATER_THAN', None, 0.5);  _links.new(_rm.outputs[0], _bd.inputs[0])
_bf = _math('MULTIPLY', None, 0.85);     _links.new(_bd.outputs[0], _bf.inputs[0])
_bg = _math('ADD', None, 0.15);          _links.new(_bf.outputs[0], _bg.inputs[0])
_fac = _math('MULTIPLY'); _links.new(_var.outputs[0], _fac.inputs[0]); _links.new(_bg.outputs[0], _fac.inputs[1])
_nm = _math('MULTIPLY')
_links.new(_m10.outputs[0], _nm.inputs[0]); _links.new(_fac.outputs[0], _nm.inputs[1])
_links.new(_nm.outputs[0], _m11.inputs[0])

# --- structured glowing crown: tiered cylinders, green->white gradient ---
bpy.ops.object.select_all(action='DESELECT')
_parts = []
for r, h, z in ((7.5, 5.0, 102.5), (5.0, 5.0, 107.0), (2.4, 9.0, 112.0)):
    bpy.ops.mesh.primitive_cylinder_add(radius=r, depth=h, location=(0, 0, z), vertices=24)
    _parts.append(bpy.context.object)
for p in _parts:
    p.select_set(True)
bpy.context.view_layer.objects.active = _parts[0]
bpy.ops.object.join()
crown = bpy.context.object
crown.name = 'crown'
crown.scale = (1.35, 1.35, 1.1)
cm = bpy.data.materials.new('crown_grad')
cm.use_nodes = True
cnt = cm.node_tree
cnt.nodes.clear()
cout = cnt.nodes.new('ShaderNodeOutputMaterial')
cgeo = cnt.nodes.new('ShaderNodeNewGeometry')
csep = cnt.nodes.new('ShaderNodeSeparateXYZ')
cmr = cnt.nodes.new('ShaderNodeMapRange')
cmr.inputs['From Min'].default_value = 100.0
cmr.inputs['From Max'].default_value = 117.0
crp = cnt.nodes.new('ShaderNodeValToRGB')
crp.color_ramp.elements[0].position = 0.0
crp.color_ramp.elements[0].color = (0.12, 1.0, 0.22, 1)
crp.color_ramp.elements[1].position = 1.0
crp.color_ramp.elements[1].color = (0.50, 1.0, 0.55, 1)
cem = cnt.nodes.new('ShaderNodeEmission')
cem.inputs['Strength'].default_value = 5.0
cnt.links.new(cgeo.outputs['Position'], csep.inputs['Vector'])
cnt.links.new(csep.outputs['Z'], cmr.inputs['Value'])
cnt.links.new(cmr.outputs['Result'], crp.inputs['Fac'])
cnt.links.new(crp.outputs['Color'], cem.inputs['Color'])
cnt.links.new(cem.outputs[0], cout.inputs['Surface'])
crown.data.materials.append(cm)

# green energy flood around the tower's upper half (soft, wispy — not a solid pill)
bvfx_volume(name='crown_aura', center=(0, 0, 74), size=(78, 78, 120),
            optical_depth=0.45, color=(0.13, 0.9, 0.22), emission_strength=4.5,
            noise_scale=2.2, contrast=(0.40, 0.75))

# ---------------------------------------------------------------- ground
bpy.ops.mesh.primitive_plane_add(size=4000, location=(0, 0, 0))
g = bpy.context.object
g.name = 'ground'
gm = bpy.data.materials.new('ground_mat')
gm.use_nodes = True
bsdf = gm.node_tree.nodes['Principled BSDF']
bsdf.inputs['Base Color'].default_value = (0.004, 0.010, 0.005, 1)
bsdf.inputs['Roughness'].default_value = 0.9
g.data.materials.append(gm)

# ---------------------------------------------------------------- city lights
bvfx_scatter_emissive(count=20000, area=1400.0, z_range=(0.5, 3.0),
                      color=(1.0, 0.88, 0.60), strength=35.0, seed=7, dot=0.9, name='city_warm')
bvfx_scatter_emissive(count=5000, area=1200.0, z_range=(0.5, 3.0),
                      color=(0.45, 1.0, 0.55), strength=28.0, seed=13, dot=0.8, name='city_green')
bvfx_scatter_emissive(count=15000, area=1600.0, z_range=(0.5, 3.0),
                      color=(1.0, 0.9, 0.65), strength=45.0, seed=31, dot=1.4, name='city_far')
far = bpy.data.objects.get('city_far')
if far:
    far.location.y = 700.0

# ---------------------------------------------------------------- lesser tower fields
def tower_field(name, n, seed, w, h):
    rnd = random.Random(seed)
    me = bpy.data.meshes.new(name + '_pts')
    verts = []
    while len(verts) < n:
        x = rnd.uniform(-900, 900); y = rnd.uniform(-400, 1100)
        if (x * x + y * y) ** 0.5 < 130: continue        # clear of hero
        if abs(x) < 90 and y < 40: continue              # clear of camera corridor
        verts.append((x, y, 0))
    me.from_pydata(verts, [], [])
    inst = bpy.data.objects.new(name, me)
    sc.collection.objects.link(inst)
    bpy.ops.mesh.primitive_cube_add(size=1)
    t = bpy.context.object
    t.name = name + '_tpl'
    t.scale = (w, w, h); t.location = (0, 0, h / 2)
    bpy.ops.object.transform_apply(scale=True, location=True)
    bvfx_emissive_windows(t, window_color=(0.9, 0.85, 0.55), strength=2.2,
                          density=7, aspect=1.2, mortar=0.45)
    t.parent = inst
    inst.instance_type = 'VERTS'

tower_field('towers_a', 40, 21, 8, 30)
tower_field('towers_b', 50, 22, 6, 18)
tower_field('towers_c', 30, 23, 10, 45)

# ---------------------------------------------------------------- world + volumetrics
bvfx_volumetric_world(color=(0.010, 0.045, 0.018), bg_strength=0.4,
                      vol_color=(0.05, 0.20, 0.09), density=0.0003)
sc.eevee.volumetric_start = 0.1
sc.eevee.volumetric_end = 800.0
try:
    sc.eevee.volumetric_tile_size = '2'
except Exception:
    pass
sc.eevee.volumetric_samples = 64

# ---------------------------------------------------------------- cloud ceiling dome
bpy.ops.mesh.primitive_uv_sphere_add(radius=3000, location=(0, 0, -100), segments=48, ring_count=24)
dome = bpy.context.object
dome.name = 'cloud_dome'
dome.scale = (1, 1, 0.30)
m = bpy.data.materials.new('cloud_dome_mat')
m.use_nodes = True
nt = m.node_tree
nt.nodes.clear()
out = nt.nodes.new('ShaderNodeOutputMaterial')
tc = nt.nodes.new('ShaderNodeTexCoord')
mp = nt.nodes.new('ShaderNodeMapping')
mp.inputs['Scale'].default_value = (1, 1, 0.55)
no = nt.nodes.new('ShaderNodeTexNoise')
no.inputs['Scale'].default_value = 2.2
no.inputs['Detail'].default_value = 7.0
no.inputs['Roughness'].default_value = 0.55
rp = nt.nodes.new('ShaderNodeValToRGB')
rp.color_ramp.elements[0].position = 0.38
rp.color_ramp.elements[0].color = (0.002, 0.005, 0.004, 1)
rp.color_ramp.elements[1].position = 0.76
rp.color_ramp.elements[1].color = (0.030, 0.052, 0.046, 1)
em = nt.nodes.new('ShaderNodeEmission')
em.inputs['Strength'].default_value = 1.0
nt.links.new(tc.outputs['Generated'], mp.inputs['Vector'])
nt.links.new(mp.outputs['Vector'], no.inputs['Vector'])
nt.links.new(no.outputs['Fac'], rp.inputs['Fac'])
nt.links.new(rp.outputs['Color'], em.inputs['Color'])
nt.links.new(em.outputs[0], out.inputs['Surface'])
dome.data.materials.append(m)
bm = bmesh.new()
bm.from_mesh(dome.data)
for f in bm.faces:
    f.normal_flip()
bm.to_mesh(dome.data)
bm.free()

# ---------------------------------------------------------------- wisps + haze
bvfx_volume(name='wisp_L', center=(-280, 160, 130), size=(420, 90, 140),
            optical_depth=0.22, color=(0.05, 0.10, 0.07), emission_strength=0.07,
            noise_scale=2.2, contrast=(0.48, 0.68), stretch=(0.3, 1.0, 1.0))
bvfx_volume(name='wisp_R', center=(300, 190, 155), size=(420, 90, 150),
            optical_depth=0.22, color=(0.05, 0.10, 0.07), emission_strength=0.07,
            noise_scale=2.6, contrast=(0.48, 0.68), stretch=(0.3, 1.0, 1.0))
bvfx_volume(name='haze', center=(0, 320, 170), size=(1100, 160, 260),
            optical_depth=0.22, color=(0.06, 0.13, 0.09), emission_strength=0.04,
            noise_scale=1.8, contrast=(0.48, 0.72), stretch=(0.35, 1.0, 1.0))
bvfx_volume(name='horizon_haze', center=(0, 750, 30), size=(1800, 300, 60),
            optical_depth=0.30, color=(0.10, 0.10, 0.07), emission_strength=0.10,
            noise_scale=1.8, contrast=(0.42, 0.70), stretch=(0.35, 1.0, 1.0))
bvfx_volume(name='wisp_top', center=(0, 0, 340), size=(1000, 300, 170),
            optical_depth=0.25, color=(0.06, 0.14, 0.09), emission_strength=0.06,
            noise_scale=1.6, contrast=(0.45, 0.70), stretch=(0.35, 1.0, 1.0))

# ---------------------------------------------------------------- camera: dive + CCW barrel roll
rig = bpy.data.objects.new('cam_rig', None)
sc.collection.objects.link(rig)
rig.location = (0, -190, 30)
cam_data = bpy.data.cameras.new('cam')
cam_data.lens = 24.0
cam_data.clip_end = 12000.0
cam = bpy.data.objects.new('cam', cam_data)
sc.collection.objects.link(cam)
cam.parent = rig
sc.camera = cam
bvfx_aim(rig, (0, 0, 58))                    # slight up-tilt: horizon sits low in frame
rig.keyframe_insert('location', frame=1)
rig.location = (0, -168, 24)                 # ~22u forward dive
rig.keyframe_insert('location', frame=48)
cam.rotation_mode = 'XYZ'
cam.rotation_euler = (0, 0, 0)               # upright at M1
cam.keyframe_insert('rotation_euler', frame=1)
cam.rotation_euler = (0, 0, math.radians(360))   # one full CCW barrel roll
cam.keyframe_insert('rotation_euler', frame=48)

def _set_bezier(ob):
    ad = ob.animation_data
    if not ad or not ad.action:
        return
    try:
        fcs = list(ad.action.fcurves)
    except Exception:
        fcs = []
        slot = ad.action_slot
        for layer in ad.action.layers:
            for strip in layer.strips:
                cb = strip.channelbag(slot) if slot else None
                fcs += list(cb.fcurves) if cb else [f for b in strip.channelbags for f in b.fcurves]
    for fc in fcs:
        for kp in fc.keyframe_points:
            kp.interpolation = 'BEZIER'
            kp.handle_left_type = 'AUTO_CLAMPED'
            kp.handle_right_type = 'AUTO_CLAMPED'

_set_bezier(rig)
_set_bezier(cam)

# ---------------------------------------------------------------- grade: bloom
bvfx_glare_bloom(threshold=1.2, size=1.0, strength=0.7)
