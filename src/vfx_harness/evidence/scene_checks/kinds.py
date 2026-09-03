"""Authoritative Blender-state and cross-layer interface contracts.

``scene_checks.json`` is a strict schema-2 document. Contracts address objects,
materials, shader controls and compositor nodes by semantic custom properties, never by
datablock names. Object selectors distinguish ``bvfx_role`` from ``bvfx_control`` so a
planner cannot put control ids in a role field and publish an unresolvable contract. Their
lifecycle decides which prior-layer guarantees remain active for the layer currently being
built.
"""

from __future__ import annotations

import re

from vfx_harness.domain.contracts import LIFECYCLE_ROW_KEYS
from vfx_harness.domain.evidence_kinds import FUNCTIONAL_KINDS as _FUNCTIONAL_KINDS
from vfx_harness.domain.evidence_kinds import PROJECTED_ORIGIN_KINDS as PROJECTED_ORIGIN_KINDS

OBJECT_KINDS = {
    "bbox_width",
    "bbox_height",
    "bbox_center_x",
    "bbox_center_y",
    "bbox_top_y",
    "bbox_bottom_y",
    "projected_origin_x",
    "projected_origin_y",
    "object_count",
    "mesh_vertex_count",
    "smooth_fraction",
    "radial_inward_fraction",
    "object_property",
    "visible_fraction",
}
MATERIAL_KINDS = {"material_count", "material_user_count", "material_assignment_fraction"}
NODE_KINDS = {"node_count", "node_socket_value", "node_link_count"}
STATE_KINDS = {"animation_count", "compositor_enabled"}
TEMPORAL_KINDS = {
    "keyframe_schedule",
    "onset_order",
    "radial_distance_trend",
    "transform_return_delta",
    "curve_derivative_max",
    "path_clearance_min",
    "parallax_displacement_profile",
}
WINDOW_KINDS = TEMPORAL_KINDS - {"keyframe_schedule"}
# Empty compare_roles used to report 1e9 and PASS every min-bound ("vacuously clear").
# That sealed collision contracts against nothing. The sentinel is not a distance.
PATH_CLEARANCE_UNMEASURED = 1e9
# A keyframe_schedule path/frame miss must fail op:max even when evaluated values
# accidentally match the samples (static RNA, drivers, custom props on another path).
KEYFRAME_SCHEDULE_PATH_MISS_OVER_HI = 1.0
# Every key the evaluator, gate, and orchestration actually read off a contract row.
# A row carrying anything else is not "extra metadata" — it is a claim the harness
# silently ignores. Run 20260823T154920Z shipped `at_frame: 36`, nothing read it,
# `row.get("frame", 1)` defaulted to 1, and a sealed frame-1 reading was reported as a
# frame-36 retraction failure for a whole build. Unknown keys now fail closed.
KNOWN_ROW_KEYS = LIFECYCLE_ROW_KEYS | frozenset({
    "id", "kind", "axis", "op", "lo", "hi", "value", "unit",
    "decision_id", "frame", "frames", "frame_step", "region", "component", "samples",
    "motion_epsilon", "property", "tol", "uniform_tol", "direction", "domain",
    "graph", "socket", "socket_index", "socket_direction", "from_socket", "to_socket",
    "probe_mode", "probe_scale", "probe_values", "response_metric", "stat",
    "roles", "control_roles", "material_roles", "compare_roles",
    "compare_control_roles", "node_roles", "node_group_roles",
    "from_node_roles", "to_node_roles",
})
# Scene state these kinds read changes with the frame, so a row that does not say
# WHICH frame it reads silently measures frame 1 via the historical default.
FRAME_SCOPED_KINDS = {
    "bbox_width",
    "bbox_height",
    "bbox_center_x",
    "bbox_center_y",
    "bbox_top_y",
    "bbox_bottom_y",
    "projected_origin_x",
    "projected_origin_y",
    "mesh_vertex_count",
    "radial_inward_fraction",
    "object_property",
    "visible_fraction",
    # a control sweep RENDERS at a frame; run 20260825 (17581c) authored a palette
    # response with no frame, the silent f1 default measured a subject occluded at f1,
    # and two build attempts burned four repairs on a structurally-0.0 reading
    "control_render_response",
    "render_region_stat",
}
FUNCTIONAL_KINDS = set(_FUNCTIONAL_KINDS)
SUPPORTED_KINDS = (
    OBJECT_KINDS | MATERIAL_KINDS | NODE_KINDS | STATE_KINDS | TEMPORAL_KINDS | FUNCTIONAL_KINDS
)
# What each metric can honestly certify. Counting the rim modules proves they EXIST; it
# cannot prove they chase, because a count has no time in it. Run 20260823T154920Z
# certified a chase claim with `object_count` and a "reads as layered machined metal"
# claim with radial closure — both metrics measured correctly, and neither could support
# the claim it was bound to.
BBOX_KINDS = frozenset({
    "bbox_width", "bbox_height", "bbox_center_x",
    "bbox_center_y", "bbox_top_y", "bbox_bottom_y",
})
SURFACE_PROJECTED_KINDS = BBOX_KINDS | {"visible_fraction"}
PROJECTED_CONTEXT_KINDS = BBOX_KINDS | PROJECTED_ORIGIN_KINDS
_PROJECTED_KINDS = SURFACE_PROJECTED_KINDS | PROJECTED_ORIGIN_KINDS
# Inclusive width greater than half the normalized frame is "somewhere on screen"
# (Room 1046 cam-aim 0.2–0.8 sealed a nadir camera). Vis already fails lo<=0.
VACUOUS_NORMALIZED_BAND_SPAN = 0.5
SUBJECT_COMPOSITION_RULE = (
    "a projected_composition owner covers each judge frame with bbox_* of a rendered "
    "subject, not projected_origin of a camera-only host. When that subject does not "
    "exist yet, the camera layer authors the bbox with activates_at equal to the "
    "compiled earliest_geometry_layer, lifecycle persistent, and fault_owner on the "
    "camera owner layer; "
    "the camera unit binds the ids through composition_context and does not seal them"
)
# These instruments cannot produce a reading without ``scene.camera``.  Keep the
# capability beside the canonical metric registry so planning and execution do not
# maintain divergent guesses about which evidence needs a camera.  Functional kinds
# render a frame; parallax and the projected kinds call the camera projection helpers.
CAMERA_REQUIRED_KINDS = frozenset(
    _PROJECTED_KINDS | FUNCTIONAL_KINDS | {"parallax_displacement_profile"}
)
KIND_DOMAINS: dict[str, str] = {
    **dict.fromkeys(TEMPORAL_KINDS, "temporal"),
    **dict.fromkeys(_PROJECTED_KINDS, "projected_composition"),
    **dict.fromkeys(FUNCTIONAL_KINDS, "image"),
    **dict.fromkeys(
        (OBJECT_KINDS - _PROJECTED_KINDS) | MATERIAL_KINDS | NODE_KINDS | STATE_KINDS,
        "scene",
    ),
    # screen-space displacement is a projected claim even though it samples two frames
    "parallax_displacement_profile": "projected_composition",
}
SUPPORTED_OPS = {"band", "eq", "min", "max"}
OPERATOR_FIELDS = {
    "band": {
        "required": ["lo", "hi"],
        "description": "numeric inclusive lower and upper thresholds",
    },
    "eq": {
        "required": ["value"],
        "optional": ["tol"],
        "description": (
            "numeric equality target in `value` with optional numeric tolerance in "
            "`tol`; there is no `eq` field"
        ),
    },
    "min": {
        "required": ["lo"],
        "description": "numeric lower threshold",
    },
    "max": {
        "required": ["hi"],
        "description": "numeric upper threshold",
    },
}

# object_property may only certify properties Blender itself evaluates. A custom
# property is written by the builder that the contract judges — self-certification
# (run 20260824T153427Z-91b7c1 bound R29 to an invented `clearance_min_distance`).
_MEASURED_PROPERTY = re.compile(
    r"^(?:(?:delta_)?location|(?:delta_)?rotation_euler|scale|dimensions)(?:\.\d+)?$"
    r"|^data\.(?:lens|angle|clip_start|clip_end|energy|size|sensor_width|sensor_height|ortho_scale)$"
    # data.users is Blender-maintained datablock reference counting — read-only to
    # builders and the honest instancing proof (shared mesh data => users > 1). Two
    # materializer sessions escalated its absence as a vocabulary gap.
    r"|^data\.users$"
    r"|^(?:hide_render|hide_viewport)$"
)

KIND_DEFINITIONS = {
    "bbox_width": "projected union width in normalized camera coordinates",
    "bbox_height": "projected union height in normalized camera coordinates",
    "bbox_center_x": "projected union horizontal centre; 0=left, 1=right",
    "bbox_center_y": "projected union vertical centre; 0=top, 1=bottom",
    "bbox_top_y": "top of projected union; normalized top-left coordinates",
    "bbox_bottom_y": "bottom of projected union; normalized top-left coordinates",
    "projected_origin_x": (
        "horizontal normalized camera projection of exactly one selected object's world "
        "origin; accepts Empty/control hosts and proves camera alignment, not rendered "
        "visibility; its repair owner is the camera provider"
    ),
    "projected_origin_y": (
        "vertical normalized camera projection of exactly one selected object's world "
        "origin; accepts Empty/control hosts and proves camera alignment, not rendered "
        "visibility; its repair owner is the camera provider"
    ),
    "object_count": "number of objects whose bvfx_role matches roles",
    "mesh_vertex_count": "evaluated mesh vertex total across matched object roles",
    "smooth_fraction": "fraction of matched mesh polygons using smooth shading",
    "radial_inward_fraction": "fraction where radial XY normal dot face centre <= 0 (inward)",
    "object_property": (
        "numeric property read from every semantically selected object; a `data.*` "
        "property is read from every selected host that owns a data-block, hosts with no "
        "data-block (Empties, control markers) are typed out and named in the note, and a "
        "selection with no data-block host fails closed"
    ),
    "visible_fraction": (
        "of each named role's ON-SCREEN surface samples at the declared frame, the "
        "fraction whose camera ray reaches that role's surface before any other object; "
        "the scalar is the min across named roles (logical AND — a union cannot hide a "
        "failing subject). Reads 0.0 when nothing of a named role is on screen. "
        "Occlusion truth — projection-only bbox rows pass straight through an occluder"
    ),
    "material_count": "number of materials whose bvfx_role matches material_roles",
    "material_user_count": "total Blender users of matched semantic materials",
    "material_assignment_fraction": "fraction of selected objects assigned a matching material role",
    "node_count": "number of shader/compositor nodes matching node_roles",
    "node_socket_value": "numeric socket value on one semantic shader/compositor node",
    "node_link_count": "number of links between semantic nodes and optional sockets",
    "animation_count": "animation datablocks on the selected semantic state",
    "compositor_enabled": "1 when compositing and a semantic compositor group exist",
    "control_render_response": "pixel response when a semantic numeric control is swept low to high",
    "render_region_stat": (
        "one absolute luminance statistic (mean or stddev, 0-255) of the rendered frame's "
        "declared region. The exposure anchor: every relative metric (responses, deltas, "
        "socket values) passes at any brightness, and run 20260825 sealed four lookdev "
        "units over a composed frame reading mean 11/stddev 1.2 against refs at 32-81/28-64 "
        "— measurably lit machinery, visually a dead plate. Copy targets from measure_ref"
    ),
    "onset_order": (
        "comparison-role onset frame minus selected-role onset frame; positive means selected roles start first"
    ),
    "radial_distance_trend": "least-squares slope of mean XY distance from origin across a frame window",
    "transform_return_delta": "selected transform-component delta between two declared frames",
    "keyframe_schedule": (
        "maximum property error against an exact semantic keyframe schedule; any missing or "
        "extra keyed frame fails the contract. Sample path P matches object P, object "
        "`data.P`, or the data-block P fcurve; a path miss names those aliases and the "
        "data_paths present and fails closed — it is not an unmeasurable binding defect. "
        "A data-block path is judged on every selected host that owns a data-block; "
        "hosts with none (a rig's Empty pivot) are typed out and named, and a selection "
        "with no data-block host fails closed"
    ),
    "frame_delta": "mean absolute rendered-pixel delta between two declared frames",
    "curve_derivative_max": (
        "maximum per-frame evaluated change of one transform property across the whole "
        "frame window; bounds smoothness where endpoint deltas cannot. The evidence note "
        "names the argmax adjacent-frame pair and every segment that exceeds hi"
    ),
    "path_clearance_min": (
        "minimum distance from the selected objects' evaluated origins to compare_roles "
        "mesh surfaces across the frame window; an empty obstacle selection is not a "
        "measurement (fail closed). Persistent lifecycle re-evaluates the same row as "
        "obstacle geometry arrives — it does not make absence a PASS"
    ),
    "parallax_displacement_profile": (
        "screen-space displacement of the selected group's centroid divided by the "
        "compare_roles group's, between the two declared frames; >1 means the selected "
        "group visibly moves more (near-ground parallax)"
    ),
}
