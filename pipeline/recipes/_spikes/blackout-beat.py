# The shot the blackout is cut into: two emissive materials with the node names the recipe
# addresses, a compositor Glare, a plume mesh + its volume domain, and the three streaks the
# curtain keeps alive on the darkest frame.
import bpy

sc = bpy.context.scene


def _emissive(mat_name, node_name, strength):
    m = bpy.data.materials.new(mat_name); m.use_nodes = True
    t = m.node_tree; t.nodes.clear()
    out = t.nodes.new("ShaderNodeOutputMaterial")
    em = t.nodes.new("ShaderNodeEmission"); em.name = em.label = node_name
    em.inputs["Strength"].default_value = strength
    t.links.new(em.outputs[0], out.inputs["Surface"])
    return m


_emissive("street_mat", "Emission", 14.0)
_emissive("dot_mat", "em", 2.35)

_glare = bvfx_glare_bloom(threshold=0.6, size=0.55, strength=0.30)
_glare.id_data.name = "bvfx_compositor"      # the recipe looks it up by group name
_glare.name = "Glare"

for _n in ("plume_cloud", "plume_glow", "streak_0", "streak_1", "streak_2"):
    _me = bpy.data.meshes.new(_n)
    _me.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], [], [[0, 1, 2, 3]])
    _o = bpy.data.objects.new(_n, _me)
    sc.collection.objects.link(_o)

SPIKE_ARGS["sock"] = bpy.data.materials["street_mat"].node_tree.nodes["Emission"].inputs[1]
SPIKE_ARGS["base"] = 14.0
SPIKE_ARGS["seq"] = ((14, 1.0), (20, 0.01), (24, 1.0))
SPIKE_ARGS["keep"] = {"streak_0", "streak_1", "streak_2"}
SPIKE_ARGS["ad"] = bpy.data.materials["street_mat"].node_tree.animation_data
