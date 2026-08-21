# Three stand-in objects for the proxy -> hero handoff.
import bpy

def _cube(name):
    me = bpy.data.meshes.new(name)
    me.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], [], [[0, 1, 2, 3]])
    o = bpy.data.objects.new(name, me)
    bpy.context.scene.collection.objects.link(o)
    return o

proxy_a_body = _cube("proxy_a_body")
proxy_a_plume = _cube("proxy_a_plume")
proxy_b = _cube("proxy_b")

SPIKE_ARGS["objects_visible_before"] = [proxy_a_body, proxy_a_plume]
SPIKE_ARGS["objects_visible_after"] = [proxy_b]
