"""The evaluation harness — the thing that decides whether a change to this pipeline
actually helped.

    python -m pipeline.evals freeze   shots/barrel_roll --label before-context-editing
    python -m pipeline.evals check    shots/barrel_roll            # free, no model
    python -m pipeline.evals variance shots/barrel_roll --n 6      # cheap, real critic
    python -m pipeline.evals compare  barrel_roll:latest evals/baselines/.../x.json
    python -m pipeline.evals list

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
import json
import sys

from .brief import load_shot
from .eval import BASELINES, VARIANCE
from .eval import baseline as _baseline
from .eval import compare as _compare
from .eval import variance as _variance
from .eval.determinism import metric_scale_consistency, replay_equivalence
from .eval.integrity import artifact_integrity


def _cmd_check(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pipeline.evals check")
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
        # A sweep over refs alone would miss that the renders are the side that arrives
        # at the wrong resolution.
        images = list(shot.refs) + sorted(
            p for p in (shot.folder / "renders").glob("*.png")
            if "_motion" not in p.name) if (shot.folder / "renders").is_dir() else list(shot.refs)
    results.append(metric_scale_consistency(images))

    if args.skip_replay:
        from .eval.determinism import Result
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
    ap = argparse.ArgumentParser(prog="pipeline.evals compare")
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
        print(f"(judge noise from {v['at']}, n={v['n']}, scope {v['scope']}: "
              f"mean spread {v['mean']['spread']})")
    print()
    print(_compare.report(a, b, margin=args.margin, judge_noise=noise))
    return 0


def _cmd_list(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pipeline.evals list")
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


_COMMANDS = {
    "freeze": _baseline.main,
    "variance": _variance.main,
    "check": _cmd_check,
    "compare": _cmd_compare,
    "list": _cmd_list,
}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help") or argv[0] not in _COMMANDS:
        print(__doc__)
        print("commands: " + " · ".join(_COMMANDS))
        return 0 if argv and argv[0] in ("-h", "--help") else 1
    return _COMMANDS[argv[0]](argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
