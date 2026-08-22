"""Is the run on track, and are the newer capabilities actually being used?

    python -m vfx_harness.application.inspect_run <shot>              # the digest
    python -m vfx_harness.application.inspect_run <shot> --layer 3    # one layer, with its timeline
    python -m vfx_harness.application.inspect_run <shot> --tools      # tool adoption only
    python -m vfx_harness.application.inspect_run <shot> --json

Two questions, one reader. They are asked together because they have the same answer
shape and were previously answered from different places: trajectory came from
per-layer reports, adoption from grepping a console log that no longer existed, and
the actual conversation from the SDK's own session files outside the repo entirely.

ON TRACK is not "did it pass". A layer can pass on its first round and still be a
problem, and a layer can sit at 2.5 for three rounds and be one round from fine. What
separates them is the SHAPE of the round means — rising, flat, or sawtoothing — and
whether the canonical replay agreed with the live session. Those get read out per layer
and summarised as a single trajectory word.

ARE THE IMPROVEMENTS WORKING is deliberately answered as adoption first, effect second.
`render_pass`, `check_scene`, `diff_frames`, and `verify_change` were built, verified
against Blender and written into the builder prompt. If the builder never calls them, no
downstream measurement of their effect means anything — and the fix is the prompt, not
the tool. So a zero-call count is reported as a FINDING, not as an absence.
"""

from __future__ import annotations

import argparse
import itertools
import json
from collections import Counter
from pathlib import Path

from vfx_harness.domain.brief import load_shot
from vfx_harness.observability import run_artifacts, transcript

# The Phase 1/2 additions and what each is FOR, so a report about an unused tool says
# what capability is going unused rather than just naming a symbol.
_DIAGNOSTIC = {
    "render_pass": "isolate one channel (diffuse/emit/shadow/ao/normal/depth) or shade "
                   "mode, so an axis is judged on the signal it is about",
    "check_scene": "judgment-free facts — visibility, framing, motion, mesh, scale — "
                   "instead of asserting them",
    "diff_frames": "compare explicit image paths when diagnosing a render change",
    "verify_change": "prove one live edit changed pixels without managing render paths",
}


def _load_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _trajectory(means: list[float]) -> str:
    """One word for the shape of a layer's round means.

    A bare list of numbers makes the reader do this arithmetic every time, and the
    interesting cases (flat, and sawtooth) are exactly the ones that look fine at a glance
    because the last number is the highest.
    """
    vals = [m for m in means if isinstance(m, (int, float))]
    if not vals:
        return "unscored"
    if len(vals) == 1:
        return "single round"
    first, last = vals[0], vals[-1]
    gain = last - first
    drops = sum(1 for a, b in itertools.pairwise(vals) if b < a - 1e-9)
    if drops and gain > 0.25:
        # Went up overall but lost ground somewhere: something is being fixed and broken
        # in the same conversation, which is the signature of a layer whose axes fight.
        return "sawtooth (rising, with regressions)"
    if drops >= 2:
        return "sawtooth (unstable)"
    if abs(gain) <= 0.1:
        return "FLAT — rounds are not moving the score"
    return "rising" if gain > 0 else "FALLING"


def layers(shot_folder: Path, *, run_id: str | None = None) -> list[dict]:
    out = []
    layout = run_artifacts.select(shot_folder, run_id)
    if not layout:
        return out
    report_dir = layout.reports / "layers"
    for p in sorted(report_dir.glob("layer-*.json"),
                    key=lambda q: (len(q.stem), q.stem)):
        rec = _load_json(p)
        if rec:
            rec["_path"] = str(p)
            out.append(rec)
    return out


def adoption(recs: list[dict]) -> dict:
    """Diagnostic-tool usage across every layer that has a report."""
    per_layer: dict[str, dict] = {}
    totals: Counter = Counter()
    for rec in recs:
        tools = rec.get("tools") or {}
        got = {t: (tools.get("adoption") or {}).get(t, 0) for t in _DIAGNOSTIC}
        # An OLD report predates the adoption field entirely. That is not "zero calls" —
        # it is "not measured", and conflating them would invent evidence that the tools
        # were ignored on runs that could not have called them.
        measured = "adoption" in tools
        applicable = tools.get("applicability") or dict.fromkeys(_DIAGNOSTIC, True)
        per_layer[str(rec.get("layer"))] = {
            "measured": measured, "calls": got, "applicability": applicable,
            "looked": tools.get("looked"), "measured_calls": tools.get("measured"),
            "verified": tools.get("verified"),
            "total": tools.get("total", 0),
        }
        if measured:
            totals.update({tool: count for tool, count in got.items()
                           if applicable.get(tool, True)})
    measured_layers = [k for k, v in per_layer.items() if v["measured"]]
    return {
        "per_layer": per_layer,
        "totals": dict(totals),
        "measured_layers": measured_layers,
        "unmeasured_layers": [k for k, v in per_layer.items() if not v["measured"]],
        "never_used": [t for t in _DIAGNOSTIC if measured_layers and not totals.get(t)
                       and any(per_layer[layer]["applicability"].get(t, True)
                               for layer in measured_layers)],
    }


def transcripts(shot_folder: Path, *, run_ids: set[str] | None = None) -> list[dict]:
    """What durable records exist, and what is in them — without loading them whole."""
    out = []
    paths = transcript.find(shot_folder)
    if run_ids is not None:
        paths = [path for path in paths if any(
            transcript.run_id_for(path, shot_folder) == run_id
            or path.stem.endswith(run_id)
            for run_id in run_ids
        )]
    for p in paths:
        events = transcript.read(p)
        kinds = Counter(e.get("kind") for e in events)
        tools = Counter(e.get("tool") for e in events if e.get("kind") == "tool_use")
        errs = [e for e in events if e.get("kind") == "tool_result" and e.get("is_error")]
        crits = [e for e in events if e.get("kind") == "critic"]
        focus = [e for e in events if e.get("kind") == "critic_focus"]
        imgs = sum(1 for e in events
                   for c in (e.get("content") if isinstance(e.get("content"), list) else [])
                   if isinstance(c, dict) and c.get("type") == "image")
        out.append({
            "file": p.name, "path": str(p), "events": len(events),
            "kb": round(p.stat().st_size / 1024),
            "kinds": dict(kinds.most_common()),
            "tools": dict(tools.most_common()),
            "tool_errors": len(errs),
            "critic_calls": len(crits),
            "focus_panels": sum(len(e.get("panels") or []) for e in focus),
            "critic_means": [c.get("mean") for c in crits],
            "unparseable": kinds.get("unparseable", 0),
            # How many renders the model was actually shown. The pixels live under
            # renders/; what matters here is that a verdict had images behind it at all.
            "images_shown": imgs,
        })
    return out


def collect(shot_folder: str | Path, *, history: bool = False,
            run_id: str | None = None) -> dict:
    folder = Path(shot_folder)
    layout = run_artifacts.select(folder, run_id)
    recs = layers(folder, run_id=run_id)
    shot = _load_json(folder / "shot.json")
    acc = (shot.get("acceptance") or {})
    from vfx_harness.orchestration.unit_state import load as load_unit_state

    work_units = []
    for layer_id in sorted({str(rec.get("layer")) for rec in recs if rec.get("layer") is not None}):
        state = load_unit_state(folder, layer_id)
        work_units.extend(
            {
                "layer": layer_id,
                "unit": unit_id,
                "status": row.get("status"),
                "falsification_id": (row.get("falsification") or {}).get("record_id"),
            }
            for unit_id, row in (state.get("units") or {}).items()
        )
    per = []
    for rec in recs:
        means = [r.get("mean") for r in rec.get("rounds", [])]
        canon = rec.get("canonical") or []
        per.append({
            "layer": rec.get("layer"), "title": rec.get("title"),
            "status": rec.get("status"), "means": means,
            "trajectory": _trajectory(means),
            "rounds": len(means),
            "canonical_pass": all(c.get("pass") for c in canon) if canon else None,
            "canonical_conflict": any(c.get("judge_conflict") for c in canon),
            "canonical_contract_gap": any(c.get("contract_gap") for c in canon),
            "canonical_reproduced": any(c.get("decided_by") == "pixel_reproduction"
                                         for c in canon),
            "canonical": [{"frame": c.get("frame"), "mean": c.get("mean"),
                           "pass": c.get("pass"),
                           "contract_gap": c.get("contract_gap", False),
                           "judge_conflict": c.get("judge_conflict", False),
                           "decided_by": c.get("decided_by", "critic")} for c in canon],
            "cost_usd": rec.get("cost_usd", 0.0), "turns": rec.get("turns", 0),
            "minutes": round((rec.get("seconds") or 0) / 60, 1),
            "hooks_fired": sorted(k for k, v in (rec.get("hooks") or {}).items() if v),
            "no_metric_feedback": (
                not rec.get("revalidation")
                and (rec.get("tools") or {}).get("look_feedback_applicable", True)
                and not (rec.get("hooks") or {}).get("metric_feedback")),
            # None, not 0. A report written before the tool telemetry existed has no
            # counts, and printing "0 tool calls" for a layer that made hundreds is the
            # same mistake as counting an unmeasured layer as an unused tool.
            "tools_total": (rec.get("tools") or {}).get("total"),
            "look_per_measure": (rec.get("tools") or {}).get("look_per_measure"),
            "run_id": rec.get("run_id"), "attempt": rec.get("attempt"),
        })
    return {
        "shot": folder.name,
        "run": ({
            "run_id": layout.run_id,
            "root": str(layout.root),
            "manifest": str(layout.manifest),
            "status": _load_json(layout.status),
        } if layout else None),
        "layers": per,
        "cost_usd": round(sum(p["cost_usd"] for p in per), 2),
        "minutes": round(sum(p["minutes"] for p in per), 1),
        "passed": sum(1 for p in per if p["status"] == "passed"),
        "work_units": work_units,
        "hypotheses_falsified": sum(
            1 for row in work_units if row["status"] == "hypothesis_falsified"
        ),
        "adoption": adoption(recs),
        "transcripts": transcripts(
            folder, run_ids=None if history else
            {str(rec.get("run_id")) for rec in recs if rec.get("run_id")}),
        "acceptance": {"passed": acc.get("passed"), "total": acc.get("total"),
                       "repair_plan": [c.get("layer") for c in (acc.get("repair_plan") or [])],
                       "superseded": acc.get("superseded") or []},
    }


def _findings(d: dict) -> list[str]:
    """The lines worth acting on. Kept separate from the layout so the report can lead
    with them — a digest whose warnings are buried in the middle gets read as a wall."""
    out = []
    for p in d["layers"]:
        if p["trajectory"].startswith("FLAT"):
            out.append(f"layer {p['layer']} spent {p['rounds']} rounds and "
                       f"{p['turns']} turns without moving its score "
                       f"({' → '.join(str(m) for m in p['means'])}). Rounds are not the "
                       f"constraint here; what it is being told is.")
        if p["trajectory"].startswith("sawtooth"):
            out.append(f"layer {p['layer']} regressed between rounds "
                       f"({' → '.join(str(m) for m in p['means'])}) — a fix for one axis "
                       f"is breaking another.")
        if p.get("canonical_conflict"):
            out.append(f"layer {p['layer']} has a JUDGE CONFLICT: executable evidence "
                       f"and the critic disagree, with no evidence-backed repair. Re-run "
                       f"the judge or review it; do not rebuild blindly.")
        elif p["canonical_pass"] is False:
            out.append(f"layer {p['layer']} passed LIVE but its canonical replay did not "
                       f"reproduce — the script does not rebuild the scene it was scored on.")
        if p["no_metric_feedback"] and p["status"] != "?":
            out.append(f"layer {p['layer']} received NO objective metric feedback — the "
                       f"critic's prose was the only signal it got.")
        lpm = p["look_per_measure"]
        if lpm is not None and lpm < 1.0:
            out.append(f"layer {p['layer']} measured more than it looked (ratio {lpm}) — "
                       f"the pattern that has tracked failure.")
    ad = d["adoption"]
    for t in ad["never_used"]:
        out.append(f"`{t}` was never called across {len(ad['measured_layers'])} measured "
                   f"layer(s). It exists to {_DIAGNOSTIC[t]} — an unused tool means the "
                   f"BUILDER PROMPT is not landing, so fix the prompt before concluding "
                   f"the tool is not needed.")
    if not d["transcripts"]:
        out.append("no transcripts on disk — this shot last ran before durable "
                   "recording existed, so its inputs and tool calls are unrecoverable. "
                   "The next run will have them.")
    for t in d["transcripts"]:
        if t["unparseable"]:
            out.append(f"{t['file']} has {t['unparseable']} truncated line(s) — that "
                       f"process was killed mid-write.")
    return out


def _means(vals: list, keep: int = 6) -> str:
    """Round means for the table. A ten-round layer would otherwise blow the column width
    and misalign every row after it; the full sequence is in the findings and the JSON."""
    if not vals:
        return "—"
    if len(vals) <= keep:
        return " → ".join(str(v) for v in vals)
    head = " → ".join(str(v) for v in vals[:2])
    tail = " → ".join(str(v) for v in vals[-3:])
    return f"{head} …+{len(vals) - 5}… {tail}"


def report(d: dict) -> str:
    L = [f"── run digest · {d['shot']} ──",
         f"   {d['passed']}/{len(d['layers'])} layers passed · ${d['cost_usd']:.2f} · "
         f"{d['minutes']:.0f} min"]
    acc = d["acceptance"]
    if acc.get("total"):
        L.append(f"   acceptance {acc['passed']}/{acc['total']} moments"
                 + (f" · repair routes to layer(s) {', '.join(acc['repair_plan'])}"
                    if acc["repair_plan"] else ""))

    f = _findings(d)
    L.append("")
    if f:
        L.append(f"   ⚠ {len(f)} finding(s) — read these first:")
        for x in f:
            L.append(f"     · {x}")
    else:
        L.append("   ✓ nothing anomalous: every layer moved, replayed and got metric "
                 "feedback, and every diagnostic tool saw use.")

    L += ["", "   layers"]
    for p in d["layers"]:
        mark = {"passed": "✅", "failed": "✗", "judge_conflict": "⚠", "contract_gap": "◇"}.get(
            p["status"], "·")
        can = ("CONTRACT GAP" if p.get("canonical_contract_gap") else
               "JUDGE CONFLICT" if p.get("canonical_conflict") else
               "pixel reproduction ok" if p.get("canonical_reproduced") else
               "replay ok" if p["canonical_pass"] else
               "REPLAY FAILED" if p["canonical_pass"] is False else "no replay")
        L.append(f"     {mark} {p['layer']!s:<3} {(p['title'] or '')[:34]:<34} "
                 f"{_means(p['means']):<24} {p['trajectory']:<32} {can}")
        tools = (f"{p['tools_total']} tool calls" if p["tools_total"] is not None
                 else "tool use NOT RECORDED (report predates the telemetry)")
        L.append(f"        ${p['cost_usd']:.2f} · {p['turns']} turns · "
                 f"{p['minutes']:.0f} min · {tools}"
                 + (f" · look/measure {p['look_per_measure']}"
                    if p["look_per_measure"] is not None else ""))

    ad = d["adoption"]
    L += ["", "   diagnostic tool adoption (Phase 1/2 additions)"]
    if not ad["measured_layers"]:
        L.append("     not measured on any layer — every report here predates the "
                 "telemetry. Re-run a layer to populate it.")
    for t, why in _DIAGNOSTIC.items():
        n = ad["totals"].get(t, 0)
        flag = "  ← NEVER CALLED" if ad["measured_layers"] and not n else ""
        L.append(f"     {t:<13} {n:>4} call(s){flag}")
        L.append(f"                    {why}")
    if ad["unmeasured_layers"]:
        L.append(f"     (layer(s) {', '.join(ad['unmeasured_layers'])} predate this "
                 f"telemetry — not counted as zero)")

    L += ["", "   transcripts (inputs, outputs, every tool call)"]
    if not d["transcripts"]:
        L.append("     none")
    for t in d["transcripts"]:
        L.append(f"     {t['file']:<34} {t['events']:>6} events · {t['kb']:>5}KB · "
                 f"{t['critic_calls']} critic call(s) · {t['tool_errors']} tool error(s) · "
                 f"{t['images_shown']} image(s) shown · "
                 f"{t.get('focus_panels', 0)} focus panel(s)")
        if t["critic_means"]:
            L.append(f"        means {', '.join(str(m) for m in t['critic_means'])}")
    return "\n".join(L)


def timeline(path: Path, *, limit: int = 0) -> str:
    """The readable spine of one transcript: prompts, tool calls, verdicts, outcome.

    Not the whole file — thinking and prose are the bulk of it and are better read in the
    JSONL directly. This is the sequence of ACTIONS, which is what you want when the
    question is "what did it actually do, in what order".
    """
    out = [f"── timeline · {path.name} ──"]
    for e in transcript.read(path):
        k = e.get("kind")
        dt = e.get("dt")
        stamp = f"  [{dt:>7}s] " if isinstance(dt, (int, float)) else "  [      ?] "
        if k == "prompt":
            out.append(f"{stamp}PROMPT ({e.get('role')}) {len(e.get('text') or '')} chars")
        elif k == "tool_use":
            args = e.get("input") or {}
            bits = {k2: v for k2, v in args.items()
                    if k2 not in ("script", "code", "content")}
            note = json.dumps(bits, default=str)[:110]
            if "script" in args:
                note += f"  [script {len(str(args['script']))} chars]"
            out.append(f"{stamp}→ {e.get('tool')} {note}")
        elif k == "tool_result" and e.get("is_error"):
            out.append(f"{stamp}←  ERROR {str(e.get('content'))[:160]}")
        elif k == "critic":
            out.append(f"{stamp}⚖ CRITIC f{e.get('frame')} mean {e.get('mean')} "
                       f"{str(e.get('verdict')).upper()}"
                       + ("  (borderline)" if e.get("borderline") else ""))
            for i in (e.get("issues") or [])[:3]:
                out.append(f"{' ' * 14}· {str(i)[:130]}")
        elif k == "result":
            out.append(f"{stamp}■ {e.get('subtype')} · {e.get('turns')} turns · "
                       f"${e.get('cost_usd') or 0:.2f}")
        elif k in ("layer_end", "accept_end", "died"):
            detail = {a: b for a, b in e.items() if a not in ("kind", "dt", "seq")}
            out.append(f"{stamp}▣ {k}: "
                       f"{json.dumps(detail, default=str)[:200]}")
        if limit and len(out) > limit:
            out.append(f"  … truncated at {limit} lines")
            break
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="vfx_harness.application.inspect_run", description=__doc__.split("\n")[0])
    ap.add_argument("folder")
    ap.add_argument("--layer", help="print the action timeline for this layer's transcript")
    ap.add_argument("--stage", default="build", help="stage for --layer (build/plan/accept)")
    ap.add_argument("--tools", action="store_true", help="adoption only")
    ap.add_argument("--history", action="store_true",
                    help="include transcripts from prior attempts")
    ap.add_argument("--run", help="inspect one structured run id (default: latest)")
    ap.add_argument("--list-runs", action="store_true",
                    help="list structured runs and their terminal status")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    shot = load_shot(a.folder)
    if a.list_runs:
        rows = run_artifacts.list_runs(shot.folder)
        if a.json:
            print(json.dumps(rows, indent=2))
        elif not rows:
            print("no structured runs")
        else:
            for row in rows:
                print(f"{row['run_id']}  {row['state']:<12}  {row.get('started_at') or '-'}")
        return 0
    if run_artifacts.select(shot.folder, a.run) is None:
        target = f"run {a.run!r}" if a.run else "a structured run"
        raise SystemExit(
            f"no {target} under {shot.folder / 'runs'}; run the project first. "
            "Shot-root legacy output is unsupported."
        )
    if a.layer:
        current = next((rec for rec in layers(shot.folder, run_id=a.run)
                        if str(rec.get("layer")) == str(a.layer)), {})
        run_id = a.run or (None if a.history else current.get("run_id"))
        hits = [p for p in transcript.find(shot.folder, stage=a.stage, run_id=run_id)
                if f"layer{a.layer}-" in p.name or p.stem.endswith(f"layer{a.layer}")]
        if not hits:
            have = ", ".join(p.name for p in transcript.find(shot.folder)) or "none"
            raise SystemExit(f"no {a.stage} transcript for layer {a.layer}. have: {have}")
        for p in hits:
            print(timeline(p))
        return 0

    d = collect(shot.folder, history=a.history, run_id=a.run)
    if a.json:
        print(json.dumps(d, indent=2))
        return 0
    if a.tools:
        print(json.dumps(d["adoption"], indent=2))
        return 0
    print(report(d))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
