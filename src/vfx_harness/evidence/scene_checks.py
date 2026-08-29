"""Authoritative Blender-state and cross-layer interface contracts.

``scene_checks.json`` is a strict schema-2 document. Contracts address objects,
materials, shader controls and compositor nodes by semantic custom properties, never by
datablock names. Object selectors distinguish ``bvfx_role`` from ``bvfx_control`` so a
planner cannot put control ids in a role field and publish an unresolvable contract. Their
lifecycle decides which prior-layer guarantees remain active for the layer currently being
built.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from itertools import pairwise
from pathlib import Path

from PIL import Image, ImageChops, ImageStat

from vfx_harness.domain.contracts import active_for, load_document, validate_lifecycle
from vfx_harness.domain.semantic_roles import selector_punctuation_error

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
KNOWN_ROW_KEYS = frozenset({
    "id", "kind", "axis", "op", "lo", "hi", "value", "unit",
    "owner_layer", "fault_owner", "activates_at", "lifecycle", "expires_at",
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
FUNCTIONAL_KINDS = {"control_render_response", "frame_delta", "render_region_stat"}
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
PROJECTED_ORIGIN_KINDS = frozenset({"projected_origin_x", "projected_origin_y"})
SURFACE_PROJECTED_KINDS = BBOX_KINDS | {"visible_fraction"}
PROJECTED_CONTEXT_KINDS = BBOX_KINDS | PROJECTED_ORIGIN_KINDS
_PROJECTED_KINDS = SURFACE_PROJECTED_KINDS | PROJECTED_ORIGIN_KINDS
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
    "object_property": "numeric property read from every semantically selected object",
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
        "data_paths present and fails closed — it is not an unmeasurable binding defect"
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


def keyframe_schedule_path_aliases(path: str) -> tuple[str, ...]:
    """Object-level data_paths that satisfy one sample key.

    Camera ``location`` lives on the object. Light ``energy`` lives on the Light
    ID (object fcurve ``data.energy``, or data-block fcurve ``energy``). An explicit
    ``data.`` or custom-property path (``["energy"]``) stays exact — custom props
    are not a silent alias for RNA energy.
    """
    token = str(path or "").strip()
    if not token:
        return ()
    if token.startswith(("[", "data.")):
        return (token,)
    return (token, f"data.{token}")


def keyframe_schedule_matching_frames(
    *,
    object_paths: dict[str, set[int]],
    data_paths: dict[str, set[int]],
    sample_path: str,
) -> set[int]:
    """Frames keyed for ``sample_path`` on the object action and its data-block."""
    aliases = set(keyframe_schedule_path_aliases(sample_path))
    frames: set[int] = set()
    for data_path, keyed in object_paths.items():
        if data_path in aliases:
            frames |= set(keyed)
    for data_path, keyed in data_paths.items():
        if data_path in aliases or f"data.{data_path}" in aliases:
            frames |= set(keyed)
    return frames


def keyframe_schedule_present_paths(
    object_data_paths: list[str],
    datablock_data_paths: list[str],
) -> list[str]:
    """Inventory the schedule instrument will name on a miss (HIR-0018 both sides)."""
    out = [str(path) for path in object_data_paths]
    for path in datablock_data_paths:
        token = str(path)
        out.append(token if token.startswith("data.") else f"data.{token}")
    return out


def keyframe_schedule_miss_note(
    host: str,
    path: str,
    *,
    actual_frames: list[int] | set[int],
    expected_frames: list[int] | set[int],
    present_paths: list[str],
) -> str:
    aliases = list(keyframe_schedule_path_aliases(path))
    present = ", ".join(repr(item) for item in present_paths) if present_paths else "(none)"
    return (
        f"{host} {path!r} aliases {aliases}: keyframes "
        f"{sorted(actual_frames)} != {sorted(expected_frames)}; "
        f"fcurve data_paths present: {present}"
    )


def keyframe_schedule_path_miss_value(hi: float | None) -> float:
    """Numeric fail for a path/frame miss; always exceeds ``op: max`` ``hi``."""
    return float(hi or 0) + KEYFRAME_SCHEDULE_PATH_MISS_OVER_HI


def visible_fraction_min(
    role_fractions: Mapping[str, float], named_roles: Sequence[str]
) -> float:
    """AND across named roles: missing or empty is 0.0, not a pooled union."""
    if named_roles:
        return min(float(role_fractions.get(role, 0.0)) for role in named_roles)
    values = [float(value) for value in role_fractions.values()]
    return min(values) if values else 0.0


def visible_fraction_note(role_fractions: Mapping[str, float]) -> str:
    if not role_fractions:
        return ""
    parts = [
        f"{role}={float(frac):.6g}"
        for role, frac in sorted(role_fractions.items())
    ]
    return "per-role " + ", ".join(parts)


def _target(row: dict) -> str:
    op = row.get("op", "band")
    if op == "band":
        return f"{row.get('lo')}..{row.get('hi')}"
    if op == "eq":
        return f"= {row.get('value')} ± {row.get('tol', 0)}"
    if op == "min":
        return f">= {row.get('lo')}"
    if op == "max":
        return f"<= {row.get('hi')}"
    return str(op)


def _holds(row: dict, value, *, role_fractions: Mapping[str, float] | None = None) -> bool:
    if value is None:
        return False
    try:
        if str(row.get("kind")) == "visible_fraction":
            named = [str(item) for item in _selectors(row, "roles")]
            if named and isinstance(role_fractions, Mapping):
                lo = float(row.get("lo") or 0)
                if any(float(role_fractions.get(role, 0.0)) < lo for role in named):
                    return False
        value = float(value)
        if (
            str(row.get("kind")) == "path_clearance_min"
            and value >= PATH_CLEARANCE_UNMEASURED
        ):
            return False
        op = row.get("op", "band")
        if op == "band":
            return float(row["lo"]) <= value <= float(row["hi"])
        if op == "eq":
            return abs(value - float(row["value"])) <= float(row.get("tol", 0))
        if op == "min":
            return value >= float(row["lo"])
        if op == "max":
            return value <= float(row["hi"])
    except (TypeError, ValueError):
        return False
    return False


def _selectors(row: dict, key: str) -> list[str]:
    value = row.get(key)
    if isinstance(value, str):
        value = [value]
    return value if isinstance(value, list) else []


def validate_row(row: dict) -> str | None:
    if not isinstance(row, dict):
        return "record must be an object"
    if not row.get("id"):
        return "missing id"
    if any(key in row for key in ("objects", "materials", "nodes")):
        return "datablock-name selectors are removed; use semantic role selectors"
    punctuation = selector_punctuation_error(row)
    if punctuation:
        return punctuation
    life = validate_lifecycle(row)
    if life:
        return life
    kind = str(row.get("kind", ""))
    if kind not in SUPPORTED_KINDS:
        # Sessions authoring contracts are workspace-confined: this message is their
        # only route to the registry, and an unnamed enum invites invented kinds.
        return (
            f"unsupported kind {kind!r}; supported kinds: "
            + ", ".join(sorted(SUPPORTED_KINDS))
        )
    if kind in OBJECT_KINDS and not (
        _selectors(row, "roles") or _selectors(row, "control_roles")
    ):
        return "object contract requires non-empty roles or control_roles"
    if kind in MATERIAL_KINDS - {"material_assignment_fraction"} and not _selectors(row, "material_roles"):
        return "material contract requires non-empty material_roles"
    if kind == "material_assignment_fraction" and (
        not _selectors(row, "roles") or not _selectors(row, "material_roles")
    ):
        return "material_assignment_fraction requires roles and material_roles"
    if kind in NODE_KINDS:
        if row.get("graph") not in {"material", "compositor", "world"}:
            return "node contract graph must be material, compositor, or world"
        if row.get("graph") == "material" and not _selectors(row, "material_roles"):
            return "material node contract requires material_roles"
        if kind != "node_link_count" and not _selectors(row, "node_roles"):
            return f"{kind} requires node_roles"
    if kind == "control_render_response":
        if row.get("graph") not in {"material", "compositor", "world"}:
            return "control_render_response graph must be material, compositor, or world"
        if row.get("graph") == "material" and not _selectors(row, "material_roles"):
            return "material control response requires material_roles"
        if not _selectors(row, "node_roles"):
            return "control_render_response requires node_roles"
        values = row.get("probe_values")
        if not isinstance(values, list) or len(values) != 2:
            return "control_render_response requires two probe_values"
        region = row.get("region")
        if (
            not isinstance(region, list)
            or len(region) != 4
            or not all(isinstance(v, (int, float)) for v in region)
            or not all(0 <= float(v) <= 1 for v in region)
            or not (region[0] < region[2] and region[1] < region[3])
        ):
            return "control_render_response requires a normalized TOP-LEFT region"
        if row.get("response_metric", "mean_delta") not in {"mean_delta", "mae"}:
            return "control_render_response metric must be mean_delta or mae"
        if row.get("socket_direction", "auto") not in {"auto", "input", "output"}:
            return "control_render_response socket_direction must be auto, input, or output"
    if kind in WINDOW_KINDS | {"frame_delta"}:
        frames = row.get("frames")
        if (
            not isinstance(frames, list)
            or len(frames) != 2
            or any(isinstance(value, bool) or not isinstance(value, int) or value < 1 for value in frames)
            or frames[0] >= frames[1]
        ):
            return f"{kind} requires two increasing positive integer frames"
    if kind in TEMPORAL_KINDS and not (
        _selectors(row, "roles") or _selectors(row, "control_roles")
    ):
        return f"{kind} requires non-empty roles or control_roles"
    if kind == "keyframe_schedule":
        samples = row.get("samples")
        if not isinstance(samples, list) or len(samples) < 2:
            return "keyframe_schedule requires at least two samples"
        seen_frames: set[int] = set()
        paths: set[str] | None = None
        for index, sample in enumerate(samples):
            if not isinstance(sample, dict):
                return f"keyframe_schedule samples[{index}] must be an object"
            frame = sample.get("frame")
            if isinstance(frame, bool) or not isinstance(frame, int) or frame < 1:
                return f"keyframe_schedule samples[{index}].frame must be a positive integer"
            if frame in seen_frames:
                return "keyframe_schedule sample frames must be unique"
            seen_frames.add(frame)
            values = sample.get("values")
            if not isinstance(values, dict) or not values:
                return f"keyframe_schedule samples[{index}].values must be a non-empty object"
            sample_paths = set(values)
            if paths is None:
                paths = sample_paths
            elif sample_paths != paths:
                return "keyframe_schedule samples must declare the same property paths"
            for path, expected in values.items():
                if not isinstance(path, str) or not path.strip():
                    return "keyframe_schedule property paths must be non-empty strings"
                numeric = expected if isinstance(expected, list) else [expected]
                if not numeric or any(
                    isinstance(value, bool) or not isinstance(value, (int, float))
                    for value in numeric
                ):
                    return "keyframe_schedule values must be numeric scalars or vectors"
    unknown = sorted(set(row) - KNOWN_ROW_KEYS)
    if unknown:
        return (
            "unknown contract key(s) " + ", ".join(unknown)
            + " — the harness would ignore them silently; accepted keys are "
            + ", ".join(sorted(KNOWN_ROW_KEYS))
        )
    if kind in FRAME_SCOPED_KINDS:
        frame = row.get("frame")
        if isinstance(frame, bool) or not isinstance(frame, int) or frame < 1:
            return (
                f"{kind} must declare `frame` as a positive integer: this reading "
                "changes with the frame, and an undeclared frame silently measures "
                "frame 1"
            )
    if kind in _PROJECTED_KINDS:
        # the metric is intrinsically inside [0,1]; a bound outside the frame is
        # trivially satisfiable or unsatisfiable — a target, not a measurement
        # (run 20260824T153427Z-91b7c1 bound R4 to bbox_center_x with lo=-1.0)
        for bound_key in ("lo", "hi"):
            bound = row.get(bound_key)
            numeric = not isinstance(bound, bool) and isinstance(bound, (int, float))
            if numeric and not -0.25 <= float(bound) <= 1.25:
                return (
                    f"{kind} {bound_key}={bound} lies outside the normalized frame — "
                    "the metric can only read [0,1], so this target is vacuous. "
                    "Use a bound inside the frame, another kind, or record a "
                    "vocabulary-gap escalation"
                )
    if kind == "render_region_stat":
        region = row.get("region")
        if (
            not isinstance(region, list)
            or len(region) != 4
            or not all(isinstance(v, (int, float)) for v in region)
            or not all(0 <= float(v) <= 1 for v in region)
            or not (region[0] < region[2] and region[1] < region[3])
        ):
            return "render_region_stat requires a normalized TOP-LEFT region"
        if row.get("stat") not in {"mean", "stddev", "mean_r", "mean_g", "mean_b"}:
            return (
                "render_region_stat stat must be mean, stddev, or a channel mean "
                "(mean_r/mean_g/mean_b)"
            )
        lo, hi = row.get("lo"), row.get("hi")
        # the statistic lives in [0,255]; a bound outside it, or a floor at zero,
        # passes every frame ever rendered — an anchor that anchors nothing
        for bound in (lo, hi):
            if isinstance(bound, (int, float)) and not isinstance(bound, bool) and not 0 <= float(bound) <= 255:
                return "render_region_stat bounds live in [0,255]"
        if row.get("op") == "min" and isinstance(lo, (int, float)) and not isinstance(lo, bool) and float(lo) <= 0:
            return "render_region_stat min with lo<=0 passes any frame — vacuous"
        if row.get("op") == "max" and isinstance(hi, (int, float)) and not isinstance(hi, bool) and float(hi) >= 255:
            return "render_region_stat max with hi>=255 passes any frame — vacuous"
    if kind == "visible_fraction":
        lo, hi = row.get("lo"), row.get("hi")
        if row.get("op") == "min" and isinstance(lo, (int, float)) and not isinstance(lo, bool) and float(lo) <= 0:
            return "visible_fraction min with lo<=0 passes even when fully occluded — vacuous"
        if row.get("op") == "max" and isinstance(hi, (int, float)) and not isinstance(hi, bool) and float(hi) >= 1:
            return "visible_fraction max with hi>=1 passes even when fully visible — vacuous"
    if kind == "object_property":
        prop = str(row.get("property") or "")
        if not _MEASURED_PROPERTY.match(prop):
            return (
                f"object_property cannot certify {prop!r}: only Blender-evaluated "
                "properties are measurements. A custom property is written by the same "
                "builder the contract judges — self-certification. Use a measured kind "
                "(curve_derivative_max, path_clearance_min, keyframe_schedule, bbox_*) "
                "or record a vocabulary-gap escalation"
            )
    if kind == "onset_order":
        primary = set(_selectors(row, "roles")) | set(_selectors(row, "control_roles"))
        compare = set(_selectors(row, "compare_roles")) | set(
            _selectors(row, "compare_control_roles")
        )
        if not compare:
            return "onset_order requires compare_roles or compare_control_roles"
        # The metric is onset(compare) - onset(roles). Overlapping selectors compare a
        # set against itself, which is 0 by construction — a contract that can never
        # pass and never fails honestly. Run 20260823T154920Z burned a build on one.
        shared = sorted(primary & compare)
        if shared:
            return (
                "onset_order selectors must be disjoint; "
                + ", ".join(shared)
                + " appears on both sides, which forces the difference to 0 regardless "
                "of the scene"
            )
    if kind == "node_socket_value" and row.get("component") is not None:
        component = str(row.get("component")).upper().strip()
        if component not in {"0", "1", "2", "3", "R", "G", "B", "A"}:
            return (
                "node_socket_value component must be a channel index 0-3 or a letter "
                "R/G/B/A"
            )
    if kind == "transform_return_delta" and row.get("component", "location") not in {
        "location",
        "rotation",
        "scale",
    }:
        return "transform_return_delta component must be location, rotation, or scale"
    if kind == "curve_derivative_max" and row.get("property", "location") not in {
        "location",
        "rotation_euler",
        "scale",
    }:
        return "curve_derivative_max property must be location, rotation_euler, or scale"
    if kind in {"path_clearance_min", "parallax_displacement_profile"}:
        compare = set(_selectors(row, "compare_roles"))
        if not compare:
            return f"{kind} requires compare_roles naming the other side"
        primary = set(_selectors(row, "roles")) | set(_selectors(row, "control_roles"))
        shared = sorted(primary & compare)
        if shared:
            return (
                f"{kind} selectors must be disjoint; "
                + ", ".join(shared)
                + " appears on both sides, which measures the subject against itself"
            )
    if kind == "path_clearance_min":
        step = row.get("frame_step", 1)
        if isinstance(step, bool) or not isinstance(step, int) or step < 1:
            return "path_clearance_min frame_step must be a positive integer"
        lo, hi = row.get("lo"), row.get("hi")
        if row.get("op") == "min" and isinstance(lo, (int, float)) and not isinstance(lo, bool):
            if float(lo) <= 0:
                return "path_clearance_min min with lo<=0 passes any measured distance — vacuous"
            if float(lo) >= PATH_CLEARANCE_UNMEASURED:
                return (
                    "path_clearance_min lo at or above 1e9 is the empty-selection sentinel, "
                    "not a distance — vacuous"
                )
        if (
            row.get("op") == "max"
            and isinstance(hi, (int, float))
            and not isinstance(hi, bool)
            and float(hi) >= PATH_CLEARANCE_UNMEASURED
        ):
            return (
                "path_clearance_min max with hi>=1e9 passes any measured distance — vacuous"
            )
    if kind == "frame_delta":
        region = row.get("region")
        if region is not None and (
            not isinstance(region, list)
            or len(region) != 4
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in region)
            or not all(0 <= float(v) <= 1 for v in region)
            or not (region[0] < region[2] and region[1] < region[3])
        ):
            return "frame_delta region must be a normalized TOP-LEFT box"
    if kind == "node_socket_value" and (
        not row.get("socket") or row.get("direction", "input") not in {"input", "output"}
    ):
        return "node_socket_value requires socket and input/output direction"
    if row.get("socket_index") is not None and (
        not isinstance(row.get("socket_index"), int)
        or isinstance(row.get("socket_index"), bool)
        or row["socket_index"] < 0
    ):
        return "socket_index must be a non-negative integer"
    if kind == "node_link_count" and (not _selectors(row, "from_node_roles") or not _selectors(row, "to_node_roles")):
        return "node_link_count requires from_node_roles and to_node_roles"
    if kind == "object_property" and not row.get("property"):
        return "object_property requires a numeric property path"
    if kind == "animation_count" and row.get("domain", "all") not in {
        "all",
        "objects",
        "materials",
        "node_trees",
        "world",
        "scene",
    }:
        return "animation_count domain is invalid"
    op = str(row.get("op", "band"))
    if op not in SUPPORTED_OPS:
        return f"unsupported op {op!r}"
    try:
        if op == "band":
            if row.get("lo") is None or row.get("hi") is None:
                return "band requires lo and hi"
            if float(row["lo"]) > float(row["hi"]):
                return "band lo exceeds hi"
        elif op == "eq":
            float(row["value"])
            float(row.get("tol", 0))
        elif op == "min":
            float(row["lo"])
        else:
            float(row["hi"])
    except (KeyError, TypeError, ValueError):
        return f"{op} threshold must be numeric"
    return None


def _sample_vector(values: dict, property_name: str) -> tuple[float, ...] | None:
    raw = values.get(property_name)
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return (float(raw),)
    if (
        isinstance(raw, list)
        and raw
        and all(not isinstance(item, bool) and isinstance(item, (int, float)) for item in raw)
    ):
        return tuple(float(item) for item in raw)
    return None


def schedule_derivative_floor(schedule: dict, property_name: str) -> dict | None:
    """Worst consecutive-sample linear floor for one property (max-component, matching the probe).

    ``curve_derivative_max`` reads max(|Δcomponent|) per adjacent frame. LINEAR between
    two sealed keys is already that floor: no handle or extra key can go slower while
    still hitting both samples. Run ``20260826T170413Z-ba2b4c`` burned two repairs on
    f1 y=−30 → f24 y=140 (170/23 ≈ 7.39) against ``hi: 6.0``.
    """
    samples = schedule.get("samples")
    if not isinstance(samples, list) or len(samples) < 2:
        return None
    ordered = sorted(
        (row for row in samples if isinstance(row, dict) and isinstance(row.get("frame"), int)),
        key=lambda row: int(row["frame"]),
    )
    worst: dict | None = None
    for left, right in pairwise(ordered):
        frame_a, frame_b = int(left["frame"]), int(right["frame"])
        if frame_b <= frame_a:
            continue
        vec_a = _sample_vector(left.get("values") or {}, property_name)
        vec_b = _sample_vector(right.get("values") or {}, property_name)
        if vec_a is None or vec_b is None or len(vec_a) != len(vec_b):
            continue
        delta = max(abs(after - before) for before, after in zip(vec_a, vec_b, strict=True))
        floor = delta / (frame_b - frame_a)
        if worst is None or floor > float(worst["floor"]):
            worst = {
                "floor": floor,
                "frame_a": frame_a,
                "frame_b": frame_b,
                "delta": delta,
            }
    return worst


def _coalesce_adjacent(
    segments: list[tuple[int, int, float]],
) -> list[tuple[int, int, float, int]]:
    """Merge contiguous adjacent-frame pairs into ``(start, end, peak, pair_count)``."""
    if not segments:
        return []
    out: list[tuple[int, int, float, int]] = []
    start, end, peak = segments[0]
    count = 1
    for frame_a, frame_b, delta in segments[1:]:
        if frame_a == end:
            end = frame_b
            peak = max(peak, delta)
            count += 1
            continue
        out.append((start, end, peak, count))
        start, end, peak, count = frame_a, frame_b, delta, 1
    out.append((start, end, peak, count))
    return out


def _argmax_span(
    segments: list[tuple[int, int, float]],
) -> tuple[int, int, float] | None:
    """Longest coalesced span at the peak delta; ties prefer the earlier start."""
    if not segments:
        return None
    peak = max(item[2] for item in segments)
    at_peak = [item for item in segments if item[2] >= peak - 1e-9]
    start, end, delta, _count = max(
        _coalesce_adjacent(at_peak), key=lambda span: (span[3], -span[0])
    )
    return start, end, delta


def curve_derivative_note(
    segments: list[tuple[int, int, float]],
    *,
    hi: float | None = None,
    limit: int = 8,
) -> str:
    """Name the argmax adjacent-frame span and compact segments that already exceed ``hi``.

    Run ``20260826T170413Z-ba2b4c`` reported ``7.391312`` with an empty note. LINEAR
    interpolation makes every pair in f1→f24 the same max-component delta; coalescing
    those pairs is the measurement, not a first-pair accident. A scalar without its
    argmax is an estimate.
    """
    span = _argmax_span(segments)
    if span is None:
        return ""
    frame_a, frame_b, peak = span
    parts = [f"argmax f{frame_a}→f{frame_b} ({peak:.6g})"]
    if hi is None:
        return parts[0]
    over = [item for item in segments if item[2] > float(hi) + 1e-9]
    if not over:
        return parts[0]
    shown: list[str] = []
    spans = _coalesce_adjacent(over)
    for start, end, delta, count in spans[:limit]:
        piece = f"f{start}→f{end}={delta:.6g}"
        if count > 1:
            piece += f" ×{count}"
        shown.append(piece)
    extra = f" +{len(spans) - limit} more" if len(spans) > limit else ""
    parts.append("exceeds hi " + ", ".join(shown) + extra)
    return "; ".join(parts)


def _derivative_segments(raw) -> list[tuple[int, int, float]]:
    out: list[tuple[int, int, float]] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) != 3:
            continue
        try:
            start, end, delta = int(item[0]), int(item[1]), float(item[2])
        except (TypeError, ValueError):
            continue
        if isinstance(item[2], bool) or end <= start:
            continue
        out.append((start, end, delta))
    return out


def _optional_hi(row: dict) -> float | None:
    hi = row.get("hi")
    if isinstance(hi, bool) or not isinstance(hi, (int, float)):
        return None
    return float(hi)


def schedule_smoothness_contradictions(rows: list[dict]) -> list[dict[str, str]]:
    """Pair a schedule with a same-role derivative cap whose hi is below the linear floor."""
    schedules = [row for row in rows if isinstance(row, dict) and row.get("kind") == "keyframe_schedule"]
    derivatives = [row for row in rows if isinstance(row, dict) and row.get("kind") == "curve_derivative_max"]
    out: list[dict[str, str]] = []
    for deriv in derivatives:
        hi = deriv.get("hi")
        if isinstance(hi, bool) or not isinstance(hi, (int, float)):
            continue
        roles = tuple(sorted(str(role) for role in _selectors(deriv, "roles")))
        if not roles:
            continue
        prop = str(deriv.get("property") or "location")
        for schedule in schedules:
            if tuple(sorted(str(role) for role in _selectors(schedule, "roles"))) != roles:
                continue
            worst = schedule_derivative_floor(schedule, prop)
            if worst is None or float(worst["floor"]) <= float(hi) + 1e-9:
                continue
            schedule_id = str(schedule.get("id") or "<missing>")
            deriv_id = str(deriv.get("id") or "<missing>")
            span = int(worst["frame_b"]) - int(worst["frame_a"])
            out.append({
                "schedule_id": schedule_id,
                "smoothness_id": deriv_id,
                "message": (
                    f"{deriv_id}: hi {hi} is below the linear floor {worst['floor']:.6g} of "
                    f"{schedule_id} samples ({prop} Δ={worst['delta']:.6g} over frames "
                    f"{worst['frame_a']}→{worst['frame_b']}, {span} frames). Raise hi, widen "
                    "the span, or reduce Δ — interpolation cannot invent a third option"
                ),
            })
    return out


def validate_row_set(rows: list[dict]) -> list[str]:
    """Cross-row contradictions no single row can reveal.

    An auto-socket control_render_response demands a socket literally named 'Value'
    on THE one node its selector matches; a node_socket_value pinning the same
    (graph, node_roles) selector demands that node expose the pinned socket. Both
    published together in run 20260825 (world-bloom-response wanted 'Value' on the
    Glare its sibling pinned to 'Threshold' — CompositorNodeGlare exposes neither
    a 'Value' input nor output), and the contradiction only surfaced two builds
    and four repairs later. Explicitness costs one field; require it up front.

    A ``keyframe_schedule`` whose consecutive samples already exceed a same-role
    ``curve_derivative_max`` ``hi`` is the same class: the builder cannot satisfy
    both (run 20260826T170413Z-ba2b4c).
    """
    findings: list[str] = []
    def _selector(row: dict) -> tuple[str, tuple[str, ...]]:
        return (str(row.get("graph") or ""), tuple(sorted(_selectors(row, "node_roles"))))
    pinned: dict[tuple[str, tuple[str, ...]], list[str]] = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("kind") != "node_socket_value":
            continue
        if row.get("socket") or row.get("socket_index") is not None:
            pinned.setdefault(_selector(row), []).append(str(row.get("id") or "<missing>"))
    for row in rows:
        if not isinstance(row, dict) or row.get("kind") != "control_render_response":
            continue
        if row.get("socket") or row.get("socket_index") is not None:
            continue
        selector = _selector(row)
        if selector[1] and selector in pinned:
            findings.append(
                f"{row.get('id', '<missing>')}: auto-socket control_render_response shares "
                f"selector {list(selector[1])} (graph {selector[0]!r}) with socket-pinned "
                f"row(s) {pinned[selector]} — one node cannot be required to expose both a "
                "literal 'Value' socket and the pinned socket; declare 'socket' on this row"
            )
    findings.extend(row["message"] for row in schedule_smoothness_contradictions(rows))
    return findings


def _blender_probe(rows: list[dict], frame: int) -> str:
    payload = json.dumps(rows)
    return f"""\
import bpy, fnmatch, json, math
import checks as _checks  # worker sibling — the ONE projection implementation (ADR-0003)
_rows=json.loads({json.dumps(payload)}); _scene=bpy.context.scene; _FRAME={int(frame)}
_scene.frame_set(_FRAME); _camera=_scene.camera; _out=[]
def _p(row,key):
    v=row.get(key) or []
    return [v] if isinstance(v,str) else v
_m=_checks.match_semantic
def _objects(row):
    return sorted([o for o in _scene.objects
                   if (not _p(row,'roles') or _m(o.get('bvfx_role'),_p(row,'roles')))
                   and (not _p(row,'control_roles') or
                        _m(o.get('bvfx_control'),_p(row,'control_roles')))],key=lambda o:o.name)
def _materials(row):
    return sorted([m for m in bpy.data.materials
                   if _m(m.get('bvfx_role'),_p(row,'material_roles'))],key=lambda m:m.name)
def _nr(n): return n.get('bvfx_control') or n.get('bvfx_role') or ''
def _nm(n,pats):
    # Either-of, never precedence: with `control or role` a control-tagged node's ROLE
    # was unreachable by any selector — run 20260825 tagged AtmosphereVolume with both,
    # and its role selector silently resolved to the wrong node ("matched 1").
    return _m(n.get('bvfx_control'),pats) or _m(n.get('bvfx_role'),pats)
def _graphs(row):
    if row.get('graph')=='material': return [(m.name,m.node_tree) for m in _materials(row) if m.node_tree]
    if row.get('graph')=='compositor':
        ng=getattr(_scene,'compositing_node_group',None); return [('compositor',ng)] if ng else []
    nt=_scene.world.node_tree if _scene.world and _scene.world.use_nodes else None
    return [('world',nt)] if nt else []
def _projected(objects,dg):
    # Evaluated meshes are clipped against the camera frustum as EDGES before the
    # perspective divide (checks.frustum_union_ndc), so the union is the visible
    # portion and every coordinate is inside [0,1] by construction. The previous
    # vertex-projection accepted off-frustum blowup: a camera facing away from its
    # subject read bbox_height 1132.53608 "normalized" and PASSED >= 0.25
    # (run 20260824T103842Z-afec73). `points` also used to leak across iterations:
    # an object whose to_mesh() failed reused the PREVIOUS object's vertices.
    _V=__import__('mathutils').Vector
    _mvp=_checks.camera_clip_matrix(_scene,dg)
    clip=[]; edges=[]; base=0; empty=0
    for obj in objects:
        ev=obj.evaluated_get(dg); mesh=None; points=[]; pairs=[]
        if ev.type=='MESH':
            try:
                mesh=ev.to_mesh()
                points=[ev.matrix_world@v.co for v in mesh.vertices]
                pairs=[(e.vertices[0],e.vertices[1]) for e in mesh.edges]
            except Exception:
                points=[ev.matrix_world@_V(c) for c in ev.bound_box]; pairs=list(_checks.BOX_EDGES)
            finally:
                if mesh is not None: ev.to_mesh_clear()
        else:
            points=[ev.matrix_world@_V(c) for c in ev.bound_box]; pairs=list(_checks.BOX_EDGES)
        if not points: empty+=1
        clip.extend(tuple(_mvp@p.to_4d()) for p in points)
        edges.extend((base+a,base+b) for a,b in pairs)
        base+=len(points)
    return _checks.frustum_union_ndc(clip,edges),empty
def _property(target,path):
    value=target
    for token in str(path).split('.'):
        value=value[int(token)] if token.isdigit() else getattr(value,token)
    return float(value)
def _raw_property(target,path):
    value=target
    for token in str(path).split('.'):
        value=value[int(token)] if token.isdigit() else getattr(value,token)
    try: return tuple(float(v) for v in value)
    except TypeError: return float(value)
def _delta(actual,expected):
    if isinstance(expected,list):
        actual=tuple(actual)
        if len(actual)!=len(expected): raise ValueError('scheduled vector length differs')
        return max(abs(float(a)-float(b)) for a,b in zip(actual,expected))
    return abs(float(actual)-float(expected))
def _animated(v): return int(bool(getattr(v,'animation_data',None)))
def _fcurves(target):
    ad=getattr(target,'animation_data',None)
    if not ad or not ad.action: return []
    legacy=getattr(ad.action,'fcurves',None)
    if legacy and len(legacy): return list(legacy)
    out=[]; slot=getattr(ad,'action_slot',None)
    for layer in getattr(ad.action,'layers',[]):
        for strip in getattr(layer,'strips',[]):
            bags=[]
            if slot is not None and hasattr(strip,'channelbag'):
                try:
                    cb=strip.channelbag(slot)
                    if cb is not None: bags=[cb]
                except Exception: bags=[]
            if not bags: bags=list(getattr(strip,'channelbags',[]))
            for cb in bags: out.extend(cb.fcurves)
    return out
def _path_aliases(path):
    p=str(path)
    if p.startswith('[') or p.startswith('data.'):
        return (p,)
    return (p, 'data.'+p)
def _eval_property(target,path):
    last=None
    for alias in _path_aliases(path):
        try: return _raw_property(target, alias)
        except Exception as exc: last=exc
    raise last
def _host_fcurves(o):
    rows=[]
    for fc in _fcurves(o):
        rows.append(('object', fc.data_path, fc))
    data=getattr(o,'data',None)
    if data is not None:
        for fc in _fcurves(data):
            rows.append(('data', fc.data_path, fc))
    return rows
def _schedule_frames(o, path):
    aliases=set(_path_aliases(path)); frames=set()
    for kind, dp, fc in _host_fcurves(o):
        if kind=='object' and dp in aliases:
            frames.update(int(round(k.co.x)) for k in fc.keyframe_points)
        elif kind=='data' and (dp in aliases or ('data.'+dp) in aliases):
            frames.update(int(round(k.co.x)) for k in fc.keyframe_points)
    return frames
def _present_paths(o):
    out=[]
    for kind, dp, _fc in _host_fcurves(o):
        out.append(dp if kind=='object' else (dp if str(dp).startswith('data.') else 'data.'+dp))
    return out
def _state(items,frame):
    _scene.frame_set(int(frame)); dg=bpy.context.evaluated_depsgraph_get(); out=[]
    for item in items:
        ev=item.evaluated_get(dg)
        out.append((item.name,tuple(float(v) for row in ev.matrix_world for v in row),
                    bool(ev.hide_render),bool(ev.hide_viewport)))
    return out
def _onset(items,start,end,epsilon):
    base=_state(items,start)
    for frame in range(int(start)+1,int(end)+1):
        current=_state(items,frame)
        for before,after in zip(base,current):
            if before[0]!=after[0] or before[2:]!=after[2:]: return frame
            if max(abs(a-b) for a,b in zip(before[1],after[1]))>epsilon: return frame
    raise ValueError('selector has no evaluated transform/visibility onset in frame window')
def _transforms(items,frame):
    _scene.frame_set(int(frame)); dg=bpy.context.evaluated_depsgraph_get(); out={{}}
    for item in items:
        loc,rot,scale=item.evaluated_get(dg).matrix_world.decompose()
        out[item.name]=(loc.copy(),rot.copy(),scale.copy())
    return out
def _seen_tags(row):
    tags=[]
    for _g,_nt in _graphs(row):
        for n in _nt.nodes:
            tags += [str(n.get('bvfx_control') or ''), str(n.get('bvfx_role') or '')]
    return sorted(set(t for t in tags if t))[:16]
def _seen_object_roles():
    return sorted(set(str(o.get('bvfx_role')) for o in bpy.data.objects if o.get('bvfx_role')))[:24]
def _missobj(row):
    # A bare "matched no objects" left probing the live scene as the only way to learn
    # what WAS tagged; the miss must name both sides or every selector typo costs a session.
    return ('roles '+repr(_p(row,'roles'))+' / control_roles '+repr(_p(row,'control_roles'))+
            ' matched no objects; object roles present: '+(', '.join(_seen_object_roles()) or '(none)'))
for row in _rows:
    kind=row['kind']; value=None; error=''; note=''; segments=[]; role_fractions={{}}
    # Every row measures its DECLARED frame with its own depsgraph. Temporal kinds
    # (keyframe_schedule, onset_order, …) excurse to other frames and never restored
    # the batch frame, so every later row silently measured whatever frame the previous
    # row parked the scene at — run 20260824T103842Z-afec73 sealed frame-240 readings
    # as f1 and f36 evidence. Ambient shared state is not an instrument.
    _scene.frame_set(_FRAME)
    _row_dg=bpy.context.evaluated_depsgraph_get()
    objects=(
        _objects(row) if row.get('roles') or row.get('control_roles') else [])
    materials=_materials(row) if row.get('material_roles') else []; matched=[]
    try:
        if kind=='object_count':
            value=len(objects)
            if not objects and (row.get('roles') or row.get('control_roles')): note=_missobj(row)
        elif kind.startswith('bbox_'):
            if not objects: raise ValueError(_missobj(row))
            rec,empty=_projected(objects,_row_dg)
            if rec is None:
                raise ValueError(
                    f'none of {{len(objects)}} selected object(s) intersects the camera frustum '
                    f'at frame {{_FRAME}} ({{empty}} contributed no points)')
            x0,y0,x1,y1=rec['bbox']
            value={{'bbox_width':x1-x0,'bbox_height':y1-y0,'bbox_center_x':(x0+x1)/2,'bbox_center_y':(y0+y1)/2,'bbox_top_y':y0,'bbox_bottom_y':y1}}[kind]
        elif kind in ('projected_origin_x','projected_origin_y'):
            if not objects: raise ValueError(_missobj(row))
            if len(objects)!=1:
                raise ValueError(
                    f'{{kind}} requires exactly one selected object origin; matched '
                    f'{{len(objects)}} objects: '+', '.join(o.name for o in objects))
            if _scene.camera is None: raise ValueError('scene has no camera at the declared frame')
            point=objects[0].evaluated_get(_row_dg).matrix_world.translation.to_4d()
            clip=_checks.camera_clip_matrix(_scene,_row_dg)@point
            if clip.w<=0: raise ValueError('selected object origin is behind the active camera')
            nx=clip.x/clip.w; ny=clip.y/clip.w
            value=(nx+1.0)/2.0 if kind=='projected_origin_x' else (1.0-ny)/2.0
        elif kind=='visible_fraction':
            # Of EACH named role's ON-SCREEN surface samples, the fraction whose camera
            # ray reaches that role before anything else. A pooled union hid a failing
            # core behind passing rings (HIR-0051): the scalar is min(per-role), and a
            # named role with no samples reads 0.0. Zero on-screen samples is a failing
            # measurement, not an instrument error (HIR-0019).
            if not objects: raise ValueError(_missobj(row))
            if _scene.camera is None: raise ValueError('scene has no camera at the declared frame')
            def _vis_frac(sel):
                if not sel: return 0.0
                return _checks.surface_visible_fraction(
                    _scene,_row_dg,_scene.camera,sel)['visible_fraction']
            named=_p(row,'roles')
            if named:
                for role in named:
                    role_fractions[str(role)]=_vis_frac(_objects({{**row,'roles':[role]}}))
                value=min(role_fractions.values()) if role_fractions else 0.0
                note='per-role '+', '.join(r+'='+('%.6g'%role_fractions[r]) for r in named)
            else:
                value=_vis_frac(objects)
        elif kind=='mesh_vertex_count':
            value=sum(len(o.evaluated_get(_row_dg).data.vertices)
                      for o in objects if o.type=='MESH')
        elif kind=='smooth_fraction':
            ps=[p for o in objects if o.type=='MESH' for p in o.data.polygons]
            value=sum(1 for p in ps if p.use_smooth)/len(ps)
        elif kind=='radial_inward_fraction':
            tested=[]
            for o in objects:
                if o.type!='MESH': continue
                ev=o.evaluated_get(_row_dg); mw=ev.matrix_world
                for p in ev.data.polygons:
                    c=mw@p.center
                    n=(mw.to_3x3()@p.normal).normalized()
                    r=(c.x*c.x+c.y*c.y)**.5
                    if r>=1e-9 and abs(n.z)<=.9: tested.append((n.x*c.x+n.y*c.y)/r<=0)
            if not tested: raise ValueError('no radial faces to test on the selection')
            value=sum(tested)/len(tested)
        elif kind=='object_property':
            vs=[_property(o,row['property']) for o in objects]
            if not vs: raise ValueError(_missobj(row))
            if max(vs)-min(vs)>float(row.get('uniform_tol',1e-6)):
                raise ValueError('selected objects do not share one property value')
            value=sum(vs)/len(vs)
        elif kind=='material_count': value=len(materials)
        elif kind=='material_user_count': value=sum(m.users for m in materials)
        elif kind=='material_assignment_fraction':
            wanted=_p(row,'material_roles')
            if not objects: raise ValueError(_missobj(row))
            # only material-capable members are judged: an Empty marker counted as
            # "unassigned" makes the metric unsatisfiable over any mixed selection
            # (run 20260826: the dressed collective tier role includes layer 1's
            # Empties and the honest 3/3-mesh dressing read 0.5 forever)
            capable=[o for o in objects if hasattr(o.data,'materials') if o.data is not None]
            if not capable:
                raise ValueError('selected roles contain no material-capable objects '
                                 '(types: '+', '.join(sorted(set(o.type for o in objects)))+')')
            good=0
            for o in capable:
                assigned=[s.material for s in o.material_slots if s.material]
                good+=int(bool(assigned) and all(_m(m.get('bvfx_role'),wanted) for m in assigned))
            value=good/len(capable)
        elif kind in ('node_count','node_socket_value'):
            wanted=_p(row,'node_roles')
            for graph,nt in _graphs(row):
                for node in nt.nodes:
                    if _nm(node,wanted): matched.append((graph,node))
            if kind=='node_count':
                value=len(matched)
                if not matched and wanted:
                    note=('node_roles '+repr(wanted)+' matched 0 nodes in '+repr(row.get('graph'))+
                          ' graph(s); semantic tags present: '+(', '.join(_seen_tags(row)) or '(none)'))
            else:
                if len(matched)!=1:
                    raise ValueError('node_roles '+repr(wanted)+' matched '+str(len(matched))+' nodes'
                                     +' in '+repr(row.get('graph'))+' graph(s); semantic tags present: '
                                     +(', '.join(_seen_tags(row)) or '(none)'))
                node=matched[0][1]; sockets=node.inputs if row.get('direction','input')=='input' else node.outputs
                socket=(sockets[int(row['socket_index'])] if row.get('socket_index') is not None
                        else sockets.get(row['socket']))
                if socket is None:
                    raise ValueError('semantic node '+node.bl_idname+' has no requested socket '
                                     +repr(row.get('socket'))+'; available '
                                     +row.get('direction','input')+' sockets: '
                                     +(', '.join(s.name for s in sockets) or '(none)'))
                raw=socket.default_value; comp=row.get('component')
                # authors write channels as letters; run d2ea42 authored component 'B'
                # and int('B') killed the row as a binding defect
                if comp is not None:
                    comp={{'R':0,'G':1,'B':2,'A':3}}.get(str(comp).upper().strip(),comp)
                value=float(raw[int(comp)]) if comp is not None else float(raw)
        elif kind=='node_link_count':
            value=0
            for graph,nt in _graphs(row):
                for link in nt.links:
                    if (not _nm(link.from_node,_p(row,'from_node_roles'))
                            or not _nm(link.to_node,_p(row,'to_node_roles'))):
                        continue
                    if row.get('from_socket') and link.from_socket.name!=row['from_socket']: continue
                    if row.get('to_socket') and link.to_socket.name!=row['to_socket']: continue
                    value+=1
        elif kind=='compositor_enabled':
            ng=getattr(_scene,'compositing_node_group',None); wanted=_p(row,'node_group_roles')
            value=int(bool(_scene.render.use_compositing and ng and (not wanted or _m(ng.get('bvfx_role'),wanted))))
        elif kind=='animation_count':
            domain=row.get('domain','all'); values=[]
            if domain in ('all','objects'): values+=objects
            if domain in ('all','materials'): values+=materials
            if domain in ('all','node_trees'): values += [m.node_tree for m in materials if m.node_tree]
            if domain in ('all','world') and _scene.world: values += [_scene.world,_scene.world.node_tree]
            if domain in ('all','scene'): values += [_scene]
            value=sum(_animated(v) for v in values if v is not None)
        elif kind=='keyframe_schedule':
            if not objects: raise ValueError(_missobj(row))
            samples=row['samples']; expected_frames={{int(s['frame']) for s in samples}}
            paths=set(samples[0]['values']); deltas=[]; miss=[]
            miss_floor=float(row.get('hi') or 0)+1.0
            for o in objects:
                present=_present_paths(o)
                frames_ok=True
                for path in paths:
                    actual_frames=_schedule_frames(o, path)
                    if actual_frames!=expected_frames:
                        frames_ok=False
                        miss.append(
                            o.name+' '+repr(path)+' aliases '+repr(list(_path_aliases(path)))+
                            ': keyframes '+repr(sorted(actual_frames))+' != '+repr(sorted(expected_frames))+
                            '; fcurve data_paths present: '+
                            (', '.join(repr(p) for p in present) or '(none)'))
                for sample in samples:
                    _scene.frame_set(int(sample['frame'])); dg=bpy.context.evaluated_depsgraph_get()
                    ev=o.evaluated_get(dg)
                    for path,expected in sample['values'].items():
                        try:
                            deltas.append(_delta(_eval_property(ev,path),expected))
                        except Exception as exc:
                            frames_ok=False
                            miss.append(
                                o.name+' '+repr(path)+' unreadable: '+str(exc)[:160]+
                                '; fcurve data_paths present: '+
                                (', '.join(repr(p) for p in present) or '(none)'))
                            deltas.append(miss_floor)
                if not frames_ok:
                    deltas.append(miss_floor)
            if miss:
                note='; '.join(miss)[:400]
            value=max(deltas) if deltas else 0.0
        elif kind=='onset_order':
            other=_objects({{**row,
                'roles':_p(row,'compare_roles'),
                'control_roles':_p(row,'compare_control_roles')}})
            if not objects or not other: raise ValueError('onset selector matched no objects')
            a,b=row['frames']; epsilon=float(row.get('motion_epsilon',1e-5))
            value=_onset(other,a,b,epsilon)-_onset(objects,a,b,epsilon)
        elif kind=='radial_distance_trend':
            if not objects: raise ValueError(_missobj(row))
            a,b=row['frames']; samples=[]
            for f in range(int(a),int(b)+1):
                _scene.frame_set(int(f)); dg=bpy.context.evaluated_depsgraph_get()
                samples.append(sum((o.evaluated_get(dg).matrix_world.translation.x**2+
                                    o.evaluated_get(dg).matrix_world.translation.y**2)**.5
                                   for o in objects)/len(objects))
            xs=list(range(len(samples))); xm=sum(xs)/len(xs); ym=sum(samples)/len(samples)
            value=sum((x-xm)*(y-ym) for x,y in zip(xs,samples))/max(sum((x-xm)**2 for x in xs),1e-12)
        elif kind=='transform_return_delta':
            if not objects: raise ValueError(_missobj(row))
            a,b=row['frames']; first=_transforms(objects,a); second=_transforms(objects,b)
            component=row.get('component','location'); deltas=[]
            for name in first:
                if component=='location': deltas.append((second[name][0]-first[name][0]).length)
                elif component=='scale': deltas.append((second[name][2]-first[name][2]).length)
                else: deltas.append(first[name][1].rotation_difference(second[name][1]).angle)
            value=max(deltas)
        elif kind=='curve_derivative_max':
            if not objects: raise ValueError(_missobj(row))
            a,b=row['frames']; path=row.get('property') or 'location'
            deltas=[]; prev=None; prev_f=None
            for f in range(int(a),int(b)+1):
                _scene.frame_set(int(f)); dg=bpy.context.evaluated_depsgraph_get()
                cur=[_eval_property(o.evaluated_get(dg),path) for o in objects]
                if prev is not None:
                    pair=None
                    for before,after in zip(prev,cur):
                        bv=before if isinstance(before,tuple) else (before,)
                        av=after if isinstance(after,tuple) else (after,)
                        step=max(abs(x-y) for x,y in zip(av,bv))
                        pair=step if pair is None or step>pair else pair
                    if pair is not None:
                        deltas.append(pair)
                        segments.append([int(prev_f), int(f), pair])
                prev=cur; prev_f=f
            if not deltas: raise ValueError('frame window has no adjacent frame pair')
            value=max(deltas)
        elif kind=='path_clearance_min':
            if not objects: raise ValueError(_missobj(row))
            a,b=row['frames']; step=int(row.get('frame_step') or 1)
            obstacle_sel=_p(row,'compare_roles'); best=None; saw_obstacle=False
            for f in range(int(a),int(b)+1,step):
                _scene.frame_set(int(f)); dg=bpy.context.evaluated_depsgraph_get()
                obstacles=[o for o in _scene.objects
                           if o.type=='MESH' and _m(o.get('bvfx_role'),obstacle_sel)]
                if obstacles: saw_obstacle=True
                points=[o.evaluated_get(dg).matrix_world.translation.copy() for o in objects]
                for obstacle in obstacles:
                    ev=obstacle.evaluated_get(dg)
                    try: inverse=ev.matrix_world.inverted()
                    except Exception: continue
                    for point in points:
                        try: hit,local,_normal,_index=ev.closest_point_on_mesh(inverse@point)
                        except Exception: continue
                        if hit:
                            distance=((ev.matrix_world@local)-point).length
                            best=distance if best is None or distance<best else best
            if best is None:
                mesh_roles=sorted({{str(o.get('bvfx_role')) for o in _scene.objects
                    if o.type=='MESH' and o.get('bvfx_role')}})[:24]
                if not saw_obstacle:
                    raise ValueError(
                        'compare_roles '+repr(obstacle_sel)+
                        ' matched no mesh obstacles; mesh roles present: '+
                        (', '.join(mesh_roles) or '(none)')+
                        ' — empty obstacle selection is not clearance')
                raise ValueError(
                    'compare_roles '+repr(obstacle_sel)+
                    ' matched mesh obstacles but closest_point_on_mesh produced no distance')
            value=best
        elif kind=='parallax_displacement_profile':
            far_group=_objects({{**row,'roles':_p(row,'compare_roles'),'control_roles':[]}})
            if not objects or not far_group:
                raise ValueError('parallax selector matched no objects on one side')
            a,b=row['frames']
            def _centroid(items,f):
                _scene.frame_set(int(f)); dg=bpy.context.evaluated_depsgraph_get()
                mvp=_checks.camera_clip_matrix(_scene,dg); xs=[]; ys=[]
                for o in items:
                    v=mvp@o.evaluated_get(dg).matrix_world.translation.to_4d()
                    if v.w>1e-9: xs.append(v.x/v.w); ys.append(v.y/v.w)
                if not xs: raise ValueError('a parallax group has no object in front of the camera')
                return sum(xs)/len(xs), sum(ys)/len(ys)
            n1=_centroid(objects,a); n2=_centroid(objects,b)
            f1=_centroid(far_group,a); f2=_centroid(far_group,b)
            near_move=((n2[0]-n1[0])**2+(n2[1]-n1[1])**2)**.5
            far_move=((f2[0]-f1[0])**2+(f2[1]-f1[1])**2)**.5
            if near_move<1e-6 and far_move<1e-6:
                raise ValueError('neither group displaces on screen between the frames')
            value=1e9 if far_move<1e-6 else near_move/far_move
    # 400, not 160: the miss diagnostics carry the selector AND the tags present,
    # and a truncated enumeration reads as a complete one.
    except Exception as exc: value=None; error=str(exc)[:400]
    _out.append({{'id':row['id'],'value':value,'objects':[o.name for o in objects],
      'roles':[str(o.get('bvfx_role','')) for o in objects],'materials':[m.name for m in materials],
      'controls':[str(o.get('bvfx_control','')) for o in objects],
      'material_roles':[str(m.get('bvfx_role','')) for m in materials],
      'nodes':[n.name for g,n in matched],'error':error,'note':note,'segments':segments,
      'role_fractions':role_fractions}})
RESULT=_out
"""


def _evidence(rows: list[dict], raw: list[dict]) -> list[dict]:
    readings = {str(item.get("id")): item for item in raw if isinstance(item, dict)}
    out = []
    for row in rows:
        reading = readings.get(str(row.get("id")), {})
        error = validate_row(row) or str(reading.get("error") or "")
        value = reading.get("value")
        if (
            str(row.get("kind")) == "path_clearance_min"
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
            and float(value) >= PATH_CLEARANCE_UNMEASURED
        ):
            error = error or (
                "path_clearance_min empty obstacle selection is not clearance "
                "(the 1e9 sentinel never PASSes)"
            )
            value = None
        if isinstance(value, float):
            value = round(value, 6)
        note = str(reading.get("note") or "")
        extra: dict = {}
        if str(row.get("kind")) == "curve_derivative_max":
            parsed = _derivative_segments(reading.get("segments"))
            formatted = curve_derivative_note(parsed, hi=_optional_hi(row))
            if formatted:
                note = formatted
            span = _argmax_span(parsed)
            if span is not None:
                extra["argmax_frames"] = [span[0], span[1]]
                extra["argmax_delta"] = round(span[2], 6)
        role_fracs = reading.get("role_fractions")
        if (
            str(row.get("kind")) == "visible_fraction"
            and isinstance(role_fracs, dict)
            and role_fracs
        ):
            extra["role_fractions"] = {
                str(key): round(float(frac), 6) for key, frac in role_fracs.items()
            }
            if not note:
                note = visible_fraction_note(extra["role_fractions"])
        out.append(
            {
                "id": str(row.get("id") or "<missing>"),
                "axis": str(row.get("axis") or ""),
                "metric": str(row.get("kind") or "scene_contract"),
                "definition": KIND_DEFINITIONS.get(str(row.get("kind") or ""), ""),
                "value": value,
                "target": _target(row),
                "pass": not error and _holds(
                    row, value, role_fractions=extra.get("role_fractions")
                ),
                "note": note,
                "origin": "planner",
                "source": "interface_contract",
                "authoritative": True,
                "owner_layer": str(row.get("owner_layer") or ""),
                "fault_owner": str(row.get("fault_owner") or ""),
                "activates_at": str(row.get("activates_at") or ""),
                "lifecycle": str(row.get("lifecycle") or ""),
                "objects": list(reading.get("objects") or []),
                "roles": list(reading.get("roles") or []),
                "controls": list(reading.get("controls") or []),
                "materials": list(reading.get("materials") or []),
                "material_roles": list(reading.get("material_roles") or []),
                "nodes": list(reading.get("nodes") or []),
                **extra,
                **({"error": error} if error else {}),
            }
        )
    return out


def load_rows(shot_folder: str | Path) -> list[dict]:
    from vfx_harness.orchestration.plan_authority import selected_artifact_path

    return load_document(selected_artifact_path(shot_folder, "scene_checks.json"), "contracts")


def layer_evidence(shot_folder: str | Path, layer_id: str, *, frame: int, session) -> list[dict]:
    """Evaluate every lifecycle-active contract, including persistent prior interfaces."""
    from vfx_harness.orchestration.plan_authority import selected_artifact_path

    path = selected_artifact_path(shot_folder, "scene_checks.json")
    if not path.is_file():
        return []
    try:
        all_rows = load_rows(shot_folder)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return [
            {
                "id": "scene-contract-document",
                "axis": "",
                "metric": "schema",
                "value": None,
                "target": "schema=2",
                "pass": False,
                "origin": "planner",
                "source": "interface_contract",
                "authoritative": True,
                "error": str(exc)[:160],
            }
        ]
    rows = [r for r in all_rows if isinstance(r, dict) and active_for(r, layer_id, frame)]
    static = [r for r in rows if r.get("kind") not in FUNCTIONAL_KINDS]
    invalid = [r for r in static if validate_row(r)]
    runnable = [r for r in static if not validate_row(r)]
    raw = []
    if runnable:
        result = session.run(_blender_probe(runnable, frame), journal=False)
        raw = result.get("result") or []
        if not isinstance(raw, list):
            raw = []
    return _evidence(invalid, []) + _evidence(runnable, raw)


def _control_script(row: dict, value=None) -> str:
    spec = json.dumps(
        {
            key: row.get(key)
            for key in (
                "graph",
                "material_roles",
                "node_roles",
                "socket",
                "socket_index",
                "socket_direction",
            )
        }
    )
    return f"""\
import bpy, json
import checks as _checks
spec=json.loads({json.dumps(spec)})
match=_checks.match_semantic
graphs=[]
if spec['graph']=='material':
    mats=[m for m in bpy.data.materials if match(m.get('bvfx_role'),spec['material_roles'])]
    graphs=[m.node_tree for m in mats if m.node_tree]
elif spec['graph']=='compositor':
    ng=getattr(bpy.context.scene,'compositing_node_group',None); graphs=[ng] if ng else []
else:
    nt=bpy.context.scene.world.node_tree if bpy.context.scene.world and bpy.context.scene.world.use_nodes else None
    graphs=[nt] if nt else []
nodes=[n for nt in graphs for n in nt.nodes
       if match(n.get('bvfx_control'),spec['node_roles'])
       or match(n.get('bvfx_role'),spec['node_roles'])]
if len(nodes)!=1:
    seen=sorted(set(str(t) for nt in graphs for n in nt.nodes
                    for t in (n.get('bvfx_control'),n.get('bvfx_role')) if t))[:16]
    raise ValueError("semantic control "+repr(spec['node_roles'])+" matched "+str(len(nodes))
                     +" nodes in "+str(len(graphs))+" "+str(spec['graph'])+" graph(s);"
                     +" semantic tags present: "+(", ".join(seen) or "(none)"))
direction=spec.get('socket_direction') or 'auto'
collections=(
    [('input',nodes[0].inputs)] if direction=='input' else
    [('output',nodes[0].outputs)] if direction=='output' else
    [('input',nodes[0].inputs),('output',nodes[0].outputs)]
)
socket=None; resolved_direction=None
for candidate_direction,sockets in collections:
    try:
        candidate=(sockets[int(spec['socket_index'])] if spec.get('socket_index') is not None
                   else sockets.get(spec.get('socket') or 'Value'))
    except IndexError:
        candidate=None
    if candidate is not None:
        socket=candidate; resolved_direction=candidate_direction; break
if socket is None:
    raise ValueError("semantic control node "+nodes[0].bl_idname+" has no requested "+direction
                     +" socket "+repr(spec.get('socket') or 'Value')
                     +"; inputs="+repr([s.name for s in nodes[0].inputs][:12])
                     +" outputs="+repr([s.name for s in nodes[0].outputs][:12])
                     +". With no 'socket' declared the sweep resolves a socket literally named"
                     +" 'Value': tag a ShaderNodeValue that drives the target property,"
                     +" or declare 'socket' in the contract row.")
_tree=nodes[0].id_data
_ad=getattr(_tree,'animation_data',None)
_dpath=socket.path_from_id('default_value')
if _ad and any(d.data_path==_dpath for d in (_ad.drivers or [])):
    # A driver re-evaluates the socket every depsgraph update, so the sweep's write is
    # silently clobbered and the measured response is always 0.0 — run 20260825
    # (17581c) burned both repairs proving a beautiful frame-driven look that no
    # contract could ever measure. Fail loudly with the workable rig instead.
    raise ValueError("semantic control socket '"+socket.name+"' on "+nodes[0].name
                     +" is DRIVER-OWNED: the sweep writes default_value and the driver"
                     +" overwrites it at evaluation, so the measured response is always"
                     +" 0. Keep the swept control FREE (e.g. a tagged ShaderNodeValue)"
                     +" and combine it with the animated quantity via a Math node;"
                     +" drive the Math operand, never the tagged control itself.")
before=float(socket.default_value)
new={value!r}
if new is not None: socket.default_value=float(new)
RESULT={{'before':before,'after':float(socket.default_value),'node':nodes[0].name,
        'socket':socket.name,'socket_direction':resolved_direction}}
"""


def functional_evidence(
    shot_folder: str | Path, layer_id: str, *, session, rows: list[dict] | None = None
) -> list[dict]:
    """Render transactional control sweeps or deterministic two-frame deltas."""
    selected = (
        rows
        if rows is not None
        else [
            row
            for row in load_rows(shot_folder)
            if isinstance(row, dict)
            and row.get("kind") in FUNCTIONAL_KINDS
            and active_for(row, layer_id, int(row.get("frame", 1)))
        ]
    )
    out = []
    for row in selected:
        error = validate_row(row)
        value = None
        original = None
        try:
            if error:
                raise ValueError(error)
            images = []
            if row.get("kind") == "frame_delta":
                probe_values = row["frames"]
            elif row.get("kind") == "render_region_stat":
                probe_values = [row["frame"]]
            else:
                initial = session.run(_control_script(row), journal=False).get("result") or {}
                original = float(initial["before"])
                probe_values = row["probe_values"]
            for probe_value in probe_values:
                if row.get("kind") == "control_render_response":
                    session.run(_control_script(row, float(probe_value)), journal=False)
                    render_frame = int(row.get("frame", 1))
                else:
                    render_frame = int(probe_value)
                rendered = session.render_full(
                    frame=render_frame,
                    mode=str(row.get("probe_mode", "eevee")),
                    scale=float(row.get("probe_scale", 0.5)),
                )
                with Image.open(rendered["image_path"]) as source:
                    image = source.convert("RGB")
                    region = row.get("region")
                    if region:
                        x0, y0, x1, y1 = region
                        image = image.crop(
                            (
                                int(image.width * x0),
                                int(image.height * y0),
                                max(int(image.width * x0) + 1, int(image.width * x1)),
                                max(int(image.height * y0) + 1, int(image.height * y1)),
                            )
                        )
                    images.append(image)
            if row.get("kind") == "render_region_stat":
                stat_name = str(row.get("stat"))
                if stat_name in {"mean_r", "mean_g", "mean_b"}:
                    # hue is contractable: the luminance-only anchors passed a frame
                    # whose RGB spread was 15 against the ref's 57 (run af3084 —
                    # "bright but nearly colorless")
                    channel = {"mean_r": 0, "mean_g": 1, "mean_b": 2}[stat_name]
                    value = ImageStat.Stat(images[0].convert("RGB")).mean[channel]
                else:
                    stats = ImageStat.Stat(images[0].convert("L"))
                    value = stats.mean[0] if stat_name == "mean" else stats.stddev[0]
            elif row.get("kind") == "frame_delta" or row.get("response_metric", "mean_delta") == "mae":
                low, high = images
                if low.size != high.size:
                    raise ValueError("rendered frames have different dimensions")
                value = ImageStat.Stat(ImageChops.difference(low, high).convert("L")).mean[0]
            else:
                low, high = images
                low_mean = ImageStat.Stat(low.convert("L")).mean[0]
                high_mean = ImageStat.Stat(high.convert("L")).mean[0]
                value = high_mean - low_mean
        except Exception as exc:
            # 400, not 160: control-resolution misses enumerate the tags/sockets present,
            # and a truncated enumeration reads as a complete one.
            error = str(exc)[:400]
        finally:
            if original is not None:
                try:
                    session.run(_control_script(row, original), journal=False)
                except Exception as exc:
                    error = f"restore failed: {exc}"[:160]
        # The instrument itself is part of the reading: a failing 0.0 with no metric,
        # region, or frame named sent two builds hunting the control instead of the
        # measurement (run 17581c: mean_delta is luminance-only, so a hue-swap palette
        # control reads ~0; the row also measured the silent-default frame).
        if row.get("kind") == "render_region_stat":
            instrument = (
                f"measured as luminance {row.get('stat')} (0-255) over region "
                f"{row.get('region')} at frame {row.get('frame')}"
            )
        else:
            instrument = (
                f"measured as {row.get('response_metric', 'mean_delta')}"
                + (" (luminance-only: a pure hue shift reads ~0 — palette/tint semantics"
                   " need response_metric: mae)"
                   if row.get("kind") == "control_render_response"
                   and row.get("response_metric", "mean_delta") == "mean_delta"
                   else "")
                + f" over region {row.get('region')}"
                + (f" at frame {row.get('frame')}" if row.get("frame") is not None else "")
            )
        out.append(
            {
                "id": str(row.get("id") or "<missing>"),
                "axis": str(row.get("axis") or ""),
                "metric": str(row.get("kind") or "control_render_response"),
                "definition": KIND_DEFINITIONS[str(row.get("kind") or "control_render_response")],
                "value": round(value, 4) if isinstance(value, (int, float)) else None,
                "target": _target(row),
                "note": instrument,
                "pass": not error and _holds(row, value),
                "origin": "planner",
                "source": "interface_contract",
                "authoritative": True,
                "owner_layer": str(row.get("owner_layer") or ""),
                "fault_owner": str(row.get("fault_owner") or ""),
                "activates_at": str(row.get("activates_at") or ""),
                "lifecycle": str(row.get("lifecycle") or ""),
                **({"error": error} if error else {}),
            }
        )
    return out


def prior_interface_evidence(shot_folder: str | Path, layer_id: str, *, session) -> list[dict]:
    """Revalidate every active interface owned by an earlier layer before mutation.

    Contracts retain their own judge frame, so this cannot accidentally validate a rest
    transform at the current layer's primary action frame.  A failed prior interface is
    attributed to ``fault_owner`` and stops before a downstream builder is asked to work
    around corrupt input.
    """
    from vfx_harness.orchestration.plan_authority import selected_artifact_path

    path = selected_artifact_path(shot_folder, "scene_checks.json")
    if not path.is_file():
        return []
    rows = load_rows(shot_folder)
    current = int(layer_id)
    selected = [
        r
        for r in rows
        if isinstance(r, dict)
        and not validate_lifecycle(r)
        and int(r["owner_layer"]) < current
        and active_for(r, current)
    ]
    out = []
    by_frame: dict[int, list[dict]] = {}
    for row in selected:
        by_frame.setdefault(int(row.get("frame", 1)), []).append(row)
    for frame, frame_rows in sorted(by_frame.items()):
        functional = [r for r in frame_rows if r.get("kind") in FUNCTIONAL_KINDS]
        static = [r for r in frame_rows if r.get("kind") not in FUNCTIONAL_KINDS]
        invalid = [r for r in static if validate_row(r)]
        runnable = [r for r in static if not validate_row(r)]
        raw = []
        if runnable:
            result = session.run(_blender_probe(runnable, frame), journal=False)
            raw = result.get("result") or []
            if not isinstance(raw, list):
                raw = []
        out.extend(_evidence(invalid, []))
        out.extend(_evidence(runnable, raw))
        out.extend(functional_evidence(shot_folder, layer_id, session=session, rows=functional))
    return out
