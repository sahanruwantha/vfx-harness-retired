"""Compositor bloom — the post-process glow the reference's look is carried by.

The traced Silk Road run showed the builder could never reach the reference's "heavy bloom" by
tuning lights, because bloom is a *post* effect, not a lighting setting: EEVEE Next dropped the
built-in bloom, so it now lives in the compositor as a Glare node. Blender 5.x reworked the
compositor into a node group (``scene.compositing_node_group``) whose settings are input sockets
(``Type``/``Threshold``/``Size``/``Strength``) and whose result flows out a Group Output — this
emits that wiring so every emissive tower window and city light blooms like the reference.
"""

from __future__ import annotations


def bloom_compositor_code(
    *,
    glare_type: str = "Bloom",
    threshold: float = 0.7,
    size: float = 0.7,
    strength: float = 0.5,
) -> str:
    """bpy that wires a Glare (bloom) node into the scene's compositor node group.

    ``threshold`` — only pixels brighter than this bloom (lower = more glow). ``size`` 0-1 = glow
    radius. ``strength`` = glow intensity. Idempotent: reuses the compositor group and clears its
    nodes, so re-applying across panel variants never stacks.
    """
    return f"""
import bpy
scene = bpy.context.scene
scene.use_nodes = True  # REQUIRED — without it the compositor group is ignored on render
group = scene.compositing_node_group
if group is None:
    group = bpy.data.node_groups.new("Compositor", type='CompositorNodeTree')
    scene.compositing_node_group = group
for node in list(group.nodes):
    group.nodes.remove(node)
if not any(getattr(s, 'in_out', '') == 'OUTPUT' for s in group.interface.items_tree):
    group.interface.new_socket("Image", in_out='OUTPUT', socket_type='NodeSocketColor')
output = group.nodes.new('NodeGroupOutput')
layers = group.nodes.new('CompositorNodeRLayers')
glare = group.nodes.new('CompositorNodeGlare')
try:
    glare.inputs['Type'].default_value = {glare_type!r}
except Exception:
    pass
for _name, _value in (('Threshold', {threshold}), ('Size', {size}), ('Strength', {strength})):
    if _name in glare.inputs:
        try:
            glare.inputs[_name].default_value = _value
        except Exception:
            pass
group.links.new(layers.outputs['Image'], glare.inputs['Image'])
group.links.new(glare.outputs['Image'], output.inputs['Image'])
result = {{"bloom": True, "glare_type": {glare_type!r}, "threshold": {threshold}}}
"""


def comp_graph_code(
    *,
    glare_type: str = "Bloom",
    threshold: float = 0.75,
    size: float = 0.75,
    strength: float = 0.6,
    exposure: float = 0.0,
    bright: float = 0.0,
    contrast: float = 8.0,
) -> str:
    """A real finishing comp: Render Layers -> Glare(Bloom) -> Bright/Contrast -> Exposure -> output.

    The genuine post chain the reference look needs — bloom on the emissives plus a grade (crush with
    contrast, tune exposure). Same 5.x node-group wiring as :func:`bloom_compositor_code`
    (``use_nodes=True`` REQUIRED; output via a Group Output socket), extended with grade nodes. The
    comp department tunes these params to the reference; this is the reliable graph they build on.
    """
    return f"""
import bpy
scene = bpy.context.scene
scene.use_nodes = True  # REQUIRED — without it the compositor group is ignored on render
group = scene.compositing_node_group
if group is None:
    group = bpy.data.node_groups.new("Compositor", type='CompositorNodeTree')
    scene.compositing_node_group = group
for node in list(group.nodes):
    group.nodes.remove(node)
if not any(getattr(s, 'in_out', '') == 'OUTPUT' for s in group.interface.items_tree):
    group.interface.new_socket("Image", in_out='OUTPUT', socket_type='NodeSocketColor')
output = group.nodes.new('NodeGroupOutput')
layers = group.nodes.new('CompositorNodeRLayers')
glare = group.nodes.new('CompositorNodeGlare')
try:
    glare.inputs['Type'].default_value = {glare_type!r}
except Exception:
    pass
for _n, _v in (('Threshold', {threshold}), ('Size', {size}), ('Strength', {strength})):
    if _n in glare.inputs:
        try: glare.inputs[_n].default_value = _v
        except Exception: pass

_node = glare
bc = group.nodes.new('CompositorNodeBrightContrast')
for _n, _v in (('Bright', {bright}), ('Contrast', {contrast})):
    if _n in bc.inputs:
        try: bc.inputs[_n].default_value = _v
        except Exception: pass
group.links.new(_node.outputs['Image'], bc.inputs['Image']); _node = bc

_has_exp = False
try:
    exp = group.nodes.new('CompositorNodeExposure')
    if 'Exposure' in exp.inputs:
        exp.inputs['Exposure'].default_value = {exposure}
    group.links.new(_node.outputs['Image'], exp.inputs['Image']); _node = exp; _has_exp = True
except Exception:
    _has_exp = False

group.links.new(layers.outputs['Image'], glare.inputs['Image'])
group.links.new(_node.outputs['Image'], output.inputs['Image'])
result = {{"comp": True, "glare_type": {glare_type!r}, "graded": True, "has_exposure": _has_exp}}
"""
