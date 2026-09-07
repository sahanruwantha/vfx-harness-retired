"""Stage 2 — the PLAN harness."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from claude_agent_sdk import query

from vfx_harness.agents.builder.critic_focus import _image_block, _one_user_message
from vfx_harness.agents.model_stream import with_idle_deadline
from vfx_harness.agents.plan_guardrails import planner_hooks
from vfx_harness.agents.plan_tools import build_plan_tools
from vfx_harness.agents.planner.pkg import planner_package
from vfx_harness.agents.planner.rematerialize import (
    _rematerialize_layer,
)
from vfx_harness.agents.planner.types import (
    _phase_tools,
)
from vfx_harness.agents.prompts import (
    LAYER_PLANNER_ADDENDUM,
    layer_user_prompt,
)
from vfx_harness.agents.resilience import result_signal, run_session
from vfx_harness.agents.sdk_options import sdk_options
from vfx_harness.agents.unit_scope import compile_scope_with_predecessors
from vfx_harness.domain.contracts import load_document
from vfx_harness.domain.work_units import ready_units
from vfx_harness.evaluation.plan_gate import report as gate_report
from vfx_harness.evaluation.plan_gate import run as run_plan_gate
from vfx_harness.infrastructure.config import Settings
from vfx_harness.knowledge.recipe_tools import build_recipe_tools
from vfx_harness.observability import costlog, run_artifacts, transcript
from vfx_harness.observability.log import log, log_message
from vfx_harness.orchestration.authority_capsule_resolution import (
    selected_layer_capsule_digest,
)
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
from vfx_harness.orchestration.builder_execution_fence import (
    BuilderExecutionFenceLease,
    require_builder_execution_lease,
)
from vfx_harness.orchestration.layer_outcome_context import prior_outcomes_block
from vfx_harness.orchestration.layer_outcome_paths import layer_identity_segment
from vfx_harness.orchestration.layer_plans import (
    amendment_block,
    contract_gaps_block,
    is_selected_bundle_member,
    stamp_work_unit_plan,
    validate_work_unit_plan_authority,
    work_unit_plan_authority_path,
    work_unit_plan_path,
)
from vfx_harness.orchestration.ledger import load_layers
from vfx_harness.orchestration.plan_authority import prepare_consumer_view
from vfx_harness.orchestration.unit_completion_state import (
    authorize_completed_units_for_layer,
)
from vfx_harness.orchestration.unit_state import (
    authorized_passed_unit_ids,
    validate_current,
)
from vfx_harness.orchestration.unit_state import (
    load as load_unit_state,
)
from vfx_harness.orchestration.unit_state_lock import unit_state_lock
from vfx_harness.orchestration.work_unit_plan_transaction import (
    WorkUnitPlanTransactionConflict,
    work_unit_plan_transaction,
)

if TYPE_CHECKING:
    from vfx_harness.agents.builder.attempt_guard import UnitAttemptGuard

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


async def _generate_layer_plan(
    folder: str | Path,
    layer_id: str,
    *,
    unit_id: str | None = None,
    model: str | None = None,
    blender: str = "blender",
    max_turns: int = 24,
    rematerialize: tuple[str, str, list[str], bool] | None = None,
    materialize_only: bool = False,
    attempt_guard: UnitAttemptGuard | None = None,
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
        # A prior remat that crashed after publishing an intent is recovered from its
        # immutable staged transition before a new replacement begins. The candidate
        # must carry its unpublished overlay and gate-attested state effect together;
        # accepted units are not a door refusal because the transaction preserves exact
        # unchanged unit bindings (HIR-0026, HIR-0052, HIR-0171).
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
    try:
        with authority_selection_lock(shot.folder, exclusive=False):
            require_matching_authority_selection_token(
                selected_authority.selection_token,
                read_authority_selection_heads(shot.folder).token,
            )
            layers_hash = selected_layer_capsule_digest(
                shot.folder,
                str(layer.id),
                selected_authority,
            )
            with unit_state_lock(shot.folder, str(layer.id), exclusive=False):
                state = load_unit_state(shot.folder, str(layer.id))
                if not state:
                    raise ValueError(
                        "materialization publication did not create authority-bound "
                        f"work-unit state for layer {layer.id}"
                    )
                validate_current(state, str(layer.id), layer.stages)
                if state.get("plan_hash") != layers_hash:
                    raise ValueError(
                        f"layer {layer.id} work-unit state does not bind its selected "
                        "semantic authority capsule"
                    )
    except (AuthoritySelectionConflict, AuthoritySelectionHeadError) as exc:
        raise ValueError(
            "selected authority changed before layer-plan state initialization"
        ) from exc
    if materialize_only:
        return selected_authority.artifact_paths["layers.json"]
    passed = {
        uid for uid, row in (state.get("units") or {}).items() if row.get("status") == "passed"
    }
    authorized_completions = authorize_completed_units_for_layer(
        shot.folder,
        str(layer.id),
        layer.stages,
        expected_plan_hash=layers_hash,
        selected_authority=selected_authority,
    )
    sealed = authorized_passed_unit_ids(
        state,
        layer.stages,
        completion_authorization=authorized_completions,
    )
    ready = ready_units(
        layer.stages, passed, sealed_producers=sealed
    )
    if unit_id is not None:
        selected = next((unit for unit in layer.stages if unit.id == unit_id), None)
        if selected is None:
            raise KeyError(
                f"unknown unit {unit_id!r} in layer {layer.id}; available: "
                + ", ".join(unit.id for unit in layer.stages)
            )
        # A raw ``passed`` projection is not dependency authority.  Only the exact
        # digest-bound completion receipt seals a producer for its successors.
        missing = sorted(set(selected.depends_on) - sealed)
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
    if attempt_guard is None:
        raise ValueError(
            "paid unit-plan generation is builder-owned and requires an exact planning "
            "attempt; run `vfx build` instead"
        )
    if (attempt_guard.layer_id, attempt_guard.unit.id) != (
        str(layer.id),
        selected.id,
    ):
        raise ValueError(
            "unit planner attempt guard does not match the selected layer and unit"
        )
    attempt_guard.check(f"start unit plan {layer.id}.{selected.id}")
    target.parent.mkdir(parents=True, exist_ok=True)
    rel_target = target.relative_to(shot.folder).as_posix()
    feedback = "\n\n".join(
        x
        for x in (
            prior_outcomes_block(
                shot,
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
        completion_authorization=authorized_completions,
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
        unit_plan_selected_authority=selected_authority,
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
    options = sdk_options(
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
            attempt_guard=attempt_guard,
        ),
    )
    judge_names = {Path(ref).name for _frame, ref in layer.judges}
    blocks = _kickoff_blocks(
        kickoff, shot, refs=tuple(ref for ref in shot.refs if ref.name in judge_names)
    )
    async def _attempt() -> str:
        said: list[str] = []
        async for message in with_idle_deadline(
            query(prompt=_one_user_message(blocks), options=options),
            label="unit plan",
        ):
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
                attempt_guard.check(f"query unit plan {layer.id}.{selected.id}")
                await run_session(
                    _attempt,
                    succeeded=_wrote,
                    label=f"plan layer {layer.id} unit {selected.id}",
                )
                attempt_guard.check(f"complete unit plan {layer.id}.{selected.id}")
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
            with attempt_guard.hold(
                f"stamp unit plan {layer.id}.{selected.id}"
            ):
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
            with attempt_guard.hold(
                f"publish gated unit plan {layer.id}.{selected.id}"
            ):
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


async def generate_layer_plan(
    folder: str | Path,
    layer_id: str,
    *,
    unit_id: str | None = None,
    model: str | None = None,
    blender: str = "blender",
    max_turns: int = 24,
    rematerialize: tuple[str, str, list[str], bool] | None = None,
    materialize_only: bool = False,
    attempt_guard: UnitAttemptGuard | None = None,
    fence_lease: BuilderExecutionFenceLease | None = None,
) -> Path:
    """Materialize a layer or plan one claimed unit under its live builder fence."""

    if materialize_only:
        return await _generate_layer_plan(
            folder,
            layer_id,
            unit_id=unit_id,
            model=model,
            blender=blender,
            max_turns=max_turns,
            rematerialize=rematerialize,
            materialize_only=True,
            attempt_guard=attempt_guard,
        )

    require_builder_execution_lease(fence_lease, folder)
    with fence_lease.operation(folder):
        return await _generate_layer_plan(
            folder,
            layer_id,
            unit_id=unit_id,
            model=model,
            blender=blender,
            max_turns=max_turns,
            rematerialize=rematerialize,
            materialize_only=False,
            attempt_guard=attempt_guard,
        )
