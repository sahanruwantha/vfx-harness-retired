"""Stage 3 — the build + critic loop for one PLAN LAYER."""

from __future__ import annotations

import json
from dataclasses import dataclass
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
    builder_system,
    capability_feedback_groups,
)
from vfx_harness.agents.builder.candidate_script import (
    edit_scratch_candidate,
    write_scratch_candidate,
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
from vfx_harness.infrastructure.trusted_files import (
    TrustedFileBinding,
    TrustedFileError,
    read_trusted_file,
    require_trusted_file_unchanged,
)
from vfx_harness.knowledge.recipes import RECIPES_DIR, build_recipe_tools, recipe_index
from vfx_harness.observability import transcript
from vfx_harness.observability.log import (
    TOOL_USE,
    log,
)
from vfx_harness.orchestration import generate_construction as generate_construction
from vfx_harness.orchestration import unit_state
from vfx_harness.orchestration.layer_plans import read_layer_plan, read_work_unit_plan
from vfx_harness.orchestration.ledger import Ledger
from vfx_harness.orchestration.selected_layer_chain import selected_layer_chain
from vfx_harness.orchestration.unit_completion_state import (
    current_completion_receipt_digests,
)
from vfx_harness.orchestration.unit_evaluation_receipts import (
    ExecutedReplayDependency,
    ExecutedReplayInput,
)

if TYPE_CHECKING:
    from vfx_harness.orchestration.authority_selection import ResolvedSelectedAuthority


def _prior_layer_paths(
    shot: Shot,
    layer,
    *,
    force: bool = False,
    selected_authority: ResolvedSelectedAuthority | None = None,
) -> list[Path]:
    """Return the exact accepted selected-DAG prefix before this layer.

    Raises UnpassedPrior unless every one of them is recorded 'passed'."""

    chain = selected_layer_chain(
        shot,
        selected_authority=selected_authority,
    )
    matches = [index for index, candidate in enumerate(chain) if candidate.id == layer.id]
    if len(matches) != 1 or chain[matches[0]] != layer:
        raise ValueError(
            f"layer {getattr(layer, 'id', None)!r} is not the exact selected-DAG layer"
        )
    prior_layers = chain[: matches[0]]
    # A replay prefix without readable acceptance authority is not a degraded mode.
    # Propagate malformed/missing selected-layer or ledger state before any prior bytes
    # can reach Blender; otherwise an unaccepted artifact becomes the successor's base.
    ledger = Ledger(shot, selected_authority=selected_authority)
    keep: list[Path] = []
    unpassed: list[str] = []
    with current_completion_receipt_digests(shot.folder) as verified_receipts:
        for g in prior_layers:
            p = shot.folder / g.script
            if not p.is_file():
                unpassed.append(f"layer {g.id} ({g.script}) has no replay script")
                continue
            keep.append(p)
            try:
                state = unit_state.load(shot.folder, str(g.id))
                unit_state.validate_current(state, str(g.id), g.stages)
            except ValueError as exc:
                unpassed.append(f"layer {g.id} work-unit state is invalid: {exc}")
                continue
            sealed = unit_state.digest_matched_passed(state, g.stages)
            missing_units = [
                unit.id
                for unit in g.stages
                if unit.id not in sealed
                or (str(g.id), unit.id) not in verified_receipts
            ]
            if missing_units:
                unpassed.append(
                    f"layer {g.id} has no source-verified completion for "
                    + ", ".join(missing_units)
                )
                continue
            st = ledger.status(g.as_milestone())
            if st != "passed":
                unpassed.append(f"layer {g.id} ({p.name}) is '{st}'")
                continue
            # 'passed' is a verdict on a SCRIPT, not on a layer id. Editing the
            # composed script afterwards leaves the pass describing obsolete bytes.
            why = ledger.stale(g.as_milestone())
            if why:
                unpassed.append(why)
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


@dataclass(frozen=True, slots=True)
class PreparedArtifactReplayInput:
    """Exact descriptor-read source bytes plus their canonical replay locator."""

    executed: ExecutedReplayInput
    source_path: Path
    payload: bytes
    construction: generate_construction.PreparedConstructionReplayInput | None = None
    construction_prepared: bool = False


def require_prepared_artifact_replay_input_unchanged(
    prepared: PreparedArtifactReplayInput,
) -> None:
    """Retain script plus optional pointer/GLB identity through publication."""

    try:
        require_trusted_file_unchanged(
            prepared.executed.source_binding,
            "canonical artifact replay input",
        )
        if prepared.construction is not None:
            generate_construction.require_prepared_construction_replay_current(
                prepared.construction
            )
    except (TrustedFileError, generate_construction.GenerateConstructionError) as exc:
        raise BlenderError(str(exc)) from exc


def _prepare_artifact_replay_inputs(
    shot_root: str | Path,
    entries: list[tuple[str, Path]],
) -> tuple[PreparedArtifactReplayInput, ...]:
    """Read the complete ordered replay prefix before any byte reaches Blender."""

    root = Path(shot_root).expanduser().absolute()
    prepared: list[PreparedArtifactReplayInput] = []
    for index, (locator, source_path) in enumerate(entries):
        try:
            snapshot = read_trusted_file(
                root,
                source_path,
                f"canonical artifact replay input {index}",
                require_nonempty=True,
            )
        except TrustedFileError as exc:
            raise BlenderError(str(exc)) from exc
        try:
            construction = generate_construction.prepare_construction_replay_input(
                root,
                source_path,
            )
        except generate_construction.GenerateConstructionError as exc:
            raise BlenderError(str(exc)) from exc
        dependencies = (
            ()
            if construction is None
            else tuple(
                ExecutedReplayDependency(
                    kind=dependency.kind,
                    path=dependency.path,
                    sha256=dependency.sha256,
                    source_binding=dependency.binding,
                )
                for dependency in construction.dependencies
            )
        )
        prepared.append(
            PreparedArtifactReplayInput(
                executed=ExecutedReplayInput(
                    script_path=locator,
                    script_sha256=snapshot.sha256,
                    source_binding=snapshot.binding,
                    dependencies=dependencies,
                ),
                source_path=snapshot.binding.path,
                payload=snapshot.payload,
                construction=construction,
                construction_prepared=True,
            )
        )
    for item in prepared:
        require_prepared_artifact_replay_input_unchanged(item)
    return tuple(prepared)


def _run_artifact_script(
    session: BlenderSession,
    path: Path,
    prepared_input: PreparedArtifactReplayInput | None = None,
    *,
    journal: bool = True,
) -> dict:
    """Replay one artifact and publish its evaluated state to the next consumer.

    A successful Python execution is not yet a Blender dependency-graph boundary.
    Successors may legally consume producer world transforms immediately, so every
    artifact replay ends with an unjournalled current-frame evaluation (HIR-0117).
    """
    source: str
    source_binding: TrustedFileBinding | None = None
    if prepared_input is None:
        source = path.read_text(encoding="utf-8")
    else:
        expected = Path(path).expanduser().absolute()
        if prepared_input.source_path != expected:
            raise BlenderError(
                "prepared artifact replay input belongs to another source path: "
                f"expected {expected}, found {prepared_input.source_path}"
            )
        source_binding = prepared_input.executed.source_binding
        try:
            require_trusted_file_unchanged(
                source_binding,
                "canonical artifact replay input",
            )
        except TrustedFileError as exc:
            raise BlenderError(str(exc)) from exc
        try:
            source = prepared_input.payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BlenderError(f"artifact replay script is not UTF-8: {path}") from exc
    if prepared_input is not None and prepared_input.construction_prepared:
        try:
            generate_construction.pin_prepared_construction_replay(
                session,
                prepared_input.construction,
            )
        except generate_construction.GenerateConstructionError as exc:
            raise BlenderError(str(exc)) from exc
    else:
        generate_construction.pin_for_script(session, path)
    result = session.run(
        source,
        journal=journal,
        execution_policy="artifact",
    )
    session.run(_ARTIFACT_EVALUATION_BARRIER, journal=False)
    if source_binding is not None:
        require_prepared_artifact_replay_input_unchanged(prepared_input)
    return result


def _run_prior_paths(
    session: BlenderSession,
    paths: list[Path],
    prepared_inputs: tuple[PreparedArtifactReplayInput, ...] | None = None,
) -> list[str]:
    """Replay the accepted chain. A failure here is NOT this layer's fault: layer scripts
    reference each other's objects by name (30_purple.py does D.objects['tower_dot']
    from 20_green.py), so re-running an early layer can invalidate every later one and
    the break only surfaces now. Say so plainly instead of leaking a raw bpy KeyError."""
    if prepared_inputs is not None and len(prepared_inputs) != len(paths):
        raise ChainBroken(
            "canonical prior replay input count does not match the selected prefix"
        )
    names = []
    for index, p in enumerate(paths):
        log(f"running prior layer script {p.name}")
        try:
            _run_artifact_script(
                session,
                p,
                None if prepared_inputs is None else prepared_inputs[index],
            )
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


def _plan_layer_excerpt(
    shot: Shot,
    layer,
    unit=None,
    *,
    selected_authority: ResolvedSelectedAuthority | None = None,
) -> str:
    """The layer's just-in-time execution plan; giant-plan fallback is forbidden."""

    if unit is not None:
        return read_work_unit_plan(
            shot.folder,
            layer,
            unit,
            selected_authority=selected_authority,
        )
    return read_layer_plan(
        shot.folder,
        layer,
        selected_authority=selected_authority,
    )


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
    selected_authority: ResolvedSelectedAuthority | None = None,
    attempt_guard=None,
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
            shot.folder,
            [shot.folder, RECIPES_DIR],
            ref_rel=ref_rel,
            script_rel=script_rel,
            phase=phase,
            selected_authority=selected_authority,
            attempt_guard=attempt_guard,
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
    return ClaudeAgentOptions(
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
            await agent.query(
                f"MODE remains {mode.upper()}_SCRIPT. Continue from the exact file state "
                "you just left. Do not discover more files or broaden the repair. Finish "
                f"the smallest necessary operation on `{script_rel}`, summarize it, and stop."
            )
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
