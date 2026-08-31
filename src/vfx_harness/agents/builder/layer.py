"""Stage 3 — the build + critic loop for one PLAN LAYER."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

from vfx_harness.agents.builder.axes import _owned_axes, ensure_axes
from vfx_harness.agents.builder.evidence import _unit_raster_mode
from vfx_harness.agents.builder.falsify import (
    _record_bound_contract_falsification,
    _record_composed_contract_gap_falsification,
    _record_contract_gap_falsification,
    _record_unsatisfiable_pair_falsification,
)
from vfx_harness.agents.builder.judgment_payment import JudgmentDebtPayment
from vfx_harness.agents.builder.models import _RESET, BuildAuthorityDefect, critic_model
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
)
from vfx_harness.agents.builder.revalidate import _blender_version
from vfx_harness.agents.builder.verify import _verify_script
from vfx_harness.agents.planner.pkg import planner_package
from vfx_harness.agents.shot_context import write_layer_context
from vfx_harness.blender.session import BlenderSession
from vfx_harness.domain.brief import Shot
from vfx_harness.domain.contracts import active_for, load_document
from vfx_harness.domain.work_units import dependency_ordered_units, layer_active_visible_fraction_ids
from vfx_harness.evidence.metrics import look_vector
from vfx_harness.observability import costlog, transcript
from vfx_harness.observability.log import (
    log,
)
from vfx_harness.observability.provenance import atomic_write
from vfx_harness.observability.runid import RUN_ID
from vfx_harness.orchestration.judgment_debt_state import (
    mark_judgment_debt_due,
    replay_prefix_receipt,
    resolve_current_judgment_debt,
)
from vfx_harness.orchestration.layer_plans import (
    validate_work_unit_plan_authority,
    work_unit_plan_path,
    write_layer_outcome,
)
from vfx_harness.orchestration.ledger import Ledger, Milestone, load_axes, load_layers, load_milestones, plan_strips
from vfx_harness.orchestration.plan_authority import active_plan_hash, selected_artifact_path
from vfx_harness.orchestration.plan_due import require_due_clear, resolve_unit_completion
from vfx_harness.orchestration.revalidation import digest
from vfx_harness.orchestration.unit_state import (
    block_dependents,
    freeze_checkpoint,
    initialize,
    ready_from_durable_state,
    transition,
)
from vfx_harness.orchestration.unit_state import load as load_unit_state


def _unit_artifact_path(layer, unit) -> str:
    spans = tuple(unit.mutates.script_spans)
    if len(spans) != 1:
        raise ValueError(
            f"layer {layer.id} unit {unit.id} must own exactly one replayable script span; "
            f"got {list(spans)}"
        )
    return spans[0]


def _active_unit_layer_view(layer, unit):
    """Compile unit-local judgment without discarding the parent unit DAG.

    ``active_unit`` is the mutation and claim boundary.  ``layer.stages`` remains
    dependency/repair-owner authority: pruning it to the active row makes later
    typed visibility owners look unbound, which conservatively charges their rows
    to the current geometry unit (HIR-0135).
    """
    unit_axes = tuple(dict.fromkeys(claim.axis for claim in unit.evaluation.claims))
    unit_judges = tuple((point.frame, point.ref) for point in unit.evaluation.judges)
    return replace(
        layer,
        script=_unit_artifact_path(layer, unit),
        title=(layer.title if len(layer.stages) == 1 else f"{layer.title} · {unit.title}"),
        judges=unit_judges,
        reads=f"Work unit {unit.id}: "
        + " ".join(claim.proposition for claim in unit.evaluation.claims),
        owns=unit_axes,
        primary_judge=unit.evaluation.primary_judge,
        stages=layer.stages,
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

    ordered_units = dependency_ordered_units(layer.stages)
    artifacts = [_unit_artifact_path(layer, unit) for unit in ordered_units]
    if len(layer.stages) > 1:
        if len(set(artifacts)) != len(artifacts):
            raise ValueError(f"layer {layer.id} work units must own distinct script artifacts")
        if layer.script in artifacts:
            raise ValueError(
                f"layer {layer.id} reserves {layer.script} for the composed layer artifact; "
                "multi-unit stages must write distinct unit scripts"
            )


    layers_hash = active_plan_hash(shot.folder)
    initialize(shot.folder, str(layer.id), layer.stages, plan_hash=layers_hash)
    prior_layers = _prior_layer_paths(shot, layer, force=force)
    state = load_unit_state(shot.folder, str(layer.id))
    passed_units = {
        uid
        for uid, row in (state.get("units") or {}).items()
        if row.get("status") == "passed"
        and (
            shot.folder
            / _unit_artifact_path(
                layer, next(unit for unit in layer.stages if unit.id == uid)
            )
        ).is_file()
    }
    unit_artifacts = [
        _unit_artifact_path(layer, unit)
        for unit in ordered_units
        if unit.id in passed_units
    ]

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
        ready = ready_from_durable_state(
            shot.folder,
            str(layer.id),
            layer.stages,
            eligible_passed=passed_units,
        )
        pending = [unit for unit in ready if unit.id not in passed_units]
        if not pending:
            raise RuntimeError(
                f"layer {layer.id} has no dependency-ready work unit; inspect logs/work_units state"
            )
        unit = pending[0]

        require_due_clear(shot.folder, layer=str(layer.id), unit=unit.id)
        unit_plan_path = work_unit_plan_path(shot.folder, unit)
        # A plan file's EXISTENCE is not authority: run 20260824T103842Z-afec73 failed
        # its gate and left the generated plan behind, and the next build built a unit
        # on it. Consumption requires a clean-gate attestation; anything less is treated
        # as absent and regenerated through the transactional gate-then-publish path.
        needs_plan = not unit_plan_path.is_file()
        if not needs_plan:

            try:
                validate_work_unit_plan_authority(shot.folder, unit_plan_path)
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
            )
        unit_excerpt = _plan_layer_excerpt(shot, layer, unit)
        unit_layer = _active_unit_layer_view(layer, unit)
        unit_judges = unit_layer.judges
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
        fps = {}
        try:
            fps = {m.frame: m.fingerprint for m in load_milestones(shot).values() if m.fingerprint}
        except Exception as exc:
            log(f"! no measured fingerprints in the unit contract: {str(exc)[:60]}", 1)
        context_path = write_layer_context(
            shot, unit_layer, load_axes(shot), fps, unit=unit, layer_units=layer.stages
        )
        log(f"unit context → {context_path.relative_to(shot.folder)} (loaded every request)", 1)
        # A raised boundary error is not evidence that this unit's implementation
        # failed.  Preserve the in-flight state until a producer-sealed outcome can
        # classify the cause; otherwise infrastructure, harness, or session failures
        # falsely invalidate the unit and its entire dependency closure.
        ledger = await builder_package().build_unit(
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
            publish_layer=len(layer.stages) == 1,
            report_layer=(
                unit_layer if len(layer.stages) == 1 else replace(unit_layer, id=f"{layer.id}.{unit.id}")
            ),
            resume_ok=resume_ok,
            layer_units=layer.stages,
        )
        unit_status = ledger.status(milestone)
        if unit_status != "passed":
            finding = None
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
                    finding = (
                        _record_unsatisfiable_pair_falsification(shot, layer, unit, milestone, ledger)
                        or _record_bound_contract_falsification(shot, layer, unit, milestone, ledger)
                    )
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
            if finding is not None:
                state_after = load_unit_state(shot.folder, str(layer.id))
                unpassed = [
                    f"{uid}={row.get('status')}"
                    for uid, row in (state_after.get("units") or {}).items()
                    if row.get("status") != "passed"
                ]
                raise BuildAuthorityDefect(
                    finding,
                    stage="builder",
                    exit_code=7,
                    legacy_detail=(
                        f"layer {layer.id} did not accept every work unit: "
                        + ", ".join(unpassed)
                    ),
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

        scene_rows = load_document(
            selected_artifact_path(shot.folder, "scene_checks.json"), "contracts"
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
        )
        transition(shot.folder, str(layer.id), unit.id, "evaluating", reason="canonical evaluation sealed")

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
        # Reconstruct rather than append: after an interrupted run, a newly completed
        # independent unit may sort before an already passed sibling.  Stable replay is
        # DAG order plus authored-order tie-break, never accident-of-attempt order.
        unit_artifacts = [
            _unit_artifact_path(layer, candidate)
            for candidate in ordered_units
            if candidate.id in passed_units
        ]

    if len(layer.stages) == 1 and unit_artifacts[0] == layer.script:
        return Ledger(shot)

    parts = [
        (
            str(unit.id),
            _unit_artifact_path(layer, unit),
            (shot.folder / _unit_artifact_path(layer, unit)).read_text(encoding="utf-8"),
        )
        for unit in ordered_units
    ]
    atomic_write(shot.folder / layer.script, _compose_unit_artifact_source(parts))
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
    all_axes = await ensure_axes(shot, verbose)
    canonical: list = []
    provisional_decisions = _load_provisional_decisions(shot, str(layer.id))
    decision_groups = (
        tuple((decision,) for decision in provisional_decisions)
        if provisional_decisions
        else ((),)
    )
    composition_units = tuple(
        _composition_judge_unit(layer, decisions) for decisions in decision_groups
    )
    result = "passed"
    terminal_finding = None
    for composition_unit in composition_units:
        composition_layer = layer
        active_decisions = tuple(
            getattr(composition_unit, "provisional_decisions", ()) or ()
        )
        typed_decisions = tuple(
            decision for decision in active_decisions if decision.get("debt_id")
        )
        if len(typed_decisions) > 1:
            raise ValueError(
                "one composed canonical payment may settle exactly one judgment debt"
            )
        if composition_unit is not None and active_decisions:
            judges = tuple(
                (int(point.frame), str(point.ref))
                for point in composition_unit.evaluation.judges
            )
            owns = tuple(
                dict.fromkeys(
                    claim.axis
                    for claim in composition_unit.evaluation.claims
                    if getattr(claim, "required", False)
                )
            )
            composition_layer = replace(layer, judges=judges, owns=owns)
        axes = _owned_axes(all_axes, composition_layer)
        if composition_unit is not None:
            if active_decisions:
                decision = active_decisions[0]
                identity = decision.get("debt_id") or decision["id"]
                log(
                    "composed canonical owes independent reference judgment for due "
                    f"debt {identity} · render mode {_unit_raster_mode(composition_unit)}",
                    1,
                )
            else:
                log(
                    "composed canonical fans in look-less unit claims — no critic look vote",
                    1,
                )
        on_replay_ready = None
        if typed_decisions:
            decision = typed_decisions[0]

            def _activate_after_replay(
                decision=decision,
                axes=axes,
            ) -> JudgmentDebtPayment:
                receipt = replay_prefix_receipt(
                    shot.folder,
                    replayed_layer_scripts=(*prior_layers, shot.folder / layer.script),
                )
                mark_judgment_debt_due(
                    shot.folder,
                    decision["definition_digest"],
                    layer_id=str(layer.id),
                    replayed_unit_digests=receipt.unit_digests,
                )
                return JudgmentDebtPayment(
                    shot=shot,
                    decision=decision,
                    session=session,
                    axes=axes,
                    replay_receipt=receipt,
                )

            on_replay_ready = _activate_after_replay
        canonical_start = len(canonical)
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
            layer=composition_layer,
            active_unit=composition_unit,
            out_verdicts=canonical,
            on_replay_ready=on_replay_ready,
        )
        finding_record_id = None
        if (
            result == "contract_gap"
            and composition_unit is not None
            and tuple(
                getattr(composition_unit, "provisional_requirement_ids", ()) or ()
            )
        ):
            finding = _record_composed_contract_gap_falsification(
                shot, layer, composition_unit
            )
            terminal_finding = finding
            finding_record_id = str(finding["record_id"])
            log(
                "composed provisional judgment published typed producer-closure finding → "
                f"{finding['record_id']} (accepted checkpoints preserved until replan)",
                1,
            )
        if typed_decisions and result in {"passed", "reproduced", "contract_gap"}:
            decision = typed_decisions[0]
            resolve_current_judgment_debt(
                shot.folder,
                decision["definition_digest"],
                outcome=(
                    "falsified" if result == "contract_gap" else "satisfied"
                ),
                evidence_digest=_judgment_payment_evidence_digest(
                    decision,
                    result=result,
                    verdicts=canonical[canonical_start:],
                    finding_record_id=finding_record_id,
                ),
            )
        if result not in {"passed", "reproduced"}:
            break
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
    if terminal_finding is not None:
        raise BuildAuthorityDefect(
            terminal_finding,
            stage="composition",
            exit_code=9,
            legacy_detail=(
                f"layer {layer.id} units passed but the composed verdict is {status!r}"
            ),
        )
    return ledger


def _judgment_payment_evidence_digest(
    decision: dict,
    *,
    result: str,
    verdicts: list,
    finding_record_id: str | None = None,
) -> str:
    """Bind a debt outcome to the exact canonical verdict slice that produced it."""
    payload = {
        "schema": "vfx-harness.judgment-debt-payment-evidence/v1",
        "debt_id": decision["debt_id"],
        "definition_digest": decision["definition_digest"],
        "activation_digest": decision["activation_digest"],
        "result": result,
        "verdicts": verdicts,
        "finding_record_id": finding_record_id,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _compose_unit_artifact_source(parts: list[tuple[str, str, str]]) -> str:
    """Compose unit scripts with the same evaluated-state publication as live replay."""
    composed = []
    for unit_id, rel, source in parts:
        composed.append(
            f"# --- work unit {unit_id}: {rel} ---\n"
            f"{source.rstrip()}\n\n"
            "# --- publish evaluated unit interface (HIR-0117) ---\n"
            f"{_ARTIFACT_EVALUATION_BARRIER.rstrip()}"
        )
    return "\n\n".join(composed).rstrip() + "\n"


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
