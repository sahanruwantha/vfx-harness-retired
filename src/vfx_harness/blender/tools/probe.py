"""Agent SDK tools over a warm BlenderSession.

`build_blender_tools(session)` returns an in-process MCP server and the list of
qualified tool names to put in `ClaudeAgentOptions.allowed_tools`. Render tools
return image content blocks, so the agent literally sees the frames it makes.
"""

from __future__ import annotations

import contextlib
import math
import re

import anyio
from claude_agent_sdk import tool
from PIL import Image, ImageChops, ImageStat

from vfx_harness.blender.black_frame_report import summarize_density_probe
from vfx_harness.blender.tools.guards import _closed_density_repeat_message, _probe_values_with_original
from vfx_harness.blender.tools.images import _b64, _region_metrics
from vfx_harness.blender.tools.payment import _text
from vfx_harness.blender.tools.reports import _comparison_lock_error
from vfx_harness.evidence.compare_panels import crop_pixels, mark_pair, validate_crop
from vfx_harness.evidence.scene_checks import _control_script


def register_probe(
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
):
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
        if args.get("graph") == "world" and str(args.get("socket") or "") == "Density" and args.get("node_role"):
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
        if args.get("graph") == "world" and str(args.get("socket") or "") == "Density" and args.get("node_role"):
            probe_key = f"{args['node_role']}@{int(args['frame'])}"
            known = comparison_state.setdefault("world_density_probes", {}).setdefault(probe_key, [])
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

            diagnosis = summarize_density_probe(rows)
            if diagnosis:
                comparison_state.setdefault("world_density_probe_diagnoses", {})[probe_key] = diagnosis

        candidate = Image.open(best_path).convert("RGB")
        reference = Image.open(ref_path).convert("RGB")
        reference = crop_pixels(reference, crop) if crop else reference
        height = min(768, candidate.height, reference.height)
        cand = candidate.resize((max(1, round(candidate.width * height / candidate.height)), height))
        ref = reference.resize((max(1, round(reference.width * height / reference.height)), height))
        sheet = Image.new("RGB", (cand.width + ref.width, height), (18, 18, 22))
        sheet.paste(cand, (0, 0))
        sheet.paste(ref, (cand.width, 0))

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

    return probe_control
