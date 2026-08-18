"""One structured report per layer, so a run can be validated by reading instead of grepping.

Checking layer 1 today took six separate greps — rounds from one log line, cost from the
SDK's `done` line, ablation from shot.json, recipe pulls from a jsonl, the journal from a
directory listing, the snapshot from another. Anything nobody grepped for stayed invisible,
which is how a metrics hook no-opped for an entire build phase without a trace.

Counters live here because the things worth counting happen inside hooks, which have no
other way to report: a hook that fires is silent by design, and a hook that never fires is
silent by accident. Only the difference between them is interesting.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

_COUNTS: Counter = Counter()


def bump(event: str, n: int = 1) -> None:
    """Record something a hook did. Cheap enough to call on every tool call."""
    _COUNTS[event] += n


def snapshot_counts() -> dict:
    return dict(_COUNTS)


def reset_counts() -> None:
    _COUNTS.clear()


def write(shot_folder: str | Path, layer, *, status: str, rounds: list,
          canonical: list | None = None, ablation: dict | None = None,
          cost: float = 0.0, turns: int = 0, seconds: float = 0.0,
          journal: dict | None = None, recipes: list | None = None,
          reviews: list | None = None, tokens: dict | None = None,
          approach: str | None = None, extra: dict | None = None) -> Path:
    folder = Path(shot_folder)
    rec = {
        "layer": getattr(layer, "id", "?"),
        "title": getattr(layer, "title", ""),
        "status": status,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "judges": [{"frame": f, "ref": r} for f, r in getattr(layer, "judges", [])],
        "owns": list(getattr(layer, "owns", ())),
        "rounds": rounds,
        "canonical": canonical or [],
        "ablation": ablation or {},
        "reviews": reviews or [],
        "recipes_pulled": sorted(set(recipes or [])),
        "journal": journal or {},
        "cost_usd": round(cost, 4),
        "turns": turns,
        "seconds": round(seconds, 1),
        "tokens": tokens or {},
        # WHY the builder built it this way, not only what it ran. The journal records
        # run_bpy calls; without the stated approach, "did the recipe index change what
        # it reached for?" is unanswerable except by reading raw SDK transcripts.
        "approach": approach,
        "hooks": snapshot_counts(),
        **(extra or {}),
    }
    out = folder / "logs" / f"run_layer{rec['layer']}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rec, indent=2) + "\n", encoding="utf-8")
    return out


def summary(rec: dict) -> str:
    """The block printed at the end of a layer — everything I used to grep for."""
    h = rec.get("hooks", {})
    can = " · ".join(f"f{c['frame']}:{c['mean']}{'✅' if c['pass'] else '✗'}"
                     for c in rec.get("canonical", [])) or "—"
    rounds = " → ".join(str(r.get("mean")) for r in rec.get("rounds", [])) or "—"
    lines = [
        f"── layer {rec['layer']} · {rec['title']} · {rec['status'].upper()} ──",
        f"   rounds     {rounds}",
        f"   canonical  {can}",
        f"   owns       {', '.join(rec.get('owns', [])) or '—'}",
        f"   cost       ${rec.get('cost_usd', 0):.2f} · {rec.get('turns', 0)} turns · "
        f"{rec.get('seconds', 0) / 60:.0f} min",
    ]
    t = rec.get("tokens") or {}
    if t:
        read, made = t.get("cache_read_input_tokens", 0), t.get("cache_creation_input_tokens", 0)
        served = read + made + t.get("input_tokens", 0)
        hit = f"{100 * read / served:.0f}%" if served else "n/a"
        lines.append(f"   tokens     in {t.get('input_tokens', 0):,} · out "
                     f"{t.get('output_tokens', 0):,} · cache read {read:,} / created "
                     f"{made:,} → {hit} hit")
        # A cached prefix is the difference between ~0.1x and 1x on the biggest part of
        # every request, so a collapse here is a cost regression hiding as normal output.
        if served > 50_000 and read / served < 0.5:
            lines.append("   ⚠ cache hit rate is LOW — something is breaking the stable "
                         "prefix (system prompt / tool defs / CLAUDE.md)")
    if rec.get("approach"):
        lines.append(f"   approach   {str(rec['approach'])[:160]}")
    if rec.get("run_id"):
        lines.append(f"   run        {rec['run_id']} · attempt {rec.get('attempt', '?')}")
    if rec.get("ablation"):
        a = rec["ablation"]
        lines.append(f"   ablation   {'moved ' + ', '.join(a['moved']) if a.get('moved') else a.get('note', '—')}")
    if rec.get("recipes_pulled"):
        lines.append(f"   recipes    {', '.join(rec['recipes_pulled'])}")
    if rec.get("journal"):
        j = rec["journal"]
        lines.append(f"   journal    {j.get('calls', 0)} calls, {j.get('chars', 0) // 1024}KB")
    tu = rec.get("tools") or {}
    if tu:
        top = " · ".join(f"{k.split('__')[-1]} {v}"
                         for k, v in list((tu.get("calls") or {}).items())[:5])
        lines.append(f"   tools      {tu.get('total', 0)} calls — {top}")
        # LOOKING vs MEASURING. On barrel_roll every layer that passed called
        # compare_frame 7-41 times; the layer that failed three times called it 3-5 and
        # called measure_regions 17-44 instead — the only layer where measuring
        # outnumbered looking, and it optimised onto its target numbers with a render
        # that still did not match the picture. Surfaced as a warning rather than a bare
        # number because it is a LEADING indicator: by the time the verdict reads 2.0 the
        # money is already spent.
        lpm = tu.get("look_per_measure")
        if lpm is not None and lpm < 1.0 and tu.get("measured", 0) >= 8:
            lines.append(f"   ⚠ MEASURED MORE THAN IT LOOKED ({tu.get('looked')} render/"
                         f"compare vs {tu.get('measured')} measurements). Layers that pass "
                         f"look at the reference far more than they measure, and this "
                         f"ratio has tracked failure — check the done-check is not "
                         f"all-numeric.")
    if rec.get("reviews"):
        lines.append(f"   reviews    {len(rec['reviews'])} "
                     f"({sum(1 for r in rec['reviews'] if r.get('replace'))} said REPLACE)")
    # hooks: silence is the interesting signal, so name what did NOT happen too
    fired = [f"{k}={v}" for k, v in sorted(h.items()) if v]
    lines.append(f"   hooks      {' · '.join(fired) if fired else 'NOTHING FIRED — verify the hooks are wired'}")
    if not h.get("metric_feedback"):
        lines.append("   ⚠ the builder received NO objective metric feedback this layer")
    return "\n".join(lines)
