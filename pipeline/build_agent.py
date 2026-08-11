"""Stage 3 — the build + critic loop for one milestone.

A BUILD agent drives a warm Blender session (run_bpy / render_*) to hit a milestone
frame, iterating against a reference-scored CRITIC until the render clears the bar.
On pass it persists a deterministic `build/<milestone>.py`, and the harness re-runs
that script from an empty scene to confirm it reproduces — the script, not the live
scene, is the artifact of record.

Usage:
    python -m pipeline.build_agent <shot-folder> [--milestone M1] [--rounds 4]
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import time
from pathlib import Path

import anyio
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    TextBlock,
    query,
)

from .blender.session import BlenderError, BlenderSession
from .blender.tools import build_blender_tools
from .brief import Shot, load_shot
from .log import log, log_message
from .build_prompts import (
    CRITIC_SYSTEM,
    builder_kickoff,
    builder_system,
    critic_prompt,
    finalize_prompt,
    revision_prompt,
)
from .ledger import Ledger, Milestone, load_axes, load_milestones
from .recipes import RECIPES_DIR, build_recipe_tools

MODEL = "claude-fable-5"

AXES_SYSTEM = """\
You define the CRITIC RUBRIC for one VFX shot. Read brief.md and the reference images,
then output the 5-7 look axes a VFX supervisor would score a render on against THESE
references — the dimensions this specific shot's look lives or dies by (e.g. composition,
atmosphere, the hero subject's detail, environment, palette, finish/grade). Make them
specific to this shot's content and style, not generic. Return ONLY a JSON array of
{"key": "snake_case", "desc": "one concrete line"} and nothing else.
"""

DISTILL_SYSTEM = """\
You harvest REUSABLE Blender recipes from a build that just passed its critic. Read the
build script. Identify 0-2 GENERAL techniques worth reusing on other shots (volumetrics,
materials, compositor/grade, instancing) — NOT shot-specific values or trivia. For each,
Write pipeline/recipes/<slug>.md with frontmatter (name, tags, blender: "5.2+", when,
verified: true), a short GOTCHAS note, and a parameterized code snippet. If a similar
recipe already exists, improve it instead of duplicating. If nothing is general enough,
write nothing and say so.
"""

# Gate: the render passes when every axis clears PASS_MIN and the mean clears
# PASS_MEAN (both on the critic's 0–5 scale). Calibrated from data: across 4 builds the
# best/canonical band is 3.1-3.3 with ~±0.15 critic noise and coupled global axes that
# REDISTRIBUTE score under revision — 3.3 sat inside that band and was missed by ≤0.16
# four times straight. 3.1 = clearly-good, reachably above the noise floor.
PASS_MIN = 2
PASS_MEAN = 3.1

_RESET = "import bpy\nbpy.ops.wm.read_factory_settings(use_empty=True)\n"


def _prior_scripts(shot: Shot, m: Milestone) -> list[Path]:
    """Build scripts of EARLIER milestones, in order. A milestone extends the same
    shot's scene — M2 = M1's scene + keyed deltas — so priors run before building,
    and the full shot is m1.py + m2.py + … applied in sequence."""
    order = list(load_milestones(shot))
    build_dir = shot.folder / "build"
    out = []
    for mid in order[:order.index(m.id)]:
        cand = [p for p in build_dir.glob("*.py") if p.stem.lower() == mid.lower()] \
            if build_dir.is_dir() else []
        if cand:
            # deterministic pick: exact-lowercase name wins over case variants
            cand.sort(key=lambda p: (p.name != f"{mid.lower()}.py", p.name))
            out.append(cand[0])
    return out


def _run_priors(session: BlenderSession, shot: Shot, m: Milestone) -> list[str]:
    names = []
    for p in _prior_scripts(shot, m):
        log(f"running prior milestone script {p.name}")
        session.run(p.read_text(encoding="utf-8"))
        names.append(p.name)
    return names


def _preamble(shot: Shot) -> str:
    """Deterministic scene setup the build script may assume is already applied."""
    return (
        "import bpy\n"
        "sc = bpy.context.scene\n"
        # resolve the EEVEE engine name for this build (5.2 = BLENDER_EEVEE, not …_NEXT)
        "_av = {e.identifier for e in bpy.types.RenderSettings.bl_rna.properties['engine'].enum_items}\n"
        "sc.render.engine = 'BLENDER_EEVEE_NEXT' if 'BLENDER_EEVEE_NEXT' in _av else 'BLENDER_EEVEE'\n"
        f"sc.render.fps = {shot.fps}\n"
        "sc.frame_start = 1\n"
        f"sc.frame_end = {shot.frames}\n"
        "sc.render.use_motion_blur = True\n"
    )


# --------------------------------------------------------------------------- #
# Agent plumbing                                                               #
# --------------------------------------------------------------------------- #
def _builder_options(shot: Shot, mcp_servers: dict, tool_names: list[str],
                     axes: list[tuple[str, str]]) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        model=MODEL,
        system_prompt=builder_system(axes),
        cwd=str(shot.folder),
        mcp_servers=mcp_servers,
        allowed_tools=["Read", "Write", "Glob", *tool_names],
        disallowed_tools=["Bash", "WebFetch", "WebSearch"],
        permission_mode="bypassPermissions",
        max_buffer_size=32 * 1024 * 1024,  # renders/read-images can exceed the 1MB default
        setting_sources=[],
        max_turns=120,  # a full look-build is turn-heavy; 60 capped mid-build
        effort="high",
    )


def _axes_options(shot: Shot) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        model=MODEL, system_prompt=AXES_SYSTEM, cwd=str(shot.folder),
        allowed_tools=["Read", "Glob"], disallowed_tools=["Write", "Edit", "Bash"],
        permission_mode="bypassPermissions", max_buffer_size=32 * 1024 * 1024,
        setting_sources=[], max_turns=6, effort="medium",
    )


def _critic_options(shot: Shot) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        model=MODEL,
        system_prompt=CRITIC_SYSTEM,
        cwd=str(shot.folder),
        allowed_tools=["Read", "Glob"],
        disallowed_tools=["Write", "Edit", "Bash"],
        permission_mode="bypassPermissions",
        max_buffer_size=32 * 1024 * 1024,  # renders/read-images can exceed the 1MB default
        setting_sources=[],
        max_turns=8,
        effort="high",
    )


def _extract_json_list(text: str) -> list:
    """Pull the last JSON array out of a reply (fenced or bare)."""
    fenced = re.findall(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    candidates = fenced or re.findall(r"(\[.*\])", text, re.DOTALL)
    for chunk in reversed(candidates):
        try:
            return json.loads(chunk)
        except json.JSONDecodeError:
            continue
    raise ValueError("no parseable JSON array")


async def ensure_axes(shot: Shot, verbose: bool = True) -> list[tuple[str, str]]:
    """The critic rubric for this shot — derived once from brief + refs, then cached."""
    path = shot.folder / "critic_axes.json"
    if path.is_file():
        return load_axes(shot)
    log("deriving critic axes from brief + refs…")
    ref_list = "\n".join(f"  - {p.name}" for p in shot.refs) or "  (none)"
    prompt = (f"Define the critic rubric for shot '{shot.id}'. Read `brief.md` and these "
              f"reference images, then output the JSON axes array:\n{ref_list}")
    text = ""
    async for message in query(prompt=prompt, options=_axes_options(shot)):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    text += block.text
    try:
        data = _extract_json_list(text)
        clean = [{"key": a["key"], "desc": a["desc"]} for a in data if a.get("key") and a.get("desc")]
        if clean:
            path.write_text(json.dumps(clean, indent=2) + "\n")
    except Exception as e:  # fall back to defaults
        log(f"axes derive failed ({str(e)[:80]}); using defaults")
    axes = load_axes(shot)
    log(f"critic axes: {', '.join(k for k, _ in axes)}")
    return axes


async def distill_recipe(shot: Shot, m: Milestone, verbose: bool = True) -> None:
    """Harvest reusable recipes from a passing build into the cookbook (best-effort)."""
    script_path = shot.folder / "build" / f"{m.id.lower()}.py"
    if not script_path.is_file():
        return
    log("distilling reusable recipes from the passing build…")
    repo = Path(__file__).resolve().parent.parent
    options = ClaudeAgentOptions(
        model=MODEL, system_prompt=DISTILL_SYSTEM, cwd=str(repo),
        allowed_tools=["Read", "Write", "Glob"], disallowed_tools=["Bash"],
        permission_mode="bypassPermissions", max_buffer_size=32 * 1024 * 1024,
        setting_sources=[], max_turns=16, effort="medium",
    )
    prompt = (f"Milestone {m.id} of shot '{shot.id}' just passed. Read its build script "
              f"`{script_path}` and the existing recipes in `{RECIPES_DIR}`. Harvest 0-2 "
              f"general, reusable techniques into pipeline/recipes/<slug>.md.")
    async for message in query(prompt=prompt, options=options):
        if verbose:
            log_message(message)


async def _drain(client: ClaudeSDKClient, verbose: bool) -> None:
    """Consume one builder response, logging reasoning/tools/results if verbose."""
    async for message in client.receive_response():
        if verbose:
            log_message(message)


def _extract_json(text: str) -> dict:
    """Pull the last JSON object out of the critic's reply (fenced or bare)."""
    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidates = fenced or re.findall(r"(\{.*\})", text, re.DOTALL)
    for chunk in reversed(candidates):
        try:
            return json.loads(chunk)
        except json.JSONDecodeError:
            continue
    raise ValueError("critic returned no parseable JSON scorecard")


def _gate(verdict: dict) -> dict:
    """Compute pass/mean from the critic's axis scores and fold them into verdict."""
    scores = {k: float(v) for k, v in verdict.get("scores", {}).items()
              if isinstance(v, (int, float))}
    mean = round(sum(scores.values()) / len(scores), 2) if scores else 0.0
    verdict["mean"] = mean
    verdict["pass"] = bool(scores) and mean >= PASS_MEAN and min(scores.values()) >= PASS_MIN
    return verdict


async def _critique(shot: Shot, m: Milestone, candidate_rel: str,
                    axes: list[tuple[str, str]], session: BlenderSession, verbose: bool) -> dict:
    text = ""
    motion_rel, motion_frames = None, None
    if shot.frontmatter.get("type") == "motion" and shot.frames > 1:
        try:  # a motion strip so motion/finish axes are judged across frames, not a still
            motion_rel, motion_frames = _stash_motion_strip(session, shot, m, candidate_rel.split("/")[-1].split(".")[0])
        except Exception as e:
            log(f"motion strip skipped: {str(e)[:80]}", 1)
    log(f"critic: scoring {candidate_rel} vs {m.ref}" + (f" (+motion {motion_frames})" if motion_rel else ""), 1)
    async for message in query(prompt=critic_prompt(shot, m, candidate_rel, axes, motion_rel, motion_frames),
                               options=_critic_options(shot)):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    text += block.text
    verdict = _gate(_extract_json(text))
    scores = ", ".join(f"{k}={v}" for k, v in verdict.get("scores", {}).items())
    log(f"critic: {scores} | mean {verdict['mean']} | "
        f"{'PASS ✅' if verdict['pass'] else 'REVISE ✎'}", 1)
    for issue in verdict.get("issues", [])[:6]:
        log(f"· fix: {issue}", 2)
    return verdict


def _stash_render(session: BlenderSession, shot: Shot, m: Milestone, tag: str,
                  scale: float = 0.5) -> str:
    """Render the milestone frame (eevee) and copy it into the shot for the critic.
    Returns the path relative to the shot folder."""
    src = session.render(frame=m.frame, mode="eevee", scale=scale)
    dest_dir = shot.folder / "renders"
    dest_dir.mkdir(exist_ok=True)
    dest = dest_dir / f"{m.id}_{tag}.png"
    shutil.copyfile(src, dest)
    return f"renders/{dest.name}"


def _stash_motion_strip(session: BlenderSession, shot: Shot, m: Milestone, tag: str,
                        span: int = 6, scale: float = 0.4):
    """Render a few frames from the milestone onward and montage them side by side, so the
    critic can judge MOTION (blur, continuous movement) — a single still at the start
    frame can't show it. Returns (rel_path, frames)."""
    from PIL import Image
    frames = sorted({m.frame, min(shot.frames, m.frame + span), min(shot.frames, m.frame + 2 * span)})
    ims = [Image.open(session.render(frame=f, mode="eevee", scale=scale)).convert("RGB") for f in frames]
    h = min(im.height for im in ims)
    ims = [im.resize((max(1, round(im.width * h / im.height)), h)) for im in ims]
    sheet = Image.new("RGB", (sum(im.width for im in ims), h), (10, 10, 12))
    x = 0
    for im in ims:
        sheet.paste(im, (x, 0)); x += im.width
    dest = shot.folder / "renders" / f"{m.id}_{tag}_motion.png"
    dest.parent.mkdir(exist_ok=True)
    sheet.save(dest)
    return f"renders/{dest.name}", frames


# --------------------------------------------------------------------------- #
# The loop                                                                     #
# --------------------------------------------------------------------------- #
async def build_milestone(shot: Shot, m: Milestone, session: BlenderSession, *,
                          rounds: int = 2, verbose: bool = True) -> Ledger:
    ledger = Ledger(shot)
    ledger.begin(m)

    axes = await ensure_axes(shot, verbose)  # per-shot critic rubric (derived from refs)
    bserver, bnames = build_blender_tools(session, assets_dir=shot.folder / "assets",
                                          shot_dir=shot.folder)
    rserver, rnames = build_recipe_tools()
    mcp_servers = {"blender": bserver, "recipes": rserver}
    tool_names = bnames + rnames

    # Start every build from the same deterministic base + earlier milestones' scripts
    # (a milestone extends the shot's existing scene, it doesn't rebuild the world).
    session.run(_RESET)
    session.run(_preamble(shot))
    priors = _run_priors(session, shot, m)

    passed = False
    best = {"mean": -1.0, "round": 0, "render": None, "verdict": None}
    prev_mean = None
    opts = _builder_options(shot, mcp_servers, tool_names, axes)
    async with ClaudeSDKClient(options=opts) as builder:
        await builder.query(builder_kickoff(shot, m, priors=priors))
        await _drain(builder, verbose)

        for rnd in range(1, rounds + 1):
            log(f"── round {rnd}/{rounds} — rendering + critiquing frame {m.frame} ──")
            t_round = time.monotonic()
            render_rel = _stash_render(session, shot, m, f"r{rnd}")
            snap = session.snapshot(f"{m.id}_r{rnd}")  # checkpoint scene STATE per round
            verdict = await _critique(shot, m, render_rel, axes, session, verbose)
            verdict["round_s"] = round(time.monotonic() - t_round, 1)
            ledger.record_round(m, kind="iter", index=rnd, render=render_rel, verdict=verdict)
            # best-of-N: keep the highest-scoring round (render AND scene snapshot)
            if verdict["mean"] > best["mean"]:
                best = {"mean": verdict["mean"], "round": rnd, "render": render_rel,
                        "verdict": verdict, "snap": snap}
                shutil.copyfile(shot.folder / render_rel, shot.folder / "renders" / f"{m.id}_best.png")
            if verdict["pass"]:
                passed = True
                break
            # stop early if a revision didn't improve (plateau) — don't burn a round
            if prev_mean is not None and verdict["mean"] <= prev_mean:
                log(f"no gain over last round ({verdict['mean']} ≤ {prev_mean}) — stopping revisions")
                break
            prev_mean = verdict["mean"]
            if rnd < rounds:
                await builder.query(revision_prompt(m, verdict, render_rel))
                await _drain(builder, verbose)
        log(f"best round: r{best['round']} mean {best['mean']} → renders/{m.id}_best.png")

        # finalize from the BEST round's scene, not the last one — a regressed revision
        # must not be what gets written into build/<m>.py (cost build8 the gate).
        if best.get("snap") and best["round"] != rnd:
            log(f"restoring best round r{best['round']} scene state before finalize")
            session.restore(best["snap"])
            await builder.query(
                f"NOTE: the scene has been RESTORED to your round-{best['round']} state "
                f"(the best-scoring round, mean {best['mean']}) — your later revision "
                f"scored worse and was discarded. Write the build script to reproduce "
                f"THIS restored scene.")
            await _drain(builder, verbose)

        # Persist the deterministic recipe regardless — it's the artifact of record.
        await builder.query(finalize_prompt(shot, m, priors=priors))
        await _drain(builder, verbose)

    # Confirm the written script REPRODUCES the milestone from an empty scene. This is a
    # reproduction check, NOT a second quality gate — with ~±0.15 judge noise, requiring
    # two consecutive gate-clears at the boundary is double jeopardy (cost M3 a pass).
    canonical_ok = await _verify_script(shot, m, session, axes, ledger, verbose,
                                        live_best_mean=best["mean"])
    ok = passed and canonical_ok
    ledger.mark(m, "passed" if ok else "failed", best=best)
    if ok:
        try:  # harvest reusable recipes from the passing build (best-effort)
            await distill_recipe(shot, m, verbose)
        except Exception as e:
            log(f"distill skipped: {str(e)[:80]}")
    return ledger


async def _verify_script(shot: Shot, m: Milestone, session: BlenderSession,
                         axes: list[tuple[str, str]], ledger: Ledger, verbose: bool,
                         live_best_mean: float | None = None) -> bool:
    script_path = shot.folder / "build" / f"{m.id.lower()}.py"
    if not script_path.is_file():  # tolerate case slips (e.g. build/M1.py)
        alt = [p for p in (shot.folder / "build").glob("*.py")
               if p.stem.lower() == m.id.lower()]
        if alt:
            script_path = alt[0]
        else:
            log(f"! builder never wrote build/{m.id.lower()}.py")
            return False
    log(f"verifying {script_path.name} reproduces from an empty scene…")
    try:
        session.run(_RESET)
        session.run(_preamble(shot))
        _run_priors(session, shot, m)  # deltas assume earlier milestones ran first
        session.run(script_path.read_text(encoding="utf-8"))
    except BlenderError as e:
        log(f"! build script failed: {str(e)[:200]}")
        ledger.record_round(
            m, kind="canonical", index=0, render="",
            verdict=_gate({"scores": {}, "issues": [f"script error: {e}"]}))
        return False
    render_rel = _stash_render(session, shot, m, "canonical")
    verdict = await _critique(shot, m, render_rel, axes, session, verbose)
    ledger.record_round(m, kind="canonical", index=0, render=render_rel, verdict=verdict)
    if verdict["pass"]:
        return True
    # reproduction tolerance: within judge noise of the live best still counts as
    # "the script reproduces what passed" (0.3 ≈ 2× observed ±0.15 critic noise)
    if live_best_mean is not None and verdict["mean"] >= live_best_mean - 0.3:
        log(f"canonical {verdict['mean']} within noise of live best {live_best_mean} — "
            f"reproduction verified")
        return True
    return False


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #
async def _run(folder: str, milestone: str, rounds: int, blender: str) -> None:
    shot = load_shot(folder)
    milestones = load_milestones(shot)
    m = milestones.get(milestone.upper())
    if m is None:
        raise SystemExit(f"unknown milestone {milestone!r}; known: {', '.join(milestones)}")

    log(f"build agent: shot '{shot.id}' milestone {m.id} (frame {m.frame}), model {MODEL}")
    session = BlenderSession(blender=blender, blend_file=None,
                             assets_dir=shot.folder / "assets").start()
    try:
        ledger = await build_milestone(shot, m, session, rounds=rounds)
    finally:
        session.close()
    log(f"{m.id}: {ledger.status(m)}  →  {ledger.path}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Build one milestone with the critic loop.")
    ap.add_argument("folder", help="shot folder (contains brief.md + refs/)")
    ap.add_argument("--milestone", default="M1", help="milestone id (default M1)")
    ap.add_argument("--rounds", type=int, default=2, help="max build↔critic rounds")
    ap.add_argument("--blender", default="blender", help="blender executable")
    args = ap.parse_args()
    anyio.run(_run, args.folder, args.milestone, args.rounds, args.blender)


if __name__ == "__main__":
    main()
