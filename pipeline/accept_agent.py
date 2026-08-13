"""Stage 4 — acceptance: judge the FINISHED chain against the plan's approval moments.

The build stage judges each gate on the axes it owns, at one frame, mid-chain. That is
the right question for a build unit and the wrong one for the shot: an approval moment is
a whole frame produced by the CUMULATIVE chain, and it is only real once every gate has
run. So acceptance is judged exactly once, here, on the full rubric.

    python -m pipeline.accept_agent <shot-folder> [--moment M2]

Writes an `acceptance` block into shot.json next to `milestones`.
"""

from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

import anyio

from .blender.session import BlenderSession
from .blender.tools import build_blender_tools
from .brief import Shot, load_shot
from .build_agent import _critique, _gate, _stash_render, ensure_axes
from .ledger import Ledger, Milestone, load_gates, load_milestones
from .metrics import compare, look_vector, report
from .log import log


def _chain(session: BlenderSession, shot: Shot) -> list[str]:
    """Run every gate script from an empty scene — the deliverable, start to finish."""
    from .build_agent import _RESET, _preamble
    session.run(_RESET)
    session.run(_preamble(shot))
    ran = []
    for g in sorted(load_gates(shot).values(), key=lambda g: g.script):
        p = shot.folder / g.script
        if not p.is_file():
            log(f"! {g.script} missing — gate {g.id} never produced a script", 1)
            continue
        log(f"chain: {g.script}", 1)
        session.run(p.read_text(encoding="utf-8"))
        ran.append(g.script)
    return ran


async def accept(shot: Shot, session: BlenderSession, only: str | None = None,
                 verbose: bool = True) -> dict:
    moments = load_milestones(shot)
    if only:
        moments = {k: v for k, v in moments.items() if k == only} or moments
    axes = await ensure_axes(shot, verbose)
    ran = _chain(session, shot)
    log(f"chain rebuilt from empty: {len(ran)} scripts — judging {len(moments)} moment(s)")

    ledger = Ledger(shot)
    results: dict[str, dict] = {}
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
            deltas = compare(look_vector(str(shot.folder / render_rel)),
                             look_vector(str(shot.folder / m.ref)))
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
            verdict = _gate({"scores": {}, "issues": [str(d) for d in blocking[:4]]})
            verdict["pass"] = False
            verdict["decided_by"] = "metrics"
        else:
            verdict = await _critique(shot, m, render_rel, axes, session, verbose,
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
        verdict["pass"] = ok
        why = "" if not blocking else f"  (critic {verdict['mean']}, but {len(blocking)} metric(s) out of tolerance)"
        log(f"{mid}: mean {verdict['mean']} {'PASS ✅' if ok else 'FAIL ✗'}{why}")

    ledger.data["acceptance"] = {
        "scripts": ran,
        "moments": results,
        "passed": sum(1 for r in results.values() if r["pass"]),
        "total": len(results),
    }
    ledger.save()
    if not only:                      # a partial run cannot judge the whole chain
        ledger.data["acceptance"]["superseded"] = reconcile(shot, results, ledger)
        ledger.save()
    log(f"acceptance: {ledger.data['acceptance']['passed']}/{len(results)} moments passed "
        f"→ {ledger.path}")
    for mid, r in results.items():
        log(f"  {mid} f{r['frame']}: {r['mean']} {'✅' if r['pass'] else '✗'}", 1)
    return ledger.data["acceptance"]


def reconcile(shot: Shot, results: dict, ledger: Ledger) -> list[str]:
    """Correct the record: a gate that PASSED while the moments it answers for FAILED.

    Nothing linked these before, so both verdicts sat in shot.json contradicting each
    other in silence — server_to_hansa's G50 passed at 3.75 on the one axis it owns while
    M3 (2.50) and M4 (2.20), the two moments in its own judge list, both failed. A gate
    verdict is a claim about a layer; an acceptance verdict is a claim about the frame
    that layer is responsible for. When they disagree, acceptance wins — it judged the
    finished chain.
    """
    gates = load_gates(shot)
    failed_frames = {r["frame"] for r in results.values() if not r["pass"]}
    notes = []
    for g in gates.values():
        claimed = {f for f, _ in g.judges}
        bad = sorted(claimed & failed_frames)
        if not bad:
            continue
        m = g.as_milestone()
        if ledger.status(m) != "passed":
            continue
        note = (f"gate {g.id} passed, but the moment(s) it answers for failed at "
                f"f{', f'.join(map(str, bad))} — superseded by acceptance")
        ledger._slot(m)["superseded_by_acceptance"] = {"frames": bad, "at": _now_str()}
        notes.append(note)
        log(f"! {note}")
    if notes:
        ledger.save()
    return notes


def _now_str() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def _run(folder: str, only: str | None, blender: str) -> None:
    shot = load_shot(folder)
    session = BlenderSession(blender=blender, blend_file=None,
                             assets_dir=shot.folder / "assets",
                             cwd=shot.folder).start()
    try:
        await accept(shot, session, only=only)
    finally:
        session.close()


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Judge the finished chain against the plan's acceptance moments.")
    ap.add_argument("folder", help="shot folder (contains brief.md + acceptance.json)")
    ap.add_argument("--moment", default=None, help="judge only this moment (e.g. M2)")
    ap.add_argument("--blender", default="blender")
    args = ap.parse_args()
    anyio.run(_run, args.folder, args.moment, args.blender)


if __name__ == "__main__":
    main()
