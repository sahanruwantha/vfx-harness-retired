# The five look materials + world the envelope drives, with the exact node names the
# recipe's LOOK table addresses. Node NAMES are the fragile part of this technique, so the
# spike scene reproduces them rather than papering over them with a lookup by type.
import bpy


def _tree_with(mat_name, node_specs):
    m = bpy.data.materials.new(mat_name); m.use_nodes = True
    t = m.node_tree; t.nodes.clear()
    out = t.nodes.new("ShaderNodeOutputMaterial")
    em = t.nodes.new("ShaderNodeEmission"); em.name = "Emission"
    t.links.new(em.outputs[0], out.inputs["Surface"])
    for n in node_specs:
        mn = t.nodes.new("ShaderNodeMath"); mn.name = n
    return m


_tree_with("city_far_mat", ())
_tree_with("street_mat", ())
_tree_with("cloud_dome_mat", ("dome_gain",))
_tree_with("sky_haze_vol", ("Math.013",))
_tree_with("tower_windows", ("Math.011",))

_w = bpy.data.worlds.new("sky_world"); _w.use_nodes = True
if "Background" not in _w.node_tree.nodes:
    _w.node_tree.nodes.new("ShaderNodeBackground").name = "Background"

SETTLE_START, SETTLE_END = 40, 48
