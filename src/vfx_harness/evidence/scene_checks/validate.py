"""Authoritative Blender-state and cross-layer interface contracts.

``scene_checks.json`` is a strict schema-2 document. Contracts address objects,
materials, shader controls and compositor nodes by semantic custom properties, never by
datablock names. Object selectors distinguish ``bvfx_role`` from ``bvfx_control`` so a
planner cannot put control ids in a role field and publish an unresolvable contract. Their
lifecycle decides which prior-layer guarantees remain active for the layer currently being
built.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from itertools import pairwise

from vfx_harness.domain.contracts import validate_lifecycle
from vfx_harness.domain.evidence_kinds import PROJECTED_ORIGIN_KINDS as PROJECTED_ORIGIN_KINDS
from vfx_harness.domain.semantic_roles import selector_punctuation_error
from vfx_harness.evidence.scene_checks.kinds import (
    _MEASURED_PROPERTY,
    _PROJECTED_KINDS,
    FRAME_SCOPED_KINDS,
    KEYFRAME_SCHEDULE_PATH_MISS_OVER_HI,
    KNOWN_ROW_KEYS,
    MATERIAL_KINDS,
    NODE_KINDS,
    OBJECT_KINDS,
    PATH_CLEARANCE_UNMEASURED,
    SUPPORTED_KINDS,
    SUPPORTED_OPS,
    TEMPORAL_KINDS,
    VACUOUS_NORMALIZED_BAND_SPAN,
    WINDOW_KINDS,
)


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
        if row.get("op") == "band":
            lo, hi = row.get("lo"), row.get("hi")
            numeric = (
                not isinstance(lo, bool)
                and not isinstance(hi, bool)
                and isinstance(lo, (int, float))
                and isinstance(hi, (int, float))
            )
            if numeric and float(hi) - float(lo) > VACUOUS_NORMALIZED_BAND_SPAN:
                return (
                    f"{kind} band width {float(hi) - float(lo):g} covers more than half "
                    "the normalized frame — vacuous. Tighten lo/hi around the ref "
                    "measurement (HIR-0127)"
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

    def numeric(value: object) -> bool:
        return isinstance(value, (int, float)) and not isinstance(value, bool)

    if op == "band":
        if row.get("lo") is None or row.get("hi") is None:
            return 'op "band" requires numeric fields `lo` and `hi`'
        if not numeric(row.get("lo")) or not numeric(row.get("hi")):
            return 'op "band" requires numeric fields `lo` and `hi`'
        if float(row["lo"]) > float(row["hi"]):
            return "band lo exceeds hi"
    elif op == "eq":
        if not numeric(row.get("value")):
            return (
                'op "eq" requires numeric field `value` (and optional numeric `tol`); '
                "do not add an `eq` field"
            )
        if not numeric(row.get("tol", 0)):
            return 'op "eq" optional field `tol` must be numeric'
    elif op == "min":
        if not numeric(row.get("lo")):
            return 'op "min" requires numeric field `lo`'
    elif not numeric(row.get("hi")):
        return 'op "max" requires numeric field `hi`'
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
