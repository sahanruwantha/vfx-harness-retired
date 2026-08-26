"""Stage 3 — the build + critic loop for one PLAN LAYER.

A BUILD agent drives a warm Blender session (run_bpy / render_*) to implement one
layer from its strict schema-declared work-unit ticket (the execution spec), iterating against a reference-scored
CRITIC until the layer's judge frame clears the bar. On pass it persists the layer's
deterministic delta script (build/NN_<layer>.py); the harness re-runs the whole chain
from an empty scene to confirm it reproduces — the scripts, not the live scene, are
the artifact of record.

Usage:
    python -m vfx_harness.agents.builder <shot-folder> --layer <id> [--rounds 2]
"""

from __future__ import annotations

import argparse
import base64
import difflib
import fnmatch
import hashlib
import io
import json
import os
import re
import shutil
import time
from dataclasses import replace
from itertools import pairwise
from pathlib import Path

import anyio
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    query,
)

from vfx_harness.agents.approach import review as approach_review
from vfx_harness.agents.approach import revision_from_review
from vfx_harness.agents.build_prompts import (
    CRITIC_SYSTEM,
    axes_own_look,
    axis_feedback_groups,
    builder_kickoff,
    builder_system,
    canonical_repair_prompt,
    capability_feedback_groups,
    critic_prompt,
    finalize_prompt,
    recurring_complaints,
    revision_prompt,
)
from vfx_harness.agents.guardrails import builder_hooks, distiller_hooks
from vfx_harness.agents.shot_context import clear_layer_context, write_layer_context
from vfx_harness.application.preflight import empty_success, warn_if_broken
from vfx_harness.domain.brief import Shot, load_shot
from vfx_harness.evidence.claim_evidence import Observation, append_gap_record, reconcile_observations
from vfx_harness.evidence.compare_panels import save_focus_sheet, validate_crop
from vfx_harness.infrastructure.config import (
    DEFAULT_CRITIC_MODEL,
    DEFAULT_EXECUTION_MODEL,
    PROJECT_ROOT,
    Settings,
    load_environment,
)
from vfx_harness.infrastructure.sandbox import sandbox_hooks
from vfx_harness.knowledge.recipes import RECIPES_DIR, build_recipe_tools, log_recipe_use, recipe_index
from vfx_harness.observability import costlog, run_artifacts, transcript
from vfx_harness.observability.log import (
    TOOL_USE,
    _result_text,
    log,
    log_message,
    reset_tool_use,
    tool_use_summary,
)
from vfx_harness.observability.provenance import check as provenance_check
from vfx_harness.observability.runid import RUN_ID
from vfx_harness.observability.runlog import bump, reset_counts, snapshot_counts
from vfx_harness.observability.runlog import summary as run_summary
from vfx_harness.observability.runlog import write as write_run
from vfx_harness.orchestration.escalate import unanswered_for_layer
from vfx_harness.orchestration.layer_state import record_round as state_round
from vfx_harness.orchestration.layer_state import start as state_start
from vfx_harness.orchestration.ledger import Ledger, Milestone, load_axes, load_layers, plan_strips

from ..blender.session import BlenderError, BlenderSession
from ..blender.tools import build_blender_tools

MODEL = DEFAULT_EXECUTION_MODEL
# The critic scores renders and runs 3-4x per layer to the builder's one session, so it
# dominates layer cost. It was fable-5 on that reasoning; it is opus-5 now because the
# verdict is the pipeline's only measure of quality and a cheaper judge is a false economy
# when every downstream decision rests on it.
#
# NOTE the calibration below: _JUDGE_SD = 0.603 and the adjudication band derived from it
# were MEASURED ON FABLE-5. They are the wrong constants for this judge until re-measured
# (`python -m vfx_harness.evaluation.cli variance <shot>`). Until then the panel is being convened on
# a noise estimate that belongs to a different model.
CRITIC_MODEL = DEFAULT_CRITIC_MODEL
_FOCUS_RENDER_LOCK = anyio.Lock()

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
Write vfx_harness/knowledge/recipes/<slug>.md with frontmatter (name, tags, blender: "5.2+", when,
verified: false), a short GOTCHAS note, and a parameterized code snippet. If a similar
recipe already exists, improve it instead of duplicating. If nothing is general enough,
write nothing and say so.

ALWAYS write `verified: false`. You are not able to verify anything — you are reading a
script, not running one. `verified: true` is set ONLY by vfx_harness.knowledge.verify_recipes, and only
after the snippet has been EXECUTED in a headless Blender and the top-level callables it
defines actually INVOKED, with what changed and a sha256 of the exact code that ran
recorded as evidence in vfx_harness/knowledge/recipes/_verified.json. `--audit` fails on any recipe
claiming verification without a matching spike entry, and the test suite asserts it — so
declaring it yourself does not merely lie to every future build, it breaks the build.

Verification is earned, by running:

    python -m vfx_harness.knowledge.verify_recipes --name <slug> --sync

which promotes the flag only if the spike actually passes.

Write your snippet so it CAN be verified: put the technique in a top-level function with
plain, defaulted arguments. Code that only runs inside a larger shot-specific block cannot
be proved to work — one recipe's shader function was never called, so verification proved
only that an unrelated loop beneath it ran, and a Blender-4 call inside that function
would have gone undetected.

If the snippet needs scaffolding before it can run — a named material or object it assumes
a real shot provides, a placeholder constant, or arguments the verifier cannot guess —
also Write vfx_harness/knowledge/recipes/_spikes/<slug>.py supplying them. Read
vfx_harness/knowledge/recipes/_spikes/README.md first; it documents the SPIKE_ARGS convention. Keep
that scaffolding OUT of the .md: find_recipe hands the recipe body to a builder verbatim,
and test scaffolding in there gets pasted straight into a shot.
"""

# Layer: the render passes when every axis clears PASS_MIN and the mean clears
# PASS_MEAN (both on the critic's 0–5 scale). Calibrated from data: across 4 builds the
# best/canonical band is 3.1-3.3, and coupled global axes REDISTRIBUTE score under
# revision — 3.3 sat inside that band and was missed by ≤0.16 four times straight.
#
# The "~±0.15 critic noise" this once claimed was WRONG, and wrong in the dangerous
# direction. Measured directly: the same render against the same reference on one axis
# scored 4.0, 3.0, 3.0, 2.0 across four repeats — a 2-point spread that flipped the
# verdict. On a one- or two-axis layer the mean IS that single number, so a lone verdict
# near the line is close to a coin flip. Hence _judge() below, which buys a second and
# third opinion exactly where the decision is uncertain.
PASS_MIN = 2
PASS_MEAN = 3.1

# Circuit breaker. Turns are a poor proxy for what we actually care about — a layer
# needing 200 cheap turns is fine, one burning $40 in 40 turns is not — so cap SPEND
# and leave turns as loose headroom. Observed: BR C $10.69 passing, BR G $15.95 while
# truncated at 120 turns. Both default to unlimited in the SDK.
MAX_BUDGET_USD = 25.0
# Advisory token countdown shown to the model. None disables it; sized from measured
# layer usage rather than one global guess, since an undersized budget causes
# premature partial completion.
TASK_BUDGET_TOKENS: int | None = None

MAX_TURNS = 400
MAX_CONTINUES = 3  # turn-cap nudges before we call the build truncated

_REPO = PROJECT_ROOT

_RESET = "import bpy\nbpy.ops.wm.read_factory_settings(use_empty=True)\n"

# Terminations that mean "the builder never finished", as opposed to "it finished badly".
_TRUNCATED = {"error_max_turns", "error_max_budget_usd"}


class BuildTruncated(RuntimeError):
    """The builder ran out of budget mid-build — no verdict is meaningful."""


class BuildUnpassed(RuntimeError):
    """A direct build completed without accepting every unit in its requested layer."""


class UnpassedPrior(RuntimeError):
    """A layer below this one was never accepted — building on it would compound it."""


def builder_model() -> str:
    """Configured live-builder/axes model; resolved after the CLI loads its environment."""
    return Settings.from_environment(load_dotenv_file=False).builder_model


def script_model() -> str:
    """Configured finalizer and canonical-repair model."""
    return Settings.from_environment(load_dotenv_file=False).script_model


def critic_model() -> str:
    """Configured authoritative visual judge model."""
    return Settings.from_environment(load_dotenv_file=False).critic_model


def distiller_model() -> str:
    return Settings.from_environment(load_dotenv_file=False).distiller_model


def _prior_layer_paths(shot: Shot, layer, *, force: bool = False) -> list[Path]:
    """Layer scripts that must run before this layer: all EXISTING build/NN_*.py with a
    lower numeric prefix, in order. Each layer stacks on the ones before it.

    Raises UnpassedPrior unless every one of them is recorded 'passed'."""

    def num(p: Path) -> int:
        m = re.match(r"(\d+)", p.name)
        return int(m.group(1)) if m else 10_000

    mine = num(Path(layer.script))
    build_dir = shot.folder / "build"
    if not build_dir.is_dir():
        return []
    found = sorted((p for p in build_dir.glob("[0-9]*.py") if num(p) < mine), key=num)
    # The build dir is a glob, the ledger is the record of what was ACCEPTED. An
    # interrupted layer leaves a script that would silently join the chain (a killed
    # seam layer left an un-critiqued 40_seam.py queued for the two after it).
    try:
        ledger, layers = Ledger(shot), load_layers(shot)
    except Exception as e:
        log(f"! chaining WITHOUT the ledger cross-check: {str(e)[:70]}")
        return found
    by_script = {Path(g.script).name: g for g in layers.values()}
    keep, unpassed = [], []
    for p in found:
        g = by_script.get(p.name)
        if g is None:
            log(f"! {p.name} matches no layer in layers.json — SKIPPING (orphan)")
            continue
        st = ledger.status(g.as_milestone())
        if st != "passed":
            unpassed.append(f"layer {g.id} ({p.name}) is '{st}'")
        else:
            # 'passed' is a verdict on a SCRIPT, not on a layer id. Editing the script
            # afterwards leaves the pass in place describing code that no longer exists,
            # and every layer above then builds on renders of the old version. Treated
            # exactly like an unpassed prior, because that is what it is.
            why = ledger.stale(g.as_milestone())
            if why:
                unpassed.append(why)
        keep.append(p)
    # FAIL CLOSED. This used to warn and chain anyway, so a layer could be built on top of
    # a predecessor whose content was never accepted — every judgement above it then rests
    # on unreviewed geometry. The protection previously lived in the shell script that
    # drove a full run, which meant running a single layer by hand silently bypassed it.
    if unpassed and not force:
        raise UnpassedPrior(
            f"refusing to build layer {layer.id} on unaccepted work: "
            + "; ".join(unpassed)
            + ". Re-run those layers, or pass --force to chain anyway (debugging only)."
        )
    if unpassed:
        log(f"! --force: chaining {len(unpassed)} unaccepted prior(s) — " + "; ".join(unpassed))
    return keep


class ChainBroken(RuntimeError):
    """A prior layer script no longer composes — the chain must be repaired first."""


def _run_prior_paths(session: BlenderSession, paths: list[Path]) -> list[str]:
    """Replay the accepted chain. A failure here is NOT this layer's fault: layer scripts
    reference each other's objects by name (30_purple.py does D.objects['tower_dot']
    from 20_green.py), so re-running an early layer can invalidate every later one and
    the break only surfaces now. Say so plainly instead of leaking a raw bpy KeyError."""
    names = []
    for p in paths:
        log(f"running prior layer script {p.name}")
        try:
            session.run(p.read_text(encoding="utf-8"))
        except BlenderError as e:
            first = str(e).strip().splitlines()[0]
            raise ChainBroken(
                f"{p.name} no longer composes onto the scene built by the layers before "
                f"it: {first}\n  The chain is broken, not this layer. Re-run {p.name}'s "
                f"layer (or restore the prior script it was authored against) before "
                f"building further."
            ) from e
        names.append(p.name)
    return names


def _plan_layer_excerpt(shot: Shot, layer, unit=None) -> str:
    """The layer's just-in-time execution plan; giant-plan fallback is forbidden."""
    from vfx_harness.orchestration.layer_plans import read_layer_plan, read_work_unit_plan

    return read_work_unit_plan(shot.folder, layer, unit) if unit is not None else read_layer_plan(shot.folder, layer)


def _preamble(shot: Shot) -> str:
    """Deterministic scene setup the build script may assume is already applied."""
    return (
        "import bpy\n"
        "sc = bpy.context.scene\n"
        # resolve the EEVEE engine name for this build (5.2 = BLENDER_EEVEE, not …_NEXT)
        "_av = {e.identifier for e in bpy.types.RenderSettings.bl_rna.properties['engine'].enum_items}\n"
        "sc.render.engine = 'BLENDER_EEVEE_NEXT' if 'BLENDER_EEVEE_NEXT' in _av else 'BLENDER_EEVEE'\n"
        f"sc.render.fps = {shot.fps}\n"
        f"sc.render.resolution_x = {shot.resolution[0]}\n"
        f"sc.render.resolution_y = {shot.resolution[1]}\n"
        "sc.render.resolution_percentage = 100\n"
        "sc.frame_start = 1\n"
        f"sc.frame_end = {shot.frames}\n"
        "sc.render.use_motion_blur = True\n"
    )


# --------------------------------------------------------------------------- #
# Agent plumbing                                                               #
# --------------------------------------------------------------------------- #
def _builder_options(
    shot: Shot,
    mcp_servers: dict,
    tool_names: list[str],
    axes: list[tuple[str, str]],
    ref_rel: str | None = None,
    script_rel: str | None = None,
    phase: dict[str, str] | None = None,
    ticket_context: str | None = None,
) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        model=builder_model(),
        system_prompt=builder_system(
            axes,
            recipe_index(context=ticket_context) if ticket_context is not None else recipe_index(),
            ticket_context=ticket_context,
        ),
        cwd=str(shot.folder),
        hooks=builder_hooks(
            shot.folder, [shot.folder, RECIPES_DIR], ref_rel=ref_rel, script_rel=script_rel, phase=phase
        ),
        mcp_servers=mcp_servers,
        # LIVE_BUILD owns the warm Blender scene, never the artifact on disk.  Write/Edit
        # are absent rather than merely prompt-discouraged; publication and repair use
        # dedicated sessions below with mutually exclusive mutation surfaces.
        allowed_tools=["Read", "Glob", "Grep", "WebFetch", *tool_names],
        # allowed_tools is an AUTO-APPROVE list, not a whitelist: under bypassPermissions
        # every unlisted tool still runs. Deny explicitly or it is available.
        # WebFetch is allowed but hook-restricted to Blender docs (see guardrails):
        # with no lookup at all the builder re-guesses a failing API verbatim.
        disallowed_tools=["Write", "Edit", "Bash", "Task", "Agent", "NotebookEdit", "KillShell", "BashOutput"],
        permission_mode="bypassPermissions",
        max_buffer_size=32 * 1024 * 1024,  # renders/read-images can exceed the 1MB default
        # "project" loads shots/<id>/CLAUDE.md on EVERY request, so the layer contract
        # survives compaction — the kickoff message does not.
        setting_sources=["project"],
        max_turns=MAX_TURNS,  # headroom only — MAX_BUDGET_USD is the real stop
        max_budget_usd=MAX_BUDGET_USD,
        # Three different jobs, and they are not substitutes:
        #   task_budget      ADVISORY — the model sees the countdown and can reserve room
        #                    to finish and validate instead of being cut off mid-thought
        #   max_budget_usd   ENFORCED financial ceiling
        #   max_turns        runaway-loop backstop
        # Advisory pacing does not fix a loop that cannot converge — layer 2 burned 46
        # rounds and layer 5 sixteen because nothing could FAIL them on the axis that
        # mattered, and a countdown would only have stopped them sooner with less to show.
        # It is here because being cut off mid-script is strictly worse than landing early.
        **({"task_budget": TASK_BUDGET_TOKENS} if TASK_BUDGET_TOKENS else {}),
        effort="high",
    )


_SCRIPT_SYSTEM = """\
You are a narrow build-artifact agent. Follow the requested MODE exactly. You do not have
Blender tools and must not redesign the scene. In FINALIZE_SCRIPT, publish the complete
requested script once with Write and never Edit it. In REPAIR_SCRIPT, make only the stated
local correction with Edit and never replace the whole file. Read only the named script,
journal, plan, and verdict evidence needed for that operation. Your working directory is
already the shot folder: use every named relative path verbatim. Never prefix a path with
the repository root or guess an alternative location.
"""


def _build_probe_candidate_server(shot: Shot, script_rel: str, probe_ctx: dict):
    """One tool that lets a script session SEE the scene its artifact rebuilds.

    Run 20260824T103842Z-afec73's canonical repairs reasoned soundly from text findings
    alone and rewrote a correct script into one whose camera faced away from the set at
    every frame — the rebuilt consequences of an edit were invisible to the session
    editing it. probe_candidate rebuilds the CURRENT artifact in a disposable worker and
    returns the authoritative evidence rows, evaluated camera/role world transforms at
    the judge frames, and a small solid render per frame."""
    import anyio
    from claude_agent_sdk import create_sdk_mcp_server, tool

    calls = 0

    def _probe() -> dict:
        from vfx_harness.blender.session import BlenderSession
        from vfx_harness.evidence.scene_checks import layer_evidence as scene_layer_evidence

        probe_dir = Path(probe_ctx["scratch_dir"])
        probe_dir.mkdir(parents=True, exist_ok=True)
        verify = BlenderSession(
            blender=probe_ctx["blender"], artifacts_dir=probe_dir, cwd=None
        ).start()
        try:
            verify.run(_RESET, journal=False)
            verify.run(_preamble(shot), journal=False)
            for prior in probe_ctx["prior_paths"]:
                verify.run(Path(prior).read_text(encoding="utf-8"), journal=False)
            verify.run((shot.folder / script_rel).read_text(encoding="utf-8"), journal=False)
            try:
                rig_contract = verify.check(kind="rig_contract")
            except Exception as exc:
                rig_contract = {"ok": None, "issues": [f"check failed: {str(exc)[:120]}"]}
            role_patterns = list(probe_ctx.get("roles") or [])
            frames_out = []
            for frame, ref in probe_ctx["judges"]:
                rows = scene_layer_evidence(
                    shot.folder, str(probe_ctx["layer_id"]), frame=int(frame), session=verify
                )
                transforms = verify.run(
                    "import bpy, json, math, fnmatch\n"
                    f"sc=bpy.context.scene; sc.frame_set({int(frame)})\n"
                    "dg=bpy.context.evaluated_depsgraph_get()\n"
                    "out={'camera': None, 'roles': {}}\n"
                    "cam=sc.camera\n"
                    "if cam:\n"
                    "    ev=cam.evaluated_get(dg)\n"
                    "    out['camera']={'name':cam.name,\n"
                    "        'world_location':[round(v,3) for v in ev.matrix_world.translation],\n"
                    "        'world_rotation_deg':[round(math.degrees(a),1) for a in ev.matrix_world.to_euler()]}\n"
                    f"patterns={json.dumps(role_patterns)}\n"
                    "for o in sc.objects:\n"
                    "    role=str(o.get('bvfx_role') or '')\n"
                    "    if role and (not patterns or any(fnmatch.fnmatchcase(role,p) for p in patterns)):\n"
                    "        bucket=out['roles'].setdefault(role,[])\n"
                    "        if len(bucket)<8:\n"
                    "            ev=o.evaluated_get(dg)\n"
                    "            bucket.append({'name':o.name,\n"
                    "                'world_location':[round(v,3) for v in ev.matrix_world.translation]})\n"
                    "RESULT=out\n",
                    journal=False,
                ).get("result") or {}
                try:
                    render = verify.render(frame=int(frame), mode="solid", scale=0.33)
                except Exception as exc:  # a render failure is a finding, not a crash
                    render = f"render failed: {str(exc)[:120]}"
                frames_out.append({
                    "frame": int(frame),
                    "ref": str(ref),
                    "camera": transforms.get("camera"),
                    "roles": transforms.get("roles"),
                    "solid_render": render,
                    "evidence": [
                        {
                            key: row.get(key)
                            for key in ("id", "kind", "value", "target", "pass", "error", "note")
                            if row.get(key) not in (None, "")
                        }
                        for row in rows
                    ],
                })
            return {"script": script_rel, "rig_contract": rig_contract, "frames": frames_out}
        finally:
            verify.close()

    @tool(
        "probe_candidate",
        f"Rebuild the CURRENT `{script_rel}` from an empty scene in a disposable worker "
        "and return, per judge frame: the authoritative evidence rows the gate will "
        "compute, the evaluated camera and role world transforms, and a solid-mode "
        "render you can Read as an image. Call it BEFORE diagnosing and AFTER editing — "
        "an edit whose rebuilt consequences you have not seen is a guess. Deterministic, "
        "no model cost; capped at three calls.",
        {"type": "object", "properties": {}},
    )
    async def probe_candidate(args):
        nonlocal calls
        calls += 1
        if calls > 3:
            return {"content": [{"type": "text", "text": "probe_candidate call cap reached (3)"}], "is_error": True}
        try:
            result = await anyio.to_thread.run_sync(_probe)
        except Exception as exc:
            log(f"! probe_candidate failed: {str(exc)[:120]}", 1)
            return {"content": [{"type": "text", "text": f"probe failed: {exc}"}], "is_error": True}
        return {"content": [{"type": "text", "text": json.dumps(result, indent=1)}]}

    server = create_sdk_mcp_server(name="candidate", version="0.1.0", tools=[probe_candidate])
    return server, ["mcp__candidate__probe_candidate"]


def _script_options(
    shot: Shot, *, mode: str, script_rel: str, probe_ctx: dict | None = None
) -> ClaudeAgentOptions:
    finalize = mode == "finalize"
    phase = {"mode": mode}
    mcp_servers = {}
    probe_tools: list[str] = []
    if probe_ctx is not None:
        server, probe_tools = _build_probe_candidate_server(shot, script_rel, probe_ctx)
        mcp_servers["candidate"] = server
    if not finalize:
        # repairs design mechanisms; the cookbook's harness lessons (rig aim ownership,
        # slotted actions, …) are exactly the knowledge blind repairs lacked
        from vfx_harness.knowledge.recipes import build_recipe_tools

        recipe_server, recipe_names = build_recipe_tools()
        mcp_servers["recipes"] = recipe_server
        probe_tools = [*probe_tools, *recipe_names]
    return ClaudeAgentOptions(
        model=script_model(),
        system_prompt=_SCRIPT_SYSTEM,
        cwd=str(shot.folder),
        hooks=builder_hooks(shot.folder, [shot.folder], script_rel=script_rel, phase=phase),
        mcp_servers=mcp_servers,
        allowed_tools=(
            [*(["Read", "Write", "Glob"] if finalize else ["Read", "Edit", "Grep"]), *probe_tools]
        ),
        disallowed_tools=(
            ["Edit", "Bash", "WebFetch", "WebSearch", "Task", "Agent", "NotebookEdit"]
            if finalize
            else ["Write", "Glob", "Bash", "WebFetch", "WebSearch", "Task", "Agent", "NotebookEdit"]
        ),
        permission_mode="bypassPermissions",
        setting_sources=[],
        max_turns=20,
        max_budget_usd=MAX_BUDGET_USD,
        max_buffer_size=32 * 1024 * 1024,
        effort="high",
    )


async def _run_script_agent(
    shot: Shot,
    *,
    mode: str,
    script_rel: str,
    prompt: str,
    verbose: bool,
    probe_ctx: dict | None = None,
) -> dict:
    """Run one phase-pure file session and return its terminal SDK accounting.

    ``query()`` is deliberately not used here.  On ``error_max_turns`` its subprocess
    raises after yielding the terminal result, which used to escape the canonical repair
    transaction after Edit had already mutated the artifact.  A streaming client keeps
    the same narrow session alive across the checkpoint, just as the live builder does.
    """
    async with ClaudeSDKClient(
        options=_script_options(shot, mode=mode, script_rel=script_rel, probe_ctx=probe_ctx)
    ) as agent:
        await agent.query(prompt)
        info = await _drain_once(agent, verbose)
        if info["subtype"] == "error_max_turns":
            log(
                f"⏸ {mode} agent hit its turn checkpoint ({info['turns']} turns) — "
                "continuing once to finish the in-progress artifact operation",
                1,
            )
            await agent.query(
                f"MODE remains {mode.upper()}_SCRIPT. Continue from the exact file state "
                "you just left. Do not discover more files or broaden the repair. Finish "
                f"the smallest necessary operation on `{script_rel}`, summarize it, and stop."
            )
            info = await _drain_once(agent, verbose)
        return info


def _axes_options(shot: Shot) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        model=builder_model(),
        system_prompt=AXES_SYSTEM,
        cwd=str(shot.folder),
        hooks=sandbox_hooks(shot.folder, cwd=shot.folder),
        allowed_tools=["Read", "Glob"],
        disallowed_tools=["Write", "Edit", "Bash", "Grep", "WebFetch", "WebSearch", "Task", "Agent", "NotebookEdit"],
        permission_mode="bypassPermissions",
        max_buffer_size=32 * 1024 * 1024,
        setting_sources=[],
        max_turns=6,
        effort="low",  # one cheap classification
    )


def _critic_schema(
    axes: list[tuple[str, str]],
    *,
    allow_na: bool = True,
    focus_frames: list[int] | None = None,
) -> dict:
    """Force the verdict shape instead of regex-scraping the last {...} out of prose.
    Layer builds pass only their owned axes, so scope is no longer a model decision there.
    Full-rubric/acceptance calls may still need n/a for beat-specific axes."""
    numeric = {"type": "integer", "minimum": 0, "maximum": 5}
    score = {"anyOf": [numeric, {"type": "string", "enum": ["n/a"]}]} if allow_na else numeric
    return {
        "type": "object",
        "properties": {
            "scores": {
                "type": "object",
                "properties": {k: score for k, _ in axes},
                "required": [k for k, _ in axes],
                "additionalProperties": False,
            },
            "observations": {
                "type": "array",
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "kind": {"type": "string", "enum": ["qualitative", "measurable"]},
                        "axis": {"type": "string", "enum": [k for k, _ in axes]},
                        "property": {"type": "string"},
                        "observation": {"type": "string"},
                        "action": {"type": "string"},
                        "moment": {
                            "type": "integer",
                            **({"enum": sorted(set(focus_frames))} if focus_frames else {"minimum": 1}),
                        },
                        "roles": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                        },
                        "claim_id": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        "check_ids": {"type": "array", "items": {"type": "string"}},
                        "panel_ids": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": [
                        "id",
                        "kind",
                        "axis",
                        "property",
                        "observation",
                        "action",
                        "moment",
                        "roles",
                        "claim_id",
                        "check_ids",
                        "panel_ids",
                    ],
                    "additionalProperties": False,
                },
                "description": "One typed observation for each axis scored below 3. "
                "Bind planned defects to one exact claim and its evidence ids. Use a null "
                "claim_id only for a coverage defect absent from the supplied claim manifest.",
            },
            "focus_requests": {
                "type": "array",
                "maxItems": 2,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "short stable id"},
                        "axis": {"type": "string", "enum": [k for k, _ in axes]},
                        "source": {
                            "type": "string",
                            "enum": ["candidate_frame", "motion_strip"],
                            "description": "coordinate space used by region",
                        },
                        "source_frame": {
                            "type": "integer",
                            **({"enum": sorted(set(focus_frames))} if focus_frames else {"minimum": 1}),
                            "description": "shot frame whose detail must be rerendered",
                        },
                        "region": {
                            "type": "array",
                            "items": {"type": "number", "minimum": 0, "maximum": 1},
                            "minItems": 4,
                            "maxItems": 4,
                        },
                        "reason": {"type": "string"},
                    },
                    "required": ["id", "axis", "source", "source_frame", "region", "reason"],
                    "additionalProperties": False,
                },
                "description": "At most two normalized TOP-LEFT regions needed to resolve "
                "a material below-3/uncertain visual decision. source=candidate_frame "
                "uses coordinates local to source_frame; source=motion_strip uses global "
                "strip coordinates and must remain inside that frame's one panel. Empty "
                "when the supplied images are sufficient.",
            },
            # Asked EXPLICITLY because the critic will otherwise mention a bad reference
            # in `issues` and score anyway: handed a render of a night city against a
            # green meadow, it wrote "cannot be the shot's look reference" and returned
            # camera_framing=4, PASS — silently grading the frame against the brief's
            # prose instead of an image. Ref-relative scoring is the whole premise, so
            # this has to be a first-class field, not a remark.
            "reference_usable": {
                "type": "boolean",
                "description": "false if the REFERENCE image is not a plausible target "
                "for this candidate at all (wrong shot, wrong beat, "
                "corrupt, blank). Absent-by-design content that a LATER "
                "layer adds does NOT make a reference unusable.",
            },
            "reference_note": {"type": "string", "description": "one line; required when unusable"},
        },
        "required": ["scores", "observations", "focus_requests", "reference_usable", "reference_note"],
        "additionalProperties": False,
    }


def _critic_options(
    shot: Shot,
    axes: list[tuple[str, str]] | None = None,
    *,
    allow_na: bool = True,
    focus_frames: list[int] | None = None,
) -> ClaudeAgentOptions:
    # NO TOOLS. The images arrive attached to the request (see _critique), so the critic
    # has nothing to fetch and cannot score a frame it never saw. This deleted three
    # layers of machinery that existed only to police the old tool loop: the sandbox
    # redirect for critic reads, the request/result id pairing, and the blind-critic guard.
    return ClaudeAgentOptions(
        model=critic_model(),
        system_prompt=CRITIC_SYSTEM,
        cwd=str(shot.folder),
        allowed_tools=[],
        disallowed_tools=[
            "Read",
            "Glob",
            "Write",
            "Edit",
            "Bash",
            "Grep",
            "WebFetch",
            "WebSearch",
            "Task",
            "Agent",
            "NotebookEdit",
        ],
        permission_mode="bypassPermissions",
        max_buffer_size=32 * 1024 * 1024,  # a multi-image request exceeds the 1MB default
        setting_sources=[],
        # Structured output can require a final protocol turn after the model has
        # finished reasoning. In the real Layer 1 run, both first attempts exhausted a
        # two-turn ceiling and both retries succeeded. Three turns are protocol headroom,
        # not an invitation to loop: the critic has no tools and only one user message.
        max_turns=3,
        # The judgement everything depends on — but "xhigh" here was an assertion, never a
        # measurement, and it is the single largest cost in the pipeline. Measured on layer
        # 1: the critic produced 5-12k output per session for $4.18-11.31, i.e. $0.63-1.28
        # per 1k output, against the builder's $0.07-0.09 — 9-18x more per token, while
        # reading HALF the cache. Extended thinking is billed as output and does not appear
        # in the output field, which is where the money goes. Configurable so the claim can
        # be tested with `evals variance` instead of argued about.
        effort=CRITIC_EFFORT,
        # Validated at the tool layer with automatic retries, instead of scraping the
        # last {...} out of free text — one critic already returned nothing parseable.
        output_format=(
            {
                "type": "json_schema",
                "schema": _critic_schema(axes, allow_na=allow_na, focus_frames=focus_frames),
            }
            if axes
            else None
        ),
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
    """The critic rubric for this shot.

    Written by the PLAN stage (critic_axes.json): the planner has the deepest scene read
    AND knows the layer breakdown, so it is the only stage that can guarantee every axis
    has an owning layer. Missing axes are a migration failure, not an invitation for the
    builder to invent a different rubric.
    """
    from vfx_harness.orchestration.plan_authority import selected_artifact_path

    path = selected_artifact_path(shot.folder, "critic_axes.json")
    if path.is_file():
        return load_axes(shot)
    raise FileNotFoundError(
        f"{path} missing — legacy builder-side rubric derivation has been removed; "
        "generate and gate the strict global plan"
    )


def _warn_unowned_axes(shot: Shot, axes: list[tuple[str, str]]) -> None:
    """Audit the axis↔layer mapping in BOTH directions — each catches a real bug we hit.

    axis with no layer  → unearnable: nobody can ever score it.
    layer with no axis  → unjudgeable: the layer is scored purely on OTHER layers' work,
                         so its own contribution is invisible and its revisions polish
                         someone else's layer (SH G60 "ENVIRONMENT" scored only on
                         typography and palette until `environment_depth` was added).

    An axis owned only by the LAST layer is fine and deliberately NOT flagged: with
    `owns` in force the earlier layers simply aren't judged on it, which is the point.
    """
    layers = load_layers(shot)
    if not any(g.owns for g in layers.values()):
        raise ValueError("strict layers.json contract requires owned axes; legacy unscoped judging is not supported")
    keys = {k for k, _ in axes}
    orphan = sorted(keys - {a for g in layers.values() for a in g.owns})
    if orphan:
        log(f"! axes owned by NO layer — unearnable: {orphan}")
    mute = sorted(g.id for g in layers.values() if not g.owns)
    if mute:
        log(f"! layers owning NO axis — judged only on other layers' work: {mute}")
    unknown = sorted({a for g in layers.values() for a in g.owns} - keys)
    if unknown:
        log(f"! layers claim axes not in the rubric: {unknown}")


def _owned_axes(axes: list[tuple[str, str]], layer) -> list[tuple[str, str]]:
    """Deterministically scope a layer before prompting or schema construction."""
    owned = set(getattr(layer, "owns", ()) or ())
    return [row for row in axes if row[0] in owned]


_MOTION_AXIS_WORDS = (
    "motion",
    "animation",
    "continuity",
    "timing",
    "trajectory",
    "interpolation",
    "velocity",
    "monotonic",
    "easing",
)


def _axes_need_motion(axes: list[tuple[str, str]]) -> bool:
    """Legacy-free helper for unlayered acceptance/eval calls only.

    Staged layers use their explicit ``temporal_evidence`` policy.  This heuristic remains
    for acceptance milestones, which do not have work-unit manifests.
    """
    text = " ".join(f"{key} {description}" for key, description in axes).lower()
    return any(word in text for word in _MOTION_AXIS_WORDS)


def _layer_needs_motion(layer) -> bool:
    return getattr(layer, "temporal_evidence", None) == "motion"


def _evidence_convergence_stop(layer, verdict: dict) -> bool:
    """Stop revisions when executable owned evidence has nothing left to repair.

    This does not manufacture a PASS: canonical verification may still record a judge
    conflict.  It prevents an evidence-free low score from sending the builder into more
    geometry edits after every authoritative contract is green.
    """
    if layer is None or verdict.get("pass"):
        return False
    authoritative = [row for row in (verdict.get("evidence") or []) if row.get("authoritative")]
    layer_id = str(getattr(layer, "id", ""))
    owned = [
        row for row in authoritative if str(row.get("fault_owner") or row.get("owner_layer") or layer_id) == layer_id
    ]
    return bool(
        owned
        and all(row.get("pass") for row in authoritative)
        and not verdict.get("issues")
        and not verdict.get("reference_unusable")
    )


def _builder_ticket_context(plan_excerpt: str, scope: str | None, axes: list[tuple[str, str]], layer=None) -> str:
    """Local retrieval query for prompt modules; never sent as extra prompt text.

    The full excerpt contains gotchas and regression notes about other departments. Feeding
    all of that to retrieval makes a finish ticket look like camera+city+asset work again.
    Headings, scope/done clauses, approach lines and explicit recipe calls express what this
    layer can actually change; the builder still receives the complete excerpt at kickoff.
    """
    ticket_lines = []
    for line in plan_excerpt.splitlines():
        stripped = line.strip()
        if (
            stripped.startswith(("### ", "**Scope", "**Done", "**G"))
            or "build/approach:" in stripped
            or "find_recipe(" in stripped
        ):
            ticket_lines.append(stripped)
    layer_bits = [str(getattr(layer, key, "") or "") for key in ("title", "reads", "script")]
    axis_bits = [f"{name}: {description}" for name, description in axes]
    return "\n".join(x for x in ("\n".join(ticket_lines), scope or "", *layer_bits, *axis_bits) if x)


async def distill_recipe(
    shot: Shot, m: Milestone, verbose: bool = True, script_rel: str | None = None, errors: list[str] | None = None
) -> None:
    """Harvest reusable recipes into the cookbook (best-effort).

    Two sources, either of which is enough to be worth a pass: a build script that
    PASSED (proven technique) and API errors the builder hit and worked around (proven
    gotcha). The second used to be discarded entirely.
    """
    script_path = shot.folder / (script_rel or f"build/{m.id.lower()}.py")
    if not script_path.is_file() and not errors:
        return
    what = []
    if script_path.is_file():
        what.append("the passing build")
    if errors:
        what.append(f"{len(errors)} self-corrected error(s)")
    log(f"distilling reusable recipes from {' + '.join(what)}…")
    repo = PROJECT_ROOT
    options = ClaudeAgentOptions(
        model=distiller_model(),
        system_prompt=DISTILL_SYSTEM,
        cwd=str(repo),
        hooks=distiller_hooks(RECIPES_DIR, shot.folder, cwd=repo),
        allowed_tools=["Read", "Write", "Glob"],
        # Grep is why the distiller walked out to ~/.claude and read this session's
        # transcript looking for context on an error message.
        disallowed_tools=["Bash", "Grep", "WebFetch", "WebSearch", "Task", "Agent"],
        permission_mode="bypassPermissions",
        max_buffer_size=32 * 1024 * 1024,
        setting_sources=[],
        max_turns=16,
        effort="medium",
    )
    parts = [
        f"Layer {m.id} of shot '{shot.id}' just finished. Existing recipes are in "
        f"`{RECIPES_DIR}` — improve one rather than duplicating it."
    ]
    if script_path.is_file():
        parts.append(f"It PASSED: read its build script `{script_path}` and harvest 0-2 general, reusable techniques.")
    if errors:
        joined = "\n".join(f"  - {e}" for e in errors[:8])
        parts.append(
            f"It also hit these API errors and worked around them:\n{joined}\n"
            f"Each cost the builder turns and will cost the next builder the same. For "
            f"any that is a GENERAL Blender-5 gotcha (not a shot-specific typo), record "
            f"the correct usage. VERIFY the correct form against the recipes or the "
            f"script before writing it — a confidently wrong recipe is worse than none, "
            f"so if you cannot confirm the fix, write nothing for that error."
        )
    prompt = " ".join(parts)

    async def _run_distiller():
        async for message in query(prompt=prompt, options=options):
            if verbose:
                log_message(message)
            elif isinstance(message, ResultMessage):
                costlog.record(message)

    if costlog.is_bound():
        with costlog.scoped(role="distiller", phase="distill"):
            await _run_distiller()
    else:
        costlog.bind(shot.folder, role="distiller", phase="distill", layer=m.id, run_id=RUN_ID)
        try:
            await _run_distiller()
        finally:
            costlog.unbind()


# API errors the builder hit and worked around. The distiller only ever harvested from
# PASSING builds, so a gotcha the builder fumbled and recovered from left no trace —
# four distinct Blender-5 errors self-corrected in one run and taught the cookbook
# nothing. These are the highest-value recipes precisely because they cost turns.
_ERRORS: list[str] = []
_RECIPES_USED: list[str] = []
_JOURNAL_INFO: dict = {}


# The builder's own statement of what it is going to do, captured once per layer. The
# journal records the run_bpy calls it made; nothing recorded the reasoning that produced
# them, so whether the recipe index actually changed what it reached for was unanswerable
# without reading raw SDK transcripts.
_APPROACH: dict = {}


def _collect_approach(message) -> None:
    if _APPROACH.get("text") or not isinstance(message, AssistantMessage):
        return
    for b in message.content:
        if not isinstance(b, TextBlock):
            continue
        for line in b.text.splitlines():
            if line.strip().upper().startswith("APPROACH:"):
                _APPROACH["text"] = line.split(":", 1)[1].strip()[:400]
                log(f"approach: {_APPROACH['text'][:120]}", 1)
                return


def _collect_errors(message) -> None:
    """Reuse log's extractor: a tool result's content may be a str, a list of dicts, or a
    list of BLOCK OBJECTS. My first version only handled the first two, so object-shaped
    results extracted to "" and were dropped — it collected 1 of 2 errors on BR layer S."""
    for b in getattr(message, "content", []) or []:
        if not getattr(b, "is_error", False):
            continue
        txt = _result_text(b)
        head = txt.strip().splitlines()[0][:200] if txt.strip() else ""
        if head and head not in _ERRORS:
            _ERRORS.append(head)


async def _drain_once(client: ClaudeSDKClient, verbose: bool) -> dict:
    """Consume one builder response; report HOW it ended.

    The SDK signals a truncated loop with subtype='error_max_turns' on the final
    ResultMessage. Discarding it (as this used to) means a build that was cut off
    mid-scene is indistinguishable from one that finished — BR layer G was critiqued,
    scored and recorded 'failed' while half-built. Always look at the subtype.
    """
    info = {"subtype": "unknown", "turns": 0, "cost": 0.0, "session_id": None, "tokens": {}}
    async for message in client.receive_response():
        if verbose:
            log_message(message)
        else:
            # Quiet means no console noise, not no durable evidence.  Previously every
            # non-result message vanished from quiet transcripts, including the SDK's
            # compact_boundary notification.
            transcript.message(message)
            if isinstance(message, ResultMessage):
                # Accounting is correctness data, not console decoration. Quiet runs
                # still record their sessions even though they skip the pretty-printer.
                costlog.record(message)
        if type(message).__name__ == "SystemMessage" and getattr(message, "subtype", None) == "compact_boundary":
            bump("compaction_completed")
            if not verbose:
                log("↻ context compaction completed; continuing from the SDK summary")
        _collect_errors(message)
        _collect_approach(message)
        if isinstance(message, ResultMessage):
            # Token economics lived ONLY in the SDK's session JSONL, outside the repo, so
            # "which layer burned the budget, and was the stable prefix actually cached?"
            # could not be answered from anything the pipeline writes. A collapsing
            # cache-hit rate is the early warning that the cached prefix has been broken.
            u = getattr(message, "usage", None)
            info = {
                "session_id": getattr(message, "session_id", None),
                "subtype": getattr(message, "subtype", "unknown"),
                "turns": getattr(message, "num_turns", 0) or 0,
                # total_cost_usd is cumulative for the session and Optional on error paths
                "cost": getattr(message, "total_cost_usd", None) or 0.0,
                "tokens": {
                    k: (u.get(k) or 0)
                    for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
                }
                if isinstance(u, dict)
                else {},
            }
    return info


async def _drain(client: ClaudeSDKClient, verbose: bool, *, continues: int = MAX_CONTINUES) -> dict:
    """Drain a builder response, nudging it onward if it hit the turn cap.

    A streaming-input session SURVIVES error_max_turns — the builder is still there
    with its full context, and each new message starts a fresh turn budget. So an
    exhausted cap is a pause, not a death: tell it how much it has spent and let it
    finish. (Single-shot query() raises instead; that is why this only works here.)
    """
    info = await _drain_once(client, verbose)
    for i in range(continues):
        if info["subtype"] != "error_max_turns":
            break
        log(f"⏸ builder hit the turn cap ({info['turns']} turns, ${info['cost']:.2f}) — continuing {i + 1}/{continues}")
        await client.query(
            f"You have hit a turn checkpoint: {info['turns']} turns and "
            f"${info['cost']:.2f} spent on this layer so far, out of a ${MAX_BUDGET_USD:.0f} "
            f"budget. You have NOT been reset — the scene and your context are intact. "
            f"Continue from exactly where you stopped, but start converging: finish the "
            f"work in progress and prefer landing the layer over further refinement."
        )
        info = await _drain_once(client, verbose)
    if info["subtype"] == "error_max_turns":
        log(f"! builder still truncated after {continues} continuations ({info['turns']} turns, ${info['cost']:.2f})")
    try:
        usage = await client.get_context_usage()
        compact = {
            "total_tokens": usage.get("totalTokens"),
            "max_tokens": usage.get("maxTokens"),
            "raw_max_tokens": usage.get("rawMaxTokens"),
            "percentage": usage.get("percentage"),
            "auto_compact": usage.get("isAutoCompactEnabled"),
            "auto_compact_threshold": usage.get("autoCompactThreshold"),
            "memory_files": [
                {k: row.get(k) for k in ("path", "type", "tokens") if k in row}
                for row in (usage.get("memoryFiles") or [])
            ],
        }
        transcript.event("context_usage", **compact)
        if float(compact.get("percentage") or 0) >= 70:
            log(
                f"⚠ context {compact['percentage']:.1f}% full "
                f"({compact['total_tokens']}/{compact['max_tokens']} tokens)"
            )
    except Exception as exc:
        # Context telemetry is diagnostic and must never make a completed build fail.
        transcript.event("context_usage_unavailable", error=str(exc)[:160])
    return info


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


def _verdict(verdict: dict) -> dict:
    """Compute pass/mean from the critic's IN-SCOPE axis scores. A scaffolding stage
    marks axes a later stage delivers as "n/a" — absent-by-design must not drag the
    mean (judging layer L on emission scored it 1.25 while its own axis scored 4)."""
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
    # A verdict measured against the wrong plate is not a verdict. The critic reports
    # this itself; before it was asked directly it would note the mismatch in `issues`
    # and pass regardless. No score can rescue this — the plan's ref path is wrong.
    if verdict.get("reference_usable") is False:
        verdict["pass"] = False
        verdict["reference_unusable"] = True
        return verdict
    # Thresholds must be GRANULARITY-AWARE. mean = sum/n, so one axis point of judge
    # noise moves the mean by 1/n: 0.125 across 8 axes but 1.0 across one. A scoped
    # layer with 1-2 in-scope axes must not face a harsher bar than a full acceptance
    # layer (PASS_MEAN 3.1 on a single axis silently demands a 4).
    if n and n <= 2:
        verdict["pass"] = min(scores.values()) >= 3
    else:
        verdict["pass"] = bool(scores) and mean >= PASS_MEAN and min(scores.values()) >= PASS_MIN
    return verdict


def _focus_references(
    shot: Shot,
    m: Milestone,
    allowed_frames: list[int] | tuple[int, ...] | set[int] | None = None,
) -> dict[int, str]:
    """Reference-bearing frames that can produce an aligned optical focus panel.

    Live layer review may inspect any judged frame.  A canonical *per-frame* review is
    different: its verdict is attributed to one frame and used by the transactional
    repair guard for that frame.  In that mode callers restrict this map so a defect at
    f120 cannot silently turn the recorded f40 verdict into a failure.
    """
    references = {int(m.frame): str(m.ref)}
    layer_id = str(m.id).split("@", 1)[0]
    try:
        layer = load_layers(shot).get(layer_id)
    except (FileNotFoundError, ValueError, json.JSONDecodeError):
        layer = None
    if layer is not None:
        references.update({int(frame): str(ref) for frame, ref in layer.judges})
    allowed = {int(frame) for frame in allowed_frames} if allowed_frames is not None else None
    return {
        frame: ref
        for frame, ref in references.items()
        if 1 <= frame <= shot.frames and (shot.folder / ref).is_file()
        if allowed is None or frame in allowed
    }


def _motion_strip_crop(crop: list[float], frames: list[int], source_frame: int) -> list[float]:
    """Map one strip-global crop into a single frame-local crop.

    A request crossing a panel seam is ambiguous by construction: there is no single
    Blender frame/reference pair that can be optically rerendered for it.
    """
    if not frames:
        raise ValueError("motion-strip focus requires strip frames")
    x0, y0, x1, y1 = crop
    count = len(frames)
    first = min(count - 1, int(x0 * count))
    last = min(count - 1, int(max(x0, x1 - 1e-9) * count))
    if first != last:
        raise ValueError("motion-strip focus crop crosses a panel boundary")
    if int(frames[first]) != int(source_frame):
        raise ValueError(f"motion-strip crop selects f{frames[first]}, not declared f{source_frame}")
    return [round(x0 * count - first, 6), y0, round(x1 * count - first, 6), y1]


def _focus_requests(
    verdict: dict,
    axes: list[tuple[str, str]],
    *,
    focus_references: dict[int, str],
    motion_frames: list[int] | None = None,
) -> list[dict]:
    """Validate critic-selected crops before they can trigger renders or extra judging.

    A request is supplemental evidence, not an escape hatch from scoring the full frame:
    it must name an in-scope axis that is actually borderline/failing, be a genuine zoom
    rather than nearly the whole image, and use the public top-left coordinate convention.
    """
    allowed = {key for key, _desc in axes}
    scores = verdict.get("scores") or {}
    out, seen = [], set()
    for index, item in enumerate(verdict.get("focus_requests") or []):
        if not isinstance(item, dict) or len(out) >= 2:
            continue
        axis = str(item.get("axis") or "")
        score = scores.get(axis)
        if axis not in allowed or not isinstance(score, (int, float)) or score > 3:
            continue
        source = str(item.get("source") or "")
        try:
            source_frame = int(item.get("source_frame"))
        except (TypeError, ValueError):
            continue
        if source not in {"candidate_frame", "motion_strip"} or source_frame not in focus_references:
            continue
        try:
            requested_crop = list(validate_crop(item.get("region")))
            crop = (
                _motion_strip_crop(requested_crop, list(motion_frames or []), source_frame)
                if source == "motion_strip"
                else requested_crop
            )
            crop = list(validate_crop(crop))
        except ValueError:
            continue
        if crop[2] - crop[0] < 0.02 or crop[3] - crop[1] < 0.02:
            continue  # below this, even the capped optical render has too few real pixels
        max_span = 0.9 if source == "motion_strip" else 0.75
        if crop[2] - crop[0] > max_span or crop[3] - crop[1] > 0.75:
            continue
        raw_id = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(item.get("id") or "")).strip("_")
        panel_id = (raw_id or f"focus_{index + 1}")[:40]
        if panel_id in seen:
            panel_id = f"{panel_id}_{index + 1}"
        seen.add(panel_id)
        reason = " ".join(str(item.get("reason") or "").split())[:180]
        if not reason:
            continue
        out.append(
            {
                "id": panel_id,
                "axis": axis,
                "source": source,
                "source_frame": source_frame,
                "requested_crop": requested_crop,
                "crop": crop,
                "reference": focus_references[source_frame],
                "reason": reason,
            }
        )
    return out


async def _make_focus_panels(
    shot: Shot,
    m: Milestone,
    session: BlenderSession,
    requests: list[dict],
    candidate_rel: str = "candidate",
) -> list[dict]:
    """Optically rerender frame-local crops against that frame's matching reference."""
    panels = []
    rendered_other_frame = False
    try:
        async with _FOCUS_RENDER_LOCK:
            for item in requests[:2]:
                crop = item["crop"]
                source_frame = int(item["source_frame"])
                rendered_other_frame = rendered_other_frame or source_frame != int(m.frame)
                reference = shot.folder / item["reference"]
                fraction = max(crop[2] - crop[0], crop[3] - crop[1])
                res_pct = min(800, max(150, round(110 / fraction)))
                rendered = await anyio.to_thread.run_sync(
                    lambda c=crop, pct=res_pct, f=source_frame: session.render_full(
                        frame=f,
                        mode="eevee",
                        scale=0.5,
                        **{"pass": "beauty"},
                        shade="beauty",
                        crop=c,
                        res_pct=pct,
                    )
                )
                candidate_tag = re.sub(r"[^a-zA-Z0-9_-]+", "_", Path(candidate_rel).stem)[:60]
                dest = (run_artifacts.renders_dir(shot.folder)
                        / f"{candidate_tag}_focus_{item['id']}_f{source_frame}.jpg")
                dest.parent.mkdir(parents=True, exist_ok=True)
                meta = await anyio.to_thread.run_sync(
                    lambda r=rendered, c=crop, d=dest, ref=reference: save_focus_sheet(
                        r["image_path"], ref, c, d, ("side_by_side", "wipe")
                    )
                )
                if not meta["has_signal"]:
                    dest.unlink(missing_ok=True)
                    raise ValueError(f"focus {item['id']} is empty in both candidate and reference at f{source_frame}")
                panels.append(
                    {
                        **item,
                        "res_pct": res_pct,
                        "image_rel": str(dest.relative_to(shot.folder)),
                        "candidate_source_px": meta["candidate_source_px"],
                        "reference_crop_px": meta["reference_crop_px"],
                        "comparison_px": meta["comparison_px"],
                        "views": meta["views"],
                        "upscaled": meta["upscaled"],
                        "mean_abs_diff": meta["mean_abs_diff"],
                        "signal": meta["signal"],
                    }
                )
    finally:
        if rendered_other_frame:
            await anyio.to_thread.run_sync(
                lambda: session.run(f"bpy.context.scene.frame_set({int(m.frame)})", journal=False)
            )
    return panels


def _required_focus_requests(shot: Shot, layer_id: str, frame: int, axes: list[tuple[str, str]]) -> list[dict]:
    """Load planner-declared optical evidence that must reach the first judge.

    Whole-frame vision cannot reliably grade a feature occupying a few encoder patches.
    Making the crop a contract property turns zooming from a critic-dependent recovery
    path into deterministic evidence acquisition for every shot.
    """
    from vfx_harness.domain.contracts import active_for, load_document
    from vfx_harness.orchestration.plan_authority import selected_artifact_path

    path = selected_artifact_path(shot.folder, "checks.json")
    if not path.is_file():
        return []
    try:
        reference_by_frame = {
            int(judge_frame): str(ref) for judge_frame, ref in load_layers(shot)[str(layer_id)].judges
        }
    except (KeyError, FileNotFoundError, ValueError, json.JSONDecodeError):
        reference_by_frame = {}
    owned_axes = {str(key) for key, _description in axes}
    requests = []
    for row in load_document(path, "checks"):
        focus = row.get("focus")
        if not isinstance(focus, dict) or not focus.get("required"):
            continue
        if not active_for(row, layer_id, frame):
            continue
        axis = str(row.get("axis") or "")
        if axis not in owned_axes:
            continue
        reference = reference_by_frame.get(int(frame))
        if not reference or not (shot.folder / reference).is_file():
            raise ValueError(f"check {row.get('id')} requires focus at f{frame}, but that frame has no layer reference")
        try:
            crop = list(validate_crop(focus.get("crop")))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"check {row.get('id')} has an invalid required focus crop") from exc
        reason = " ".join(str(focus.get("reason") or row.get("note") or "").split())
        if not reason:
            raise ValueError(f"check {row.get('id')} required focus needs a reason")
        requests.append(
            {
                "id": str(focus.get("id") or row.get("id"))[:40],
                "axis": axis,
                "source": "candidate_frame",
                "source_frame": int(frame),
                "requested_crop": crop,
                "crop": crop,
                "reference": reference,
                "reason": reason[:180],
            }
        )
    if len(requests) > 2:
        raise ValueError("at most two required focus contracts may target one judge frame")
    return requests


def _claim_context(
    shot: Shot, m: Milestone, *, enabled: bool
) -> tuple[list[dict], dict[str, frozenset[str]], set[str]]:
    """Return only claims that are active at this exact judge moment.

    The critic sees a read-only manifest.  It may point at a claim, but only the harness
    resolves that claim to evidence and decides whether repair is authorized.
    """
    if not enabled:
        return [], {}, set()
    milestone_parts = str(m.id).split("@", 1)
    layer_id = milestone_parts[0]
    unit_id = milestone_parts[1] if len(milestone_parts) == 2 and not milestone_parts[1].startswith("f") else None
    try:
        layer = load_layers(shot)[layer_id]
    except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError):
        return [], {}, set()
    claims, bindings, qualified = [], {}, set()
    for unit in layer.stages:
        if unit_id is not None and unit.id != unit_id:
            continue
        for claim in unit.evaluation.claims:
            if int(m.frame) not in claim.moments:
                continue
            claims.append(
                {
                    "id": claim.id,
                    "proposition": claim.proposition,
                    "axis": claim.axis,
                    "property": claim.property,
                    "roles": list(claim.subject_roles),
                    "controls": list(claim.subject_controls),
                    "authority": claim.authority,
                    "evidence_ids": list(claim.binding_ids),
                }
            )
            bindings[claim.id] = frozenset(claim.binding_ids)
            if claim.authority == "qualified_qualitative_required":
                qualified.add(claim.id)
    return claims, bindings, qualified


def _filter_critic_issues(
    verdict: dict,
    evidence: list[dict] | None,
    *,
    claim_bindings: dict[str, frozenset[str]] | None = None,
    qualified_claims: set[str] | None = None,
) -> dict:
    """Reconcile typed critic observations against exact claim/evidence bindings.

    A passing check is a contradiction only for the property that explicitly owns it.
    An uncovered measurable defect is a plan defect (``contract_gap``), never an
    instruction for the builder to make an unplanned scene mutation.
    """
    parsed, parse_errors = [], []
    for index, raw in enumerate(verdict.get("observations") or []):
        try:
            parsed.append(Observation.parse(raw, f"observations[{index}]"))
        except ValueError as exc:
            parse_errors.append({"state": "protocol_error", "reason": str(exc), "observation": raw})
    if not verdict.get("pass") and not parsed and not parse_errors:
        parse_errors.append(
            {
                "state": "protocol_error",
                "reason": "a failing scorecard must include at least one typed observation",
                "observation": {},
            }
        )
    if verdict.get("pass") and parsed:
        parse_errors.append(
            {
                "state": "protocol_error",
                "reason": "a passing scorecard must not include blocking observations",
                "observation": {},
            }
        )
    reconciled = reconcile_observations(
        parsed,
        evidence or [],
        claim_bindings=claim_bindings,
        qualified_claims=qualified_claims or set(),
    )
    rows = reconciled["observations"] + parse_errors
    by_state = {
        state: [row for row in rows if row.get("state") == state]
        for state in ("actionable", "contradicted", "contract_gap", "unverified_qualitative", "protocol_error")
    }
    verdict["observation_reconciliation"] = rows
    verdict["issues"] = [
        str(row["observation"]["action"])
        for row in by_state["actionable"]
    ]
    verdict["contradicted_issues"] = [
        {
            "issue": row["observation"]["observation"],
            "check_ids": row.get("check_ids", []),
            "reason": row["reason"],
        }
        for row in by_state["contradicted"]
    ]
    verdict["contract_gaps"] = by_state["contract_gap"]
    verdict["unverified_observations"] = by_state["unverified_qualitative"]
    verdict["protocol_errors"] = by_state["protocol_error"]
    verdict["contract_gap"] = bool(by_state["contract_gap"])
    verdict["needs_human"] = bool(by_state["unverified_qualitative"])
    if not verdict.get("pass") and not verdict["issues"]:
        verdict["judge_conflict"] = bool(by_state["contradicted"] or by_state["protocol_error"])
    return verdict


def _audit_panel_citations(verdict: dict, focus_panels: list[dict] | None) -> dict:
    """Keep focus provenance honest; a model cannot cite a panel it was not shown."""
    valid = {str(panel.get("id")) for panel in (focus_panels or []) if panel.get("id")}
    invalid = []
    for index, meta in enumerate(verdict.get("observations") or []):
        if not isinstance(meta, dict):
            continue
        cited = [str(panel_id) for panel_id in (meta.get("panel_ids") or [])]
        bad = [panel_id for panel_id in cited if panel_id not in valid]
        if bad:
            invalid.append({"observation_index": index, "observation_id": meta.get("id"), "panel_ids": bad})
        meta["panel_ids"] = [panel_id for panel_id in cited if panel_id in valid]
    verdict["invalid_panel_citations"] = invalid
    return verdict


def _apply_evidence_gate(verdict: dict, evidence: list[dict] | None) -> dict:
    """Let proven planner contracts overrule a critic pass.

    Evidence is symmetric: a passing check prevents an invented measurable repair, and a
    failing authoritative check prevents a flattering visual score from shipping it.
    Builder-authored checks are shown to the critic but remain advisory because the author
    chose both the measurement and its band.
    """
    failures = [item for item in (evidence or []) if item.get("authoritative") and not item.get("pass")]
    verdict["evidence_failures"] = failures
    if not failures:
        return verdict
    issues = list(verdict.get("issues") or [])
    for item in failures:
        tag = f"[check:{item['id']}]"
        if any(str(issue).lstrip().startswith(tag) for issue in issues):
            continue
        detail = ""
        if item.get("open_items"):
            detail = "; unresolved: " + " | ".join(str(value) for value in item["open_items"][:3])
        issues.append(
            f"{tag} executable contract fails: {item.get('metric')} reads "
            f"{item.get('value')} against {item.get('target')}; correct the property "
            f"measured by this check{detail}"
        )
    verdict["issues"] = issues[:6]
    verdict["pass"] = False
    verdict["judge_conflict"] = False
    verdict["decided_by"] = "checks"
    return verdict


def _repair_delta(pre: list, post: list) -> dict:
    """What a canonical repair round actually achieved, per judged frame.

    Two questions, and the loop used to get the second one wrong.

    `broke` — frames that PASSED before the repair and do not now. A repair that trades
    a passing frame for a failing one is not a fix, and the caller reverts on it.

    `progressed` — whether the round moved toward a pass at all. This used to compare the
    SUM of the failing frames' means, which is the wrong statistic for a conjunctive
    requirement: the layer passes only when EVERY judged frame clears the bar, so the
    binding constraint is the worst frame, while a sum rises whenever any one frame does.
    A four-frame layer going 1.0→2.0 on one frame and holding the rest scored as
    improvement and bought another round that could not lead to a pass. Layer 2 spent five
    attempts in that state — the sum crept up while the minimum sat still. Progress means
    the worst failing frame improved, or there are fewer failing frames than before.
    """
    was = {f: v.get("mean") for (f, _r), v in pre}
    now = {f: v.get("mean") for (f, _r), v in post}
    was_pass = {f for (f, _r), v in pre if v.get("pass")}
    failed = [f for (f, _r), v in pre if not v.get("pass")]
    still = [f for (f, _r), v in post if not v.get("pass")]
    # Only frames judged BOTH times can be compared. A frame missing from `post` has no
    # score to improve on, and treating its absence as 0 would read as a regression.
    common = [f for f in failed if f in now]
    was_worst = min((was.get(f) or 0) for f in common) if common else None
    now_worst = min((now.get(f) or 0) for f in common) if common else None

    def contract_failures(rows: list) -> int:
        return sum(
            1
            for (_frame_ref, verdict) in rows
            for item in (verdict.get("evidence") or [])
            if item.get("authoritative") and not item.get("pass")
        )

    was_contract = contract_failures(pre)
    now_contract = contract_failures(post)
    progressed = (
        (was_worst is not None and now_worst > was_worst)
        or len(still) < len(failed)
        or (was_contract > 0 and now_contract < was_contract)
    )
    return {
        "was": was,
        "now": now,
        "broke": sorted(f for (f, _r), v in post if f in was_pass and not v.get("pass")),
        "was_worst": was_worst,
        "now_worst": now_worst,
        "was_failing": len(failed),
        "now_failing": len(still),
        "was_contract_failures": was_contract,
        "now_contract_failures": now_contract,
        "progressed": progressed,
    }


def _repair_action(delta: dict, attempt: int, max_attempts: int | None = None) -> str:
    """Choose the transactional disposition of one canonical repair.

    A rejected patch is rolled back, but rejection is evidence about an approach—not a
    reason to throw away an unused repair attempt.  The old loop stopped immediately on
    regression, which made ``MAX_CANON_REPAIRS = 2`` misleading: Layer 4 used one
    geometric approach, regressed a protected frame, and was denied its second attempt.
    """
    max_attempts = MAX_CANON_REPAIRS if max_attempts is None else max_attempts
    rejected = bool(delta.get("broke")) or not bool(delta.get("progressed"))
    if not rejected:
        return "accept"
    return "rollback_retry" if attempt < max_attempts else "rollback_stop"


def _repair_change_summary(before: str, after: str, limit: int = 3200) -> str:
    """Small, prompt-safe account of a rejected script edit for the next repair agent."""
    changed = [
        line
        for line in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="")
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    ]
    text = "\n".join(changed)
    return text[:limit] + ("\n…" if len(text) > limit else "")


# Claude's long-edge sweet spot. Beyond this an image costs tokens without adding
# discriminable detail, and the critic scores several images per call.
CRITIC_EFFORT = os.environ.get("VFXH_CRITIC_EFFORT", "xhigh")

_CRITIC_MAX_PX = 1568


def _image_block(path: Path, max_px: int = _CRITIC_MAX_PX) -> dict:
    """A base64 JPEG content block, downscaled to the useful maximum.

    Verified against the live API before adopting: three images attached with positional
    labels came back mapped correctly (reference/candidate/motion strip), and a control
    request with no image attached correctly reported that it had none.
    """
    from PIL import Image

    im = Image.open(path).convert("RGB")
    if max(im.size) > max_px:
        im.thumbnail((max_px, max_px), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(buf.getvalue()).decode()},
    }


async def _one_user_message(blocks: list[dict]):
    """Stream exactly one multimodal user message; the SDK ends input when we return."""
    yield {"type": "user", "session_id": "", "message": {"role": "user", "content": blocks}, "parent_tool_use_id": None}


def _structured_or_text(message, acc: dict) -> None:
    """Collect the verdict however the SDK delivers it.

    With output_format=json_schema the answer arrives as a StructuredOutput TOOL USE
    block, NOT as text — a request that scored three images perfectly returned no
    TextBlock at all. Reading only text made the verdict depend on the model ALSO
    volunteering prose JSON, which it is under no obligation to do.
    """
    if not isinstance(message, AssistantMessage):
        return
    for block in message.content:
        if isinstance(block, ToolUseBlock) and block.name == "StructuredOutput":
            if isinstance(block.input, dict):
                acc["structured"] = block.input
        elif isinstance(block, TextBlock):
            acc["text"] = acc.get("text", "") + block.text


async def _critique(
    shot: Shot,
    m: Milestone,
    candidate_rel: str,
    axes: list[tuple[str, str]],
    session: BlenderSession,
    verbose: bool,
    scope: str | None = None,
    prior_rel: str | None = None,
    prior_mean: float | None = None,
    evidence: list[dict] | None = None,
    review_mode: str = "observer",
    focus_panels: list[dict] | None = None,
    motion_evidence: tuple[str, list[int]] | None = None,
    allow_motion: bool | None = None,
    focus_frames: list[int] | tuple[int, ...] | set[int] | None = None,
) -> dict:
    motion_rel, motion_frames = motion_evidence or (None, None)
    wants_motion = _axes_need_motion(axes) if allow_motion is None else allow_motion
    if motion_rel is None and shot.frontmatter.get("type") == "motion" and shot.frames > 1 and wants_motion:
        try:  # a motion strip so motion/finish axes are judged across frames, not a still
            stem = candidate_rel.split("/")[-1].split(".")[0]
            motion_rel, motion_frames = _stash_motion_strip(session, shot, m, stem)
        except Exception as e:
            log(f"motion strip skipped: {str(e)[:80]}", 1)
    focus_references = _focus_references(shot, m, focus_frames)
    claim_manifest, claim_bindings, qualified_claims = _claim_context(
        shot, m, enabled=scope is not None
    )
    log(
        f"critic[{critic_model()}]: scoring {candidate_rel} vs {m.ref}"
        + (f" (+motion {motion_frames})" if motion_rel else ""),
        1,
    )
    prompt = critic_prompt(
        shot,
        m,
        candidate_rel,
        axes,
        motion_rel,
        motion_frames,
        scope,
        evidence=evidence,
        claims=claim_manifest,
        review_mode=review_mode,
        focus_panels=focus_panels,
        focus_frames=sorted(focus_references),
    )

    # ATTACH the images instead of asking an agent to fetch them. A missing file is now a
    # loud failure here rather than a confident score on a frame that was never seen.
    ref_abs, cand_abs = shot.folder / m.ref, shot.folder / candidate_rel
    for p, what in ((ref_abs, "reference"), (cand_abs, "candidate render")):
        if not p.is_file():
            raise BlenderError(f"critic cannot score {m.id}: {what} missing at {p}")
    blocks = [
        {"type": "text", "text": prompt},
        {"type": "text", "text": "FIRST — the REFERENCE:"},
        _image_block(ref_abs),
        {"type": "text", "text": "SECOND — the CANDIDATE render:"},
        _image_block(cand_abs),
    ]
    for panel in (focus_panels or [])[:2]:
        panel_abs = shot.folder / panel["image_rel"]
        if not panel_abs.is_file():
            raise BlenderError(f"critic focus panel missing at {panel_abs}")
        blocks += [
            {
                "type": "text",
                "text": (
                    f"FOCUS PANEL {panel['id']} — axis {panel['axis']}, crop "
                    f"{panel['crop']} within source frame f{panel['source_frame']} "
                    f"against {panel['reference']} (TOP-LEFT normalized), optical res_pct "
                    f"{panel['res_pct']}. It contains aligned CANDIDATE | REFERENCE "
                    f"and a 50/50 wipe. Reason: {panel['reason']}"
                ),
            },
            _image_block(panel_abs),
        ]
    if motion_rel and (shot.folder / motion_rel).is_file():
        blocks += [
            {"type": "text", "text": f"THIRD — the MOTION STRIP, frames {motion_frames}:"},
            _image_block(shot.folder / motion_rel),
        ]
    # The previous best, so the critic can judge DIRECTION of travel and not only
    # absolute state. Explicitly framed as context: it must score the candidate.
    if prior_rel and (shot.folder / prior_rel).is_file():
        blocks += [
            {
                "type": "text",
                "text": f"CONTEXT ONLY — the best PREVIOUS attempt at this frame, "
                f"which scored {prior_mean}. Do NOT score this image. Use it "
                f"to say whether the candidate improved or regressed, and "
                f"record as a typed observation anything the previous attempt got right "
                f"that the candidate has lost:",
            },
            _image_block(shot.folder / prior_rel),
        ]

    # Still retried: the critic is a transient-failure choke point — an SDK hiccup here
    # once killed a layer AFTER it had passed at 4.0 and written its script. Scoring is
    # idempotent. What is gone is retrying because the critic never opened its images.
    acc: dict = {}
    for attempt in range(1, 4):
        acc = {}
        try:
            critic_role = "focus_critic" if review_mode == "focus_review" else "critic"
            with costlog.scoped(
                role=critic_role,
                phase=review_mode,
                frame=getattr(m, "frame", None),
                model=critic_model(),
            ):
                async for message in query(
                    prompt=_one_user_message(blocks),
                    options=_critic_options(
                        shot,
                        axes,
                        allow_na=scope is None,
                        focus_frames=sorted(focus_references),
                    ),
                ):
                    _structured_or_text(message, acc)
                    # The critic loop does NOT call log_message, which is where costlog was
                    # hooked — so record at the source while the scoped role is active.
                    costlog.record(message)
            if acc.get("structured") or acc.get("text", "").strip():
                break
            log(f"critic returned nothing (attempt {attempt}/3) — retrying", 1)
        except Exception as e:
            if attempt == 3:
                raise
            log(f"critic error (attempt {attempt}/3): {str(e)[:90]} — retrying", 1)
    if not (acc.get("structured") or acc.get("text", "").strip()):
        raise BlenderError(f"critic returned no verdict for {m.id} after 3 attempts")
    verdict = _verdict(acc.get("structured") or _extract_json(acc["text"]))
    verdict = _audit_panel_citations(verdict, focus_panels)
    verdict = _filter_critic_issues(
        verdict,
        evidence,
        claim_bindings=claim_bindings if scope is not None else None,
        qualified_claims=qualified_claims,
    )
    verdict = _apply_evidence_gate(verdict, evidence)
    verdict["evidence"] = evidence or []
    # A PASS followed by six urgent "fix" bullets is internally inconsistent and was a
    # major source of misleading run logs. Preserve such notes as non-blocking polish for
    # audit, but never route them into a repair path or present them as contractual defects.
    if verdict.get("pass") and verdict.get("issues"):
        verdict["polish"] = list(verdict["issues"])
        verdict["issues"] = []
    if verdict.get("reference_unusable"):
        log(
            f"✗ critic says the REFERENCE is unusable for {m.id}: "
            f"{verdict.get('reference_note', '(no note)')} — fix {m.ref} in the plan; "
            f"no score is meaningful against it",
            1,
        )
    scores = ", ".join(f"{k}={v}" for k, v in verdict.get("scores", {}).items())
    na = verdict.get("na_axes") or []
    log(
        f"critic: {scores} | mean {verdict['mean']} (over {len(verdict.get('scored_axes', []))} "
        f"in-scope axes{f'; n/a: {len(na)}' if na else ''}) | "
        f"{'PASS ✅' if verdict['pass'] else 'REVISE ✎'}",
        1,
    )
    for issue in verdict.get("issues", [])[:6]:
        log(f"· fix: {issue}", 2)
    for item in verdict.get("contradicted_issues", [])[:6]:
        log(f"· discarded measurable claim: {item['issue']} ({item['reason']})", 2)
    for item in verdict.get("contract_gaps", [])[:6]:
        observation = item.get("observation", {})
        log(
            f"· contract gap: {observation.get('property')}: "
            f"{observation.get('observation')} ({item.get('reason')})",
            2,
        )
    for item in verdict.get("unverified_observations", [])[:6]:
        observation = item.get("observation", {})
        log(f"· unverified qualitative observation: {observation.get('observation')}", 2)
    if verdict.get("judge_conflict"):
        log("⚠ critic score has no evidence-backed blocking issue — judge conflict", 1)
    # The judge's answer is what every control decision downstream hangs on, and it was
    # the one output with no durable home: `_critique` drains its own stream and never
    # calls log_message, so nothing but this console line recorded WHICH image scored
    # what against which reference. Recorded with the inputs beside it, because a score
    # without its render/reference pair cannot be re-checked.
    transcript.event(
        "critic",
        milestone=m.id,
        frame=m.frame,
        candidate=candidate_rel,
        ref=m.ref,
        model=critic_model(),
        mean=verdict.get("mean"),
        verdict="pass" if verdict.get("pass") else "revise",
        decided_by=verdict.get("decided_by", "critic"),
        scores=verdict.get("scores", {}),
        scored_axes=verdict.get("scored_axes", []),
        na_axes=na,
        borderline=_borderline(verdict),
        reference_unusable=bool(verdict.get("reference_unusable")),
        issues=verdict.get("issues", []),
        contradicted_issues=verdict.get("contradicted_issues", []),
        observations=verdict.get("observations", []),
        observation_reconciliation=verdict.get("observation_reconciliation", []),
        contract_gaps=verdict.get("contract_gaps", []),
        contract_gap=bool(verdict.get("contract_gap")),
        unverified_observations=verdict.get("unverified_observations", []),
        protocol_errors=verdict.get("protocol_errors", []),
        invalid_panel_citations=verdict.get("invalid_panel_citations", []),
        judge_conflict=bool(verdict.get("judge_conflict")),
        evidence=evidence or [],
        focus_requests=verdict.get("focus_requests", []),
        focus_panels=[{k: v for k, v in panel.items() if k != "image_abs"} for panel in (focus_panels or [])],
        scope="layer" if scope else "full-rubric",
        review_mode=review_mode,
        motion_strip=motion_rel,
    )
    return verdict


# How close to the pass line counts as "noise could flip this".
#
# MEASURED (N=12, same render vs same reference, layer-1 scope, one axis):
#   scores [2,4,3,2,3,3,3,3,3,3,4,3] → median 3.0, spread 2.0, sd 0.603,
#   and 2 of 12 draws flipped the verdict — a 17% flip rate on an unchanged image.
#
# A FIXED band was the wrong shape. Noise in the MEAN falls as 1/sqrt(n), so one constant
# is simultaneously too narrow on a 3-axis layer and wasteful on an 8-axis one. Two
# sigma of the mean at this sd: n=3 → 0.70, n=4 → 0.60, n=6 → 0.49, n=8 → 0.43. The old
# flat 0.4 was only defensible at n≈8, and most layers here are narrower than that.
#
# STALE AS OF THE SWITCH TO OPUS-5. Every number above was measured on FABLE-5. Judge
# noise is a property of the judge, so both the sd and the flip rate belong to a model
# that is no longer scoring anything here. The band may now be too wide (paying for
# panels that were never in doubt) or too narrow (passing verdicts that a second opinion
# would have flipped) — and which of those it is, is not currently known.
# Re-measure before trusting the adjudication economics: python -m vfx_harness.evaluation.cli variance <shot>
_JUDGE_SD = 0.603  # fable-5 measurement; see above

# How many times a canonical failure may be handed back before we stop paying for it.
# Two, plus a no-improvement break: a repair that moved nothing will not move anything
# next time, and the money is better spent on a human reading the critique.
MAX_CANON_REPAIRS = 2


def _adjudicate_band(n_scored: int) -> float:
    """2σ of the mean for this many axes, clamped to a sane range."""
    if n_scored <= 0:
        return 0.4
    return min(0.8, max(0.4, 2 * _JUDGE_SD / (n_scored**0.5)))


def _borderline(verdict: dict) -> bool:
    """Could judge noise flip this verdict?"""
    scores = [v for v in verdict.get("scores", {}).values() if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if not scores:
        return False
    if len(scores) <= 2:
        # Only scores adjacent to the 2/3 decision boundary can flip the verdict with one
        # point of ordinary judge noise.  Treating a perfect 4/5 on one owned axis as
        # "borderline" doubled every canonical critic call without changing a decision.
        return any(score in (2, 3) for score in scores)
    return abs(verdict.get("mean", 0.0) - PASS_MEAN) <= _adjudicate_band(len(scores)) or min(scores) == PASS_MIN


def _needs_critic_panel(verdict: dict) -> bool:
    """Whether another subjective opinion could change the decision.

    An authoritative executable check is deterministic for this scene state.  Paying a
    second critic to look at the same pixels cannot turn that failed contract into a pass;
    it only duplicates cost and creates another transient-failure point.
    """
    return (
        not verdict.get("reference_unusable")
        and verdict.get("decided_by") != "checks"
        and not verdict.get("contract_gap")
        and not verdict.get("needs_human")
        and not verdict.get("protocol_errors")
        and _borderline(verdict)
    )


def _round_rank(verdict: dict | None) -> tuple[bool, float]:
    """Rank a round by validity first, aesthetic score second.

    The evidence gate can force REVISE while retaining the critic's visual mean.  A
    contract-failing 4.0 must never beat a contract-passing 4.0 merely because it was
    encountered first.
    """
    verdict = verdict or {}
    return bool(verdict.get("pass")), float(verdict.get("mean", -1.0))


async def _judge(
    shot: Shot,
    m: Milestone,
    candidate_rel: str,
    axes: list[tuple[str, str]],
    session: BlenderSession,
    verbose: bool,
    scope: str | None = None,
    **kw,
) -> dict:
    """Score the frame, buying extra opinions ONLY where the decision is uncertain.

    One critic call decided every layer until now. That is fine when a verdict is far
    from the line and indefensible when it is near it: the identical render/reference
    pair scored 4.0, 3.0, 3.0, 2.0 on repeats, so a single 3 was deciding whether the
    whole chain proceeded. Here a borderline verdict goes to best-of-three on the
    pass/fail question, which is where the noise actually hurts.
    """
    critic_kw = dict(kw)
    critic_kw.pop("review_mode", None)
    critic_kw.pop("focus_panels", None)
    motion_evidence = critic_kw.pop("motion_evidence", None)
    allow_motion = critic_kw.pop("allow_motion", None)
    motion_frames_override = critic_kw.pop("motion_frames_override", None)
    focus_frames_override = critic_kw.pop("focus_frames_override", None)
    if (
        motion_evidence is None
        and shot.frontmatter.get("type") == "motion"
        and shot.frames > 1
        and (_axes_need_motion(axes) if allow_motion is None else allow_motion)
    ):
        try:
            stem = candidate_rel.split("/")[-1].split(".")[0]
            motion_evidence = _stash_motion_strip(session, shot, m, stem, frames_override=motion_frames_override)
        except Exception as exc:
            log(f"motion strip skipped: {str(exc)[:80]}", 1)
    focus_references = _focus_references(shot, m, focus_frames_override)
    required = _required_focus_requests(shot, str(m.id).split("@", 1)[0], int(m.frame), axes)
    focus_panels = []
    if required:
        log(f"contract requires {len(required)} aligned focus panel(s) before judgment", 1)
        focus_panels = await _make_focus_panels(shot, m, session, required, candidate_rel=candidate_rel)
        transcript.event(
            "contract_focus",
            milestone=m.id,
            frame=m.frame,
            candidate=candidate_rel,
            reference=m.ref,
            requests=required,
            panels=focus_panels,
        )
    first = await _critique(
        shot,
        m,
        candidate_rel,
        axes,
        session,
        verbose,
        scope,
        review_mode="observer",
        focus_panels=focus_panels or None,
        motion_evidence=motion_evidence,
        allow_motion=allow_motion,
        focus_frames=focus_frames_override,
        **critic_kw,
    )
    requests = (
        _focus_requests(
            first,
            axes,
            focus_references=focus_references,
            motion_frames=(motion_evidence[1] if motion_evidence else None),
        )
        if not first.get("reference_unusable") and first.get("decided_by") != "checks"
        else []
    )
    if requests and not focus_panels:
        log(f"critic requested {len(requests)} aligned focus panel(s) — rendering optical crops before deciding", 1)
        try:
            focus_panels = await _make_focus_panels(shot, m, session, requests, candidate_rel=candidate_rel)
            transcript.event(
                "critic_focus",
                milestone=m.id,
                frame=m.frame,
                candidate=candidate_rel,
                reference=m.ref,
                requests=requests,
                panels=focus_panels,
            )
            focused = await _critique(
                shot,
                m,
                candidate_rel,
                axes,
                session,
                verbose,
                scope,
                review_mode="focus_review",
                focus_panels=focus_panels,
                motion_evidence=motion_evidence,
                allow_motion=allow_motion,
                focus_frames=focus_frames_override,
                **critic_kw,
            )
            focused["focus_requested"] = requests
            focused["focus_panels"] = focus_panels
            first = focused
        except Exception as exc:
            first["focus_error"] = str(exc)[:200]
            log(f"! focus panel review unavailable: {str(exc)[:120]} — retaining the full-frame verdict", 1)
    if not _needs_critic_panel(first):
        return first
    log(
        f"borderline verdict (mean {first['mean']}, "
        f"{len(first.get('scored_axes', []))} axis/axes) — seeking a second opinion",
        1,
    )
    panel = [first]
    for _extra in range(2, 4):
        mode = "evidence_audit" if _extra == 2 else "tie_breaker"
        v = await _critique(
            shot,
            m,
            candidate_rel,
            axes,
            session,
            verbose,
            scope,
            review_mode=mode,
            focus_panels=focus_panels or None,
            motion_evidence=motion_evidence,
            allow_motion=allow_motion,
            focus_frames=focus_frames_override,
            **critic_kw,
        )
        panel.append(v)
        votes = [p["pass"] for p in panel]
        if len(panel) == 2 and votes[0] == votes[1]:
            break  # unanimous; a third cannot change it
        if len(panel) == 3:
            break
    votes = [p["pass"] for p in panel]
    means = sorted(p["mean"] for p in panel)
    agreed = sum(votes) > len(votes) / 2
    out = dict(panel[0])
    out["pass"] = agreed
    out["mean"] = means[len(means) // 2]  # median resists the outlier
    out["panel"] = [{"mean": p["mean"], "pass": p["pass"]} for p in panel]
    # Take the issues from a judge that agrees with the panel, so the builder is not
    # handed fixes derived from the verdict that lost the vote.
    for p in panel:
        if p["pass"] == agreed:
            out["issues"], out["scores"] = p.get("issues", []), p.get("scores", {})
            out["contradicted_issues"] = p.get("contradicted_issues", [])
            out["contract_gaps"] = p.get("contract_gaps", [])
            out["contract_gap"] = bool(p.get("contract_gap"))
            out["unverified_observations"] = p.get("unverified_observations", [])
            out["protocol_errors"] = p.get("protocol_errors", [])
            break
    agreeing = [p for p in panel if p["pass"] == agreed]
    out["judge_conflict"] = bool(not agreed and agreeing and all(p.get("judge_conflict") for p in agreeing))
    log(
        f"panel of {len(panel)}: means {[p['mean'] for p in panel]} · "
        f"votes {['PASS' if v else 'REVISE' for v in votes]} → "
        f"{'PASS ✅' if agreed else 'REVISE ✎'} (median {out['mean']})",
        1,
    )
    return out


def _stash_render(session: BlenderSession, shot: Shot, m: Milestone, tag: str, scale: float = 0.5) -> str:
    """Render the judge frame (eevee) and copy it into the shot for the critic.
    Returns the path relative to the shot folder."""
    src = session.render(frame=m.frame, mode="eevee", scale=scale)
    dest_dir = run_artifacts.renders_dir(shot.folder)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{m.id}_{tag}.png"
    shutil.copyfile(src, dest)
    return dest.relative_to(shot.folder).as_posix()


def _image_reproduction(live: str | Path, canonical: str | Path) -> dict:
    """Judgment-free answer to "did the script reproduce the accepted pixels?".

    Reproduction is not a second aesthetic review.  The old implementation asked the
    critic again and called a score delta determinism; an unchanged image had already
    measured a two-point critic spread.  EEVEE can move a few antialiased edge values
    across clean replays, so this uses a deliberately tight near-equality band rather
    than requiring a byte-identical PNG container.
    """
    from PIL import Image, ImageChops, ImageStat

    a, b = Path(live), Path(canonical)
    result = {"live": str(a), "canonical": str(b), "match": False}
    if not a.is_file() or not b.is_file():
        result["reason"] = "missing image"
        return result
    with Image.open(a) as ia, Image.open(b) as ib:
        ia, ib = ia.convert("RGB"), ib.convert("RGB")
        result["size"] = [ia.width, ia.height]
        if ia.size != ib.size:
            result["reason"] = f"size mismatch {ia.size} vs {ib.size}"
            return result
        diff = ImageChops.difference(ia, ib)
        hist = diff.histogram()
        counts = [0] * 256
        for index, count in enumerate(hist):
            counts[index % 256] += count
        total = max(1, ia.width * ia.height * 3)
        cumulative = 0
        p99 = 255
        for value, count in enumerate(counts):
            cumulative += count
            if cumulative >= 0.99 * total:
                p99 = value
                break
        mae = sum(stat * n for stat, n in enumerate(counts)) / total
        changed = sum(counts[5:]) / total
        rms = sum(ImageStat.Stat(diff).rms) / 3
        result.update(mae=round(mae, 4), rms=round(rms, 4), p99=p99, changed_gt4=round(changed, 6))
        result["match"] = bool(mae <= 1.0 and p99 <= 3 and changed <= 0.005)
        if not result["match"]:
            result["reason"] = "pixel delta exceeds reproduction tolerance"
    return result


def _unit_evidence_ids(unit, frame: int) -> set[str] | None:
    """Exact evidence boundary for one work unit at one judge moment.

    Returning ``None`` preserves layer/composed evaluation. A unit returns a set even
    when empty so a malformed or missing binding cannot silently fall back to every
    contract in the parent layer.
    """
    if unit is None:
        return None
    return {
        binding.id
        for claim in unit.evaluation.claims
        if int(frame) in claim.moments
        for binding in claim.evidence
    }


def image_evidence_required_for(image_bindings, capabilities) -> bool:
    """Whether a unit must produce candidate-bound image evidence before sealing.

    Declared look ownership counts as much as an explicit image binding: geometry
    cannot certify how something reads, so run 20260823T154920Z closed a "reads as
    layered machined metal" claim with radial closure and sealed on scene contracts."""
    return bool(image_bindings) or bool(capabilities)


def _unit_completion_evidence_ids(unit) -> set[str] | None:
    """All evidence required before a bounded unit may stop mutating.

    Live iteration renders the primary judge for speed, but scene contracts are cheap and can
    probe every declared moment. Restricting the convergence guard to the primary frame lets a
    multi-moment unit seal before its other required claims have even been evaluated.
    """
    if unit is None:
        return None
    return {
        binding.id
        for claim in unit.evaluation.claims
        if claim.required
        for binding in claim.evidence
    }


def _scope_unit_evidence(evidence: list[dict], unit, frame: int) -> list[dict]:
    """Keep only evidence explicitly bound by the active unit's moment."""
    ids = _unit_evidence_ids(unit, frame)
    if ids is None:
        return evidence
    return [
        row
        for row in evidence
        if str(row.get("id")) in ids or row.get("source") == "builder_state"
    ]


def _render_evidence(
    shot: Shot,
    layer,
    m: Milestone,
    render_rel: str,
    session: BlenderSession,
    *,
    active_unit=None,
) -> list[dict]:
    if layer is None:
        return []
    evidence = []
    stage = (
        "post_grade"
        if any("grade" in str(axis).lower() for axis in (getattr(layer, "owns", ()) or ()))
        else "pre_grade"
    )
    try:
        from vfx_harness.evidence.checks import layer_evidence

        evidence.extend(
            layer_evidence(shot.folder, str(layer.id), frame=m.frame, ref=m.ref, render=render_rel, stage=stage)
        )
    except Exception as exc:
        log(f"! image evidence unavailable: {str(exc)[:90]}", 1)
    try:
        from vfx_harness.evidence.scene_checks import functional_evidence
        from vfx_harness.evidence.scene_checks import layer_evidence as scene_layer_evidence

        evidence.extend(scene_layer_evidence(shot.folder, str(layer.id), frame=m.frame, session=session))
        evidence.extend(functional_evidence(shot.folder, str(layer.id), session=session))
    except Exception as exc:
        log(f"! live-scene evidence unavailable: {str(exc)[:90]}", 1)
    evidence.extend(_worklist_evidence(shot.folder, str(layer.id)))
    return _scope_unit_evidence(evidence, active_unit, int(m.frame))


def _reproduction_hint(row: dict) -> str:
    """The exact local invocation that re-measures a failing contract row.

    Guidance that arrives attached to the failure it explains gets used; the same
    guidance delivered ambiently does not (`vfx inspect` flags diagnostic tools that
    were never called on every measured layer)."""
    kind = str(row.get("metric") or row.get("kind") or "")
    objects = [name for name in (row.get("objects") or []) if name]
    subject = objects[0] if objects else "<role object>"
    frame = row.get("frame") or (row.get("frames") or [None])[0]
    if kind.startswith("bbox_"):
        return f"check_scene(kind='bbox', object='{subject}', frame={frame})"
    if kind == "keyframe_schedule":
        return f"list_keyframes(object='{subject}')"
    if kind in {"curve_derivative_max", "onset_order", "radial_distance_trend", "transform_return_delta"}:
        return f"check_scene(kind='motion', object='{subject}', frames=[…judged window…])"
    if kind == "mesh_vertex_count":
        return f"check_scene(kind='mesh', object='{subject}')"
    if kind in {"path_clearance_min"}:
        return f"check_scene(kind='visibility', object='{subject}', frame={frame}) + run_bpy distance probe"
    return ""


def _executable_unit_verdict(
    unit,
    frame: int,
    axes: list[tuple[str, str]],
    evidence: list[dict],
    contract_frames: dict[str, int] | None = None,
) -> dict | None:
    """Let exact executable claims decide an atomic unit without a vision call."""
    if unit is None:
        return None
    required = [
        claim
        for claim in unit.evaluation.claims
        if claim.required and int(frame) in claim.moments
    ]
    if not required or any(claim.authority != "executable_required" for claim in required):
        return None
    # A frame-scoped contract produces its reading at ITS declared frame only. A claim
    # judging [72, 150] that binds vis-f72 AND vis-f150 was faulted at each frame for
    # the OTHER frame's row (run 20260825: detail_instancing 'required bound evidence
    # was not produced' at both frames with both rows green at their own). A binding is
    # due here unless it is scoped to a different frame this claim also judges; a
    # binding scoped to a frame NO claim moment covers stays due — loudly missing beats
    # silently never-checked.
    binding_moments: dict[str, set[int]] = {}
    declared_moments: dict[str, set[int]] = {}
    for claim in required:
        for binding in claim.evidence:
            binding_moments.setdefault(binding.id, set()).update(
                int(moment) for moment in claim.moments
            )
            if getattr(binding, "moments", None) is not None:
                declared_moments.setdefault(binding.id, set()).update(
                    int(moment) for moment in binding.moments
                )
    def _due_here(binding_id: str) -> bool:
        # the author's declared moments are the model; frame-inference is only the
        # fallback for undeclared bindings on frame-carrying contracts
        declared_set = declared_moments.get(binding_id)
        if declared_set is not None:
            return int(frame) in declared_set
        declared = (contract_frames or {}).get(binding_id)
        if declared is None or int(declared) == int(frame):
            return True
        return int(declared) not in binding_moments.get(binding_id, set())
    required_ids = {
        binding.id
        for claim in required
        for binding in claim.evidence
        if _due_here(binding.id)
    }
    by_id = {str(row.get("id")): row for row in evidence if row.get("id")}
    missing = sorted(required_ids - set(by_id))
    failures = [
        by_id[eid]
        for eid in sorted(required_ids & set(by_id))
        if not by_id[eid].get("pass")
    ]
    worklist_failures = [
        row for row in evidence if row.get("source") == "builder_state" and not row.get("pass")
    ]
    passed = not missing and not failures and not worklist_failures
    # A metric that could not be measured is not a metric that measured and missed.
    # `smooth_fraction` over camera roles reads None because there is no mesh to shade;
    # reporting that as "fails" sent two repair rounds after something unfixable
    # (run 20260824T060927Z). Name it as inapplicable and point at the binding.
    def _issue(row: dict) -> str:
        head = f"[check:{row['id']}]"
        if row.get("value") is None:
            # The evidence row already carries WHY it could not be measured; withholding
            # it left a probe of the live scene as the only way to learn that 50 objects
            # were in frame and the projection still returned nothing.
            why = str(row.get("error") or "").strip()
            # Selector ambiguity/absence is the BUILDER's tagging to fix — run
            # 20260825T044518Z's repair read the blanket "needs re-materialization"
            # verdict for a 7-nodes-one-tag defect and deferred a fix that was one
            # retag away. Only a metric that cannot apply to the subject CLASS is a
            # binding defect.
            lowered = why.lower()
            if "matched 0 nodes" in lowered or "matched no objects" in lowered:
                return (
                    f"{head} {row.get('metric')} could not be measured: {why}. The "
                    "contract's subject does not exist yet — CREATE it and tag it with "
                    "the contract's role/control; this is this build's defect to fix."
                )
            if "matched" in lowered and "nodes" in lowered:
                return (
                    f"{head} {row.get('metric')} could not be measured: {why}. The "
                    "semantic selector must resolve to EXACTLY ONE node — remove the "
                    "role/control tag from the duplicates so one node carries it; this "
                    "is this build's tagging defect to fix, not a binding defect."
                )
            if "has no requested" in lowered and "socket" in lowered:
                return (
                    f"{head} {row.get('metric')} could not be measured: {why}. When the "
                    "contract names no socket, resolution looks for a socket literally "
                    "named 'Value' (inputs then outputs) — tag a ShaderNodeValue whose "
                    "output drives the quantity, or a node with a 'Value' socket; this "
                    "is this build's tagging defect to fix, not a binding defect."
                )
            return (
                f"{head} contract is INAPPLICABLE to its subject: {row.get('metric')} "
                f"could not be measured on these roles (target {row.get('target')})"
                + (f" — {why}" if why else "")
                + ". This is a binding defect, not a build defect — the metric cannot "
                "apply to what the claim names; it needs re-materialization, not a repair."
            )
        hint = _reproduction_hint(row)
        note = str(row.get("note") or "").strip()
        return (
            f"{head} executable contract fails: {row.get('metric')} reads "
            f"{row.get('value')} against {row.get('target')}"
            + (f" — {note}" if note else "")
            + (f" · reproduce: {hint}" if hint else "")
        )

    issues = [_issue(row) for row in [*failures, *worklist_failures]]
    issues.extend(f"[check:{eid}] required bound evidence was not produced" for eid in missing)
    scored_axes = [key for key, _description in axes]
    score = 5 if passed else 1
    return {
        "scores": dict.fromkeys(scored_axes, score),
        "mean": float(score),
        "pass": passed,
        "issues": issues,
        "scored_axes": scored_axes,
        "na_axes": [],
        "observations": [],
        "evidence": evidence,
        "evidence_failures": [*failures, *worklist_failures],
        "missing_evidence": missing,
        "decided_by": "unit_executable_evidence",
        "judge_conflict": False,
        "contract_gap": bool(missing),
    }


async def _judge_unit_or_layer(
    shot: Shot,
    m: Milestone,
    candidate_rel: str,
    axes: list[tuple[str, str]],
    session: BlenderSession,
    verbose: bool,
    scope: str | None,
    evidence: list[dict],
    *,
    active_unit=None,
    **kwargs,
) -> dict:
    from vfx_harness.evidence.scene_checks import load_rows as _load_contract_rows

    try:
        contract_frames = {
            str(r.get("id")): int(r.get("frame"))
            for r in _load_contract_rows(shot.folder)
            if isinstance(r, dict) and r.get("id") and r.get("frame") is not None
        }
    except (OSError, ValueError):
        contract_frames = {}
    verdict = _executable_unit_verdict(
        active_unit, int(m.frame), axes, evidence, contract_frames=contract_frames
    )
    if verdict is None:
        return await _judge(
            shot,
            m,
            candidate_rel,
            axes,
            session,
            verbose,
            scope,
            evidence=evidence,
            **kwargs,
        )
    status = "PASS ✅" if verdict["pass"] else "REVISE ✎"
    expected = len(_unit_evidence_ids(active_unit, int(m.frame)) or ())
    observed = expected - len(verdict["missing_evidence"])
    log(f"unit evidence: {observed}/{expected} bound checks observed · {status}", 1)
    for issue in verdict["issues"][:6]:
        log(f"· fix: {issue}", 2)
    transcript.event(
        "unit_evidence",
        milestone=m.id,
        frame=m.frame,
        candidate=candidate_rel,
        verdict="pass" if verdict["pass"] else "revise",
        decided_by=verdict["decided_by"],
        evidence=evidence,
        missing_evidence=verdict["missing_evidence"],
    )
    return verdict


def _worklist_evidence(shot_folder: str | Path, layer_id: str) -> list[dict]:
    """Turn builder-declared unfinished work into a deterministic handoff blocker.

    The worklist is durable agent state, not a visual opinion.  If the builder says a
    scoped item remains open, a flattering critic score must not erase that fact.  The
    failed evidence row routes through the ordinary evidence gate, which reopens one
    repair round.  Missing/empty worklists remain non-authoritative so older shots do not
    acquire a new contract merely by being inspected.
    """
    path = (run_artifacts.shot_state_dir(shot_folder)
            / "worklists" / f"layer-{layer_id}.json")
    if not path.is_file():
        return []
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        items = [str(item) for item in state.get("items", [])]
        done = {str(item) for item in state.get("done", [])}
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        return [
            {
                "id": f"L{layer_id}-builder-worklist-valid",
                "axis": "",
                "metric": "builder_worklist",
                "value": None,
                "target": "valid durable worklist",
                "pass": False,
                "origin": "harness",
                "source": "builder_state",
                "authoritative": True,
                "owner_layer": layer_id,
                "fault_owner": layer_id,
                "error": str(exc)[:160],
            }
        ]
    if not items:
        return []
    left = [item for item in items if item not in done]
    return [
        {
            "id": f"L{layer_id}-builder-worklist-complete",
            "axis": "",
            "metric": "builder_worklist_open_items",
            "value": len(left),
            "target": "= 0 ± 0",
            "pass": not left,
            "origin": "harness",
            "source": "builder_state",
            "authoritative": True,
            "owner_layer": layer_id,
            "fault_owner": layer_id,
            "open_items": left[:6],
        }
    ]


def _layer_motion_frames(layer, m: Milestone, total_frames: int) -> list[int] | None:
    """Return one strip that spans the whole temporal unit, not one local moment.

    A motion-owned layer with several judge frames is a sequence contract.  Showing the
    primary frame's local acceptance strip can contain only a deliberate hold and still
    invite a high continuity score.  Preserve every judge frame and add the midpoint of
    each interval so starts, transitions and settles all have temporal evidence.
    """
    judges = sorted({int(frame) for frame, _ref in (getattr(layer, "judges", ()) or ())})
    if len(judges) > 1:
        mids = [round((left + right) / 2) for left, right in pairwise(judges)]
        return sorted({frame for frame in [*judges, *mids] if 1 <= frame <= total_frames})
    if m.strip:
        return sorted({int(frame) for frame in m.strip if 1 <= int(frame) <= total_frames})
    return None


def _stash_motion_strip(
    session: BlenderSession,
    shot: Shot,
    m: Milestone,
    tag: str,
    span: int = 6,
    scale: float = 0.4,
    frames_override: list[int] | None = None,
):
    """Montage a few frames around the judge frame so the critic can judge MOTION — a
    single still can't show blur or continuity. Returns (rel_path, frames).

    Prefers the PLAN's strip for this moment (acceptance.json), which the planner chose
    to cover the beat with a stated max gap. The old default only stepped FORWARD from
    the judge frame, so a botched approach was structurally invisible: barrel_roll's M2
    was judged at f20 (correctly near-black, scored 4.0) while f16-f18 sat at mean ~57
    with the world-swap in full view — and the strip [12,16,18,20,22] that would have
    caught it was parsed, stored, and never used.
    """
    from PIL import Image

    if frames_override:
        frames = sorted({f for f in frames_override if 1 <= f <= shot.frames})
    elif m.strip:
        frames = sorted({f for f in m.strip if 1 <= f <= shot.frames})
    else:
        frames = sorted({m.frame, min(shot.frames, m.frame + span), min(shot.frames, m.frame + 2 * span)})
    MAX = 8  # enough for four judge beats plus the transitions between them
    if len(frames) > MAX:
        # Thin the middle, but the JUDGE FRAME is never droppable — it is the frame the
        # verdict is about. (A naive sorted(...)[:MAX] silently cut f72 off SH's M1.)
        others = [f for f in frames if f != m.frame]
        step = max(1, round(len(others) / (MAX - 1)))
        thinned = others[::step][: MAX - 1]
        if others and others[-1] not in thinned:  # always keep the far end of the beat
            thinned = [*thinned[: MAX - 2], others[-1]]
        frames = sorted({*thinned, m.frame})
    ims = [Image.open(session.render(frame=f, mode="eevee", scale=scale)).convert("RGB") for f in frames]
    h = min(im.height for im in ims)
    ims = [im.resize((max(1, round(im.width * h / im.height)), h)) for im in ims]
    sheet = Image.new("RGB", (sum(im.width for im in ims), h), (10, 10, 12))
    x = 0
    for im in ims:
        sheet.paste(im, (x, 0))
        x += im.width
    dest = run_artifacts.renders_dir(shot.folder) / f"{m.id}_{tag}_motion.png"
    dest.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(dest)
    return dest.relative_to(shot.folder).as_posix(), frames


# --------------------------------------------------------------------------- #
# The loop                                                                     #
# --------------------------------------------------------------------------- #
def _blender_version(session: BlenderSession) -> str:
    try:
        row = session.run("RESULT = bpy.app.version_string", journal=False)
        return str(row.get("result") or row.get("RESULT") or "unknown")
    except Exception:
        return "unknown"


def _scene_object_manifest(session: BlenderSession) -> dict[str, str]:
    result = session.run(
        "RESULT = {o.name: str(o.get('bvfx_role') or '') for o in bpy.context.scene.objects}",
        journal=False,
    ).get("result")
    return {str(name): str(role) for name, role in (result or {}).items()}


def _scope_added_object_errors(
    before: dict[str, str], after: dict[str, str], allowed_roles: tuple[str, ...]
) -> list[str]:
    """Reject persisted helpers and semantic roles outside a scoped unit's authority."""
    def allowed(role: str) -> bool:
        for pattern in allowed_roles:
            if fnmatch.fnmatchcase(role, pattern):
                return True
            # Mutation manifests name semantic namespaces (``camera``), while
            # executable contracts and objects use concrete descendants
            # (``camera.*`` / ``camera.main``). A namespace owns only its
            # dot-delimited descendants, never a similar sibling such as ``camera_rig``.
            if not any(token in pattern for token in "*?[") and role.startswith(pattern + "."):
                return True
        return False

    errors = []
    for name in sorted(set(after) - set(before)):
        role = after[name]
        if not role:
            errors.append(f"new object {name!r} has no bvfx_role")
        elif not allowed(role):
            errors.append(
                f"new object {name!r} has undeclared role {role!r}; allowed {list(allowed_roles)}"
            )
    return errors


def _try_revalidate(
    shot: Shot,
    m: Milestone,
    script_rel: str,
    prior_paths: list[Path],
    session: BlenderSession,
    *,
    layer,
    ledger: Ledger,
    t_layer: float,
    active_unit=None,
) -> Ledger | None:
    """Replay an unchanged sealed layer without launching builder or critic models."""
    if layer is None:
        return None
    from vfx_harness.orchestration.layer_plans import load_layer_outcome, record_revalidation
    from vfx_harness.orchestration.revalidation import eligibility, input_manifest

    outcome = load_layer_outcome(shot.folder, str(layer.id))
    blender_version = _blender_version(session)
    manifest = input_manifest(shot.folder, layer, blender_version=blender_version)
    eligible, reasons = eligibility(outcome, manifest, shot.folder)
    if not eligible:
        if outcome:
            log(f"revalidation fast path unavailable: {'; '.join(reasons[:3])}", 1)
        return None

    log("REVALIDATE: sealed inputs are unchanged; replaying scripts and authoritative evidence before any model launch")
    try:
        session.run(_RESET)
        session.run(_preamble(shot))
        _run_prior_paths(session, prior_paths)
        before_objects = _scene_object_manifest(session)
        session.run((shot.folder / script_rel).read_text(encoding="utf-8"))
        # The unit whose script just replayed owns the scope being checked. Falling back
        # to "the only stage" silently SKIPPED scope entirely for every multi-unit
        # layer — a fast path that cannot verify scope must not be taken at all.
        unit = active_unit or (layer.stages[0] if len(layer.stages) == 1 else None)
        if unit is not None and unit.mutates.mode == "scoped":
            scope_errors = _scope_added_object_errors(
                before_objects, _scene_object_manifest(session), unit.mutates.roles
            )
            if scope_errors:
                log(f"REVALIDATE miss: scoped artifact violation ({'; '.join(scope_errors[:3])})", 1)
                return None
        elif unit is None and any(
            stage.mutates.mode == "scoped" for stage in layer.stages
        ):
            log(
                "REVALIDATE miss: layer has scoped units but no active unit to check "
                "their artifact scope against",
                1,
            )
            return None
    except Exception as exc:
        log(f"REVALIDATE miss: deterministic replay failed ({str(exc)[:120]})", 1)
        return None

    sealed = {int(row["frame"]): row for row in outcome.get("canonical") or []}
    canonical = []
    frame_results = []
    judges = list(layer.judges)
    for frame, ref in judges:
        m_i = m if len(judges) == 1 else layer.milestone_at(frame, ref, plan_strips(shot))
        render_rel = _stash_render(session, shot, m_i, f"revalidate_f{frame}")
        evidence = _render_evidence(shot, layer, m_i, render_rel, session)
        authoritative = [row for row in evidence if row.get("authoritative")]
        prior = sealed.get(int(frame)) or {}
        reproduction = _image_reproduction(shot.folder / str(prior.get("render", "")), shot.folder / render_rel)
        passed = bool(authoritative and all(row.get("pass") for row in authoritative) and reproduction.get("match"))
        verdict = {
            "scores": {},
            "mean": float((outcome.get("best") or {}).get("mean") or 4.0),
            "pass": passed,
            "issues": [],
            "evidence": evidence,
            "decided_by": "deterministic_revalidation",
            "reproduction": reproduction,
            "render": render_rel,
        }
        canonical.append(((frame, ref), verdict))
        frame_results.append(
            {
                "frame": frame,
                "pass": passed,
                "authoritative_total": len(authoritative),
                "authoritative_passed": sum(bool(row.get("pass")) for row in authoritative),
                "reproduction": reproduction,
            }
        )
    if not all(row[1]["pass"] for row in canonical):
        bad = [str(frame) for (frame, _ref), verdict in canonical if not verdict["pass"]]
        log(
            f"REVALIDATE miss: evidence or canonical pixels changed at f{', f'.join(bad)}; "
            "falling back to the full builder",
            1,
        )
        return None

    best = dict(outcome.get("best") or {})
    ledger.record_round(m, kind="revalidate", index=0, render=canonical[0][1]["render"], verdict=canonical[0][1])
    ledger.mark(m, "passed", best=best)
    attempt = int(ledger._slot(m).get("attempt") or 0)
    record_revalidation(shot.folder, str(layer.id), run_id=RUN_ID, attempt=attempt, evidence=frame_results)
    rec_path = write_run(
        shot.folder,
        layer,
        status="passed",
        rounds=[{"kind": "revalidate", "mean": canonical[0][1]["mean"], "pass": True}],
        canonical=[
            {
                "frame": frame,
                "mean": verdict["mean"],
                "pass": True,
                "decided_by": "deterministic_revalidation",
                "evidence": verdict["evidence"],
                "reproduction": verdict["reproduction"],
            }
            for (frame, _ref), verdict in canonical
        ],
        cost=0.0,
        turns=0,
        seconds=time.monotonic() - t_layer,
        extra={
            "run_id": RUN_ID,
            "attempt": attempt,
            "revalidation": True,
            "cost_sessions": 0,
            "cost_by_role": {},
            "tools": {
                "calls": {},
                "total": 0,
                "adoption": {},
                "applicability": dict.fromkeys(("render_pass", "check_scene", "diff_frames", "verify_change"), False),
                "unused_required_tools": [],
                "not_applicable_tools": ["render_pass", "check_scene", "diff_frames", "verify_change"],
            },
        },
    )
    log("\n" + run_summary(json.loads(rec_path.read_text(encoding="utf-8"))))
    log("REVALIDATE PASS: authoritative evidence and sealed pixels are unchanged; builder and critic sessions skipped")
    return ledger


def _retry_warm_start(previous_status: str, script_path: Path) -> bool:
    """Whether a fresh retry should replay its last artifact into the warm scene."""
    retryable = {"failed", "judge_conflict", "contract_gap", "truncated", "in_progress"}
    return previous_status in retryable and script_path.is_file()


async def build_unit(
    shot: Shot,
    m: Milestone,
    script_rel: str,
    prior_paths: list[Path],
    session: BlenderSession,
    *,
    rounds: int = 2,
    verbose: bool = True,
    plan_excerpt: str = "",
    scope: str | None = None,
    layer=None,
    active_unit=None,
    publish_layer: bool = True,
    report_layer=None,
    resume_ok: bool = False,
) -> Ledger:
    """The build+critic engine for ONE unit of work. Iteration is judged at m.frame vs
    m.ref; the canonical check covers every frame `layer` claims (see _verify_script)."""
    ledger = Ledger(shot)
    previous_slot = dict(ledger._slot(m))
    previous_status = str(previous_slot.get("status") or "")
    retry_script = shot.folder / script_rel
    warm_start_candidate = _retry_warm_start(previous_status, retry_script)
    ledger._slot(m)["script"] = script_rel
    ledger.begin(m)
    t_layer = time.monotonic()

    revalidated = _try_revalidate(
        shot, m, script_rel, prior_paths, session, layer=layer, ledger=ledger,
        t_layer=t_layer, active_unit=active_unit,
    )
    if revalidated is not None:
        return revalidated

    all_axes = await ensure_axes(shot, verbose)  # per-shot critic rubric (from the plan)
    _warn_unowned_axes(shot, all_axes)
    # A layer's ownership is already deterministic in layers.json. Passing every rubric
    # axis and asking the critic to decide which were n/a made the denominator move between
    # identical repeats. Filter before the builder prompt, critic prompt, and JSON schema.
    axes = _owned_axes(all_axes, layer)
    # Shared with both the Blender tool server and the PreToolUse phase guard. The tool
    # flips scene_contracts_passed atomically; the next speculative mutation is denied.
    #
    # Typed unit authority decides look scope. Only a unit that declares no capabilities
    # at all (legacy schema-4 layers) falls back to scanning axis identifiers, which
    # silently denied an appearance-owning unit its own feedback in run 20260823T154920Z.
    _declared_capabilities = tuple(getattr(active_unit, "look_capabilities", ()) or ())
    _feedback_groups = (
        capability_feedback_groups(_declared_capabilities)
        if _declared_capabilities
        else axis_feedback_groups(axes)
    )
    _look_actions = bool(_feedback_groups)
    active_evidence_ids = _unit_completion_evidence_ids(active_unit)
    active_image_evidence_ids = {
        binding.id
        for claim in (active_unit.evaluation.claims if active_unit else ())
        if claim.required
        for binding in claim.evidence
        if binding.kind == "image_contract"
    }
    image_evidence_required = image_evidence_required_for(
        active_image_evidence_ids, _declared_capabilities
    )
    phase = {
        "mode": "live",
        "round": 1,
        "frame": int(m.frame),
        "look_actions": _look_actions,
        "active_evidence_ids": active_evidence_ids,
        "active_image_evidence_ids": active_image_evidence_ids,
        "image_evidence_required": image_evidence_required,
    }
    comparison_state = phase
    scope_baseline: set[str] = set()
    bserver, bnames = build_blender_tools(
        session,
        assets_dir=shot.folder / "assets",
        shot_dir=shot.folder,
        layer_id=getattr(layer, "id", m.id),
        comparison_state=comparison_state,
        feedback_groups=sorted(_feedback_groups) if _declared_capabilities else None,
        mutation_roles=(
            active_unit.mutates.roles
            if active_unit is not None and active_unit.mutates.mode == "scoped"
            else None
        ),
        # populated AFTER reset+priors+warm-start replay below (the server is built
        # first): prior layers' objects are their own authority, not this unit's
        # violations — without the baseline the live scope check told every later
        # unit to delete the previous layers' sealed work
        scope_baseline=scope_baseline,
    )
    rserver, rnames = build_recipe_tools(
        on_use=lambda names: (log_recipe_use(shot.folder, names), _RECIPES_USED.extend(names))
    )
    mcp_servers = {"blender": bserver, "recipes": rserver}
    tool_names = bnames + rnames

    # Deterministic base + prior delta scripts (a unit extends the existing scene).
    session.run(_RESET)
    session.run(_preamble(shot))
    resume = ledger.get_resume(m) if resume_ok else None
    if resume:
        log(
            f"↻ resuming layer {m.id} from round {resume['round']}: restoring scene "
            f"+ replaying journal[{resume['journal_index']}:]"
        )
        session.restore(resume["blend"])
        try:
            n = session.replay(resume["journal_index"]).get("replayed", 0)
            log(f"  replayed {n} journalled call(s) — scene matches the session", 1)
        except BlenderError as e:
            log(f"  ! replay failed ({str(e)[:60]}) — continuing from the checkpoint", 1)
        priors = [Path(p).name for p in prior_paths]
    else:
        priors = _run_prior_paths(session, prior_paths)

    if layer is not None and int(layer.id) > 1:
        from vfx_harness.evidence.scene_checks import prior_interface_evidence

        interfaces = prior_interface_evidence(shot.folder, str(layer.id), session=session)
        failed_interfaces = [row for row in interfaces if not row.get("pass")]
        if failed_interfaces:
            detail = "; ".join(
                f"{row.get('id')}={row.get('value')} target {row.get('target')} (repair layer {row.get('fault_owner')})"
                for row in failed_interfaces[:6]
            )
            raise BlenderError(
                f"layer {layer.id} cannot start: prior interface revalidation failed: "
                f"{detail}. Rebuild the fault-owning layer; downstream compensation is "
                "forbidden"
            )
        if interfaces:
            log(f"prior interface preflight: {len(interfaces)}/{len(interfaces)} pass", 1)

    warm_started = False
    if warm_start_candidate:
        # A failed canonical script is still valuable measured work.  Starting the next
        # attempt from priors alone made the builder spend another hour recreating the
        # same rig, while the artifact containing its best state sat unused on disk.
        # Replay it through the journal so finalisation still sees a complete L4 delta.
        # If it no longer executes, restore the clean prior chain and fall back loudly.
        try:
            session.run(retry_script.read_text(encoding="utf-8"))
            warm_started = True
            log(
                f"retry warm start: replayed prior {previous_status} artifact {script_rel}; "
                "builder will repair this scene instead of rebuilding it",
                1,
            )
        except BlenderError as exc:
            log(f"retry warm start rejected ({str(exc)[:120]}); restoring clean prior layers", 1)
            session.run(_RESET)
            session.run(_preamble(shot))
            priors = _run_prior_paths(session, prior_paths)

    # The scene is now fully staged (priors + any warm-start replay): everything present
    # is inherited authority the live scope check must not flag against this unit.
    scope_baseline.update(_scene_object_manifest(session))

    # Per-layer, not per-process: the counts are attributed to one layer's report.
    reset_tool_use()
    reset_counts()
    # The durable record of this layer's conversation. Bound BEFORE the kickoff so the
    # very first thing in the file is the prompt the builder was given — a transcript of
    # answers to an unrecorded question cannot be audited, and the contract is exactly
    # what changes between the runs we want to compare.
    _attempt = int(ledger._slot(m).get("attempt") or 0)
    costlog.bind(
        shot.folder,
        role="builder",
        phase="live_build",
        layer=str(getattr(layer, "id", m.id)),
        run_id=RUN_ID,
        attempt=_attempt,
        model=builder_model(),
    )
    _tpath = transcript.bind(shot.folder, "build", label=f"layer{getattr(layer, 'id', m.id)}", run_id=RUN_ID)
    if _tpath:
        log(f"transcript → {_tpath.relative_to(shot.folder)}", 1)
    # Conclusions that outlive the transcript: a compaction or a crash-resume costs the
    # conversation, not the measured state of each judge frame or what has been ruled out.
    state_start(shot.folder, getattr(layer, "id", m.id), list(getattr(layer, "judges", None) or [(m.frame, m.ref)]))
    canon_verdicts: list = []
    passed = False
    reviewed = False  # one approach review per layer; a second plateau stops
    best = {"mean": -1.0, "round": 0, "render": None, "verdict": None}
    prev_mean = None
    ticket_context = _builder_ticket_context(plan_excerpt, scope, axes, layer)
    opts = _builder_options(
        shot,
        mcp_servers,
        tool_names,
        axes,
        ref_rel=m.ref,
        script_rel=script_rel,
        phase=phase,
        ticket_context=ticket_context,
    )
    if resume and resume.get("session_id"):
        opts.resume = resume["session_id"]  # SDK restores the CONVERSATION
    async with ClaudeSDKClient(options=opts) as builder:
        also = [(f, r) for f, r in (layer.judges if layer else ()) if f != m.frame]
        # What earlier ATTEMPTS at this layer were told and kept being told. Without
        # this each attempt starts blind to the last one's corrections: layer 5's second
        # attempt rebuilt a six-light rig not knowing the first had twice been told the
        # hero was not light-linked and the podium was overlit.
        hist = recurring_complaints(shot, m)
        if warm_started:
            hist += (
                f"\nRETRY WARM START: `{script_rel}` from the previous {previous_status} "
                "attempt has already been replayed into the live scene and journal. Inspect "
                "and repair that state; do not delete it and rebuild the same rig. The final "
                "script must still contain the complete layer delta.\n"
            )
        # The operator's retry reason is durable state's most valuable line for THIS
        # session, and it was never delivered: run bo636v8h1 wandered the exact sign
        # inversion its reopen reason spelled out, because `vfx units retry --reason`
        # landed in the unit history and the kickoff never read it.
        if layer is not None and active_unit is not None:
            try:
                from vfx_harness.orchestration.unit_state import load as _load_unit_state

                _events = (
                    (_load_unit_state(shot.folder, str(layer.id)).get("units") or {})
                    .get(str(active_unit.id), {})
                    .get("history")
                    or []
                )
                _reopen = next(
                    (
                        str(event.get("reason") or "")
                        for event in reversed(_events)
                        if event.get("to") == "retryable"
                    ),
                    "",
                )
            except (OSError, ValueError):
                _reopen = ""
            if _reopen:
                hist += (
                    "\nREOPENED BY OPERATOR — this retry exists because:\n"
                    f"{_reopen}\n"
                )
        if hist:
            log(f"prior attempts: surfacing {hist.count('    - ')} recurring complaint(s) to the builder", 1)
        _kickoff = builder_kickoff(
            shot,
            m,
            priors=priors,
            script_rel=script_rel,
            plan_excerpt=plan_excerpt,
            also_judged=also or None,
            history=hist,
        )
        transcript.prompt(
            _kickoff,
            role="kickoff",
            layer=getattr(layer, "id", m.id),
            judges=[[f, r] for f, r in (layer.judges if layer else ())],
            owns=list(getattr(layer, "owns", ())),
            model=builder_model(),
            system_prompt_chars=len(opts.system_prompt or ""),
        )
        await builder.query(_kickoff)
        _tools_before = sum(TOOL_USE.values())
        info = last_info = await _drain(builder, verbose)
        # A layer that spent nothing and touched no tool did not build anything, whatever
        # the result subtype claims. Caught here rather than after the critic, because the
        # next thing this function does is pay a vision model to look at an empty scene.
        _why = empty_success(info, sum(TOOL_USE.values()) - _tools_before)
        if _why:
            log(f"✗ build DID NOTHING: {_why}")
            transcript.event("empty_success", why=_why, **info)
            ledger.mark(m, "failed", best=None)
            raise BuildTruncated(f"layer {m.id}: {_why}")
        if info["subtype"] in _TRUNCATED:
            # Scoring a half-built scene produces a "failed" that says nothing about the
            # look, buys a misleading ledger entry, and pays the critic to judge it.
            log(
                f"✗ build TRUNCATED ({info['subtype']}, {info['turns']} turns, "
                f"${info['cost']:.2f}) — not critiquing an unfinished scene"
            )
            ledger.mark(m, "truncated", best=None)
            raise BuildTruncated(
                f"layer {m.id}: {info['subtype']} after {info['turns']} turns "
                f"(${info['cost']:.2f}); raise MAX_BUDGET_USD or split the layer"
            )

        for rnd in range(1, rounds + 1):
            log(f"── round {rnd}/{rounds} — rendering + critiquing frame {m.frame} ──")
            t_round = time.monotonic()
            render_rel = _stash_render(session, shot, m, f"r{rnd}")
            snap = session.snapshot(f"{m.id}_r{rnd}")  # {blend, journal_index}
            # Resume point: the SDK restores the CONVERSATION, the snapshot+journal
            # restores the SCENE. Both are needed or a resumed layer reasons about a
            # world that no longer exists.
            ledger.set_resume(
                m,
                session_id=last_info.get("session_id"),
                blend=snap["blend"],
                journal_index=snap["journal_index"],
                round=rnd,
            )
            # Show the critic the previous best. Judging each round in isolation, it
            # re-derives an absolute verdict every time and cannot tell a round that
            # IMPROVED things from one that made them worse — which is also part of why
            # the same render scored 4.0/3.0/3.0/2.0 across repeats. Attaching one more
            # image is nearly free now that images are attached rather than fetched.
            prior = best.get("render") if best.get("render") and best["render"] != render_rel else None
            evidence = _render_evidence(
                shot, layer, m, render_rel, session, active_unit=active_unit
            )
            verdict = await _judge_unit_or_layer(
                shot,
                m,
                render_rel,
                axes,
                session,
                verbose,
                scope,
                prior_rel=prior,
                prior_mean=best["mean"],
                evidence=evidence,
                active_unit=active_unit,
                motion_frames_override=_layer_motion_frames(layer, m, shot.frames),
                allow_motion=_layer_needs_motion(layer),
            )
            verdict["round_s"] = round(time.monotonic() - t_round, 1)
            convergence_stop = _evidence_convergence_stop(layer, verdict)
            if convergence_stop:
                verdict["convergence_stop"] = "authoritative_owned_evidence"
            ledger.record_round(m, kind="iter", index=rnd, render=render_rel, verdict=verdict)
            state_round(
                shot.folder,
                frame=m.frame,
                mean=verdict["mean"],
                passed=verdict["pass"],
                scores=verdict.get("scores"),
                issues=verdict.get("issues"),
                approach=_APPROACH.get("text"),
            )
            # best-of-N: a valid round outranks an invalid round before aesthetic mean.
            # In particular, do not restore a contract-failing 4.0 over a later passing
            # 4.0 during finalize (the scene graph and pixels may differ independently).
            if _round_rank(verdict) > _round_rank(best.get("verdict")):
                best = {"mean": verdict["mean"], "round": rnd, "render": render_rel, "verdict": verdict, "snap": snap}
                best_path = run_artifacts.renders_dir(shot.folder) / f"{m.id}_best.png"
                best_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(shot.folder / render_rel, best_path)
            if verdict["pass"]:
                passed = True
                break
            if convergence_stop:
                log(
                    "Convergence stop: every authoritative contract passes and "
                    "the critic supplied no evidence-backed actionable defect; finalizing "
                    "instead of making another speculative geometry edit",
                    1,
                )
                break
            # A plateau is where a professional CHANGES TECHNIQUE, not where they stop.
            # This used to `break`: layer G tuned to 2.83 twice while city_texture sat at
            # 2 in every round, because nothing ever asked if instanced boxes with a
            # regular window grid could reach the reference at all. They cannot.
            plateaued = prev_mean is not None and verdict["mean"] <= prev_mean
            prev_mean = verdict["mean"]
            if rnd >= rounds:
                break
            comparison_state["round"] = rnd + 1
            # A critic-backed revision is the only event that reopens geometry mutation.
            phase["scene_contracts_passed"] = False
            if plateaued and layer is not None and not reviewed:
                reviewed = True
                log(f"plateau ({verdict['mean']} ≤ prev) — escalating to APPROACH REVIEW")
                with costlog.scoped(
                    role="approach_reviewer",
                    phase="approach_review",
                    model=Settings.from_environment(load_dotenv_file=False).reviewer_model,
                ):
                    out = await approach_review(
                        shot,
                        layer,
                        render_rel,
                        verdict,
                        script_rel,
                        metric_report=_metric_report(shot, render_rel, layer.judge_ref),
                        verbose=verbose,
                    )
                if not out["text"]:
                    break  # review unavailable: old behaviour
                ledger.record_review(m, rnd, out)
                await builder.query(revision_from_review(layer, out))
            elif plateaued:
                log(f"plateau again after review ({verdict['mean']}) — stopping revisions")
                break
            else:
                await builder.query(revision_prompt(m, verdict, render_rel))
            last_info = await _drain(builder, verbose)
        log(f"best round: r{best['round']} mean {best['mean']} → renders/{m.id}_best.png")

        # finalize from the BEST round's scene, not the last one — a regressed revision
        # must not be what gets written into build/<m>.py (cost build8 the layer).
        if best.get("snap") and best["round"] != rnd:
            log(f"restoring best round r{best['round']} scene state before finalize")
            session.restore(best["snap"]["blend"])
            await builder.query(
                f"NOTE: the scene has been RESTORED to your round-{best['round']} state "
                f"(the best-scoring round, mean {best['mean']}) — your later revision "
                f"scored worse and was discarded. Inspect and acknowledge THIS restored "
                f"scene only. Do NOT write or edit {script_rel} yet: MODE is still "
                f"LIVE_BUILD, and the harness will send a separate FINALIZE_SCRIPT "
                f"request after it captures the journal."
            )
            await _drain(builder, verbose)

        # Persist the deterministic recipe regardless — it's the artifact of record.
        journal_rel = None
        try:  # a transcript to prune beats re-authoring 20KB+ from memory
            layout = run_artifacts.ensure(shot.folder, command="build")
            journal = layout.checkpoints / "journals" / f"layer-{m.id}.py"
            journal.parent.mkdir(parents=True, exist_ok=True)
            jrel = journal.relative_to(shot.folder).as_posix()
            # The finalizer must see the SELECTED checkpoint's prefix, not every call
            # ever accepted: the scene was just restored to the best round, so later
            # rounds' calls describe a world that no longer exists.
            info = session.journal(
                path=str(journal), limit=(best.get("snap") or {}).get("journal_index")
            )
            if info.get("calls"):
                journal_rel = jrel
                _JOURNAL_INFO.clear()
                _JOURNAL_INFO.update(info)
                log(f"journal: {info['calls']} accepted run_bpy calls ({info['chars'] // 1024}KB) → {jrel}")
                if info.get("dropped"):
                    log(
                        f"truncated to selected round r{best['round']}: "
                        f"{info['dropped']} call(s) from discarded rounds excluded",
                        1,
                    )
        except Exception as e:  # never block finalize on a nicety
            log(f"journal unavailable ({str(e)[:60]})")
        phase["mode"] = "finalize"
        probe_ctx = {
            "blender": session.blender,
            "scratch_dir": str(
                run_artifacts.ensure(shot.folder, command="build").scratch / "candidate-probe"
            ),
            "prior_paths": [str(path) for path in prior_paths],
            "judges": [
                (int(frame), str(ref))
                for frame, ref in (layer.judges if layer is not None else [(m.frame, m.ref)])
            ],
            "layer_id": str(getattr(layer, "id", m.id)),
            "roles": list(active_unit.mutates.roles) if active_unit is not None else [],
        }
        with costlog.scoped(role="finalizer", phase="finalize_script", model=script_model()):
            fin = await _run_script_agent(
                shot,
                mode="finalize",
                script_rel=script_rel,
                prompt=finalize_prompt(shot, m, priors=priors, script_rel=script_rel, journal_rel=journal_rel),
                verbose=verbose,
                probe_ctx=probe_ctx,
            )
        if fin["subtype"] in _TRUNCATED:
            # The script is probably half-written; verifying it would record a look
            # failure for a budget problem (the same lie truncation told at kickoff).
            log(
                f"✗ finalize TRUNCATED ({fin['subtype']}, ${fin['cost']:.2f}) — "
                f"{script_rel} may be incomplete; not scoring it"
            )
            ledger.mark(m, "truncated", best=best)
            raise BuildTruncated(
                f"layer {m.id}: finalize hit {fin['subtype']} (${fin['cost']:.2f}); "
                f"raise MAX_BUDGET_USD or split the layer"
            )

        # Confirm the written script REPRODUCES the unit from an empty scene. This is a
        # reproduction check, NOT a second quality layer — with judge noise, requiring two
        # consecutive layer-clears at the boundary is double jeopardy (cost M3 a pass).
        #
        # Done INSIDE the builder session so a canonical failure can be handed back. It
        # used to run after the session closed, so the critique was unreachable and the
        # layer simply died with it. Both barrel_roll layer-2 attempts produced ~24
        # specific, actionable fixes this way ("brightest strip runs down the centre where
        # the reference has two bright OUTER strips", "sign cabinet inverted: panel glows,
        # text dark") and discarded every one. Closing that gap by hand — read the log,
        # diagnose, edit the plan, re-run — cost $16.52 and two rounds of human attention
        # for feedback the pipeline was already holding.
        canonical = await _verify_script(
            shot,
            m,
            script_rel,
            prior_paths,
            session,
            axes,
            ledger,
            verbose,
            live_best_mean=best["mean"],
            live_best_render=best.get("render"),
            live_best_verdict=best.get("verdict"),
            scope=scope,
            layer=layer,
            active_unit=active_unit,
            out_verdicts=canon_verdicts,
        )

        # Repair rounds, bounded. The target here is the SCRIPT's output from an empty
        # scene — not the live scene the builder has been tuning, which is why it must be
        # re-verified canonically or the loop would keep re-passing live and failing here.
        rejected_repairs: list[str] = []
        for attempt in range(1, MAX_CANON_REPAIRS + 1):
            if canonical != "failed":
                break
            failed = [(f, v) for (f, _r), v in (canon_verdicts or []) if not v.get("pass")]
            # The frames that currently PASS are constraints, not background. Feeding back
            # only the failures produced textbook whack-a-mole: repair 1 fixed f440 and
            # left f45 broken, repair 2 fixed f45 and BROKE f440. Every frame passed at
            # some point; never all at once. The builder was told what was wrong and
            # nothing about what it must not break, so trading one for the other looked
            # like progress. builder_kickoff already says "a change that fixes f{frame}
            # and breaks another of your frames is not a fix" — that instruction just
            # never made it into the repair path.
            holding = [(f, v) for (f, _r), v in (canon_verdicts or []) if v.get("pass")]
            if not failed:
                break
            log(
                f"canonical failed on {len(failed)} frame(s) — repair {attempt}/"
                f"{MAX_CANON_REPAIRS}, feeding the critique back"
                + (f" (protecting {len(holding)} passing frame(s))" if holding else "")
            )
            # Keep the script we are about to modify, plus the verdicts that describe it,
            # so a repair that makes things worse can be undone rather than merely regretted.
            layout = run_artifacts.ensure(shot.folder, command="build")
            backup = layout.checkpoints / "repairs" / f"layer-{m.id}-before-{attempt}.py"
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(shot.folder / script_rel, backup)
            pre_script = backup.read_text(encoding="utf-8")
            pre_verdicts = list(canon_verdicts or [])
            pre_canonical = canonical
            phase["mode"] = "repair"
            repair_error = None
            try:
                with costlog.scoped(
                    role="repair", phase="repair_script", repair=attempt, model=script_model()
                ):
                    last_info = await _run_script_agent(
                        shot,
                        mode="repair",
                        script_rel=script_rel,
                        prompt=canonical_repair_prompt(
                            m,
                            failed,
                            script_rel,
                            holding=holding,
                            rejected_repairs=rejected_repairs,
                        ),
                        verbose=verbose,
                        probe_ctx=probe_ctx,
                    )
            except Exception as exc:
                # Edit is not atomic with the SDK session: the agent can mutate the file
                # and then lose its process before yielding a terminal ResultMessage.
                # Restore before doing anything else so an infrastructure failure can
                # never leak a half-finished repair into the next run.
                repair_error = f"{type(exc).__name__}: {str(exc)[:240]}"
                shutil.copyfile(backup, shot.folder / script_rel)
                rejected_repairs.append(
                    f"repair {attempt} was interrupted before validation ({repair_error}); "
                    "its partial file state was discarded."
                )
                log(
                    f"✗ canonical repair interrupted ({repair_error}) — restored "
                    f"{script_rel} from its pre-repair snapshot",
                    1,
                )
                if attempt < MAX_CANON_REPAIRS:
                    continue
                break
            if last_info["subtype"] in _TRUNCATED:
                # A terminal budget/turn result is the non-exceptional form of the same
                # interrupted transaction.  Never leave the edits in place unverified.
                shutil.copyfile(backup, shot.folder / script_rel)
                rejected_repairs.append(
                    f"repair {attempt} ended at {last_info['subtype']} before canonical "
                    "validation; its partial file state was discarded."
                )
                log(
                    f"✗ canonical repair TRUNCATED ({last_info['subtype']}) — restored "
                    f"{script_rel} from its pre-repair snapshot",
                    1,
                )
                if attempt < MAX_CANON_REPAIRS:
                    continue
                break
            # Re-verify, then compare against EVERY frame's pre-repair score rather than
            # only the failing ones — see _repair_delta, which owns both judgements (did
            # this break a passing frame, and did it move toward a pass at all).
            canon_verdicts.clear()
            canonical = await _verify_script(
                shot,
                m,
                script_rel,
                prior_paths,
                session,
                axes,
                ledger,
                verbose,
                live_best_mean=best["mean"],
                live_best_render=(best.get("render") if passed else None),
                live_best_verdict=(best.get("verdict") if passed else None),
                scope=scope,
                layer=layer,
                active_unit=active_unit,
                out_verdicts=canon_verdicts,
            )
            delta = _repair_delta(pre_verdicts, canon_verdicts or [])
            was, now, broke = delta["was"], delta["now"], delta["broke"]
            action = _repair_action(delta, attempt)
            if action != "accept":
                # ENFORCE monotonicity transactionally.  A regressed or inert patch is
                # never retained, but an unused attempt remains useful when the next
                # agent is shown which approach was rejected and why.
                post_script = (shot.folder / script_rel).read_text(encoding="utf-8")
                if broke:
                    reason = (
                        f"repair {attempt} regressed protected frame(s) "
                        f"{', '.join('f' + str(f) for f in broke)} "
                        f"({{ {', '.join(f'{f}: ({was[f]}, {now.get(f)})' for f in broke)} }})."
                    )
                else:
                    reason = (
                        f"repair {attempt} moved neither the worst failing frame "
                        f"({delta['was_worst']} → {delta['now_worst']}) nor the failing-frame count "
                        f"({delta['was_failing']} → {delta['now_failing']})."
                    )
                change_summary = _repair_change_summary(pre_script, post_script)
                rejected_repairs.append(
                    reason
                    + (
                        f"\nRejected script delta (do not repeat this mechanism unchanged):\n{change_summary}"
                        if change_summary
                        else ""
                    )
                )
                shutil.copyfile(backup, shot.folder / script_rel)
                log(
                    f"✗ {reason} Reverting {script_rel} to its pre-repair state"
                    + (
                        "; trying the remaining repair approach"
                        if action == "rollback_retry"
                        else "; repair budget exhausted"
                    ),
                    1,
                )
                canon_verdicts.clear()
                canon_verdicts.extend(pre_verdicts)
                canonical = pre_canonical
                if action == "rollback_retry":
                    continue
                break
    # The CANONICAL render is the deliverable — it is what the chain re-runs. If it
    # clears the bar the unit passed, whatever the live search scored on the way (with
    # finalize-from-best-snapshot the clean rebuild often outscores every live round:
    # SH G20 rounds 2.67/3.00, canonical 3.67 — previously recorded as a failure).
    ok = canonical == "passed" or (passed and canonical == "reproduced")
    status = (
        "passed"
        if ok
        else "contract_gap"
        if canonical == "contract_gap"
        else "judge_conflict"
        if canonical == "judge_conflict"
        else "failed"
    )
    if layer is not None and publish_layer:
        # The renders are final only now. A builder check authored mid-layer was proven
        # against whatever render existed then, and a later attempt replaced it — three of
        # layer 1's shipped as stale. Re-verify here, where "the render" stops moving,
        # even when a noisy critic disagrees with it.
        try:
            from vfx_harness.evaluation.plan_gate import _builder_render
            from vfx_harness.evidence.checks import revalidate_layer

            rv = revalidate_layer(
                shot.folder, str(getattr(layer, "id", m.id)), lambda c: _builder_render(shot.folder, c)
            )
            if rv["dropped"]:
                log(
                    f"builder checks: {rv['kept']} held, {len(rv['dropped'])} dropped as "
                    f"stale (authored against a render a later attempt replaced)",
                    1,
                )
                for cid, why in rv["dropped"]:
                    log(f"  dropped {cid}: {why}", 2)
            elif rv["kept"]:
                log(f"builder checks: all {rv['kept']} still hold on the final renders", 1)
        except Exception as e:
            log(f"! builder-check revalidation skipped: {str(e)[:120]}", 1)
        from vfx_harness.orchestration.layer_plans import write_layer_outcome

        outcome = write_layer_outcome(
            shot.folder,
            layer,
            status=status,
            best=best,
            canonical=canon_verdicts,
            run_id=RUN_ID,
            attempt=ledger._slot(m).get("attempt"),
            blender_version=_blender_version(session),
        )
        log(f"layer outcome → {outcome.relative_to(shot.folder)}", 1)
    # Publishing the sealed outcome is part of completion. Marking the ledger first could
    # let a later layer advance with no feedback artifact if the outcome write failed.
    ledger.mark(m, status, best=best)
    if ok and layer is not None and publish_layer:
        abl = await _ablate(shot, layer, prior_paths, script_rel, session)
        ledger.record_ablation(m, abl)
        if abl.get("moved"):
            log("ablation: " + " · ".join(f"{k}{v:+.0%}" for k, v in abl["moved"].items()), 1)
        elif abl.get("note"):
            log(f"ablation: {abl['note']}", 1)  # never silent — a skip is a result
        if not abl["ok"]:
            log(f"! {abl['note']}")
    if ok:
        v = ledger.snapshot_scripts(m, "pass")
        if v:
            log(f"chain snapshot → {Path(v).name} (revert point)", 1)
    if ok or _ERRORS:
        # QUEUED, not run here. Distillation writes recipes for FUTURE layers; nothing
        # downstream in this run needs them, yet the run used to sit and wait for a model
        # to finish writing prose before the next layer could start. Drain the queue with
        # `python -m vfx_harness.agents.distill <shot>` after the run, or set VFXH_DISTILL_INLINE=1.
        req = {"milestone": m.id, "script_rel": script_rel if ok else None, "errors": list(_ERRORS), "run_id": RUN_ID}
        if Settings.from_environment().distill_inline:
            try:
                await distill_recipe(shot, m, verbose, script_rel=script_rel if ok else None, errors=list(_ERRORS))
            except Exception as e:
                log(f"distill skipped: {str(e)[:80]}")
        else:
            try:
                q = run_artifacts.logs_dir(shot.folder) / "distill_queue.jsonl"
                q.parent.mkdir(parents=True, exist_ok=True)
                with q.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(req) + "\n")
                log(
                    f"distillation queued ({len(_ERRORS)} error(s)) → "
                    f"{q.relative_to(shot.folder)}; drain with "
                    f"`python -m vfx_harness.agents.distill {shot.folder}`",
                    1,
                )
            except OSError as e:
                log(f"! could not queue distillation, running it inline: {e}")
                try:
                    await distill_recipe(shot, m, verbose, script_rel=script_rel if ok else None, errors=list(_ERRORS))
                except Exception as e2:
                    log(f"distill skipped: {str(e2)[:80]}")
    # One report per layer: everything that previously took six greps, plus what the
    # HOOKS did — a hook that never fires is silent by accident and invisible otherwise.
    try:
        slot = ledger._slot(m)
        attempt_cost = costlog.attempt_totals(shot.folder, run_id=RUN_ID, attempt=int(slot.get("attempt") or 0))
        rec_path = write_run(
            shot.folder,
            (report_layer or layer) if layer is not None else m,
            status=slot.get("status", "?"),
            rounds=[
                {
                    "kind": r.get("kind"),
                    "mean": r.get("mean"),
                    "pass": r.get("pass"),
                    "focus_requested": r.get("focus_requested", []),
                    "focus_panels": r.get("focus_panels", []),
                }
                for r in slot.get("rounds", [])
                if r.get("kind") != "canonical"
            ],
            canonical=[
                {
                    "frame": f,
                    "mean": v["mean"],
                    "pass": v["pass"],
                    "decided_by": v.get("decided_by", "critic"),
                    "judge_conflict": bool(v.get("judge_conflict")),
                    "contract_gap": bool(v.get("contract_gap")),
                    "contract_gaps": v.get("contract_gaps", []),
                    "evidence": v.get("evidence", []),
                    "focus_requested": v.get("focus_requested", []),
                    "focus_panels": v.get("focus_panels", []),
                    "reproduction": v.get("reproduction"),
                }
                for (f, _r), v in (canon_verdicts or [])
            ],
            ablation=slot.get("ablation", {}),
            reviews=slot.get("reviews", []),
            recipes=_RECIPES_USED,
            journal=_JOURNAL_INFO,
            cost=attempt_cost["cost_usd"],
            turns=attempt_cost["turns"],
            seconds=time.monotonic() - t_layer,
            tokens={
                "input_tokens": attempt_cost["tokens"]["input"],
                "output_tokens": attempt_cost["tokens"]["output"],
                "cache_read_input_tokens": attempt_cost["tokens"]["cache_read"],
                "cache_creation_input_tokens": attempt_cost["tokens"]["cache_create"],
            },
            approach=_APPROACH.get("text"),
            extra={
                "run_id": RUN_ID,
                "attempt": slot.get("attempt"),
                "session_id": last_info.get("session_id"),
                "cost_sessions": attempt_cost["sessions"],
                "cost_by_role": attempt_cost["by_role"],
                "models": {
                    "builder": builder_model(),
                    "script": script_model(),
                    "critic": critic_model(),
                    "reviewer": Settings.from_environment(load_dotenv_file=False).reviewer_model,
                },
                # WHICH tools the builder reached for. The four layers that passed
                # barrel_roll called compare_frame 7-41 times; the one that failed
                # three times called it 3-5 and measured 17-44 instead. Recovering
                # that took grepping a console log that no longer exists.
                "tools": tool_use_summary(
                    motion_owned=_layer_needs_motion(layer),
                    automatic_scene_checks=int(snapshot_counts().get("automatic_scene_contract_probe", 0)),
                    look_feedback_applicable=axes_own_look(axes),
                ),
            },
        )
        import json as _json

        _rec = _json.loads(rec_path.read_text())
        log("\n" + run_summary(_rec))
        # The layer's OUTCOME as the transcript's last word, so one file answers "what was
        # it asked, what did it do, what did the judge say, how did it end" without
        # joining across three artifacts.
        transcript.event(
            "layer_end",
            status=_rec.get("status"),
            layer=_rec.get("layer"),
            cost_usd=_rec.get("cost_usd"),
            turns=_rec.get("turns"),
            rounds=_rec.get("rounds"),
            canonical=_rec.get("canonical"),
            tools=_rec.get("tools"),
            hooks=_rec.get("hooks"),
            report=str(rec_path),
        )
    except Exception as e:
        log(f"! run report unavailable: {str(e)[:80]}")
    transcript.unbind()
    costlog.unbind()
    _ERRORS.clear()
    _RECIPES_USED.clear()
    _APPROACH.clear()
    return ledger


async def build_layer(
    shot: Shot,
    layer,
    session: BlenderSession,
    *,
    rounds: int = 2,
    verbose: bool = True,
    resume_ok: bool = False,
    force: bool = False,
) -> Ledger:
    """Execute one layer as its declared dependency-ordered work-unit DAG.

    Each unit receives only its own just-in-time plan, writes one independently replayable
    artifact, and is judged only on its own claims/moments.  Multi-unit layers publish the
    layer script only after every unit has sealed and the composed artifact replays cleanly.
    """
    if layer.execution == "jit_deferred":
        raise ValueError(
            f"layer {layer.id} is jit_deferred and has no executable unit DAG; "
            f"run `vfx plan {shot.folder} --layer {layer.id}` to materialize and gate it first"
        )
    from vfx_harness.domain.contracts import active_for, load_document
    from vfx_harness.domain.work_units import ready_units
    from vfx_harness.observability.provenance import atomic_write
    from vfx_harness.orchestration.layer_plans import work_unit_plan_path, write_layer_outcome
    from vfx_harness.orchestration.revalidation import digest
    from vfx_harness.orchestration.unit_state import block_dependents, freeze_checkpoint, initialize, transition
    from vfx_harness.orchestration.unit_state import load as load_unit_state

    def artifact_for(unit) -> str:
        spans = tuple(unit.mutates.script_spans)
        if len(spans) != 1:
            raise ValueError(
                f"layer {layer.id} unit {unit.id} must own exactly one replayable script span; "
                f"got {list(spans)}"
            )
        return spans[0]

    artifacts = [artifact_for(unit) for unit in layer.stages]
    if len(layer.stages) > 1:
        if len(set(artifacts)) != len(artifacts):
            raise ValueError(f"layer {layer.id} work units must own distinct script artifacts")
        if layer.script in artifacts:
            raise ValueError(
                f"layer {layer.id} reserves {layer.script} for the composed layer artifact; "
                "multi-unit stages must write distinct unit scripts"
            )

    from vfx_harness.orchestration.plan_authority import active_plan_hash, selected_artifact_path

    layers_hash = active_plan_hash(shot.folder)
    initialize(shot.folder, str(layer.id), layer.stages, plan_hash=layers_hash)
    prior_layers = _prior_layer_paths(shot, layer, force=force)
    state = load_unit_state(shot.folder, str(layer.id))
    passed_units = {
        uid
        for uid, row in (state.get("units") or {}).items()
        if row.get("status") == "passed"
        and (
            shot.folder / artifact_for(next(unit for unit in layer.stages if unit.id == uid))
        ).is_file()
    }
    unit_artifacts = [artifact_for(unit) for unit in layer.stages if unit.id in passed_units]

    # The layer-level scope remains a boundary statement; each unit adds a narrower
    # claim/property manifest and its own plan below.
    # EVERY layer is one layer of many, so every layer gets a scope block. Judging any
    # layer on the whole rubric scores it for work later layers do (BR layer G: floor on
    # emissive_finish, which layer F delivers 4 layers later — 2.83 ceiling in two
    # independent builds) and, worse, penalises elements a layer correctly REMOVED
    # (SH G50 @ f300 scored typography_legibility=0; the type hides at f197 by design).
    done = ""
    owned = (
        f"  THIS LAYER OWNS: {', '.join(layer.owns)}.\n"
        f"  The critic receives ONLY those axes and scores every one numerically."
        if layer.owns
        else "  Score only what THIS layer's scope covers."
    )
    # Name what the LATER layers deliver, by title. `owns` scopes the critic by AXIS, and
    # that is not fine-grained enough: layer 2 owns hero_tower_read, so every tower-shaped
    # thing in a wide frame counts against it — including the background city, which is
    # layer 3's job and has not been built yet. It was marked down at f440 for
    # "featureless flat-grey boxes" that were never its work. An axis can be owned and
    # still have parts of its subject produced elsewhere.
    later = [
        f"layer {g.id} ({g.title})"
        for g in sorted(load_layers(shot).values(), key=lambda g: str(g.script))
        if str(g.script) > str(layer.script)
    ]
    not_yet = (
        f"  STILL TO COME, and therefore NOT this layer's to deliver or be marked "
        f"down for: {'; '.join(later)}. Judge the SUBJECT this layer built. If a "
        f"weakness in an owned axis comes from something a later layer delivers, "
        f"do NOT treat it as a defect in the subject this layer built.\n"
        if later
        else ""
    )
    scope = (
        f"  Layer {layer.id} — {layer.title} (one build stage of many; later layers "
        f"add the rest of the look).\n  {layer.reads}\n{done}\n{owned}\n{not_yet}"
        f"  Elements that are correctly absent because another layer adds or removes "
        f"them must not depress any supplied axis."
    ).strip()
    if len(layer.judges) > 1:
        log(
            f"layer {layer.id} answers for {len(layer.judges)} frames: "
            + ", ".join(f"f{f} vs {r}" for f, r in layer.judges)
        )
    strips = plan_strips(shot)
    while len(passed_units) < len(layer.stages):
        ready = ready_units(layer.stages, passed_units)
        pending = [unit for unit in ready if unit.id not in passed_units]
        if not pending:
            raise RuntimeError(
                f"layer {layer.id} has no dependency-ready work unit; inspect logs/work_units state"
            )
        unit = pending[0]
        from vfx_harness.orchestration.plan_due import require_due_clear

        require_due_clear(shot.folder, layer=str(layer.id), unit=unit.id)
        unit_plan_path = work_unit_plan_path(shot.folder, unit)
        # A plan file's EXISTENCE is not authority: run 20260824T103842Z-afec73 failed
        # its gate and left the generated plan behind, and the next build built a unit
        # on it. Consumption requires a clean-gate attestation; anything less is treated
        # as absent and regenerated through the transactional gate-then-publish path.
        needs_plan = not unit_plan_path.is_file()
        if not needs_plan:
            from vfx_harness.orchestration.layer_plans import validate_work_unit_plan_authority

            try:
                validate_work_unit_plan_authority(shot.folder, unit_plan_path)
            except ValueError as exc:
                log(f"existing unit plan is not gated authority ({str(exc)[:160]}) — regenerating")
                needs_plan = True
        if needs_plan:
            from .planner import generate_layer_plan

            log(f"generating just-in-time plan for dependency-ready unit {layer.id}.{unit.id}")
            # generation is transactional: it publishes to the shot only through a clean
            # deterministic gate and retracts its artifacts otherwise
            await generate_layer_plan(
                shot.folder,
                str(layer.id),
                unit_id=unit.id,
                blender=Settings.from_environment().blender_bin,
            )
        unit_excerpt = _plan_layer_excerpt(shot, layer, unit)
        unit_axes = tuple(dict.fromkeys(claim.axis for claim in unit.evaluation.claims))
        unit_judges = tuple((point.frame, point.ref) for point in unit.evaluation.judges)
        unit_layer = replace(
            layer,
            script=artifact_for(unit),
            title=(layer.title if len(layer.stages) == 1 else f"{layer.title} · {unit.title}"),
            judges=unit_judges,
            reads=f"Work unit {unit.id}: " + " ".join(claim.proposition for claim in unit.evaluation.claims),
            owns=unit_axes,
            primary_judge=unit.evaluation.primary_judge,
            stages=(unit,),
        )
        unit_ref = next(ref for frame, ref in unit_judges if frame == unit.evaluation.primary_judge)
        milestone = (
            layer.as_milestone(strips)
            if len(layer.stages) == 1
            else Milestone(
                f"{layer.id}@{unit.id}",
                unit.evaluation.primary_judge,
                unit_ref,
                unit_layer.reads,
                strips.get(unit.evaluation.primary_judge, ()),
            )
        )
        current = (load_unit_state(shot.folder, str(layer.id)).get("units") or {}).get(unit.id, {})
        if current.get("status") == "blocked":
            transition(shot.folder, str(layer.id), unit.id, "planning", reason="dependency closure is now passed")
        elif current.get("status") == "pending":
            transition(shot.folder, str(layer.id), unit.id, "planning", reason="unit became dependency-ready")
        transition(shot.folder, str(layer.id), unit.id, "building", reason="builder transaction started")
        try:
            fps = {}
            try:
                from vfx_harness.orchestration.ledger import load_milestones

                fps = {m.frame: m.fingerprint for m in load_milestones(shot).values() if m.fingerprint}
            except Exception as exc:
                log(f"! no measured fingerprints in the unit contract: {str(exc)[:60]}", 1)
            context_path = write_layer_context(shot, unit_layer, load_axes(shot), fps, unit=unit)
            log(f"unit context → {context_path.relative_to(shot.folder)} (loaded every request)", 1)
            ledger = await build_unit(
                shot,
                milestone,
                artifact_for(unit),
                prior_layers + [shot.folder / rel for rel in unit_artifacts],
                session,
                rounds=rounds,
                verbose=verbose,
                plan_excerpt=unit_excerpt,
                scope=scope + f"\n  ACTIVE WORK UNIT: {unit.id} — {unit.title}. Only its claims may authorize repair.",
                layer=unit_layer,
                active_unit=unit,
                publish_layer=len(layer.stages) == 1,
                report_layer=(
                    unit_layer if len(layer.stages) == 1 else replace(unit_layer, id=f"{layer.id}.{unit.id}")
                ),
                resume_ok=resume_ok,
            )
        except Exception:
            transition(shot.folder, str(layer.id), unit.id, "failed", reason="unit build raised")
            block_dependents(
                shot.folder,
                str(layer.id),
                unit.id,
                layer.stages,
                reason=f"dependency {unit.id} failed",
            )
            raise
        unit_status = ledger.status(milestone)
        if unit_status != "passed":
            if unit_status == "contract_gap":
                try:
                    finding = _record_contract_gap_falsification(shot, layer, unit)
                    log(
                        "plan hypothesis falsified by executable evidence → "
                        f"{finding['record_id']} (transactional replan required)",
                        1,
                    )
                except (OSError, ValueError, KeyError) as exc:
                    transition(
                        shot.folder,
                        str(layer.id),
                        unit.id,
                        "failed",
                        reason="contract gap could not produce typed falsification evidence",
                        metadata={"error": str(exc)},
                    )
                    unit_status = "failed_unrecorded_plan_finding"
            elif unit_status == "failed":
                try:
                    finding = _record_bound_contract_falsification(shot, layer, unit, milestone, ledger)
                except (OSError, ValueError, KeyError) as exc:
                    transition(
                        shot.folder,
                        str(layer.id),
                        unit.id,
                        "failed",
                        reason="falsified bound contracts could not produce typed evidence",
                        metadata={"error": str(exc)},
                    )
                    unit_status = "failed_unrecorded_plan_finding"
                else:
                    if finding is None:
                        transition(
                            shot.folder,
                            str(layer.id),
                            unit.id,
                            "failed",
                            reason=unit_status,
                        )
                    else:
                        log(
                            "plan hypothesis falsified by executable evidence → "
                            f"{finding['record_id']} (transactional replan required)",
                            1,
                        )
            else:
                transition(
                    shot.folder,
                    str(layer.id),
                    unit.id,
                    "failed",
                    reason=unit_status,
                )
            block_dependents(
                shot.folder,
                str(layer.id),
                unit.id,
                layer.stages,
                reason=f"dependency {unit.id} ended {unit_status}",
            )
            return ledger

        if unit.protects.ids:
            active_ids = {
                str(row["id"])
                for name, key in (("scene_checks.json", "contracts"), ("checks.json", "checks"))
                for row in load_document(selected_artifact_path(shot.folder, name), key)
                if active_for(row, layer.id)
            }
        else:
            active_ids = {
                str(row["id"])
                for row in load_document(selected_artifact_path(shot.folder, "scene_checks.json"), "contracts")
                if active_for(row, layer.id) and int(row.get("owner_layer")) < int(layer.id)
            }
        artifact = shot.folder / artifact_for(unit)
        slot = ledger._slot(milestone)
        best_render = shot.folder / str((slot.get("best") or {}).get("render") or "")
        frozen_state = freeze_checkpoint(
            shot.folder,
            str(layer.id),
            unit,
            active_contract_ids=active_ids,
            candidate_hash=digest(best_render) or "missing",
            settings_hash=hashlib.sha256(b"eevee:0.5").hexdigest(),
            script_hash=digest(artifact) or "missing",
            input_hash=layers_hash,
        )
        transition(shot.folder, str(layer.id), unit.id, "evaluating", reason="canonical evaluation sealed")
        from vfx_harness.orchestration.plan_due import resolve_unit_completion

        passed_evidence = {
            (binding.kind, binding.id)
            for claim in unit.evaluation.claims
            if claim.required
            for binding in claim.evidence
            if binding.kind in {"scene_contract", "image_contract"}
        }
        passed_evidence.add(("replay", f"{layer.id}.{unit.id}"))
        resolve_unit_completion(
            shot.folder,
            layer=str(layer.id),
            unit=unit.id,
            passed_evidence=passed_evidence,
            checkpoint_hash=str(
                frozen_state["units"][unit.id]["checkpoint"]["candidate_hash"]
            ),
        )
        require_due_clear(
            shot.folder,
            layer=str(layer.id),
            unit=unit.id,
            completion=True,
        )
        transition(shot.folder, str(layer.id), unit.id, "passed", reason="all required unit claims passed")
        passed_units.add(unit.id)
        unit_artifacts.append(artifact_for(unit))

    if len(layer.stages) == 1 and unit_artifacts[0] == layer.script:
        return Ledger(shot)

    composed = []
    for unit in layer.stages:
        rel = artifact_for(unit)
        composed.append(f"# --- work unit {unit.id}: {rel} ---\n" + (shot.folder / rel).read_text(encoding="utf-8"))
    atomic_write(shot.folder / layer.script, "\n\n".join(composed).rstrip() + "\n")
    log(f"published composed layer artifact → {layer.script}")

    ledger = Ledger(shot)
    milestone = layer.as_milestone(strips)
    ledger._slot(milestone)["script"] = layer.script
    ledger.begin(milestone)
    composition_attempt = int(ledger._slot(milestone).get("attempt") or 0)
    costlog.bind(
        shot.folder,
        role="layer_composition",
        phase="canonical_composition",
        layer=str(layer.id),
        run_id=RUN_ID,
        attempt=composition_attempt,
        model=critic_model(),
    )
    transcript.bind(
        shot.folder,
        "build",
        label=f"layer{layer.id}-composition",
        run_id=RUN_ID,
    )
    axes = _owned_axes(await ensure_axes(shot, verbose), layer)
    canonical: list = []
    result = await _verify_script(
        shot,
        milestone,
        layer.script,
        prior_layers,
        session,
        axes,
        ledger,
        verbose,
        scope=scope,
        layer=layer,
        out_verdicts=canonical,
    )
    status = "passed" if result == "passed" else result
    best = {"round": 0, "mean": min((v.get("mean", 0) for _fr, v in canonical), default=0), "render": None}
    write_layer_outcome(
        shot.folder,
        layer,
        status=status,
        best=best,
        canonical=canonical,
        run_id=RUN_ID,
        attempt=ledger._slot(milestone).get("attempt"),
        blender_version=_blender_version(session),
    )
    ledger.mark(milestone, status, best=best)
    transcript.unbind()
    costlog.unbind()
    return ledger


def _ablation_frames(shot: Shot, layer) -> list[int]:
    """Choose frames where this layer can actually have an effect.

    Static layers use their primary judge.  A temporal layer uses every declared judge
    frame, because its rest frame is often intentionally identical before and after.
    """
    judges = [int(frame) for frame, _ref in layer.judges]
    temporal = getattr(layer, "temporal_evidence", "none")
    return sorted(set(judges)) if len(judges) > 1 and temporal in {"keyframes", "motion"} else judges[:1]


async def _ablate(shot: Shot, layer, prior_paths: list[Path], script_rel: str, session: BlenderSession) -> dict:
    """Does this layer's script actually CHANGE its judge frames?

    The pipeline already uses ablation inside a build — `warm-session-probe-loop` proved
    Glare strength was not driving halation and that DOF was a silent no-op. The same
    question one level up has never been asked: a layer can pass on axes it does not own
    (SH G60 scored 2.67 on typography and palette, neither of which is its work) while
    contributing nothing measurable at all.

    Render the frames where this department can contribute WITHOUT the layer's script,
    then WITH it, and compare. No model.
    """
    from vfx_harness.evidence.metrics import look_vector

    frames = _ablation_frames(shot, layer)
    try:
        session.run(_RESET)
        session.run(_preamble(shot))
        _run_prior_paths(session, prior_paths)
        try:
            without = {frame: look_vector(session.render(frame=frame, mode="eevee", scale=0.4)) for frame in frames}
        except Exception as e:
            # The FIRST layer has no priors, so "without it" is an empty scene with no
            # camera. That is not a skip — it is the strongest possible result: nothing
            # renders at all until this layer runs.
            if "no camera" in str(e).lower():
                session.run((shot.folder / script_rel).read_text(encoding="utf-8"))
                for frame in frames:
                    session.render(frame=frame, mode="eevee", scale=0.4)  # must now work
                return {
                    "ok": True,
                    "frames": frames,
                    "moved": {},
                    "note": "scene cannot render at all without this layer (no camera) — it establishes the spine",
                }
            raise
        session.run((shot.folder / script_rel).read_text(encoding="utf-8"))
        with_ = {frame: look_vector(session.render(frame=frame, mode="eevee", scale=0.4)) for frame in frames}
    except Exception as e:
        return {"ok": True, "note": f"ablation INCONCLUSIVE: {str(e)[:70]}"}
    moved = {}
    for frame in frames:
        for key, value in with_[frame].items():
            base = without[frame].get(key, 0.0)
            denom = max(abs(base), 1e-6)
            if abs(value - base) > max(0.02 * denom, 1e-6):
                label = key if len(frames) == 1 else f"f{frame}:{key}"
                moved[label] = round((value - base) / denom, 3)
    top = sorted(moved.items(), key=lambda kv: -abs(kv[1]))[:4]
    changed = bool(top) and max(abs(v) for _k, v in top) > 0.05
    return {
        "ok": changed,
        "moved": dict(top),
        "frames": frames,
        "note": (
            ""
            if changed
            else f"frames {frames} are essentially IDENTICAL with and without "
            f"{Path(script_rel).name} — this layer may be a no-op"
        ),
    }


def _metric_report(shot: Shot, render_rel: str, ref_rel: str) -> str:
    """Objective ref-deltas for the reviewer — technique problems show up as structural
    metrics (points, detail) rather than exposure."""
    try:
        from vfx_harness.evidence.metrics import compare, look_pair, report

        d = compare(*look_pair(str(shot.folder / render_rel), str(shot.folder / ref_rel)))
        return report(d) if d else ""
    except Exception as e:
        log(f"! metric report unavailable for the review: {str(e)[:70]}", 1)
        return ""


def _persist_contract_gaps(shot: Shot, layer, m: Milestone, render_rel: str, verdict: dict) -> None:
    """Pin a coverage defect to the exact canonical pixels and comparison boundary."""
    rows = list(verdict.get("observation_reconciliation") or [])
    if not verdict.get("contract_gap") or not any(row.get("state") == "contract_gap" for row in rows):
        return
    candidate = shot.folder / render_rel
    if not candidate.is_file():
        return
    unit_id = "acceptance"
    if layer is not None:
        active = [
            unit.id
            for unit in layer.stages
            if any(int(m.frame) in claim.moments for claim in unit.evaluation.claims)
        ]
        unit_id = active[0] if len(active) == 1 else "+".join(active) or "coverage_audit"
    settings = {
        "mode": "eevee",
        "scale": 0.5,
        "frame": int(m.frame),
        "reference": str(m.ref),
        "reference_sha256": hashlib.sha256((shot.folder / m.ref).read_bytes()).hexdigest(),
    }
    append_gap_record(
        shot.folder,
        layer=str(getattr(layer, "id", m.id)),
        unit=unit_id,
        candidate_hash=hashlib.sha256(candidate.read_bytes()).hexdigest(),
        settings_hash=hashlib.sha256(
            json.dumps(settings, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        rows=rows,
    )


def _record_contract_gap_falsification(shot: Shot, layer, unit) -> dict:
    """Promote the latest verified coverage gap into typed replan authority.

    ``contract_gap`` is narrower than a failed contract: it means a measurable observation
    has no authoritative contract binding, so repairing the scene would require authority
    the active unit does not have. Ordinary executable misses never enter this path.
    """
    from vfx_harness.domain.plan_records import load_assumptions
    from vfx_harness.observability.run_artifacts import shot_state_dir
    from vfx_harness.orchestration.layer_plans import work_unit_plan_path
    from vfx_harness.orchestration.plan_authority import resolve_current
    from vfx_harness.orchestration.unit_state import record_hypothesis_falsification

    gaps_path = shot_state_dir(shot.folder) / "contract-gaps.jsonl"
    records = []
    if gaps_path.is_file():
        for line_no, line in enumerate(gaps_path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{gaps_path}:{line_no} is invalid JSON: {exc}") from exc
            if (
                str(row.get("layer")) == str(layer.id)
                and str(row.get("unit")) in {unit.id, "coverage_audit"}
                and row.get("classification") == "plan_defect"
            ):
                records.append(row)
    if not records:
        raise ValueError(
            f"contract gap for {layer.id}.{unit.id} has no hash-pinned gap record; "
            "refusing to invent replanning evidence"
        )
    gap = records[-1]
    observations = list(gap.get("gaps") or [])
    if not observations:
        raise ValueError("latest contract-gap record has no observations")
    bundle = resolve_current(shot.folder)
    cited_contracts = sorted({
        str(contract_id)
        for finding in observations
        for contract_id in finding.get("check_ids") or []
        if str(contract_id).strip()
    })
    owner = f"{layer.id}.{unit.id}"
    decisions = [
        {"id": assumption.id, "strength": assumption.decision_strength}
        for assumption in load_assumptions(bundle.root)
        if assumption.falsification_owner == owner
        or set(assumption.falsification_contract_ids) & set(cited_contracts)
    ]
    unit_plan = work_unit_plan_path(shot.folder, unit)
    return record_hypothesis_falsification(
        shot.folder,
        str(layer.id),
        unit,
        layer.stages,
        bundle_hash=bundle.content_hash,
        unit_plan_hash=hashlib.sha256(unit_plan.read_bytes()).hexdigest(),
        candidate_hash=str(gap.get("candidate_hash")),
        settings_hash=str(gap.get("settings_hash")),
        contract_ids=cited_contracts,
        observations=observations,
        decisions=decisions,
        conflict={
            "kind": "contract",
            "required_authority": (
                "add an independently measurable claim/contract binding before any scene repair"
            ),
            "roles": list(unit.mutates.roles),
            "controls": list(unit.mutates.controls),
        },
        evidence=["state/contract-gaps.jsonl"],
    )


def _record_bound_contract_falsification(shot: Shot, layer, unit, milestone, ledger) -> dict | None:
    """Escalate terminal failing contracts that sit on a declared decision falsification path.

    Classification is by declared authority: a decision names the exact contracts that can
    falsify it, so a terminal miss on one of them can only pass by amending that decision.
    A failing contract that no decision names returns None and stays an ordinary failure —
    uncertainty does not gain plan-defect authority.
    """
    from vfx_harness.domain.plan_records import load_assumptions
    from vfx_harness.domain.unit_outcomes import falsifying_decisions
    from vfx_harness.orchestration.layer_plans import work_unit_plan_path
    from vfx_harness.orchestration.plan_authority import resolve_current
    from vfx_harness.orchestration.unit_state import record_hypothesis_falsification

    slot = ledger._slot(milestone)
    rounds = [row for row in slot.get("rounds") or [] if row.get("kind") == "canonical"]
    if not rounds:
        return None
    final = rounds[-1]
    failing = [
        dict(row)
        for row in final.get("evidence") or []
        if isinstance(row, dict) and row.get("id") and not row.get("pass")
    ]
    if not failing:
        return None
    try:
        bundle = resolve_current(shot.folder)
        assumptions = load_assumptions(bundle.root)
    except (OSError, ValueError, KeyError) as exc:
        log(f"! falsification classification skipped (unreadable selected authority): {str(exc)[:90]}", 1)
        return None
    decisions = falsifying_decisions((str(row["id"]) for row in failing), assumptions)
    if not decisions:
        return None
    listed = {cid for record in decisions for cid in record.falsification_contract_ids}
    observations = [row for row in failing if str(row["id"]) in listed]
    cited = sorted({str(row["id"]) for row in observations})
    script_rel = slot.get("script")
    if not script_rel:
        raise ValueError("terminal canonical verdict has no recorded build script")
    candidate_hash = hashlib.sha256((shot.folder / script_rel).read_bytes()).hexdigest()
    settings = {
        "frame": int(milestone.frame),
        "ref": str(milestone.ref),
        "script": str(script_rel),
        "round": final.get("round"),
        "attempt": slot.get("attempt"),
        "run_id": final.get("run_id"),
    }
    settings_hash = hashlib.sha256(
        json.dumps(settings, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    unit_plan = work_unit_plan_path(shot.folder, unit)
    evidence = [f"artifact:{script_rel}#sha256={candidate_hash}"]
    render_rel = final.get("render")
    if render_rel and (shot.folder / str(render_rel)).is_file():
        evidence.append(f"render:{render_rel}")
    return record_hypothesis_falsification(
        shot.folder,
        str(layer.id),
        unit,
        layer.stages,
        bundle_hash=bundle.content_hash,
        unit_plan_hash=hashlib.sha256(unit_plan.read_bytes()).hexdigest(),
        candidate_hash=candidate_hash,
        settings_hash=settings_hash,
        contract_ids=cited,
        observations=observations,
        decisions=[
            {"id": record.id, "strength": record.decision_strength} for record in decisions
        ],
        conflict={
            "kind": "decision",
            "required_authority": (
                "amend decision(s) "
                + ", ".join(record.id for record in decisions)
                + " through vfx units replan --falsification; the failing contracts are their "
                "declared falsification path, so passing requires authority outside this unit"
            ),
            "roles": list(unit.mutates.roles),
            "controls": list(unit.mutates.controls),
        },
        evidence=evidence,
    )


async def _verify_script(
    shot: Shot,
    m: Milestone,
    script_rel: str,
    prior_paths: list[Path],
    session: BlenderSession,
    axes: list[tuple[str, str]],
    ledger: Ledger,
    verbose: bool,
    live_best_mean: float | None = None,
    live_best_render: str | None = None,
    live_best_verdict: dict | None = None,
    scope: str | None = None,
    layer=None,
    active_unit=None,
    out_verdicts: list | None = None,
) -> str:
    """-> passed | reproduced | contract_gap | judge_conflict | failed.

    Scores EVERY frame the layer answers for, not just the primary. Iteration renders one
    frame for speed; the deliverable has to hold at all of them, and a layer is only as
    good as its worst claimed frame.
    """
    script_path = shot.folder / script_rel
    if not script_path.is_file():
        log(f"! builder never wrote {script_rel}")
        return "failed"
    log(f"verifying {script_path.name} reproduces from an empty scene…")
    try:
        session.run(_RESET)
        session.run(_preamble(shot))
        _run_prior_paths(session, prior_paths)  # deltas assume priors ran first
        before_objects = _scene_object_manifest(session)
        session.run(script_path.read_text(encoding="utf-8"))
        if active_unit is not None and active_unit.mutates.mode == "scoped":
            scope_errors = _scope_added_object_errors(
                before_objects,
                _scene_object_manifest(session),
                active_unit.mutates.roles,
            )
            if scope_errors:
                log("! scoped artifact violation: " + "; ".join(scope_errors[:6]))
                verdict = _verdict({"scores": {}, "issues": scope_errors})
                ledger.record_round(m, kind="canonical", index=0, render="", verdict=verdict)
                # A scope violation is the MOST repairable canonical failure — its text
                # names the offending objects and the allowed roles. Returning with no
                # per-frame verdicts starved the repair loop (`failed` stayed empty) and
                # runs 20260825T000404Z/022805Z each burned a manual `vfx units retry`
                # on one-line role-tag fixes. Publish the violation AS the failing
                # verdict for every judged frame so the repair loop engages.
                if out_verdicts is not None:
                    for frame, ref in (list(layer.judges) if layer is not None else [(m.frame, m.ref)]):
                        out_verdicts.append(((frame, ref), {
                            **verdict,
                            "mean": 1.0,
                            "pass": False,
                            "issues": list(scope_errors),
                        }))
                return "failed"
    except BlenderError as e:
        log(f"! build script failed: {str(e)[:200]}")
        verdict = _verdict({"scores": {}, "issues": [f"script error: {e}"]})
        ledger.record_round(m, kind="canonical", index=0, render="", verdict=verdict)
        # a script that cannot execute is equally repairable — feed the loop the error
        if out_verdicts is not None:
            for frame, ref in (list(layer.judges) if layer is not None else [(m.frame, m.ref)]):
                out_verdicts.append(((frame, ref), {
                    **verdict,
                    "mean": 1.0,
                    "pass": False,
                    "issues": [f"script error: {e}"],
                }))
        return "failed"
    judges = list(layer.judges) if layer is not None else [(m.frame, m.ref)]
    # Render serially (one Blender session), then score CONCURRENTLY — the critic calls
    # are independent judgements of already-written PNGs, and multi-frame judging tripled
    # the pass count on server_to_hansa (8 -> 18).
    shots_ = []
    for frame, ref in judges:
        if len(judges) == 1:
            m_i = m
        else:
            unit_tag = str(m.id).split("@", 1)[1] if "@" in str(m.id) and "@f" not in str(m.id) else None
            m_i = (
                Milestone(
                    f"{layer.id}@{unit_tag}",
                    frame,
                    ref,
                    m.reads,
                    plan_strips(shot).get(frame, ()),
                )
                if unit_tag
                else layer.milestone_at(frame, ref, plan_strips(shot))
            )
        render_rel = _stash_render(session, shot, m_i, f"canonical_f{frame}" if len(judges) > 1 else "canonical")
        shots_.append((frame, ref, m_i, render_rel))

    canonical_motion_evidence = None
    if shot.frontmatter.get("type") == "motion" and shot.frames > 1 and _layer_needs_motion(layer):
        try:
            canonical_motion_evidence = _stash_motion_strip(
                session,
                shot,
                m,
                f"{m.id}_canonical",
                frames_override=_layer_motion_frames(layer, m, shot.frames),
            )
        except Exception as exc:
            log(f"canonical motion strip skipped: {str(exc)[:80]}", 1)

    # A one-frame layer that already passed live has one reproduction question: did its
    # script rebuild those accepted pixels? Answer that with pixels, not another aesthetic
    # vote. Multi-frame layers still need their additional claimed frames judged because
    # live iteration only rendered the primary one.
    if len(shots_) == 1 and live_best_render and live_best_verdict:
        frame, ref, m_i, render_rel = shots_[0]
        reproduction = _image_reproduction(shot.folder / live_best_render, shot.folder / render_rel)
        if reproduction.get("match"):
            evidence = _render_evidence(
                shot, layer, m_i, render_rel, session, active_unit=active_unit
            )
            blocking = [item for item in evidence if item.get("authoritative") and not item.get("pass")]
            if blocking:
                log(
                    f"canonical reproduces live pixels, but {len(blocking)} authoritative "
                    f"check(s) fail — quality remains undecided",
                    1,
                )
            elif live_best_verdict.get("pass") or live_best_verdict.get("contract_gap"):
                is_gap = bool(live_best_verdict.get("contract_gap"))
                verdict = {
                    "scores": dict(live_best_verdict.get("scores") or {}),
                    "mean": live_best_verdict.get("mean", live_best_mean or 0.0),
                    "pass": not is_gap,
                    "issues": [],
                    "scored_axes": list(live_best_verdict.get("scored_axes") or []),
                    "na_axes": list(live_best_verdict.get("na_axes") or []),
                    "decided_by": "pixel_reproduction",
                    "reproduction": reproduction,
                    "evidence": evidence,
                    "contract_gap": is_gap,
                    "contract_gaps": list(live_best_verdict.get("contract_gaps") or []),
                    "observation_reconciliation": list(
                        live_best_verdict.get("observation_reconciliation") or []
                    ),
                }
                ledger.record_round(m, kind="canonical", index=0, render=render_rel, verdict=verdict)
                wrapped = [((frame, ref), verdict)]
                if out_verdicts is not None:
                    out_verdicts.extend(wrapped)
                log(
                    f"canonical pixels reproduce {'contract-gap' if is_gap else 'accepted'} live render "
                    f"(MAE {reproduction['mae']}, p99 {reproduction['p99']}, "
                    f">4 delta {reproduction['changed_gt4']:.2%}) — no second quality vote",
                    1,
                )
                if is_gap:
                    _persist_contract_gaps(shot, layer, m_i, render_rel, verdict)
                    return "contract_gap"
                return "reproduced"
    results: list = [None] * len(shots_)

    async def _score(i, m_i, render_rel):
        evidence = _render_evidence(
            shot, layer, m_i, render_rel, session, active_unit=active_unit
        )
        results[i] = await _judge_unit_or_layer(
            shot,
            m_i,
            render_rel,
            axes,
            session,
            verbose,
            scope,
            evidence=evidence,
            active_unit=active_unit,
            motion_evidence=canonical_motion_evidence,
            allow_motion=_layer_needs_motion(layer),
            focus_frames_override=[int(m_i.frame)],
        )

    if len(shots_) == 1:
        await _score(0, shots_[0][2], shots_[0][3])
    else:
        async with anyio.create_task_group() as tg:
            for i, (_f, _r, m_i, rr) in enumerate(shots_):
                tg.start_soon(_score, i, m_i, rr)
    verdicts = []
    for i, (frame, ref, _m_i, render_rel) in enumerate(shots_):
        v = results[i]
        _persist_contract_gaps(shot, layer, _m_i, render_rel, v)
        ledger.record_round(m, kind="canonical", index=i, render=render_rel, verdict=v)
        verdicts.append(((frame, ref), v))
    if out_verdicts is not None:
        out_verdicts.extend(verdicts)  # the run report needs the per-frame results
    if len(judges) > 1:
        log(
            "canonical per-frame: "
            + " · ".join(f"f{f}:{v['mean']}{'✅' if v['pass'] else '✗'}" for (f, _), v in verdicts),
            1,
        )
    # weakest claimed frame decides — that is the entire point of listing them
    (worst_frame, _), verdict = min(verdicts, key=lambda kv: kv[1]["mean"])
    if all(v["pass"] for _, v in verdicts):
        log(f"canonical clears every claimed frame (worst f{worst_frame} {verdict['mean']})", 1)
        return "passed"
    failures = [v for _, v in verdicts if not v.get("pass")]
    if failures and all(v.get("contract_gap") and not v.get("issues") for v in failures):
        log(
            "canonical candidate has uncovered measurable defects — recording "
            "CONTRACT_GAP for transactional replanning",
            1,
        )
        return "contract_gap"
    if failures and all(v.get("judge_conflict") for v in failures):
        log("canonical checks and critic disagree with no evidence-backed repair — recording JUDGE_CONFLICT", 1)
        return "judge_conflict"
    return "failed"


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #
async def _run(
    folder: str, layer_id: str, rounds: int, blender: str, resume_ok: bool = False, force: bool = False
) -> None:
    # Cheapest possible check, first: a credential in a variable nothing reads costs a
    # whole layer to discover otherwise, and it does not fail loudly when it happens.
    warn_if_broken()
    shot = load_shot(folder)
    # Is the plan still a plan for THIS brief? Editing brief.md leaves the plan stale with
    # nothing recording the divergence, and every layer below is then built to a spec that
    # no longer exists. An edited INPUT refuses; an edited artifact only warns, since
    # hand-tuning layers.json is a legitimate thing to do mid-build.
    from vfx_harness.orchestration.plan_authority import POINTER, resolve_current

    if (shot.folder / POINTER).exists():
        resolve_current(shot.folder)
        stale = []
    else:
        stale = provenance_check(shot.folder)
    for s in stale:
        log(f"! plan provenance: {s}")
    if any("CHANGED since the plan" in s for s in stale) and not force:
        log("   re-plan with `python -m vfx_harness.agents.planner <folder>`, or --force")
        raise SystemExit(8)

    # Questions are asked at PLAN time and must be settled BEFORE any layer runs. Building
    # on an unanswered assumption is how barrel_roll ended up 16:9 against 2:1 references
    # — by the time a later layer could notice, the camera had been committed three layers
    # earlier and every composition score was measured against the wrong crop.
    layers = load_layers(shot)
    g = layers.get(layer_id) or layers.get(layer_id.upper())
    if g is None:
        raise SystemExit(f"unknown layer {layer_id!r}; known: {', '.join(layers)}")
    from vfx_harness.orchestration.plan_due import require_due_clear

    require_due_clear(shot.folder, layer=str(g.id))
    unanswered = unanswered_for_layer(shot.folder, g)
    if unanswered and not force:
        log(
            f"✗ {len(unanswered)} unanswered question(s) from the plan — answer them "
            f"before building (or pass --force to build on the assumptions):"
        )
        for q in unanswered:
            log(f"   Q{q['id']}: {q['question']}", 1)
            log(f"        assuming: {q['assumption']}", 1)
        log(f'   answer with: python -m vfx_harness.orchestration.escalate {folder} --answer <id> "..."')
        raise SystemExit(5)
    if unanswered:
        log(f"! building with {len(unanswered)} question(s) unanswered (--force)")
    session = BlenderSession(
        blender=blender, blend_file=None, assets_dir=shot.folder / "assets", cwd=shot.folder
    ).start()
    try:
        # Name EVERY judge frame. The banner used to print only the first, while the very
        # next line said "answers for 4 frames" — and single-frame judging is precisely
        # the bug that let a blacked-out stretch of barrel_roll through, so a banner that
        # under-reports the judge list is the wrong thing to get wrong.
        judged = " · ".join(f"f{f} vs {r}" for f, r in g.judges) or "no judge frame"
        log(
            f"build agent: shot '{shot.id}' LAYER {g.id} — {g.title} "
            f"(judges: {judged}) → {g.script}, builder {builder_model()}, critic {critic_model()}"
        )
        ledger = await build_layer(shot, g, session, rounds=rounds, resume_ok=resume_ok, force=force)
        status = ledger.status(g.as_milestone())
        log(f"{g.id}: {status}  →  {ledger.path}")
        from vfx_harness.orchestration.unit_state import load as load_unit_state

        unit_state = load_unit_state(shot.folder, str(g.id))
        unpassed = [
            f"{uid}={row.get('status')}"
            for uid, row in (unit_state.get("units") or {}).items()
            if row.get("status") != "passed"
        ]
        if unpassed:
            raise BuildUnpassed(
                f"layer {g.id} did not accept every work unit: {', '.join(unpassed)}"
            )
    finally:
        session.close()
        # This was imported and never called. write_layer_context() overwrites CLAUDE.md
        # per layer, so nothing leaked BETWEEN layers — but the last layer's contract was
        # left behind in the shot folder, where any later project-scoped session would
        # silently load it as if it were current. Generated context should not outlive
        # the layer that generated it. (Only removes a file it wrote; never a hand-written
        # CLAUDE.md.)
        clear_layer_context(shot)


def main() -> None:
    load_environment()
    ap = argparse.ArgumentParser(description="Build one plan layer with the critic loop.")
    ap.add_argument("folder", help="shot folder (contains brief.md + refs/)")
    ap.add_argument("--layer", required=True, help="layer id from layers.json — 1, 2, 3 …")
    ap.add_argument("--rounds", type=int, default=2, help="max build↔critic rounds")
    ap.add_argument("--blender", default="blender", help="blender executable")
    ap.add_argument(
        "--force",
        action="store_true",
        help="override the pre-build refusals: unanswered plan questions "
        "(uses the assumptions) and unaccepted prior layers. Debugging "
        "only — anything built this way rests on unreviewed work.",
    )
    ap.add_argument(
        "--resume",
        action="store_true",
        help="continue a crashed/truncated run: restore its scene checkpoint, "
        "replay the journal, and resume the same SDK session",
    )
    args = ap.parse_args()
    shot = load_shot(args.folder)
    with run_artifacts.invocation(shot.folder, "build", shot_id=shot.id,
                                  parameters={"layer": args.layer, "rounds": args.rounds}):
        try:
            anyio.run(_run, args.folder, args.layer, args.rounds, args.blender,
                      args.resume, args.force)
        # `from None` on all three: the handler has already logged a message written for a
        # human, and the exit code carries the meaning for the driver. Chaining the original
        # traceback on top would bury both under a stack nobody needs.
        except BuildTruncated as e:
            log(f"BUILD TRUNCATED — {e}")
            raise SystemExit(3) from None
        except BuildUnpassed as e:
            log(f"BUILD UNPASSED — {e}")
            raise SystemExit(7) from None
        except ChainBroken as e:
            log(f"CHAIN BROKEN — {e}")
            raise SystemExit(4) from None
        except UnpassedPrior as e:
            log(f"UNACCEPTED PRIOR — {e}")
            raise SystemExit(6) from None


if __name__ == "__main__":
    main()
