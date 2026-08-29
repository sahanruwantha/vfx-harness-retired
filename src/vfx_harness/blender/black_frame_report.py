"""Typed scene-state diagnosis attached to nearly black beauty renders."""

from __future__ import annotations

import ast
import contextlib
import math
from collections.abc import Sequence
from typing import Any


def _number(value: float) -> str:
    return format(float(value), ".6g")


def same_density(left: float, right: float) -> bool:
    """Compare authored Python floats with Blender's float32 socket read-back."""
    return math.isclose(float(left), float(right), rel_tol=1e-6, abs_tol=1e-9)


def effective_volume_span(
    camera_clip_end: float | None,
    volumetric_start: float | None,
    volumetric_end: float | None,
) -> float | None:
    """Canonical sampled depth available to a World-volume diagnosis."""
    if camera_clip_end is None:
        return None
    span = float(camera_clip_end)
    if volumetric_end is not None:
        span = min(
            span,
            max(0.0, float(volumetric_end) - float(volumetric_start or 0.0)),
        )
    return span


def format_black_frame_context(
    *,
    frame: int,
    camera_clip_end: float | None,
    volume_rows: Sequence[dict[str, Any]],
    background_strength: float | None,
    lights: Sequence[dict[str, Any]],
    volumetric_start: float | None = None,
    volumetric_end: float | None = None,
    subjects: Sequence[dict[str, Any]] = (),
) -> str:
    """Name likely mechanical causes and the next measurement, without guessing."""
    lines = ["BLACK-FRAME SCENE CAUSE CARD (typed state):"]
    if camera_clip_end is not None:
        lines.append(f"  camera clip_end={_number(camera_clip_end)}")
    if volumetric_start is not None and volumetric_end is not None:
        lines.append(
            "  eevee volumetric range="
            f"{_number(volumetric_start)}..{_number(volumetric_end)}"
        )
    if background_strength is not None:
        lines.append(f"  world Background.Strength={_number(background_strength)}")

    unlinked: list[dict[str, Any]] = []
    for row in volume_rows:
        density = row.get("density")
        density_text = "linked" if row.get("linked") else _number(float(density or 0.0))
        lines.append(
            f"  world volume {row.get('name')!r} role={row.get('role') or '-'} "
            f"Density={density_text}"
        )
        if not row.get("linked") and density is not None and float(density) > 0:
            unlinked.append(row)

    for row in lights:
        lines.append(
            f"  light {row.get('name')!r} role={row.get('role') or '-'} "
            f"energy={_number(float(row.get('energy') or 0.0))} "
            f"camera_distance={_number(float(row.get('camera_distance') or 0.0))}"
        )

    if volumetric_end is not None and subjects:
        outside = [
            row
            for row in subjects
            if float(row.get("camera_distance") or 0.0) > float(volumetric_end)
        ]
        if outside:
            rendered = ", ".join(
                f"{row.get('role') or row.get('name') or '?'}@"
                f"{_number(float(row.get('camera_distance') or 0.0))}"
                for row in outside[:6]
            )
            lines.append(
                f"  subjects beyond volumetric_end: {rendered} "
                "(renderer depth coverage, not a density target)"
            )

    if unlinked and camera_clip_end:
        row = max(unlinked, key=lambda item: float(item.get("density") or 0.0))
        density = float(row["density"])
        effective_span = effective_volume_span(
            camera_clip_end, volumetric_start, volumetric_end
        ) or 0.0
        optical_scale = density * effective_span
        lines.append(
            "  extinction scale: Density×effective_volume_span="
            + _number(optical_scale)
            + f" (span={_number(effective_span)}; a contract ceiling is not a calibrated target)"
        )
        role = str(row.get("role") or "").strip()
        if role and optical_scale >= 1.0:
            values = sorted({0.0, 1e-6, 1e-5, 1e-4, 1e-3, density})
            rendered = ", ".join(_number(value) for value in values)
            lines.append(
                "  NEXT MEASUREMENT: probe_control(graph='world', node_role="
                f"{role!r}, socket='Density', values=[{rendered}], frame={int(frame)}, "
                "mode='draft'). Measure density before another light-energy or placement guess."
            )
    elif not volume_rows:
        lines.append("  no active World volume node found")
    return "\n".join(lines)


def authored_density_values(script: str) -> tuple[float, ...]:
    """Literal World-density values a free-form mutation proposes."""
    try:
        tree = ast.parse(script)
    except SyntaxError:
        return ()
    assignments = sorted(
        (node for node in ast.walk(tree) if isinstance(node, (ast.Assign, ast.AnnAssign))),
        key=lambda node: getattr(node, "lineno", 0),
    )
    world_derived: set[str] = set()

    def names_in(node: ast.AST) -> set[str]:
        return {item.id for item in ast.walk(node) if isinstance(item, ast.Name)}

    def mentions_world(node: ast.AST) -> bool:
        return any(
            isinstance(item, ast.Attribute) and item.attr == "world"
            for item in ast.walk(node)
        ) or bool(names_in(node) & world_derived)

    # A tiny forward provenance pass is enough for the authored patterns the tool
    # supports: w=scene.world; nt=w.node_tree; node=nt.nodes[...]. Unknown provenance
    # abstains instead of classifying every material Density socket as World density.
    for assignment in assignments:
        targets = assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
        if not mentions_world(assignment.value):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                world_derived.add(target.id)

    values: list[float] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value_node = node.value
            for target in targets:
                if not isinstance(target, ast.Attribute) or target.attr != "default_value":
                    continue
                subscript = target.value
                if not isinstance(subscript, ast.Subscript):
                    continue
                try:
                    key = ast.literal_eval(subscript.slice)
                    value = float(ast.literal_eval(value_node))
                except (ValueError, TypeError):
                    continue
                if key == "Density" and mentions_world(subscript):
                    values.append(value)
        if not isinstance(node, ast.Call):
            continue
        name = node.func.id if isinstance(node.func, ast.Name) else ""
        if name != "bvfx_volumetric_world":
            continue
        for keyword in node.keywords:
            if keyword.arg != "density":
                continue
            with contextlib.suppress(ValueError, TypeError):
                values.append(float(ast.literal_eval(keyword.value)))
    return tuple(values)


def summarize_density_probe(rows: Sequence[dict[str, Any]]) -> str:
    """Turn a numeric density sweep into a bounded causal routing statement."""
    if not rows:
        return ""
    candidate_peak = max(float(row.get("mean") or 0.0) for row in rows)
    reference_peak = max(float(row.get("ref_mean") or 0.0) for row in rows)
    if reference_peak > 10.0 and candidate_peak < max(3.0, reference_peak * 0.1):
        return (
            "DENSITY HYPOTHESIS CLOSED: every tested density remained near-black "
            f"(brightest mean {candidate_peak:.2f}/255 vs reference "
            f"{reference_peak:.2f}/255). Do not repeat this sweep. Density is not the "
            "dominant missing signal; keep the required energy schedule intact and "
            "measure local-light contribution/coverage or placement next."
        )
    spread = max(float(row.get("mean") or 0.0) for row in rows) - min(
        float(row.get("mean") or 0.0) for row in rows
    )
    return (
        "DENSITY SWEEP READBACK: candidate mean changed by "
        f"{spread:.2f}/255 across the tested range. Select only a measured value using "
        "the owned image contracts; the sweep has been restored to its original value."
    )


def summarize_black_placement_search(samples: Sequence[dict[str, Any]]) -> str:
    """Close repeated coordinate guessing after measured near-black non-progress.

    This is a bounded-search policy, not a claim that continuous 3D space was
    exhaustively searched. Once three genuinely different placement scales retain a
    near-black beauty, another free-form coordinate guess is not earned judgment.
    """
    usable: list[dict[str, float]] = []
    for row in samples:
        try:
            distance = float(row["camera_distance"])
            mean = float(row["mean"])
            black_pct = float(row["black_pct"])
        except (KeyError, TypeError, ValueError):
            continue
        if not all(math.isfinite(value) for value in (distance, mean, black_pct)):
            continue
        if distance <= 0 or mean > 3.0 or black_pct < 85.0:
            continue
        if any(
            math.isclose(distance, prior["camera_distance"], rel_tol=0.05)
            for prior in usable
        ):
            continue
        usable.append({
            "camera_distance": distance,
            "mean": mean,
            "black_pct": black_pct,
        })

    if len(usable) < 3:
        return ""
    distances = [row["camera_distance"] for row in usable]
    lo, hi = min(distances), max(distances)
    if hi / lo < 4.0:
        return ""
    return (
        "LIGHT-PLACEMENT SEARCH CLOSED: "
        f"{len(usable)} distinct placements spanning camera distance {lo:.3g}–{hi:.3g} "
        "all remained near-black (mean <=3/255, black >=85%). Another free-form "
        "coordinate guess is not a new causal experiment. Use a different declared "
        "control, or call cannot_express_in_scope with the failing contract ids."
    )
