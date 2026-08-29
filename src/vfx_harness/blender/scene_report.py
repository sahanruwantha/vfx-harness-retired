"""Pure scene-status projections used by Blender's typed inspection boundary."""

from __future__ import annotations


def render_status_lines(scene, stats: dict[str, int]) -> list[str]:
    render = scene.render
    lines = [
        f"render: engine={render.engine} res={render.resolution_x}x{render.resolution_y}"
        f"@{render.resolution_percentage}% frames={scene.frame_start}-{scene.frame_end}"
        f" fps={render.fps} motion_blur={render.use_motion_blur}"
        f" shutter={getattr(render, 'motion_blur_shutter', None)}"
        f" camera={scene.camera.name if scene.camera else None}",
        f"scene: objects={stats['objects']} mesh={stats['mesh_objects']} "
        f"verts={stats['verts']} tris={stats['tris']}",
    ]
    view = getattr(scene, "view_settings", None)
    display = getattr(scene, "display_settings", None)
    lines.append(
        "color: "
        f"display={getattr(display, 'display_device', None)} "
        f"transform={getattr(view, 'look', None)} "
        f"exposure={getattr(view, 'exposure', None)} "
        f"gamma={getattr(view, 'gamma', None)}"
    )
    comp = getattr(scene, "compositing_node_group", None)
    comp_nodes = list(getattr(comp, "nodes", []) or [])
    lines.append(
        "compositor: "
        f"group={getattr(comp, 'name', None)} enabled={bool(comp)} nodes={len(comp_nodes)} "
        f"types={','.join(sorted({node.bl_idname for node in comp_nodes})) or '-'}"
    )
    eevee = getattr(scene, "eevee", None)
    view_layers = list(getattr(scene, "view_layers", []) or [])
    layer = view_layers[0] if view_layers else None
    lines.append(
        "eevee: "
        f"volumetric_start={getattr(eevee, 'volumetric_start', None)} "
        f"volumetric_end={getattr(eevee, 'volumetric_end', None)} "
        f"volumetric_samples={getattr(eevee, 'volumetric_samples', None)} "
        f"volumetric_shadows={getattr(eevee, 'use_volumetric_shadows', None)} "
        f"taa_render_samples={getattr(eevee, 'taa_render_samples', None)} "
        f"vector_pass={getattr(layer, 'use_pass_vector', None)} "
        f"depth_pass={getattr(layer, 'use_pass_z', None)} "
        f"world={getattr(getattr(scene, 'world', None), 'name', None)}"
    )
    return lines


def light_status_line(obj) -> str:
    data = obj.data
    return (
        f"  {obj.name} role={obj.get('bvfx_role') or '-'!s} "
        f"owner={obj.get('bvfx_owner_layer') or '-'!s} type={data.type} "
        f"energy={round(float(data.energy), 4)} "
        f"color={tuple(round(float(value), 4) for value in data.color)} "
        f"shape={getattr(data, 'shape', None)} size={getattr(data, 'size', None)} "
        f"size_y={getattr(data, 'size_y', None)} "
        f"custom_distance={getattr(data, 'use_custom_distance', None)} "
        f"cutoff={getattr(data, 'cutoff_distance', None)} "
        f"hide_render={bool(obj.hide_render)} "
        f"loc={tuple(round(value, 3) for value in obj.location)}"
    )


def material_status_lines(scene, materials) -> list[str]:
    """Compact material inventory including the object slots that consume each row."""
    assignments: dict[str, list[str]] = {}
    for obj in scene.objects:
        for index, slot in enumerate(getattr(obj, "material_slots", ())):
            material = getattr(slot, "material", None)
            if material is not None:
                assignments.setdefault(material.name, []).append(f"{obj.name}[{index}]")
    rows = ["materials:"]
    material_rows = list(materials)
    if not material_rows:
        return [*rows, "  (none)"]
    for material in material_rows:
        users = ",".join(assignments.get(material.name, ())) or "-"
        rows.append(
            f"  {material.name} role={material.get('bvfx_role') or '-'!s} "
            f"owner={material.get('bvfx_owner_layer') or '-'!s} "
            f"nodes={bool(getattr(material, 'use_nodes', False))} objects={users}"
        )
    return rows
