"""Agent SDK tools for the PLAN harness — scene forensics + the spike lab.

`build_plan_tools(shot_folder, blender=...)` returns an in-process MCP server and the
qualified tool names for `ClaudeAgentOptions.allowed_tools`:

  - probe_video     ffprobe a refs video (fps / frames / duration / size)
  - contact_sheet   tiled overview of a frame range, source frame numbers burned in
  - extract_frames  up to 4 exact frames at detail, with exposure/structure metrics
  - measure_ref     objective fingerprint of a ref still (plan targets, measured)
  - ask_supervisor  a question only the client can settle; planning continues on your
                    stated assumption and a human answers before the build starts
  (video tools intentionally absent — the shot folder contains stills + brief only)
  - spike           one-shot headless Blender run to VERIFY a researched technique

Scene truth comes from these tools, not from memory: choreography is read off the
source video, fingerprints are measured off the stills, and a researched rig is
proven in the lab before it may enter a ticket.

LOGGING — same doctrine as the build harness (harness narrative via log() + full
transcript via log_message in the runner). Additionally, everything the lab produces
is PERSISTED under <shot>/logs/plan_lab/ for post-mortem forensics:
  - every image the agent saw (downscaled JPEGs, numbered in call order)
  - every spike: NN.py (full script — the transcript clips tool inputs at 400 chars),
    NN.out (full blender stdout+stderr), NN.png (render, when requested)
"""

from __future__ import annotations

import itertools
import json
import subprocess
import tempfile
import textwrap
import time
from pathlib import Path

import anyio
from claude_agent_sdk import create_sdk_mcp_server, tool

from .blender.tools import _b64, _load, _metrics_line, _stats
from .log import log

SERVER_NAME = "plan"
_MAX_TILES = 25          # 5 columns × up to 5 rows per contact sheet
_MAX_FRAMES = 4          # full-detail frames per extract_frames call
_SPIKE_TIMEOUT = 180     # s, hard cap for one headless blender run
_JPEG_Q = 85


class _CheckBatchBudget:
    """Two exploratory single checks, then a mandatory batch; scoped per plan session."""

    def __init__(self, limit: int = 2):
        self.limit = limit
        self.singles = 0

    def take_single(self) -> bool:
        if self.singles >= self.limit:
            return False
        self.singles += 1
        return True

    def reset_after_batch(self) -> None:
        self.singles = 0


def _metric_list() -> str:
    """The metric vocabulary, GENERATED from the registry.

    It was hardcoded into the tool descriptions, so `region_lit_variance` existed in
    METRICS and was advertised nowhere — the same shape as contact_sheet being defined and
    never registered. A capability nothing names is indistinguishable from one that does
    not exist.
    """
    from .checks import METRICS
    return " · ".join(sorted(METRICS))


def _text(s: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": s}], **({"is_error": True} if is_error else {})}


def _sh(cmd: list[str], timeout: int = 120) -> tuple[int, str, str]:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


# --------------------------------------------------------------------------- #
# video forensics (sync workers, called via anyio.to_thread)                   #
# --------------------------------------------------------------------------- #
def _probe(video: Path) -> dict:
    rc, out, err = _sh([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate,nb_frames,duration",
        "-of", "json", str(video),
    ])
    if rc != 0:
        raise RuntimeError(f"ffprobe failed: {err.strip()[:300]}")
    st = json.loads(out)["streams"][0]
    num, den = (st.get("r_frame_rate") or "25/1").split("/")
    fps = float(num) / float(den or 1)
    frames = int(st.get("nb_frames") or 0) or int(float(st.get("duration") or 0) * fps)
    return {"width": st.get("width"), "height": st.get("height"), "fps": fps,
            "frames": frames, "duration": float(st.get("duration") or 0)}


def _sheet(video: Path, start: int, end: int, step: int, out_dir: Path) -> tuple[Path, list[int]]:
    shown = list(range(start, end + 1, step))[:_MAX_TILES]
    end = shown[-1]
    rows = -(-len(shown) // 5)  # ceil
    vf = (f"drawtext=text='%{{frame_num}}':x=10:y=10:fontsize=48:fontcolor=yellow:"
          f"borderw=3:bordercolor=black,"
          f"select='between(n\\,{start}\\,{end})*not(mod(n-{start}\\,{step}))',"
          f"scale=380:-2,tile=5x{rows}")
    out = out_dir / f"sheet_{start}_{end}_{step}.png"
    rc, _, err = _sh(["ffmpeg", "-loglevel", "error", "-y", "-i", str(video),
                      "-vf", vf, "-vsync", "0", "-frames:v", "1", str(out)], timeout=180)
    if rc != 0 or not out.is_file():
        raise RuntimeError(f"ffmpeg sheet failed: {err.strip()[:300]}")
    return out, shown


def _frame(video: Path, n: int, fps: float, out_dir: Path) -> Path:
    out = out_dir / f"f{n:05d}.png"
    rc, _, err = _sh(["ffmpeg", "-loglevel", "error", "-y", "-ss", f"{n / fps:.4f}",
                      "-i", str(video), "-frames:v", "1", str(out)], timeout=60)
    if rc != 0 or not out.is_file():
        raise RuntimeError(f"ffmpeg frame {n} failed: {err.strip()[:300]}")
    return out


# --------------------------------------------------------------------------- #
# spike lab                                                                    #
# --------------------------------------------------------------------------- #
_SPIKE_HEADER = """\
import bpy
sc = bpy.context.scene
_engines = {i.identifier for i in
            bpy.types.RenderSettings.bl_rna.properties['engine'].enum_items}
sc.render.engine = ('BLENDER_EEVEE_NEXT' if 'BLENDER_EEVEE_NEXT' in _engines
                    else 'BLENDER_EEVEE')
for _o in list(bpy.data.objects):
    bpy.data.objects.remove(_o, do_unlink=True)
# ---- spike body ----
"""

_SPIKE_RENDER = """
# ---- spike render ----
import bpy
sc = bpy.context.scene
sc.frame_set({frame})
sc.render.resolution_x, sc.render.resolution_y = 960, 540
sc.render.filepath = r"{out}"
sc.render.image_settings.file_format = 'PNG'
bpy.ops.render.render(write_still=True)
print("SPIKE_RENDER_OK", sc.render.filepath)
"""


def _spike(blender: str, script: str, render_frame: int | None,
           timeout: int, py: Path, render_out: Path) -> dict:
    body = textwrap.dedent(script)
    src = _SPIKE_HEADER + body + (
        _SPIKE_RENDER.format(frame=render_frame, out=str(render_out))
        if render_frame is not None else "")
    py.write_text(src, encoding="utf-8")
    t0 = time.monotonic()
    rc, out, err = _sh([blender, "--background", "--factory-startup",
                        "--python", str(py)], timeout=min(timeout, _SPIKE_TIMEOUT))
    wall = time.monotonic() - t0
    full = (out + "\n--- stderr ---\n" + err).strip()
    py.with_suffix(".out").write_text(full, encoding="utf-8")
    errors = [l for l in full.splitlines()
              if any(k in l for k in ("Error", "Traceback", "error:", "Exception"))][:10]
    return {"rc": rc, "wall": wall, "tail": full[-2500:], "errors": errors,
            "render": render_out if render_out.is_file() else None}


# --------------------------------------------------------------------------- #
# the MCP server                                                               #
# --------------------------------------------------------------------------- #
def build_plan_tools(shot_folder: Path, *, blender: str = "blender",
                     lab_dir: Path | None = None):
    shot_folder = Path(shot_folder)
    work = Path(tempfile.mkdtemp(prefix="planlab-"))          # raw ffmpeg output
    lab = Path(lab_dir) if lab_dir else shot_folder / "logs" / "plan_lab"
    lab.mkdir(parents=True, exist_ok=True)
    seq = itertools.count(1)
    spikes = itertools.count(1)

    def _resolve(p: str) -> Path:
        path = Path(p)
        return path if path.is_absolute() else (shot_folder / path)

    def _keep(im, stem: str) -> Path:
        """Persist exactly what the agent saw (downscaled JPEG) for post-mortem."""
        out = lab / f"{next(seq):03d}_{stem}.jpg"
        im.save(out, format="JPEG", quality=_JPEG_Q)
        return out

    # Exploration is useful; turning fifty independent checks into fifty narrated tool
    # turns is not. This counter is scoped to one plan-agent session. Two single probes let
    # the planner learn a metric/region; after that the batch tool is the only path until a
    # batch has run, at which point two more targeted follow-ups are available.
    check_budget = _CheckBatchBudget()

    @tool(
        "probe_video",
        "ffprobe a reference video (path relative to the shot folder, e.g. "
        "'refs/source_25fps.mp4'): fps, frame count, duration, resolution. Call this "
        "FIRST so contact_sheet/extract_frames use real frame numbers.",
        {"type": "object", "properties": {"path": {"type": "string"}},
         "required": ["path"]},
    )
    async def probe_video(args):
        t0 = time.monotonic()
        try:
            info = await anyio.to_thread.run_sync(_probe, _resolve(args["path"]))
        except Exception as e:
            log(f"plan-lab ✗ probe {args['path']}: {str(e)[:120]}", 1)
            return _text(f"probe failed: {e}", is_error=True)
        log(f"plan-lab probe {args['path']} → {info['frames']}f @ {info['fps']:g}fps "
            f"{info['width']}×{info['height']} ({time.monotonic() - t0:.1f}s)", 1)
        return _text(json.dumps(info))

    @tool(
        "contact_sheet",
        "Tiled overview of a video frame range with SOURCE frame numbers burned into "
        "each tile (yellow, top-left). Args: path, start, end, step — at most 25 tiles "
        "per call (5 columns). Sweep the whole video in a few calls, then re-call with "
        "a small step to zoom into transitions. This is how you do the scene read.",
        {"type": "object",
         "properties": {"path": {"type": "string"}, "start": {"type": "integer"},
                        "end": {"type": "integer"}, "step": {"type": "integer"}},
         "required": ["path", "start", "end", "step"]},
    )
    async def contact_sheet(args):
        step = max(1, int(args["step"]))
        t0 = time.monotonic()
        try:
            path, shown = await anyio.to_thread.run_sync(
                _sheet, _resolve(args["path"]), int(args["start"]), int(args["end"]),
                step, work)
        except Exception as e:
            log(f"plan-lab ✗ sheet {args.get('start')}–{args.get('end')} "
                f"step {step}: {str(e)[:120]}", 1)
            return _text(f"contact_sheet failed: {e}", is_error=True)
        im = _load(str(path))
        kept = _keep(im, f"sheet_{shown[0]}-{shown[-1]}_s{step}")
        log(f"plan-lab sheet {shown[0]}–{shown[-1]} step {step} "
            f"({len(shown)} tiles, {time.monotonic() - t0:.1f}s) → {kept.name}", 1)
        return {"content": [
            {"type": "text", "text": f"frames {shown[0]}–{shown[-1]} step {step} "
                                     f"({len(shown)} tiles, read left→right, top→bottom)"},
            {"type": "image", "data": _b64(im), "mimeType": "image/jpeg"},
        ]}

    @tool(
        "extract_frames",
        "Up to 4 exact video frames at full detail, each with exposure + structure "
        "metrics. Use on the frames that matter (state changes, milestones, rotation "
        "checkpoints) after contact_sheet has located them.",
        {"type": "object",
         "properties": {"path": {"type": "string"},
                        "frames": {"type": "array", "items": {"type": "integer"}}},
         "required": ["path", "frames"]},
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
            blocks.append({"type": "text",
                           "text": f"[v:{n}]\n{_stats(im)}\n{_metrics_line(im)}"})
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
        {"type": "object", "properties": {"path": {"type": "string"}},
         "required": ["path"]},
    )
    async def measure_ref(args):
        try:
            im = _load(str(_resolve(args["path"])))
        except Exception as e:
            log(f"plan-lab ✗ measure {args['path']}: {str(e)[:120]}", 1)
            return _text(f"measure failed: {e}", is_error=True)
        line = f"{args['path']}\n{_stats(im)}\n{_metrics_line(im)}"
        log(f"plan-lab measure {args['path']}: "
            f"{_stats(im).removeprefix('exposure: ')}", 1)
        # The image travels WITH its numbers. This tool used to return text only, so
        # "measured it" and "looked at it" were separable — and the one plan written that
        # way scored 26 mentions of halation (which measure_ref reports) against ZERO for
        # camera angle, shadow side or solid form (which only the picture carries). Those
        # are exactly the properties no exposure statistic can express, and the resulting
        # render read as a flat card. A planner can still decline to look; it can no
        # longer measure without being shown.
        return {"content": [
            {"type": "text", "text": line},
            {"type": "image", "data": _b64(im), "mimeType": "image/jpeg"},
        ]}

    @tool(
        "measure_check",
        "RUN a candidate done-check before you commit it to a ticket. Returns its value on "
        "the reference, its value on the adversary you name, this metric's own resampling "
        "noise, and a verdict. A check may not enter the plan until this returns OK.\n"
        f"metric: {_metric_list()}.\n"
        "regions: normalised [x0,y0,x1,y1] in 0..1, origin TOP-LEFT (x right, y down) — "
        "key 'r' for the single-region metrics, "
        "'a' and 'b' for region_ratio (a/b). op: '>=' | '<=' | 'band' with lo/hi.\n"
        "rejects: paths to renders this check EXISTS TO REJECT — name the artifact showing "
        "the defect you are guarding against. Without it the check is graded against "
        "whatever bad renders happen to exist, and a check that only rejects an easy "
        "unrelated failure looks discriminating while being blind to its real target.",
        {"type": "object",
         "properties": {"id": {"type": "string"}, "metric": {"type": "string"},
                        "op": {"type": "string"}, "lo": {"type": "number"},
                        "hi": {"type": "number"}, "ref": {"type": "string"},
                        "regions": {"type": "object"},
                        "rejects": {"type": "array", "items": {"type": "string"}}},
         "required": ["metric", "op", "ref"]},
    )
    async def measure_check(args):
        if not check_budget.take_single():
            log("plan-lab x measure_check: single-call exploration cap reached", 1)
            return _text(
                "SINGLE-CHECK EXPLORATION CAP REACHED. Draft the remaining candidates as "
                "one CHECK MANIFEST and call measure_checks(checks=[...]). The cap resets "
                "after a batch so you can probe up to two rejected cases precisely.",
                is_error=True,
            )
        from .checks import Check, verify
        try:
            c = Check.from_dict({**args, "lo": args.get("lo", float("-inf")),
                                 "hi": args.get("hi", float("inf"))})
            ref = _resolve(args["ref"])
            if not ref.is_file():
                return _text(f"reference {args['ref']} does not exist", is_error=True)
            corpus = sorted(q for sib in shot_folder.parent.glob("*/renders")
                            if sib.parent.name != shot_folder.name
                            and not sib.parent.name.startswith("_")
                            for q in sib.glob("*_best.png"))
            v = await anyio.to_thread.run_sync(
                lambda: verify(c, ref, corpus, root=Path.cwd()))
        except Exception as e:
            log(f"plan-lab x measure_check {args.get('id')}: {str(e)[:120]}", 1)
            return _text(f"measure_check failed: {e}", is_error=True)
        head = "OK - this check is fit to commit" if v.ok else "REJECTED - do NOT commit this"
        body = [f"{head}",
                f"  target      {c.target()} on {c.metric}",
                f"  reference   {args['ref']} reads {v.ref_value:.4g}"
                f"  -> {'satisfies' if v.ref_value is not None and c.holds(v.ref_value) else 'FAILS'}"]
        if v.bad_values:
            caught = not all(c.holds(x) for x in v.bad_values)
            body.append(
                f"  adversary   reads {min(v.bad_values):.4g}..{max(v.bad_values):.4g}"
                f"  -> {'REJECTED by the check (good)' if caught else 'PASSES the check (BAD)'}")
        if v.floor:
            body.append(f"  noise floor {v.floor:.4g} (this metric's own movement under resampling)")
        body += [f"  ! {r}" for r in v.reasons]
        if v.ok and v.ref_value is not None:
            adv = f"[{v.bad_values[0]:.4g}]" if v.bad_values else "[]"
            body += ["", "  COPY THIS into the check's `proof` field, unedited:",
                     f'    "proof": {{"ref": {v.ref_value:.4g}, "adversary": {adv}}}',
                     "  The gate re-runs the spec you ship and compares it to these numbers. "
                     "If you", "  change the regions or thresholds afterwards, RUN IT AGAIN — "
                     "a proof that does", "  not reproduce means the spec you tested is not "
                     "the spec you shipped."]
        log(f"plan-lab measure_check {args.get('id', c.metric)}: "
            f"{'OK' if v.ok else 'REJECTED'}", 1)
        return _text("\n".join(body))

    @tool(
        "measure_checks",
        "Run MANY candidate done-checks in ONE call. Same rules and same verdicts as "
        "measure_check, but authoring 50 checks one at a time cost 91 round-trips, 112 "
        "turns and 133k output tokens in a single repair round — the model was narrating "
        "between independent measurements that have no bearing on each other. Batch them.\n"
        "Pass `checks`: a list of the same objects measure_check takes (metric, op, lo/hi, "
        "ref, regions, rejects). Returns one compact line per check plus a paste-ready "
        "`proof` block for the ones that pass. Up to 40 per call.",
        {"type": "object",
         "properties": {"checks": {"type": "array", "items": {"type": "object"}}},
         "required": ["checks"]},
    )
    async def measure_checks(args):
        from .checks import Check, verify
        specs = list(args.get("checks") or [])[:40]
        if not specs:
            return _text("no checks supplied", is_error=True)
        corpus = sorted(q for sib in shot_folder.parent.glob("*/renders")
                        if sib.parent.name != shot_folder.name
                        and not sib.parent.name.startswith("_")
                        for q in sib.glob("*_best.png"))

        def _run() -> tuple[list[str], dict]:
            lines, proofs, n_ok = [], {}, 0
            for d in specs:
                cid = str(d.get("id", "?"))
                try:
                    c = Check.from_dict({**d, "lo": d.get("lo", float("-inf")),
                                         "hi": d.get("hi", float("inf"))})
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
                    proofs[cid] = {"ref": round(v.ref_value, 4),
                                   "adversary": [round(x, 4) for x in v.bad_values[:1]]}
                    lines.append(f"  OK       {cid:10} {c.metric} {c.target()} "
                                 f"· ref {v.ref_value:.4g}"
                                 + (f" · adv {v.bad_values[0]:.4g}" if v.bad_values else ""))
                else:
                    why = v.reasons[0].split("—")[0].strip() if v.reasons else "failed"
                    detail = (v.reasons[0].split("—", 1)[1].strip()[:150]
                              if v.reasons and "—" in v.reasons[0] else "")
                    lines.append(f"  REJECTED {cid:10} {why}: {detail}")
            return lines, proofs, n_ok

        lines, proofs, n_ok = await anyio.to_thread.run_sync(_run)
        check_budget.reset_after_batch()
        head = (f"{n_ok}/{len(specs)} fit to commit. REJECTED ones must be fixed or dropped "
                f"— the gate re-runs every rule.")
        tail = ("\n\nPaste these `proof` values into the matching records, unedited. If you "
                "then change a region or threshold, RUN IT AGAIN:\n"
                + json.dumps(proofs, indent=1)) if proofs else ""
        log(f"plan-lab measure_checks: {n_ok}/{len(specs)} ok", 1)
        return _text(head + "\n" + "\n".join(lines) + tail)

    @tool(
        "spike",
        "VERIFY a researched technique in a one-shot headless Blender before it enters "
        "a ticket. Your script runs from an EMPTY scene (engine preset to EEVEE); build "
        "the minimal rig that proves the mechanism (seconds, not a look test). Pass "
        "render_frame to get a 960×540 render back; always print() the values you need "
        "to check. No bvfx helpers here — raw bpy, exactly like the internet snippet "
        "you are testing.",
        {"type": "object",
         "properties": {"script": {"type": "string"},
                        "render_frame": {"type": "integer"},
                        "timeout": {"type": "integer"}},
         "required": ["script"]},
    )
    async def spike(args):
        n = next(spikes)
        py = lab / f"spike_{n:02d}.py"
        render_out = lab / f"spike_{n:02d}.png"
        try:
            res = await anyio.to_thread.run_sync(
                _spike, blender, args["script"], args.get("render_frame"),
                int(args.get("timeout", 120)), py, render_out)
        except subprocess.TimeoutExpired:
            log(f"plan-lab ✗ spike #{n} TIMEOUT → {py.name}", 1)
            return _text(f"spike timed out — simplify the rig or raise timeout "
                         f"(script kept: logs/{lab.name}/{py.name})", is_error=True)
        except Exception as e:
            log(f"plan-lab ✗ spike #{n} launch failed: {str(e)[:120]}", 1)
            return _text(f"spike failed to launch: {e}", is_error=True)
        status = "ok" if res["rc"] == 0 and not res["errors"] else "ERRORS"
        log(f"plan-lab spike #{n} rc={res['rc']} {res['wall']:.1f}s [{status}] "
            f"→ {py.name}" + (f" + {render_out.name}" if res["render"] else ""), 1)
        for e in res["errors"][:3]:
            log(f"· {e[:160]}", 2)
        head = (f"spike #{n} · exit {res['rc']} in {res['wall']:.1f}s · "
                f"kept: logs/{lab.name}/{py.name}"
                + (f" · ERRORS: {' | '.join(res['errors'])}" if res["errors"] else ""))
        blocks = [{"type": "text", "text": f"{head}\n--- output tail ---\n{res['tail']}"}]
        if res["render"] is not None:
            im = _load(str(res["render"]))
            blocks.append({"type": "text", "text": _stats(im)})
            blocks.append({"type": "image", "data": _b64(im), "mimeType": "image/jpeg"})
        return {"content": blocks, **({"is_error": True} if res["rc"] != 0 else {})}

    # probe_video / contact_sheet / extract_frames are NOT registered: a real brief
    # arrives as reference images plus prose, never the finished shot. They were dead
    # tools whose doctrine ("SCENE READ — probe_video first") the planner still tried to
    # follow, silently degrading to five stills for a 480-frame shot. Every unregistered
    # tool is also schema text removed from every request.
    @tool(
        "ask_supervisor",
        "Raise a question ONLY the client can settle — an ambiguity in the brief, a "
        "contradiction between the brief and the stills, or a taste call that is theirs. "
        "Does not block: state the assumption you will plan on and continue. A human "
        "answers before an AFFECTED layer starts. Name the affected layer ids and/or "
        "owned axes; use global_decision only when every layer truly depends on it. "
        "Do NOT use it for anything measure_ref or a spike could answer.",
        {"type": "object",
         "properties": {"question": {"type": "string"},
                        "assumption": {"type": "string"},
                        "why_it_matters": {"type": "string"},
                        "affected_layers": {"type": "array", "items": {"type": "string"}},
                        "affected_axes": {"type": "array", "items": {"type": "string"}},
                        "global_decision": {"type": "boolean"}},
         "required": ["question", "assumption", "affected_layers"]},
    )
    async def ask_supervisor(args):
        from .escalate import ask as _ask
        qid = _ask(shot_folder, layer="PLAN", question=args["question"],
                   assumption=args["assumption"],
                   why_it_matters=args.get("why_it_matters", ""),
                   affected_layers=args.get("affected_layers") or [],
                   affected_axes=args.get("affected_axes") or [],
                   global_decision=bool(args.get("global_decision")))
        return _text(f"Recorded as Q{qid}. Continue planning on: {args['assumption']}")

    # probe_video / contact_sheet / extract_frames were DEFINED and never registered, so
    # they were unreachable on every shot — not just stills-only ones. contact_sheet's own
    # description reads "This is how you do the scene read", and it has never once been
    # callable. Nothing detected that, because an absent tool is indistinguishable from a
    # tool the model chose not to call.
    #
    # Registered conditionally on the shot actually having video: a stills-only shot should
    # not carry three tools whose every call can only fail, and a shot WITH video must not
    # silently lose its scene read. The exclusion is now a decision with a reason instead
    # of an omission.
    video = sorted((shot_folder / "refs").glob("*.mp4")) if (shot_folder / "refs").is_dir() \
        else []
    tools = [measure_ref, measure_check, measure_checks, spike, ask_supervisor]
    if video:
        tools = [probe_video, contact_sheet, extract_frames, *tools]
    server = create_sdk_mcp_server(name=SERVER_NAME, version="0.1.0", tools=tools)
    names = [f"mcp__{SERVER_NAME}__{t.name}" for t in tools]
    log(f"plan tools: {', '.join(t.name for t in tools)}"
        + (f"  ({len(video)} video ref(s))" if video else "  (stills only — video tools "
                                                          "not registered)"), 1)
    return server, names
