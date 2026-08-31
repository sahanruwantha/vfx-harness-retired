"""Agent SDK tools over a warm BlenderSession.

`build_blender_tools(session)` returns an in-process MCP server and the list of
qualified tool names to put in `ClaudeAgentOptions.allowed_tools`. Render tools
return image content blocks, so the agent literally sees the frames it makes.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import anyio
from claude_agent_sdk import tool
from PIL import Image

from vfx_harness.blender.session import BlenderError
from vfx_harness.blender.tools.guards import _compare_image
from vfx_harness.blender.tools.images import _b64, _image, _load, _metrics_line, _stats, subtract_png
from vfx_harness.blender.tools.payment import _text
from vfx_harness.blender.tools.reports import (
    _METRIC_H,
    _comparison_lock_error,
    _comparison_mode_scale,
    _image_evidence_ids_at_frame,
    _pixel_contract_gate,
    _warn_suffix,
    followup_after_image_gate_pass,
    preview_render_mode,
)
from vfx_harness.evidence.compare_panels import crop_pixels, save_context_sheet, save_focus_sheet, validate_crop


def register_compare(
    session,
    _call,
    comparison_state,
    comparison_locks,
    shot_dir,
    layer_id,
    assets_dir,
    feedback_policy,
    mutation_roles,
    scope_baseline,
    unit_scope,
    _black_frame_note,
    _black_search_stop,
    _register_candidate,
    selected_authority=None,
):
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
            mode = preview_render_mode(feedback_policy["look_actions"], args.get("mode"), look_default="draft")
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
        "zoom. With mode omitted, typed look ownership selects EEVEE for look work and "
        "Workbench solid for look-less form/layout diagnostics; solid never pays beauty "
        "image debt. The first call locks mode+scale for the entire critic round. Crop calls "
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
        mode, scale = _comparison_mode_scale(args, base_lock, look_actions=feedback_policy["look_actions"])
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
            and (comparison_state.get("scene_interfaces_ready") or comparison_state.get("image_evidence_required"))
        ):
            try:
                gate_pass, gate_rows = await anyio.to_thread.run_sync(
                    lambda: _pixel_contract_gate(
                        shot_dir,
                        str(layer_id),
                        frame=int(args["frame"]),
                        ref=str(ref),
                        render=r["image_path"],
                        evidence_ids=_image_evidence_ids_at_frame(comparison_state, int(args["frame"])),
                        selected_authority=selected_authority,
                    )
                )
                passed_rows = sum(bool(row.get("pass")) for row in gate_rows)
                gate_note = f"\nBOUND IMAGE CHECKS: {passed_rows}/{len(gate_rows)} pass"
                if gate_pass:
                    comparison_state["pixel_contracts_passed"] = True
                    if comparison_state.get("current_scene_contracts_present"):
                        comparison_state["scene_contracts_passed"] = True
                        gate_note += followup_after_image_gate_pass(comparison_state, gate_rows)
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
            out["content"][0]["text"] += f"\nIMAGE EVIDENCE HANDLE: {evidence_handle}"
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

    return diff_frames, verify_change, compare_frame, render_frames, import_asset
