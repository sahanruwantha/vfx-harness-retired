"""Agent SDK tools over a warm BlenderSession.

`build_blender_tools(session)` returns an in-process MCP server and the list of
qualified tool names to put in `ClaudeAgentOptions.allowed_tools`. Render tools
return image content blocks, so the agent literally sees the frames it makes.
"""

from __future__ import annotations

import ast
import contextlib
from pathlib import Path

from PIL import Image

from vfx_harness.blender.black_frame_report import same_density
from vfx_harness.blender.tools.images import _b64, _metrics_line, _stats
from vfx_harness.blender.tools.reports import _DISPLAY_H, _METRIC_H, _SHEET_MAX_W
from vfx_harness.evidence.compare_panels import mark_pair
from vfx_harness.evidence.image_payment_inputs import (
    _candidate_for_proposed_check as _candidate_for_proposed_check,
)
from vfx_harness.evidence.metrics import compare as _mcompare
from vfx_harness.evidence.metrics import look_pair as _lp


def _to_metric_size(im: Image.Image) -> Image.Image:
    """One canonical geometry for measurement, independent of render scale.

    Downscale only. Upscaling invents no detail but redistributes it, which is what made
    a render read softer than the reference it was being compared against.
    """
    w = max(1, round(im.width * _METRIC_H / im.height))
    return im.resize((w, _METRIC_H), Image.LANCZOS)


def _to_display_size(im: Image.Image) -> Image.Image:
    w = max(1, round(im.width * _DISPLAY_H / im.height))
    return im.resize((w, _DISPLAY_H), Image.LANCZOS)


def _compare_image(cand_path: str, ref_path: Path, caption: str, *, feedback_groups=None) -> dict:
    cand_raw = Image.open(cand_path).convert("RGB")
    ref_raw = Image.open(ref_path).convert("RGB")

    # MEASURE on identically-resampled copies…
    cand_m, ref_m = _to_metric_size(cand_raw), _to_metric_size(ref_raw)
    groups = (
        set(feedback_groups)
        if feedback_groups is not None
        else {"exposure", "detail", "emitters", "halation", "color", "motion"}
    )
    text = (
        f"{caption}\n{_stats(cand_m, feedback_groups=groups)}\n{_metrics_line(cand_m, ref_m, feedback_groups=groups)}"
    )

    # …and state the SIGNED GAP, not just the two numbers. The builder was reading its own
    # absolute values ("exposure: mean 22/255") and having to remember or re-derive the
    # reference's, so it searched instead of solving: across one layer f440's mean went
    # 22 → 74 → 44, overshooting and correcting, ~10 compare calls in 19 minutes. The full
    # signed, banded delta already existed in metrics.compare() and was used by the
    # acceptance stage — the tool the builder actually calls dozens of times per layer
    # simply never called it.
    try:

        metric_group = {
            "exposure_mean": "exposure",
            "clipped_pct": "exposure",
            "black_pct": "exposure",
            "points": "emitters",
            "points_top": "emitters",
            "points_mid": "emitters",
            "points_bot": "emitters",
            "hot_core": "emitters",
            "chroma_spread": "color",
            "halation": "halation",
            "halation_top": "halation",
            "halation_mid": "halation",
            "halation_bot": "halation",
            "streak_continuity": "motion",
        }
        deltas = [d for d in _mcompare(*_lp(cand_path, str(ref_path))) if metric_group.get(d.key, "detail") in groups]
        if deltas:
            text += "\ngap vs reference (signed — fix the sign, not just the number):\n" + "\n".join(
                f"  {d}" for d in deltas[:6]
            )
            if len(deltas) > 6:
                text += f"\n  … and {len(deltas) - 6} smaller gap(s)"
        elif groups:
            text += "\ngap vs reference: every tracked metric is within tolerance"
        else:
            text += (
                "\ngap vs reference: appearance/detail deltas intentionally hidden "
                "for this layer; they are not actionable ownership signals"
            )
    except Exception as e:
        # Never break a comparison over the extra readout — but say it is missing, or a
        # builder silently loses its only objective signal and nobody can tell.
        text += f"\n(signed gap unavailable: {type(e).__name__}: {str(e)[:70]})"
    if abs(cand_raw.width / max(cand_raw.height, 1) - ref_raw.width / max(ref_raw.height, 1)) > 0.02:
        # Different aspect means the metric bands are not describing the same regions;
        # say so rather than reporting a confident number about mismatched frames.
        text += (
            f"\n⚠ aspect mismatch: render {cand_raw.width}x{cand_raw.height} vs "
            f"reference {ref_raw.width}x{ref_raw.height} — band metrics compare "
            f"different parts of the frame"
        )

    if cand_raw.height < _METRIC_H:
        text += (
            f"\n⚠ render is only {cand_raw.height}px tall — below the {_METRIC_H}px "
            f"measurement height, so it had to be upscaled and detail metrics read "
            f"soft. Re-render at a higher scale before trusting them."
        )

    # …and build the side-by-side from the ORIGINALS, purely for looking at. Display
    # resizing is separate on purpose: it must never feed back into the numbers.
    cand_d, ref_d = _to_display_size(cand_raw), _to_display_size(ref_raw)
    sheet = Image.new("RGB", (cand_d.width + ref_d.width, _DISPLAY_H), (18, 18, 22))
    sheet.paste(cand_d, (0, 0))
    sheet.paste(ref_d, (cand_d.width, 0))

    sheet = mark_pair(sheet, cand_d.width, "CANDIDATE — LEFT", "REFERENCE — RIGHT")
    # Two frames side by side hit the stdio ceiling at half the height one frame does.
    # Shrink only if the join is genuinely too wide, and shrink the SHEET — never the
    # copies the numbers above were computed from.
    if sheet.width > _SHEET_MAX_W:
        h = max(1, round(sheet.height * _SHEET_MAX_W / sheet.width))
        sheet = sheet.resize((_SHEET_MAX_W, h), Image.LANCZOS)
    return {
        "content": [
            {"type": "text", "text": text},
            {"type": "image", "data": _b64(sheet), "mimeType": "image/jpeg"},
        ]
    }


def _run_bpy_instrument_hint(script: str, error: str) -> str:
    """Name the existing instrument when free-form bpy reinvents a check_scene kind.

    Run 20260826T170413Z-ba2b4c died on ``BVHTree.FromMesh`` while
    ``path_clearance_min`` / ``check_scene(kind='motion')`` already measured the
    same quantity. A second clearance implementation is not a fix.
    """
    blob = f"{script}\n{error}"
    extra = ""
    if "FromMesh" in blob or "BVHTree" in blob or "find_nearest" in blob or "closest_dist" in blob:
        extra = (
            "\nHINT: do not invent a BVH/nearest-point probe in run_bpy. Path clearance "
            "is the bound ``path_clearance_min`` contract (obstacles via compare_roles; "
            "the worker uses closest_point_on_mesh). Motion smoothness is "
            "check_scene(kind='motion', role=..., frames=[...]). BVHTree.FromMesh is not "
            "the 5.x constructor (FromBMesh) and is not the instrument."
        )
    elif "to_mesh(" in blob and "clearance" in blob.lower():
        extra = (
            "\nHINT: mesh-distance probes belong to the path_clearance_min contract and "
            "check_scene, not a private to_mesh loop."
        )
    return error + extra if extra and extra.strip() not in error else error


def _pending_black_frame_probe(comparison_state: dict) -> dict | None:
    """Return a genuinely unpaid black-frame probe, retiring stale guard state."""
    required = comparison_state.get("black_frame_required_probe")
    if not isinstance(required, dict):
        return None
    role = str(required.get("role") or "")
    frame = int(required.get("frame") or 0)
    density = float(required.get("density") or 0.0)
    measured = (comparison_state.get("world_density_probes") or {}).get(f"{role}@{frame}", [])

    if any(same_density(value, density) for value in measured):
        comparison_state.pop("black_frame_required_probe", None)
        return None
    return required


def _schedule_override_without_keying(script: str, protected_paths: set[str]) -> set[str]:
    """Return protected animated paths a payload tries to override as a live probe."""

    try:
        tree = ast.parse(script)
    except SyntaxError:
        return set()
    terminals = {path.rsplit(".", 1)[-1] for path in protected_paths}
    keyed: set[str] = set()
    uses_fcurve_inventory = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "bvfx_fcurves":
            uses_fcurve_inventory = True
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "keyframe_insert":
            continue
        value = None
        if node.args:
            with contextlib.suppress(ValueError, TypeError):
                value = ast.literal_eval(node.args[0])
        for keyword in node.keywords:
            if keyword.arg == "data_path":
                with contextlib.suppress(ValueError, TypeError):
                    value = ast.literal_eval(keyword.value)
        if isinstance(value, str):
            keyed.add(value.rsplit(".", 1)[-1])

    violated = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            # Editing a BezTriple coordinate changes the authored schedule just as
            # surely as assigning the driven property.  The common diagnostic bypass
            # is `for kp in bvfx_fcurves(...): kp.co[1] = ...`; conservatively protect
            # every active scheduled path when a payload combines the fcurve inventory
            # helper with a coordinate write.  Direct `keyframe_points[..].co[..]`
            # writes are identifiable without the helper and receive the same guard.
            attrs = {child.attr for child in ast.walk(target) if isinstance(child, ast.Attribute)}
            if "co" in attrs and (uses_fcurve_inventory or "keyframe_points" in attrs):
                violated.update(protected_paths)
            if not isinstance(target, ast.Attribute):
                continue
            if target.attr in terminals and target.attr not in keyed:
                violated.update(path for path in protected_paths if path.rsplit(".", 1)[-1] == target.attr)
            if target.attr == "mute":
                with contextlib.suppress(ValueError, TypeError):
                    if ast.literal_eval(node.value) is True:
                        violated.update(protected_paths)
    return violated


def _closed_density_repeat_message(comparison_state: dict, *, role: str, frame: int, values: list[float]) -> str:
    """Refuse an exact repeat after the density causal branch has closed."""

    probe_key = f"{role}@{int(frame)}"
    measured = (comparison_state.get("world_density_probes") or {}).get(probe_key, [])
    diagnosis = (comparison_state.get("world_density_probe_diagnoses") or {}).get(probe_key, "")
    if not (
        str(diagnosis).startswith("DENSITY HYPOTHESIS CLOSED")
        and measured
        and all(any(same_density(value, prior) for prior in measured) for value in values)
    ):
        return ""
    return (
        f"{diagnosis}\nBLOCKED: every requested Density value for {role!r} at "
        f"f{int(frame)} was already measured. This causal branch is closed; do not "
        "repeat the sweep. Test a different authorized variable or call "
        "cannot_express_in_scope when no such variable remains."
    )
def _render_setting_writes(script: str) -> set[str]:
    """Return free-form writes to harness-owned renderer configuration.

    Scoped units own semantic roles and controls, not the render harness. Diagnostics
    go through transactional render tools. This follows the common
    ``sc = bpy.context.scene; ee = sc.eevee`` alias form as well as direct chains.
    """

    try:
        tree = ast.parse(script)
    except SyntaxError:
        return set()
    namespaces = {
        "eevee",
        "cycles",
        "render",
        "view_settings",
        "display_settings",
        "sequencer_colorspace_settings",
    }
    setting_aliases: set[str] = set()

    def attrs(node: ast.AST) -> list[str]:
        out = []
        while isinstance(node, ast.Attribute):
            out.append(node.attr)
            node = node.value
        return list(reversed(out))

    def root_name(node: ast.AST) -> str | None:
        while isinstance(node, (ast.Attribute, ast.Subscript)):
            node = node.value
        return node.id if isinstance(node, ast.Name) else None

    def is_setting_expr(node: ast.AST) -> bool:
        return bool(namespaces.intersection(attrs(node))) or root_name(node) in setting_aliases

    assignments = sorted(
        (node for node in ast.walk(tree) if isinstance(node, (ast.Assign, ast.AnnAssign))),
        key=lambda node: getattr(node, "lineno", 0),
    )
    for node in assignments:
        if not is_setting_expr(node.value):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        setting_aliases.update(target.id for target in targets if isinstance(target, ast.Name))

    writes: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, (ast.Attribute, ast.Subscript)) and is_setting_expr(target):
                    chain = attrs(target)
                    writes.add(".".join(chain[-2:]) or "render_setting")
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "setattr"
            and len(node.args) >= 2
            and is_setting_expr(node.args[0])
        ):
            try:
                name = str(ast.literal_eval(node.args[1]))
            except (TypeError, ValueError):
                name = "<dynamic>"
            writes.add(name)
    return writes


def _scoped_renderer_write_error(script: str, mutation_roles: tuple[str, ...] | None) -> str:
    """Fail closed before a scoped unit changes harness-owned render policy."""
    if not mutation_roles:
        return ""
    writes = _render_setting_writes(script)
    if not writes:
        return ""
    return (
        "BLOCKED: scoped units do not own free-form renderer configuration writes ("
        + ", ".join(sorted(writes))
        + "). Render engine, sampling, thresholds, ray tracing, and color management "
        "belong to the harness unless a typed plan control grants them. Use "
        "render_frame/render_pass for transactional diagnostics; if the declared roles "
        "cannot pass under canonical settings, call cannot_express_in_scope."
    )


def _black_search_stop_message(comparison_state: dict) -> str:
    """Name the only legal transition after bounded black-frame search closes."""
    stopped = comparison_state.get("black_frame_search_exhausted")
    if not isinstance(stopped, dict):
        return ""
    ids = [str(item) for item in stopped.get("contract_ids") or []]
    invocation = (
        "cannot_express_in_scope(contract_ids=" + repr(ids) + ", reason=<the measured density + placement floor>)"
    )
    return (
        "BLOCKED: the bounded black-frame causal search is exhausted at "
        f"f{int(stopped.get('frame') or 0)} for {stopped.get('role')!r}. "
        + str(stopped.get("reason") or "")
        + " No further render, probe, or scene mutation is legal in this branch. Call "
        + invocation
        + "."
    )


def _probe_values_with_original(values: list[float], original: float) -> list[float]:
    """Include the restored live value without exceeding the public eight-value cap."""

    out = list(values)
    if any(same_density(value, original) for value in out):
        return out
    if len(out) == 8:
        nearest = min(range(len(out)), key=lambda index: abs(out[index] - original))
        out[nearest] = original
    else:
        out.append(original)
    return sorted(set(out))
