"""Skill ledger — what this pipeline is systematically good and bad at.

Every critic round is already written to `shot.json`, and we throw the aggregate away.
Rolled up, it says something no single layer can: `city_texture` scored 2 in EVERY round
of EVERY barrel_roll build, `environment_depth` failed at three of five server_to_hansa
moments. That is not a layer having a bad day, it is a capability the pipeline does not
have — and it is the closest thing to a craftsperson knowing their own weak areas.

Use it to decide what to learn next. A weak axis with no recipe is the recipe to write; a
weak axis WITH a recipe means the recipe is not being found or not working, which is a
different fix (index, hook, or helper).

    python -m pipeline.skills            # the report
    python -m pipeline.skills --json     # machine-readable
"""

from __future__ import annotations

import argparse
import json
import statistics as st
from collections import defaultdict
from pathlib import Path

from .recipes import search_recipes

SHOTS = Path(__file__).resolve().parent.parent / "shots"


def _unproven() -> set[str]:
    """Recipes with no live spike evidence (see `pipeline.verify_recipes`).

    "WEAK DESPITE a recipe existing" has a third explanation the report used to miss:
    the recipe may never have been proven to RUN. Distinguishing that from "not found"
    and "found but wrong" is the difference between a fix and a guess."""
    from .verify_recipes import audit, load_ledger
    from .recipes import _all
    return {r["name"] for r, entitled, _why in audit(_all(), load_ledger()) if not entitled}


def _rounds(shot_json: Path):
    try:
        d = json.loads(shot_json.read_text(encoding="utf-8"))
    except Exception:
        return
    for gid, slot in (d.get("milestones") or {}).items():
        for r in slot.get("rounds", []):
            yield shot_json.parent.name, gid, r.get("kind", "iter"), r.get("scores", {})
    acc = d.get("acceptance") or {}
    for mid, r in (acc.get("moments") or {}).items():
        yield shot_json.parent.name, f"accept:{mid}", "acceptance", r.get("scores", {})


def collect(root: Path = SHOTS) -> dict:
    per_axis: dict[str, list] = defaultdict(list)
    seen_shots: set[str] = set()
    for sj in sorted(root.glob("*/shot.json")):
        if sj.parent.name.startswith("_"):
            continue
        for shot, gid, kind, scores in _rounds(sj):
            seen_shots.add(shot)
            for axis, v in (scores or {}).items():
                if isinstance(v, (int, float)):
                    per_axis[axis].append({"shot": shot, "layer": gid, "kind": kind,
                                           "score": float(v)})
    out = {}
    for axis, rows in per_axis.items():
        vals = [r["score"] for r in rows]
        out[axis] = {
            "n": len(vals),
            "mean": round(st.mean(vals), 2),
            "best": max(vals),
            "worst": min(vals),
            "shots": sorted({r["shot"] for r in rows}),
            # the damning statistic: never once cleared the bar, in any shot, any round
            "never_cleared": max(vals) < 3,
            "recipes": [h["name"] for h in search_recipes(axis.replace("_", " "), k=2)],
        }
    return {"shots": sorted(seen_shots), "axes": out}


def report(data: dict) -> str:
    axes = data["axes"]
    if not axes:
        return "no scored rounds yet — build something first"
    try:
        unproven = _unproven()
    except Exception as e:
        print(f"! could not read the spike ledger: {e}")
        unproven = set()
    lines = [f"SKILL LEDGER — {len(axes)} axes across {len(data['shots'])} shot(s): "
             f"{', '.join(data['shots'])}", ""]
    lines.append(f"  {'axis':32s} {'n':>3s} {'mean':>5s} {'best':>4s}  {'':2s} recipes")
    for axis, s in sorted(axes.items(), key=lambda kv: kv[1]["mean"]):
        flag = "✗✗" if s["never_cleared"] else ("✗ " if s["mean"] < 3.0 else "  ")
        names = [n + " (UNPROVEN)" if n in unproven else n for n in s["recipes"]]
        lines.append(f"  {axis:32s} {s['n']:>3d} {s['mean']:>5.2f} {s['best']:>4.0f}  "
                     f"{flag} {', '.join(names) or '— NO RECIPE'}")
    weak = [a for a, s in axes.items() if s["never_cleared"]]
    gaps = [a for a, s in axes.items() if s["mean"] < 3.0 and not s["recipes"]]
    covered = [a for a, s in axes.items() if s["mean"] < 3.0 and s["recipes"]]
    lines.append("")
    if weak:
        lines.append(f"NEVER CLEARED 3 in any shot or round: {', '.join(sorted(weak))}")
        lines.append("  → a capability gap, not a bad layer. Nothing tuned its way out.")
    if gaps:
        lines.append(f"WEAK and NO RECIPE covers it: {', '.join(sorted(gaps))}")
        lines.append("  → write the recipe. This is the highest-value thing to learn next.")
    if covered:
        lines.append(f"WEAK DESPITE a recipe existing: {', '.join(sorted(covered))}")
        lines.append("  → the recipe is not being found, or does not work. Different fix: "
                     "index entry, guardrail hook, or promote it to a bvfx_* helper.")
    blind = sorted({n for a in covered for n in axes[a]["recipes"]} & unproven)
    if blind:
        lines.append(f"  → and these covering recipes have NO spike evidence: "
                     f"{', '.join(blind)}. Run `python -m pipeline.verify_recipes "
                     f"--name <n>` before blaming the axis.")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="What this pipeline is good and bad at.")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--root", default=str(SHOTS))
    args = ap.parse_args()
    data = collect(Path(args.root))
    print(json.dumps(data, indent=2) if args.json else report(data))


if __name__ == "__main__":
    main()
