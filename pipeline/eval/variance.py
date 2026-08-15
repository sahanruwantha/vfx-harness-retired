"""How much does the critic disagree with ITSELF?

The pipeline's entire control flow hangs off one integer per axis. A layer proceeds, a
revision is demanded, a chain is accepted — all decided by a number that was never
measured for repeatability. It was measured once, by hand: the same render against the
same reference on one axis returned 4.0, 3.0, 3.0, 2.0 across four repeats. A two-point
spread on a five-point scale, straddling the pass line.

That single observation is currently load-bearing. It set PASS_MEAN's justification, it
motivated the best-of-three panel in `build_agent._judge`, and it is the sole evidence
behind `_ADJUDICATE_BAND = 0.4` — a number the comment beside it honestly calls a guess.
This module makes that measurement repeatable and cheap so the guess can be replaced.

It needs no build. It re-scores a render that already exists, through the real critic,
with the real prompt, at the real effort setting.

TWO HONEST LIMITS, both reported in the output rather than buried here:

  1. No motion strip. `_critique` builds the strip by rendering frames from a live
     Blender session; scoring an existing PNG has no session. For a `motion` shot the
     production critic therefore sees one more image than this eval does. The measured
     spread is a proxy for production spread, not the same quantity.
  2. One pair. Variance measured on ONE render/reference pair is a point estimate of a
     quantity that plainly depends on how ambiguous that particular frame is. It bounds
     nothing about other frames.
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timezone
from pathlib import Path

from ..brief import Shot, load_shot
from ..ledger import Milestone, load_axes, load_layers
from ..log import log
from . import VARIANCE

# Below this many repeats, dispersion statistics are decoration. Three repeats can
# report a spread but cannot distinguish "the judge is stable" from "we got lucky
# twice", and a band derived from three points is a guess with extra steps.
MIN_N_FOR_BAND = 6


class _NoSession:
    """Stands in for the warm Blender session `_critique` expects.

    `_critique` only touches the session to build a motion strip. Raising here (rather
    than passing None and hoping) makes the skip explicit in the log instead of turning
    into an AttributeError that reads like a bug.
    """

    def render(self, *a, **kw):
        raise RuntimeError("no Blender session — the variance eval scores an existing "
                           "still, so no motion strip is attached")


def layer_scope(shot: Shot, layer) -> str:
    """Rebuild the scope block `build_agent.build_layer` sends with a layer's verdict.

    Mirrored, not imported: the block is constructed inline inside `build_layer`, and
    the whole point of this eval is to measure the critic under PRODUCTION conditions.
    Scoring the same frame on the unrestricted rubric measures a different thing — a
    layout layer judged on emission axes hits a floor it can never lift, which is the
    exact bug `owns` was introduced to fix. If `build_layer`'s block changes, this drifts;
    tests/test_harness.py carries a tripwire on the distinctive markers.
    """
    from ..build_agent import _plan_layer_excerpt

    excerpt = _plan_layer_excerpt(shot, layer)
    done = "\n".join(ln for ln in excerpt.splitlines()
                     if ln.startswith(("**Scope", "**Judge artifact", "**Done")))
    owned = (f"  THIS LAYER OWNS: {', '.join(layer.owns)}.\n"
             f"  Score ONLY those axes; every other axis is \"n/a\"."
             if layer.owns else
             f"  Score only what THIS layer's scope covers; everything else is \"n/a\".")
    return (f"  Layer {layer.id} — {layer.title} (one build stage of many; later layers "
            f"add the rest of the look).\n  {layer.reads}\n{done}\n{owned}\n"
            f"  Elements that are correctly ABSENT at this frame (they appear or "
            f"disappear in other layers) are \"n/a\", never 0.").strip()


def _dispersion(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "values": values,
        "mean": round(statistics.fmean(values), 3),
        "median": round(statistics.median(values), 3),
        "min": min(values),
        "max": max(values),
        "spread": round(max(values) - min(values), 3),
        "stdev": round(statistics.stdev(values), 3) if len(values) > 1 else 0.0,
    }


async def measure(shot: Shot, *, render_rel: str, ref_rel: str, n: int = 3,
                  layer_id: str | None = None, milestone_id: str = "VAR",
                  frame: int = 0, reads: str = "", verbose: bool = True) -> dict:
    """Score one existing render/reference pair `n` times through the real critic."""
    from ..build_agent import PASS_MEAN, PASS_MIN, _ADJUDICATE_BAND, _borderline, _critique

    axes = load_axes(shot)
    scope = None
    if layer_id:
        layer = load_layers(shot)[layer_id]
        scope = layer_scope(shot, layer)
        if not frame:
            frame = layer.judge_frame
        reads = reads or layer.reads
        milestone_id = layer_id
    # strip=() deliberately: an empty strip is what makes _stash_motion_strip's failure
    # a clean skip rather than a partially-built montage.
    m = Milestone(milestone_id, frame or 1, ref_rel, reads or "(no target-state text)", ())

    for rel in (render_rel, ref_rel):
        if not (shot.folder / rel).is_file():
            raise FileNotFoundError(f"{shot.folder / rel} does not exist — the variance "
                                    f"eval scores files, it does not render them")

    log(f"judge variance: {n} repeats of {render_rel} vs {ref_rel}"
        + (f" under layer {layer_id} scope" if layer_id else " on the FULL rubric"))
    verdicts: list[dict] = []
    for i in range(1, n + 1):
        log(f"repeat {i}/{n}", 1)
        v = await _critique(shot, m, render_rel, axes, _NoSession(), verbose, scope)
        verdicts.append(v)

    per_axis: dict[str, dict] = {}
    for key, _desc in axes:
        vals = [float(v["scores"][key]) for v in verdicts
                if isinstance(v.get("scores", {}).get(key), (int, float))
                and not isinstance(v["scores"][key], bool)]
        na = sum(1 for v in verdicts if not isinstance(v.get("scores", {}).get(key), (int, float))
                 or isinstance(v.get("scores", {}).get(key), bool))
        d = _dispersion(vals)
        d["n_a"] = na
        # An axis the critic sometimes scores and sometimes calls n/a is its own defect:
        # the mean is computed over the in-scope axes, so a wandering denominator moves
        # the verdict without any score changing.
        d["scope_unstable"] = bool(vals) and na > 0
        per_axis[key] = d

    means = [v["mean"] for v in verdicts]
    passes = [bool(v["pass"]) for v in verdicts]
    n_pass = sum(passes)
    mean_stats = _dispersion(means)
    band_evidence = (round(max(abs(x - mean_stats["median"]) for x in means), 3)
                     if means else 0.0)

    rec = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "shot": shot.id,
        "render": render_rel,
        "ref": ref_rel,
        "layer": layer_id,
        "scope": "layer" if layer_id else "full-rubric",
        "n": n,
        "critic_model": _critic_model(),
        "motion_strip": False,     # see the module docstring — a labelled proxy
        "axes_in_rubric": [k for k, _ in axes],
        "per_axis": per_axis,
        "mean": mean_stats,
        "verdicts": [{"mean": v["mean"], "pass": bool(v["pass"]),
                      "scored_axes": v.get("scored_axes", []),
                      "na_axes": v.get("na_axes", []),
                      "borderline": _borderline(v)} for v in verdicts],
        "pass_count": n_pass,
        "flip_rate": round(min(n_pass, n - n_pass) / n, 3) if n else 0.0,
        "unanimous": n_pass in (0, n),
        "thresholds": {"PASS_MEAN": PASS_MEAN, "PASS_MIN": PASS_MIN,
                       "ADJUDICATE_BAND": _ADJUDICATE_BAND},
        "band_evidence": band_evidence,
        "enough_for_band": n >= MIN_N_FOR_BAND,
    }
    return rec


def _critic_model() -> str:
    from ..build_agent import CRITIC_MODEL
    return CRITIC_MODEL


def report(rec: dict) -> str:
    """The human-readable finding. States what the numbers do NOT support, in the
    output, because a caveat that lives only in a report gets separated from the number
    the first time someone quotes it."""
    n = rec["n"]
    lines = [
        f"── judge variance · {rec['shot']} · {rec['render']} vs {rec['ref']} ──",
        f"   critic     {rec['critic_model']} · {n} repeats · scope {rec['scope']}"
        + (f" (layer {rec['layer']})" if rec["layer"] else ""),
        f"   motion     NO strip attached — a proxy for a `motion` shot's production "
        f"critic, which sees one more image",
        "",
        "   per axis (only axes the critic actually scored):",
    ]
    scored_any = False
    for key, d in rec["per_axis"].items():
        if not d.get("n"):
            lines.append(f"     {key:<26} n/a in all {n} repeats")
            continue
        scored_any = True
        flag = "  ⚠ SCOPE UNSTABLE (scored sometimes, n/a others)" if d["scope_unstable"] else ""
        lines.append(
            f"     {key:<26} {d['values']}  median {d['median']} · spread {d['spread']} "
            f"· sd {d['stdev']}{flag}")
    if not scored_any:
        lines.append("     (nothing was scored — every axis came back n/a)")
    m = rec["mean"]
    lines += [
        "",
        f"   mean       {m['values']} → median {m['median']} · spread {m['spread']} "
        f"· sd {m['stdev']}",
        f"   verdict    {rec['pass_count']}/{n} PASS · flip rate {rec['flip_rate']:.0%}"
        + ("  (unanimous)" if rec["unanimous"] else "  ⚠ THE SAME IMAGE FLIPPED VERDICT"),
    ]
    t = rec["thresholds"]
    lines.append(f"   thresholds PASS_MEAN {t['PASS_MEAN']} · PASS_MIN {t['PASS_MIN']} "
                 f"· _ADJUDICATE_BAND {t['ADJUDICATE_BAND']}")
    lines.append("")
    if not rec["enough_for_band"]:
        lines.append(
            f"   ⚠ N={n} IS TOO SMALL TO RECOMMEND A BAND. This run tells you the spread "
            f"was AT LEAST {m['spread']}; it cannot tell you the spread's distribution, "
            f"and the observed range of {n} draws systematically UNDERSTATES the true "
            f"range. Re-run with --n {MIN_N_FOR_BAND} or more before quoting a number.")
    else:
        rec_band = max(rec["band_evidence"], 0.0)
        lines.append(
            f"   band evidence: every observed mean sat within {rec_band} of the median. "
            f"A verdict whose mean is within that distance of PASS_MEAN "
            f"({t['PASS_MEAN']}) could plausibly have landed on the other side, so "
            f"_ADJUDICATE_BAND should be AT LEAST {rec_band} "
            f"(currently {t['ADJUDICATE_BAND']}).")
        lines.append(
            f"   Caveat that travels with that number: it is ONE render/reference pair. "
            f"Ambiguity is a property of the frame, so this is a lower bound for the "
            f"pipeline as a whole, not an estimate of it.")
    if any(d.get("scope_unstable") for d in rec["per_axis"].values()):
        lines.append(
            "   ⚠ At least one axis moved in and out of scope between repeats. The mean "
            "is sum/n over IN-SCOPE axes, so that alone changes the verdict with no "
            "score changing — check the scope block before trusting any mean here.")
    return "\n".join(lines)


def save(rec: dict) -> Path:
    stamp = rec["at"].replace(":", "").replace("-", "")
    tag = f"{rec['scope']}_n{rec['n']}"
    out = VARIANCE / rec["shot"] / f"{stamp}_{tag}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rec, indent=2) + "\n", encoding="utf-8")
    return out


def latest(shot_id: str) -> dict | None:
    """The most recent variance measurement for a shot, if one was ever taken.

    `compare` uses it to say whether a score delta is inside measured noise. Without it
    the only honest answer is "unknown", which is what returning None means here.
    """
    found = sorted((VARIANCE / shot_id).glob("*.json")) if (VARIANCE / shot_id).is_dir() else []
    if not found:
        return None
    return json.loads(found[-1].read_text(encoding="utf-8"))


def _default_pair(shot: Shot) -> tuple[str, str, str | None]:
    """Pick a render/reference pair that already exists, preferring a canonical render
    the pipeline itself judged. Returns (render_rel, ref_rel, layer_id)."""
    data_path = shot.folder / "shot.json"
    if data_path.is_file():
        data = json.loads(data_path.read_text(encoding="utf-8"))
        for lid, slot in data.get("milestones", {}).items():
            for r in reversed(slot.get("rounds", [])):
                render, ref = r.get("render"), slot.get("ref")
                if render and ref and (shot.folder / render).is_file() \
                        and (shot.folder / ref).is_file():
                    return render, ref, lid
    raise FileNotFoundError(
        f"no judged render/reference pair found in {shot.folder}/shot.json — pass "
        f"--render and --ref explicitly")


def main(argv: list[str]) -> int:
    import argparse

    import anyio

    ap = argparse.ArgumentParser(prog="pipeline.evals variance")
    ap.add_argument("folder", help="shot folder")
    # Deliberately LOW. A casual run should cost cents; the output says plainly that a
    # low N cannot support a band recommendation.
    ap.add_argument("--n", type=int, default=3, help="repeats (default 3; >=6 to quote a band)")
    ap.add_argument("--render", help="render path relative to the shot folder")
    ap.add_argument("--ref", help="reference path relative to the shot folder")
    ap.add_argument("--layer", help="score under this layer's production scope block")
    ap.add_argument("--full-rubric", action="store_true",
                    help="ignore layer scope and score every axis (acceptance-like)")
    ap.add_argument("--no-save", action="store_true")
    args = ap.parse_args(argv)

    shot = load_shot(args.folder)
    render, ref, layer_id = args.render, args.ref, args.layer
    if not (render and ref):
        d_render, d_ref, d_layer = _default_pair(shot)
        render, ref = render or d_render, ref or d_ref
        layer_id = layer_id or d_layer
        log(f"no pair given — using the last judged pair: {render} vs {ref}")
    if args.full_rubric:
        layer_id = None

    rec = anyio.run(lambda: measure(shot, render_rel=render, ref_rel=ref, n=args.n,
                                    layer_id=layer_id))
    print("\n" + report(rec))
    if not args.no_save:
        print(f"\n→ {save(rec)}")
    return 0
