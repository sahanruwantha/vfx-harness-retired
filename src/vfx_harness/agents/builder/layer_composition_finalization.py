"""Composed replay, judgment, and terminal publication for one built layer.

The unit scheduler owns dependency-ready construction.  This leaf owns the later
serialized boundary: compose accepted unit artifacts, seal replay before judgment,
mint the terminal receipt, and reconcile its public projections.  Collaborators are
read from the caller's module so established ``builder.layer`` test and runtime
replacement surfaces remain authoritative.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any

from vfx_harness.agents.builder.layer_artifact import (
    LayerArtifactPublicationConflict,
)
from vfx_harness.domain.brief import Shot
from vfx_harness.domain.layer_finalizations import (
    LayerFinalizationPredecessorInput,
)
from vfx_harness.evidence.scene_checks import scene_contract_declared_frames
from vfx_harness.orchestration import layer_publication
from vfx_harness.orchestration.layer_evaluation_receipts import (
    LayerEvaluationReceiptConflict,
)
from vfx_harness.orchestration.layer_replay_receipts import (
    LayerReplayReceiptConflict,
)

if TYPE_CHECKING:
    from vfx_harness.orchestration.authority_selection import (
        ResolvedSelectedAuthority,
    )
    from vfx_harness.orchestration.ledger import Ledger


def _discard_preserving_primary_error(
    primary: BaseException,
    discard: Callable[[object], None],
    prepared: object,
    *,
    conflict_type: type[BaseException],
    label: str,
) -> None:
    """Keep the causal failure while retaining a typed cleanup diagnostic."""

    try:
        discard(prepared)
    except conflict_type as cleanup_error:
        primary.add_note(f"{label} cleanup diagnostic: {cleanup_error}")


def _json_ready(value):
    """Detach one finite JSON projection while normalizing tuples to lists."""

    return json.loads(
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )


def _canonical_receipt_rows(canonical: list) -> tuple[dict, ...]:
    rows: list[dict] = []
    for index, item in enumerate(canonical):
        if (
            not isinstance(item, (tuple, list))
            or len(item) != 2
            or not isinstance(item[0], (tuple, list))
            or len(item[0]) != 2
            or not isinstance(item[1], dict)
        ):
            raise ValueError(f"layer finalization canonical[{index}] has an invalid shape")
        frame, ref = item[0]
        rows.append(
            {
                "frame": int(frame),
                "ref": str(ref),
                "verdict": _json_ready(dict(item[1])),
            }
        )
    return tuple(rows)


def _finalization_predecessor_inputs(
    shot: Shot,
    prior_paths: list[Path],
    selected_layers: dict,
    selected_authority: ResolvedSelectedAuthority,
) -> tuple[LayerFinalizationPredecessorInput, ...]:
    by_script = {str(candidate.script): candidate for candidate in selected_layers.values()}
    rows: list[LayerFinalizationPredecessorInput] = []
    shot_root = shot.folder.expanduser().absolute()
    for index, path in enumerate(prior_paths):
        try:
            relative = path.expanduser().absolute().relative_to(shot_root).as_posix()
        except ValueError as exc:
            raise ValueError(f"layer finalization predecessor[{index}] escapes the shot root") from exc
        predecessor = by_script.get(relative)
        if predecessor is None:
            raise ValueError(f"layer finalization predecessor[{index}] is not a selected layer: {relative}")
        try:
            publication = layer_publication.require_current_layer_publication(
                shot.folder,
                predecessor,
                selected_authority,
            )
        except layer_publication.LayerPublicationConflict as exc:
            raise ValueError(
                f"layer finalization cannot consume predecessor {predecessor.id} "
                f"without a current receipt-backed publication: {exc}. Reconcile "
                "that predecessor's exact terminal projections before claiming the "
                "downstream layer"
            ) from exc
        receipt = publication.receipt
        rows.append(
            LayerFinalizationPredecessorInput.mint(
                layer_id=str(predecessor.id),
                finalization_receipt_digest=receipt.receipt_digest,
                script_path=receipt.layer_script_path,
                script_sha256=receipt.layer_script_sha256,
            )
        )
    return tuple(rows)


async def finalize_composed_layer(
    runtime: ModuleType,
    shot: Shot,
    layer: Any,
    session: Any,
    *,
    ordered_units: tuple[Any, ...],
    prior_layers: list[Path],
    selected_layers: dict[str, Any],
    selected_authority: ResolvedSelectedAuthority,
    layers_hash: str,
    strips: dict,
    scope: str,
    verbose: bool,
) -> Ledger:
    """Seal and publish one composed layer under its finalization claim."""

    predecessor_inputs = runtime._finalization_predecessor_inputs(
        shot,
        prior_layers,
        selected_layers,
        selected_authority,
    )
    proposed_layer_sha256 = runtime.proposed_layer_artifact_sha256(
        shot.folder,
        ((unit.id, runtime._unit_artifact_path(layer, unit)) for unit in ordered_units),
        evaluation_barrier=runtime._ARTIFACT_EVALUATION_BARRIER,
    )
    finalization_claim = runtime.claim_layer_finalization(
        shot.folder,
        layer,
        expected_plan_hash=layers_hash,
        run_id=runtime.RUN_ID,
        layer_script_sha256=proposed_layer_sha256,
        predecessor_inputs=predecessor_inputs,
        selection_token=selected_authority.selection_token,
    )
    finalization_guard = runtime.LayerFinalizationClaimGuard.bind(
        shot.folder,
        finalization_claim,
        layer.stages,
        selected_authority,
    )
    prepared_artifact = runtime.prepare_layer_artifact(
        shot.folder,
        finalization_guard,
        evaluation_barrier=runtime._ARTIFACT_EVALUATION_BARRIER,
    )
    prepared_artifact_sha256 = prepared_artifact.sha256
    try:
        runtime.commit_layer_artifact(
            shot.folder,
            prepared_artifact,
            finalization_guard,
        )
    except BaseException as exc:
        _discard_preserving_primary_error(
            exc,
            runtime.discard_layer_artifact,
            prepared_artifact,
            conflict_type=LayerArtifactPublicationConflict,
            label="layer-artifact",
        )
        raise
    runtime.log(f"published {finalization_claim.mode} layer artifact → {layer.script} ({finalization_claim.claim_id})")

    guarded_session = runtime.AttemptBoundBlenderSession(session, finalization_guard)
    ledger = runtime.AuthorityBoundLedger(
        shot,
        selected_authority,
        execution_guard=finalization_guard,
    )
    milestone = layer.as_milestone(strips)
    ledger._slot(milestone)["script"] = layer.script
    ledger._slot(milestone)["layer_finalization_claim"] = finalization_claim.claim_id
    ledger.begin(milestone)
    composition_attempt = int(ledger._slot(milestone).get("attempt") or 0)
    runtime.costlog.bind(
        shot.folder,
        role="layer_composition",
        phase="canonical_composition",
        layer=str(layer.id),
        run_id=runtime.RUN_ID,
        attempt=composition_attempt,
        model=runtime.critic_model(),
    )
    runtime.transcript.bind(
        shot.folder,
        "build",
        label=f"layer{layer.id}-composition",
        run_id=runtime.RUN_ID,
    )
    finalization_guard.check(f"start {finalization_claim.mode} layer axis resolution")
    all_axes = await runtime.ensure_axes(shot, verbose, selected_authority)
    finalization_guard.check(f"finish {finalization_claim.mode} layer axis resolution")
    canonical: list = []
    provisional_decisions = runtime._load_provisional_decisions(
        shot,
        str(layer.id),
        selected_authority=selected_authority,
    )
    finalization_guard.check("load current layer provisional judgments")
    # One group per debt, plus one per unit medium no debt group covers: a contract is
    # re-measured in the medium it was paid in or not at all (HIR-0241).
    group_plans = runtime.composed_group_plans(layer, provisional_decisions)
    composition_units = tuple(
        runtime._composition_judge_unit(layer, decisions, medium=medium) for decisions, medium in group_plans
    )
    result = "passed"
    stored_layer_replays: list[Any] = []
    evaluation_groups: list[dict] = []
    deferred_debt_projections: list[dict] = []
    deferred_finding_unit = None
    for group_index, composition_unit in enumerate(composition_units):
        composition_layer = layer
        active_decisions = tuple(getattr(composition_unit, "provisional_decisions", ()) or ())
        typed_decisions = tuple(decision for decision in active_decisions if decision.get("debt_id"))
        if len(typed_decisions) > 1:
            raise ValueError("one composed canonical payment may settle exactly one judgment debt")
        if composition_unit is not None and active_decisions:
            judges = tuple((int(point.frame), str(point.ref)) for point in composition_unit.evaluation.judges)
            owns = tuple(
                dict.fromkeys(
                    evaluation_claim.axis
                    for evaluation_claim in composition_unit.evaluation.claims
                    if getattr(evaluation_claim, "required", False)
                )
            )
            composition_layer = runtime.replace(layer, judges=judges, owns=owns)
        axes = runtime._owned_axes(all_axes, composition_layer)
        if composition_unit is not None:
            if active_decisions:
                decision = active_decisions[0]
                identity = decision.get("debt_id") or decision["id"]
                runtime.log(
                    "composed canonical owes independent reference judgment for due "
                    f"debt {identity} · render mode "
                    f"{runtime._unit_raster_mode(composition_unit)}",
                    1,
                )
            else:
                runtime.log(
                    "composed canonical fans in look-less unit claims — no critic look vote",
                    1,
                )
        decision = typed_decisions[0] if typed_decisions else None
        payment_holder: list[Any] = []
        replay_prefix_holder: list[Any] = []

        def _capture_replay_before_observation(
            replay_inputs,
            decision=decision,
            replay_prefix_holder=replay_prefix_holder,
        ):
            prefix = runtime.replay_prefix_receipt(
                shot.folder,
                replayed_layer_scripts=(*prior_layers, shot.folder / layer.script),
                replay_inputs=replay_inputs,
                selected_authority=selected_authority,
                finalization_claim=finalization_claim,
            )
            replay_bindings, _source_identities = runtime.prepare_replay_inputs(
                shot.folder.expanduser().absolute(),
                replay_inputs,
            )
            payment_generation = None
            if decision is not None:
                try:
                    definition, activation, _state = next(
                        row
                        for row in runtime.current_judgment_debt_states_for_authority(
                            shot.folder,
                            selected_authority,
                        )
                        if row[0].digest == decision["definition_digest"]
                    )
                except StopIteration as exc:
                    raise ValueError(
                        "layer replay cannot resolve its selected judgment debt"
                    ) from exc
                if activation is None:
                    raise ValueError(
                        "layer replay judgment debt has no selected payer activation"
                    )
                payment_generation = runtime.payment_generation_for_replay(
                    definition,
                    activation,
                    prefix,
                )
            replay_prefix_holder.append(prefix)
            return prefix, replay_bindings, payment_generation

        def _compile_group_plan(
            payment_generation,
            decision=decision,
            axes=axes,
            composition_unit=composition_unit,
            composition_layer=composition_layer,
            group_index=group_index,
        ):
            raster_required = runtime._unit_requires_raster(
                shot,
                composition_unit,
                selected_authority=selected_authority,
            )
            # Each bound row is required at the frames it declares, not at every moment of
            # the claim that binds it (HIR-0204).
            declared_frames = scene_contract_declared_frames(
                shot.folder, selected_authority=selected_authority
            )
            claim_rows = tuple(
                runtime.LayerReplayClaimRequirement(
                    claim_id=str(claim.id),
                    authority=str(claim.authority),
                    judge_frames=tuple(
                        int(frame)
                        for frame in claim.moments
                        if any(
                            int(frame) == int(point[0])
                            for point in composition_layer.judges
                        )
                    ),
                    evidence_ids=tuple(str(row.id) for row in claim.evidence),
                    evidence_frames=tuple(
                        (str(row.id), declared_frames.get(str(row.id), ()))
                        for row in claim.evidence
                        if declared_frames.get(str(row.id))
                    ),
                )
                for claim in (
                    getattr(
                        getattr(composition_unit, "evaluation", None),
                        "claims",
                        (),
                    )
                    or ()
                )
                if getattr(claim, "required", False)
                and any(
                    int(frame) in claim.moments
                    for frame, _ref in composition_layer.judges
                )
            )
            if not claim_rows:
                if not raster_required:
                    raise ValueError(
                        "executable layer replay has no required claim authority"
                    )
                claim_rows = (
                    runtime.LayerReplayClaimRequirement(
                        claim_id=f"layer:{layer.id}:qualified-look",
                        authority="qualified_qualitative_required",
                        judge_frames=tuple(
                            int(frame) for frame, _ref in composition_layer.judges
                        ),
                        evidence_ids=(),
                    ),
                )
            return runtime.LayerReplayEvaluationGroupPlan(
                group_index=group_index,
                planned_group_count=len(composition_units),
                requirement_ids=tuple(
                    getattr(
                        composition_unit,
                        "provisional_requirement_ids",
                        (),
                    )
                    or ()
                ),
                debt_id=None if decision is None else str(decision["debt_id"]),
                definition_digest=(
                    None if decision is None else str(decision["definition_digest"])
                ),
                activation_digest=(
                    None if decision is None else str(decision["activation_digest"])
                ),
                payment_generation_digest=(
                    None if payment_generation is None else payment_generation.digest
                ),
                judge_points=tuple(
                    (int(frame), str(ref)) for frame, ref in composition_layer.judges
                ),
                # One source of truth with JudgmentDebtPayment._points: the debt's own
                # moments, not the group's judge list (HIR-0206).
                debt_points=(
                    ()
                    if decision is None
                    else tuple(
                        (int(frame), str(ref))
                        for frame, ref in (decision.get("judge_points") or ())
                    )
                ),
                axes=tuple(str(key) for key, _description in axes),
                claims=claim_rows,
                evidence_kind="render" if raster_required else "executable_only",
                render_mode=(
                    runtime._unit_raster_mode(composition_unit)
                    if raster_required
                    else None
                ),
                render_scale=0.5 if raster_required else None,
            )

        def _publish_group_receipt(
            replay_inputs,
            replay_bindings,
            observation,
        ):
            receipt = runtime.LayerReplayReceipt.mint(
                claim=finalization_claim,
                layer_script_sha256=prepared_artifact_sha256,
                replay_inputs=replay_bindings,
                observation=observation,
                replay_status="ready",
                created_at=runtime.state_now(),
            )
            prepared_replay = runtime.prepare_layer_replay_receipt(
                shot.folder,
                receipt,
                finalization_guard,
            )
            try:
                stored_layer_replay = runtime.commit_layer_replay_receipt(
                    shot.folder,
                    prepared_replay,
                    finalization_guard,
                )
            except BaseException as exc:
                _discard_preserving_primary_error(
                    exc,
                    runtime.discard_layer_replay_receipt,
                    prepared_replay,
                    conflict_type=LayerReplayReceiptConflict,
                    label="layer-replay-receipt",
                )
                raise
            stored_layer_replays.append(stored_layer_replay)
            runtime.require_replay_inputs_unchanged(
                shot.folder,
                replay_inputs,
                where="layer-finalization replay input after receipt publication",
            )
            return stored_layer_replay

        def _seal_observation_before_judgment(
            replay_inputs,
            observations,
            replay_context,
            decision=decision,
            axes=axes,
            payment_holder=payment_holder,
            composition_unit=composition_unit,
            composition_layer=composition_layer,
            group_index=group_index,
        ):
            prefix, replay_bindings, payment_generation = replay_context
            plan = _compile_group_plan(payment_generation)
            points = tuple(
                runtime.LayerReplayPointObservation.mint(
                    plan=plan,
                    frame=int(row["frame"]),
                    ref=str(row["ref"]),
                    ref_sha256=runtime.digest(shot.folder / str(row["ref"])),
                    evidence=row["evidence"],
                    render=row["render"],
                    render_sha256=(
                        None
                        if row["render_capture"] is None
                        else row["render_capture"]["png_sha256"]
                    ),
                    render_capture=row["render_capture"],
                )
                for row in observations
            )
            motion_evidence = observations[0].get("motion_evidence")
            if any(
                row.get("motion_evidence") != motion_evidence
                for row in observations[1:]
            ):
                raise ValueError(
                    "layer replay points disagree on their evaluated motion capture"
                )
            auxiliary_captures = ()
            if motion_evidence is not None:
                motion_locator, motion_frames = motion_evidence
                motion_sha256 = runtime.digest(shot.folder / str(motion_locator))
                if motion_sha256 is None:
                    raise ValueError(
                        "layer replay motion capture is missing before observation sealing"
                    )
                auxiliary_captures = (
                    {
                        "kind": "motion_montage",
                        "locator": str(motion_locator),
                        "sha256": motion_sha256,
                        "frames": [int(frame) for frame in motion_frames],
                    },
                )
            stored_layer_replay = _publish_group_receipt(
                replay_inputs,
                replay_bindings,
                runtime.LayerReplayObservation(
                    replay_prefix=prefix,
                    plan=plan,
                    points=points,
                    auxiliary_captures=auxiliary_captures,
                ),
            )
            if decision is None:
                return stored_layer_replay.receipt, None
            payment = runtime.JudgmentDebtPayment(
                shot=shot,
                decision=decision,
                session=guarded_session,
                axes=axes,
                replay_receipt=prefix,
                layer_replay_receipt=stored_layer_replay.receipt,
                policy=runtime.JudgmentDebtPaymentPolicy.layer_finalization(),
            )
            payment_holder.append(payment)
            return stored_layer_replay.receipt, payment

        def _seal_failed_replay(
            replay_inputs,
            stage,
            message,
            replay_context,
        ):
            if replay_context is None:
                raise ValueError(
                    "failed layer replay did not produce its exact replay identity"
                )
            prefix, replay_bindings, payment_generation = replay_context
            plan = _compile_group_plan(payment_generation)
            stored = _publish_group_receipt(
                replay_inputs,
                replay_bindings,
                runtime.LayerReplayObservation.failed(
                    replay_prefix=prefix,
                    plan=plan,
                    stage=stage,
                    message=message,
                ),
            )
            return stored.receipt

        canonical_start = len(canonical)
        result = await runtime._verify_script(
            shot,
            milestone,
            layer.script,
            prior_layers,
            guarded_session,
            axes,
            ledger,
            verbose,
            scope=scope,
            layer=composition_layer,
            active_unit=composition_unit,
            out_verdicts=canonical,
            on_replay_ready=_capture_replay_before_observation,
            on_observation_ready=_seal_observation_before_judgment,
            on_replay_failed=_seal_failed_replay,
            selected_authority=selected_authority,
            execution_guard=finalization_guard,
            canonical_namespace=f"finalization_group_{group_index}",
        )
        evaluation_groups.append(
            {
                "group_index": group_index,
                "result": result,
                "requirement_ids": list(
                    getattr(
                        composition_unit,
                        "provisional_requirement_ids",
                        (),
                    )
                    or ()
                ),
                "debt_id": None if decision is None else decision["debt_id"],
                "definition_digest": (None if decision is None else decision["definition_digest"]),
                "activation_digest": (None if decision is None else decision["activation_digest"]),
                "canonical_start": canonical_start,
                "canonical_end": len(canonical),
                "payment_failures": (
                    []
                    if not payment_holder
                    else [
                        {
                            "request": row.request.as_dict(),
                            "failure": row.failure.as_dict(),
                        }
                        for row in payment_holder[0].deferred_payment_attempt_failures
                    ]
                ),
            }
        )
        if (
            result == "contract_gap"
            and composition_unit is not None
            and tuple(
                getattr(
                    composition_unit,
                    "provisional_requirement_ids",
                    (),
                )
                or ()
            )
        ):
            deferred_finding_unit = composition_unit
        if typed_decisions:
            replay_failed = (
                stored_layer_replays[-1].receipt.observation.execution_status
                == "failed"
            )
            if replay_failed:
                if payment_holder or len(replay_prefix_holder) != 1:
                    raise ValueError(
                        "failed typed layer replay cannot claim a judgment payment"
                    )
            else:
                if len(payment_holder) != 1 or len(replay_prefix_holder) != 1:
                    raise ValueError(
                        "typed layer judgment must consume exactly one sealed "
                        "replay prefix"
                    )
                payment = payment_holder[0]
                deferred_debt_projections.append(
                    {
                        "decision": dict(decision),
                        "result": result,
                        "canonical_start": canonical_start,
                        "canonical_end": len(canonical),
                        "payment_failures": [
                            {
                                "request": row.request.as_dict(),
                                "failure": row.failure.as_dict(),
                            }
                            for row in payment.deferred_payment_attempt_failures
                        ],
                    }
                )
        if result not in {"passed", "reproduced"}:
            break
    if not stored_layer_replays:
        raise ValueError(f"layer {layer.id} finalization produced no immutable replay receipt")
    evaluation = runtime.LayerEvaluationReceipt.mint(
        replay_receipts=tuple(
            runtime.LayerReplayReceiptBinding.mint(
                locator=stored.locator,
                sha256=stored.sha256,
                receipt=stored.receipt,
            )
            for stored in stored_layer_replays
        ),
        evaluation_groups=runtime._json_ready(evaluation_groups),
        canonical=runtime._canonical_receipt_rows(canonical),
        created_at=runtime.state_now(),
    )
    prepared_evaluation = runtime.prepare_layer_evaluation_receipt(
        shot.folder,
        evaluation,
        finalization_guard,
    )
    try:
        stored_evaluation = runtime.commit_layer_evaluation_receipt(
            shot.folder,
            prepared_evaluation,
            finalization_guard,
        )
    except BaseException as exc:
        _discard_preserving_primary_error(
            exc,
            runtime.discard_layer_evaluation_receipt,
            prepared_evaluation,
            conflict_type=LayerEvaluationReceiptConflict,
            label="layer-evaluation-receipt",
        )
        raise
    status = evaluation.final_status
    best = {
        "round": 0,
        "mean": min(
            (verdict.get("mean", 0) for _frame_ref, verdict in canonical),
            default=0,
        ),
        "render": None,
    }
    finalization_guard.check(f"prepare layer {layer.id} terminal projections")
    blender_version = runtime._blender_version(guarded_session)
    ablation = (
        await runtime.builder_package()._ablate(
            shot,
            layer,
            prior_layers,
            layer.script,
            guarded_session,
        )
        if status == "passed"
        else {"ok": False, "note": "not run because layer finalization did not pass"}
    )
    finalization_guard.check(f"finish layer {layer.id} ablation")
    prepared_revalidation = runtime.prepare_layer_revalidation(
        shot.folder,
        str(layer.id),
        lambda check: runtime._builder_render(shot.folder, check),
        selected_authority=selected_authority,
    )
    finalization_guard.check(f"finish layer {layer.id} image-check revalidation")
    revalidation_projection = runtime.layer_revalidation_projection(prepared_revalidation)
    finalization_completed_at = runtime.state_now()
    prepared_finding = (
        None
        if deferred_finding_unit is None
        else runtime._prepare_composed_contract_gap_falsification(
            shot,
            layer,
            deferred_finding_unit,
            recorded_at=finalization_completed_at,
            selected_authority=selected_authority,
        )
    )
    finding_payload = None if prepared_finding is None else prepared_finding.payload
    terminal_debt_projections: list[dict] = []
    for debt_projection in deferred_debt_projections:
        debt_result = str(debt_projection["result"])
        resolution = None
        if debt_result in {"passed", "reproduced", "contract_gap"}:
            decision = debt_projection["decision"]
            evidence_digest = runtime._judgment_payment_evidence_digest(
                decision,
                result=debt_result,
                verdicts=canonical[int(debt_projection["canonical_start"]) : int(debt_projection["canonical_end"])],
                finding_record_id=(None if finding_payload is None else str(finding_payload["record_id"])),
            )
            resolution = {
                "outcome": ("falsified" if debt_result == "contract_gap" else "satisfied"),
                "evidence_digest": evidence_digest,
            }
        terminal_debt_projections.append({**debt_projection, "resolution": resolution})
    outcome_projection = runtime.build_layer_outcome_projection(
        shot.folder,
        layer,
        best=best,
        canonical=canonical,
        finalization_claim=evaluation.claim,
        final_status=status,
        blender_version=blender_version,
        revalidation_projection=revalidation_projection,
        selected_authority=selected_authority,
    )
    finalization_guard.check(f"finish layer {layer.id} outcome projection")
    current_predecessors = runtime._finalization_predecessor_inputs(
        shot,
        prior_layers,
        selected_layers,
        selected_authority,
    )
    if current_predecessors != evaluation.claim.predecessor_inputs:
        raise runtime.LayerFinalizationAuthorityLost(
            f"terminal commit refused because layer {layer.id} predecessor "
            "publications changed during replay or judgment"
        )
    terminal_projection = runtime._json_ready(
        {
            "schema": runtime.LAYER_FINALIZATION_PROJECTION_SCHEMA,
            "best": best,
            "blender_version": blender_version,
            "ablation": ablation,
            "revalidation": revalidation_projection,
            "judgment_debts": terminal_debt_projections,
            "finding": finding_payload,
            "outcome": outcome_projection.as_dict(),
            "ledger": {
                "status": status,
                "script": layer.script,
                "script_sha256": stored_layer_replays[0].receipt.layer_script_sha256,
            },
        }
    )
    terminal_receipt = runtime.LayerFinalizationReceipt.mint(
        evaluation_receipt=evaluation,
        evaluation_receipt_locator=stored_evaluation.locator,
        evaluation_receipt_sha256=stored_evaluation.sha256,
        projection=terminal_projection,
        completed_at=finalization_completed_at,
    )
    try:
        runtime.complete_layer_finalization(
            shot.folder,
            terminal_receipt,
            evaluation,
            layer.stages,
            selection_token=selected_authority.selection_token,
        )
        reconciled = runtime.reconcile_layer_finalization(
            shot,
            layer,
            terminal_receipt,
            selected_authority=selected_authority,
            strips=strips,
        )
        if reconciled.revalidation["dropped"]:
            runtime.log(
                f"builder checks: {reconciled.revalidation['kept']} held, "
                f"{len(reconciled.revalidation['dropped'])} dropped as stale",
                1,
            )
        terminal_finding = reconciled.finding
        if terminal_finding is not None:
            runtime.log(
                "composed provisional judgment published typed producer-closure "
                f"finding → {terminal_finding['record_id']}",
                1,
            )
        runtime.log(
            f"layer outcome → {reconciled.outcome.relative_to(shot.folder)}",
            1,
        )
        terminal_ledger = reconciled.ledger
    finally:
        runtime.discard_layer_revalidation(prepared_revalidation)
        runtime.transcript.unbind()
        runtime.costlog.unbind()
    if terminal_finding is not None:
        raise runtime.BuildAuthorityDefect(
            terminal_finding,
            stage="composition",
            exit_code=9,
            legacy_detail=(f"layer {layer.id} units passed but the composed verdict is {status!r}"),
        )
    return terminal_ledger


__all__ = [
    "_canonical_receipt_rows",
    "_finalization_predecessor_inputs",
    "_json_ready",
    "finalize_composed_layer",
]
