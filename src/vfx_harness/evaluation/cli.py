"""The evaluation harness — the thing that decides whether a change to this pipeline
actually helped.

    python -m vfx_harness.evaluation.cli freeze   shots/barrel_roll --label before-context-editing
    python -m vfx_harness.evaluation.cli check    shots/barrel_roll            # free, no model
    python -m vfx_harness.evaluation.cli plan                                  # free, gate the plan
    python -m vfx_harness.evaluation.cli grounding                             # free, plan vs plates
    python -m vfx_harness.evaluation.cli checks                                # free, Blender only
    python -m vfx_harness.evaluation.cli variance shots/barrel_roll --n 6      # cheap, real critic
    python -m vfx_harness.evaluation.cli blank    shots/barrel_roll --layer 5  # cheap, real critic
    python -m vfx_harness.evaluation.cli compare  barrel_roll:latest artifacts/evaluations/baselines/.../x.json
    python -m vfx_harness.evaluation.cli panels                                # free, from ledgers
    python -m vfx_harness.evaluation.cli list

Why this exists, in one paragraph. Every claim about this pipeline so far has rested on
one person reading one run, and in a single session that produced five confident findings
that all had to be retracted — a truncation hypothesis, an acceptance thesis, a "finish
doesn't reproduce" claim, a layer-ownership assignment, and a "detail 46% high" that was
an artifact of the measurer's own image upscaling. That last one matters most: the
instrument was wrong and nothing existed to catch it. Two measurements from the same
session set the bar for what this harness has to be able to reproduce: the same render
scored against the same reference on one axis returned 4.0, 3.0, 3.0, 2.0 across four
repeats (a 2-point spread that flipped the verdict), and a recipe passed verification for
months because its function was never called.

The design rule throughout: an eval that reports a confident number it cannot support is
worse than no eval, because it is exactly the failure mode it exists to prevent. So
proxies are labelled as proxies IN THE OUTPUT, skipped checks say why they were skipped,
and a difference that cannot be distinguished from noise is reported as noise rather than
as a delta.

Exit codes:  0 ok · 1 usage/IO error · 3 a deterministic check FAILED
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
import tempfile
from collections import Counter
from pathlib import Path

from vfx_harness.blender.session import BlenderError, BlenderSession
from vfx_harness.domain.brief import load_shot
from vfx_harness.evaluation import BASELINES, VARIANCE
from vfx_harness.evaluation import baseline as _baseline
from vfx_harness.evaluation import blank as _blank
from vfx_harness.evaluation import compare as _compare
from vfx_harness.evaluation import grounding as _grounding
from vfx_harness.evaluation import plan_gate as _pg
from vfx_harness.evaluation import variance as _variance
from vfx_harness.infrastructure.config import load_environment
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration.plan_authority import (
    POINTER,
    PlanPublicationError,
    prepare_consumer_view,
    resolve_current,
)

from .determinism import Result, metric_scale_consistency, replay_equivalence
from .integrity import artifact_integrity


def _cmd_checks(argv: list[str]) -> int:
    """Phase 1 gate: every judgment-free check must FIRE on a known-bad scene.

    A check nobody has seen fail is not a check. This boots a throwaway headless
    Blender, builds one deliberately broken fixture per check, and fails if any of
    them stays silent — which is the only way to earn the right to trust a green one.
    """
    ap = argparse.ArgumentParser(prog="vfx_harness.evaluation.cli checks")
    ap.add_argument("--blender", default="blender")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    session = BlenderSession(blender=args.blender).start()
    try:
        res = session.check("self_test")
    except BlenderError as e:
        print(f"✗ the check fixtures could not run: {e}")
        return 3
    finally:
        session.close()

    if args.json:
        print(json.dumps(res, indent=2))
        return 0 if res.get("gate", {}).get("ok") else 3

    gate = res.get("gate", {})
    print("\n── Phase 1 checks · known-bad fixtures ──")
    for kind, d in res.items():
        if kind == "gate":
            continue
        fired = d.get("fired")
        mark = "✓" if fired else "✗"
        detail = "; ".join(d.get("issues") or []) or "(no issues reported)"
        print(f"  {mark} {kind:<12} {'fired on the bad scene' if fired else 'STAYED SILENT'}"
              f"  — {detail}")
    if gate.get("ok"):
        print("\nEvery check fired on its fixture. A green result from these is now "
              "evidence rather than an assumption.")
        return 0
    print(f"\n{len(gate.get('silent', []))} check(s) stayed silent: "
          f"{', '.join(gate.get('silent', []))}. Do NOT trust those checks — a check "
          f"that cannot see a defect built to trip it will not see a real one.")
    return 3


def _cmd_check(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="vfx_harness.evaluation.cli check")
    ap.add_argument("folder", help="shot folder")
    ap.add_argument("--blender", default="blender")
    ap.add_argument("--skip-replay", action="store_true",
                    help="skip the Blender replay (the only slow check)")
    ap.add_argument("--replay-passes", type=int, default=2)
    ap.add_argument("--replay-scale", type=float, default=0.5)
    ap.add_argument("--image", action="append", default=None,
                    help="image for the metric self-consistency sweep (repeatable; "
                         "default: every reference plus every judged render)")
    ap.add_argument("--json", action="store_true", help="machine-readable results")
    args = ap.parse_args(argv)

    shot = load_shot(args.folder)
    results = [artifact_integrity(shot)]

    if args.image:
        images = [shot.folder / i for i in args.image]
    else:
        # Refs AND judged renders: the two populations the metric is ever applied to.
        # Anything already below the delivery resolution is dropped by the checker with
        # a note — a stashed 960x480 render cannot play the full-quality control.
        rdir = run_artifacts.readable_renders_dir(shot.folder)
        # motion strips are montages of several frames — a horizontal join is not a
        # frame, and the banded metrics would be measuring the seams.
        renders = sorted(p for p in rdir.glob("*.png")
                         if "_motion" not in p.name) if rdir.is_dir() else []
        images = list(shot.refs) + renders
    # The shot's own delivery resolution defines what `scale` is a fraction OF. Without
    # it the sweep invents scales the pipeline never renders at and reports the artifact
    # it finds there as though the builder were living with it — which is exactly the
    # mistake the first version of this check made.
    results.append(metric_scale_consistency(images, delivery=shot.resolution))

    if args.skip_replay:
        results.append(Result("replay equivalence", ok=None,
                              detail="--skip-replay was passed"))
    else:
        results.append(replay_equivalence(shot, blender=args.blender,
                                          passes=args.replay_passes,
                                          scale=args.replay_scale))

    if args.json:
        print(json.dumps([r.as_dict() for r in results], indent=2))
    else:
        print(f"\n── deterministic checks · {shot.id} ──")
        for r in results:
            print(r)
        n_fail = sum(1 for r in results if r.ok is False)
        n_skip = sum(1 for r in results if r.ok is None)
        print(f"\n{len(results) - n_fail - n_skip} passed · {n_fail} FAILED · "
              f"{n_skip} skipped")
        if n_fail:
            print("A failure here is a finding about the repo, not about the harness. "
                  "Read the detail above before changing anything.")
    return 3 if any(r.ok is False for r in results) else 0


def _cmd_compare(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="vfx_harness.evaluation.cli compare")
    ap.add_argument("a", help="baseline A (path, <shot>/<name>.json, or <shot>:latest)")
    ap.add_argument("b", help="baseline B")
    ap.add_argument("--margin", type=float, default=0.10,
                    help="non-inferiority margin on final task success (default 0.10)")
    args = ap.parse_args(argv)
    a, b = _baseline.load(args.a), _baseline.load(args.b)
    # Measured judge noise, if anyone ever measured it for this shot. Without it, score
    # movements below cannot be called signal or noise, and the report says exactly that
    # rather than picking one.
    v = _variance.latest(a["shot"])
    noise = v["mean"]["spread"] if v and v.get("mean", {}).get("n", 0) > 1 else None
    if v:
        print(f"(measured judge noise, from {v['at']}: n={v['n']}, scope {v['scope']}, "
              f"{v['render']} — mean spread {v['mean']['spread']}. One pair, so this is "
              f"a lower bound on the pipeline's noise, not an estimate of it.)")
    else:
        print(f"(no usable judge-variance measurement for {a['shot']} — a run must have "
              f"n>={_variance.MIN_N_FOR_BAND} AND sit near the pass line to count. "
              f"Score movements below cannot be called signal or noise.)")
    print()
    print(_compare.report(a, b, margin=args.margin, judge_noise=noise))
    return 0


def _cmd_list(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="vfx_harness.evaluation.cli list")
    ap.add_argument("shot", nargs="?", help="limit to one shot")
    args = ap.parse_args(argv)
    for store, what in ((BASELINES, "baselines"), (VARIANCE, "variance runs")):
        print(f"\n{what} under {store}:")
        if not store.is_dir():
            print("  (none yet)")
            continue
        shots = [d for d in sorted(store.iterdir()) if d.is_dir()
                 and (not args.shot or d.name == args.shot)]
        if not shots:
            print("  (none yet)")
        for d in shots:
            for p in sorted(d.glob("*.json")):
                try:
                    rec = json.loads(p.read_text(encoding="utf-8"))
                except json.JSONDecodeError as e:
                    print(f"  {d.name}/{p.name}  UNREADABLE ({e})")
                    continue
                if what == "baselines":
                    f = rec.get("final", {})
                    final = (f"final {f['passed']}/{f['total']}" if f.get("present")
                             else "final n/a")
                    print(f"  {d.name}/{p.name}  {rec.get('label') or '-'}  {final}")
                else:
                    print(f"  {d.name}/{p.name}  n={rec.get('n')} "
                          f"scope={rec.get('scope')} spread={rec.get('mean', {}).get('spread')}")
    return 0


def _cmd_panels(argv: list[str]) -> int:
    """Did adjudication ever change a verdict, and what did it cost?

    Free — reads panels already recorded in the ledgers. Exists so "is best-of-three
    earning its keep" is answered from data rather than taste, and stays re-answerable
    as runs accumulate.
    """
    pat = argv[0] if argv else "shots/*"
    rows = []
    for f in sorted(glob.glob(f"{pat}/shot.json")):
        d = json.loads(Path(f).read_text(encoding="utf-8"))
        for lid, slot in (d.get("milestones") or {}).items():
            for r in slot.get("rounds", []):
                panel = r.get("panel")
                if not panel:
                    continue
                rows.append({"shot": Path(f).parent.name, "layer": lid,
                             "kind": r.get("kind", "iter"), "n": len(panel),
                             "first_pass": panel[0]["pass"], "final": r.get("pass"),
                             "means": [x["mean"] for x in panel]})
    if not rows:
        print("no panels recorded yet — run a layer whose verdict lands near the line")
        return 0
    changed = [r for r in rows if r["first_pass"] != r["final"]]
    extra = sum(r["n"] - 1 for r in rows)
    shots = len({r["shot"] for r in rows})
    print(f"── adjudication · {len(rows)} panel(s) across {shots} shot(s) ──")
    print(f"   extra critic calls paid : {extra}")
    print(f"   verdicts CHANGED        : {len(changed)}  "
          f"({100 * len(changed) / len(rows):.0f}% of panels)")
    print(f"   panel sizes             : {dict(Counter(r['n'] for r in rows))}")
    for n in sorted({r["n"] for r in rows}):
        at = [r for r in rows if r["n"] == n]
        ch = [r for r in at if r in changed]
        note = ("   (tautological — a unanimous pair cannot differ from its own first "
                "judge; this call is the DETECTOR that earns the n=3 call)"
                if n == 2 else "")
        print(f"     n={n}: {len(ch)}/{len(at)} changed{note}")
    for r in changed:
        print(f"   ! {r['shot']} layer {r['layer']} ({r['kind']}): means {r['means']} — "
              f"first judge said {'PASS' if r['first_pass'] else 'REVISE'}, "
              f"panel said {'PASS' if r['final'] else 'REVISE'}")
    print("\n   Read this as cost-per-correction, not a hit rate. The second call can "
          "never change\n   a verdict by itself — it exists to find the disagreements the "
          "third call resolves.\n   Weigh `extra calls` against a layer's total cost "
          "before tightening anything.")
    return 0


def _cmd_grounding(argv: list[str]) -> int:
    """Is every number in the plan re-derivable from the plate it claims to describe?

    Free — no model, no Blender. Defaults to every shot, because the one thing this found
    on barrel_roll (a target that is unreachable at any resolution) is exactly the kind of
    defect that is invisible in one shot and obvious across several.
    """


    ap = argparse.ArgumentParser(prog="vfx_harness.evaluation.cli grounding")
    ap.add_argument("folder", nargs="?", help="shot folder (default: every shot)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    folders = [Path(args.folder)] if args.folder else \
        [Path(p).parent for p in sorted(glob.glob("shots/*/acceptance.json"))]
    if not folders:
        print("no shot has an acceptance.json yet — nothing to ground")
        return 1
    recs = [_grounding.audit(f) for f in folders]
    if args.json:
        print(json.dumps(recs, indent=2))
    else:
        print("\n\n".join(_grounding.report(r) for r in recs))
    bad = sum(r.get("n_mismatch", 0) + r.get("n_unmeasurable", 0) + r.get("n_error", 0)
              for r in recs)
    return 3 if bad or any("error" in r for r in recs) else 0


def _cmd_plan(argv: list[str]) -> int:
    """The deterministic bar a plan must clear before anything is built on it.

    Free — no model, no Blender. Checks the plan against the artifacts on disk rather
    than against its own claims: fingerprints re-derive from their plates, citations
    resolve, `✓spiked` tickets cite a lab file, and layers/axes/moments agree.
    """


    ap = argparse.ArgumentParser(prog="vfx_harness.evaluation.cli plan")
    ap.add_argument("folder", nargs="?", help="shot folder (default: every shot)")
    ap.add_argument("--plan", default="plans/global.md", help="global plan file to gate")
    ap.add_argument("--feedback", action="store_true",
                    help="print the repair brief a --until-clean round would receive")
    args = ap.parse_args(argv)

    folders = [Path(args.folder)] if args.folder else \
        [Path(p).parent.parent for p in sorted(glob.glob("shots/*/plans/global.md"))]
    if not folders:
        print("no shot has plans/global.md yet — nothing to gate")
        return 1
    evaluation_roots = []
    authority_failures = {}
    for folder in folders:

        if (folder / POINTER).exists():
            try:
                resolve_current(folder)

                runs = folder.resolve() / "runs"
                runs.mkdir(parents=True, exist_ok=True)
                temporary = tempfile.TemporaryDirectory(
                    prefix="plan-eval-",
                    dir=runs,
                )
                ephemeral_root = Path(temporary.name)
                scratch_root = ephemeral_root / "scratch"
                scratch_root.mkdir()
                ephemeral = run_artifacts.RunLayout(
                    shot=folder.resolve(),
                    run_id=ephemeral_root.name,
                    root=ephemeral_root,
                )
                evaluation_roots.append((prepare_consumer_view(ephemeral), temporary))
            except PlanPublicationError as exc:
                authority_failures[folder.resolve()] = str(exc)
                evaluation_roots.append(None)
        else:
            evaluation_roots.append((folder, None))
    results = []
    for selected, folder in zip(evaluation_roots, folders, strict=True):
        if selected is None:
            results.append(_pg.GateResult(folder.name, [
                _pg.Finding(
                    "authority", True, "plans/current.json",
                    authority_failures[folder.resolve()],
                    "publish a fresh complete plan bundle; do not copy legacy files over the pointer",
                )
            ]))
        else:
            root, temporary = selected
            try:
                results.append(_pg.run(root, args.plan))
            finally:
                if temporary is not None:
                    temporary.cleanup()
    for result, folder in zip(results, folders, strict=True):
        result.shot = folder.name
    if args.feedback:
        print("\n\n".join(_pg.feedback(r) or f"{r.shot}: clean" for r in results))
    else:
        print("\n\n".join(_pg.report(r) for r in results))
    return 3 if any(not r.clean for r in results) else 0


_COMMANDS = {
    "freeze": _baseline.main,
    "variance": _variance.main,
    "blank": _blank.main,
    "check": _cmd_check,
    "plan": _cmd_plan,
    "grounding": _cmd_grounding,
    "checks": _cmd_checks,
    "compare": _cmd_compare,
    "panels": _cmd_panels,
    "list": _cmd_list,
}


def main(argv: list[str] | None = None) -> int:
    load_environment()
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help") or argv[0] not in _COMMANDS:
        print(__doc__)
        print("commands: " + " · ".join(_COMMANDS))
        return 0 if argv and argv[0] in ("-h", "--help") else 1
    return _COMMANDS[argv[0]](argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
