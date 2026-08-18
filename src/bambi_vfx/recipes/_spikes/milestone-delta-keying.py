# A mid-shot scene: the haze volume the delta dims, the emissive mesh it hides, and a
# path material that ALREADY carries keys on the exact (data_path, array_index) curves
# set_keys() retargets — without them its `assert hit` is the thing under test.
import bpy

sc = bpy.context.scene


def _emissive(name, em_name="Emission"):
    m = bpy.data.materials.new(name); m.use_nodes = True
    t = m.node_tree; t.nodes.clear()
    out = t.nodes.new("ShaderNodeOutputMaterial")
    em = t.nodes.new("ShaderNodeEmission"); em.name = em_name
    t.links.new(em.outputs[0], out.inputs["Surface"])
    return m


_haze = _emissive("horizon_haze_vol", "bvfx_dim_horizon_haze_vol_em")
# the settle-assertion block samples nodes['Emission'] on whatever tree is current; a real
# volume material carries one alongside the named dim node.
_haze.node_tree.nodes.new("ShaderNodeEmission").name = "Emission"

_me = bpy.data.meshes.new("seam_lights")
_me.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], [], [[0, 1, 2, 3]])
sc.collection.objects.link(bpy.data.objects.new("seam_lights", _me))

_gold = _emissive("path_gold")
_em = _gold.node_tree.nodes["Emission"]
for _f, _s in ((340, 20.0), (372, 40.0), (462, 30.0)):
    _em.inputs[1].default_value = _s
    _em.inputs[1].keyframe_insert("default_value", frame=_f)
for _f, _g in ((340, 0.5), (374, 0.7), (462, 0.5)):
    _em.inputs[0].default_value = (1.0, _g, 0.2, 1.0)
    _em.inputs[0].keyframe_insert("default_value", frame=_f)

# the settle assertion samples `nt` and the scene camera
nt = _gold.node_tree
HOLD_START, LAST_FRAME = 462, 470

SPIKE_ARGS["ad"] = _gold.node_tree.animation_data     # linearise() needs real curves
