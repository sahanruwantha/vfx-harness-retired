"""Stage 3 — the build + critic loop for one PLAN LAYER."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path

from vfx_harness.agents.builder.attempt_guard import (
    AttemptBoundBlenderSession,
    UnitAttemptGuard,
)
from vfx_harness.agents.builder.authority import (
    AuthorityBoundLedger,
    commit_selected_authority,
    require_selected_authority_unchanged,
)
from vfx_harness.agents.builder.axes import _owned_axes, ensure_axes
from vfx_harness.agents.builder.composition_helpers import (
    compose_unit_artifact_source,
)
from vfx_harness.agents.builder.composition_helpers import (
    judgment_payment_evidence_digest as _judgment_payment_evidence_digest,
)
from vfx_harness.agents.builder.evidence import (
    _unit_raster_mode,
    _unit_requires_raster,
)
from vfx_harness.agents.builder.execution_guard import ExecutionAuthorityLost
from vfx_harness.agents.builder.falsify import (
    _prepare_composed_contract_gap_falsification,
)
from vfx_harness.agents.builder.judgment_payment import (
    JudgmentDebtPayment,
    JudgmentDebtPaymentPolicy,
)
from vfx_harness.agents.builder.layer_artifact import (
    commit_layer_artifact,
    discard_layer_artifact,
    prepare_layer_artifact,
    proposed_layer_artifact_sha256,
)
from vfx_harness.agents.builder.layer_composition_finalization import (
    _canonical_receipt_rows as _canonical_receipt_rows,
)
from vfx_harness.agents.builder.layer_composition_finalization import (
    _finalization_predecessor_inputs as _finalization_predecessor_inputs,
)
from vfx_harness.agents.builder.layer_composition_finalization import (
    _json_ready as _json_ready,
)
from vfx_harness.agents.builder.layer_composition_finalization import (
    finalize_composed_layer,
)
from vfx_harness.agents.builder.layer_finalization_guard import (
    LayerFinalizationAuthorityLost,
    LayerFinalizationClaimGuard,
)
from vfx_harness.agents.builder.layer_finalization_reconcile import (
    reconcile_layer_finalization,
)
from vfx_harness.agents.builder.models import (
    _RESET,
    BuildAuthorityDefect,
    UnclaimableUnit,
    critic_model,
)
from vfx_harness.agents.builder.pkg import builder_package
from vfx_harness.agents.builder.prior import (
    _ARTIFACT_EVALUATION_BARRIER,
    _plan_layer_excerpt,
    _prior_layer_paths,
    _run_artifact_script,
)
from vfx_harness.agents.builder.provisional_judgment import (
    _composition_judge_unit,
    _load_provisional_decisions,
    composed_group_plans,
)
from vfx_harness.agents.builder.revalidate import _blender_version
from vfx_harness.agents.builder.unit_completion import (
    complete_and_resolve_unit,
    resolve_completed_unit,
)
from vfx_harness.agents.builder.unit_context import (
    _active_unit_layer_view as _active_unit_layer_view,
)
from vfx_harness.agents.builder.unit_context import (
    _unit_artifact_path as _unit_artifact_path,
)
from vfx_harness.agents.builder.unit_context import write_attempt_bound_unit_context
from vfx_harness.agents.builder.unit_failure import handle_unpassed_unit
from vfx_harness.agents.builder.verify import _verify_script
from vfx_harness.agents.planner.pkg import planner_package
from vfx_harness.agents.shot_context import clear_layer_context, write_layer_context
from vfx_harness.blender.session import BlenderSession
from vfx_harness.domain.brief import Shot
from vfx_harness.domain.contracts import active_for, load_document
from vfx_harness.domain.judgment_debt_replay_receipts import (
    payment_generation_for_replay,
)
from vfx_harness.domain.layer_finalizations import (
    LAYER_FINALIZATION_PROJECTION_SCHEMA,
    LayerEvaluationReceipt,
    LayerFinalizationPredecessorInput,
    LayerFinalizationReceipt,
    LayerReplayClaimRequirement,
    LayerReplayEvaluationGroupPlan,
    LayerReplayObservation,
    LayerReplayPointObservation,
    LayerReplayReceipt,
    LayerReplayReceiptBinding,
)
from vfx_harness.domain.unit_completion_receipts import UnitCompletionReceipt
from vfx_harness.domain.work_units import dependency_ordered_units, layer_active_visible_fraction_ids
from vfx_harness.evaluation.plan_gate import _builder_render
from vfx_harness.evidence.checks import (
    discard_layer_revalidation,
    prepare_layer_revalidation,
)
from vfx_harness.evidence.layer_revalidation_projection import (
    layer_revalidation_projection,
)
from vfx_harness.evidence.metrics import look_vector
from vfx_harness.observability import costlog, transcript
from vfx_harness.observability.log import (
    log,
)
from vfx_harness.observability.runid import RUN_ID
from vfx_harness.orchestration.authority_capsule_resolution import (
    selected_layer_capsule_digest,
)
from vfx_harness.orchestration.authority_selection import (
    ResolvedSelectedAuthority,
    resolve_selected_authority,
)
from vfx_harness.orchestration.builder_execution_fence import (
    BuilderExecutionFenceLease,
    builder_execution_fence,
    require_builder_execution_lease,
)
from vfx_harness.orchestration.judgment_debt_state import (
    current_judgment_debt_states_for_authority,
    replay_prefix_receipt,
    require_replay_inputs_unchanged,
)
from vfx_harness.orchestration.layer_evaluation_receipts import (
    commit_layer_evaluation_receipt,
    discard_layer_evaluation_receipt,
    prepare_layer_evaluation_receipt,
)
from vfx_harness.orchestration.layer_finalization_state import (
    claim_layer_finalization,
    complete_layer_finalization,
    current_layer_finalization_receipt,
)
from vfx_harness.orchestration.layer_plans import (
    build_layer_outcome_projection,
    validate_work_unit_plan_authority,
    work_unit_plan_path,
)
from vfx_harness.orchestration.layer_replay_receipts import (
    commit_layer_replay_receipt,
    discard_layer_replay_receipt,
    prepare_layer_replay_receipt,
)
from vfx_harness.orchestration.ledger import Ledger, Milestone, load_axes, load_layers, load_milestones, plan_strips
from vfx_harness.orchestration.plan_due import require_due_clear
from vfx_harness.orchestration.revalidation import digest
from vfx_harness.orchestration.unit_completion_state import (
    authorize_completed_units_for_layer,
)
from vfx_harness.orchestration.unit_replay_inputs import prepare_replay_inputs
from vfx_harness.orchestration.unit_state import (
    freeze_checkpoint,
    ready_from_durable_state,
    transition,
    unclaimable_state,
    unresolved_falsification,
)
from vfx_harness.orchestration.unit_state import load as load_unit_state
from vfx_harness.orchestration.unit_state_claims import (
    claim_ready_unit_for_build,
    claim_ready_unit_for_planning,
)
from vfx_harness.orchestration.unit_state_storage import now as state_now

__all__ = [
    "LAYER_FINALIZATION_PROJECTION_SCHEMA",
    "AttemptBoundBlenderSession",
    "AuthorityBoundLedger",
    "JudgmentDebtPayment",
    "JudgmentDebtPaymentPolicy",
    "LayerEvaluationReceipt",
    "LayerFinalizationAuthorityLost",
    "LayerFinalizationClaimGuard",
    "LayerFinalizationPredecessorInput",
    "LayerFinalizationReceipt",
    "LayerReplayClaimRequirement",
    "LayerReplayEvaluationGroupPlan",
    "LayerReplayObservation",
    "LayerReplayPointObservation",
    "LayerReplayReceipt",
    "LayerReplayReceiptBinding",
    "_ablate",
    "_ablation_frames",
    "_blender_version",
    "_builder_render",
    "_canonical_receipt_rows",
    "_compose_unit_artifact_source",
    "_composition_judge_unit",
    "_finalization_predecessor_inputs",
    "_json_ready",
    "_judgment_payment_evidence_digest",
    "_load_provisional_decisions",
    "_owned_axes",
    "_prepare_composed_contract_gap_falsification",
    "_unit_raster_mode",
    "_unit_requires_raster",
    "_verify_script",
    "build_layer",
    "build_layer_already_fenced",
    "build_layer_outcome_projection",
    "claim_layer_finalization",
    "commit_layer_artifact",
    "commit_layer_evaluation_receipt",
    "commit_layer_replay_receipt",
    "complete_layer_finalization",
    "composed_group_plans",
    "costlog",
    "critic_model",
    "current_judgment_debt_states_for_authority",
    "discard_layer_artifact",
    "discard_layer_evaluation_receipt",
    "discard_layer_replay_receipt",
    "discard_layer_revalidation",
    "ensure_axes",
    "layer_revalidation_projection",
    "payment_generation_for_replay",
    "prepare_layer_artifact",
    "prepare_layer_evaluation_receipt",
    "prepare_layer_replay_receipt",
    "prepare_layer_revalidation",
    "prepare_replay_inputs",
    "proposed_layer_artifact_sha256",
    "replay_prefix_receipt",
    "require_replay_inputs_unchanged",
    "state_now",
    "transcript",
]


async def _build_layer_under_execution_fence(
    shot: Shot,
    layer,
    session: BlenderSession,
    *,
    rounds: int = 2,
    verbose: bool = True,
    resume_ok: bool = False,
    force: bool = False,
    unit_builder: Callable[..., Awaitable[Ledger]] | None = None,
    selected_authority: ResolvedSelectedAuthority | None = None,
    fence_lease: BuilderExecutionFenceLease,
) -> Ledger:
    """Execute one layer as its declared dependency-ordered work-unit DAG.

    Each unit receives only its own just-in-time plan, writes one independently replayable
    artifact, and is judged only on its own claims/moments.  Multi-unit layers publish the
    layer script only after every unit has sealed and the composed artifact replays cleanly.
    """
    selected_authority = selected_authority or resolve_selected_authority(shot.folder)
    selected_layers = load_layers(shot, selected_authority=selected_authority)
    selected_layer = selected_layers.get(str(layer.id))
    if selected_layer is None or selected_layer != layer:
        raise ValueError(
            f"supplied layer {getattr(layer, 'id', None)!r} does not exactly match the selected authority snapshot"
        )
    layer = selected_layer

    def publish(operation, mutation):
        return commit_selected_authority(
            shot.folder,
            selected_authority,
            operation=operation,
            mutation=mutation,
        )

    def selected_artifact(name: str) -> Path:
        if selected_authority.plan is None:
            return shot.folder / name
        try:
            return selected_authority.artifact_paths[name]
        except KeyError as exc:
            raise ValueError(f"selected authority omits {name}") from exc

    if layer.execution == "jit_deferred":
        raise ValueError(
            f"layer {layer.id} is jit_deferred and has no executable unit DAG; "
            f"run `vfx plan {shot.folder} --layer {layer.id}` to materialize and gate it first"
        )

    ordered_units = dependency_ordered_units(layer.stages)
    artifacts = [_unit_artifact_path(layer, unit) for unit in ordered_units]
    if len(set(artifacts)) != len(artifacts):
        raise ValueError(f"layer {layer.id} work units must own distinct script artifacts")
    if layer.script in artifacts:
        raise ValueError(
            f"layer {layer.id} reserves {layer.script} for the composed layer artifact; "
            "every work unit must use its identity-derived unit script"
        )
    layers_hash = selected_layer_capsule_digest(
        shot.folder,
        str(layer.id),
        selected_authority,
    )
    prior_layers = _prior_layer_paths(
        shot,
        layer,
        force=force,
        selected_authority=selected_authority,
    )
    state = load_unit_state(shot.folder, str(layer.id))
    passed_units: set[str] = set()
    for completed in ordered_units:
        slot = (state.get("units") or {}).get(completed.id, {})
        if slot.get("status") != "passed":
            continue
        receipt = UnitCompletionReceipt.parse(
            slot.get("completion_receipt"),
            f"work-unit state {layer.id}.{completed.id}.completion_receipt",
        )
        resolve_completed_unit(
            shot.folder,
            str(layer.id),
            completed,
            layer.stages,
            receipt,
            expected_plan_hash=layers_hash,
            selected_authority=selected_authority,
        )
        passed_units.add(completed.id)
    completion_authorization = authorize_completed_units_for_layer(
        shot.folder,
        str(layer.id),
        layer.stages,
        expected_plan_hash=layers_hash,
        selected_authority=selected_authority,
    )
    authorized_unit_ids = (
        completion_authorization.unit_ids
        if completion_authorization is not None
        else frozenset()
    )
    if passed_units != authorized_unit_ids:
        raise ValueError(
            f"layer {layer.id} passed-unit set changed during receipt authorization"
        )
    unit_artifacts = [_unit_artifact_path(layer, unit) for unit in ordered_units if unit.id in passed_units]
    strips = plan_strips(shot, selected_authority)
    existing_finalization = current_layer_finalization_receipt(
        shot.folder,
        str(layer.id),
    )
    if existing_finalization is not None:
        reconciled = reconcile_layer_finalization(
            shot,
            layer,
            existing_finalization,
            selected_authority=selected_authority,
            strips=strips,
        )
        if reconciled.revalidation["dropped"]:
            log(
                f"builder checks: {reconciled.revalidation['kept']} held, "
                f"{len(reconciled.revalidation['dropped'])} dropped as stale",
                1,
            )
        log(
            "reconciled terminal layer finalization without Blender or critic spend "
            f"→ {reconciled.outcome.relative_to(shot.folder)}",
            1,
        )
        if reconciled.finding is not None:
            raise BuildAuthorityDefect(
                reconciled.finding,
                stage="composition",
                exit_code=9,
                legacy_detail=(
                    f"layer {layer.id} units passed but terminal composition is {existing_finalization.final_status!r}"
                ),
            )
        return reconciled.ledger

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
        for g in sorted(selected_layers.values(), key=lambda g: str(g.script))
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
    while len(passed_units) < len(layer.stages):
        ready = ready_from_durable_state(
            shot.folder,
            str(layer.id),
            layer.stages,
            eligible_passed=passed_units,
            completion_authorization=completion_authorization,
        )
        pending = [unit for unit in ready if unit.id not in passed_units]
        if not pending:
            raise RuntimeError(f"layer {layer.id} has no dependency-ready work unit; inspect logs/work_units state")
        unit = pending[0]

        # A falsified unit is not waiting for a retry: its only legal successor is
        # `superseded`, which a reviewed authority transaction publishes. The claim
        # below correctly refuses it, but refusing with a traceback from a boundary
        # that holds the finding wastes the one thing the operator needs (HIR-0214).
        blocking_finding = unresolved_falsification(shot.folder, str(layer.id), unit.id)
        if blocking_finding is not None:
            raise BuildAuthorityDefect(
                blocking_finding,
                stage="builder",
                exit_code=7,
                legacy_detail=(
                    f"layer {layer.id} unit {unit.id} is hypothesis_falsified by "
                    f"{blocking_finding.get('record_id')} and no builder may claim it. "
                    "Its only legal successor is `superseded`, published by a reviewed "
                    "authority transaction: vfx plan <shot> --layer <owning layer> "
                    "--rematerialize --owner <you> --trigger <why> --evidence "
                    f"state/hypothesis-falsifications/{blocking_finding.get('record_id')}.json"
                    " (add --discard-accepted when the layer holds passed units). "
                    "`vfx units retry` does not accept this state."
                ),
            )

        # The same boundary, one state over. An operator interrupt — including one
        # taken to honour a budget ceiling — leaves the active unit in `building`, and
        # the claim below refuses it with a traceback while holding the status, the
        # legal states, and the transaction that clears it. HIR-0214 fixed this for
        # `hypothesis_falsified` and this path never received it.
        blocking_state = unclaimable_state(shot.folder, str(layer.id), unit.id)
        if blocking_state is not None:
            status, next_action = blocking_state
            raise UnclaimableUnit(
                f"layer {layer.id} unit {unit.id} is {status!r} and no builder may "
                f"claim it. {next_action}"
            )

        require_due_clear(
            shot.folder,
            layer=str(layer.id),
            unit=unit.id,
            selected_authority=selected_authority,
        )
        # The typed claim transaction owns selection-SH -> state-EX itself.  Wrapping
        # it in ``commit_selected_authority`` would open the selection lock through a
        # second descriptor and deadlock against this process's outer flock.
        planning_claim = claim_ready_unit_for_planning(
            shot.folder,
            str(layer.id),
            unit.id,
            layer.stages,
            expected_plan_hash=layers_hash,
            eligible_passed=passed_units,
            completion_authorization=completion_authorization,
            run_id=RUN_ID,
            selection_token=selected_authority.selection_token,
            reason="unit selected from the dependency-ready durable state",
        )
        planning_guard = UnitAttemptGuard.bind(
            shot.folder,
            str(layer.id),
            unit,
            layer.stages,
            planning_claim,
            expected_plan_hash=layers_hash,
            selected_authority=selected_authority,
        )
        unit_plan_path = work_unit_plan_path(
            shot.folder,
            unit,
            selected_authority=selected_authority,
        )
        # A plan file's EXISTENCE is not authority: run 20260824T103842Z-afec73 failed
        # its gate and left the generated plan behind, and the next build built a unit
        # on it. Consumption requires a clean-gate attestation; anything less is treated
        # as absent and regenerated through the transactional gate-then-publish path.
        needs_plan = not unit_plan_path.is_file()
        if not needs_plan:
            try:
                validate_work_unit_plan_authority(
                    shot.folder,
                    unit_plan_path,
                    selected_authority=selected_authority,
                )
            except ValueError as exc:
                log(f"existing unit plan is not gated authority ({str(exc)[:160]}) — regenerating")
                needs_plan = True
        if needs_plan:
            log(f"generating just-in-time plan for dependency-ready unit {layer.id}.{unit.id}")
            # generation is transactional: it publishes to the shot only through a clean
            # deterministic gate and retracts its artifacts otherwise
            await planner_package().generate_layer_plan(
                shot.folder,
                str(layer.id),
                unit_id=unit.id,
                blender=builder_package().Settings.from_environment().blender_bin,
                attempt_guard=planning_guard,
                fence_lease=fence_lease,
            )
            require_selected_authority_unchanged(
                shot.folder,
                selected_authority,
                operation=f"continue builder after planning {layer.id}.{unit.id}",
            )
        unit_excerpt = _plan_layer_excerpt(
            shot,
            layer,
            unit,
            selected_authority=selected_authority,
        )
        unit_layer = _active_unit_layer_view(layer, unit)
        unit_judges = unit_layer.judges
        unit_ref = next(ref for frame, ref in unit_judges if frame == unit.evaluation.primary_judge)
        milestone = Milestone(
            f"{layer.id}@{unit.id}",
            unit.evaluation.primary_judge,
            unit_ref,
            unit_layer.reads,
            strips.get(unit.evaluation.primary_judge, ()),
        )
        attempt = claim_ready_unit_for_build(
            shot.folder,
            str(layer.id),
            unit.id,
            layer.stages,
            planning_claim,
            expected_plan_hash=layers_hash,
            eligible_passed=passed_units,
            completion_authorization=completion_authorization,
            run_id=RUN_ID,
            selection_token=selected_authority.selection_token,
            reason="builder transaction started",
        )
        attempt_guard = planning_guard.promoted(attempt)
        context_path = write_attempt_bound_unit_context(
            shot,
            layer,
            unit_layer,
            unit,
            attempt_guard,
            selected_authority,
            load_milestones=load_milestones,
            load_axes=load_axes,
            write_context=write_layer_context,
            clear_context=clear_layer_context,
        )
        log(f"unit context → {context_path.relative_to(shot.folder)} (loaded every request)", 1)
        # A raised boundary error is not evidence that this unit's implementation
        # failed.  Preserve the in-flight state until a producer-sealed outcome can
        # classify the cause; otherwise infrastructure, harness, or session failures
        # falsely invalidate the unit and its entire dependency closure.
        execute_unit = builder_package().build_unit if unit_builder is None else unit_builder
        ledger = await execute_unit(
            shot,
            milestone,
            _unit_artifact_path(layer, unit),
            prior_layers + [shot.folder / rel for rel in unit_artifacts],
            session,
            rounds=rounds,
            verbose=verbose,
            plan_excerpt=unit_excerpt,
            scope=scope + f"\n  ACTIVE WORK UNIT: {unit.id} — {unit.title}. Only its claims may authorize repair.",
            layer=unit_layer,
            active_unit=unit,
            report_layer=(unit_layer if len(layer.stages) == 1 else replace(unit_layer, id=f"{layer.id}.{unit.id}")),
            resume_ok=resume_ok,
            layer_units=layer.stages,
            selected_authority=selected_authority,
            attempt_guard=attempt_guard,
            fence_lease=fence_lease,
        )
        unit_status = ledger.status(milestone)
        if unit_status != "passed":
            handle_unpassed_unit(
                shot,
                layer,
                unit,
                milestone,
                ledger,
                unit_status,
                attempt=attempt,
                selected_authority=selected_authority,
                publish_external=publish,
            )
            return ledger

        if unit.protects.ids:
            active_ids = {
                str(row["id"])
                for name, key in (("scene_checks.json", "contracts"), ("checks.json", "checks"))
                for row in load_document(selected_artifact(name), key)
                if active_for(row, layer.id)
            }
        else:
            active_ids = {
                str(row["id"])
                for row in load_document(
                    selected_artifact("scene_checks.json"),
                    "contracts",
                )
                if active_for(row, layer.id) and int(row.get("owner_layer")) < int(layer.id)
            }

        scene_rows = load_document(
            selected_artifact("scene_checks.json"),
            "contracts",
        )
        vis_ids = layer_active_visible_fraction_ids(scene_rows, layer.id)
        artifact = shot.folder / _unit_artifact_path(layer, unit)
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
            layer_active_vis_ids=vis_ids,
            attempt=attempt,
            selection_token=selected_authority.selection_token,
        )
        transition(
            shot.folder,
            str(layer.id),
            unit.id,
            "evaluating",
            reason="canonical evaluation sealed",
            attempt=attempt,
            selection_token=selected_authority.selection_token,
        )

        completion_receipt = complete_and_resolve_unit(
            shot.folder,
            str(layer.id),
            unit,
            layer.stages,
            attempt,
            expected_plan_hash=attempt.plan_hash,
            selected_authority=selected_authority,
            checkpoint_hash=str(frozen_state["units"][unit.id]["checkpoint"]["candidate_hash"]),
        )
        passed_units.add(unit.id)
        completion_authorization = authorize_completed_units_for_layer(
            shot.folder,
            str(layer.id),
            layer.stages,
            expected_plan_hash=layers_hash,
            selected_authority=selected_authority,
        )
        if (
            completion_authorization is None
            or completion_authorization.receipt_digest(unit.id)
            != completion_receipt.receipt_digest
        ):
            raise ValueError(
                f"layer {layer.id} unit {unit.id} completion was not authorized"
            )
        # Reconstruct rather than append: after an interrupted run, a newly completed
        # independent unit may sort before an already passed sibling.  Stable replay is
        # DAG order plus authored-order tie-break, never accident-of-attempt order.
        unit_artifacts = [
            _unit_artifact_path(layer, candidate) for candidate in ordered_units if candidate.id in passed_units
        ]

    # Deliberate package-facade lookup: the composition leaf reads the existing
    # builder.layer replacement surfaces used by tests and runtime configuration.
    from vfx_harness.agents.builder import layer as layer_runtime  # noqa: PLC0415

    return await finalize_composed_layer(
        layer_runtime,
        shot,
        layer,
        session,
        ordered_units=ordered_units,
        prior_layers=prior_layers,
        selected_layers=selected_layers,
        selected_authority=selected_authority,
        layers_hash=layers_hash,
        strips=strips,
        scope=scope,
        verbose=verbose,
    )


async def build_layer(
    shot: Shot,
    layer,
    session: BlenderSession,
    *,
    rounds: int = 2,
    verbose: bool = True,
    resume_ok: bool = False,
    force: bool = False,
    unit_builder: Callable[..., Awaitable[Ledger]] | None = None,
    selected_authority: ResolvedSelectedAuthority | None = None,
) -> Ledger:
    """Serialize all shared shot and Blender state for one complete layer build."""

    if resume_ok:
        raise ValueError(
            "builder resume refused: legacy ledger checkpoint/session rows are not "
            "bound to an exact work-unit attempt receipt; reviewed `vfx units retry` "
            "starts a new attempt from current authority"
        )
    with builder_execution_fence(shot.folder) as fence_lease:
        return await build_layer_already_fenced(
            shot,
            layer,
            session,
            rounds=rounds,
            verbose=verbose,
            resume_ok=resume_ok,
            force=force,
            unit_builder=unit_builder,
            selected_authority=selected_authority,
            fence_lease=fence_lease,
        )


async def build_layer_already_fenced(
    shot: Shot,
    layer,
    session: BlenderSession,
    *,
    rounds: int = 2,
    verbose: bool = True,
    resume_ok: bool = False,
    force: bool = False,
    unit_builder: Callable[..., Awaitable[Ledger]] | None = None,
    selected_authority: ResolvedSelectedAuthority | None = None,
    fence_lease: BuilderExecutionFenceLease,
) -> Ledger:
    """Build under the caller's one live shot fence without reacquiring it."""

    require_builder_execution_lease(fence_lease, shot.folder)
    with fence_lease.operation(shot.folder):
        if resume_ok:
            raise ValueError(
                "builder resume refused: legacy ledger checkpoint/session rows are not "
                "bound to an exact work-unit attempt receipt; reviewed `vfx units retry` "
                "starts a new attempt from current authority"
            )
        return await _build_layer_under_execution_fence(
            shot,
            layer,
            session,
            rounds=rounds,
            verbose=verbose,
            resume_ok=resume_ok,
            force=force,
            unit_builder=unit_builder,
            selected_authority=selected_authority,
            fence_lease=fence_lease,
        )


def _compose_unit_artifact_source(parts: list[tuple[str, str, str]]) -> str:
    """Preserve the historical builder helper API around pure source assembly."""

    return compose_unit_artifact_source(
        parts,
        evaluation_barrier=_ARTIFACT_EVALUATION_BARRIER,
    )


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

    frames = _ablation_frames(shot, layer)
    try:
        session.run(_RESET)
        session.run(builder_package()._preamble(shot))
        builder_package()._run_prior_paths(session, prior_paths)
        try:
            without = {frame: look_vector(session.render(frame=frame, mode="eevee", scale=0.4)) for frame in frames}
        except ExecutionAuthorityLost:
            raise
        except Exception as e:
            # The FIRST layer has no priors, so "without it" is an empty scene with no
            # camera. That is not a skip — it is the strongest possible result: nothing
            # renders at all until this layer runs.
            if "no camera" in str(e).lower():
                _run_artifact_script(session, shot.folder / script_rel)
                for frame in frames:
                    session.render(frame=frame, mode="eevee", scale=0.4)  # must now work
                return {
                    "ok": True,
                    "frames": frames,
                    "moved": {},
                    "note": "scene cannot render at all without this layer (no camera) — it establishes the spine",
                }
            raise
        _run_artifact_script(session, shot.folder / script_rel)
        with_ = {frame: look_vector(session.render(frame=frame, mode="eevee", scale=0.4)) for frame in frames}
    except ExecutionAuthorityLost:
        raise
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
