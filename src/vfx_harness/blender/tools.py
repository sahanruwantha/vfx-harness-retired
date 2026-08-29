"""Agent SDK tools over a warm BlenderSession.

`build_blender_tools(session)` returns an in-process MCP server and the list of
qualified tool names to put in `ClaudeAgentOptions.allowed_tools`. Render tools
return image content blocks, so the agent literally sees the frames it makes.
"""

from __future__ import annotations

import base64
import contextlib
import fnmatch
import hashlib
import io
import json
import math
import re
import shutil
from collections.abc import Mapping
from pathlib import Path

import anyio
from claude_agent_sdk import create_sdk_mcp_server, tool
from PIL import Image

from vfx_harness.domain.atomicity import LIVE_WRITE_FAMILY_RULE, script_write_family_evidence
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


def _run_bpy_write_family_error(source: str, unit_scope: Mapping | None) -> str:
    """Refuse a payload whose typed Blender calls exceed compiled unit authority."""
    if not unit_scope:
        return ""
    clusters = [
        row
        for row in (unit_scope.get("write_clusters") or [])
        if isinstance(row, Mapping) and row.get("instrument_family")
    ]
    if len(clusters) != 1:
        labels = [
            "/".join(
                str(row.get(key) or "")
                for key in ("role_namespace", "host_class", "instrument_family")
            )
            for row in clusters
        ]
        return (
            "BLOCKED: active unit does not have exactly one derived write-cluster; "
            f"found {labels or ['(none)']}. Rematerialize or split the unit before "
            f"mutating Blender. {LIVE_WRITE_FAMILY_RULE}"
        )
    try:
        evidence = script_write_family_evidence(source)
    except SyntaxError as exc:
        return f"BLOCKED: run_bpy payload is not valid Python at line {exc.lineno}: {exc.msg}"
    planned = str(clusters[0]["instrument_family"])
    allowed = {planned}
    if planned == "camera":
        allowed.add("keyframe")
    mutates = unit_scope.get("mutates") or {}
    if isinstance(mutates, Mapping) and mutates.get("dresses"):
        allowed.add("shading")
    illegal = [item for item in evidence if item.family not in allowed]
    if not illegal:
        return ""
    observed = ", ".join(item.label() for item in evidence)
    return (
        "BLOCKED: run_bpy payload exceeds the active unit's derived write family "
        f"{planned!r}; detected {observed}. Allowed families for this unit: "
        + ", ".join(sorted(allowed))
        + ". Split or rematerialize the work; semantic role tags cannot make mixed "
        f"mutation legal. {LIVE_WRITE_FAMILY_RULE}"
    )


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _parent_chain_hash(prior_paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in prior_paths:
        resolved = Path(path).resolve()
        digest.update(resolved.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(resolved.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _capture_image_artifact(
    *,
    shot_dir: Path,
    source: str | Path,
    frame: int,
    mode: str,
    scale: float,
    resolution: list[int] | tuple[int, ...] | None,
    role: str,
    unit_id: str,
    parent_chain_hash: str,
) -> dict:
    """Copy one plate to immutable run evidence and return a non-path agent handle."""
    src = Path(source)
    sha = _sha256_file(src)
    layout = run_artifacts.ensure(shot_dir, command="build")
    safe_unit = re.sub(r"[^A-Za-z0-9_.-]+", "_", unit_id or "unit")[:60]
    safe_role = re.sub(r"[^A-Za-z0-9_.-]+", "_", role)[:40]
    dest = run_artifacts.renders_dir(shot_dir) / (
        f"{safe_unit}_{safe_role}_f{int(frame):04d}_{sha[:16]}.png"
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.is_file():
        shutil.copyfile(src, dest)
    handle = f"image:{safe_role}:f{int(frame)}:{sha[:16]}"
    return {
        "handle": handle,
        "path": dest.relative_to(shot_dir).as_posix(),
        "sha256": sha,
        "run_id": layout.run_id,
        "frame": int(frame),
        "mode": str(mode),
        "scale": float(scale),
        "resolution": list(resolution or []),
        "role": role,
        "unit_id": unit_id,
        "parent_chain_hash": parent_chain_hash,
    }


def _payment_eligible_candidate(rendered: dict) -> bool:
    """Whether a live render can share the fixed v2 adversary settings."""
    return str(rendered.get("mode")) == "eevee" and float(rendered.get("scale", 0.0)) == 0.5


def capture_image_adversaries(
    session: BlenderSession,
    shot_dir: str | Path,
    comparison_state: dict,
    prior_paths: list[Path],
) -> dict[int, dict]:
    """Render the pre-unit chain once; the model never chooses its own adversary."""
    debts = list(comparison_state.get("image_debts") or [])
    if not debts:
        comparison_state["image_adversaries"] = {}
        return {}
    root = Path(shot_dir)
    parent_hash = _parent_chain_hash(prior_paths)
    comparison_state["parent_chain_hash"] = parent_hash
    records: dict[int, dict] = {}
    registry = comparison_state.setdefault("image_artifacts", {})
    for frame in sorted({int(row["frame"]) for row in debts}):
        rendered = session.call("render", frame=frame, mode="eevee", scale=0.5)
        record = _capture_image_artifact(
            shot_dir=root,
            source=rendered["image_path"],
            frame=frame,
            mode="eevee",
            scale=0.5,
            resolution=rendered.get("resolution"),
            role="pre_unit_adversary",
            unit_id=str(comparison_state.get("unit_id") or "unit"),
            parent_chain_hash=parent_hash,
        )
        records[frame] = record
        registry[record["handle"]] = record
    comparison_state["image_adversaries"] = records
    return records


def _text(s: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": s}], **({"is_error": True} if is_error else {})}


def _merge_worklist_items(state: dict, new_items: list[str]) -> dict:
    """Carry unresolved items across a rewritten checklist without stale completions."""
    old_done = set(state.get("done", []))
    unfinished = [item for item in state.get("items", []) if item not in old_done]
    state["items"] = list(dict.fromkeys([*unfinished, *new_items]))
    state["done"] = [item for item in state.get("done", []) if item in state["items"]]
    return state


def _refresh_unpaid_image_debts(comparison_state: dict, shot_dir: str | Path | None) -> list[dict]:
    """Recompute unpaid image-contract debts from disk after propose_checks / mutation."""
    from vfx_harness.domain.image_debts import (
        debts_from_dicts,
        unpaid_image_contract_debts,
    )
    from vfx_harness.evidence.checks import load_image_contract_payment_rows

    cards = debts_from_dicts(comparison_state.get("image_debts"))
    if not cards or not shot_dir:
        comparison_state["unpaid_image_debts"] = []
        return []
    unpaid = [
        card.as_dict()
        for card in unpaid_image_contract_debts(
            cards, load_image_contract_payment_rows(shot_dir)
        )
    ]
    comparison_state["unpaid_image_debts"] = unpaid
    return unpaid


def _scene_completion_state(
    evidence: list[dict], layer_id: str, required_ids: set[str] | None = None
) -> dict:
    """Separate healthy inherited inputs from authority to seal the current layer.

    Persistent upstream interfaces must pass, but they cannot prove that a downstream
    department has performed its own work.  Only an active contract owned by the current
    layer may close the live mutation gate.  Layers with image-only/subjective completion
    keep mutation open until they voluntarily hand off to the critic.

    `required_ids` are the SCENE contracts bound to the active unit's required claims
    (HIR-0048: image-contract debts are a separate card, not a scene-selector miss).
    A contract that is never evaluated — selector matched nothing, probe errored, frame
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
        line += (
            "  ⚠ frame almost entirely black — on draft/EEVEE use the typed scene "
            "cause card before changing energy, density, or exposure; on solid/wire "
            "inspect framing and visibility because Workbench does not test lighting"
        )
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
        span = r.get("peak_speed_span") or []
        peak = (
            f"f{span[0]}→f{span[1]}"
            if isinstance(span, list) and len(span) == 2
            else f"f{r.get('peak_speed_frame')}"
        )
        lines.append(
            f"  max speed {r.get('max_speed')} u/f ({peak}) · "
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


def _scope_offenders(
    manifest: dict, allowed: tuple[str, ...], baseline: set[str] | None = None
) -> list[str]:
    """Objects whose semantic role falls outside the unit's declared authority.

    Reported on every call until fixed, never once-and-forgotten: builders create first
    and tag second, so suppressing a name after first sight hides an untagged object for
    the rest of the build — which is how an untagged curve reached canonical replay.

    ``baseline`` is the manifest at unit start (after priors replayed): prior layers'
    objects are THEIR authority, not this unit's violation. Without it every later unit
    was told to delete the previous layers' work (runs 20260825T022805Z/023xxx flagged
    layer 1's proxies against every layer-2 unit, advice that would have destroyed
    sealed geometry if followed)."""
    return [
        f"{name!r} role={str(role) or '<none>'}"
        for name, role in sorted(manifest.items())
        if name not in (baseline or set()) and not _role_in_scope(str(role), allowed)
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


def preview_render_mode(
    look_actions: bool, requested: str | None, *, look_default: str
) -> str:
    """Live preview default: Workbench when the unit owns no look, else the look default.

    Canonical EEVEE remains the sealed artifact. Run ``20260826T170413Z-ba2b4c`` created
    a temp sun to light an EEVEE verify on an executable-only camera unit. ``render_pass``
    already forces matcap when ``look_actions`` is false; ``render_frame`` and
    ``verify_change`` still defaulted to EEVEE/draft.
    """
    if requested:
        return str(requested)
    return look_default if look_actions else "solid"


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


def _object_or_role_error(args: dict, tool: str) -> str | None:
    """Builder tools address roles; display names are the fallback, never both."""
    has_object = bool(str(args.get("object") or "").strip())
    has_role = bool(str(args.get("role") or "").strip())
    if has_object and has_role:
        return f"{tool}: pass role= or object=, not both"
    if not has_object and not has_role:
        return f"{tool}: requires role= (semantic selector) or object= (display name)"
    return None


def _check_args_error(kind: str, args: dict) -> str | None:
    requirements = {
        "visibility": ("frame",),
        "framing": (),
        "motion": ("frames",),
        "mesh": (),
        "scale": (),
        "passes": ("frame",),
        "bbox": ("frame",),
    }
    missing = [name for name in requirements[kind] if args.get(name) is None]
    if kind == "framing" and args.get("frame") is None and not args.get("frames"):
        missing.append("frame or frames")
    if missing:
        return f"check_scene(kind={kind!r}) requires " + ", ".join(dict.fromkeys(missing))
    if kind == "motion" and len(args.get("frames") or []) < 2:
        return "check_scene(kind='motion') requires frames with at least 2 entries"
    if kind != "passes":
        selector = _object_or_role_error(args, f"check_scene(kind={kind!r})")
        if selector:
            return selector
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
    if evidence_ids is not None:
        observed = {str(row.get("id")) for row in rows}
        missing_ids = sorted(set(evidence_ids) - observed)
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
            for evidence_id in missing_ids
        ]
        selected = [*rows, *missing]
        return bool(selected) and not missing and all(row.get("pass") for row in rows), selected
    authoritative = [row for row in rows if row.get("authoritative")]
    if authoritative:
        return all(row.get("pass") for row in authoritative), authoritative
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


def _image_evidence_ids_at_frame(state: Mapping[str, object], frame: int) -> set[str] | None:
    """Exact image bindings owed at one plate, not the unit's cross-frame union."""
    debts = state.get("image_debts") or []
    if debts:
        return {
            str(row.get("id"))
            for row in debts
            if isinstance(row, Mapping)
            and row.get("id")
            and int(row.get("frame", -1)) == int(frame)
        }
    ids = state.get("active_image_evidence_ids")
    return set(ids) if ids is not None else None


def _unpaid_image_debt_note(state: dict) -> str:
    """Payment path for owed image-contract ids. Not a selector miss; not critic handoff."""
    unpaid = list(state.get("unpaid_image_debts") or [])
    if not unpaid:
        return ""
    listed = ", ".join(
        f"{row.get('id')} (f{row.get('frame')}, {row.get('property')}, {row.get('axis')})"
        for row in unpaid
        if isinstance(row, dict)
    )
    return (
        "\nIMAGE-CONTRACT DEBTS UNPAID: "
        + listed
        + ". Call propose_checks with those exact ids; frame, property kind, and axis "
        "must match. A role or control retag cannot produce them. compare_frame "
        "evaluates paid rows; unpaid debts are not critic handoff."
    )


def followup_after_scene_contracts_pass(state: dict) -> str:
    """What the builder may do after bound scene rows pass.

    Look ownership is not an image-contract set. Treating it as one told a
    look unit that 3/3 existence rows were a critic handoff and locked
    ``run_bpy`` before appearance iteration (HIR-0044). Unpaid image-contract
    debts are a compiled card, not a scene-selector miss (HIR-0048).
    """
    unpaid_note = _unpaid_image_debt_note(state)
    if unpaid_note:
        return "\nSCENE CONTRACTS PASS." + unpaid_note
    if state.get("look_unsettled"):
        return (
            "\nSCENE CONTRACTS PASS: executable existence/wiring is done. This unit "
            "still owns look and binds no image contract; run_bpy remains open. "
            "Use render_frame / compare_frame as observation, not as a mutation lock."
        )
    if state.get("image_evidence_required"):
        return (
            "\nSCENE CONTRACTS PASS: call one FULL-FRAME compare_frame now. "
            "This unit binds authoritative image evidence; a failed bound "
            "check reopens one repair. Further run_bpy edits are blocked "
            "until that comparison."
        )
    return (
        "\nUNIT HANDOFF READY: every bound executable scene contract "
        "passes and this unit binds no image contract. Use the required "
        "diagnostics, then stop; no beauty comparison is required."
    )


def followup_after_image_gate_pass(state: dict, gate_rows: list) -> str:
    """Empty image-contract rows are not a critic handoff (HIR-0044)."""
    if state.get("look_unsettled"):
        return (
            "\nLOOK ITERATION OPEN: no bound image contract closed this plate. "
            "A 0/0 image-contract pass is not critic handoff; mutation remains legal."
        )
    unpaid = _unpaid_image_debt_note(state)
    if unpaid:
        return unpaid
    if not gate_rows:
        return (
            "\nNO BOUND IMAGE CONTRACTS on this comparison. 0/0 pass is not critic "
            "handoff; it does not certify look."
        )
    return (
        "\nCRITIC HANDOFF READY: current-layer scene and image "
        "contracts pass. Do not mutate again without critic-backed evidence."
    )


CANNOT_EXPRESS_SCHEMA = {
    "type": "object",
    "properties": {
        "contract_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "failing or contradictory contract ids",
        },
        "reason": {
            "type": "string",
            "description": "why no in-scope edit can pass, with the measured floor if any",
        },
        "fault_owner_units": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "optional upstream unit ids whose sealed outcome causes the measured floor; "
                "choose only from unit_scope.fault_owner_options"
            ),
        },
    },
    "required": ["contract_ids", "reason"],
}

CANNOT_EXPRESS_DESCRIPTION = (
    "Record that the failing contracts cannot be satisfied inside this unit's "
    "mutation scope or without contradicting another sealed contract. This STOPS "
    "further repair attempts and records a typed plan defect (hypothesis_falsified). "
    "Use it when interpolation, a child object, extra mutation, or an unpaid "
    "image-contract debt cannot legally pass — not for a fix you have not measured. "
    "Name the bare contract ids (no check: prefix). Distinct from ask_supervisor, "
    "which does not block. When executable evidence pins the floor to a sealed upstream "
    "unit, include its id from unit_scope.fault_owner_options so replan invalidates the "
    "semantic owner rather than only retrying this unit."
)


def record_cannot_express(comparison_state: dict | None, args: dict) -> dict:
    """Write a typed in-scope abstention onto the session the repair loop reads."""
    from vfx_harness.domain.image_debts import (
        classify_cannot_express,
        debts_from_dicts,
        normalize_evidence_id,
    )

    ids = [
        normalize_evidence_id(item)
        for item in (args.get("contract_ids") or [])
        if str(item).strip()
    ]
    reason = str(args.get("reason") or "").strip()
    if not ids or not reason:
        return _text(
            "cannot_express_in_scope requires non-empty contract_ids and reason",
            is_error=True,
        )
    if comparison_state is None:
        return _text(
            "cannot_express_in_scope is not bound in this session",
            is_error=True,
        )
    requested_owners = sorted({str(item).strip() for item in args.get("fault_owner_units") or [] if str(item).strip()})
    options = {
        str(row.get("id")): row
        for row in comparison_state.get("fault_owner_options") or []
        if isinstance(row, dict) and row.get("id")
    }
    unknown_owners = sorted(set(requested_owners) - set(options))
    if unknown_owners:
        return _text(
            "unknown fault_owner_units "
            + ", ".join(unknown_owners)
            + "; legal upstream options: "
            + (", ".join(sorted(options)) or "(none)"),
            is_error=True,
        )
    debts = debts_from_dicts(comparison_state.get("image_debts"))
    classification = classify_cannot_express(ids, debts)
    comparison_state["cannot_express"] = {
        "contract_ids": ids,
        "reason": reason,
        "classification": classification,
        "fault_owner_units": requested_owners,
    }
    return _text(
        "Recorded cannot_express_in_scope for "
        + ", ".join(ids)
        + f" ({classification}). Do not edit the script further. The harness will stop "
        "remaining repairs and publish a typed plan defect; vfx units replan consumes it. "
        f"Reason: {reason}"
    )


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
    measured = (comparison_state.get("world_density_probes") or {}).get(
        f"{role}@{frame}", []
    )
    from vfx_harness.blender.black_frame_report import same_density

    if any(same_density(value, density) for value in measured):
        comparison_state.pop("black_frame_required_probe", None)
        return None
    return required


def _schedule_override_without_keying(script: str, protected_paths: set[str]) -> set[str]:
    """Return protected animated paths a payload tries to override as a live probe."""
    import ast

    try:
        tree = ast.parse(script)
    except SyntaxError:
        return set()
    terminals = {path.rsplit(".", 1)[-1] for path in protected_paths}
    keyed: set[str] = set()
    uses_fcurve_inventory = False
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "bvfx_fcurves"
        ):
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
            attrs = {
                child.attr
                for child in ast.walk(target)
                if isinstance(child, ast.Attribute)
            }
            if "co" in attrs and (
                uses_fcurve_inventory or "keyframe_points" in attrs
            ):
                violated.update(protected_paths)
            if not isinstance(target, ast.Attribute):
                continue
            if target.attr in terminals and target.attr not in keyed:
                violated.update(
                    path
                    for path in protected_paths
                    if path.rsplit(".", 1)[-1] == target.attr
                )
            if target.attr == "mute":
                with contextlib.suppress(ValueError, TypeError):
                    if ast.literal_eval(node.value) is True:
                        violated.update(protected_paths)
    return violated


def _closed_density_repeat_message(
    comparison_state: dict, *, role: str, frame: int, values: list[float]
) -> str:
    """Refuse an exact repeat after the density causal branch has closed."""
    from vfx_harness.blender.black_frame_report import same_density

    probe_key = f"{role}@{int(frame)}"
    measured = (comparison_state.get("world_density_probes") or {}).get(probe_key, [])
    diagnosis = (comparison_state.get("world_density_probe_diagnoses") or {}).get(
        probe_key, ""
    )
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


def _candidate_for_proposed_check(
    check: dict, default_handle: str | None, registry: dict
) -> tuple[str, dict | None, str]:
    """Resolve one check's frame-local immutable candidate handle."""
    handle = str(check.get("after_handle") or default_handle or "")
    if not handle:
        return "", None, "after_handle is required on the check or at batch level"
    record = registry.get(handle)
    if not isinstance(record, dict) or record.get("role") != "live_candidate":
        available = sorted(
            key
            for key, value in registry.items()
            if isinstance(value, dict) and value.get("role") == "live_candidate"
        )[-6:]
        hint = ", ".join(available) or "none — call render_frame first"
        return handle, None, f"unknown current-run candidate handle {handle!r}; recent handles: {hint}"
    return handle, record, ""


def _render_setting_writes(script: str) -> set[str]:
    """Return free-form writes to harness-owned renderer configuration.

    Scoped units own semantic roles and controls, not the render harness. Diagnostics
    go through transactional render tools. This follows the common
    ``sc = bpy.context.scene; ee = sc.eevee`` alias form as well as direct chains.
    """
    import ast

    try:
        tree = ast.parse(script)
    except SyntaxError:
        return set()
    namespaces = {
        "eevee", "cycles", "render", "view_settings", "display_settings",
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


def _scoped_renderer_write_error(
    script: str, mutation_roles: tuple[str, ...] | None
) -> str:
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
        "cannot_express_in_scope(contract_ids="
        + repr(ids)
        + ", reason=<the measured density + placement floor>)"
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
    from vfx_harness.blender.black_frame_report import same_density

    out = list(values)
    if any(same_density(value, original) for value in out):
        return out
    if len(out) == 8:
        nearest = min(range(len(out)), key=lambda index: abs(out[index] - original))
        out[nearest] = original
    else:
        out.append(original)
    return sorted(set(out))


def build_blender_tools(
    session: BlenderSession,
    assets_dir: str | Path | None = None,
    shot_dir: str | Path | None = None,
    layer_id: str | None = None,
    comparison_state: dict | None = None,
    feedback_groups: list[str] | None = None,
    mutation_roles: tuple[str, ...] | None = None,
    scope_baseline: set[str] | None = None,
    unit_scope: dict | None = None,
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

    async def _black_frame_note(rendered: dict) -> str:
        """Return typed scene causes only for a nearly black look render."""
        if str(rendered.get("mode")) not in {"draft", "eevee"}:
            return ""
        from vfx_harness.evidence.metrics import look_vector

        metrics = look_vector(_load(rendered["image_path"]))
        if float(metrics.get("black_pct", 0.0)) <= 85.0:
            return ""
        try:
            context = await _call("black_context", frame=int(rendered["frame"]))
        except BlenderError as exc:
            return f"\n⚠ black-frame scene diagnosis unavailable: {str(exc)[:100]}"
        frame = int(rendered["frame"])
        clip_end = context.get("camera_clip_end")
        from vfx_harness.blender.black_frame_report import effective_volume_span

        sampled_span = effective_volume_span(
            clip_end,
            context.get("volumetric_start"),
            context.get("volumetric_end"),
        )
        high_rows = [
            row
            for row in context.get("volume_rows") or []
            if not row.get("linked")
            and row.get("density") is not None
            and sampled_span is not None
            and float(row["density"]) * float(sampled_span) >= 1.0
            and str(row.get("role") or "").strip()
        ]
        if high_rows:
            row = max(high_rows, key=lambda item: float(item["density"]))
            role = str(row["role"])
            density = float(row["density"])
            probed = comparison_state.setdefault("world_density_probes", {}).get(
                f"{role}@{frame}", []
            )
            from vfx_harness.blender.black_frame_report import same_density

            already_measured = any(same_density(value, density) for value in probed)
            if not already_measured:
                comparison_state["black_frame_required_probe"] = {
                    "role": role,
                    "frame": frame,
                    "density": density,
                }
        text = str(context.get("text") or "").strip()
        if high_rows and already_measured:
            # The worker cause card cannot see session probe history. Retire a stale
            # prescription explicitly; concurrent frame renders must not resurrect it.
            text = "\n".join(
                line for line in text.splitlines() if "NEXT MEASUREMENT:" not in line
            )
            _pending_black_frame_probe(comparison_state)
            probe_key = f"{role}@{frame}"
            diagnosis = str(
                (comparison_state.get("world_density_probe_diagnoses") or {}).get(
                    probe_key, ""
                )
            ).strip()
            text += "\n  " + (
                diagnosis
                if diagnosis
                else "DENSITY ALREADY MEASURED at this frame; repeating the same "
                "sweep is not a legal next step. Test a different causal variable."
            )
            if diagnosis.startswith("DENSITY HYPOTHESIS CLOSED"):
                from vfx_harness.blender.black_frame_report import (
                    summarize_black_placement_search,
                )

                for light in context.get("lights") or []:
                    light_role = str(light.get("role") or "").strip()
                    distance = light.get("camera_distance")
                    if not light_role or distance is None:
                        continue
                    search_key = f"{light_role}@{frame}"
                    trials = comparison_state.setdefault(
                        "black_placement_trials", {}
                    ).setdefault(search_key, [])
                    trials.append({
                        "camera_distance": distance,
                        "mean": metrics.get("exposure_mean", 0.0),
                        "black_pct": metrics.get("black_pct", 0.0),
                    })
                    closure = summarize_black_placement_search(trials)
                    if closure and comparison_state.get("scene_contracts_passed"):
                        contract_ids = [
                            str(card.get("id"))
                            for card in comparison_state.get("image_debts") or []
                            if int(card.get("frame") or -1) == frame and card.get("id")
                        ]
                        comparison_state["black_frame_search_exhausted"] = {
                            "frame": frame,
                            "role": light_role,
                            "contract_ids": contract_ids,
                            "reason": closure,
                        }
                        text += "\n  " + closure
        return "\n" + text if text else ""

    def _black_search_stop() -> str:
        return _black_search_stop_message(comparison_state)

    def _register_candidate(rendered: dict) -> str | None:
        if not shot_dir or not comparison_state.get("image_debts"):
            return None
        # A runtime payment must be directly comparable with the harness-captured
        # adversary.  Do not mint opaque handles for diagnostic previews: they
        # cannot pass the v2 provenance/settings check and advertising them teaches
        # the builder a dead-end action.
        if not _payment_eligible_candidate(rendered):
            return None
        record = _capture_image_artifact(
            shot_dir=shot_dir,
            source=rendered["image_path"],
            frame=int(rendered["frame"]),
            mode=str(rendered["mode"]),
            scale=float(rendered.get("scale", 0.5)),
            resolution=rendered.get("resolution"),
            role="live_candidate",
            unit_id=str(comparison_state.get("unit_id") or "unit"),
            parent_chain_hash=str(comparison_state.get("parent_chain_hash") or ""),
        )
        comparison_state.setdefault("image_artifacts", {})[record["handle"]] = record
        return str(record["handle"])

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
        "bvfx_vector_blur(...) for a wired Blender-5 compositor node; "
        "bvfx_light(...) for type-safe POINT/AREA/SPOT creation or conversion; "
        "bvfx_emission(name,color,strength). To DEBUG a material/world, use inspect_nodes "
        "instead of rendering repeatedly to guess. Tag every contract-facing datablock "
        "with bvfx_role(target,'material.floor.worn',owner_layer='2') — one dotted "
        "token per host, commas are not membership — and every shader/"
        "compositor control with bvfx_control(node,'control.orb.gain',owner_layer='2'); "
        "semantic roles survive renames and are the only supported contract interface.",
        {"type": "object", "properties": {"script": {"type": "string"}}, "required": ["script"]},
    )
    async def run_bpy(args):
        family_error = _run_bpy_write_family_error(
            str(args.get("script") or ""), unit_scope
        )
        if family_error:
            return _text(family_error, is_error=True)
        stop = _black_search_stop()
        if stop:
            return _text(stop, is_error=True)
        required_probe = _pending_black_frame_probe(comparison_state)
        if isinstance(required_probe, dict):
            role = str(required_probe.get("role") or "")
            frame = int(required_probe.get("frame") or 0)
            density = float(required_probe.get("density") or 0.0)
            return _text(
                "BLOCKED: the last look render was nearly black with an unmeasured "
                f"World density {density:g} on {role!r} at f{frame}. Call the "
                "cause-card probe_control density sweep before another free-form "
                "scene mutation; a ceiling is not a target.",
                is_error=True,
            )
        if comparison_state.get("scene_contracts_passed") and shot_dir:
            from vfx_harness.evidence.scene_checks import load_rows

            active_ids = comparison_state.get("active_evidence_ids")
            schedule_rows = [
                row
                for row in load_rows(shot_dir)
                if row.get("kind") == "keyframe_schedule"
                and (active_ids is None or str(row.get("id")) in active_ids)
            ]
            protected_paths = {
                str(path)
                for row in schedule_rows
                for sample in row.get("samples") or []
                for path in (sample.get("values") or {})
            }
            violations = _schedule_override_without_keying(
                str(args.get("script") or ""), protected_paths
            )
            if violations:
                ids = [
                    str(row.get("id"))
                    for row in schedule_rows
                    if any(
                        str(path) in violations
                        for sample in row.get("samples") or []
                        for path in (sample.get("values") or {})
                    )
                ]
                return _text(
                    "BLOCKED: required exact schedule(s) already pass: "
                    + ", ".join(ids)
                    + ". This payload would disable or override protected animated path(s) "
                    + ", ".join(sorted(violations))
                    + " without keying a legal schedule. Do not use authored state as a "
                    "diagnostic. Use render_pass(pass='light_coverage', light=...) for "
                    "normalized read-only coverage; if coverage is visible but the "
                    "contract-scale beauty remains black after the density branch is "
                    "closed, call cannot_express_in_scope and name the schedule conflict.",
                    is_error=True,
                )
        renderer_error = _scoped_renderer_write_error(
            str(args.get("script") or ""), mutation_roles
        )
        if renderer_error:
            return _text(renderer_error, is_error=True)
        from vfx_harness.blender.black_frame_report import (
            authored_density_values,
            same_density,
        )

        proposed_densities = authored_density_values(str(args.get("script") or ""))
        if proposed_densities:
            tested = {
                float(value)
                for values in (comparison_state.get("world_density_probes") or {}).values()
                for value in values
            }
            unmeasured = [
                value
                for value in proposed_densities
                if tested
                and not any(same_density(value, prior) for prior in tested)
            ]
            if unmeasured:
                return _text(
                    "BLOCKED: World Density value(s) "
                    + ", ".join(f"{value:g}" for value in unmeasured)
                    + " were not measured by probe_control. Commit a tested value "
                    + "or include the new candidate in a density sweep first.",
                    is_error=True,
                )
        try:
            r = await _call("run", code=args["script"], transactional=True)
        except BlenderError as e:
            return _text(_run_bpy_instrument_hint(str(args.get("script") or ""), str(e)), is_error=True)
        # A successful script may have changed pixels even when this layer has no scene
        # completion contract. Never carry an earlier comparison verdict across it.
        comparison_state["pixel_contracts_passed"] = False
        comparison_state["mutation_serial"] = int(comparison_state.get("mutation_serial", 0)) + 1
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
                offenders = _scope_offenders(manifest, mutation_roles, scope_baseline)
                comparison_state["scope_offenders"] = list(offenders)
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
                _refresh_unpaid_image_debts(comparison_state, shot_dir)
                authoritative = state["authoritative"]
                passed = [row for row in authoritative if row.get("pass")]
                failed = state["failures"]
                if authoritative:
                    from vfx_harness.observability.runlog import bump

                    bump("automatic_scene_contract_probe")
                    contract_note = f"\nAUTHORITATIVE SCENE CONTRACTS: {len(passed)}/{len(authoritative)} pass"
                    if state["missing"]:
                        # Unevaluated required SCENE evidence used to read as silence.
                        # Image-contract debts are a separate card (HIR-0048).
                        comparison_state["scene_contracts_passed"] = False
                        comparison_state["scene_interfaces_ready"] = False
                        contract_note += (
                            " · REQUIRED SCENE EVIDENCE NOT PRODUCED: "
                            + ", ".join(state["missing"][:6])
                            + "\n  These scene contracts are bound to required claims but "
                            "were never evaluated — usually a selector matching no object, "
                            "or a frame group that never ran. They cannot pass by absence."
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
                        if not comparison_state.get("look_unsettled") and not comparison_state.get(
                            "image_evidence_required"
                        ):
                            comparison_state["pixel_contracts_passed"] = True
                        contract_note += followup_after_scene_contracts_pass(comparison_state)
                    else:
                        comparison_state["scene_interfaces_ready"] = True
                        comparison_state["current_scene_contracts_present"] = False
                        comparison_state["scene_contracts_passed"] = False
                        contract_note += (
                            "\nINHERITED INTERFACES PASS, but this layer owns no active "
                            "scene completion contract. They prove healthy inputs, not "
                            "that the current layer is finished."
                        )
                        unpaid_note = _unpaid_image_debt_note(comparison_state)
                        if unpaid_note:
                            contract_note += unpaid_note
                        else:
                            contract_note += (
                                " Live mutation remains open until the builder hands "
                                "its scoped work off."
                            )
            except Exception as exc:
                contract_note = (
                    f"\n⚠ automatic scene-contract probe unavailable: {type(exc).__name__}: {str(exc)[:100]}"
                )
        if comparison_state.get("scope_offenders"):
            # A candidate with an unowned object is not converged even if its numeric
            # rows happen to pass. Keep the corrective mutation window open; canonical
            # replay will reject this exact state.
            comparison_state["scene_contracts_passed"] = False
            comparison_state["pixel_contracts_passed"] = False
            contract_note += (
                "\nSCOPE CLEANUP REQUIRED: convergence remains open until every newly "
                "created object is deleted or assigned a declared semantic role."
            )
        return _text(body + meta + warn + contract_note)

    @tool(
        "unit_scope",
        "Compiled scope of the ACTIVE work unit: mutation roles/controls/dresses/"
        "script_spans, bound contract ids (kind, roles, frame), claims, judge frames, "
        "and every bvfx_* helper injected into run_bpy. Same card as kickoff and "
        "CLAUDE.md. Query this; do not inspect.getsource or guess a sibling unit.",
        {"type": "object", "properties": {}, "required": []},
    )
    async def unit_scope_tool(_args):
        if not unit_scope:
            return _text(
                "no active work unit — unit_scope is compiled per unit",
                is_error=True,
            )
        return _text(json.dumps(unit_scope, indent=2, sort_keys=True))

    @tool(
        "inspect_scene",
        "Read the scene as text (Tier-1, free, no render): objects+transforms+modifiers"
        "+particle systems, materials with object-slot consumers, world, lights, color "
        "management, compositor, and "
        "render settings. section='lights' reports energy/color/visibility/location so "
        "light setup never needs a read-only run_bpy probe; section='cameras' reports "
        "evaluated world pose, forward vector, lens, and sensor. Each object line includes "
        "local/evaluated world location, dimensions, visibility, role, and owner. Pass "
        "frame= for evaluated transforms and role= to filter by semantic bvfx_role "
        "(fnmatch). Use this to verify structure before spending a render.",
        {
            "type": "object",
            "properties": {
                "section": {
                    "type": "string",
                    "enum": ["all", "objects", "materials", "world", "render", "lights", "cameras"],
                },
                "frame": {"type": "integer", "description": "optional evaluation frame"},
                "role": {
                    "type": "string",
                    "description": "optional bvfx_role selector (fnmatch); miss names present roles and names",
                },
            },
            "required": [],
        },
    )
    async def inspect_scene(args):
        try:
            r = await _call(
                "inspect",
                section=args.get("section", "all"),
                **({"frame": int(args["frame"])} if args.get("frame") is not None else {}),
                **({"role": args["role"]} if args.get("role") else {}),
            )
        except BlenderError as e:
            return _text(str(e), is_error=True)
        return _text(r["text"])

    @tool(
        "inspect_nodes",
        "Dump a NODE GRAPH as text — every node's type, unlinked input values, all "
        "output socket names, and links. target = 'compositor' (the bloom/glare graph — 5.x has "
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
        "The Graph-Editor read (Tier-1, free): every F-curve on an object AND its "
        "data-block (Light/Camera energy lives on data.energy) as frame→value pairs "
        "with interpolation. Address by role= (bvfx_role); a shared role lists every "
        "host (including hide_render). object= is the display-name fallback for one "
        "host. Use to verify easing/timing without rendering. A keyframe_schedule "
        "sample path energy matches data.energy; a custom ['energy'] is a different path.",
        {
            "type": "object",
            "properties": {
                "role": {"type": "string", "description": "bvfx_role selector (preferred)"},
                "object": {"type": "string", "description": "display name; use role= instead"},
            },
            "required": [],
        },
    )
    async def list_keyframes(args):
        selector = _object_or_role_error(args, "list_keyframes")
        if selector:
            return _text(selector, is_error=True)
        payload = {k: v for k, v in args.items() if k in ("role", "object") and v}
        try:
            r = await _call("keyframes", **payload)
        except BlenderError as e:
            return _text(str(e), is_error=True)
        return _text(r["text"])

    @tool(
        "render_frame",
        "See one frame. mode='solid'/'wire' = fast Workbench (~0.1s, composition/"
        "silhouette); mode='draft' = fast low-sample EEVEE (~0.5s, quick look checks — "
        "use this while iterating); mode='eevee' = full-quality look (~1-3s, for final "
        "judging). On a unit with no look capabilities the default is solid (geometry "
        "without lights). Pass mode='eevee' only when you need beauty. scale is 0..1 "
        "(default 0.4). Returns the image + an EXPOSURE readout (mean/clipped/black) "
        "so you can catch blowout objectively.",
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
        stop = _black_search_stop()
        if stop:
            return _text(stop, is_error=True)
        mode = preview_render_mode(
            feedback_policy["look_actions"], args.get("mode"), look_default="eevee"
        )
        try:
            r = await _call(
                "render", frame=int(args["frame"]), mode=mode, scale=float(args.get("scale", 0.4))
            )
        except BlenderError as e:
            return _text(str(e), is_error=True)
        # h_render does not echo scale because its pixel resolution is the executable
        # setting. Preserve the caller value for provenance equality with the harness-
        # captured pre-unit adversary.
        r["scale"] = float(args.get("scale", 0.4))
        handle = _register_candidate(r)
        cap = f"frame {r['frame']} ({r['mode']})" + _warn_suffix(r)
        if handle:
            cap += f"\nIMAGE EVIDENCE HANDLE: {handle}"
        if not feedback_policy["look_actions"] and not args.get("mode"):
            cap += (
                " · Workbench default for an executable-only unit "
                "(pass mode='eevee' for beauty; lighting is not this unit's scope)"
            )
        cap += await _black_frame_note(r)
        return _image(r["image_path"], cap, feedback_groups=feedback_policy["groups"])

    @tool(
        "render_pass",
        "Render one frame as a DIAGNOSTIC rather than a beauty shot, so you can see the "
        "thing you are actually being judged on. `pass` isolates a render pass — use "
        "'diffuse_direct' to see MODELLING BY LIGHT with emission removed (a render "
        "setting, not something to squint past), 'emit' to see only self-lit surfaces, "
        "'light_coverage' with `light=` to render clay under that local light while "
        "transactionally suppressing World lighting/volume (separates placement from "
        "atmospheric extinction), "
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
                    "enum": [
                        "beauty", "light_coverage", "diffuse_direct", "emit",
                        "shadow", "ao", "normal", "depth", "crypto",
                    ],
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
        stop = _black_search_stop()
        if stop:
            return _text(stop, is_error=True)
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
        if args.get("pass") == "light_coverage" and not str(args.get("light") or "").strip():
            return _text(
                "pass='light_coverage' requires light='<LightObject>' so the causal "
                "isolation has one named subject",
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
            f"frame {r['frame']} · {r.get('caption', '')}"
            + f"\nsettings: pass={r.get('pass')} "
            f"shade={r.get('effective_shade') or r.get('shade')} "
            f"light={r.get('light')} crop={r.get('crop')} res_pct={r.get('res_pct')}" + _warn_suffix(r)
        )
        if r.get("pass") != "light_coverage":
            cap += await _black_frame_note(r)
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
        "oracle crop box to hand to render_pass. Address subjects with role= (bvfx_role); "
        "a shared role that matches several hosts is not a miss — pass object= with one "
        "of the named hosts. object= is the display-name fallback. A miss names present "
        "roles and names. "
        "Required arguments: visibility=role-or-object+frame; "
        "framing=role-or-object+(frame or frames); motion=role-or-object+2+ frames; "
        "mesh/scale=role-or-object; passes=frame; bbox=role-or-object+frame. For "
        "intentional open shells, mesh accepts allow_boundary=true and still rejects "
        "branch/wire edges.",
        {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["visibility", "framing", "motion", "mesh", "scale", "passes", "bbox"],
                },
                "role": {
                    "type": "string",
                    "description": (
                        "bvfx_role selector (preferred); fnmatch; exactly one host — "
                        "if several share the role, pass object="
                    ),
                },
                "object": {
                    "type": "string",
                    "description": "display name; use role= instead when you know the semantic role",
                },
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
        "contract_result",
        "Evaluate one contract bound to the ACTIVE unit by exact id. Scene contracts "
        "use the canonical evaluator (including multi-role visible_fraction logical AND) "
        "and report per-role details. Image contracts require image_handle from an "
        "eevee render at that frame. Use this instead of recreating contract math in "
        "run_bpy or guessing from a beauty render.",
        {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "image_handle": {"type": "string"},
            },
            "required": ["id"],
            "additionalProperties": False,
        },
    )
    async def contract_result(args):
        if not shot_dir or not layer_id:
            return _text("contract_result needs an active shot layer", is_error=True)
        cid = str(args["id"])
        scene_ids = set(comparison_state.get("active_evidence_ids") or [])
        image_ids = set(comparison_state.get("active_image_evidence_ids") or [])
        if cid not in scene_ids | image_ids:
            present = sorted(scene_ids | image_ids)
            return _text(
                f"contract {cid!r} is not bound to this unit; bound ids: "
                + (", ".join(present) if present else "none"),
                is_error=True,
            )
        if cid in scene_ids:
            try:
                from vfx_harness.evidence.scene_checks import layer_evidence, load_rows

                source = next(row for row in load_rows(shot_dir) if str(row.get("id")) == cid)
                frames = source.get("frames") or [source.get("frame", comparison_state.get("frame", 1))]
                rows: list[dict] = []
                for frame in frames:
                    measured = await anyio.to_thread.run_sync(
                        lambda f=int(frame): layer_evidence(
                            shot_dir, str(layer_id), frame=f, session=session
                        )
                    )
                    rows.extend(row for row in measured if str(row.get("id")) == cid)
            except (StopIteration, OSError, ValueError, BlenderError) as exc:
                return _text(f"could not evaluate scene contract {cid}: {exc}", is_error=True)
            return _text(json.dumps(rows, indent=2, sort_keys=True))

        handle = str(args.get("image_handle") or "")
        record = (comparison_state.get("image_artifacts") or {}).get(handle)
        if not isinstance(record, dict) or record.get("role") != "live_candidate":
            return _text(
                f"image contract {cid!r} requires a current IMAGE EVIDENCE HANDLE; "
                "call render_frame(mode='eevee', scale=0.5) at its owed frame",
                is_error=True,
            )
        try:
            from vfx_harness.evidence.checks import layer_evidence as image_layer_evidence
            from vfx_harness.orchestration.ledger import load_layers

            layer = load_layers(type("ShotRef", (), {"folder": shot_dir})())[str(layer_id)]
            ref = dict(layer.judges).get(int(record["frame"]), "")
            rows = image_layer_evidence(
                shot_dir,
                str(layer_id),
                frame=int(record["frame"]),
                ref=str(ref),
                render=str(record["path"]),
                stage=("post_grade" if any("grade" in axis.lower() for axis in layer.owns) else "pre_grade"),
            )
        except (OSError, ValueError, KeyError) as exc:
            return _text(f"could not evaluate image contract {cid}: {exc}", is_error=True)
        return _text(json.dumps([row for row in rows if str(row.get("id")) == cid], indent=2, sort_keys=True))

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
    change_baselines: dict[str, tuple[Path, int, str, float, int]] = {}

    @tool(
        "verify_change",
        "Prove whether an edit changed the intended frame, without managing image paths. "
        "Call action='baseline' BEFORE run_bpy with a short label, frame, mode and scale. "
        "After the edit call action='compare' with the same label; the tool re-renders the "
        "stored frame at the IDENTICAL settings and returns |before-after| plus mean/max "
        "delta. A near-black result means the edit was a visible no-op. Use this whenever "
        "you are changing a node, light, visibility state, modifier, or small feature and "
        "cannot prove from a numeric scene check that the intended pixels moved. On a unit "
        "with no look capabilities the default mode is solid (Workbench), not draft EEVEE.",
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
        stop = _black_search_stop()
        if stop:
            return _text(stop, is_error=True)
        label = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(args["label"]).strip())[:60]
        if not label:
            return _text("label must contain at least one letter or number", is_error=True)
        if args["action"] == "baseline":
            if args.get("frame") is None:
                return _text("baseline requires frame", is_error=True)
            frame = int(args["frame"])
            mode = preview_render_mode(
                feedback_policy["look_actions"], args.get("mode"), look_default="draft"
            )
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
            change_baselines[label] = (
                dest,
                frame,
                mode,
                scale,
                int(comparison_state.get("mutation_serial", 0)),
            )
            cap = (
                f"change baseline '{label}' captured at f{frame} "
                f"mode={mode} scale={scale:g}. Make ONE edit, then call "
                f"verify_change(action='compare', label='{label}')."
            )
            if not feedback_policy["look_actions"] and not args.get("mode"):
                cap += (
                    " Workbench default for an executable-only unit "
                    "(pass mode='eevee' for beauty; lighting is not this unit's scope)."
                )
            return _image(str(dest), cap)

        prior = change_baselines.get(label)
        if prior is None:
            return _text(f"no baseline named {label!r}; call action='baseline' first", is_error=True)
        before, frame, mode, scale, baseline_serial = prior
        if int(comparison_state.get("mutation_serial", 0)) == baseline_serial:
            return _text(
                f"no successful run_bpy edit occurred after baseline {label!r}; the "
                "attempted edit was blocked or never sent, so there is no change to verify",
                is_error=True,
            )
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
        stop = _black_search_stop()
        if stop:
            return _text(stop, is_error=True)
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
        r["scale"] = scale
        evidence_handle = _register_candidate(r) if crop is None else None
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
                        evidence_ids=_image_evidence_ids_at_frame(
                            comparison_state, int(args["frame"])
                        ),
                    )
                )
                passed_rows = sum(bool(row.get("pass")) for row in gate_rows)
                gate_note = f"\nBOUND IMAGE CHECKS: {passed_rows}/{len(gate_rows)} pass"
                if gate_pass:
                    comparison_state["pixel_contracts_passed"] = True
                    if comparison_state.get("current_scene_contracts_present"):
                        comparison_state["scene_contracts_passed"] = True
                        gate_note += followup_after_image_gate_pass(
                            comparison_state, gate_rows
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
        if crop is None:
            out["content"][0]["text"] += await _black_frame_note(r)
        if evidence_handle:
            out["content"][0]["text"] += (
                f"\nIMAGE EVIDENCE HANDLE: {evidence_handle}"
            )
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
        stop = _black_search_stop()
        if stop:
            return _text(stop, is_error=True)
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
            r = await _call("run", code=script, transactional=True)
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
        stop = _black_search_stop()
        if stop:
            return _text(stop, is_error=True)
        if shot_dir is None:
            return _text("probe_control requires a shot directory", is_error=True)
        values = [float(v) for v in args.get("values") or []]
        if not 2 <= len(values) <= 8 or not all(math.isfinite(v) for v in values):
            return _text("values must contain 2-8 finite numbers", is_error=True)
        if (
            args.get("graph") == "world"
            and str(args.get("socket") or "") == "Density"
            and args.get("node_role")
        ):
            repeat = _closed_density_repeat_message(
                comparison_state,
                role=str(args["node_role"]),
                frame=int(args["frame"]),
                values=values,
            )
            if repeat:
                return _text(repeat, is_error=True)
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

        # ONE control resolver: probe_control kept its own copy of this script and the
        # copies disagreed on diagnostics — the canonical resolver enumerates the tags
        # present on a miss, the copy said only "matched 0 nodes" (ADR-0003's registry
        # split, re-grown). The tool now renders the same script canonical evidence uses.
        from vfx_harness.evidence.scene_checks import _control_script

        selector_row = {
            "graph": args.get("graph"),
            "material_roles": [args["material_role"]] if args.get("material_role") else [],
            "node_roles": [args["node_role"]] if args.get("node_role") else [],
            "socket": args.get("socket"),
            "socket_index": args.get("socket_index"),
            "socket_direction": args.get("socket_direction"),
        }

        def control_script(value=None):
            return _control_script(selector_row, value=value)

        try:
            initial = await anyio.to_thread.run_sync(lambda: session.run(control_script(), journal=False))
            original = float((initial.get("result") or {})["before"])
        except Exception as exc:
            return _text(f"control resolution failed: {exc}", is_error=True)
        values = _probe_values_with_original(values, original)
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

        diagnosis = ""
        if (
            args.get("graph") == "world"
            and str(args.get("socket") or "") == "Density"
            and args.get("node_role")
        ):
            probe_key = f"{args['node_role']}@{int(args['frame'])}"
            known = comparison_state.setdefault("world_density_probes", {}).setdefault(
                probe_key, []
            )
            for value in values:
                if value not in known:
                    known.append(value)
            required = comparison_state.get("black_frame_required_probe")
            if (
                isinstance(required, dict)
                and str(required.get("role")) == str(args["node_role"])
                and int(required.get("frame") or 0) == int(args["frame"])
            ):
                comparison_state.pop("black_frame_required_probe", None)
            from vfx_harness.blender.black_frame_report import summarize_density_probe

            diagnosis = summarize_density_probe(rows)
            if diagnosis:
                comparison_state.setdefault("world_density_probe_diagnoses", {})[
                    probe_key
                ] = diagnosis

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
        if diagnosis:
            lines.append(diagnosis)
        return {
            "content": [
                {"type": "text", "text": "\n".join(lines)},
                {"type": "image", "data": _b64(sheet), "mimeType": "image/jpeg"},
            ]
        }

    tools = [
        run_bpy,
        unit_scope_tool,
        inspect_scene,
        inspect_nodes,
        list_keyframes,
        render_frame,
        render_frames,
        render_pass,
        check_scene,
        contract_result,
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
        active_unit_id = str(comparison_state.get("unit_id") or "")
        active_unit_hash = str(comparison_state.get("unit_hash") or "")
        if not active_unit_id or not active_unit_hash:
            return _text(
                "worklist requires the active unit id and digest; layer-only worklists "
                "cannot authorize another unit generation",
                is_error=True,
            )
        from vfx_harness.observability.worklists import (
            load_unit_worklist,
            write_unit_worklist,
        )

        try:
            wl, state = load_unit_worklist(
                shot_dir,
                layer_id=layer_part,
                unit_id=active_unit_id,
                unit_hash=active_unit_hash,
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            return _text(f"worklist refused: {exc}", is_error=True)
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
        write_unit_worklist(wl, state)
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
        stop = _black_search_stop()
        if stop:
            return _text(stop, is_error=True)
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
        "Each check must PASS on your render and FAIL on the state before your unit ran. "
        "The harness captures that adversary before the unit starts; you cannot select "
        "or manufacture it.\n"
        "after_handle is the IMAGE EVIDENCE HANDLE returned by render_frame or an "
        "uncropped compare_frame at the owed frame. Raw paths are intentionally not "
        "accepted. For a multi-frame batch, put after_handle on each check; a batch-level "
        "after_handle is shorthand only when every check uses the same frame. Render at "
        "mode='eevee', scale=0.5 so it is settings-identical to the "
        "harness adversary. Survivors are appended to the "
        "runtime_checks.json evidence ledger; planner contracts remain immutable in "
        "checks.json. Propose few and real. When the active unit owes image-contract "
        "debts, each kept row must use an owed id with matching frame, property kind, "
        "and axis; a different id while debts remain is rejected naming requested vs owed.",
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
                                "description": (
                                    "Literal metric operands: every region_mean/min/p5/max/"
                                    "sigma/lit_pct/green_excess/lit_variance check requires "
                                    "exactly key 'r', e.g. {'r':[x0,y0,x1,y1]}; "
                                    "region_ratio requires keys 'a' and 'b'. Labels such as "
                                    "'target', 'region', or a subject name are not operands."
                                ),
                                "additionalProperties": {
                                    "type": "array",
                                    "minItems": 4,
                                    "maxItems": 4,
                                    "items": {"type": "number"},
                                },
                            },
                            "note": {"type": "string"},
                            "after_handle": {
                                "type": "string",
                                "description": (
                                    "Frame-local current-run immutable candidate handle; "
                                    "overrides the batch-level shorthand"
                                ),
                            },
                        },
                        "required": ["id", "metric", "op"],
                        "additionalProperties": False,
                    },
                },
                "after_handle": {
                    "type": "string",
                    "description": "Current-run immutable handle returned by render_frame/compare_frame",
                },
            },
            "required": ["checks"],
            "additionalProperties": False,
        },
    )
    async def propose_checks(args):
        from vfx_harness.domain.image_debts import (
            debts_from_dicts,
            normalize_evidence_id,
            reject_proposed_image_check,
            unpaid_image_contract_debts,
        )
        from vfx_harness.domain.work_units import read_document
        from vfx_harness.evidence.checks import (
            IMAGE_PAYMENT_SCHEMA,
            Check,
            load_image_contract_payment_rows,
            verify_necessity,
        )

        if not shot_dir:
            return _text("propose_checks needs a shot dir", is_error=True)
        root = Path(shot_dir)
        registry = comparison_state.get("image_artifacts") or {}
        # Every check must name the plate it is about, or the gate cannot re-run it. The
        # first version of this tool took `after`/`before` renders and never populated
        # `ref`, so four good builder checks landed in the runtime evidence ledger and all
        # four failed validation on plumbing rather than on merit.
        judge: dict[int, str] = {}
        first_ref = ""
        try:
            from vfx_harness.orchestration.plan_authority import selected_artifact_path

            for lay in read_document(selected_artifact_path(root, "layers.json")):
                if str(lay.get("id")) != str(layer_id):
                    continue
                js = lay.get("judge") or []
                judge = {int(j["frame"]): j["ref"] for j in js if j.get("ref")}
                primary = lay.get("primary_judge")
                first_ref = next((j.get("ref", "") for j in js if j.get("frame") == primary), "")
        except Exception as e:
            return _text(
                f"could not read judge refs from selected layers.json: {str(e)[:100]}",
                is_error=True,
            )
        kept, lines = [], []
        debts = debts_from_dicts(comparison_state.get("image_debts"))
        unpaid = unpaid_image_contract_debts(
            debts, load_image_contract_payment_rows(root)
        ) if debts else ()
        for d in list(args.get("checks") or [])[:20]:
            cid = normalize_evidence_id(d.get("id", "?"))
            debt_reject = reject_proposed_image_check(
                d,
                debts,
                unpaid=unpaid,
                registry=METRICS,
            )
            if debt_reject:
                lines.append(f"  REJECTED {cid:10} {debt_reject}")
                continue
            d = {**d, "id": cid}
            after_handle, after_record, handle_error = _candidate_for_proposed_check(
                d, args.get("after_handle"), registry
            )
            d.pop("after_handle", None)
            if handle_error or not isinstance(after_record, dict):
                lines.append(f"  REJECTED {cid:10} {handle_error}")
                continue
            after = root / str(after_record["path"])
            if not after.is_file() or _sha256_file(after) != after_record.get("sha256"):
                lines.append(
                    f"  REJECTED {cid:10} candidate handle {after_handle!r} no longer "
                    "matches its immutable artifact"
                )
                continue
            try:
                check_frame = int(d["frame"])
            except (KeyError, TypeError, ValueError):
                lines.append(f"  REJECTED {cid:10} frame is required for a runtime image payment")
                continue
            adversary_record = (comparison_state.get("image_adversaries") or {}).get(check_frame)
            if not isinstance(adversary_record, dict):
                lines.append(
                    f"  REJECTED {cid:10} harness captured no pre-unit adversary at f{check_frame}"
                )
                continue
            before = root / str(adversary_record["path"])
            if not before.is_file() or _sha256_file(before) != adversary_record.get("sha256"):
                lines.append(
                    f"  REJECTED {cid:10} pre-unit adversary artifact is missing or changed"
                )
                continue
            if int(after_record.get("frame", -1)) != check_frame:
                lines.append(
                    f"  REJECTED {cid:10} candidate handle is f{after_record.get('frame')}, "
                    f"but this debt is f{check_frame}"
                )
                continue
            settings = ("mode", "scale", "resolution")
            mismatch = [
                key for key in settings if after_record.get(key) != adversary_record.get(key)
            ]
            if mismatch:
                lines.append(
                    f"  REJECTED {cid:10} candidate/adversary settings differ in "
                    + ", ".join(mismatch)
                    + "; render_frame(mode='eevee', scale=0.5)"
                )
                continue
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
                            "on": after_record["path"],
                        },
                        "payment": {
                            "schema": IMAGE_PAYMENT_SCHEMA,
                            "run_id": after_record["run_id"],
                            "unit_id": str(comparison_state.get("unit_id") or ""),
                            "unit_hash": str(comparison_state.get("unit_hash") or ""),
                            "parent_chain_hash": str(
                                comparison_state.get("parent_chain_hash") or ""
                            ),
                            "candidate": {
                                key: after_record[key]
                                for key in (
                                    "path", "sha256", "frame", "mode", "scale", "resolution"
                                )
                            },
                            "adversary": {
                                key: adversary_record[key]
                                for key in (
                                    "path", "sha256", "frame", "mode", "scale", "resolution",
                                    "parent_chain_hash",
                                )
                            },
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
            replacement_keys = {(row.get("layer"), row.get("id")) for row in kept}
            cur = [
                row
                for row in cur
                if (row.get("layer"), row.get("id")) not in replacement_keys
            ]
            cur.extend(kept)
            from vfx_harness.observability.provenance import atomic_write

            atomic_write(spec, json.dumps(cur, indent=1) + "\n")
        _refresh_unpaid_image_debts(comparison_state, root)
        remaining = comparison_state.get("unpaid_image_debts") or []
        tail = ""
        if remaining:
            tail = (
                "\nStill unpaid: "
                + ", ".join(str(row.get("id")) for row in remaining)
                + ". Candidate freeze will refuse until these ids are paid or "
                "cannot_express_in_scope records unpaid_image_debt."
            )
        return _text(
            f"{len(kept)} check(s) added to runtime_checks.json.\n" + "\n".join(lines) + tail
        )

    @tool(
        "cannot_express_in_scope",
        CANNOT_EXPRESS_DESCRIPTION,
        CANNOT_EXPRESS_SCHEMA,
    )
    async def cannot_express_in_scope(args):
        return record_cannot_express(comparison_state, args)

    # ask_supervisor is deliberately PLAN-ONLY: a layer that discovers an
    # ambiguity is already building on earlier layers' answer to it.
    tools = [*tools, script_map, find_in_script, worklist, cannot_express_in_scope, measure_regions, propose_checks]
    server = create_sdk_mcp_server(name=SERVER_NAME, version="0.1.0", tools=tools)
    names = [f"mcp__{SERVER_NAME}__{t.name}" for t in tools]
    return server, names
