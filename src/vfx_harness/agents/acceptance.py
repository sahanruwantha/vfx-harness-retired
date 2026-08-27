"""Stage 4 — acceptance: judge the FINISHED chain against the plan's approval moments.

The build stage judges each layer on the axes it owns, at one frame, mid-chain. That is
the right question for a build unit and the wrong one for the shot: an approval moment is
a whole frame produced by the CUMULATIVE chain, and it is only real once every layer has
run. So acceptance is judged exactly once, here, on the full rubric.

    python -m vfx_harness.agents.acceptance <shot-folder> [--moment M2]

Writes an `acceptance` block into shot.json next to `milestones`.
"""

from __future__ import annotations

import argparse
import time
from datetime import UTC

import anyio

from vfx_harness.domain.brief import Shot, load_shot
from vfx_harness.evidence.metrics import compare, look_pair, report
from vfx_harness.infrastructure.config import load_environment
from vfx_harness.observability import run_artifacts, transcript
from vfx_harness.observability.log import log
from vfx_harness.orchestration.ledger import Ledger, load_layers, load_milestones

from ..blender.session import BlenderSession
from .builder import _judge, _stash_render, _verdict, ensure_axes


class IncompleteChain(RuntimeError):
    """Acceptance was asked to judge a shot that is not finished."""


def _chain(session: BlenderSession, shot: Shot, *, force: bool = False) -> list[str]:
    """Run every layer script from an empty scene — the deliverable, start to finish.

    Refuses a PARTIAL chain. This used to log a missing script and carry on, so
    acceptance could pronounce on a shot that was never fully built — and then
    reconcile() would mark real layer verdicts `superseded_by_acceptance` on the
    strength of that partial render, corrupting good records with a bad judgement.
    """
    layers = sorted(load_layers(shot).values(), key=lambda g: g.script)
    ledger = Ledger(shot)
    missing = [f"layer {g.id} ({g.script}) has no script"
               for g in layers if not (shot.folder / g.script).is_file()]
    unpassed = [f"layer {g.id} is '{ledger.status(g.as_milestone())}'"
                for g in layers
                if (shot.folder / g.script).is_file()
                and ledger.status(g.as_milestone()) != "passed"]
    if (missing or unpassed) and not force:
        raise IncompleteChain(
            "refusing to judge an unfinished shot — " + "; ".join(missing + unpassed)
            + ". Finish those layers first, or pass --force (the verdict will not be "
              "about the deliverable).")
    if missing or unpassed:
        log(f"! --force: judging an INCOMPLETE chain — {'; '.join(missing + unpassed)}")

    from .builder import _RESET, _preamble
    session.run(_RESET)
    session.run(_preamble(shot))
    ran = []
    for g in layers:
        p = shot.folder / g.script
        if not p.is_file():
            continue                      # only reachable under --force
        log(f"chain: {g.script}", 1)
        session.run(p.read_text(encoding="utf-8"))
        ran.append(g.script)
    return ran


async def accept(shot: Shot, session: BlenderSession, only: str | None = None,
                 verbose: bool = True, force: bool = False,
                 repair: bool = False) -> dict:
    moments = load_milestones(shot)
    if only:
        moments = {k: v for k, v in moments.items() if k == only} or moments
    axes = await ensure_axes(shot, verbose)
    tpath = transcript.bind(shot.folder, "accept")
    if tpath:
        log(f"transcript → {tpath.relative_to(shot.folder)}", 1)
    ran = _chain(session, shot, force=force)
    log(f"chain rebuilt from empty: {len(ran)} scripts — judging {len(moments)} moment(s)")
    transcript.event("accept_start", moments=list(moments), chained=ran,
                     axes=[k for k, _ in axes])

    ledger = Ledger(shot)
    results: dict[str, dict] = {}
    passed_contract_evidence: set[tuple[str, str]] = set()
    for mid, m in moments.items():
        t0 = time.monotonic()
        log(f"── {mid} @ f{m.frame} vs {m.ref} ──")
        # The whole frame IS the subject here, so NO scope block: the full rubric applies.
        render_rel = _stash_render(session, shot, m, "accept")
        # DETERMINISTIC first. The critic never once mentioned that barrel_roll M1 was
        # 54% over-exposed against its own measured target; a number catches that for
        # free and grounds the critic's feedback in something checkable.
        deltas = []
        try:
            deltas = compare(*look_pair(str(shot.folder / render_rel),
                                        str(shot.folder / m.ref)))
            log(report(deltas), 1)
        except Exception as e:
            log(f"metrics skipped: {str(e)[:80]}", 1)
        extra = report(deltas) if deltas else ""
        blocking = [d for d in deltas if d.blocking]
        if blocking:
            # The verdict is already decided, so do not pay a model to restate it. 6 of 9
            # recorded acceptance calls were in exactly this position: metrics said
            # points_bot was 84% low, the critic was called anyway, and the moment failed
            # on the metric regardless. The deltas are also better feedback than prose.
            log(f"metrics decide this moment — skipping the critic "
                f"({len(blocking)} blocking)", 1)
            verdict = _verdict({"scores": {}, "issues": [str(d) for d in blocking[:4]]})
            verdict["pass"] = False
            verdict["decided_by"] = "metrics"
        else:
            # _judge, not _critique: acceptance is the last verdict anyone gets, so a
            # borderline call here is the worst place to trust a single noisy score.
            verdict = await _judge(shot, m, render_rel, axes, session, verbose,
                                   ("MEASURED GAPS vs the reference (objective, already "
                                    "computed — treat as fact):\n" + extra) if extra else None)
            verdict["decided_by"] = "critic"
        verdict["round_s"] = round(time.monotonic() - t0, 1)
        if m.fingerprint:
            verdict["fingerprint"] = m.fingerprint
        blocking = [str(d) for d in blocking]
        ok = verdict["pass"] and not blocking
        results[mid] = {"frame": m.frame, "ref": m.ref, "render": render_rel,
                        "mean": verdict["mean"], "pass": ok,
                        "critic_pass": verdict["pass"],
                        "decided_by": verdict.get("decided_by", "critic"),
                        "metric_failures": blocking,
                        "scores": verdict.get("scores", {}),
                        "issues": verdict.get("issues", [])[:4]}
        from vfx_harness.evidence.checks import acceptance_evidence

        contract_evidence = acceptance_evidence(
            shot.folder,
            frame=m.frame,
            render=render_rel,
        )
        results[mid]["contract_evidence"] = contract_evidence
        passed_contract_evidence.update(
            (str(row["source"]), str(row["id"]))
            for row in contract_evidence
            if row.get("pass") is True and row.get("authoritative") is True
        )
        verdict["pass"] = ok
        why = "" if not blocking else f"  (critic {verdict['mean']}, but {len(blocking)} metric(s) out of tolerance)"
        log(f"{mid}: mean {verdict['mean']} {'PASS ✅' if ok else 'FAIL ✗'}{why}")
        transcript.event("accept_moment", moment=mid, **results[mid],
                         seconds=verdict.get("round_s"))

    if not only:
        from vfx_harness.orchestration.plan_due import (
            require_due_clear,
            resolve_acceptance_completion,
        )

        resolve_acceptance_completion(
            shot.folder,
            passed_evidence=passed_contract_evidence,
        )
        require_due_clear(shot.folder, acceptance=True)

    ledger.data["acceptance"] = {
        "scripts": ran,
        "moments": results,
        "passed": sum(1 for r in results.values() if r["pass"]),
        "total": len(results),
    }
    ledger.save()
    if not only:                      # a partial run cannot judge the whole chain
        ledger.data["acceptance"]["superseded"] = reconcile(shot, results, ledger)
        plan = repair_plan(shot, results)
        ledger.data["acceptance"]["repair_plan"] = plan
        ledger.save()
        if plan:
            marked = apply_repair(shot, plan, ledger) if repair else []
            if not repair:
                log(f"! {len(plan)} failing moment group(s) route to layer(s) "
                    f"{', '.join(c['layer'] for c in plan)} — re-run with --repair to "
                    f"invalidate and rebuild them")
            ledger.data["acceptance"]["repaired"] = marked
            ledger.save()
    log(f"acceptance: {ledger.data['acceptance']['passed']}/{len(results)} moments passed "
        f"→ {ledger.path}")
    for mid, r in results.items():
        log(f"  {mid} f{r['frame']}: {r['mean']} {'✅' if r['pass'] else '✗'}", 1)
    transcript.event("accept_end", **{k: v for k, v in ledger.data["acceptance"].items()
                                      if k != "moments"})
    transcript.unbind()
    return ledger.data["acceptance"]


def reconcile(shot: Shot, results: dict, ledger: Ledger) -> list[str]:
    """Correct the record: a layer that PASSED while the moments it answers for FAILED.

    Nothing linked these before, so both verdicts sat in shot.json contradicting each
    other in silence — server_to_hansa's G50 passed at 3.75 on the one axis it owns while
    M3 (2.50) and M4 (2.20), the two moments in its own judge list, both failed. A layer
    verdict is a claim about a layer; an acceptance verdict is a claim about the frame
    that layer is responsible for. When they disagree, acceptance wins — it judged the
    finished chain.
    """
    layers = load_layers(shot)
    failed_frames = {r["frame"] for r in results.values() if not r["pass"]}
    notes = []
    for g in layers.values():
        claimed = {f for f, _ in g.judges}
        bad = sorted(claimed & failed_frames)
        if not bad:
            continue
        m = g.as_milestone()
        if ledger.status(m) != "passed":
            continue
        note = (f"layer {g.id} passed, but the moment(s) it answers for failed at "
                f"f{', f'.join(map(str, bad))} — superseded by acceptance")
        ledger._slot(m)["superseded_by_acceptance"] = {"frames": bad, "at": _now_str()}
        notes.append(note)
        log(f"! {note}")
    if notes:
        ledger.save()
    return notes


def _now_str() -> str:
    from datetime import datetime
    return datetime.now(UTC).isoformat(timespec="seconds")


# A repair loop that cannot stop is worse than none — it burns the budget re-running the
# same layer against the same failure. Two attempts per layer, and a round that improves
# nothing ends it.
MAX_REPAIR_ROUNDS = 2


def repair_plan(shot: Shot, results: dict) -> list[dict]:
    """Which layers must be rebuilt to fix the failing moments, earliest first.

    Acceptance used to END here: it wrote the verdict, marked contradicting layer
    verdicts superseded, and stopped. A shot could therefore complete the whole pipeline
    with failing moments recorded and nothing done about them — acceptance was a report,
    not a stage. `owns` already maps every axis to the layer responsible for it, so the
    routing was available all along; nothing consumed it.
    """
    from .builder import PASS_MIN

    layers = load_layers(shot)
    axis_owner: dict[str, str] = {}
    for g in layers.values():
        for ax in (g.owns or ()):
            # earliest owner wins: fixing the axis at its source is what unblocks the rest
            if ax not in axis_owner or _order(layers, g.id) < _order(layers, axis_owner[ax]):
                axis_owner[ax] = g.id

    culprits: dict[str, dict] = {}
    for mid, r in results.items():
        if r["pass"]:
            continue
        weak = sorted(ax for ax, v in (r.get("scores") or {}).items()
                      if isinstance(v, (int, float)) and not isinstance(v, bool)
                      and v <= PASS_MIN)
        for ax in weak:
            owner = axis_owner.get(ax)
            if not owner:
                continue
            slot = culprits.setdefault(owner, {"layer": owner, "axes": set(),
                                               "moments": set()})
            slot["axes"].add(ax)
            slot["moments"].add(mid)
        if not weak:
            log(f"! {mid} failed but no owned axis is at/below {PASS_MIN} "
                f"(metric failures: {len(r.get('metric_failures') or [])}) — "
                f"no layer to route it to", 1)

    plan = sorted(culprits.values(), key=lambda c: _order(layers, c["layer"]))
    for c in plan:
        c["axes"], c["moments"] = sorted(c["axes"]), sorted(c["moments"])
        # Everything above the repair root re-runs too: layer scripts chain, so rebuilding
        # layer 3 invalidates the judgements made on 4-8 that were stacked on top of it.
        c["invalidates"] = [g.id for g in layers.values()
                            if _order(layers, g.id) > _order(layers, c["layer"])]
    return plan


def _order(layers: dict, layer_id: str) -> int:
    keys = sorted(layers, key=lambda k: str(layers[k].script))
    return keys.index(layer_id) if layer_id in keys else 10_000


def apply_repair(shot: Shot, plan: list[dict], ledger: Ledger) -> list[str]:
    """Mark the repair root and everything downstream as needing a rebuild."""
    if not plan:
        return []
    layers = load_layers(shot)
    root = plan[0]
    touched = [root["layer"], *list(root["invalidates"])]
    marked = []
    for lid in touched:
        g = layers.get(lid)
        if g is None:
            continue
        m = g.as_milestone()
        slot = ledger._slot(m)
        n = int(slot.get("repair_rounds", 0))
        if lid == root["layer"] and n >= MAX_REPAIR_ROUNDS:
            log(f"! layer {lid} has already been repaired {n}x — not looping again; "
                f"this needs a human or a plan change")
            return marked
        if slot.get("status") == "passed":
            slot["status"] = "needs_repair"
        slot["repair_rounds"] = n + (1 if lid == root["layer"] else 0)
        slot["repair_reason"] = {
            "axes": root["axes"], "moments": root["moments"],
            "root": root["layer"], "at": _now_str()}
        marked.append(lid)
    ledger.save()
    log(f"repair routed → rebuild layer {root['layer']} "
        f"(owns {', '.join(root['axes'])}, failing {', '.join(root['moments'])}); "
        f"{len(marked) - 1} downstream layer(s) invalidated with it")
    return marked


async def _run(folder: str, only: str | None, blender: str, force: bool = False,
               repair: bool = False) -> None:
    shot = load_shot(folder)
    from vfx_harness.orchestration.plan_due import require_due_clear

    require_due_clear(
        shot.folder,
        acceptance=True,
        record_kinds=frozenset({"assumption"}),
    )
    session = BlenderSession(blender=blender, blend_file=None,
                             assets_dir=shot.folder / "assets",
                             cwd=shot.folder).start()
    try:
        await accept(shot, session, only=only, force=force, repair=repair)
    finally:
        session.close()


def main() -> None:
    load_environment()
    ap = argparse.ArgumentParser(
        description="Judge the finished chain against the plan's acceptance moments.")
    ap.add_argument("folder", help="shot folder (contains brief.md + acceptance.json)")
    ap.add_argument("--moment", default=None, help="judge only this moment (e.g. M2)")
    ap.add_argument("--blender", default="blender")
    ap.add_argument("--force", action="store_true",
                    help="judge even an incomplete chain (debugging only — the verdict "
                         "will not be about the deliverable)")
    ap.add_argument("--repair", action="store_true",
                    help="act on a failure: invalidate the layer that owns the failing "
                         "axis and everything downstream of it, so they rebuild")
    args = ap.parse_args()
    shot = load_shot(args.folder)
    with run_artifacts.invocation(shot.folder, "accept", shot_id=shot.id,
                                  parameters={"moment": args.moment}):
        try:
            anyio.run(_run, args.folder, args.moment, args.blender, args.force,
                      args.repair)
        except IncompleteChain as e:
            log(f"INCOMPLETE CHAIN — {e}")
            raise run_artifacts.RequestedExit(7, f"INCOMPLETE CHAIN — {e}") from None


if __name__ == "__main__":
    main()
