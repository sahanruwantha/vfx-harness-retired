# blender-5-api is a reference card: its runnable blocks are the CORRECT forms of six
# APIs that moved. Each needs the thing it is a correct form OF.
import bpy

glare = bvfx_glare_bloom(threshold=0.6, size=0.55, strength=0.30)
sock = glare.inputs["Threshold"]          # a float socket for the menu-vs-float guard
nt = mat.node_tree
node = bsdf                               # the identity-vs-equality link walk
ramp = nt.nodes.new("ShaderNodeValToRGB")
cr = ramp.color_ramp

# something keyed, so all_fcurves/action_fcurves have a slotted action to walk
obj.location = (0.0, 0.0, 0.0)
obj.keyframe_insert("location", frame=1)
obj.location = (0.0, 0.0, 5.0)
obj.keyframe_insert("location", frame=24)
action = obj.animation_data.action
ad = obj.animation_data

SPIKE_ARGS["ad"] = ad
SPIKE_ARGS["action"] = action
SPIKE_ARGS["obj"] = obj
