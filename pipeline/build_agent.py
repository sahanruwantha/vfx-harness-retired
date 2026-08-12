"""Stage 3 — the build + critic loop for one PLAN GATE.

A BUILD agent drives a warm Blender session (run_bpy / render_*) to implement one
gate from plan.md (its tickets are the spec), iterating against a reference-scored
CRITIC until the gate's judge frame clears the bar. On pass it persists the gate's
deterministic delta script (build/NN_<gate>.py); the harness re-runs the whole chain
from an empty scene to confirm it reproduces — the scripts, not the live scene, are
the artifact of record.

Usage:
    python -m pipeline.build_agent <shot-folder> --gate <id> [--rounds 2]
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
from .ledger import Ledger, Milestone, load_axes, load_gates
from .recipes import RECIPES_DIR, build_recipe_tools

MODEL = "claude-opus-5"

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


def _prior_gate_paths(shot: Shot, gate) -> list[Path]:
    """Gate scripts that must run before this gate: all EXISTING build/NN_*.py with a
    lower numeric prefix, in order. Gates build on each other like layers."""
    def num(p: Path) -> int:
        m = re.match(r"(\d+)", p.name)
        return int(m.group(1)) if m else 10_000
    mine = num(Path(gate.script))
    build_dir = shot.folder / "build"
    if not build_dir.is_dir():
        return []
    return sorted((p for p in build_dir.glob("*.py") if num(p) < mine), key=num)


def _run_prior_paths(session: BlenderSession, paths: list[Path]) -> list[str]:
    names = []
    for p in paths:
        log(f"running prior gate script {p.name}")
        session.run(p.read_text(encoding="utf-8"))
        names.append(p.name)
    return names


def _plan_gate_excerpt(shot: Shot, gate) -> str:
    """The gate's own section of plan.md — its tickets ARE the build instructions."""
    plan = shot.folder / "plan.md"
    if not plan.is_file():
        return ""
    text = plan.read_text(encoding="utf-8")
    script_name = Path(gate.script).name
    lines = text.splitlines()
    id_pat = re.compile(rf"(?:GATE\s+{re.escape(gate.id)}\b|\b{re.escape(gate.id)}\s*·)")
    start = None
    for i, ln in enumerate(lines):
        if not ln.startswith("### "):
            continue
        # script name is unambiguous; gate id needs word-ish boundaries ("G" ⊂ "GATE L")
        if script_name in ln or id_pat.search(ln) or (gate.title and gate.title in ln):
            start = i
            break
    if start is None:
        return ""
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("### ") or lines[j].startswith("## "):
            end = j
            break
    return "\n".join(lines[start:end]).strip()


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


async def distill_recipe(shot: Shot, m: Milestone, verbose: bool = True,
                         script_rel: str | None = None) -> None:
    """Harvest reusable recipes from a passing build into the cookbook (best-effort)."""
    script_path = shot.folder / (script_rel or f"build/{m.id.lower()}.py")
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
    """Compute pass/mean from the critic's IN-SCOPE axis scores. A scaffolding stage
    marks axes a later stage delivers as "n/a" — absent-by-design must not drag the
    mean (judging gate L on emission scored it 1.25 while its own axis scored 4)."""
    raw = verdict.get("scores", {})
    scores, na = {}, []
    for k, v in raw.items():
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            scores[k] = float(v)
        else:  # "n/a", "N/A", null … — out of this stage's scope
            na.append(k)
    n = len(scores)
    mean = round(sum(scores.values()) / n, 2) if n else 0.0
    verdict["mean"] = mean
    verdict["scored_axes"] = sorted(scores)
    verdict["na_axes"] = sorted(na)
    # Thresholds must be GRANULARITY-AWARE. mean = sum/n, so one axis point of judge
    # noise moves the mean by 1/n: 0.125 across 8 axes but 1.0 across one. A scoped
    # gate with 1-2 in-scope axes must not face a harsher bar than a full acceptance
    # gate (PASS_MEAN 3.1 on a single axis silently demands a 4).
    if n and n <= 2:
        verdict["pass"] = min(scores.values()) >= 3
    else:
        verdict["pass"] = bool(scores) and mean >= PASS_MEAN and min(scores.values()) >= PASS_MIN
    return verdict


def _repro_tolerance(n_scored: int) -> float:
    """How far a canonical re-render may fall below the live best and still count as
    'the script reproduces it'. One axis point of noise = 1/n of the mean, so the
    tolerance has to widen as the scored-axis count shrinks."""
    return max(0.3, 1.0 / n_scored) if n_scored else 0.3


async def _critique(shot: Shot, m: Milestone, candidate_rel: str,
                    axes: list[tuple[str, str]], session: BlenderSession, verbose: bool,
                    scope: str | None = None) -> dict:
    text = ""
    motion_rel, motion_frames = None, None
    if shot.frontmatter.get("type") == "motion" and shot.frames > 1:
        try:  # a motion strip so motion/finish axes are judged across frames, not a still
            motion_rel, motion_frames = _stash_motion_strip(session, shot, m, candidate_rel.split("/")[-1].split(".")[0])
        except Exception as e:
            log(f"motion strip skipped: {str(e)[:80]}", 1)
    log(f"critic: scoring {candidate_rel} vs {m.ref}" + (f" (+motion {motion_frames})" if motion_rel else ""), 1)
    async for message in query(prompt=critic_prompt(shot, m, candidate_rel, axes, motion_rel,
                                                    motion_frames, scope),
                               options=_critic_options(shot)):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    text += block.text
    verdict = _gate(_extract_json(text))
    scores = ", ".join(f"{k}={v}" for k, v in verdict.get("scores", {}).items())
    na = verdict.get("na_axes") or []
    log(f"critic: {scores} | mean {verdict['mean']} (over {len(verdict.get('scored_axes', []))} "
        f"in-scope axes{f'; n/a: {len(na)}' if na else ''}) | "
        f"{'PASS ✅' if verdict['pass'] else 'REVISE ✎'}", 1)
    for issue in verdict.get("issues", [])[:6]:
        log(f"· fix: {issue}", 2)
    return verdict


def _stash_render(session: BlenderSession, shot: Shot, m: Milestone, tag: str,
                  scale: float = 0.5) -> str:
    """Render the judge frame (eevee) and copy it into the shot for the critic.
    Returns the path relative to the shot folder."""
    src = session.render(frame=m.frame, mode="eevee", scale=scale)
    dest_dir = shot.folder / "renders"
    dest_dir.mkdir(exist_ok=True)
    dest = dest_dir / f"{m.id}_{tag}.png"
    shutil.copyfile(src, dest)
    return f"renders/{dest.name}"


def _stash_motion_strip(session: BlenderSession, shot: Shot, m: Milestone, tag: str,
                        span: int = 6, scale: float = 0.4):
    """Render a few frames from the judge frame onward and montage them side by side, so the
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
async def build_unit(shot: Shot, m: Milestone, script_rel: str, prior_paths: list[Path],
                     session: BlenderSession, *, rounds: int = 2, verbose: bool = True,
                     plan_excerpt: str = "", scope: str | None = None) -> Ledger:
    """The build+critic engine for ONE unit of work. A unit is judged at m.frame vs
    m.ref and persists the delta script `script_rel`."""
    ledger = Ledger(shot)
    ledger.begin(m)

    axes = await ensure_axes(shot, verbose)  # per-shot critic rubric (derived from refs)
    bserver, bnames = build_blender_tools(session, assets_dir=shot.folder / "assets",
                                          shot_dir=shot.folder)
    rserver, rnames = build_recipe_tools()
    mcp_servers = {"blender": bserver, "recipes": rserver}
    tool_names = bnames + rnames

    # Deterministic base + prior delta scripts (a unit extends the existing scene).
    session.run(_RESET)
    session.run(_preamble(shot))
    priors = _run_prior_paths(session, prior_paths)

    passed = False
    best = {"mean": -1.0, "round": 0, "render": None, "verdict": None}
    prev_mean = None
    opts = _builder_options(shot, mcp_servers, tool_names, axes)
    async with ClaudeSDKClient(options=opts) as builder:
        await builder.query(builder_kickoff(shot, m, priors=priors,
                                            script_rel=script_rel,
                                            plan_excerpt=plan_excerpt))
        await _drain(builder, verbose)

        for rnd in range(1, rounds + 1):
            log(f"── round {rnd}/{rounds} — rendering + critiquing frame {m.frame} ──")
            t_round = time.monotonic()
            render_rel = _stash_render(session, shot, m, f"r{rnd}")
            snap = session.snapshot(f"{m.id}_r{rnd}")  # checkpoint scene STATE per round
            verdict = await _critique(shot, m, render_rel, axes, session, verbose, scope)
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
        await builder.query(finalize_prompt(shot, m, priors=priors, script_rel=script_rel))
        await _drain(builder, verbose)

    # Confirm the written script REPRODUCES the unit from an empty scene. This is a
    # reproduction check, NOT a second quality gate — with ~±0.15 judge noise, requiring
    # two consecutive gate-clears at the boundary is double jeopardy (cost M3 a pass).
    canonical_ok = await _verify_script(shot, m, script_rel, prior_paths, session, axes,
                                        ledger, verbose, live_best_mean=best["mean"],
                                        scope=scope)
    ok = passed and canonical_ok
    ledger.mark(m, "passed" if ok else "failed", best=best)
    if ok:
        try:  # harvest reusable recipes from the passing build (best-effort)
            await distill_recipe(shot, m, verbose, script_rel=script_rel)
        except Exception as e:
            log(f"distill skipped: {str(e)[:80]}")
    return ledger


async def build_gate(shot: Shot, gate, session: BlenderSession, *,
                     rounds: int = 2, verbose: bool = True) -> Ledger:
    """Build one PLAN GATE: chain lower-numbered gate scripts, implement this gate's
    tickets (its plan.md section is the spec), judge at its primary frame/ref."""
    excerpt = _plan_gate_excerpt(shot, gate)
    # An ACCEPTANCE gate (milestone-tagged) delivers an approval moment → judge it on
    # the full rubric. A SCAFFOLDING gate is deliberately unfinished → tell the critic
    # its scope so absent-by-design axes come back "n/a" instead of zeros.
    scope = None
    if not gate.milestone:
        done = "\n".join(ln for ln in excerpt.splitlines()
                          if ln.startswith(("**Scope", "**Judge artifact", "**Done")))
        scope = (f"  Gate {gate.id} — {gate.title} (one build stage of many; later gates "
                 f"add the rest of the look).\n  {gate.reads}\n{done}").strip()
    return await build_unit(shot, gate.as_milestone(), gate.script,
                            _prior_gate_paths(shot, gate), session,
                            rounds=rounds, verbose=verbose,
                            plan_excerpt=excerpt, scope=scope)


async def _verify_script(shot: Shot, m: Milestone, script_rel: str,
                         prior_paths: list[Path], session: BlenderSession,
                         axes: list[tuple[str, str]], ledger: Ledger, verbose: bool,
                         live_best_mean: float | None = None,
                         scope: str | None = None) -> bool:
    script_path = shot.folder / script_rel
    if not script_path.is_file():
        log(f"! builder never wrote {script_rel}")
        return False
    log(f"verifying {script_path.name} reproduces from an empty scene…")
    try:
        session.run(_RESET)
        session.run(_preamble(shot))
        _run_prior_paths(session, prior_paths)  # deltas assume priors ran first
        session.run(script_path.read_text(encoding="utf-8"))
    except BlenderError as e:
        log(f"! build script failed: {str(e)[:200]}")
        ledger.record_round(
            m, kind="canonical", index=0, render="",
            verdict=_gate({"scores": {}, "issues": [f"script error: {e}"]}))
        return False
    render_rel = _stash_render(session, shot, m, "canonical")
    verdict = await _critique(shot, m, render_rel, axes, session, verbose, scope)
    ledger.record_round(m, kind="canonical", index=0, render=render_rel, verdict=verdict)
    if verdict["pass"]:
        return True
    # reproduction tolerance: within judge noise of the live best still counts as
    # "the script reproduces what passed" — widened for low-axis-count gates, where a
    # single point of variance swings the mean by 1/n (SH G10: 4.0 → 3.0 on one axis).
    tol = _repro_tolerance(len(verdict.get("scored_axes", [])))
    if live_best_mean is not None and verdict["mean"] >= live_best_mean - tol:
        log(f"canonical {verdict['mean']} within noise (±{tol:.2f}) of live best "
            f"{live_best_mean} — reproduction verified")
        return True
    return False


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #
async def _run(folder: str, gate_id: str, rounds: int, blender: str) -> None:
    shot = load_shot(folder)
    session = BlenderSession(blender=blender, blend_file=None,
                             assets_dir=shot.folder / "assets",
                             cwd=shot.folder).start()
    try:
        gates = load_gates(shot)
        g = gates.get(gate_id) or gates.get(gate_id.upper())
        if g is None:
            raise SystemExit(f"unknown gate {gate_id!r}; known: {', '.join(gates)}")
        log(f"build agent: shot '{shot.id}' GATE {g.id} — {g.title} "
            f"(judge f{g.judge_frame} vs {g.judge_ref}) → {g.script}, model {MODEL}")
        ledger = await build_gate(shot, g, session, rounds=rounds)
        log(f"{g.id}: {ledger.status(g.as_milestone())}  →  {ledger.path}")
    finally:
        session.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Build one plan gate with the critic loop.")
    ap.add_argument("folder", help="shot folder (contains brief.md + refs/)")
    ap.add_argument("--gate", required=True,
                    help="gate id from gates.json (e.g. L, G20)")
    ap.add_argument("--rounds", type=int, default=2, help="max build↔critic rounds")
    ap.add_argument("--blender", default="blender", help="blender executable")
    args = ap.parse_args()
    anyio.run(_run, args.folder, args.gate, args.rounds, args.blender)


if __name__ == "__main__":
    main()
