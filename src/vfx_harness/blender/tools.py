"""Agent SDK tools over a warm BlenderSession.

`build_blender_tools(session)` returns an in-process MCP server and the list of
qualified tool names to put in `ClaudeAgentOptions.allowed_tools`. Render tools
return image content blocks, so the agent literally sees the frames it makes.
"""

from __future__ import annotations

import base64
import contextlib
import fnmatch
import io
import json
import math
import re
import shutil
from pathlib import Path

import anyio
from claude_agent_sdk import create_sdk_mcp_server, tool
from PIL import Image

from vfx_harness.evidence.checks import METRICS
from vfx_harness.evidence.compare_panels import crop_pixels, save_context_sheet, save_focus_sheet, validate_crop
from vfx_harness.observability import run_artifacts
from vfx_harness.observability.log import log
from vfx_harness.orchestration.escalate import ask as _ask
from vfx_harness.orchestration.script_map import find_lines as _find_lines
from vfx_harness.orchestration.script_map import outline as _outline

from .session import BlenderError, BlenderSession

SERVER_NAME = "blender"

# Renders are downscaled to JPEG before base64 so a single tool-result message stays
# well under the SDK's stdio buffer (a detailed 960px PNG base64s to >1MB and crashes
# it).
#
# 1024 was chosen against the stdio limit alone and is too small for the judgement it
# feeds. Claude tokenises images in 28x28 px patches: fine discrimination is measured at
# 0.00 accuracy for features spanning <=2 patches (56px) and 1.00 at >=4 patches (112px).
# At 1024px wide, this shot's hero facade piers span ~6px — a fifth of ONE patch, i.e.
# no representational slot in the encoder at all. 2048 doubles every feature's patch
# count and a 2048x1024 JPEG q85 base64s to ~300-450KB, comfortably inside the buffer.
_MAX_W = 2048
# The side-by-side sheet is two images wide, so it needs its own ceiling or the same
# height that is right for ONE frame doubles the payload.
_SHEET_MAX_W = 3072
_JPEG_Q = 85


def _text(s: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": s}], **({"is_error": True} if is_error else {})}


def _merge_worklist_items(state: dict, new_items: list[str]) -> dict:
    """Carry unresolved items across a rewritten checklist without stale completions."""
    old_done = set(state.get("done", []))
    unfinished = [item for item in state.get("items", []) if item not in old_done]
    state["items"] = list(dict.fromkeys([*unfinished, *new_items]))
    state["done"] = [item for item in state.get("done", []) if item in state["items"]]
    return state


def _scene_completion_state(
    evidence: list[dict], layer_id: str, required_ids: set[str] | None = None
) -> dict:
    """Separate healthy inherited inputs from authority to seal the current layer.

    Persistent upstream interfaces must pass, but they cannot prove that a downstream
    department has performed its own work.  Only an active contract owned by the current
    layer may close the live mutation gate.  Layers with image-only/subjective completion
    keep mutation open until they voluntarily hand off to the critic.

    `required_ids` are the contracts bound to the active unit's REQUIRED claims. A
    contract that is never evaluated — selector matched nothing, probe errored, frame
    group never ran — is neither a pass nor a failure, so presence-based sealing let a
    unit close while a required claim had no evidence at all. Completeness is therefore
    checked explicitly: absent required evidence blocks sealing exactly like a failure.
    """
    authoritative = [row for row in evidence if row.get("authoritative")]
    current = [row for row in authoritative if str(row.get("owner_layer") or "") == str(layer_id)]
    failures = [row for row in authoritative if not row.get("pass")]
    evaluated = {str(row.get("id")) for row in evidence}
    missing = sorted(set(required_ids) - evaluated) if required_ids else []
    return {
        "authoritative": authoritative,
        "current": current,
        "failures": failures,
        "missing": missing,
        "interfaces_ready": bool(authoritative) and not failures and not missing,
        "may_seal": bool(current) and not failures and not missing,
    }


def _bound_static_frames(
    rows: list[dict], active_ids: set[str] | None, fallback_frame: int
) -> list[int]:
    """Frames whose static contracts must be produced for the active evidence boundary."""
    if active_ids is None:
        return [int(fallback_frame)]
    from vfx_harness.evidence.scene_checks import FUNCTIONAL_KINDS

    frames = {
        int(row.get("frame", fallback_frame))
        for row in rows
        if str(row.get("id")) in active_ids and row.get("kind") not in FUNCTIONAL_KINDS
    }
    return sorted(frames or {int(fallback_frame)})


def _load(path: str) -> Image.Image:
    im = Image.open(path).convert("RGB")
    if im.width > _MAX_W:
        im = im.resize((_MAX_W, round(im.height * _MAX_W / im.width)))
    return im


def _b64(im: Image.Image) -> str:
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=_JPEG_Q)
    return base64.standard_b64encode(buf.getvalue()).decode()


def _stats(im: Image.Image, *, feedback_groups=None) -> str:
    """Objective exposure readout so the agent stops eyeballing blowout/whiteout."""
    groups = set(feedback_groups) if feedback_groups is not None else {"exposure"}
    if "exposure" not in groups:
        return "exposure metrics suppressed — exposure is owned by another layer"
    from vfx_harness.evidence.metrics import look_vector

    metrics = look_vector(im)
    mean = metrics["exposure_mean"]
    clipped = metrics["clipped_pct"]
    black = metrics["black_pct"]
    line = (
        f"exposure: mean {mean:.0f}/255 · clipped(blown) {clipped:.0f}% · black {black:.0f}%"
    )
    if clipped > 12:
        line += "  ⚠ highlights BLOWN — lower emission/light strength or exposure"
    if black > 85:
        line += "  ⚠ frame almost entirely black — add light/emission or open exposure"
    return line


def _region_metrics(im: Image.Image) -> dict:
    """Perceptual metrics per horizontal band (top/mid/bottom thirds) + halation.
    Look-agnostic: measures image properties (structure, halation), not content."""
    from vfx_harness.evidence.metrics import look_vector

    metrics = look_vector(im)
    bands = {
        name: (metrics[f"band_mean_{name}"], metrics[f"structure_{name}"])
        for name in ("top", "mid", "bot")
    }
    sampled = 960 * max(1, round(im.height * 960 / im.width)) / 4
    hot_core = metrics.get("hot_core", 0.0)
    return {
        "bands": bands,
        "halation": metrics.get("halation"),
        "hot_px": round(hot_core * sampled / 1e6),
        "hot_core": round(hot_core),
    }


def _metrics_line(im: Image.Image, ref: Image.Image | None = None, *, feedback_groups=None) -> str:
    """`structure` = local stdev per band (fog wall = LOW; wispy/structured = HIGH).
    `halation` = glow-area : hot-core ratio (hard dots = LOW; bloomy = HIGH), or `n/a`
    when the frame carries too little hot core for that ratio to mean anything.
    With a ref, report deltas so tuning becomes numeric convergence, not guessing."""
    groups = (
        set(feedback_groups)
        if feedback_groups is not None
        else {"exposure", "detail", "emitters", "halation", "color", "motion"}
    )
    if "detail" not in groups and "halation" not in groups:
        return (
            "reference appearance metrics suppressed — use authoritative contracts and diagnostics owned by this layer"
        )
    mm = _region_metrics(im)
    parts = []
    for band in ("top", "mid", "bot"):
        mean, sd = mm["bands"][band]
        parts.append(f"{band} μ{mean:.0f}/σ{sd:.0f}")
    hal = mm["halation"]
    line = f"structure: {' · '.join(parts)}" if "detail" in groups else ""
    if "halation" in groups:
        if line:
            line += " · "
        line += "halation " + (f"{hal}" if hal is not None else f"n/a (only {mm['hot_px']}px of hot core)")
    if ref is not None:
        rm = _region_metrics(ref)
        deltas = []
        if "detail" in groups:
            for band in ("top", "mid", "bot"):
                sd, rsd = mm["bands"][band][1], rm["bands"][band][1]
                if rsd > 4 and sd < rsd * 0.45:
                    deltas.append(f"{band} σ{sd:.0f} vs ref σ{rsd:.0f} → needs ~{rsd / max(sd, 1):.1f}× more structure")
        # "crank bloom" is the right instruction only when there IS a core and its glow is
        # too tight. Read off a candidate with no blown pixels at all it sent the builder
        # to the glare node when the scene had nothing bright enough to glare — bloom
        # scales what exists, so on an unlit frame it multiplies zero. Say which it is.
        rh = rm["halation"]
        if "halation" in groups and rh is not None and rh > 1:
            if hal is None:
                deltas.append(
                    f"halation n/a vs ref {rh} → only {mm['hot_px']}px reach the "
                    f"hot-core threshold (ref {rm['hot_px']}px): raise emitter or "
                    f"key intensity until something blows out, THEN judge bloom"
                )
            elif hal < rh * 0.45:
                deltas.append(f"halation {hal} vs ref {rh} → crank bloom")
        if deltas:
            line += "\nref gap: " + "; ".join(deltas)
    return line


def _warn_suffix(r: dict) -> str:
    """Scene-state warnings from the worker, attached to the render they describe.

    These are conditions where the render looks entirely plausible while silently doing
    the opposite of what was asked — a sun inside a world volume being the one that cost
    five layer-2 attempts. There is nothing in the picture to prompt suspicion, so the
    warning has to travel with it.
    """
    w = r.get("warnings") or []
    return "".join(f"\n⚠ {x}" for x in w)


def subtract_png(a_path: str, b_path: str, dest: str) -> dict:
    """|A − B| as an image, plus how much the two frames actually differ.

    Client-side: Blender's bundled Python has no Pillow, and two PNGs already on disk
    do not need a scene to be subtracted.

    `did_work` answers the question a side-by-side cannot: a near-black diff means the
    edit changed nothing. The 1.5/255 threshold is above PNG quantisation and well below
    any visible change.
    """
    from PIL import ImageChops, ImageStat

    a = Image.open(a_path).convert("RGB")
    b = Image.open(b_path).convert("RGB")
    resized = a.size != b.size
    if resized:
        b = b.resize(a.size, Image.LANCZOS)
    diff = ImageChops.difference(a, b)
    st = ImageStat.Stat(diff.convert("L"))
    mean = st.mean[0]
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    diff.save(dest)
    return {
        "image_path": dest,
        "mean_delta": round(mean, 2),
        "max_delta": int(st.extrema[0][1]),
        "did_work": mean > 1.5,
        # A resample introduces differences of its own, so a diff across two sizes
        # cannot answer "did my edit do anything". Say so rather than returning a
        # number that looks like the same measurement.
        "resized": resized,
    }


def _check_report(kind: str, r: dict) -> str:
    """A check's answer as text, with the ISSUES first.

    Deliberately not a JSON dump. The failure this class of check exists to catch is
    "the number was measured, written into a comment, and verified by nothing" — so
    the finding has to read as a finding, not as a payload to be re-derived.
    """
    issues = r.get("issues") or []
    head = f"check {kind}: " + ("PASS ✅" if r.get("ok") else "ISSUES ✗")
    lines = [head]
    for i in issues:
        lines.append(f"  ✗ {i}")
    if kind == "visibility":
        lines.append(
            f"  visible fraction {r.get('visible_fraction')} "
            f"({r.get('hits')} hit · {r.get('occluded')} occluded · "
            f"{r.get('missed')} missed of {r.get('samples')} rays)"
        )
    elif kind == "framing":
        lines.append("  coordinates: [x0,y0,x1,y1], origin TOP-LEFT (x right, y down)")
        for fr in r.get("frames", []):
            lines.append(
                f"  f{fr.get('frame')}: bbox {fr.get('bbox')} · "
                f"w {fr.get('width')} h {fr.get('height')} · "
                f"centre {fr.get('centre')} · on-screen {fr.get('on_screen')}"
            )
    elif kind == "motion":
        holds = (
            f" · holds {r.get('leading_hold_segments', 0)} before/{r.get('trailing_hold_segments', 0)} after"
            if r.get("active_frame_span")
            else ""
        )
        lines.append(
            f"  max speed {r.get('max_speed')} u/f (f{r.get('peak_speed_frame')}) · "
            f"max |accel| {r.get('max_accel')} u/f^2 · "
            f"max |jerk| {r.get('max_jerk')} u/f^3 · "
            f"{'one unbroken move' if r.get('unbroken') else 'BROKEN move'}{holds}"
        )
    elif kind == "mesh":
        c = r.get("counts", {})
        lines.append(
            f"  {c.get('verts')} verts · {c.get('edges')} edges · {c.get('faces')} faces · {c.get('islands')} island(s)"
        )
        lines.append(
            f"  non-manifold total {c.get('nonmanifold_edges')} "
            f"(boundary {c.get('boundary_edges')} · branch {c.get('branch_edges')} "
            f"· wire {c.get('wire_edges')}) · loose {c.get('loose_verts')} · "
            f"degenerate {c.get('degenerate_faces')} · n-gons {c.get('ngons')} · "
            f"poles {c.get('poles')}"
        )
        if r.get("allow_boundary"):
            lines.append("  intentional open-shell boundaries allowed; branch/wire edges remain defects")
    elif kind == "scale":
        lines.append(f"  scale {r.get('scale')} · dimensions {r.get('dimensions')} · units {r.get('unit_system')}")
    elif kind == "passes":
        lines.append(
            f"  {r.get('channels')} channels · NaN {r.get('nan')} · "
            f"Inf {r.get('inf')} · negative {r.get('negative')} · "
            f"passes {r.get('passes_enabled')}"
        )
    elif kind in ("bbox", "subject_bbox"):
        lines.append(
            f"  bbox {r.get('bbox')} (0..1, origin TOP-LEFT; x right, y down) · "
            f"w {r.get('width')} h {r.get('height')} · centre {r.get('centre')}"
        )
        lines.append(
            "  hand this straight to render_pass(crop=…, res_pct=400) — an "
            "oracle crop measures far better than a guessed one."
        )
    if not issues and kind in ("visibility", "framing", "motion", "mesh", "scale"):
        lines.append("  nothing to fix on this check — the numbers above are the record.")
    return "\n".join(lines)


def _image(path: str, caption: str, *, feedback_groups=None) -> dict:
    im = _load(path)
    return {
        "content": [
            {
                "type": "text",
                "text": (
                    f"{caption}\n{_stats(im, feedback_groups=feedback_groups)}\n"
                    f"{_metrics_line(im, feedback_groups=feedback_groups)}"
                ),
            },
            {"type": "image", "data": _b64(im), "mimeType": "image/jpeg"},
        ]
    }


# Metrics are computed at ONE fixed size for every comparison, whatever `scale` the
# render was made at. Previously both images were forced to height 512 AFTER the render
# had already been shrunk by `scale`, so the two travelled different resampling paths:
# the reference (always the full 1920x960 plate) was downscaled 0.53x and stayed sharp,
# while a render at the default scale=0.4 was 768x384 and got UPSCALED 1.33x. Detail and
# structure therefore read as systematically softer than they were, and the builder chased
# sharpness it already had. Worse, two calls at different scales were not comparable to
# each other, yet the builder varies scale freely between them — in one layer it compared
# the same frame at 0.6 and then at 1.0 while editing a node in between, making the
# reading uninterpretable. (This is the same artifact that produced a "detail 46% high"
# finding I had to retract; it was in the tool, not just the analysis.)
# 320, NOT 512, and the number matters. The smallest scale the builder uses (0.35 of a
# 1920x960 shot) renders 336px tall, so a 512 metric height would still UPSCALE the render
# while the full-res reference downscaled — the very asymmetry this is fixing, just moved.
# Measuring below every accepted render height means neither image is ever upscaled.
_METRIC_H = 320
# DISPLAY is a SEPARATE question from METRIC and the two must not be reconciled.
#
# _METRIC_H exists to stop resampling asymmetry: measure below every accepted render
# height so neither image is ever upscaled. Lower is SAFER there.
#
# _DISPLAY_H is what a vision model gets to look at, where lower is strictly worse.
# Claude tokenises in 28x28 px patches, so a feature must clear ~4 patches (112px) to be
# discriminable and is at chance below ~2 (56px). At 512px tall, this shot's hero tower
# facade piers land near 6px — 0.21 of one patch, five times below the floor. The render
# was already paid for at full resolution; shipping it shrunk is throwing away the only
# signal the judge has. 1024 is the height at which a 2:1 frame still passes Claude's
# high-resolution tier unresized (~2400 tokens, ~$0.012 a call).
#
# The reasoning for METRIC leaked into DISPLAY once already. They answer different
# questions; do not "unify" them.
_DISPLAY_H = 1024

# (frame, ref) -> the (mode, scale) it was last measured at, so a change is announced.
_LOOK_METRICS = {
    "hot_core",
    "halation",
    "halation_top",
    "halation_mid",
    "halation_bot",
    "points",
    "points_top",
    "points_mid",
    "points_bot",
    "chroma_spread",
}


_ROLE_MANIFEST = (
    "RESULT = {o.name: str(o.get('bvfx_role') or '') for o in bpy.context.scene.objects}"
)


def _scope_offenders(manifest: dict, allowed: tuple[str, ...]) -> list[str]:
    """Objects whose semantic role falls outside the unit's declared authority.

    Reported on every call until fixed, never once-and-forgotten: builders create first
    and tag second, so suppressing a name after first sight hides an untagged object for
    the rest of the build — which is how an untagged curve reached canonical replay.
    """
    return [
        f"{name!r} role={str(role) or '<none>'}"
        for name, role in sorted(manifest.items())
        if not _role_in_scope(str(role), allowed)
    ]


def _role_in_scope(role: str, allowed: tuple[str, ...]) -> bool:
    """A namespace owns its dot-delimited descendants, never a similar sibling.

    Mirrors the canonical replay rule in `builder._scope_added_object_errors` so live
    feedback and the deterministic gate cannot disagree about what is in scope."""
    if not role:
        return False
    for pattern in allowed:
        if fnmatch.fnmatchcase(role, pattern):
            return True
        if not any(token in pattern for token in "*?[") and role.startswith(pattern + "."):
            return True
    return False


def _layer_feedback_policy(shot_dir: Path | None, layer_id: str | None) -> dict:
    """Derive comparison advice from the axes this layer can actually change."""
    from vfx_harness.domain.work_units import read_document

    axes: list[str] = []
    if shot_dir and layer_id and (shot_dir / "layers.json").is_file():
        try:
            for row in read_document(shot_dir / "layers.json"):
                if str(row.get("id")) == str(layer_id):
                    axes = [str(axis).lower() for axis in (row.get("owns") or [])]
                    break
        except (OSError, ValueError):
            axes = []
    from vfx_harness.agents.build_prompts import axis_feedback_groups

    groups = axis_feedback_groups([(axis, "") for axis in axes])
    return {
        "axes": axes,
        # Empty ownership is invalid in the strict contract and earns no speculative
        # look feedback. There is intentionally no legacy all-feedback fallback.
        "groups": sorted(groups),
        "look_actions": bool(groups),
    }


def _comparison_lock_error(locks: dict, key: tuple, settings: tuple) -> str | None:
    """Lock one frame/crop's render settings for the duration of a build round."""
    previous = locks.get(key)
    if previous is None:
        locks[key] = settings
        return None
    if previous == settings:
        return None
    return (
        f"comparison settings are LOCKED for round {key[0]}: first call used "
        f"mode={previous[0]} scale={previous[1]} res_pct={previous[2]}; requested "
        f"mode={settings[0]} scale={settings[1]} res_pct={settings[2]}. Reuse the "
        f"first settings so before/after metrics remain comparable; settings may "
        f"change only after the next critic round begins"
    )


def _comparison_mode_scale(args: dict, base_lock: tuple | None) -> tuple[str, float]:
    """Resolve omitted values from the round lock instead of unrelated defaults."""
    mode = args.get("mode", base_lock[0] if base_lock else "eevee")
    scale = float(args["scale"] if "scale" in args else (base_lock[1] if base_lock else 0.4))
    return mode, scale


def _check_args_error(kind: str, args: dict) -> str | None:
    requirements = {
        "visibility": ("object", "frame"),
        "framing": ("object",),
        "motion": ("object", "frames"),
        "mesh": ("object",),
        "scale": ("object",),
        "passes": ("frame",),
        "bbox": ("object", "frame"),
    }
    missing = [name for name in requirements[kind] if args.get(name) is None]
    if kind == "framing" and args.get("frame") is None and not args.get("frames"):
        missing.append("frame or frames")
    if missing:
        return f"check_scene(kind={kind!r}) requires " + ", ".join(dict.fromkeys(missing))
    if kind == "motion" and len(args.get("frames") or []) < 2:
        return "check_scene(kind='motion') requires frames with at least 2 entries"
    return None


def _pixel_contract_gate(
    shot_dir: Path,
    layer_id: str,
    *,
    frame: int,
    ref: str,
    render: str | Path,
    evidence_ids: set[str] | None = None,
) -> tuple[bool, list[dict]]:
    """Is a scene-contract-complete candidate judgeable enough for a critic?"""
    from vfx_harness.evidence.checks import layer_evidence

    if evidence_ids is not None and not evidence_ids:
        return True, []
    rows = layer_evidence(shot_dir, layer_id, frame=frame, ref=ref, render=render)
    if evidence_ids is not None:
        rows = [row for row in rows if str(row.get("id")) in evidence_ids]
    authoritative = [row for row in rows if row.get("authoritative")]
    if authoritative:
        return all(row.get("pass") for row in authoritative), authoritative
    if evidence_ids:
        missing = [
            {
                "id": evidence_id,
                "metric": "bound_image_evidence",
                "value": None,
                "target": "produced by the active unit",
                "pass": False,
                "origin": "harness",
                "authoritative": True,
            }
            for evidence_id in sorted(evidence_ids)
        ]
        return False, missing
    image = Image.open(render).convert("L")
    mean = sum(image.getdata()) / max(1, image.width * image.height)
    row = {
        "id": "harness-frame-judgeable",
        "metric": "frame_mean",
        "value": round(mean, 4),
        "target": ">= 4",
        "pass": mean >= 4,
        "origin": "harness",
        "authoritative": True,
    }
    return bool(row["pass"]), [row]


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
        from vfx_harness.evidence.metrics import compare as _mcompare
        from vfx_harness.evidence.metrics import look_pair as _lp

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
    from vfx_harness.evidence.compare_panels import mark_pair

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


def build_blender_tools(
    session: BlenderSession,
    assets_dir: str | Path | None = None,
    shot_dir: str | Path | None = None,
    layer_id: str | None = None,
    comparison_state: dict | None = None,
    feedback_groups: list[str] | None = None,
    mutation_roles: tuple[str, ...] | None = None,
):
    """Wire the warm session as SDK tools. `assets_dir` enables `import_asset`;
    `shot_dir` enables `compare_frame` to resolve reference paths (e.g. refs/…).

    `feedback_groups` carries the active unit's DECLARED look capability resolved to
    metric families. When present it IS the policy; the layer-axis derivation is the
    legacy path for schema-4 layers with no declaring work unit."""
    assets_dir = Path(assets_dir) if assets_dir else None
    shot_dir = Path(shot_dir) if shot_dir else None
    comparison_state = comparison_state if comparison_state is not None else {"round": 1}
    comparison_locks: dict = {}
    feedback_policy = (
        {
            "axes": [],
            "groups": sorted(feedback_groups),
            "look_actions": bool(feedback_groups),
            "source": "declared_unit_capabilities",
        }
        if feedback_groups is not None
        else _layer_feedback_policy(shot_dir, layer_id)
    )

    async def _call(cmd, **args):
        return await anyio.to_thread.run_sync(lambda: session.call(cmd, **args))

    @tool(
        "run_bpy",
        "Execute Python (with `bpy` in scope) against the live scene — the hands. "
        "Model, shade, key, set render config. Set `RESULT` to return JSON data; "
        "print()s are captured. Returns TIMING + scene-delta so you can feel cost. "
        "Pre-injected helpers (use them, don't hand-roll slow loops): "
        "bvfx_scatter_emissive(count,...) for light/greeble carpets (ONE instanced "
        "object, fast for 1000s); bvfx_volume(center,size,density,color,...) for a "
        "bounded volumetric domain (clouds/nebula/fog); bvfx_volumetric_world(...) for "
        "a tinted haze sky; bvfx_glare_bloom(...) for EEVEE-Next bloom; "
        "bvfx_emission(name,color,strength). To DEBUG a material/world, use inspect_nodes "
        "instead of rendering repeatedly to guess. Tag every contract-facing datablock "
        "with bvfx_role(target,'material.floor.worn',owner_layer='2') and every shader/"
        "compositor control with bvfx_control(node,'control.orb.gain',owner_layer='2'); "
        "semantic roles survive renames and are the only supported contract interface.",
        {"type": "object", "properties": {"script": {"type": "string"}}, "required": ["script"]},
    )
    async def run_bpy(args):
        try:
            r = await _call("run", code=args["script"])
        except BlenderError as e:
            return _text(str(e), is_error=True)
        # A successful script may have changed pixels even when this layer has no scene
        # completion contract. Never carry an earlier comparison verdict across it.
        comparison_state["pixel_contracts_passed"] = False
        out = r.get("stdout", "")
        res = r.get("result")
        el, oa, va = r.get("elapsed_s"), r.get("objects_added"), r.get("verts_added")
        sc = r.get("scene", {})
        body = (out + (f"\nRESULT: {res}" if res is not None else "")).strip() or "ok"
        meta = (
            f"\n⏱ {el}s · +{oa} objects · +{va} verts · "
            f"scene now {sc.get('objects', '?')} objs / {sc.get('tris', '?')} tris"
        )
        warn = ""
        if isinstance(el, (int, float)) and el > 20:
            warn += (
                f"\n⚠ that took {el}s — too slow. Never create repeated elements in a "
                f"Python loop; use bvfx_scatter_emissive / instancing / bmesh."
            )
        if isinstance(oa, int) and oa > 200:
            warn += (
                f"\n⚠ +{oa} objects — collapse to ONE instanced mesh "
                f"(bvfx_scatter_emissive) instead of per-object creation."
            )
        # Scope is checked at canonical replay, which is AFTER the build spends its
        # whole budget: run 20260823T154920Z created camera, housing, tunnel and light
        # objects outside its declared roles and learned nothing until the end. Surface
        # the violation on the call that caused it, while the fix is one edit away.
        # Checked on EVERY successful call, not only when objects_added > 0: helpers
        # create objects through paths whose reported delta cannot be trusted, and the
        # cam_rig_spine build proved the point — two calls reported +2 objects, the live
        # check never spoke, and canonical replay found an untagged curve at the end.
        # Objects are re-examined until they are in scope, because a builder may create
        # first and tag second.
        if mutation_roles:
            try:
                manifest = (await _call("run", code=_ROLE_MANIFEST, journal=False)).get(
                    "result"
                ) or {}
                offenders = _scope_offenders(manifest, mutation_roles)
                if offenders:
                    log(f"scope: {len(offenders)} object(s) outside declared roles", 1)
                    warn += (
                        "\n⚠ SCOPE VIOLATION — this unit may only create objects in "
                        + ", ".join(mutation_roles)
                        + ": " + "; ".join(offenders[:6])
                        + "\nCanonical replay rejects these deterministically. Delete "
                        "them or tag them with a role inside your declared scope."
                    )
            except BlenderError as exc:
                # A silent probe failure is the same lie as a silent violation.
                log(f"scope probe unavailable ({str(exc)[:70]})", 1)
        # Scene contracts are the live execution authority. Evaluate them immediately
        # after every mutation so convergence is a state transition, not a suggestion the
        # model may overlook for another 80 turns. Pixel checks still happen after the
        # required comparison render.
        contract_note = ""
        if shot_dir and layer_id:
            try:
                active_ids = comparison_state.get("active_evidence_ids")
                from vfx_harness.evidence.scene_checks import layer_evidence, load_rows

                frames = _bound_static_frames(
                    load_rows(shot_dir), active_ids, int(comparison_state.get("frame", 1))
                )
                evidence = []
                for evidence_frame in frames:
                    evidence.extend(
                        await anyio.to_thread.run_sync(
                            lambda frame=evidence_frame: layer_evidence(
                                shot_dir, str(layer_id), frame=frame, session=session
                            )
                        )
                    )
                if active_ids is not None:
                    evidence = [row for row in evidence if str(row.get("id")) in active_ids]
                evidence = list({str(row.get("id")): row for row in evidence}.values())
                authoritative = [row for row in evidence if row.get("authoritative")]
                if authoritative and all(row.get("pass") for row in authoritative):
                    from vfx_harness.evidence.scene_checks import functional_evidence

                    evidence.extend(
                        await anyio.to_thread.run_sync(
                            lambda: functional_evidence(shot_dir, str(layer_id), session=session)
                        )
                    )
                    if active_ids is not None:
                        evidence = [row for row in evidence if str(row.get("id")) in active_ids]
                    evidence = list({str(row.get("id")): row for row in evidence}.values())
                    authoritative = [row for row in evidence if row.get("authoritative")]
                state = _scene_completion_state(evidence, str(layer_id), active_ids)
                authoritative = state["authoritative"]
                passed = [row for row in authoritative if row.get("pass")]
                failed = state["failures"]
                if authoritative:
                    from vfx_harness.observability.runlog import bump

                    bump("automatic_scene_contract_probe")
                    contract_note = f"\nAUTHORITATIVE SCENE CONTRACTS: {len(passed)}/{len(authoritative)} pass"
                    if state["missing"]:
                        # Unevaluated required evidence used to read as silence. Name it:
                        # a selector that matches nothing looks identical to a claim
                        # nobody wrote, and both block sealing.
                        comparison_state["scene_contracts_passed"] = False
                        comparison_state["scene_interfaces_ready"] = False
                        contract_note += (
                            " · REQUIRED EVIDENCE NOT PRODUCED: "
                            + ", ".join(state["missing"][:6])
                            + "\n  These contracts are bound to required claims but were "
                            "never evaluated — usually a selector matching no object, or "
                            "a frame group that never ran. They cannot pass by absence."
                        )
                    if failed:
                        comparison_state["scene_interfaces_ready"] = False
                        comparison_state["scene_contracts_passed"] = False
                        comparison_state["current_scene_contracts_present"] = bool(state["current"])
                        contract_note += " · failing:\n" + "\n".join(
                            f"  {row.get('id', '?')}: {row.get('metric')}="
                            f"{row.get('value')} target {row.get('target')} — "
                            f"{row.get('definition', 'see scene_checks contract')}"
                            for row in failed[:6]
                        )
                    elif state["may_seal"]:
                        comparison_state["scene_interfaces_ready"] = True
                        comparison_state["current_scene_contracts_present"] = True
                        comparison_state["scene_contracts_passed"] = True
                        if comparison_state.get("image_evidence_required"):
                            contract_note += (
                                "\nSCENE CONTRACTS PASS: call one FULL-FRAME compare_frame now. "
                                "This unit binds authoritative image evidence; a failed bound "
                                "check reopens one repair. Further run_bpy edits are blocked "
                                "until that comparison."
                            )
                        else:
                            comparison_state["pixel_contracts_passed"] = True
                            contract_note += (
                                "\nUNIT HANDOFF READY: every bound executable scene contract "
                                "passes and this unit binds no image contract. Use the required "
                                "diagnostics, then stop; no beauty comparison is required."
                            )
                    else:
                        comparison_state["scene_interfaces_ready"] = True
                        comparison_state["current_scene_contracts_present"] = False
                        comparison_state["scene_contracts_passed"] = False
                        contract_note += (
                            "\nINHERITED INTERFACES PASS, but this layer owns no active "
                            "scene completion contract. They prove healthy inputs, not "
                            "that the current layer is finished; live mutation remains "
                            "open until the builder hands its scoped work to the critic."
                        )
            except Exception as exc:
                contract_note = (
                    f"\n⚠ automatic scene-contract probe unavailable: {type(exc).__name__}: {str(exc)[:100]}"
                )
        return _text(body + meta + warn + contract_note)

    @tool(
        "inspect_scene",
        "Read the scene as text (Tier-1, free, no render): objects+transforms+modifiers"
        "+particle systems, materials, world, and render settings. Use this to verify "
        "structure before spending a render.",
        {
            "type": "object",
            "properties": {"section": {"type": "string", "enum": ["all", "objects", "materials", "world", "render"]}},
            "required": [],
        },
    )
    async def inspect_scene(args):
        r = await _call("inspect", section=args.get("section", "all"))
        return _text(r["text"])

    @tool(
        "inspect_nodes",
        "Dump a NODE GRAPH as text — every node's type, its unlinked input socket "
        "values, and the links. target = 'compositor' (the bloom/glare graph — 5.x has "
        "NO scene.node_tree, it's scene.compositing_node_group), 'world', a material "
        "name, or an object name (its active material). Use this to DEBUG shaders/"
        "compositor directly instead of rendering over and over to guess.",
        {"type": "object", "properties": {"target": {"type": "string"}}, "required": ["target"]},
    )
    async def inspect_nodes(args):
        try:
            r = await _call("nodes", target=args.get("target", "world"))
        except BlenderError as e:
            return _text(str(e), is_error=True)
        return _text(r["text"])

    @tool(
        "list_keyframes",
        "The Graph-Editor read (Tier-1, free): every F-curve on an object as "
        "frame→value pairs with interpolation. Use to verify easing/timing without rendering.",
        {"type": "object", "properties": {"object": {"type": "string"}}, "required": ["object"]},
    )
    async def list_keyframes(args):
        try:
            r = await _call("keyframes", object=args["object"])
        except BlenderError as e:
            return _text(str(e), is_error=True)
        return _text(r["text"])

    @tool(
        "render_frame",
        "See one frame. mode='solid'/'wire' = fast Workbench (~0.1s, composition/"
        "silhouette); mode='draft' = fast low-sample EEVEE (~0.5s, quick look checks — "
        "use this while iterating); mode='eevee' = full-quality look (~1-3s, for final "
        "judging). scale is 0..1 (default 0.4). Returns the image + an EXPOSURE readout "
        "(mean/clipped/black) so you can catch blowout objectively.",
        {
            "type": "object",
            "properties": {
                "frame": {"type": "integer"},
                "mode": {"type": "string", "enum": ["solid", "wire", "draft", "eevee"]},
                "scale": {"type": "number", "description": "0..1 res scale, default 0.4"},
            },
            "required": ["frame"],
        },
    )
    async def render_frame(args):
        try:
            r = await _call(
                "render", frame=int(args["frame"]), mode=args.get("mode", "eevee"), scale=float(args.get("scale", 0.4))
            )
        except BlenderError as e:
            return _text(str(e), is_error=True)
        cap = f"frame {r['frame']} ({r['mode']})" + _warn_suffix(r)
        return _image(r["image_path"], cap, feedback_groups=feedback_policy["groups"])

    @tool(
        "render_pass",
        "Render one frame as a DIAGNOSTIC rather than a beauty shot, so you can see the "
        "thing you are actually being judged on. `pass` isolates a render pass — use "
        "'diffuse_direct' to see MODELLING BY LIGHT with emission removed (a render "
        "setting, not something to squint past), 'emit' to see only self-lit surfaces, "
        "'shadow'/'ao'/'normal'/'depth'/'crypto' for the rest. `shade` overrides "
        "materials: 'clay' for form, 'silhouette' for outline, 'matcap:<name>' for a "
        "Workbench diagnostic. `light='<LightObject>'` (comma list allowed) renders with "
        "ONLY those light objects and hides the rest, so you can see what one lamp "
        "actually contributes. `crop` is "
        "[x0,y0,x1,y1] in 0..1 from the TOP-LEFT and is a true optical zoom, so pair "
        "it with res_pct (e.g. 400) to see fine detail at real resolution instead of "
        "upscaling a thumbnail. Every mode returns a caption saying what to look for.",
        {
            "type": "object",
            "properties": {
                "frame": {"type": "integer"},
                "pass": {
                    "type": "string",
                    "enum": ["beauty", "diffuse_direct", "emit", "shadow", "ao", "normal", "depth", "crypto"],
                },
                "shade": {"type": "string", "description": "beauty | clay | silhouette | matcap:<name>"},
                "light": {
                    "type": "string",
                    "description": "light OBJECT name(s), comma-separated; all other lights are hidden for this render",
                },
                "crop": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "[x0,y0,x1,y1] in 0..1, origin TOP-LEFT",
                },
                "res_pct": {"type": "integer", "description": "resolution percentage; >100 zooms a crop"},
                "scale": {"type": "number"},
            },
            "required": ["frame"],
        },
    )
    async def render_pass(args):
        crop = args.get("crop")
        bad_crop = crop is not None and (
            len(crop) != 4
            or not all(0.0 <= float(v) <= 1.0 for v in crop)
            or not (crop[0] < crop[2] and crop[1] < crop[3])
        )
        if bad_crop:
            return _text(
                "crop must be [x0,y0,x1,y1] in 0..1 with x0<x1 and y0<y1 (origin TOP-LEFT; x right, y down)",
                is_error=True,
            )
        diagnostic_args = dict(args)
        if not feedback_policy["look_actions"]:
            # Layout owns form, not beauty. One stable material/lighting-independent
            # diagnostic prevents the builder from tuning albedo or lamps to a finished
            # reference whose appearance belongs to later layers.
            diagnostic_args["pass"] = "beauty"
            diagnostic_args["shade"] = "matcap:check_normal+y"
            diagnostic_args["light"] = None
        try:
            r = await _call(
                "render",
                frame=int(args["frame"]),
                mode="eevee",
                scale=float(diagnostic_args.get("scale", 0.5)),
                **{"pass": diagnostic_args.get("pass", "beauty")},
                shade=diagnostic_args.get("shade", "beauty"),
                light=diagnostic_args.get("light"),
                crop=crop,
                res_pct=args.get("res_pct"),
            )
        except BlenderError as e:
            return _text(str(e), is_error=True)
        # The caption is the point: a visual channel with no text measured WORSE than no
        # extra channel at all. It travels in the same text block as the readouts.
        cap = (
            f"frame {r['frame']} · {r.get('caption', '')}" + f"\nsettings: pass={r.get('pass')} shade={r.get('shade')} "
            f"light={r.get('light')} crop={r.get('crop')} res_pct={r.get('res_pct')}" + _warn_suffix(r)
        )
        return _image(r["image_path"], cap, feedback_groups=feedback_policy["groups"])

    @tool(
        "check_scene",
        "JUDGMENT-FREE checks on the scene itself — no critic, no cost, no render (except "
        "`passes`). This is the whole class of defect a beauty render CANNOT show: "
        "kind='visibility' ray-casts the camera to the object (a hero behind a wall looks "
        "fine until you look for it), 'framing' gives the NDC bbox/width/centre via "
        "world_to_camera_view, 'motion' gives max speed/accel/jerk and whether the move is "
        "unbroken, 'mesh' counts non-manifold edges, loose verts, n-gons, poles and "
        "disconnected islands, 'scale' checks dimensions and that scale is applied, "
        "'passes' checks the render buffer for NaN/Inf/negative pixels, 'bbox' returns the "
        "oracle crop box to hand to render_pass. Use these to VERIFY a claim you would "
        "otherwise write in a comment. Required arguments: visibility=object+frame; "
        "framing=object+(frame or frames); motion=object+2+ frames; mesh/scale=object; "
        "passes=frame; bbox=object+frame. For intentional open shells, mesh accepts "
        "allow_boundary=true and still rejects branch/wire edges.",
        {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["visibility", "framing", "motion", "mesh", "scale", "passes", "bbox"],
                },
                "object": {"type": "string"},
                "frame": {"type": "integer"},
                "frames": {"type": "array", "items": {"type": "integer"}},
                "samples": {"type": "integer"},
                "scale": {"type": "number"},
                "allow_boundary": {
                    "type": "boolean",
                    "description": "mesh only: permit intentional open-shell "
                    "boundary edges while still rejecting wire "
                    "and >2-face branch edges",
                },
            },
            "required": ["kind"],
        },
    )
    async def check_scene(args):
        kind = args["kind"]
        args_error = _check_args_error(kind, args)
        if args_error:
            return _text(args_error, is_error=True)
        payload = {k: v for k, v in args.items() if k != "kind" and v is not None}
        try:
            r = await _call("check", kind=kind, **payload)
        except BlenderError as e:
            return _text(str(e), is_error=True)
        return _text(_check_report(kind, r))

    @tool(
        "diff_frames",
        "Subtract one render from another and SEE the difference. Give two image paths "
        "from earlier render calls. A near-black diff means nothing changed — which is the "
        "answer to 'did my edit do anything' that a side-by-side cannot give you. Reports "
        "mean and max delta alongside the image.",
        {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "string"}}, "required": ["a", "b"]},
    )
    async def diff_frames(args):
        a, b = Path(args["a"]), Path(args["b"])
        for p in (a, b):
            if not p.is_file():
                return _text(f"{p} does not exist — pass the image paths from two earlier render calls", is_error=True)
        dest = a.with_name(f"diff_{a.stem}_vs_{b.stem}.png")
        try:
            r = await anyio.to_thread.run_sync(lambda: subtract_png(str(a), str(b), str(dest)))
        except OSError as e:
            return _text(f"could not subtract those images: {e}", is_error=True)
        verdict = (
            "the two renders DIFFER"
            if r["did_work"]
            else "the two renders are essentially IDENTICAL — whatever you changed had no visible effect at this frame"
        )
        cap = (
            f"|A − B| · mean delta {r['mean_delta']}/255 · max {r['max_delta']}/255 — "
            f"{verdict}. Bright regions are where the two renders disagree."
        )
        if r["resized"]:
            cap += (
                " ⚠ the two frames were DIFFERENT SIZES, so one was resampled and "
                "part of this difference is the resample, not your edit. Re-render "
                "both at the same scale before trusting it."
            )
        return _image(r["image_path"], cap)

    # A usable no-op check cannot require the model to recover internal render paths from
    # image-only tool results. Keep the baseline in the tool process and re-render with the
    # exact same settings after the edit, so the diff answers one question and only one.
    change_baselines: dict[str, tuple[Path, int, str, float]] = {}

    @tool(
        "verify_change",
        "Prove whether an edit changed the intended frame, without managing image paths. "
        "Call action='baseline' BEFORE run_bpy with a short label, frame, mode and scale. "
        "After the edit call action='compare' with the same label; the tool re-renders the "
        "stored frame at the IDENTICAL settings and returns |before-after| plus mean/max "
        "delta. A near-black result means the edit was a visible no-op. Use this whenever "
        "you are changing a node, light, visibility state, modifier, or small feature and "
        "cannot prove from a numeric scene check that the intended pixels moved.",
        {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["baseline", "compare"]},
                "label": {"type": "string"},
                "frame": {"type": "integer"},
                "mode": {"type": "string", "enum": ["solid", "wire", "draft", "eevee"]},
                "scale": {"type": "number"},
            },
            "required": ["action", "label"],
        },
    )
    async def verify_change(args):
        label = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(args["label"]).strip())[:60]
        if not label:
            return _text("label must contain at least one letter or number", is_error=True)
        if args["action"] == "baseline":
            if args.get("frame") is None:
                return _text("baseline requires frame", is_error=True)
            frame = int(args["frame"])
            mode = str(args.get("mode", "draft"))
            scale = float(args.get("scale", 0.5))
            try:
                rendered = await _call("render", frame=frame, mode=mode, scale=scale)
            except BlenderError as e:
                return _text(str(e), is_error=True)
            src = Path(rendered["image_path"])
            root = session.artifacts if shot_dir else src.parent
            root.mkdir(parents=True, exist_ok=True)
            dest = root / f"baseline_{label}_f{frame:04d}_{mode}.png"
            shutil.copyfile(src, dest)
            change_baselines[label] = (dest, frame, mode, scale)
            return _image(
                str(dest),
                f"change baseline '{label}' captured at f{frame} "
                f"mode={mode} scale={scale:g}. Make ONE edit, then call "
                f"verify_change(action='compare', label='{label}').",
            )

        prior = change_baselines.get(label)
        if prior is None:
            return _text(f"no baseline named {label!r}; call action='baseline' first", is_error=True)
        before, frame, mode, scale = prior
        try:
            rendered = await _call("render", frame=frame, mode=mode, scale=scale)
        except BlenderError as e:
            return _text(str(e), is_error=True)
        after = Path(rendered["image_path"])
        dest = before.with_name(f"change_{label}_f{frame:04d}.png")
        try:
            result = await anyio.to_thread.run_sync(lambda: subtract_png(str(before), str(after), str(dest)))
        except OSError as e:
            return _text(f"could not verify change {label!r}: {e}", is_error=True)
        verdict = (
            "VISIBLE CHANGE"
            if result["did_work"]
            else "VISIBLE NO-OP — do not keep tuning this control; inspect the graph, "
            "keyframe, visibility, or light linkage"
        )
        cap = (
            f"change '{label}' at f{frame}, mode={mode}, scale={scale:g}: {verdict}. "
            f"Mean delta {result['mean_delta']}/255 · max {result['max_delta']}/255. "
            f"Bright regions are the pixels the edit moved."
        )
        return _image(result["image_path"], cap)

    @tool(
        "compare_frame",
        "Render a frame against its reference. With no crop, returns the standard full "
        "SIDE-BY-SIDE comparison. With `crop=[x0,y0,x1,y1]` (normalized TOP-LEFT), "
        "returns BOTH a full-frame context map with that region outlined and a true "
        "optical high-resolution focus sheet. `views` controls aligned side_by_side, "
        "50/50 wipe, overlay, and difference views. Get a measured crop from "
        "check_scene(kind='bbox'); never replace full-frame context with a cherry-picked "
        "zoom. The first call locks mode+scale for the entire critic round. Crop calls "
        "inherit that locked mode+scale when omitted; res_pct changes optical crop "
        "resolution only and is separately locked per crop.",
        {
            "type": "object",
            "properties": {
                "frame": {"type": "integer"},
                "reference": {"type": "string", "description": "e.g. refs/M1_green.jpg"},
                "mode": {"type": "string", "enum": ["solid", "wire", "draft", "eevee"]},
                "scale": {"type": "number"},
                "crop": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "[x0,y0,x1,y1] in 0..1, origin TOP-LEFT",
                },
                "res_pct": {
                    "type": "integer",
                    "minimum": 100,
                    "maximum": 800,
                    "description": "optical crop resolution; defaults from crop size",
                },
                "views": {
                    "type": "array",
                    "maxItems": 4,
                    "items": {"type": "string", "enum": ["side_by_side", "wipe", "overlay", "difference"]},
                },
            },
            "required": ["frame", "reference"],
        },
    )
    async def compare_frame(args):
        ref = args["reference"]
        ref_path = (shot_dir / ref) if (shot_dir and not Path(ref).is_absolute()) else Path(ref)
        if not ref_path.is_file():
            return _text(f"reference not found: {ref_path}", is_error=True)
        round_id = int(comparison_state.get("round", 1))
        base_lock = comparison_locks.get((round_id, "base"))
        # Omitted crop settings INHERIT the round base instead of silently returning to
        # defaults and failing the lock established by a full-frame comparison.
        mode, scale = _comparison_mode_scale(args, base_lock)
        crop = args.get("crop")
        if crop is not None:
            try:
                crop = list(validate_crop(crop))
            except ValueError as exc:
                return _text(str(exc) + " (origin TOP-LEFT; x right, y down)", is_error=True)
        views = args.get("views") or ["side_by_side", "wipe"]
        res_pct = None
        if crop is not None:
            fraction = max(crop[2] - crop[0], crop[3] - crop[1])
            requested_pct = int(args.get("res_pct") or round(110 / fraction))
            res_pct = min(800, max(100, requested_pct))
        # Mode and base scale are round-wide: changing them on another frame would make
        # improvement/regression comparisons non-equivalent. Optical crop resolution is
        # additionally locked per crop because it legitimately depends on crop size.
        lock_error = _comparison_lock_error(comparison_locks, (round_id, "base"), (mode, scale, None))
        if not lock_error:
            lock_key = (round_id, int(args["frame"]), str(ref), tuple(crop) if crop else None)
            lock_error = _comparison_lock_error(comparison_locks, lock_key, (mode, scale, res_pct))
        if lock_error:
            return _text(lock_error, is_error=True)
        try:
            r = await _call("render", frame=int(args["frame"]), mode=mode, scale=scale)
        except BlenderError as e:
            return _text(str(e), is_error=True)
        gate_note = ""
        if (
            crop is None
            and shot_dir
            and (
                comparison_state.get("scene_interfaces_ready")
                or comparison_state.get("image_evidence_required")
            )
        ):
            try:
                gate_pass, gate_rows = await anyio.to_thread.run_sync(
                    lambda: _pixel_contract_gate(
                        shot_dir,
                        str(layer_id),
                        frame=int(args["frame"]),
                        ref=str(ref),
                        render=r["image_path"],
                        evidence_ids=comparison_state.get("active_image_evidence_ids"),
                    )
                )
                passed_rows = sum(bool(row.get("pass")) for row in gate_rows)
                gate_note = f"\nAUTHORITATIVE IMAGE CONTRACTS: {passed_rows}/{len(gate_rows)} pass"
                if gate_pass:
                    comparison_state["pixel_contracts_passed"] = True
                    if comparison_state.get("current_scene_contracts_present"):
                        comparison_state["scene_contracts_passed"] = True
                        gate_note += (
                            "\nCRITIC HANDOFF READY: current-layer scene and image "
                            "contracts pass. Do not mutate again without critic-backed evidence."
                        )
                    else:
                        gate_note += (
                            "\nIMAGE CONTRACTS PASS, but this layer has no scene "
                            "completion contract. Mutation remains open; finish the scoped "
                            "work, verify it, then hand off voluntarily to the critic."
                        )
                else:
                    comparison_state["scene_contracts_passed"] = False
                    comparison_state["pixel_contracts_passed"] = False
                    failed_rows = [row for row in gate_rows if not row.get("pass")]
                    gate_note += " · failing:\n" + "\n".join(
                        f"  {row.get('id')}: {row.get('metric')}={row.get('value')} target {row.get('target')}"
                        for row in failed_rows[:6]
                    )
                    gate_note += (
                        "\nONE IMAGE-CONTRACT REPAIR REOPENED: fix only the failed property, then compare again."
                    )
            except Exception as exc:
                gate_note = (
                    f"\n⚠ image-contract gate unavailable: {type(exc).__name__}: "
                    f"{str(exc)[:100]}; critic handoff remains closed"
                )
        if crop is None:
            out = _compare_image(
                r["image_path"],
                ref_path,
                f"frame {r['frame']} ({mode})  vs  {ref}" + _warn_suffix(r),
                feedback_groups=feedback_policy["groups"],
            )
        else:
            try:
                zoom = await _call(
                    "render", frame=int(args["frame"]), mode=mode, scale=scale, crop=crop, res_pct=res_pct
                )
                stem = f"compare_focus_f{int(args['frame']):04d}"
                context_path = session.artifacts / f"{stem}_context.jpg"
                focus_path = session.artifacts / f"{stem}_detail.jpg"
                context = save_context_sheet(r["image_path"], ref_path, crop, context_path)
                focus = save_focus_sheet(zoom["image_path"], ref_path, crop, focus_path, views)
            except (BlenderError, OSError, ValueError) as exc:
                return _text(f"focus comparison failed: {exc}", is_error=True)
            candidate_crop = Image.open(zoom["image_path"]).convert("RGB")
            reference_crop = Image.open(ref_path).convert("RGB")
            reference_crop = crop_pixels(reference_crop, crop)
            # Crop metrics share one DOWNSTREAM geometry and never enlarge the smaller
            # source. Otherwise a tiny reference crop is interpolated to look smoother
            # than an optical candidate crop and the detail comparison is biased again.
            metric_h = min(_METRIC_H, candidate_crop.height, reference_crop.height)
            metric_aspect = min(
                candidate_crop.width / max(candidate_crop.height, 1),
                reference_crop.width / max(reference_crop.height, 1),
            )
            metric_size = (max(1, round(metric_h * metric_aspect)), max(1, metric_h))
            candidate_metric = candidate_crop.resize(metric_size, Image.Resampling.LANCZOS)
            reference_metric = reference_crop.resize(metric_size, Image.Resampling.LANCZOS)
            text = (
                f"frame {r['frame']} ({mode}) focus comparison vs {ref}\n"
                f"crop {crop} (normalized TOP-LEFT) · optical res_pct={res_pct} · "
                f"views={focus['views']}\n"
                f"FIRST IMAGE: mandatory full-frame context with the inspected region "
                f"outlined. SECOND IMAGE: aligned focus views.\n"
                f"candidate crop source {focus['candidate_source_px']} · reference crop "
                f"{focus['reference_crop_px']} · compared at {focus['comparison_px']} · "
                f"upscaled={focus['upscaled']} · mean abs diff {focus['mean_abs_diff']}/255\n"
                f"{_stats(candidate_metric, feedback_groups=feedback_policy['groups'])}\n"
                f"{_metrics_line(candidate_metric, reference_metric, feedback_groups=feedback_policy['groups'])}"
                + _warn_suffix(zoom)
            )
            out = {
                "content": [
                    {"type": "text", "text": text},
                    {"type": "image", "data": _b64(_load(context["image_path"])), "mimeType": "image/jpeg"},
                    {"type": "image", "data": _b64(_load(focus["image_path"])), "mimeType": "image/jpeg"},
                ]
            }
        if gate_note:
            out["content"][0]["text"] += gate_note
        return out

    @tool(
        "render_frames",
        "See several frames at once (the milestone contact sheet) — pass the frame list, "
        "e.g. [1,24,32,48]. Same mode/scale semantics as render_frame. Returns one image "
        "per frame so you can compare timing/colour against the refs.",
        {
            "type": "object",
            "properties": {
                "frames": {"type": "array", "items": {"type": "integer"}},
                "mode": {"type": "string", "enum": ["solid", "wire", "eevee"]},
                "scale": {"type": "number"},
            },
            "required": ["frames"],
        },
    )
    async def render_frames(args):
        mode = args.get("mode", "eevee")
        scale = float(args.get("scale", 0.35))
        blocks: list[dict] = []
        for f in args["frames"]:
            try:
                r = await _call("render", frame=int(f), mode=mode, scale=scale)
                im = _load(r["image_path"])
                blocks.append({"type": "text", "text": f"frame {r['frame']} ({mode})\n{_stats(im)}" + _warn_suffix(r)})
                blocks.append({"type": "image", "data": _b64(im), "mimeType": "image/jpeg"})
            except BlenderError as e:
                blocks.append({"type": "text", "text": f"frame {f}: ERROR {e}"})
        return {"content": blocks}

    @tool(
        "import_asset",
        "Import a committed, normalized 3D asset into the live scene. The mesh was "
        "frozen upstream by the asset stage (assets/<name>/model.glb), base at the "
        "origin and scaled to a known height — deterministic. Returns the names of the "
        "objects it added, which you then shade/emit, place and animate. Prefer this "
        "over modelling a bespoke hero prop by hand.",
        {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    )
    async def import_asset(args):
        if assets_dir is None:
            return _text("import_asset unavailable: no assets_dir configured", is_error=True)
        name = args["name"]
        glb = assets_dir / name / "model.glb"
        meta = assets_dir / name / "meta.json"
        if not glb.is_file():
            avail = (
                sorted(p.name for p in assets_dir.iterdir() if (p / "model.glb").is_file())
                if assets_dir.is_dir()
                else []
            )
            hint = f" Available assets: {avail}" if avail else " No assets are prepared."
            return _text(f"no asset {name!r}.{hint}", is_error=True)
        script = (
            "import bpy\n"
            "before = set(bpy.data.objects.keys())\n"
            f"bpy.ops.import_scene.gltf(filepath={str(glb)!r})\n"
            "RESULT = [n for n in bpy.data.objects.keys() if n not in before]\n"
        )
        try:
            r = await _call("run", code=script)
        except BlenderError as e:
            return _text(str(e), is_error=True)
        dims = json.loads(meta.read_text()).get("bbox_dims") if meta.is_file() else None
        return _text(f"imported {name}: objects={r.get('result')} bbox_dims={dims}")

    @tool(
        "probe_control",
        "Transactionally sweep 2-8 numeric values on one semantic shader/compositor "
        "control. The tool renders every value with locked settings, compares it with "
        "the reference (optionally at a true optical crop), returns a numeric table and "
        "best split panel, then ALWAYS restores the original value. Use this instead of "
        "a sequence of trial/revert run_bpy edits. Commit the chosen value once afterward.",
        {
            "type": "object",
            "properties": {
                "graph": {"type": "string", "enum": ["material", "compositor", "world"]},
                "material_role": {"type": "string"},
                "node_role": {"type": "string"},
                "socket": {"type": "string"},
                "socket_index": {"type": "integer"},
                "socket_direction": {"type": "string", "enum": ["auto", "input", "output"]},
                "values": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 8},
                "frame": {"type": "integer"},
                "reference": {"type": "string"},
                "crop": {"type": "array", "items": {"type": "number"}},
                "mode": {"type": "string", "enum": ["draft", "eevee"]},
                "scale": {"type": "number"},
                "res_pct": {"type": "integer"},
            },
            "required": ["graph", "node_role", "values", "frame", "reference"],
        },
    )
    async def probe_control(args):
        if shot_dir is None:
            return _text("probe_control requires a shot directory", is_error=True)
        values = [float(v) for v in args.get("values") or []]
        if not 2 <= len(values) <= 8 or not all(math.isfinite(v) for v in values):
            return _text("values must contain 2-8 finite numbers", is_error=True)
        crop = args.get("crop")
        try:
            crop = list(validate_crop(crop)) if crop is not None else None
        except (TypeError, ValueError) as exc:
            return _text(str(exc), is_error=True)
        if args["graph"] == "material" and not args.get("material_role"):
            return _text("material graph probes require material_role", is_error=True)
        ref_path = shot_dir / str(args["reference"])
        if not ref_path.is_file():
            return _text(f"reference not found: {args['reference']}", is_error=True)
        mode = args.get("mode", "eevee")
        scale = float(args.get("scale", 0.5))
        res_pct = int(args.get("res_pct", 400 if crop else 100))
        key = (comparison_state.get("round", 1), int(args["frame"]), "probe", tuple(crop or ()))
        settings = (mode, scale, res_pct)
        lock_error = _comparison_lock_error(comparison_locks, key, settings)
        if lock_error:
            return _text(lock_error, is_error=True)

        selector = json.dumps(
            {
                k: args.get(k)
                for k in (
                    "graph",
                    "material_role",
                    "node_role",
                    "socket",
                    "socket_index",
                    "socket_direction",
                )
            }
        )

        def control_script(value=None):
            payload = json.dumps(value)
            return f"""\
import bpy, fnmatch, json
spec=json.loads({json.dumps(selector)})
graphs=[]
if spec['graph']=='material':
    mats=[m for m in bpy.data.materials
          if fnmatch.fnmatchcase(str(m.get('bvfx_role','')),spec['material_role'])]
    graphs=[m.node_tree for m in mats if m.node_tree]
elif spec['graph']=='compositor':
    ng=getattr(bpy.context.scene,'compositing_node_group',None); graphs=[ng] if ng else []
else:
    nt=bpy.context.scene.world.node_tree if bpy.context.scene.world and bpy.context.scene.world.use_nodes else None
    graphs=[nt] if nt else []
nodes=[n for nt in graphs for n in nt.nodes
       if fnmatch.fnmatchcase(
           str(n.get('bvfx_control') or n.get('bvfx_role') or ''),spec['node_role'])]
if len(nodes)!=1: raise ValueError(f"semantic control matched {{len(nodes)}} nodes")
direction=spec.get('socket_direction') or 'auto'
collections=(
    [('input',nodes[0].inputs)] if direction=='input' else
    [('output',nodes[0].outputs)] if direction=='output' else
    [('input',nodes[0].inputs),('output',nodes[0].outputs)]
)
socket=None; resolved_direction=None
for candidate_direction,sockets in collections:
    try:
        candidate=(sockets[int(spec['socket_index'])]
                   if spec.get('socket_index') is not None
                   else sockets.get(spec.get('socket') or 'Value'))
    except IndexError:
        candidate=None
    if candidate is not None:
        socket=candidate; resolved_direction=candidate_direction; break
if socket is None: raise ValueError(f"semantic control has no requested {{direction}} socket")
before=float(socket.default_value)
new=json.loads({json.dumps(payload)})
if new is not None: socket.default_value=float(new)
RESULT={{'before':before,'after':float(socket.default_value),'node':nodes[0].name,
        'socket':socket.name,'socket_direction':resolved_direction}}
"""

        try:
            initial = await anyio.to_thread.run_sync(lambda: session.run(control_script(), journal=False))
            original = float((initial.get("result") or {})["before"])
        except Exception as exc:
            return _text(f"control resolution failed: {exc}", is_error=True)
        rows = []
        best_path = None
        try:
            reference = Image.open(ref_path).convert("RGB")
            reference = crop_pixels(reference, crop) if crop else reference
            for index, value in enumerate(values):
                await anyio.to_thread.run_sync(lambda v=value: session.run(control_script(v), journal=False))
                rendered = await anyio.to_thread.run_sync(
                    lambda: session.render_full(
                        frame=int(args["frame"]), mode=mode, scale=scale, crop=crop, res_pct=res_pct
                    )
                )
                candidate = Image.open(rendered["image_path"]).convert("RGB")
                probe_path = session.artifacts / (
                    f"probe_{re.sub(r'[^a-zA-Z0-9_-]+', '_', str(args['node_role']))[:40]}_"
                    f"f{int(args['frame']):04d}_{index}.png"
                )
                candidate.save(probe_path)
                w = min(candidate.width, reference.width)
                h = min(candidate.height, reference.height)
                size = (max(1, w), max(1, h))
                c = candidate.resize(size, Image.Resampling.LANCZOS)
                r = reference.resize(size, Image.Resampling.LANCZOS)
                from PIL import ImageChops, ImageStat

                mae = ImageStat.Stat(ImageChops.difference(c, r).convert("L")).mean[0]
                cm, rm = _region_metrics(c), _region_metrics(r)
                rows.append(
                    {
                        "value": value,
                        "mae": round(mae, 3),
                        "mean": round(sum(c.convert("L").getdata()) / (c.width * c.height), 2),
                        "ref_mean": round(sum(r.convert("L").getdata()) / (r.width * r.height), 2),
                        "sigma": round(cm["bands"]["mid"][1], 2),
                        "ref_sigma": round(rm["bands"]["mid"][1], 2),
                        "path": str(probe_path),
                    }
                )
            best = min(rows, key=lambda row: row["mae"])
            best_path = best["path"]
        except Exception as exc:
            return _text(f"control sweep failed: {exc}", is_error=True)
        finally:
            with contextlib.suppress(Exception):
                await anyio.to_thread.run_sync(lambda: session.run(control_script(original), journal=False))

        candidate = Image.open(best_path).convert("RGB")
        reference = Image.open(ref_path).convert("RGB")
        reference = crop_pixels(reference, crop) if crop else reference
        height = min(768, candidate.height, reference.height)
        cand = candidate.resize((max(1, round(candidate.width * height / candidate.height)), height))
        ref = reference.resize((max(1, round(reference.width * height / reference.height)), height))
        sheet = Image.new("RGB", (cand.width + ref.width, height), (18, 18, 22))
        sheet.paste(cand, (0, 0))
        sheet.paste(ref, (cand.width, 0))
        from vfx_harness.evidence.compare_panels import mark_pair

        sheet = mark_pair(sheet, cand.width, "BEST PROBE — LEFT", "REFERENCE — RIGHT")
        lines = [f"semantic control sweep; original {original:g} RESTORED", "value | MAE | mean/ref | mid-sigma/ref"]
        lines += [
            f"{row['value']:g} | {row['mae']:.3f} | {row['mean']}/{row['ref_mean']} | {row['sigma']}/{row['ref_sigma']}"
            for row in rows
        ]
        lines.append(
            f"lowest pixel MAE: {min(rows, key=lambda r: r['mae'])['value']:g}; "
            "choose by owned contracts and the panel, then commit once with run_bpy"
        )
        return {
            "content": [
                {"type": "text", "text": "\n".join(lines)},
                {"type": "image", "data": _b64(sheet), "mimeType": "image/jpeg"},
            ]
        }

    tools = [
        run_bpy,
        inspect_scene,
        inspect_nodes,
        list_keyframes,
        render_frame,
        render_frames,
        render_pass,
        check_scene,
        diff_frames,
        verify_change,
        probe_control,
    ]
    if shot_dir is not None:
        tools.append(compare_frame)
    if assets_dir is not None:
        tools.append(import_asset)

    @tool(
        "script_map",
        "STRUCTURAL INDEX of a build script — functions, sections, and which lines create "
        "or reference each named object/material. Use this INSTEAD of reading the whole "
        "file: a 536-line script maps to ~380 tokens. Then Read just that span and Edit "
        "it. Never rewrite a script you only need to change in one place.",
        {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    )
    async def script_map(args):
        p = Path(args["path"])
        if not p.is_absolute() and shot_dir:
            p = shot_dir / p
        return {"content": [{"type": "text", "text": _outline(p)}]}

    @tool(
        "find_in_script",
        "Locate a name/value inside a build script with surrounding context, so you can "
        "Read the right span instead of the whole file. Give the object name, material "
        "name, or literal you want to change.",
        {
            "type": "object",
            "properties": {"path": {"type": "string"}, "needle": {"type": "string"}},
            "required": ["path", "needle"],
        },
    )
    async def find_in_script(args):
        p = Path(args["path"])
        if not p.is_absolute() and shot_dir:
            p = shot_dir / p
        return {"content": [{"type": "text", "text": _find_lines(p, args["needle"])}]}

    @tool(
        "ask_supervisor",
        "Raise a question you cannot resolve from the brief, the stills or the plan — an "
        "ambiguity, a contradiction, or a judgement call that is genuinely the client's. "
        "This does NOT block: state the assumption you will proceed on and keep building. "
        "Use it INSTEAD of guessing silently, and instead of tuning against a target you "
        "are not sure about. Do not use it for things you could measure or spike.",
        {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "assumption": {"type": "string"},
                "why_it_matters": {"type": "string"},
                "affected_layers": {"type": "array", "items": {"type": "string"}},
                "affected_axes": {"type": "array", "items": {"type": "string"}},
                "global_decision": {"type": "boolean"},
            },
            "required": ["question", "assumption", "affected_layers"],
        },
    )
    async def ask_supervisor(args):
        if not shot_dir:
            return {"content": [{"type": "text", "text": "no shot folder — cannot ask"}]}
        qid = _ask(
            shot_dir,
            layer=layer_id or "?",
            question=args["question"],
            assumption=args["assumption"],
            why_it_matters=args.get("why_it_matters", ""),
            affected_layers=args.get("affected_layers") or [],
            affected_axes=args.get("affected_axes") or [],
            global_decision=bool(args.get("global_decision")),
        )
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Recorded as Q{qid}. Continue on your stated assumption: {args['assumption']}",
                }
            ]
        }

    @tool(
        "worklist",
        "Your build checklist ON DISK — it survives context compaction and process death, "
        "which your memory does not. Call with items=[...] to (re)write it, or done=[...] "
        "to tick things off; call with neither to read it back. Write it once at the start "
        "from your layer's tickets, then tick as you go. Layer G was killed at turn 121 with "
        "the work half-finished and no record of what remained.",
        {
            "type": "object",
            "properties": {
                "items": {"type": "array", "items": {"type": "string"}},
                "done": {"type": "array", "items": {"type": "string"}},
                "note": {"type": "string"},
            },
            "required": [],
        },
    )
    async def worklist(args):
        if not shot_dir:
            return {"content": [{"type": "text", "text": "no shot folder"}]}
        layer_part = str(layer_id or "layer")
        wl = run_artifacts.shot_state_dir(shot_dir) / "worklists" / f"layer-{layer_part}.json"
        wl.parent.mkdir(parents=True, exist_ok=True)
        state = (json.loads(wl.read_text()) if wl.is_file()
                 else {"items": [], "done": [], "notes": []})
        if args.get("items"):
            # A new attempt may restate its tickets, but it cannot erase an unresolved
            # item discovered by the previous attempt.  Carry those forward until they
            # are explicitly completed; this is the durable feedback loop across both
            # context compaction and full process restarts.
            _merge_worklist_items(state, list(args["items"]))
        for d in args.get("done", []):
            if d not in state["done"]:
                state["done"].append(d)
        if args.get("note"):
            state["notes"].append(args["note"])
        wl.write_text(json.dumps(state, indent=2))
        left = [i for i in state["items"] if i not in state["done"]]
        body = (
            "\n".join(f"  [x] {i}" for i in state["items"] if i in state["done"])
            + "\n"
            + "\n".join(f"  [ ] {i}" for i in left)
        ).strip()
        return {
            "content": [
                {"type": "text", "text": f"{len(state['done'])}/{len(state['items'])} done, {len(left)} left\n{body}"}
            ]
        }

    @tool(
        "measure_regions",
        "Measure named RECTANGLES of a rendered frame and compare them. Use this to "
        "prove a structural claim numerically instead of eyeballing it — 'the outer "
        "window strips are brighter than the recessed core', 'the sign LETTERS are "
        "brighter than the panel behind them'. Regions are in NORMALISED frame "
        "coordinates [x0,y0,x1,y1], each 0..1, origin TOP-LEFT. Returns mean/σ/max/lit%% "
        "per region plus every pairwise brightness ratio, so you never slice pixels "
        "yourself.",
        {
            "type": "object",
            "properties": {
                "frame": {"type": "integer"},
                "regions": {
                    "type": "object",
                    "description": "name -> [x0,y0,x1,y1] in 0..1, e.g. "
                    '{"left_strip":[0.42,0.2,0.46,0.8], '
                    '"core":[0.47,0.2,0.53,0.8]}',
                    "additionalProperties": {"type": "array", "items": {"type": "number"}},
                },
                "mode": {"type": "string", "enum": ["solid", "wire", "draft", "eevee"]},
                "scale": {"type": "number"},
            },
            "required": ["frame", "regions"],
        },
    )
    async def measure_regions(args):
        """Exists because the builder was writing its own measurement rig every layer.

        Asked to prove 'the outer quarters are brighter than the central half', it had no
        tool for it, so it hand-rolled `bpy.ops.render.render(write_still=True)` plus numpy
        pixel slicing INSIDE run_bpy — which produced two distinct crashes in one layer
        (a zero-size reduction and a 28-vs-31 concatenation), bypassed the session's render
        path so the metrics hook never saw those frames, and mutated
        scene.render.resolution_* on the live scene, where an exception between set and
        restore leaves the deliverable rendering at the wrong size.
        """
        regions = args.get("regions") or {}
        if not regions:
            return _text("no regions given", is_error=True)
        bad = [
            n
            for n, r in regions.items()
            if not (
                isinstance(r, list)
                and len(r) == 4
                and all(isinstance(v, (int, float)) and 0.0 <= v <= 1.0 for v in r)
                and r[0] < r[2]
                and r[1] < r[3]
            )
        ]
        if bad:
            return _text(f"regions must be [x0,y0,x1,y1] in 0..1 with x0<x2 and y0<y1; bad: {bad}", is_error=True)
        try:
            r = await _call(
                "render", frame=int(args["frame"]), mode=args.get("mode", "eevee"), scale=float(args.get("scale", 0.5))
            )
        except BlenderError as e:
            return _text(str(e), is_error=True)

        im = Image.open(r["image_path"]).convert("RGB")
        g = im.convert("L")
        W, H = g.size
        out = {}
        for name, (x0, y0, x1, y1) in regions.items():
            box = (
                max(0, int(x0 * W)),
                max(0, int(y0 * H)),
                min(W, max(int(x1 * W), int(x0 * W) + 1)),
                min(H, max(int(y1 * H), int(y0 * H) + 1)),
            )
            px = list(g.crop(box).getdata())
            n = len(px) or 1
            mean = sum(px) / n
            sd = (sum((p - mean) ** 2 for p in px) / n) ** 0.5
            out[name] = {
                "mean": round(mean, 1),
                "sd": round(sd, 1),
                "max": max(px),
                "lit_pct": round(100 * sum(1 for p in px if p >= 120) / n, 1),
                "px": n,
            }
        lines = [f"frame {r['frame']} ({r['mode']}) — {W}x{H}"]
        for name, v in out.items():
            lines.append(
                f"  {name:<16} mean {v['mean']:>5} · σ {v['sd']:>5} · "
                f"max {v['max']:>3} · lit {v['lit_pct']:>5}% · {v['px']}px"
            )
        names = list(out)
        if len(names) > 1:
            lines.append("  ratios (a/b by mean brightness):")
            for i, a in enumerate(names):
                for b in names[i + 1 :]:
                    ma, mb = out[a]["mean"], out[b]["mean"]
                    rel = ma / mb if mb > 0.5 else float("inf")
                    verdict = "BRIGHTER" if ma > mb * 1.05 else "DARKER" if mb > ma * 1.05 else "about EQUAL"
                    lines.append(f"    {a} is {verdict} than {b}  ({ma} vs {mb}, ×{rel:.2f})")
        for name, v in out.items():
            if v["px"] < 64:
                lines.append(
                    f"  ⚠ {name} is only {v['px']}px — too small to measure reliably; widen the region or raise scale"
                )
        return {"content": [{"type": "text", "text": "\n".join(lines)}]}

    @tool(
        "propose_checks",
        "Record what you learned about VERIFYING this layer, as executable checks. You are "
        "the only stage with the built scene. Runtime evidence is evaluation-only and is "
        "not execution authority; do not read runtime_checks.json. Propose only an "
        "evidence gap you actually discovered.\n"
        "Each check must PASS on your render and FAIL on the state before your layer ran — "
        "that is what proves your layer did the work, and it is why this cannot be gamed: "
        "you do not choose the adversary, the previous layer's render is.\n"
        "after and before are existing image artifact paths relative to the shot folder, "
        "never descriptions or labels. Survivors are appended to the "
        "runtime_checks.json evidence ledger; planner contracts remain immutable in "
        "checks.json. Propose few and real.",
        {
            "type": "object",
            "properties": {
                "checks": {
                    "type": "array",
                    "maxItems": 20,
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "metric": {"type": "string", "enum": sorted(METRICS)},
                            "op": {"type": "string", "enum": [">=", "<=", "band"]},
                            "lo": {"type": "number"},
                            "hi": {"type": "number"},
                            "frame": {"type": "integer"},
                            "axis": {"type": "string"},
                            "stage": {"type": "string", "enum": ["pre_grade", "post_grade", "any"]},
                            "ref": {"type": "string"},
                            "regions": {
                                "type": "object",
                                "additionalProperties": {
                                    "type": "array",
                                    "minItems": 4,
                                    "maxItems": 4,
                                    "items": {"type": "number"},
                                },
                            },
                            "note": {"type": "string"},
                        },
                        "required": ["id", "metric", "op"],
                        "additionalProperties": False,
                    },
                },
                "after": {"type": "string", "description": "Existing candidate image path relative to shot"},
                "before": {"type": "string", "description": "Existing pre-layer image path relative to shot"},
            },
            "required": ["checks", "after"],
        },
    )
    async def propose_checks(args):
        from vfx_harness.domain.work_units import read_document
        from vfx_harness.evidence.checks import Check, verify_necessity

        if not shot_dir:
            return _text("propose_checks needs a shot dir", is_error=True)
        root = Path(shot_dir)
        after = root / args["after"]
        if not after.is_file():
            available = sorted(
                (p for base in (session.artifacts, run_artifacts.renders_dir(root))
                 if base.is_dir() for p in base.glob("*.png")),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )[:6]
            hint = ", ".join(p.relative_to(root).as_posix() for p in available) or "none"
            return _text(
                f"render path {args['after']!r} does not exist. Pass an existing image "
                f"artifact path, not a description. Recent candidates: {hint}",
                is_error=True,
            )
        before = (root / args["before"]) if args.get("before") else None
        # Every check must name the plate it is about, or the gate cannot re-run it. The
        # first version of this tool took `after`/`before` renders and never populated
        # `ref`, so four good builder checks landed in the runtime evidence ledger and all
        # four failed validation on plumbing rather than on merit.
        judge: dict[int, str] = {}
        first_ref = ""
        try:
            for lay in read_document(root / "layers.json"):
                if str(lay.get("id")) != str(layer_id):
                    continue
                js = lay.get("judge") or []
                judge = {int(j["frame"]): j["ref"] for j in js if j.get("ref")}
                primary = lay.get("primary_judge")
                first_ref = next((j.get("ref", "") for j in js if j.get("frame") == primary), "")
        except Exception as e:
            return _text(f"could not read judge refs from layers.json: {str(e)[:100]}", is_error=True)
        kept, lines = [], []
        for d in list(args.get("checks") or [])[:20]:
            cid = str(d.get("id", "?"))
            try:
                ref_rel = (
                    d.get("ref") or judge.get(int(d["frame"])) if d.get("frame") is not None else d.get("ref")
                ) or first_ref
                d = {**d, "ref": ref_rel}
                c = Check.from_dict(
                    {
                        **d,
                        "layer": str(layer_id or d.get("layer", "")),
                        "lo": d.get("lo", float("-inf")),
                        "hi": d.get("hi", float("inf")),
                    }
                )
                if not ref_rel:
                    lines.append(f"  REJECTED {cid:10} no judge frame to name as its ref")
                    continue
                v = verify_necessity(c, after, before)
            except Exception as e:
                lines.append(f"  REJECTED {cid:10} {str(e)[:80]}")
                continue
            if v.ok:
                # Free-form builder notes are never persisted as future execution
                # authority. Keep only executable fields plus harness-generated proof.
                safe = {k: value for k, value in d.items() if k != "note"}
                kept.append(
                    {
                        **safe,
                        "layer": str(layer_id or d.get("layer", "")),
                        # Record the render this was proven against. A later attempt
                        # replaces the renders, and a proof that does not say which
                        # picture it came from cannot be told apart from a wrong one.
                        "proof": {
                            "ref": round(v.ref_value, 4),
                            "adversary": [round(x, 4) for x in v.bad_values[:1]],
                            "on": args["after"],
                        },
                        "origin": "builder",
                        # No prior layer means no adversary — the FIRST layer's checks
                        # are the least verified in the system, and saying so is the
                        # point. Silence here would let them count as adversaried.
                        **({"note": "[no prior-layer adversary: first layer]"} if not v.bad_values else {}),
                    }
                )
                lines.append(
                    f"  KEPT     {cid:10} {c.metric} {c.target()} · after "
                    f"{v.ref_value:.4g}" + (f" · before {v.bad_values[0]:.4g}" if v.bad_values else "")
                )
            else:
                lines.append(f"  REJECTED {cid:10} {v.reasons[0][:120]}")
        if kept:
            spec = root / "runtime_checks.json"
            cur = json.loads(spec.read_text()) if spec.is_file() else []
            have = {x.get("id") for x in cur}
            cur += [k for k in kept if k.get("id") not in have]
            spec.write_text(json.dumps(cur, indent=1) + "\n", encoding="utf-8")
        return _text(f"{len(kept)} check(s) added to runtime_checks.json.\n" + "\n".join(lines))

    # ask_supervisor is deliberately PLAN-ONLY: a layer that discovers an
    # ambiguity is already building on earlier layers' answer to it.
    tools = [*tools, script_map, find_in_script, worklist, measure_regions, propose_checks]
    server = create_sdk_mcp_server(name=SERVER_NAME, version="0.1.0", tools=tools)
    names = [f"mcp__{SERVER_NAME}__{t.name}" for t in tools]
    return server, names
