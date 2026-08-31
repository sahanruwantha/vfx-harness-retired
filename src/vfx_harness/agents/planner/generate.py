"""Stage 2 — the PLAN harness."""

from __future__ import annotations

import hashlib
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, query

from vfx_harness.agents.builder.critic_focus import _image_block, _one_user_message
from vfx_harness.agents.plan_guardrails import planner_hooks
from vfx_harness.agents.plan_tools import build_plan_tools
from vfx_harness.agents.planner.kickoff import (
    _with_target_feedback,
    mapping_expander,
)
from vfx_harness.agents.planner.pkg import planner_package
from vfx_harness.agents.planner.rematerialize import (
    _reconcile_materialized_layer_state,
    _rematerialize_layer,
)
from vfx_harness.agents.planner.types import (
    _phase_tools,
    plan_role_capabilities,
)
from vfx_harness.agents.prompts import (
    LAYER_PLANNER_ADDENDUM,
    PLANNER_SYSTEM,
    REPAIR_ADDENDUM,
    VERIFIER_ADDENDUM,
    layer_user_prompt,
    planner_user_prompt,
    repair_user_prompt,
    verifier_user_prompt,
)
from vfx_harness.agents.resilience import result_signal, run_session
from vfx_harness.agents.unit_scope import compile_scope_with_predecessors
from vfx_harness.domain.contracts import load_document
from vfx_harness.domain.work_units import ready_units
from vfx_harness.evaluation.plan_gate import report as gate_report
from vfx_harness.evaluation.plan_gate import run as run_plan_gate
from vfx_harness.infrastructure.config import Settings
from vfx_harness.knowledge.recipes import build_recipe_tools
from vfx_harness.observability import costlog, run_artifacts, transcript
from vfx_harness.observability.log import log, log_message
from vfx_harness.orchestration.authority_selection import resolve_selected_authority
from vfx_harness.orchestration.authority_selection_heads import (
    AuthoritySelectionHeadError,
    read_authority_selection_heads,
)
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionConflict,
    authority_selection_lock,
    require_matching_authority_selection_token,
)
from vfx_harness.orchestration.layer_outcome_paths import layer_identity_segment
from vfx_harness.orchestration.layer_plans import (
    amendment_block,
    contract_gaps_block,
    global_plan_path,
    is_selected_bundle_member,
    prior_outcomes_block,
    stamp_work_unit_plan,
    validate_work_unit_plan_authority,
    work_unit_plan_authority_path,
    work_unit_plan_path,
)
from vfx_harness.orchestration.ledger import load_layers
from vfx_harness.orchestration.plan_authoring import clause_registry, registry_prompt_block
from vfx_harness.orchestration.plan_authority import prepare_consumer_view, prepare_staging
from vfx_harness.orchestration.unit_state import digest_matched_passed
from vfx_harness.orchestration.unit_state import initialize as initialize_unit_state
from vfx_harness.orchestration.work_unit_plan_transaction import (
    WorkUnitPlanTransactionConflict,
    work_unit_plan_transaction,
)

_KICKOFF_MAX_PX = 1568  # same budget the critic uses; ~1600 tokens per still


def _kickoff_blocks(text: str, shot, *, refs=None) -> list[dict]:
    """Kickoff prose plus the explicitly due reference stills, in shot order."""

    blocks: list[dict] = [{"type": "text", "text": text}]
    for p in shot.refs if refs is None else refs:
        try:
            blocks.append(_image_block(p, _KICKOFF_MAX_PX))
        except Exception as e:  # a corrupt plate must not cost the whole pass
            log(f"! could not attach {p.name}: {str(e)[:120]}", 1)
    return blocks


async def generate_plan(
    folder: str | Path,
    *,
    model: str | None = None,
    blender: str = "blender",
    max_turns: int = 100,
    tag: str | None = None,
    verify_draft: str | None = None,
    repair: tuple[str, int] | None = None,
    workspace: str | Path | None = None,
) -> Path:
    """Run ONE global planning session. With `tag`, outputs are isolated:
    plans/global.md → plans/global.<tag>.md, lab artifacts → active run scratch/plan-lab/.
    With `verify_draft`, the session runs in VERIFY MODE against that draft file.
    With `repair=(findings, round)`, it runs in REPAIR MODE against `verify_draft`."""
    source_shot = planner_package().load_shot(folder)
    layout = run_artifacts.ensure(source_shot.folder, command="plan")
    if workspace is None:

        workspace = prepare_staging(layout)
    workspace = Path(workspace).resolve()
    shot = planner_package().load_shot(workspace)
    model = model or Settings.from_environment(load_dotenv_file=False).planner_model
    plan_path = global_plan_path(shot.folder)
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    lab_dir = layout.scratch / "plan-lab" / (tag or "global")


    registry = clause_registry(workspace / "brief.md")
    mapping_path = workspace / "ownership_mapping.json"

    if repair:
        findings, rnd = repair
        system = PLANNER_SYSTEM + REPAIR_ADDENDUM.format(draft=verify_draft, findings=findings)
        kickoff = repair_user_prompt(shot, verify_draft, rnd)
        mode = f"REPAIR round {rnd} (against {verify_draft})"
        role = "repair"
    elif verify_draft:
        system = PLANNER_SYSTEM + VERIFIER_ADDENDUM.format(draft=verify_draft)
        kickoff = verifier_user_prompt(shot, verify_draft)
        mode = f"VERIFY (auditing {verify_draft})"
        role = "verify"
    else:
        system = PLANNER_SYSTEM
        kickoff = planner_user_prompt(shot, registry_prompt_block(registry))
        mode = "PLAN (from scratch)"
        role = "draft"

    _expand_mapping_or_errors = mapping_expander(workspace, registry, mapping_path)

    capabilities = plan_role_capabilities(role)
    pserver, pnames = build_plan_tools(
        shot.folder,
        blender=blender,
        lab_dir=lab_dir,
        include_gate=capabilities.include_gate,
        run_layout=layout,
        enabled_tools=frozenset({"ask_supervisor", "run_gate"}),
    )
    # Every global role authors the same transaction and therefore needs the same patch and
    # validation verbs. Repair additionally loses delegation so a bounded mechanical patch
    # cannot escape into an agent that lacks its exact context or tools.
    options = ClaudeAgentOptions(
        model=model,
        system_prompt=system,
        cwd=str(shot.folder),
        mcp_servers={"plan": pserver},
        allowed_tools=[
            "Read", "Glob", "Grep", "Write", *sorted(capabilities.allowed_tools),
            *_phase_tools(pnames, "ask_supervisor", "run_gate"),
        ],
        disallowed_tools=sorted(capabilities.denied_tools),
        permission_mode="bypassPermissions",
        max_buffer_size=32 * 1024 * 1024,  # sheets/frames as base64 image blocks
        setting_sources=[],  # isolate from user/project settings
        max_turns=max_turns,
        effort="high",
        hooks=_with_target_feedback(
            planner_hooks(
                workspace,
                readable_files=(verify_draft,) if repair and verify_draft else (),
                # The mapping is the ONLY model-authored surface; every published
                # artifact is machine-expanded from it, so other writes are denied
                # rather than merely discouraged.
                writable_files=(mapping_path,),
            ),
            mapping_path,
            _expand_mapping_or_errors,
        ),
    )

    stills = [p.name for p in shot.refs]
    videos = sorted(p.name for p in (shot.folder / "refs").glob("*.mp4"))
    log(
        f"plan agent [{mode}]: shot '{shot.id}' ({shot.frames}f @ {shot.fps}fps, "
        f"{shot.engine}), model {model}" + (f", tag '{tag}'" if tag else "")
    )
    log(f"refs: {len(stills)} stills {stills} + {len(videos)} videos {videos}", 1)
    log(
        f"workspace: {workspace.relative_to(source_shot.folder)}/ · "
        f"lab: {lab_dir.relative_to(source_shot.folder)}/ · "
        f"global tools: ownership, escalation, and deterministic gate only · max_turns {max_turns}",
        1,
    )

    costlog.bind(source_shot.folder, role="plan:" + mode.split()[0].lower(), model=model, tag=tag)
    tpath = transcript.bind(source_shot.folder, "plan", label=tag or mode)
    if tpath:
        log(f"transcript → {tpath.relative_to(source_shot.folder)}", 1)
    transcript.prompt(
        kickoff, role="kickoff", mode=mode, model=model, tag=tag, refs=stills, videos=videos, max_turns=max_turns
    )
    # Attaching every future approval frame recreated whole-shot visual preproduction.
    # Ready-unit references are requested explicitly after the sparse DAG exists.
    blocks = _kickoff_blocks(kickoff, shot, refs=())
    log("kickoff: reference stills available on demand; none attached globally", 1)
    # The post-condition, not the absence of an exception. Two repair rounds were lost to a
    # session that raised "error result: success" at $0.0007 having written nothing, and the
    # real cause ("Repeated 529 Overloaded errors") was only in its assistant text — which is
    # why one attempt collects that text and hands it to the classifier.
    before = plan_path.stat().st_mtime_ns if plan_path.is_file() else -1

    async def _attempt() -> str:
        said: list[str] = []
        async for message in query(prompt=_one_user_message(blocks), options=options):
            log_message(message)
            for blk in getattr(message, "content", None) or []:
                text = getattr(blk, "text", None)
                if text:
                    said.append(text)
            if signal := result_signal(message):
                said.append(signal)
        return "\n".join(said)[-4000:]

    def _wrote() -> bool:
        return plan_path.is_file() and plan_path.stat().st_mtime_ns != before

    try:
        await run_session(_attempt, succeeded=_wrote, label=f"plan {mode}")
    except Exception as e:
        log(f"! plan session died: {str(e)[:200]}")
        transcript.event("died", error=str(e)[:2000])
        raise
    finally:
        transcript.unbind()
        costlog.unbind()
    if tag:
        final = plan_path.with_name(f"global.{tag}.md")
        plan_path.rename(final)
        plan_path = final
    lines = plan_path.read_text(encoding="utf-8").count("\n")
    log(f"plan written: {plan_path.relative_to(shot.folder)} ({lines} lines)")
    return plan_path


async def generate_layer_plan(
    folder: str | Path,
    layer_id: str,
    *,
    unit_id: str | None = None,
    model: str | None = None,
    blender: str = "blender",
    max_turns: int = 24,
    rematerialize: tuple[str, str, list[str], bool] | None = None,
) -> Path:
    """Generate one work-unit plan after its declared dependencies have sealed outcomes.

    This is intentionally a separate session and output contract. It cannot mutate the
    global plan or machine contracts, and there is no monolithic-plan fallback.
    """
    shot = planner_package().load_shot(folder)
    model = model or Settings.from_environment(load_dotenv_file=False).planner_model
    selected_authority = resolve_selected_authority(shot.folder)
    if selected_authority.plan is None:
        raise ValueError("layer planning requires selected global plan authority")
    global_path = selected_authority.artifact_paths["global.md"]
    if not global_path.is_file():
        raise FileNotFoundError(f"{global_path} missing — generate and gate the strict global plan first")
    layers = load_layers(
        shot,
        replacing_layer_id=str(layer_id) if rematerialize is not None else None,
        selected_authority=selected_authority,
    )
    try:
        layer = layers[str(layer_id)]
    except KeyError as exc:
        raise KeyError(f"unknown layer {layer_id!r}; available: {', '.join(layers)}") from exc
    if rematerialize is not None:
        # A prior remat that selected a hole then crashed leaves this layer
        # jit_deferred in the live view. The replacement still needs the unpublished
        # overlay and apply_replan — unpacking a 4-tuple as 3 and skipping unit-state
        # movement is how remat5 would publish then die (HIR-0026). Accepted units
        # are not a door refuse; apply_replan preserves matching digests (HIR-0052).
        layer = await _rematerialize_layer(
            shot, layer, rematerialize, model=model, blender=blender, max_turns=max_turns
        )
    elif layer.execution == "jit_deferred":
        await planner_package()._materialize_deferred_layer(
            shot,
            layer,
            model=model,
            blender=blender,
            max_turns=max_turns,
            selected_authority=selected_authority,
        )

    # Materialization is an authority transition, so the selected snapshot after that
    # boundary is the sole base for unit state, plan context, and terminal gating.
    selected_authority = resolve_selected_authority(shot.folder)
    if selected_authority.plan is None:
        raise ValueError("layer planning lost selected global plan authority")
    layers = load_layers(
        shot,
        selected_authority=selected_authority,
    )
    layer = layers[str(layer_id)]
    layers_hash = hashlib.sha256(
        selected_authority.artifact_paths["layers.json"].read_bytes()
    ).hexdigest()
    try:
        with authority_selection_lock(shot.folder, exclusive=False):
            require_matching_authority_selection_token(
                selected_authority.selection_token,
                read_authority_selection_heads(shot.folder).token,
            )
            _reconcile_materialized_layer_state(
                shot,
                layer,
                new_plan_hash=layers_hash,
            )
            state = initialize_unit_state(
                shot.folder,
                str(layer.id),
                layer.stages,
                plan_hash=layers_hash,
            )
    except (AuthoritySelectionConflict, AuthoritySelectionHeadError) as exc:
        raise ValueError(
            "selected authority changed before layer-plan state initialization"
        ) from exc
    passed = {
        uid for uid, row in (state.get("units") or {}).items() if row.get("status") == "passed"
    }
    ready = ready_units(
        layer.stages, passed, sealed_producers=digest_matched_passed(state, layer.stages)
    )
    if unit_id is not None:
        selected = next((unit for unit in layer.stages if unit.id == unit_id), None)
        if selected is None:
            raise KeyError(
                f"unknown unit {unit_id!r} in layer {layer.id}; available: "
                + ", ".join(unit.id for unit in layer.stages)
            )
        missing = sorted(set(selected.depends_on) - passed)
        if missing:
            raise ValueError(
                f"layer {layer.id} unit {selected.id} is blocked by unpassed dependencies: "
                + ", ".join(missing)
            )
    else:
        if not ready:
            raise ValueError(
                f"layer {layer.id} has no plannable unit; all units passed or dependencies are blocked"
            )
        selected = ready[0]
    target = work_unit_plan_path(
        shot.folder,
        selected,
        selected_authority=selected_authority,
    )
    if is_selected_bundle_member(
        shot.folder,
        target,
        selected_authority=selected_authority,
    ):
        validate_work_unit_plan_authority(
            shot.folder,
            target,
            selected_authority=selected_authority,
        )
        log(
            f"unit plan already frozen in selected bundle; model-free reuse: "
            f"{target.relative_to(shot.folder)}"
        )
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    rel_target = target.relative_to(shot.folder).as_posix()
    feedback = "\n\n".join(
        x
        for x in (
            prior_outcomes_block(
                shot.folder,
                str(layer.id),
                selected_authority=selected_authority,
            ),
            amendment_block(shot.folder, str(layer.id)),
            contract_gaps_block(shot.folder, str(layer.id), selected.id),
        )
        if x
    )
    # Do not carry the global planner's monolithic output contract into a layer session.
    # The layer doctrine is intentionally self-contained and much smaller.
    system = LAYER_PLANNER_ADDENDUM.format(
        layer_id=layer.id,
        layer_title=layer.title,
        unit_id=selected.id,
        unit_title=selected.title,
        target=rel_target,
    )

    contract_rows = load_document(
        selected_authority.artifact_paths["scene_checks.json"],
        "contracts",
    )
    unit_card = compile_scope_with_predecessors(
        unit=selected,
        layer_id=str(layer.id),
        contracts=contract_rows,
        units=layer.stages,
        durable_state=state,
        helpers=(),
    )
    predecessor_cards = list(unit_card.get("predecessor_interfaces") or [])
    kickoff = layer_user_prompt(
        shot,
        layer,
        selected,
        rel_target,
        feedback,
        unit_card=unit_card,
        predecessor_cards=predecessor_cards,
    )
    layout = run_artifacts.ensure(shot.folder, command="plan-layer")
    lab_dir = layout.scratch / "plan-lab" / layer_identity_segment(str(layer.id))
    pserver, pnames = build_plan_tools(
        shot.folder,
        blender=blender,
        lab_dir=lab_dir,
        enabled_tools=frozenset({
            "measure_ref",
            "spike",
            "ask_supervisor",
            "gate_preview",
            "publish_unit_plan",
        }),
        unit_plan_target=target,
    )
    rserver, rnames = build_recipe_tools()
    unit_plan_tools = _phase_tools(
        pnames,
        "measure_ref",
        "spike",
        "ask_supervisor",
        "gate_preview",
        "publish_unit_plan",
    )
    options = ClaudeAgentOptions(
        model=model,
        system_prompt=system,
        cwd=str(shot.folder),
        mcp_servers={"plan": pserver, "recipes": rserver},
        allowed_tools=[*unit_plan_tools, *rnames],
        disallowed_tools=["Bash", "Edit", "Write"],
        permission_mode="bypassPermissions",
        max_buffer_size=32 * 1024 * 1024,
        setting_sources=[],
        max_turns=max_turns,
        effort="high",
        hooks=planner_hooks(
            shot.folder,
            writable_files=(target,),
            strict_reads=True,
            completion_gate=False,
        ),
    )
    judge_names = {Path(ref).name for _frame, ref in layer.judges}
    blocks = _kickoff_blocks(
        kickoff, shot, refs=tuple(ref for ref in shot.refs if ref.name in judge_names)
    )
    async def _attempt() -> str:
        said: list[str] = []
        async for message in query(prompt=_one_user_message(blocks), options=options):
            log_message(message)
            for blk in getattr(message, "content", None) or []:
                if text := getattr(blk, "text", None):
                    said.append(text)
            if signal := result_signal(message):
                said.append(signal)
        return "\n".join(said)[-4000:]

    # Materialization is a transaction: the shot may keep this plan ONLY if the
    # deterministic gate accepts the resulting consumer view. Run 20260824T103842Z-afec73
    # wrote its generated plan, failed the gate in the caller, and left the file behind —
    # the next build trusted its existence and built a unit on gate-failed authority.

    authority_path = work_unit_plan_authority_path(target)
    async with work_unit_plan_transaction(target, authority_path) as plan_transaction:
        before = target.stat().st_mtime_ns if target.is_file() else -1

        def _wrote() -> bool:
            return target.is_file() and target.stat().st_mtime_ns != before

        costlog.bind(shot.folder, role="plan:layer", model=model, tag=str(layer.id))
        tpath = transcript.bind(shot.folder, "plan", label=f"layer-{layer.id}")
        if tpath:
            log(f"transcript → {tpath.relative_to(shot.folder)}", 1)
        transcript.prompt(
            kickoff,
            role="kickoff",
            mode="PLAN_LAYER",
            model=model,
            layer=layer.id,
            refs=[p.name for p in shot.refs],
        )
        try:
            try:
                log(
                    f"plan agent [LAYER {layer.id} · UNIT {selected.id}]: "
                    f"{selected.title} → {rel_target}"
                )
                await run_session(
                    _attempt,
                    succeeded=_wrote,
                    label=f"plan layer {layer.id} unit {selected.id}",
                )
            finally:
                # The MCP write is complete at this boundary.  Capture its exact bytes
                # even when the model session terminates abnormally so a legal rollback
                # can retract only this attempt.
                try:
                    plan_transaction.claim_current()
                finally:
                    try:
                        transcript.unbind()
                    finally:
                        costlog.unbind()

            text = target.read_text(encoding="utf-8")
            if len(text.strip()) < 200:
                raise ValueError(f"{target} is too small to be an executable layer plan")
            if text.count("\n") + 1 > 160:
                raise ValueError(
                    f"{target} has {text.count(chr(10)) + 1} lines; layer plans are capped at 160. "
                    "Keep evidence in machine contracts/outcomes and rewrite this as an "
                    "execution index"
                )
            # Integrity stamp first — the gate validates it, then a clean result earns
            # the gate attestation consumers require.  Each phase proves that the pair
            # still has the exact revision owned by this attempt.
            with authority_selection_lock(shot.folder, exclusive=False):
                require_matching_authority_selection_token(
                    selected_authority.selection_token,
                    read_authority_selection_heads(shot.folder).token,
                )
                plan_transaction.require_owned_current()
                stamp_work_unit_plan(
                    shot.folder,
                    target,
                    selected_authority=selected_authority,
                )
                plan_transaction.claim_current()

            gated = run_plan_gate(
                prepare_consumer_view(
                    layout,
                    selected_authority=selected_authority,
                )
            )
            # Scoped: another layer's stuck STATE belongs to that layer's own transaction
            # and must not block this layer's plan (run bwng97m5n: layer 1's amendment died
            # on layer 2's 'no ready unit' finding).
            if not gated.clean_for(str(layer.id)):
                raise RuntimeError(
                    f"generated unit plan {layer.id}.{selected.id} failed the "
                    "deterministic gate:\n" + gate_report(gated)
                )
            with authority_selection_lock(shot.folder, exclusive=False):
                require_matching_authority_selection_token(
                    selected_authority.selection_token,
                    read_authority_selection_heads(shot.folder).token,
                )
                plan_transaction.require_owned_current()
                stamp_work_unit_plan(
                    shot.folder,
                    target,
                    gate={"clean": True, "blocking": 0, "run_id": layout.run_id},
                    selected_authority=selected_authority,
                )
                plan_transaction.claim_current()
        except BaseException as exc:
            try:
                plan_transaction.rollback()
            except WorkUnitPlanTransactionConflict as rollback_exc:
                log(
                    f"unit plan rollback refused: {rel_target} was replaced by a "
                    "newer writer"
                )
                raise rollback_exc from exc
            log(f"unit plan retracted: {rel_target} did not pass the deterministic gate")
            raise
    log(f"unit plan published through a clean gate: {rel_target} ({text.count(chr(10))} lines)")
    return target
