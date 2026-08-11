# build/m4.py — M4 delta: frame 48 "settled, fully inverted; Silk Road 2.0 world; dive finished"
# Runs AFTER m1.py, m2.py, m3.py in the same scene. All changes are keyed at frame 32
# with the pre-existing evaluated values so M2/M3 frames are untouched, then ramped to
# the M4 look at frame 48. Deterministic — no randomness (white-noise mask is a fixed
# procedural texture).
import bpy

O = bpy.data.objects
M = bpy.data.materials


def _key(sock, frame, value):
    sock.default_value = value
    sock.keyframe_insert('default_value', frame=frame)


# ---------------------------------------------------------------------------
# 1) seam_lights: emission is 0 from f28 (m2 fade-out) but the mesh kept
#    rendering as black specks against the sky. Hide it once fully faded.
sl = O['seam_lights']
sl.hide_render = False
sl.keyframe_insert('hide_render', frame=28)
sl.hide_render = True
sl.keyframe_insert('hide_render', frame=29)
sl.hide_viewport = True

# ---------------------------------------------------------------------------
# 2) purple city grain: the 0.55u instanced dots read as giant bokeh blobs in
#    the near field. Shrink the instanced dot template to ~0.21u and compensate
#    emission so total flux per dot is unchanged on the already-scored frames
#    (0.55/0.21)^2 * 7.0 ≈ 47 → finer, equally-bright grain at f31/32.
src = O['city_p_src']
src.scale = (src.scale[0] * 0.3815, src.scale[1] * 0.3815, src.scale[2])

nt = M['city_p_mat'].node_tree
act = nt.animation_data.action
cb = act.layers[0].strips[0].channelbag(act.slots[0])
for fc in cb.fcurves:
    if 'Emission' in fc.data_path:
        for k in fc.keyframe_points:
            if k.co[0] == 31.0:
                k.co[1] = 47.0  # compensated (was 7.0 with the big dots)
        fc.update()
em = nt.nodes['Emission'].inputs[1]
_key(em, 32, 47.0)
_key(em, 48, 42.0)

# ---------------------------------------------------------------------------
# 3) horizon haze: dim the lilac band toward the ref's darker violet horizon
nt = M['horizon_haze_vol'].node_tree
_key(nt.nodes['bvfx_dim_horizon_haze_vol_em'].inputs[1], 32, 0.45)
_key(nt.nodes['bvfx_dim_horizon_haze_vol_em'].inputs[1], 48, 0.09)
_key(nt.nodes['bvfx_dim_horizon_haze_vol_d'].inputs[1], 32, 0.80)
_key(nt.nodes['bvfx_dim_horizon_haze_vol_d'].inputs[1], 48, 0.55)

# ---------------------------------------------------------------------------
# 4) cloud dome: settle a touch darker so the sky reads inky with wisp contrast
nt = M['cloud_dome_mat'].node_tree
_key(nt.nodes['Emission'].inputs[1], 32, 1.50)
_key(nt.nodes['Emission'].inputs[1], 48, 0.75)

# ---------------------------------------------------------------------------
# 5) SR2 hero tower texture emission: gamma keyed down lifts the mid-grey
#    window pixels selectively; sign strength kept at 7.5 so the glyphs stay
#    legible through the bloom (was clipping to white at 12+).
nt = M['Material_0.001'].node_tree
_key(nt.nodes['sr2_em'].inputs[1], 32, 7.5)
_key(nt.nodes['sr2_em'].inputs[1], 48, 7.5)
_key(nt.nodes['Gamma'].inputs['Gamma'], 32, 3.00)
_key(nt.nodes['Gamma'].inputs['Gamma'], 48, 2.25)

# ---------------------------------------------------------------------------
# 6) circuit-board window shell: dense grid of small random-on/off cyan cells
#    wrapping the shaft (z 25–73, half-width 7.7 → shell 15.9 wide). Off-cells
#    and mortar are TRANSPARENT so the shell only ADDS lit cells and never
#    occludes the glb facade. Hidden until f33; cell brightness ramps 32→42.
bpy.ops.mesh.primitive_cube_add(size=1, location=(25, 11, 49))
shell = bpy.context.active_object
shell.name = 'sr2_windows_shell'
shell.scale = (15.9, 15.9, 48.0)
bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
bvfx_emissive_windows(shell, window_color=(0.4, 0.8, 1.0),
                      strength=6.0, density=10, aspect=1.6, mortar=0.12)
shell.parent = O['sr2_tower']
shell.matrix_parent_inverse = O['sr2_tower'].matrix_world.inverted()

nt = M['sr2_windows_shell_windows'].node_tree
n, l = nt.nodes, nt.links
# finer pitch: ~0.79u columns, 1.25u rows
n['Math'].inputs[1].default_value = 1.26        # x scale
n['Math.005'].inputs[1].default_value = 0.80    # z scale
# per-cell random on/off mask (fixed procedural white noise — deterministic)
snap = n.new('ShaderNodeVectorMath'); snap.operation = 'SNAP'
snap.inputs[1].default_value = (0.794, 0.794, 1.25)
l.new(n['Texture Coordinate'].outputs['Object'], snap.inputs[0])
wn = n.new('ShaderNodeTexWhiteNoise'); wn.noise_dimensions = '3D'
l.new(snap.outputs['Vector'], wn.inputs['Vector'])
gt = n.new('ShaderNodeMath'); gt.operation = 'GREATER_THAN'
gt.inputs[1].default_value = 0.58               # ~42% of cells lit
l.new(wn.outputs['Value'], gt.inputs[0])
mul = n.new('ShaderNodeMath'); mul.operation = 'MULTIPLY'
l.new(n['Math.010'].outputs[0], mul.inputs[0])
l.new(gt.outputs[0], mul.inputs[1])
l.new(mul.outputs[0], n['Math.011'].inputs[0])
# off-cells transparent
trans = n.new('ShaderNodeBsdfTransparent')
mix = n.new('ShaderNodeMixShader')
l.new(mul.outputs[0], mix.inputs['Fac'])
l.new(trans.outputs['BSDF'], mix.inputs[1])
l.new(n['Emission'].outputs['Emission'], mix.inputs[2])
l.new(mix.outputs['Shader'], n['Material Output'].inputs['Surface'])
M['sr2_windows_shell_windows'].surface_render_method = 'BLENDED'
# keys: invisible at 32 (M3 exact), cells ramp on by 42
_key(n['Math.011'].inputs[1], 32, 0.0)
_key(n['Math.011'].inputs[1], 42, 0.75)
shell.hide_render = True
shell.keyframe_insert('hide_render', frame=32)
shell.hide_render = False
shell.keyframe_insert('hide_render', frame=33)

# ---------------------------------------------------------------------------
# 7) nebula wisps wrapping the upper third — faint purple-grey marbling the
#    crown glow blooms into. Faded in after f32.
bvfx_volume('neb_L', center=(-75, 50, 95), size=(170, 120, 55),
            optical_depth=0.35, color=(0.35, 0.2, 0.5), emission_strength=0.5,
            noise_scale=0.9, stretch=(2.2, 1.0, 0.6), edge_falloff=0.35)
bvfx_volume('neb_R', center=(85, 70, 112), size=(180, 130, 60),
            optical_depth=0.35, color=(0.35, 0.2, 0.5), emission_strength=0.5,
            noise_scale=0.9, stretch=(2.2, 1.0, 0.6), edge_falloff=0.35)
for nm, mn in (('neb_L', 'neb_L_vol'), ('neb_R', 'neb_R_vol')):
    ob = O[nm]
    ob.scale = (ob.scale[0] * 1.5, ob.scale[1] * 1.2, ob.scale[2] * 0.7)
    nt2 = M[mn].node_tree
    pv = nt2.nodes['Principled Volume']
    pv.inputs['Color'].default_value = (0.30, 0.23, 0.40, 1.0)
    pv.inputs['Emission Color'].default_value = (0.30, 0.23, 0.40, 1.0)
    _key(nt2.nodes['Math.012'].inputs[1], 32, 0.0)      # density mult
    _key(nt2.nodes['Math.012'].inputs[1], 44, 0.0035)
    _key(nt2.nodes['Math.013'].inputs[1], 32, 0.0)      # emission mult
    _key(nt2.nodes['Math.013'].inputs[1], 44, 0.02)

# ---------------------------------------------------------------------------
# 8) world tint: final grade a touch darker at the end
w = bpy.data.worlds[0].node_tree
_key(w.nodes['Background'].inputs[1], 32, 0.25)
_key(w.nodes['Background'].inputs[1], 48, 0.16)

# ---------------------------------------------------------------------------
# 9) camera: settle up slightly and dolly in as the dive finishes, so the
#    tower spans ~75% of frame height like the ref (f32 keyed at the
#    pre-existing evaluated values so M3's frame is unchanged). The faster
#    end-of-dive translation also keeps residual motion blur at f48.
rig = O['cam_rig']
rig.location.z = 25.61
rig.keyframe_insert('location', index=2, frame=32)
rig.location.z = 30.0
rig.keyframe_insert('location', index=2, frame=48)
rig.location.y = -173.91
rig.keyframe_insert('location', index=1, frame=32)
rig.location.y = -148.0
rig.keyframe_insert('location', index=1, frame=48)
