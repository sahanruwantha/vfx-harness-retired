"""Agent SDK tools for the PLAN harness — scene forensics + the spike lab."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import anyio
from claude_agent_sdk import tool

from vfx_harness.agents.plan_tools.constants import (
    _CALIBRATION_CLOSED,
    _MAX_FRAMES,
)
from vfx_harness.agents.plan_tools.media import _frame, _metric_list, _probe, _sheet, _text
from vfx_harness.agents.plan_tools.spike import (
    _ready_measure_refs,
)
from vfx_harness.blender.tools import _b64, _load, _metrics_line, _stats
from vfx_harness.evidence.checks import Check, verify
from vfx_harness.evidence.metrics import canonical_fingerprint
from vfx_harness.observability.log import log
from vfx_harness.orchestration import unit_plan_content


def register_session_tools(**closed):

    shot_folder = closed["shot_folder"]
    work = closed["work"]
    _resolve = closed["_resolve"]
    _keep = closed["_keep"]
    check_budget = closed["check_budget"]
    measure_cache_path = closed["measure_cache_path"]
    measure_ref_paths = closed["measure_ref_paths"]
    unit_plan_target = closed["unit_plan_target"]
    unit_plan_selected_authority = closed["unit_plan_selected_authority"]
    @tool(
        "publish_unit_plan",
        "Publish the complete bounded work-unit plan to the exact target selected by "
        "the harness. This tool intentionally accepts no path: output location is "
        "authority, not a model decision. The content must be at least 200 characters "
        "and no more than 160 lines.",
        {
            "type": "object",
            "properties": {"content": {"type": "string", "minLength": 200}},
            "required": ["content"],
            "additionalProperties": False,
        },
    )
    async def publish_unit_plan(args):
        if unit_plan_target is None:
            return _text(
                "publish_unit_plan is only available during JIT unit planning",
                is_error=True,
            )
        try:
            content = str(args.get("content") or "")
            target, lines = unit_plan_content.publish_unit_plan_content(
                shot_folder,
                unit_plan_target,
                content,
                selected_authority=unit_plan_selected_authority,
            )
        except (OSError, ValueError) as exc:
            return _text(f"unit plan publication refused: {exc}", is_error=True)
        return _text(
            f"UNIT PLAN PUBLISHED to {target.relative_to(shot_folder).as_posix()} "
            f"({lines} lines)"
        )

    @tool(
        "probe_video",
        "ffprobe a reference video (path relative to the shot folder, e.g. "
        "'refs/source_25fps.mp4'): fps, frame count, duration, resolution. Call this "
        "FIRST so contact_sheet/extract_frames use real frame numbers.",
        {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    )
    async def probe_video(args):
        t0 = time.monotonic()
        try:
            info = await anyio.to_thread.run_sync(_probe, _resolve(args["path"]))
        except Exception as e:
            log(f"plan-lab ✗ probe {args['path']}: {str(e)[:120]}", 1)
            return _text(f"probe failed: {e}", is_error=True)
        log(
            f"plan-lab probe {args['path']} → {info['frames']}f @ {info['fps']:g}fps "
            f"{info['width']}×{info['height']} ({time.monotonic() - t0:.1f}s)",
            1,
        )
        return _text(json.dumps(info))

    @tool(
        "contact_sheet",
        "Tiled overview of a video frame range with SOURCE frame numbers burned into "
        "each tile (yellow, top-left). Args: path, start, end, step — at most 25 tiles "
        "per call (5 columns). Sweep the whole video in a few calls, then re-call with "
        "a small step to zoom into transitions. This is how you do the scene read.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start": {"type": "integer"},
                "end": {"type": "integer"},
                "step": {"type": "integer"},
            },
            "required": ["path", "start", "end", "step"],
        },
    )
    async def contact_sheet(args):
        step = max(1, int(args["step"]))
        t0 = time.monotonic()
        try:
            path, shown = await anyio.to_thread.run_sync(
                _sheet, _resolve(args["path"]), int(args["start"]), int(args["end"]), step, work
            )
        except Exception as e:
            log(f"plan-lab ✗ sheet {args.get('start')}–{args.get('end')} step {step}: {str(e)[:120]}", 1)
            return _text(f"contact_sheet failed: {e}", is_error=True)
        im = _load(str(path))
        kept = _keep(im, f"sheet_{shown[0]}-{shown[-1]}_s{step}")
        log(
            f"plan-lab sheet {shown[0]}–{shown[-1]} step {step} "
            f"({len(shown)} tiles, {time.monotonic() - t0:.1f}s) → {kept.name}",
            1,
        )
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"frames {shown[0]}–{shown[-1]} step {step} "
                    f"({len(shown)} tiles, read left→right, top→bottom)",
                },
                {"type": "image", "data": _b64(im), "mimeType": "image/jpeg"},
            ]
        }

    @tool(
        "extract_frames",
        "Up to 4 exact video frames at full detail, each with exposure + structure "
        "metrics. Use on the frames that matter (state changes, milestones, rotation "
        "checkpoints) after contact_sheet has located them.",
        {
            "type": "object",
            "properties": {"path": {"type": "string"}, "frames": {"type": "array", "items": {"type": "integer"}}},
            "required": ["path", "frames"],
        },
    )
    async def extract_frames(args):
        video = _resolve(args["path"])
        frames = [int(f) for f in args["frames"]][:_MAX_FRAMES]
        try:
            fps = (await anyio.to_thread.run_sync(_probe, video))["fps"]
        except Exception as e:
            log(f"plan-lab ✗ extract probe {args['path']}: {str(e)[:120]}", 1)
            return _text(f"probe failed: {e}", is_error=True)
        blocks = []
        for n in frames:
            try:
                p = await anyio.to_thread.run_sync(_frame, video, n, fps, work)
            except Exception as e:
                log(f"plan-lab ✗ extract frame {n}: {str(e)[:120]}", 1)
                return _text(f"extract failed at frame {n}: {e}", is_error=True)
            im = _load(str(p))
            _keep(im, f"v{n:05d}")
            blocks.append({"type": "text", "text": f"[v:{n}]\n{_stats(im)}\n{_metrics_line(im)}"})
            blocks.append({"type": "image", "data": _b64(im), "mimeType": "image/jpeg"})
        log(f"plan-lab extract {frames} → {len(frames)} frames kept", 1)
        return {"content": blocks}

    @tool(
        "measure_ref",
        "A reference STILL and its objective fingerprint together — the IMAGE plus "
        "exposure mean/clipped/black, per-band structure σ and halation. Use these "
        "MEASURED numbers as the plan's look targets — never invent fingerprint values. "
        "Read the picture for everything the numbers cannot carry: camera height and "
        "angle, which faces are lit and which fall into shadow, what the silhouette "
        "does, how the light behaves in the air.",
        {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    )
    async def measure_ref(args):
        # Measurement scope follows published execution scope. Requiring the sparse DAG
        # first prevents a cold global session from measuring the whole shot before it has
        # decided what is actually due; later JIT sessions resolve their materialized view.
        due_refs = (
            {path.removeprefix("./") for path in measure_ref_paths}
            if measure_ref_paths is not None
            else _ready_measure_refs(shot_folder)
        )
        if due_refs is None:
            return _text(
                "measure_ref is unavailable until layers.json declares the sparse DAG; "
                "write ownership and the first ready unit before measuring its references",
                is_error=True,
            )
        requested = str(args["path"]).removeprefix("./")
        if requested not in due_refs:
            return _text(
                f"measure_ref refused {requested}: it is not judged by a ready global unit; "
                "measure it when its owner layer materializes",
                is_error=True,
            )
        try:
            source = _resolve(args["path"])
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
        except Exception as e:
            log(f"plan-lab ✗ measure {args['path']}: {str(e)[:120]}", 1)
            return _text(f"measure failed: {e}", is_error=True)
        try:
            cache = json.loads(measure_cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cache = {}
        if (hit := cache.get(digest)) is not None:
            # Draft already measured this exact image; re-tokenizing the picture for a
            # verify pass buys nothing deterministic. The numbers are the same numbers.
            log(f"plan-lab measure {args['path']}: cached fingerprint reused", 1)
            return _text(
                f"{args['path']}\ncanonical fingerprint: "
                f"{json.dumps(hit['fingerprint'], sort_keys=True)}\n"
                "(reused from this run's earlier measurement — the image was already "
                "shown then; Read the file only if you need to view it again)"
            )
        try:
            im = _load(str(source))
        except Exception as e:
            log(f"plan-lab ✗ measure {args['path']}: {str(e)[:120]}", 1)
            return _text(f"measure failed: {e}", is_error=True)

        fingerprint = canonical_fingerprint(im)
        cache[digest] = {"path": str(args["path"]), "fingerprint": fingerprint}
        measure_cache_path.parent.mkdir(parents=True, exist_ok=True)
        measure_cache_path.write_text(
            json.dumps(cache, indent=1, sort_keys=True) + "\n", encoding="utf-8"
        )
        line = f"{args['path']}\ncanonical fingerprint: {json.dumps(fingerprint, sort_keys=True)}"
        log(f"plan-lab measure {args['path']}: {_stats(im).removeprefix('exposure: ')}", 1)
        # The image travels WITH its numbers. This tool used to return text only, so
        # "measured it" and "looked at it" were separable — and the one plan written that
        # way scored 26 mentions of halation (which measure_ref reports) against ZERO for
        # camera angle, shadow side or solid form (which only the picture carries). Those
        # are exactly the properties no exposure statistic can express, and the resulting
        # render read as a flat card. A planner can still decline to look; it can no
        # longer measure without being shown.
        return {
            "content": [
                {"type": "text", "text": line},
                {"type": "image", "data": _b64(im), "mimeType": "image/jpeg"},
            ]
        }

    @tool(
        "measure_check",
        "RUN a candidate done-check before you commit it to a ticket. Returns its value on "
        "the reference, its value on the adversary you name, this metric's own resampling "
        "noise, and a verdict. A check may not enter the plan until this returns OK.\n"
        f"metric: {_metric_list()}.\n"
        "regions: normalised [x0,y0,x1,y1] in 0..1, origin TOP-LEFT (x right, y down) — "
        "key 'r' for the single-region metrics, "
        "region_ratio accepts exactly two semantic names in numerator/denominator order "
        "(or explicit numerator/denominator). op: '>=' | '<=' | 'band' with lo/hi.\n"
        "rejects: paths to renders this check EXISTS TO REJECT — name the artifact showing "
        "the defect you are guarding against. Without it the check is graded against "
        "whatever bad renders happen to exist, and a check that only rejects an easy "
        "unrelated failure looks discriminating while being blind to its real target.",
        {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "metric": {"type": "string"},
                "op": {"type": "string"},
                "lo": {"type": "number"},
                "hi": {"type": "number"},
                "ref": {"type": "string"},
                "regions": {"type": "object"},
                "rejects": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["metric", "op", "ref"],
        },
    )
    async def measure_check(args):
        if check_budget.closed:
            log("plan-lab x measure_check: calibration closed", 1)
            return _text(_CALIBRATION_CLOSED, is_error=True)
        if not check_budget.take_single():
            log("plan-lab x measure_check: single-call exploration cap reached", 1)
            return _text(
                "SINGLE-CHECK EXPLORATION CAP REACHED. Draft the remaining candidates as "
                "one CHECK MANIFEST and call measure_checks(checks=[...]). The cap resets "
                "after a batch so you can probe up to two rejected cases precisely.",
                is_error=True,
            )

        try:
            c = Check.from_dict({**args, "lo": args.get("lo", float("-inf")), "hi": args.get("hi", float("inf"))})
            ref = _resolve(args["ref"])
            if not ref.is_file():
                return _text(f"reference {args['ref']} does not exist", is_error=True)
            corpus = sorted(
                q
                for sib in shot_folder.parent.glob("*/renders")
                if sib.parent.name != shot_folder.name and not sib.parent.name.startswith("_")
                for q in sib.glob("*_best.png")
            )
            v = await anyio.to_thread.run_sync(lambda: verify(c, ref, corpus, root=Path.cwd()))
        except Exception as e:
            log(f"plan-lab x measure_check {args.get('id')}: {str(e)[:120]}", 1)
            return _text(f"measure_check failed: {e}", is_error=True)
        head = "OK - this check is fit to commit" if v.ok else "REJECTED - do NOT commit this"
        body = [
            f"{head}",
            f"  target      {c.target()} on {c.metric}",
            f"  reference   {args['ref']} reads {v.ref_value:.4g}"
            f"  -> {'satisfies' if v.ref_value is not None and c.holds(v.ref_value) else 'FAILS'}",
        ]
        if v.bad_values:
            caught = not all(c.holds(x) for x in v.bad_values)
            body.append(
                f"  adversary   reads {min(v.bad_values):.4g}..{max(v.bad_values):.4g}"
                f"  -> {'REJECTED by the check (good)' if caught else 'PASSES the check (BAD)'}"
            )
        if v.floor:
            body.append(f"  noise floor {v.floor:.4g} (this metric's own movement under resampling)")
        body += [f"  ! {r}" for r in v.reasons]
        if v.ok and v.ref_value is not None:
            adv = f"[{v.bad_values[0]:.4g}]" if v.bad_values else "[]"
            body += [
                "",
                "  COPY THIS into the check's `proof` field, unedited:",
                f'    "proof": {{"ref": {v.ref_value:.4g}, "adversary": {adv}}}',
                "  The gate re-runs the spec you ship and compares it to these numbers. If you",
                "  change the regions or thresholds afterwards, RUN IT AGAIN — a proof that does",
                "  not reproduce means the spec you tested is not the spec you shipped.",
            ]
        log(f"plan-lab measure_check {args.get('id', c.metric)}: {'OK' if v.ok else 'REJECTED'}", 1)
        return _text("\n".join(body))

    @tool(
        "measure_checks",
        "Run MANY candidate done-checks in ONE call. Same rules and same verdicts as "
        "measure_check, but authoring 50 checks one at a time cost 91 round-trips, 112 "
        "turns and 133k output tokens in a single repair round — the model was narrating "
        "between independent measurements that have no bearing on each other. Batch them.\n"
        "Pass `checks`: a list of the same objects measure_check takes (metric, op, lo/hi, "
        "ref, regions, rejects). Returns one compact line per check plus a paste-ready "
        "`proof` block for the ones that pass. Up to 40 per call. AT MOST TWO batches per "
        "session — the initial manifest and ONE repair of its rejects. After that "
        "calibration closes: drop unresolved candidates (image checks are optional) and "
        "proceed on executable scene contracts and build-time falsification.",
        {
            "type": "object",
            "properties": {"checks": {"type": "array", "items": {"type": "object"}}},
            "required": ["checks"],
        },
    )
    async def measure_checks(args):

        specs = list(args.get("checks") or [])[:40]
        if not specs:
            return _text("no checks supplied", is_error=True)
        if not check_budget.take_batch():
            log("plan-lab x measure_checks: calibration closed", 1)
            return _text(_CALIBRATION_CLOSED, is_error=True)
        corpus = sorted(
            q
            for sib in shot_folder.parent.glob("*/renders")
            if sib.parent.name != shot_folder.name and not sib.parent.name.startswith("_")
            for q in sib.glob("*_best.png")
        )

        def _run() -> tuple[list[str], dict]:
            lines, proofs, n_ok = [], {}, 0
            for d in specs:
                cid = str(d.get("id", "?"))
                try:
                    c = Check.from_dict({**d, "lo": d.get("lo", float("-inf")), "hi": d.get("hi", float("inf"))})
                    ref = _resolve(d.get("ref", ""))
                    if not ref.is_file():
                        lines.append(f"  REJECTED {cid:10} ref {d.get('ref')} does not exist")
                        continue
                    v = verify(c, ref, corpus, root=Path.cwd())
                except Exception as e:
                    lines.append(f"  REJECTED {cid:10} {str(e)[:90]}")
                    continue
                if v.ok:
                    n_ok += 1
                    proofs[cid] = {"ref": round(v.ref_value, 4), "adversary": [round(x, 4) for x in v.bad_values[:1]]}
                    lines.append(
                        f"  OK       {cid:10} {c.metric} {c.target()} "
                        f"· ref {v.ref_value:.4g}" + (f" · adv {v.bad_values[0]:.4g}" if v.bad_values else "")
                    )
                else:
                    why = v.reasons[0].split("—")[0].strip() if v.reasons else "failed"
                    detail = v.reasons[0].split("—", 1)[1].strip()[:150] if v.reasons and "—" in v.reasons[0] else ""
                    lines.append(f"  REJECTED {cid:10} {why}: {detail}")
            return lines, proofs, n_ok

        lines, proofs, n_ok = await anyio.to_thread.run_sync(_run)
        check_budget.reset_after_batch()
        head = (
            f"{n_ok}/{len(specs)} fit to commit. REJECTED ones must be fixed or dropped — the gate re-runs every rule."
        )
        tail = (
            (
                "\n\nPaste these `proof` values into the matching records, unedited. If you "
                "then change a region or threshold, RUN IT AGAIN:\n" + json.dumps(proofs, indent=1)
            )
            if proofs
            else ""
        )
        log(f"plan-lab measure_checks: {n_ok}/{len(specs)} ok", 1)
        return _text(head + "\n" + "\n".join(lines) + tail)

    return publish_unit_plan, probe_video, contact_sheet, extract_frames, measure_ref, measure_check, measure_checks
