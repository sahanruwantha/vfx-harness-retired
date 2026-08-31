"""Stage 3 — the build + critic loop for one PLAN LAYER."""

from __future__ import annotations

import json
import re
from pathlib import Path

import anyio
from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    create_sdk_mcp_server,
    tool,
)

from vfx_harness.agents.build_prompts import (
    builder_system,
    capability_feedback_groups,
)
from vfx_harness.agents.builder.drain import _drain_once
from vfx_harness.agents.builder.evidence import _scope_bound_evidence
from vfx_harness.agents.builder.models import (
    _RESET,
    MAX_BUDGET_USD,
    MAX_TURNS,
    TASK_BUDGET_TOKENS,
    BuildTruncated,
    UnpassedPrior,
    builder_model,
    script_model,
)
from vfx_harness.agents.builder.pkg import builder_package
from vfx_harness.agents.guardrails import builder_hooks
from vfx_harness.application.preflight import model_phase_failure
from vfx_harness.blender.session import BlenderError, BlenderSession
from vfx_harness.blender.tools import CANNOT_EXPRESS_DESCRIPTION, CANNOT_EXPRESS_SCHEMA, record_cannot_express
from vfx_harness.domain.brief import Shot
from vfx_harness.evidence.checks import layer_evidence as image_layer_evidence
from vfx_harness.evidence.scene_checks import layer_evidence as scene_layer_evidence
from vfx_harness.knowledge.recipes import RECIPES_DIR, build_recipe_tools, recipe_index
from vfx_harness.observability import transcript
from vfx_harness.observability.log import (
    TOOL_USE,
    log,
)
from vfx_harness.orchestration import generate_construction as generate_construction
from vfx_harness.orchestration.layer_plans import read_layer_plan, read_work_unit_plan
from vfx_harness.orchestration.ledger import Ledger, load_layers


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


_ARTIFACT_EVALUATION_BARRIER = (
    "import bpy\n"
    "_vfx_scene=bpy.context.scene\n"
    "_vfx_scene.frame_set(int(_vfx_scene.frame_current))\n"
    "bpy.context.view_layer.update()\n"
)


def _run_artifact_script(
    session: BlenderSession, path: Path, *, journal: bool = True
) -> dict:
    """Replay one artifact and publish its evaluated state to the next consumer.

    A successful Python execution is not yet a Blender dependency-graph boundary.
    Successors may legally consume producer world transforms immediately, so every
    artifact replay ends with an unjournalled current-frame evaluation (HIR-0117).
    """
    generate_construction.pin_for_script(session, path)
    result = session.run(path.read_text(encoding="utf-8"), journal=journal)
    session.run(_ARTIFACT_EVALUATION_BARRIER, journal=False)
    return result


def _run_prior_paths(session: BlenderSession, paths: list[Path]) -> list[str]:
    """Replay the accepted chain. A failure here is NOT this layer's fault: layer scripts
    reference each other's objects by name (30_purple.py does D.objects['tower_dot']
    from 20_green.py), so re-running an early layer can invalidate every later one and
    the break only surfaces now. Say so plainly instead of leaking a raw bpy KeyError."""
    names = []
    for p in paths:
        log(f"running prior layer script {p.name}")
        try:
            _run_artifact_script(session, p)
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
Blender scene tools and must not redesign the warm scene. In FINALIZE_SCRIPT, publish the
complete requested script once with Write and never Edit it. In REPAIR_SCRIPT, make only
the stated local correction with Edit and never replace the whole file. Repair binds
`cannot_express_in_scope` on the candidate server — call it when no in-scope edit can
satisfy the failing ids; ToolSearch for a blender tool will miss it. Read only the named
script, journal, plan, and verdict evidence needed for that operation. Your working
directory is already the shot folder: use every named relative path verbatim. Never prefix
a path with the repository root or guess an alternative location.
"""


def probe_preview_modes(look_capabilities) -> tuple[str, ...]:
    """Solid is geometry. A look-owning unit also gets a draft beauty plate (HIR-0042)."""
    if capability_feedback_groups(look_capabilities or ()):
        return ("solid", "draft")
    return ("solid",)


def _build_probe_candidate_server(shot: Shot, script_rel: str, probe_ctx: dict):
    """One tool that lets a script session SEE the scene its artifact rebuilds.

    Run 20260824T103842Z-afec73's canonical repairs reasoned soundly from text findings
    alone and rewrote a correct script into one whose camera faced away from the set at
    every frame — the rebuilt consequences of an edit were invisible to the session
    editing it. probe_candidate rebuilds the CURRENT artifact in a disposable worker and
    returns the authoritative evidence rows, evaluated camera/role world transforms at
    the judge frames, and a small solid render per frame."""

    calls = 0
    comparison_state = probe_ctx.get("comparison_state")
    raster_required = bool(probe_ctx.get("raster_required", True))

    def _probe() -> dict:
        probe_dir = Path(probe_ctx["scratch_dir"])
        probe_dir.mkdir(parents=True, exist_ok=True)
        verify = BlenderSession(
            blender=probe_ctx["blender"], artifacts_dir=probe_dir, cwd=None
        ).start()
        try:
            verify.run(_RESET, journal=False)
            verify.run(builder_package()._preamble(shot), journal=False)
            for prior in probe_ctx["prior_paths"]:
                _run_artifact_script(verify, Path(prior), journal=False)
            before_objects = builder_package()._scene_object_manifest(verify)
            _run_artifact_script(
                verify, shot.folder / script_rel, journal=False
            )
            scope_errors = builder_package()._candidate_scope_errors(
                str(probe_ctx.get("scope_mode") or ""),
                tuple(str(role) for role in (probe_ctx.get("roles") or [])),
                before_objects,
                builder_package()._scene_object_manifest(verify),
            )
            if scope_errors:
                raise ValueError("scoped artifact violation: " + "; ".join(scope_errors[:6]))
            try:
                rig_contract = verify.check(kind="rig_contract")
            except Exception as exc:
                rig_contract = {"ok": None, "issues": [f"check failed: {str(exc)[:120]}"]}
            role_patterns = list(probe_ctx.get("roles") or [])
            preview_modes = (
                probe_preview_modes(probe_ctx.get("look_capabilities") or ())
                if raster_required
                else ()
            )
            frames_out = []
            for frame, ref in probe_ctx["judges"]:
                rows = scene_layer_evidence(
                    shot.folder, str(probe_ctx["layer_id"]), frame=int(frame), session=verify
                )
                image_render = None
                image_rows = []
                if raster_required:
                    image_render = verify.render(frame=int(frame), mode="eevee", scale=0.5)
                    image_rows = image_layer_evidence(
                        shot.folder,
                        str(probe_ctx["layer_id"]),
                        frame=int(frame),
                        ref=str(ref),
                        render=image_render,
                        stage=str(probe_ctx.get("image_stage") or "pre_grade"),
                    )
                evidence_ids_by_frame = probe_ctx.get("evidence_ids_by_frame")
                allowed_evidence_ids = (
                    None
                    if evidence_ids_by_frame is None
                    else {
                        str(item)
                        for item in evidence_ids_by_frame.get(str(int(frame)), ())
                    }
                )
                scoped_rows = _scope_bound_evidence(
                    [*rows, *image_rows], allowed_evidence_ids
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
                frame_row = {
                    "frame": int(frame),
                    "ref": str(ref),
                    "camera": transforms.get("camera"),
                    "roles": transforms.get("roles"),
                    "evidence": [
                        {
                            key: row.get(key)
                            for key in ("id", "kind", "value", "target", "pass", "error", "note")
                            if row.get(key) not in (None, "")
                        }
                        for row in scoped_rows
                    ],
                }
                if raster_required:
                    try:
                        render = verify.render(frame=int(frame), mode="solid", scale=0.33)
                    except Exception as exc:  # a render failure is a finding, not a crash
                        render = f"render failed: {str(exc)[:120]}"
                    frame_row["solid_render"] = render
                    frame_row["image_contract_render"] = image_render
                else:
                    frame_row["raster_required"] = False
                    frame_row["raster_note"] = (
                        "typed executable scene/interface unit; candidate replay owes "
                        "no image or visual-critic evidence"
                    )
                if "draft" in preview_modes:
                    try:
                        look = verify.render(frame=int(frame), mode="draft", scale=0.33)
                    except Exception as exc:
                        look = f"render failed: {str(exc)[:120]}"
                    frame_row["look_render"] = look
                    frame_row["look_render_note"] = (
                        "draft EEVEE beauty — the critic's domain. solid_render is "
                        "Workbench geometry and is not the look plate; do not hide "
                        "occluders because they read as slats in solid"
                    )
                frames_out.append(frame_row)
            return {"script": script_rel, "rig_contract": rig_contract, "frames": frames_out}
        finally:
            verify.close()

    @tool(
        "probe_candidate",
        f"Rebuild the CURRENT `{script_rel}` from an empty scene in a disposable worker "
        "and return, per judge frame: the authoritative evidence rows the gate will "
        "compute and the evaluated camera and role world transforms. When typed evidence "
        "requires raster it also returns a solid-mode geometry render and, for declared "
        "look capabilities, a draft EEVEE look plate (`look_render`). Executable-only "
        "scene/interface units return `raster_required: false` instead of images. "
        "Diagnose look against look_render, not solid_render. "
        "Call it BEFORE diagnosing and AFTER editing — an edit whose rebuilt "
        "consequences you have not seen is a guess. Deterministic, no model cost; "
        "capped at three calls.",
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

    tools = [probe_candidate]
    names = ["mcp__candidate__probe_candidate"]
    if comparison_state is not None:

        @tool(
            "cannot_express_in_scope",
            CANNOT_EXPRESS_DESCRIPTION,
            CANNOT_EXPRESS_SCHEMA,
        )
        async def cannot_express_in_scope(args):
            return record_cannot_express(comparison_state, args)

        tools.append(cannot_express_in_scope)
        names.append("mcp__candidate__cannot_express_in_scope")
    server = create_sdk_mcp_server(name="candidate", version="0.1.0", tools=tools)
    return server, names


def _script_options(
    shot: Shot, *, mode: str, script_rel: str, probe_ctx: dict | None = None
) -> ClaudeAgentOptions:
    finalize = mode == "finalize"
    phase = {"mode": mode}
    mcp_servers = {}
    probe_tools: list[str] = []
    if probe_ctx is not None:
        ctx = probe_ctx
        if finalize:
            ctx = {key: value for key, value in probe_ctx.items() if key != "comparison_state"}
        server, probe_tools = _build_probe_candidate_server(shot, script_rel, ctx)
        mcp_servers["candidate"] = server
    if not finalize:
        # repairs design mechanisms; the cookbook's harness lessons (rig aim ownership,
        # slotted actions, …) are exactly the knowledge blind repairs lacked

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
        tools_before = sum(TOOL_USE.values())
        await agent.query(prompt)
        info = await _drain_once(agent, verbose)
        if info["subtype"] == "error_max_turns":
            log(
                f"⏸ {mode} agent hit its turn checkpoint ({info['turns']} turns) — "
                "continuing once to finish the in-progress artifact operation",
                1,
            )
            continuation_tools_before = sum(TOOL_USE.values())
            continuation_prior_cost = float(info.get("cost") or 0.0)
            await agent.query(
                f"MODE remains {mode.upper()}_SCRIPT. Continue from the exact file state "
                "you just left. Do not discover more files or broaden the repair. Finish "
                f"the smallest necessary operation on `{script_rel}`, summarize it, and stop."
            )
            info = await _drain_once(agent, verbose)
            continuation_why = model_phase_failure(
                info,
                sum(TOOL_USE.values()) - continuation_tools_before,
                prior_cost=continuation_prior_cost,
            )
            if continuation_why:
                transcript.event(
                    "empty_success",
                    phase=f"{mode}_continuation",
                    why=continuation_why,
                    **info,
                )
                raise BuildTruncated(
                    f"{mode} continuation: {continuation_why}",
                    terminal_cause="model_session_failure",
                )
        why = model_phase_failure(
            info,
            sum(TOOL_USE.values()) - tools_before,
            prior_cost=0.0,
        )
        if why:
            transcript.event("empty_success", phase=mode, why=why, **info)
            raise BuildTruncated(
                f"{mode} phase: {why}", terminal_cause="model_session_failure"
            )
        return info
