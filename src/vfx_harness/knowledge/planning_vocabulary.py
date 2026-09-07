"""Authoritative planning descriptions generated from the VFX evidence registry."""

from vfx_harness.evidence.scene_checks import (
    FRAME_SCOPED_KINDS,
    KIND_DEFINITIONS,
    KIND_DOMAINS,
    OPERATOR_FIELDS,
    SUPPORTED_KINDS,
    WINDOW_KINDS,
)


def evidence_vocabulary() -> dict:

    extra_fields = {
        "keyframe_schedule": [
            "samples: [{frame, values:{property: scalar|vector}}, …] (≥2, unique frames)"
        ],
        "object_property": [
            "frame",
            "property (Blender-evaluated path only — custom properties are "
            "self-certification and rejected; vector components use numeric paths "
            "such as location.2 or rotation_euler.1, not location.z)",
        ],
        "path_clearance_min": [
            "frames [a,b]",
            "compare_roles (obstacle roles, disjoint from roles)",
            "frame_step (optional)",
            "empty compare_roles match is not clearance (fail closed, not 1e9)",
        ],
        "parallax_displacement_profile": [
            "frames [a,b]",
            "compare_roles (far group, disjoint from roles)",
        ],
        "curve_derivative_max": ["frames [a,b]", "property (location|rotation_euler|scale)"],
        "onset_order": ["frames [a,b]", "compare_roles/compare_control_roles (disjoint)"],
        "transform_return_delta": ["frames [a,b]", "component (location|rotation|scale)"],
        "control_render_response": [
            "graph", "node_roles", "probe_values [lo,hi]", "region [x0,y0,x1,y1]",
            "frame (the render frame the sweep measures — the subject must be "
            "VISIBLE there; pair with a visible_fraction row)",
            "response_metric (mean_delta is luminance-only and reads ~0 for pure "
            "hue/tint shifts — palette semantics need mae)",
            "socket/socket_index (optional — otherwise resolution needs a socket "
            "literally named 'Value': the control tag belongs on a ShaderNodeValue, "
            "and the tagged control must stay FREE of drivers)",
        ],
        "frame_delta": ["frames [a,b]", "region (optional)"],
        "node_socket_value": [
            "graph (material|compositor|world)",
            "node_roles",
            "socket (name) or socket_index",
            "direction (input|output)",
            "component (optional, for vector sockets: channel index 0-3 or R/G/B/A)",
        ],
        "node_count": ["graph (material|compositor|world)", "node_roles"],
        "render_region_stat": [
            "stat (mean|stddev luminance, or mean_r/mean_g/mean_b channel means, "
            "all 0-255)",
            "region [x0,y0,x1,y1]",
            "op min/max/band with targets copied from measure_ref's reading of the "
            "judge reference — THE exposure anchor: every relative metric passes at "
            "any brightness, and luminance-only anchors pass a colorless frame "
            "(express 'amber' as mean_r above mean_b via two rows)",
        ],
        "visible_fraction": [
            "roles (the surfaces this judge frame is judged ON — occluders need no "
            "declaration, any closer surface counts)",
            "op min lo≈0.2–0.5 for must-be-seen; op max hi<1 for not-yet-revealed",
        ],
        "projected_origin_x": [
            "roles/control_roles selecting exactly one object (Empty/control is legal)",
            "op min/max/band in normalized camera coordinates; camera-alignment only, "
            "not visibility or subject composition coverage; repair_owner must provide "
            "camera; a band wider than half the frame is vacuous",
        ],
        "projected_origin_y": [
            "roles/control_roles selecting exactly one object (Empty/control is legal)",
            "op min/max/band in normalized top-left camera coordinates; camera-alignment "
            "only; repair_owner must provide camera; a band wider than half the frame "
            "is vacuous",
        ],
        "node_link_count": [
            "graph", "from_node_roles", "to_node_roles",
            "from_socket/to_socket (optional)",
        ],
    }
    entries = {}
    for kind in sorted(SUPPORTED_KINDS):
        fields = []
        if kind in WINDOW_KINDS and kind not in extra_fields:
            fields.append("frames [a,b]")
        if kind in FRAME_SCOPED_KINDS and kind != "object_property":
            fields.append("frame")
        fields.extend(extra_fields.get(kind, []))
        entries[kind] = {
            "definition": KIND_DEFINITIONS.get(kind, ""),
            "domain": KIND_DOMAINS.get(kind, "scene"),
            "fields": fields,
        }
    note = (
        "Projected bbox_* and projected_origin_* targets must lie inside the normalized frame; "
        "a band whose width is greater than half that frame is vacuous. "
        "bbox/visible_fraction require rendered surfaces, while projected_origin_* is "
        "the camera-owner alignment instrument for Empty/control hosts and does not "
        "cover subject composition. When the subject does not exist yet, author bbox_* "
        "with owner_layer on the camera layer, activates_at on the earliest geometry "
        "layer, lifecycle persistent, and fault_owner on the camera owner. "
        "The control "
        "producer proves fixed world state with scene evidence and publishes a typed "
        "placement_control; the camera successor depends on it, declares the exact "
        "consume, and owns projection without mutating the observed selector. "
        "path_clearance_min fails closed on an empty obstacle selection — persistent "
        "lifecycle re-evaluates as geometry arrives, it does not make absence a PASS."
    )
    return {"kinds": entries, "operators": OPERATOR_FIELDS, "note": note}
