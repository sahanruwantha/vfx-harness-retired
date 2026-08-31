"""Stage 2 — the PLAN harness."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

import anyio

from vfx_harness.agents.planner.generate import generate_layer_plan
from vfx_harness.agents.planner.pkg import planner_package
from vfx_harness.agents.planner.planning_stop import publish_global_plan_gate_stop
from vfx_harness.agents.planner.types import (
    PlanGateFailure,
    PlanLoopResult,
)
from vfx_harness.agents.resilience import AgentSessionFailure
from vfx_harness.evaluation import plan_gate
from vfx_harness.observability import run_artifacts
from vfx_harness.observability.log import log
from vfx_harness.orchestration import plan_authority
from vfx_harness.orchestration.layer_plans import (
    global_plan_path,
    is_selected_bundle_member,
    validate_work_unit_plan_authority,
)
from vfx_harness.orchestration.plan_authority import prepare_staging, promote_candidate


async def generate_plan_two_pass(
    folder: str | Path,
    *,
    draft_model: str | None = None,
    verify_model: str | None = None,
    blender: str = "blender",
    max_turns: int = 100,
    tag: str | None = None,
    verify_only: bool = False,
    workspace: str | Path | None = None,
) -> Path:
    """The standard flow: draft from scratch, then adversarially verify.
    Keeps the draft (plan.<tag->draft.md + its lab) as the audit trail."""
    shot = planner_package().load_shot(folder)
    layout = run_artifacts.ensure(shot.folder, command="plan")
    if workspace is None:

        workspace = prepare_staging(layout)
    workspace = Path(workspace).resolve()
    configured_settings = planner_package().Settings.from_environment(load_dotenv_file=False)
    configured = configured_settings.planner_model
    draft_model = draft_model or configured
    verify_model = verify_model or configured
    dtag = f"{tag}-draft" if tag else "draft"
    draft_path = global_plan_path(workspace).with_name(f"global.{dtag}.md")

    if verify_only:
        if not draft_path.is_file():
            raise FileNotFoundError(f"--verify-only needs an existing {draft_path.name}")
        log(f"two-pass: reusing existing draft {draft_path.name}")
    else:
        log(f"══ two-pass 1/2 · DRAFT · {draft_model} ══")
        await planner_package().generate_plan(
            folder,
            model=draft_model,
            blender=blender,
            max_turns=max_turns,
            tag=dtag,
            workspace=workspace,
        )

    # VERIFY audits the immutable draft path, while its write contract and run_gate tool
    # operate on plans/global.md. Seed that canonical candidate with the exact draft bytes
    # so the verifier's first gate measures the artifact it was assigned instead of
    # reporting a synthetic "global.md missing" blocker.
    verify_candidate = global_plan_path(workspace)
    verify_candidate.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(draft_path, verify_candidate)
    log(f"══ two-pass 2/2 · VERIFY · {verify_model} · auditing {draft_path.name} ══")
    verify_turns = min(
        max_turns,
        getattr(configured_settings, "plan_verify_max_turns", 6),
    )
    try:
        final = await planner_package().generate_plan(
            folder,
            model=verify_model,
            blender=blender,
            max_turns=verify_turns,
            tag=tag,
            verify_draft=draft_path.relative_to(workspace).as_posix(),
            workspace=workspace,
        )
    except AgentSessionFailure as failure:
        if failure.terminal_cause != "max_turns_exhausted":
            raise
        # The until-clean loop converges against the DETERMINISTIC gate, not the verify
        # critique. An audit that exhausts its budget must not discard a viable draft:
        # the canonical candidate was seeded from the exact draft bytes above, any
        # artifact repairs the dying verifier landed are on disk, and the gate re-measures
        # all of it from scratch. Runs 1c18c2, 7040c2, and 270652 each aborted here with a
        # converging candidate (27→7 findings in 270652) the repair rounds never saw.
        log(
            f"! verify exhausted its {verify_turns}-turn budget — handing the on-disk "
            f"candidate to the deterministic gate and repair rounds instead of discarding it"
        )
        final = verify_candidate
    log(f"two-pass complete → {final.name} (draft kept: {draft_path.name})")
    return final


async def generate_plan_until_clean(
    folder: str | Path,
    *,
    draft_model: str | None = None,
    verify_model: str | None = None,
    blender: str = "blender",
    max_turns: int = 100,
    tag: str | None = None,
    verify_only: bool = False,
    max_rounds: int = 3,
) -> PlanLoopResult:
    """Draft → verify → GATE → repair → gate → … until the plan clears or stops moving.

    The loop exists because "solid" was previously a model's own opinion of its own work,
    and every mechanism built to make it more than that was self-certified: the planner
    marked `[unknown]` zero times across four documents (which is what gates research), it
    never once called `ask_supervisor`, and the verify pass — whose instruction #2 is
    "measure-check every approval still" — shipped seven fingerprints that do not
    reproduce. So the thing this loop converges against is a DETERMINISTIC gate over the
    artifacts on disk, not another critique pass. A model cannot talk its way past it.

    Three ways to stop, and two of them are failures that must be reported as failures:

      clean    no blocking findings — the plan aims at things that are actually there
      stalled  a round produced the SAME findings as the one before it. The repair pass
               is not converging, and paying for another identical answer helps nobody
      budget   max_rounds reached with findings outstanding

    Returns the plan path and deterministic terminal outcome either way. The caller keeps
    the dirty plan as useful evidence but must publish a non-zero terminal result; what
    must never happen is a dirty plan looking clean.
    """

    configured = planner_package().Settings.from_environment(load_dotenv_file=False).planner_model
    draft_model = draft_model or configured
    verify_model = verify_model or configured

    shot = planner_package().load_shot(folder)
    layout = run_artifacts.ensure(shot.folder, command="plan")
    workspace = plan_authority.prepare_staging(layout)
    final = await generate_plan_two_pass(
        folder,
        draft_model=draft_model,
        verify_model=verify_model,
        blender=blender,
        max_turns=max_turns,
        tag=tag,
        verify_only=verify_only,
        workspace=workspace,
    )
    plan_name = final.relative_to(workspace).as_posix()

    prev_sig, outcome = None, "budget"
    for rnd in range(1, max_rounds + 1):
        # New plans use the current five-artifact contract.  The standalone gate keeps
        # missing scene checks warning-only for legacy shots, but a planner running now
        # must not claim CLEAN while leaving numeric scene facts to the vision judge.
        res = plan_gate.run(workspace, plan_name, require_scene_checks=True)
        res.shot = shot.id
        log(f"══ gate {rnd}/{max_rounds} ══")
        log(plan_gate.report(res), 1)
        if res.clean:
            outcome = res.publishable_outcome
            break
        sig = res.signature()
        if sig == prev_sig:
            outcome = "stalled"
            log(
                f"! gate findings are unchanged from round {rnd - 1} — the repair pass is "
                f"not converging. Stopping rather than paying for the same answer again."
            )
            break
        prev_sig = sig
        # The repair input is immutable evidence owned by this run. Shot-global round names
        # let a later invocation overwrite the only record of what an earlier repair saw.
        snap = plan_authority.snapshot_repair_input(layout, rnd, final)
        snap_rel = snap.relative_to(shot.folder).as_posix()
        log(
            f"══ repair {rnd}/{max_rounds} · {verify_model} · {len(res.blocking)} "
            f"blocking finding(s) → {snap_rel} ══"
        )
        final = await planner_package().generate_plan(
            folder,
            model=verify_model,
            blender=blender,
            max_turns=max_turns,
            tag=tag,
            verify_draft=str(snap),
            repair=(plan_gate.feedback(res), rnd),
            workspace=workspace,
        )
        plan_name = final.relative_to(workspace).as_posix()
    else:
        res = plan_gate.run(workspace, plan_name, require_scene_checks=True)
        res.shot = shot.id
        log("══ gate (final) ══")
        log(plan_gate.report(res), 1)
        outcome = res.publishable_outcome if res.clean else "budget"

    n = len(res.blocking)
    report_path = layout.write_report("plan_gate", res.to_dict(outcome=outcome))
    log(f"gate authority → {report_path.relative_to(shot.folder)}", 1)
    published_pointer = published_bundle = published_hash = None
    if outcome in {"clean", "clean_with_assumptions", "clean_with_deferred"} and tag is None:
        bundle = plan_authority.publish_current(
            shot.folder,
            layout,
            outcome=outcome,
            plan_path=final,
            source_root=workspace,
        )
        pointer_rel = plan_authority.POINTER.as_posix()
        published_pointer = pointer_rel
        published_bundle = bundle.root.relative_to(shot.folder).as_posix()
        published_hash = bundle.content_hash
        layout.terminal_metadata.update(
            {
                "plan_pointer": pointer_rel,
                "plan_bundle": bundle.root.relative_to(shot.folder).as_posix(),
                "plan_content_hash": bundle.content_hash,
            }
        )
        log(f"plan authority → {pointer_rel} ({bundle.content_hash[:16]})", 1)
    elif outcome in {"clean", "clean_with_assumptions", "clean_with_deferred"} and tag is not None:
        log("tagged plan is gated evidence only; it does not replace plans/current.json", 1)
    log(
        f"plan loop {outcome.upper()}: {final.name}"
        + (
            ""
            if outcome in {"clean", "clean_with_assumptions", "clean_with_deferred"}
            else f" — {n} blocking finding(s) REMAIN. `vfx evals plan {shot.folder}` lists "
            f"them; building on this plan means building toward them."
        )
    )
    loop_result = PlanLoopResult(
        final,
        outcome,
        n,
        plan_pointer=published_pointer,
        plan_bundle=published_bundle,
        plan_content_hash=published_hash,
    )
    if not loop_result.clean:
        envelope = publish_global_plan_gate_stop(layout, loop_result)
        loop_result = PlanLoopResult(
            final,
            outcome,
            n,
            plan_pointer=published_pointer,
            plan_bundle=published_bundle,
            plan_content_hash=published_hash,
            stop_envelope=envelope,
        )
    return loop_result


def main() -> None:
    settings = planner_package().Settings.from_environment()
    ap = argparse.ArgumentParser(description="Plan a shot. Default: two-pass (draft → adversarial verify).")
    ap.add_argument("folder", help="shot folder (contains brief.md, refs/)")
    ap.add_argument("--layer", help="generate only this layer's just-in-time plan")
    ap.add_argument("--unit", help="with --layer, generate this ready work unit instead of the first ready unit")
    ap.add_argument(
        "--rematerialize",
        action="store_true",
        help="with --layer, replace the layer's materialized view and design it again "
        "from global authority; apply_replan preserves units whose digests still match",
    )
    ap.add_argument(
        "--discard-accepted",
        action="store_true",
        help="with --rematerialize, permit retiring accepted orphans and wiping state "
        "when the replan base is unusable; matching digests still stay through apply_replan",
    )
    ap.add_argument("--owner", help="authority applying a --rematerialize transaction")
    ap.add_argument("--trigger", help="why the materialized view is being replaced")
    ap.add_argument(
        "--evidence",
        action="append",
        default=[],
        help="evidence locator for --rematerialize; repeat for each item",
    )
    ap.add_argument("--single", action="store_true", help="one from-scratch pass with --model (no verify)")
    ap.add_argument(
        "--verify-only", action="store_true", help="skip drafting; audit the existing plans/global.<tag->draft.md"
    )
    ap.add_argument("--model", default=settings.planner_model, help="model for --single or --layer runs")
    ap.add_argument("--draft-model", default=settings.planner_model)
    ap.add_argument("--verify-model", default=settings.planner_model)
    ap.add_argument(
        "--blender", default=settings.blender_bin, help="blender executable for the spike lab"
    )
    ap.add_argument(
        "--max-turns", type=int, default=None, help="turn cap per pass (default: 24 for --layer, 100 globally)"
    )
    ap.add_argument("--tag", default=None,
                    help="isolate plan output and its active-run plan-lab artifacts")
    ap.add_argument(
        "--until-clean",
        action="store_true",
        help="after the two passes, run the deterministic plan gate and "
        "repair until it clears, stalls, or hits --max-rounds",
    )
    ap.add_argument("--max-rounds", type=int, default=3, help="repair rounds for --until-clean (default 3)")
    ap.add_argument(
        "--promote-run",
        metavar="RUN_ID",
        help="model-free: revalidate and publish a retained gate-clean planning candidate",
    )
    args = ap.parse_args()

    if args.unit and not args.layer:
        ap.error("--unit requires --layer")
    if args.layer and (args.single or args.verify_only or args.until_clean or args.tag):
        ap.error("--layer is a dedicated JIT pass; do not combine it with global-pass flags")
    if args.rematerialize and not (args.layer and args.owner and args.trigger and args.evidence):
        ap.error(
            "--rematerialize replaces published authority: it needs --layer, --owner, "
            "--trigger, and at least one --evidence"
        )
    if args.promote_run and (
        args.layer or args.single or args.verify_only or args.until_clean or args.tag
    ):
        ap.error("--promote-run is a dedicated model-free transaction")

    shot = planner_package().load_shot(args.folder)
    command = "plan-layer" if args.layer else ("plan-promote" if args.promote_run else "plan")
    loop_result: PlanLoopResult | None = None
    with run_artifacts.invocation(shot.folder, command, shot_id=shot.id) as layout:
        if args.promote_run:

            bundle, gate_result, workspace = promote_candidate(
                shot.folder, args.promote_run, layout
            )
            plan_path = workspace / "plans" / "global.md"
            loop_result = PlanLoopResult(
                plan_path,
                gate_result.publishable_outcome,
                0,
                plan_pointer="plans/current.json",
                plan_bundle=bundle.root.relative_to(shot.folder).as_posix(),
                plan_content_hash=bundle.content_hash,
            )
            log(f"promoted candidate from run {args.promote_run}")
            log(f"plan authority → plans/current.json ({bundle.content_hash[:16]})", 1)
        elif args.layer:
            plan_path = anyio.run(
                lambda: generate_layer_plan(
                    args.folder,
                    args.layer,
                    unit_id=args.unit,
                    model=args.model,
                    blender=args.blender,
                    max_turns=args.max_turns or max(24, settings.plan_max_turns // 4),
                    rematerialize=(
                        (args.owner, args.trigger, list(args.evidence), args.discard_accepted)
                        if args.rematerialize
                        else None
                    ),
                )
            )
        elif args.single:
            plan_path = anyio.run(
                lambda: planner_package().generate_plan(
                    args.folder, model=args.model, blender=args.blender,
                    max_turns=args.max_turns or settings.plan_max_turns, tag=args.tag
                )
            )
        elif args.until_clean:
            loop_result = anyio.run(
                lambda: planner_package().generate_plan_until_clean(
                    args.folder,
                    draft_model=args.draft_model,
                    verify_model=args.verify_model,
                    blender=args.blender,
                    max_turns=args.max_turns or settings.plan_max_turns,
                    tag=args.tag,
                    verify_only=args.verify_only,
                    max_rounds=args.max_rounds,
                )
            )
            plan_path = loop_result.path
        else:
            plan_path = anyio.run(
                lambda: generate_plan_two_pass(
                    args.folder,
                    draft_model=args.draft_model,
                    verify_model=args.verify_model,
                    blender=args.blender,
                    max_turns=args.max_turns or settings.plan_max_turns,
                    tag=args.tag,
                    verify_only=args.verify_only,
                )
            )
        if args.layer:
            log(f"unit plan ready: {plan_path}")
            validate_work_unit_plan_authority(shot.folder, plan_path)
            if is_selected_bundle_member(shot.folder, plan_path):
                log("provenance → verified member of the selected immutable bundle")
            else:
                log(f"provenance → {plan_path.with_name(plan_path.name + '.authority.json')}")
        else:
            log(f"wrote {plan_path}")
            if loop_result is not None and loop_result.clean:
                log("provenance → content-addressed member of the published plan bundle")
            else:
                log("provenance → not published for this unaccepted candidate")
        if loop_result is not None and not loop_result.clean:
            envelope = loop_result.stop_envelope or publish_global_plan_gate_stop(
                layout,
                loop_result,
            )
            raise PlanGateFailure(loop_result, stop_envelope=envelope)
        if loop_result is not None:
            layout.terminal_metadata.update(
                {
                    "outcome": loop_result.outcome,
                    "blocking_count": loop_result.blocking_count,
                    "plan_gate_report": "reports/plan_gate.json",
                    **({"plan_pointer": loop_result.plan_pointer} if loop_result.plan_pointer else {}),
                    **({"plan_bundle": loop_result.plan_bundle} if loop_result.plan_bundle else {}),
                    **(
                        {"plan_content_hash": loop_result.plan_content_hash}
                        if loop_result.plan_content_hash
                        else {}
                    ),
                }
            )
