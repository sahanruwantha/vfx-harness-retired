"""Phase-pure script sessions: finalize and repair one deterministic artifact.

Split from ``prior.py`` at its "Agent plumbing" boundary: prior replay stays there,
the script-agent system prompt, candidate probe server, options, and runner live here.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    create_sdk_mcp_server,
    tool,
)

from vfx_harness.agents.build_prompts import (
    capability_feedback_groups,
)
from vfx_harness.agents.builder.builder_options import _builder_options as _builder_options
from vfx_harness.agents.builder.candidate_script import (
    edit_scratch_candidate,
    write_scratch_candidate,
)
from vfx_harness.agents.builder.drain import _drain_once
from vfx_harness.agents.builder.evidence import _scope_bound_evidence
from vfx_harness.agents.builder.models import (
    _RESET,
    MAX_BUDGET_USD,
    BuildTruncated,
    script_model,
)
from vfx_harness.agents.builder.pkg import builder_package
from vfx_harness.agents.builder.prior import _run_artifact_script
from vfx_harness.agents.guardrails import builder_hooks
from vfx_harness.agents.sdk_options import sdk_options
from vfx_harness.application.preflight import model_phase_failure
from vfx_harness.blender.session import BlenderSession
from vfx_harness.blender.tools import CANNOT_EXPRESS_DESCRIPTION, CANNOT_EXPRESS_SCHEMA, record_cannot_express
from vfx_harness.domain.brief import Shot
from vfx_harness.evidence.checks import layer_evidence as image_layer_evidence
from vfx_harness.evidence.scene_checks import layer_evidence as scene_layer_evidence
from vfx_harness.knowledge.recipe_tools import build_recipe_tools
from vfx_harness.observability import transcript
from vfx_harness.observability.log import (
    TOOL_USE,
    log,
)
from vfx_harness.orchestration import generate_construction as generate_construction

if TYPE_CHECKING:
    pass


# --------------------------------------------------------------------------- #
# Agent plumbing                                                               #
# --------------------------------------------------------------------------- #
_SCRIPT_SYSTEM = """\
You are a narrow build-artifact agent. Follow the requested MODE exactly. You do not have
Blender scene tools and must not redesign the warm scene. In FINALIZE_SCRIPT, publish the
complete requested script once with write_candidate_script. In REPAIR_SCRIPT, make only
the stated local correction with edit_candidate_script and never replace the whole file. Repair binds
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


def _build_probe_candidate_server(
    shot: Shot,
    script_rel: str,
    probe_ctx: dict,
    *,
    mode: str,
):
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
    attempt_guard = probe_ctx.get("attempt_guard")
    shot_root = Path(shot.folder).resolve()
    candidate_path = shot_root / script_rel

    def _probe() -> dict:
        probe_dir = Path(probe_ctx["scratch_dir"])
        probe_dir.mkdir(parents=True, exist_ok=True)
        verify = BlenderSession(
            blender=probe_ctx["blender"], artifacts_dir=probe_dir, cwd=shot.folder
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
                    shot.folder,
                    str(probe_ctx["layer_id"]),
                    frame=int(frame),
                    session=verify,
                    selected_authority=probe_ctx.get("selected_authority"),
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
                        selected_authority=probe_ctx.get("selected_authority"),
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
    if attempt_guard is None:
        raise ValueError("candidate script tools require an exact work-unit attempt guard")
    if mode == "finalize":

        @tool(
            "write_candidate_script",
            "Publish the complete active-attempt scratch candidate. The target is fixed "
            "by the harness; this tool accepts no path and cannot write build authority.",
            {
                "type": "object",
                "properties": {"content": {"type": "string"}},
                "required": ["content"],
                "additionalProperties": False,
            },
        )
        async def write_candidate_script(args):
            content = str(args.get("content") or "")
            if not content.strip():
                return {"content": [{"type": "text", "text": "candidate script cannot be empty"}], "is_error": True}
            write_scratch_candidate(
                shot.folder,
                candidate_path,
                content,
                attempt_guard,
            )
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"wrote {len(content)} characters to the active scratch candidate",
                    }
                ]
            }

        tools.append(write_candidate_script)
        names.append("mcp__candidate__write_candidate_script")
    elif mode == "repair":

        @tool(
            "edit_candidate_script",
            "Replace one exact string in the active-attempt scratch candidate. The "
            "target is fixed by the harness; this tool accepts no path and cannot edit "
            "build authority or a sibling candidate.",
            {
                "type": "object",
                "properties": {
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                    "replace_all": {"type": "boolean"},
                },
                "required": ["old_string", "new_string"],
                "additionalProperties": False,
            },
        )
        async def edit_candidate_script(args):
            old = str(args.get("old_string") or "")
            new = str(args.get("new_string") or "")
            replace_all = bool(args.get("replace_all", False))
            if not old:
                return {"content": [{"type": "text", "text": "old_string cannot be empty"}], "is_error": True}

            changed = edit_scratch_candidate(
                shot.folder,
                candidate_path,
                old,
                new,
                replace_all=replace_all,
                attempt_guard=attempt_guard,
            )
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"edited {changed} occurrence(s) in the active scratch candidate",
                    }
                ]
            }

        tools.append(edit_candidate_script)
        names.append("mcp__candidate__edit_candidate_script")
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
        server, probe_tools = _build_probe_candidate_server(
            shot,
            script_rel,
            ctx,
            mode=mode,
        )
        mcp_servers["candidate"] = server
    if not finalize:
        # repairs design mechanisms; the cookbook's harness lessons (rig aim ownership,
        # slotted actions, …) are exactly the knowledge blind repairs lacked

        recipe_server, recipe_names = build_recipe_tools()
        mcp_servers["recipes"] = recipe_server
        probe_tools = [*probe_tools, *recipe_names]
    return sdk_options(
        model=script_model(),
        system_prompt=_SCRIPT_SYSTEM,
        cwd=str(shot.folder),
        hooks=builder_hooks(
            shot.folder,
            [shot.folder],
            script_rel=script_rel,
            phase=phase,
            selected_authority=(probe_ctx or {}).get("selected_authority"),
            attempt_guard=(probe_ctx or {}).get("attempt_guard"),
        ),
        mcp_servers=mcp_servers,
        allowed_tools=[*(["Read", "Glob"] if finalize else ["Read", "Grep"]), *probe_tools],
        disallowed_tools=[
            "Write",
            "Edit",
            "Bash",
            "WebFetch",
            "WebSearch",
            "Task",
            "Agent",
            "NotebookEdit",
            *([] if finalize else ["Glob"]),
        ],
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
    attempt_guard = (probe_ctx or {}).get("attempt_guard")
    if attempt_guard is not None:
        attempt_guard.check(f"start {mode} script agent")
    async with ClaudeSDKClient(
        options=_script_options(shot, mode=mode, script_rel=script_rel, probe_ctx=probe_ctx)
    ) as agent:
        tools_before = sum(TOOL_USE.values())
        if attempt_guard is not None:
            attempt_guard.check(f"query {mode} script agent")
        # The SDK never echoes the input: an unrecorded kickoff cannot be audited when a
        # session degrades (run 20260902T165518Z-004470's finalizers were diagnosed from
        # the run log because their prompts were absent from the build transcript).
        transcript.prompt(
            prompt,
            role="kickoff",
            mode=f"{mode.upper()}_SCRIPT",
            model=script_model(),
            script=script_rel,
        )
        await agent.query(prompt)
        info = await _drain_once(agent, verbose)
        if attempt_guard is not None:
            attempt_guard.check(f"complete {mode} script agent")
        if info["subtype"] == "error_max_turns":
            log(
                f"⏸ {mode} agent hit its turn checkpoint ({info['turns']} turns) — "
                "continuing once to finish the in-progress artifact operation",
                1,
            )
            continuation_tools_before = sum(TOOL_USE.values())
            continuation_prior_cost = float(info.get("cost") or 0.0)
            if attempt_guard is not None:
                attempt_guard.check(f"continue {mode} script agent")
            continuation = (
                f"MODE remains {mode.upper()}_SCRIPT. Continue from the exact file state "
                "you just left. Do not discover more files or broaden the repair. Finish "
                f"the smallest necessary operation on `{script_rel}`, summarize it, and stop."
            )
            transcript.prompt(
                continuation,
                role="continuation",
                mode=f"{mode.upper()}_SCRIPT",
                model=script_model(),
                script=script_rel,
            )
            await agent.query(continuation)
            info = await _drain_once(agent, verbose)
            if attempt_guard is not None:
                attempt_guard.check(f"complete {mode} script continuation")
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
