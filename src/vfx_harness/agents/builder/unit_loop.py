"""Stage 3 — the build + critic loop for one PLAN LAYER."""

from __future__ import annotations

import shutil
import time
from functools import partial
from pathlib import Path

from claude_agent_sdk import (
    ClaudeSDKClient,
)

from vfx_harness.agents.approach import review as approach_review
from vfx_harness.agents.approach import revision_from_review
from vfx_harness.agents.build_prompts import (
    builder_kickoff,
    recurring_complaints,
    revision_prompt,
)
from vfx_harness.agents.builder.attempt_guard import (
    AttemptBoundBlenderSession,
    UnitAttemptGuard,
)
from vfx_harness.agents.builder.axes import (
    _builder_ticket_context,
    _evidence_convergence_stop,
    _layer_needs_motion,
    _owned_axes,
    _warn_unowned_axes,
    ensure_axes,
)
from vfx_harness.agents.builder.candidate_script import exact_candidate_script_path
from vfx_harness.agents.builder.critic import _round_rank
from vfx_harness.agents.builder.drain import _drain
from vfx_harness.agents.builder.evidence import (
    _unit_raster_mode,
    _unit_requires_raster,
)
from vfx_harness.agents.builder.models import (
    _RESET,
    _TRUNCATED,
    BuildTruncated,
    _budget_terminal_cause,
    builder_model,
)
from vfx_harness.agents.builder.pkg import builder_package
from vfx_harness.agents.builder.prior import (
    _builder_options,
)
from vfx_harness.agents.builder.revalidate import (
    _live_reopen_reason,
    _live_round_budget,
    _try_revalidate,
)
from vfx_harness.agents.builder.state import _APPROACH, _RECIPES_USED
from vfx_harness.agents.builder.unit_construction import resolve_unit_construction
from vfx_harness.agents.builder.unit_context import compile_unit_build_context
from vfx_harness.agents.builder.unit_finalize import (
    _metric_report,
    _persist_journal_and_finalize_script,
    _publish_unit_outcome,
    _run_canonical_repairs,
)
from vfx_harness.agents.builder.unit_runtime import (
    apply_retry_warm_start,
    publish_candidate_script,
    start_unit_runtime,
)
from vfx_harness.agents.builder.verdicts import _judge_unit_or_layer, _layer_motion_frames
from vfx_harness.agents.builder.verify import _verify_script
from vfx_harness.application.preflight import model_phase_failure
from vfx_harness.blender.session import BlenderError, BlenderSession
from vfx_harness.blender.tools import build_blender_tools, capture_image_adversaries
from vfx_harness.domain.brief import Shot
from vfx_harness.domain.image_debts import (
    UNPAID_IMAGE_DEBT,
    freeze_refusal,
    image_contract_debt_cards,
    unpaid_image_contract_debts,
)
from vfx_harness.evidence.checks import load_image_contract_payment_rows
from vfx_harness.evidence.scene_checks import prior_interface_evidence
from vfx_harness.knowledge.recipes import build_recipe_tools, log_recipe_use
from vfx_harness.observability import costlog, run_artifacts, transcript
from vfx_harness.observability.log import (
    TOOL_USE,
    log,
    reset_tool_use,
)
from vfx_harness.observability.runid import RUN_ID
from vfx_harness.observability.runlog import reset_counts
from vfx_harness.orchestration import generate_construction as generate_construction
from vfx_harness.orchestration.authority_selection import (
    ResolvedSelectedAuthority,
    resolve_selected_authority,
)
from vfx_harness.orchestration.builder_execution_fence import (
    builder_execution_fenced,
)
from vfx_harness.orchestration.layer_state import record_round as state_round
from vfx_harness.orchestration.layer_state import start as state_start
from vfx_harness.orchestration.ledger import Ledger, Milestone
from vfx_harness.orchestration.unit_state import load as _load_unit_state


@builder_execution_fenced
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
    layer_units=None,
    selected_authority: ResolvedSelectedAuthority | None = None,
    attempt_guard: UnitAttemptGuard | None = None,
) -> Ledger:
    """The build+critic engine for ONE unit of work. Iteration is judged at m.frame vs
    m.ref; the canonical check covers every frame `layer` claims (see _verify_script)."""
    if resume_ok:
        raise ValueError(
            "builder resume refused: legacy checkpoint/session rows are not exact "
            "work-unit attempt receipts; reviewed retry starts a new attempt"
        )
    if active_unit is None or attempt_guard is None:
        raise ValueError("build_unit requires an exact active work-unit attempt guard")
    selected_authority = selected_authority or resolve_selected_authority(shot.folder)
    attempt_guard.check(f"start unit {getattr(layer, 'id', m.id)}.{active_unit.id} build")
    session = AttemptBoundBlenderSession(session, attempt_guard)

    def publish(operation, mutation):
        return attempt_guard.publish(operation, mutation)

    started = start_unit_runtime(
        shot,
        m,
        script_rel,
        active_unit,
        selected_authority,
        attempt_guard,
    )
    ledger = started.ledger
    previous_status = started.previous_status
    retry_script = started.retry_script
    current_unit_hash = started.unit_hash
    warm_start_candidate = started.warm_start_candidate
    t_layer = started.started_at

    promoted = resolve_unit_construction(
        shot,
        str(getattr(layer, "id", m.id)),
        active_unit,
        current_unit_hash,
        attempt_guard,
    )
    generate_construction.pin_construction_import(session, promoted)

    revalidated = _try_revalidate(
        shot, m, script_rel, prior_paths, session, layer=layer, ledger=ledger,
        t_layer=t_layer, active_unit=active_unit,
        attempt_guard=attempt_guard,
        selected_authority=selected_authority,
    )
    if revalidated is not None:
        return revalidated

    attempt_guard.check(f"load axes for unit {active_unit.id}")
    all_axes = await ensure_axes(shot, verbose, selected_authority)
    _warn_unowned_axes(shot, all_axes, selected_authority)
    # A layer's ownership is already deterministic in layers.json. Passing every rubric
    # axis and asking the critic to decide which were n/a made the denominator move between
    # identical repeats. Filter before the builder prompt, critic prompt, and JSON schema.
    axes = _owned_axes(all_axes, layer)
    build_context = compile_unit_build_context(
        shot,
        m,
        layer,
        active_unit,
        axes,
        layer_units=layer_units,
        selected_authority=selected_authority,
    )
    _feedback_groups = build_context.feedback_groups
    _look_actions = build_context.look_actions
    phase = build_context.phase
    comparison_state = phase
    scope_baseline: set[str] = set()
    unit_scope_card = build_context.scope_card
    bserver, bnames = build_blender_tools(
        session,
        assets_dir=shot.folder / "assets",
        shot_dir=shot.folder,
        layer_id=getattr(layer, "id", m.id),
        comparison_state=comparison_state,
        feedback_groups=sorted(_feedback_groups) if active_unit is not None else None,
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
        unit_scope=unit_scope_card,
        selected_authority=selected_authority,
        attempt_guard=attempt_guard,
    )
    rserver, rnames = build_recipe_tools(
        on_use=lambda names: (log_recipe_use(shot.folder, names), _RECIPES_USED.extend(names)),
        mutation_roles=(
            tuple(active_unit.mutates.roles)
            if active_unit is not None and active_unit.mutates.mode == "scoped"
            else None
        ),
    )
    mcp_servers = {"blender": bserver, "recipes": rserver}
    tool_names = bnames + rnames

    # Deterministic base + prior delta scripts (a unit extends the existing scene).
    session.run(_RESET)
    session.run(builder_package()._preamble(shot))
    priors = builder_package()._run_prior_paths(session, prior_paths)
    generate_construction.pin_construction_import(session, promoted)
    capture_image_adversaries(session, shot.folder, comparison_state, prior_paths)
    unit_journal_start = int(session.journal().get("calls", 0))

    if layer is not None and int(layer.id) > 1:

        interfaces = prior_interface_evidence(
            shot.folder,
            str(layer.id),
            session=session,
            selected_authority=selected_authority,
        )
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

    warm_started, priors = apply_retry_warm_start(
        shot,
        session,
        prior_paths,
        priors,
        promoted,
        enabled=warm_start_candidate,
        retry_script=retry_script,
        previous_status=previous_status,
        script_rel=script_rel,
    )

    # The scene is now fully staged (priors + any warm-start replay): everything present
    # is inherited authority the live scope check must not flag against this unit.
    scope_baseline.update(builder_package()._scene_object_manifest(session))

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
    publish(
        f"start layer {getattr(layer, 'id', m.id)} builder state",
        lambda: state_start(
            shot.folder,
            getattr(layer, "id", m.id),
            list(getattr(layer, "judges", None) or [(m.frame, m.ref)]),
        ),
    )
    canon_verdicts: list = []
    canonical_replay_inputs: list = []
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
        selected_authority=selected_authority,
        attempt_guard=attempt_guard,
    )
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

                _events = (
                    (_load_unit_state(shot.folder, str(layer.id)).get("units") or {})
                    .get(str(active_unit.id), {})
                    .get("history")
                    or []
                )
                _reopen = _live_reopen_reason(_events)
            except (OSError, ValueError):
                _reopen = ""
            if _reopen:
                # One record, two consumers: the prompt block below explains the retry
                # to the model, and the phase arm unlocks mutation for it. Arming on the
                # reopen record is exact, not heuristic — the convergence guard only
                # bites when every executable row passes, so a unit that was still
                # terminal-failed and audibly reopened must have failed on evidence the
                # guard cannot observe (its canonical judgment).
                phase["judgment_unresolved"] = True
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
            unit_scope=unit_scope_card,
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
        attempt_guard.check(f"query unit {active_unit.id} live builder")
        await builder.query(_kickoff)
        _tools_before = sum(TOOL_USE.values())
        info = last_info = await _drain(
            builder,
            verbose,
            attempt_guard=attempt_guard,
        )
        attempt_guard.check(f"complete unit {active_unit.id} live builder")
        # A layer that spent nothing and touched no tool did not build anything, whatever
        # the result subtype claims. Caught here rather than after the critic, because the
        # next thing this function does is pay a vision model to look at an empty scene.
        _why = model_phase_failure(info, sum(TOOL_USE.values()) - _tools_before)
        if _why:
            log(f"✗ build DID NOTHING: {_why}")
            transcript.event("empty_success", why=_why, **info)
            ledger.mark(m, "failed", best=None)
            raise BuildTruncated(
                f"layer {m.id}: {_why}", terminal_cause="model_session_failure"
            )
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
                f"(${info['cost']:.2f}); raise MAX_BUDGET_USD or split the layer",
                terminal_cause=_budget_terminal_cause(info["subtype"]),
            )

        live_rounds = _live_round_budget(rounds, comparison_state)
        raster_required = _unit_requires_raster(
            shot,
            active_unit,
            selected_authority=selected_authority,
        )
        if not raster_required:
            log(
                "executable-only unit: evaluating typed scene/interface evidence "
                "without raster or visual critic",
                1,
            )
        if comparison_state.get("cannot_express"):
            payload = comparison_state["cannot_express"]
            log(
                "cannot_express_in_scope recorded during live build — skipping visual "
                "critique and revision rounds: "
                + ", ".join(payload.get("contract_ids") or []),
                1,
            )
        rnd = 0
        for rnd in range(1, live_rounds + 1):
            if raster_required:
                log(f"── round {rnd}/{rounds} — rendering + critiquing frame {m.frame} ──")
            else:
                log(f"── round {rnd}/{rounds} — executable evidence at frame {m.frame} ──")
            t_round = time.monotonic()
            render_rel = (
                builder_package()._stash_render(
                    session,
                    shot,
                    m,
                    f"r{rnd}",
                    mode=_unit_raster_mode(active_unit),
                )
                if raster_required
                else ""
            )
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
            evidence = builder_package()._render_evidence(
                shot,
                layer,
                m,
                render_rel,
                session,
                active_unit=active_unit,
                selected_authority=selected_authority,
            )
            attempt_guard.check(f"judge unit {active_unit.id} round {rnd}")
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
                layer=layer,
                selected_authority=selected_authority,
                attempt_guard=attempt_guard,
            )
            verdict["round_s"] = round(time.monotonic() - t_round, 1)
            convergence_stop = _evidence_convergence_stop(layer, verdict)
            if convergence_stop:
                verdict["convergence_stop"] = "authoritative_owned_evidence"
            ledger.record_round(m, kind="iter", index=rnd, render=render_rel, verdict=verdict)
            publish(
                f"record layer {getattr(layer, 'id', m.id)} round {rnd}",
                partial(
                    state_round,
                    shot.folder,
                    frame=m.frame,
                    mean=verdict["mean"],
                    passed=verdict["pass"],
                    scores=verdict.get("scores"),
                    issues=verdict.get("issues"),
                    approach=_APPROACH.get("text"),
                ),
            )
            # best-of-N: a valid round outranks an invalid round before aesthetic mean.
            # In particular, do not restore a contract-failing 4.0 over a later passing
            # 4.0 during finalize (the scene graph and pixels may differ independently).
            if _round_rank(verdict) > _round_rank(best.get("verdict")):
                best = {"mean": verdict["mean"], "round": rnd, "render": render_rel, "verdict": verdict, "snap": snap}
                if render_rel:
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
            # A fresh in-session verdict owns the mutation window from here: reset the
            # contract flag for one bounded revision, and retire the cross-session
            # reopen arm — the round discipline, not the operator's retry record, now
            # decides when mutation is legal.
            phase["scene_contracts_passed"] = False
            phase["judgment_unresolved"] = False
            if raster_required and plateaued and layer is not None and not reviewed:
                reviewed = True
                log(f"plateau ({verdict['mean']} ≤ prev) — escalating to APPROACH REVIEW")
                with costlog.scoped(
                    role="approach_reviewer",
                    phase="approach_review",
                    model=builder_package().Settings.from_environment(load_dotenv_file=False).reviewer_model,
                ):
                    attempt_guard.check(
                        f"review unit {active_unit.id} approach at round {rnd}"
                    )
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
                _revision_tools_before = sum(TOOL_USE.values())
                _revision_prior_cost = float(last_info.get("cost") or 0.0)
                attempt_guard.check(f"query unit {active_unit.id} reviewed revision")
                await builder.query(revision_from_review(layer, out))
            elif plateaued:
                log(f"plateau again after review ({verdict['mean']}) — stopping revisions")
                break
            else:
                _revision_tools_before = sum(TOOL_USE.values())
                _revision_prior_cost = float(last_info.get("cost") or 0.0)
                attempt_guard.check(f"query unit {active_unit.id} revision {rnd + 1}")
                await builder.query(revision_prompt(m, verdict, render_rel))
            last_info = await _drain(
                builder,
                verbose,
                attempt_guard=attempt_guard,
            )
            attempt_guard.check(f"complete unit {active_unit.id} revision {rnd + 1}")
            _why = model_phase_failure(
                last_info,
                sum(TOOL_USE.values()) - _revision_tools_before,
                prior_cost=_revision_prior_cost,
            )
            if _why:
                transcript.event("empty_success", phase="live_revision", why=_why, **last_info)
                raise BuildTruncated(
                    f"layer {m.id} live revision: {_why}",
                    terminal_cause="model_session_failure",
                )
            if comparison_state.get("cannot_express"):
                payload = comparison_state["cannot_express"]
                log(
                    "cannot_express_in_scope recorded during live revision — stopping "
                    "remaining critique rounds: "
                    + ", ".join(payload.get("contract_ids") or []),
                    1,
                )
                break
        if best.get("render"):
            log(f"best round: r{best['round']} mean {best['mean']} → renders/{m.id}_best.png")
        else:
            log(
                f"best round: r{best['round']} executable mean {best['mean']} "
                "(no raster owed)"
            )

        # finalize from the BEST round's scene, not the last one — a regressed revision
        # must not be what gets written into build/<m>.py (cost build8 the layer).
        if best.get("snap") and best["round"] != rnd:
            log(f"restoring best round r{best['round']} scene state before finalize")
            session.restore(best["snap"]["blend"])
            _restore_tools_before = sum(TOOL_USE.values())
            _restore_prior_cost = float(last_info.get("cost") or 0.0)
            attempt_guard.check(f"query unit {active_unit.id} restore acknowledgement")
            await builder.query(
                f"NOTE: the scene has been RESTORED to your round-{best['round']} state "
                f"(the best-scoring round, mean {best['mean']}) — your later revision "
                f"scored worse and was discarded. Inspect and acknowledge THIS restored "
                f"scene only. Do NOT write or edit {script_rel} yet: MODE is still "
                f"LIVE_BUILD, and the harness will send a separate FINALIZE_SCRIPT "
                f"request after it captures the journal."
            )
            last_info = await _drain(
                builder,
                verbose,
                attempt_guard=attempt_guard,
            )
            attempt_guard.check(f"complete unit {active_unit.id} restore acknowledgement")
            _why = model_phase_failure(
                last_info,
                sum(TOOL_USE.values()) - _restore_tools_before,
                prior_cost=_restore_prior_cost,
            )
            if _why:
                transcript.event(
                    "empty_success", phase="restore_acknowledgement", why=_why, **last_info
                )
                raise BuildTruncated(
                    f"layer {m.id} restore acknowledgement: {_why}",
                    terminal_cause="model_session_failure",
                )

        run_artifacts.ensure(shot.folder, command="build")
        candidate_path = exact_candidate_script_path(shot.folder, attempt_guard)
        candidate_script_rel = candidate_path.relative_to(shot.folder).as_posix()
        probe_ctx = await _persist_journal_and_finalize_script(
            shot,
            m,
            script_rel,
            candidate_script_rel,
            prior_paths,
            session,
            priors,
            raster_required,
            layer,
            active_unit,
            verbose,
            best,
            unit_journal_start,
            ledger,
            comparison_state,
            phase,
            attempt_guard,
            selected_authority=selected_authority,
        )


        skip_canonical = False
        if active_unit is not None:
            unpaid_cards = unpaid_image_contract_debts(
                image_contract_debt_cards(active_unit),
                load_image_contract_payment_rows(
                    shot.folder,
                    selected_authority=selected_authority,
                ),
            )
            refusal = freeze_refusal(unpaid_cards, comparison_state.get("cannot_express"))
            if refusal:
                log(
                    "✗ candidate freeze refused — unpaid image-contract debts "
                    + ", ".join(refusal["ids"]),
                    1,
                )
                comparison_state["cannot_express"] = {
                    "contract_ids": refusal["ids"],
                    "reason": refusal["reason"],
                    "classification": refusal["classification"],
                }
                skip_canonical = True
            elif (
                str((comparison_state.get("cannot_express") or {}).get("classification") or "")
                == UNPAID_IMAGE_DEBT
            ):
                log(
                    "unpaid_image_debt abstention recorded — skipping canonical "
                    "(no legal payer after freeze)",
                    1,
                )
                skip_canonical = True
        if skip_canonical:
            canonical = "failed"
        else:
            canonical = await _verify_script(
                shot,
                m,
                candidate_script_rel,
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
                out_replay_inputs=canonical_replay_inputs,
                authority_script_rel=script_rel,
                selected_authority=selected_authority,
                attempt_guard=attempt_guard,
            )

        canonical = await _run_canonical_repairs(
            shot,
            m,
            candidate_script_rel,
            prior_paths,
            session,
            axes,
            ledger,
            verbose,
            best,
            scope,
            layer,
            active_unit,
            comparison_state,
            skip_canonical,
            canonical,
            canon_verdicts,
            probe_ctx,
            raster_required,
            phase,
            passed,
            canonical_replay_inputs,
            script_rel,
            attempt_guard,
            selected_authority=selected_authority,
        )
        publish_candidate_script(
            shot.folder,
            candidate_path,
            script_rel,
            attempt_guard,
        )
    return await _publish_unit_outcome(
        shot,
        m,
        script_rel,
        prior_paths,
        session,
        layer,
        active_unit,
        publish_layer,
        report_layer,
        verbose,
        ledger,
        axes,
        best,
        passed,
        canonical,
        canon_verdicts,
        canonical_replay_inputs,
        comparison_state,
        t_layer,
        last_info,
        _look_actions,
        scope,
        attempt_guard,
        selected_authority=selected_authority,
    )
