"""Stage 3 — the build + critic loop for one PLAN LAYER."""

from __future__ import annotations

import json
import json as _json
import shutil
import time
from pathlib import Path

from vfx_harness.agents.build_prompts import (
    canonical_repair_prompt,
    finalize_prompt,
)
from vfx_harness.agents.builder.axes import _layer_needs_motion, distill_recipe
from vfx_harness.agents.builder.critic_focus import (
    _canonical_failing_ids,
    _repair_action,
    _repair_change_summary,
    _repair_delta,
    _unsatisfiable_pair_findings,
)
from vfx_harness.agents.builder.evidence import _unit_evidence_ids_by_frame
from vfx_harness.agents.builder.models import (
    _TRUNCATED,
    MAX_CANON_REPAIRS,
    BuildTruncated,
    _budget_terminal_cause,
    builder_model,
    critic_model,
    script_model,
)
from vfx_harness.agents.builder.pkg import builder_package
from vfx_harness.agents.builder.prior import _run_script_agent
from vfx_harness.agents.builder.revalidate import _blender_version
from vfx_harness.agents.builder.state import _APPROACH, _ERRORS, _JOURNAL_INFO, _RECIPES_USED
from vfx_harness.agents.builder.verify import _verify_script
from vfx_harness.evaluation.plan_gate import _builder_render
from vfx_harness.evidence.checks import revalidate_layer
from vfx_harness.evidence.metrics import compare, look_pair, report
from vfx_harness.observability import costlog, run_artifacts, transcript
from vfx_harness.observability.log import (
    log,
    tool_use_summary,
)
from vfx_harness.observability.runid import RUN_ID
from vfx_harness.observability.runlog import snapshot_counts
from vfx_harness.observability.runlog import summary as run_summary
from vfx_harness.observability.runlog import write as write_run
from vfx_harness.orchestration.layer_plans import write_layer_outcome


def _metric_report(shot, render_rel: str, ref_rel: str) -> str:
    """Objective ref-deltas for the reviewer — technique problems show up as structural
    metrics (points, detail) rather than exposure."""
    try:

        d = compare(*look_pair(str(shot.folder / render_rel), str(shot.folder / ref_rel)))
        return report(d) if d else ""
    except Exception as e:
        log(f"! metric report unavailable for the review: {str(e)[:70]}", 1)
        return ""


async def _persist_journal_and_finalize_script(
    shot,
    m,
    script_rel,
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
):
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
            path=str(journal),
            start=unit_journal_start,
            limit=(best.get("snap") or {}).get("journal_index"),
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
    probe_judges = list(
        layer.judges if layer is not None else [(m.frame, m.ref)]
    )
    evidence_ids_by_frame = _unit_evidence_ids_by_frame(
        shot, layer, active_unit, probe_judges
    )
    probe_ctx = {
        "blender": session.blender,
        "scratch_dir": str(
            run_artifacts.ensure(shot.folder, command="build").scratch / "candidate-probe"
        ),
        "prior_paths": [str(path) for path in prior_paths],
        "judges": [(int(frame), str(ref)) for frame, ref in probe_judges],
        "layer_id": str(getattr(layer, "id", m.id)),
        "scope_mode": (
            str(active_unit.mutates.mode) if active_unit is not None else ""
        ),
        "roles": list(active_unit.mutates.roles) if active_unit is not None else [],
        "look_capabilities": list(
            getattr(active_unit, "look_capabilities", ()) or ()
        ),
        "raster_required": raster_required,
        "evidence_ids_by_frame": evidence_ids_by_frame,
        "image_stage": (
            "post_grade"
            if any(
                "grade" in str(axis).lower()
                for axis in (getattr(layer, "owns", ()) or ())
            )
            else "pre_grade"
        ),
        "comparison_state": comparison_state,
    }
    with costlog.scoped(role="finalizer", phase="finalize_script", model=script_model()):
        fin = await _run_script_agent(
            shot,
            mode="finalize",
            script_rel=script_rel,
            prompt=finalize_prompt(
                shot,
                m,
                priors=priors,
                script_rel=script_rel,
                journal_rel=journal_rel,
                raster_required=raster_required,
            ),
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
            f"raise MAX_BUDGET_USD or split the layer",
            terminal_cause=_budget_terminal_cause(fin["subtype"]),
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

    return probe_ctx


async def _publish_unit_outcome(
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
    comparison_state,
    t_layer,
    last_info,
    _look_actions,
    scope,
):
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
        abl = await builder_package()._ablate(shot, layer, prior_paths, script_rel, session)
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
        if builder_package().Settings.from_environment().distill_inline:
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
                    "reviewer": builder_package().Settings.from_environment(load_dotenv_file=False).reviewer_model,
                },
                # WHICH tools the builder reached for. The four layers that passed
                # barrel_roll called compare_frame 7-41 times; the one that failed
                # three times called it 3-5 and measured 17-44 instead. Recovering
                # that took grepping a console log that no longer exists.
                "tools": tool_use_summary(
                    motion_owned=_layer_needs_motion(layer),
                    automatic_scene_checks=int(snapshot_counts().get("automatic_scene_contract_probe", 0)),
                    look_feedback_applicable=_look_actions,
                ),
            },
        )

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


async def _run_canonical_repairs(
    shot,
    m,
    script_rel,
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
):
    # Repair rounds, bounded. The target here is the SCRIPT's output from an empty
    # scene — not the live scene the builder has been tuning, which is why it must be
    # re-verified canonically or the loop would keep re-passing live and failing here.
    rejected_repairs: list[str] = []
    for attempt in range(1, MAX_CANON_REPAIRS + 1):
        if skip_canonical:
            break
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
        unsat = _unsatisfiable_pair_findings(shot, _canonical_failing_ids(canon_verdicts or []))
        if unsat and not comparison_state.get("cannot_express"):
            comparison_state["cannot_express"] = {
                "contract_ids": sorted({
                    pair["schedule_id"] for pair in unsat
                } | {pair["smoothness_id"] for pair in unsat}),
                "reason": unsat[0]["message"],
            }
        if comparison_state.get("cannot_express"):
            payload = comparison_state["cannot_express"]
            log(
                "bound contracts cannot be satisfied inside this unit — skipping "
                f"canonical repair {attempt}/{MAX_CANON_REPAIRS}: "
                + str(payload.get("reason") or payload.get("contract_ids")),
                1,
            )
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
                        raster_required=raster_required,
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
        if comparison_state.get("cannot_express"):
            shutil.copyfile(backup, shot.folder / script_rel)
            payload = comparison_state["cannot_express"]
            log(
                "cannot_express_in_scope recorded — restored pre-repair script and "
                "stopped remaining repairs: "
                + ", ".join(payload.get("contract_ids") or []),
                1,
            )
            canon_verdicts.clear()
            canon_verdicts.extend(pre_verdicts)
            canonical = pre_canonical
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
    if comparison_state.get("cannot_express"):
        ledger._slot(m)["cannot_express"] = dict(comparison_state["cannot_express"])
    return canonical
