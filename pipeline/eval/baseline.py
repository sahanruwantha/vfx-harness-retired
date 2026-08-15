"""Freeze the current state of a shot so a later state can be compared against it.

A baseline is the only thing that turns "this run looked better" into a statement anyone
can check. It has to survive the run it describes: `build/`, `renders/` and `shot.json`
are all rewritten in place by the next build, so a baseline that pointed INTO the shot
folder would silently start describing the new run. Hence: script bodies are copied in
full, renders are recorded by content hash, and the whole record lands under
`evals/baselines/<shot>/`.

What goes in is chosen by what a comparison needs, in priority order:

  final acceptance   the PRIMARY decision statistic (review §"Evals to run"): final task
                     success, not mean tokens, not layer pass rate, not critic score —
                     all three of which are gameable by the change being tested.
  layer verdicts     secondary and known to be a weak predictor of final success.
  telemetry          cost/tokens/turns/hooks. Efficiency only, never a quality argument.
  hashes             renders, scripts and plan artifacts, so "did this actually change?"
                     is answerable without eyeballing images.

run_id and attempt are carried through from the ledger unchanged. Two runs of the same
shot used to be indistinguishable on disk, which made paired evaluation impossible; the
baseline is where that identity finally gets used for something.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from ..brief import Shot, load_shot
from ..ledger import load_layers
from . import BASELINES

SCHEMA = 1


def _sha(path: Path) -> str | None:
    if not path.is_file():
        return None
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _git() -> dict:
    """Which commit produced this state. A baseline with no code identity can be
    compared against but cannot be explained."""
    def run(*args: str) -> str:
        try:
            return subprocess.run(["git", *args], capture_output=True, text=True,
                                  timeout=10, cwd=str(Path(__file__).resolve().parents[2])
                                  ).stdout.strip()
        except Exception:       # noqa: BLE001 — reported below as unknown, not swallowed
            return ""
    dirty = run("status", "--porcelain")
    return {"commit": run("rev-parse", "HEAD")[:12] or "unknown",
            "branch": run("rev-parse", "--abbrev-ref", "HEAD") or "unknown",
            # An uncommitted tree means the commit does NOT identify the code that ran.
            # Comparing two baselines that were both taken dirty is comparing unknowns.
            "dirty": bool(dirty),
            "dirty_files": sorted(ln[3:] for ln in dirty.splitlines())[:40]}


def _round_record(shot: Shot, r: dict) -> dict:
    render = r.get("render") or ""
    return {
        "round": r.get("round"),
        "kind": r.get("kind"),
        "mean": r.get("mean"),
        "pass": r.get("pass"),
        "scores": r.get("scores", {}),
        "panel": r.get("panel"),
        "round_s": r.get("round_s"),
        "run_id": r.get("run_id"),
        "attempt": r.get("attempt"),
        "render": render,
        "render_sha": _sha(shot.folder / render) if render else None,
    }


def _layer_log(shot: Shot, layer_id: str) -> dict:
    """The per-layer run record (logs/run_layerN.json) — cost, tokens, turns, hooks.

    Kept whole rather than summarised: the fields that turn out to matter are the ones
    nobody thought to extract. A metrics hook no-opped for an entire build phase and the
    only trace was a counter sitting at zero in this file.
    """
    p = shot.folder / "logs" / f"run_layer{layer_id}.json"
    if not p.is_file():
        return {"present": False, "why": f"logs/run_layer{layer_id}.json not written"}
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return {"present": False, "why": f"unreadable: {e}"}
    return {
        "present": True,
        "cost_usd": rec.get("cost_usd"),
        "turns": rec.get("turns"),
        "seconds": rec.get("seconds"),
        "tokens": rec.get("tokens") or {},
        "hooks": rec.get("hooks") or {},
        "journal": rec.get("journal") or {},
        "recipes_pulled": rec.get("recipes_pulled") or [],
        "canonical": rec.get("canonical") or [],
        "approach": rec.get("approach"),
        "reviews": len(rec.get("reviews") or []),
    }


def _final(shot: Shot, ledger_data: dict) -> dict:
    """The primary statistic: did the FINISHED chain pass its acceptance moments.

    Absent until the accept stage has run over a complete chain. Its absence is recorded
    explicitly rather than defaulted to zero — "0 of 10 passed" and "acceptance was never
    run" support completely different conclusions, and conflating them is how a partial
    build gets read as a quality regression.
    """
    acc = ledger_data.get("acceptance")
    if not isinstance(acc, dict) or not acc.get("moments"):
        return {"present": False,
                "why": "no acceptance block in shot.json — the accept stage has not "
                       "judged a finished chain for this shot"}
    moments = {}
    for mid, r in acc["moments"].items():
        render = r.get("render") or ""
        moments[mid] = {
            "frame": r.get("frame"),
            "pass": bool(r.get("pass")),
            "critic_pass": r.get("critic_pass"),
            "mean": r.get("mean"),
            "decided_by": r.get("decided_by"),
            "metric_failures": r.get("metric_failures", []),
            "scores": r.get("scores", {}),
            "render": render,
            "render_sha": _sha(shot.folder / render) if render else None,
        }
    return {"present": True,
            "passed": acc.get("passed", sum(1 for m in moments.values() if m["pass"])),
            "total": acc.get("total", len(moments)),
            "scripts": acc.get("scripts", []),
            "repair_plan": acc.get("repair_plan", []),
            "moments": moments}


def freeze(shot: Shot, *, label: str = "", note: str = "",
           with_script_text: bool = True) -> dict:
    """Build the baseline record for `shot` as it stands right now."""
    ledger_path = shot.folder / "shot.json"
    data = json.loads(ledger_path.read_text(encoding="utf-8")) if ledger_path.is_file() else {}

    try:
        layers = load_layers(shot)
    except Exception as e:      # noqa: BLE001 — a shot with no plan can still be frozen
        layers, plan_error = {}, str(e)
    else:
        plan_error = ""

    slots = data.get("milestones", {})
    layer_rec: dict[str, dict] = {}
    # Union of the plan's layers and whatever the ledger recorded. They CAN disagree —
    # a ledger slot with no matching plan layer is itself worth freezing, because that
    # disagreement is the bug (see eval.integrity).
    for lid in sorted(set(slots) | set(layers), key=lambda s: (len(s), s)):
        slot = slots.get(lid, {})
        plan_layer = layers.get(lid)
        layer_rec[lid] = {
            "title": getattr(plan_layer, "title", slot.get("title", "")),
            "status": slot.get("status", "absent"),
            "owns": list(getattr(plan_layer, "owns", ()) or []),
            "run_id": slot.get("run_id"),
            "attempt": slot.get("attempt"),
            "best": slot.get("best"),
            "ablation": slot.get("ablation"),
            "superseded_by_acceptance": slot.get("superseded_by_acceptance"),
            "plan_script": getattr(plan_layer, "script", None),
            "ledger_script": slot.get("script"),
            "rounds": [_round_record(shot, r) for r in slot.get("rounds", [])],
            "telemetry": _layer_log(shot, lid),
        }

    scripts: dict[str, dict] = {}
    build_dir = shot.folder / "build"
    if build_dir.is_dir():
        for p in sorted(build_dir.glob("[0-9]*.py")):
            rec = {"sha": _sha(p), "bytes": p.stat().st_size}
            if with_script_text:
                # The body, not a pointer. The next build rewrites build/ in place, and a
                # baseline that cannot show what the accepted script SAID can only tell
                # you that something changed, never what.
                rec["text"] = p.read_text(encoding="utf-8")
            scripts[p.name] = rec

    renders: dict[str, str | None] = {}
    rdir = shot.folder / "renders"
    if rdir.is_dir():
        for p in sorted(rdir.iterdir()):
            if p.is_file():
                renders[f"renders/{p.name}"] = _sha(p)

    plan_files = {n: _sha(shot.folder / n) for n in
                  ("brief.md", "plan.md", "layers.json", "acceptance.json",
                   "critic_axes.json", "plan.provenance.json")}

    return {
        "schema": SCHEMA,
        "shot": shot.id,
        "shot_folder": str(shot.folder),
        "label": label,
        "note": note,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git": _git(),
        "runs": data.get("runs", []),
        "plan_error": plan_error,
        "final": _final(shot, data),
        "layers": layer_rec,
        "scripts": scripts,
        "renders": renders,
        "plan_files": plan_files,
    }


def save(rec: dict, *, label: str = "") -> Path:
    stamp = rec["at"].replace(":", "").replace("-", "")
    name = f"{stamp}" + (f"_{label}" if label else "")
    out = BASELINES / rec["shot"] / f"{name}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rec, indent=2) + "\n", encoding="utf-8")
    return out


def resolve(ref: str) -> Path:
    """Accept a path, a `<shot>/<name>` pair, or `<shot>:latest`."""
    p = Path(ref)
    if p.is_file():
        return p
    if ref.endswith(":latest") or ref.endswith("/latest"):
        shot = ref.rsplit(":", 1)[0].rsplit("/", 1)[0] if ":" in ref else ref.rsplit("/", 1)[0]
        found = sorted((BASELINES / shot).glob("*.json"))
        if not found:
            raise FileNotFoundError(f"no baselines frozen for shot {shot!r} under {BASELINES}")
        return found[-1]
    cand = BASELINES / ref
    if cand.is_file():
        return cand
    for suffix in (".json",):
        if (BASELINES / (ref + suffix)).is_file():
            return BASELINES / (ref + suffix)
    raise FileNotFoundError(f"no baseline at {ref!r} (looked in {BASELINES})")


def load(ref: str) -> dict:
    path = resolve(ref)
    rec = json.loads(path.read_text(encoding="utf-8"))
    rec.setdefault("_path", str(path))
    if rec.get("schema") != SCHEMA:
        # Refusing beats silently comparing fields that moved. A comparison of two
        # differently-shaped records is exactly the kind of confident wrong number this
        # whole package exists to prevent.
        raise ValueError(f"{path} is schema {rec.get('schema')}, this build reads "
                         f"schema {SCHEMA} — re-freeze, or compare with the older code")
    return rec


def summarise(rec: dict) -> str:
    f = rec["final"]
    head = (f"baseline {rec['shot']} · {rec['at']} · {rec.get('label') or '(no label)'}\n"
            f"  git      {rec['git']['commit']}"
            f"{' DIRTY' if rec['git']['dirty'] else ''} on {rec['git']['branch']}\n")
    if f.get("present"):
        head += f"  FINAL    {f['passed']}/{f['total']} acceptance moments passed\n"
    else:
        head += f"  FINAL    unavailable — {f.get('why')}\n"
    passed = sum(1 for L in rec["layers"].values() if L["status"] == "passed")
    cost = sum(L["telemetry"].get("cost_usd") or 0.0 for L in rec["layers"].values())
    head += (f"  layers   {passed}/{len(rec['layers'])} passed (secondary — a weak "
             f"predictor of final success)\n"
             f"  cost     ${cost:.2f} across the layers that reported it "
             f"(telemetry only)\n"
             f"  frozen   {len(rec['scripts'])} scripts · {len(rec['renders'])} renders")
    return head


def main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="pipeline.evals freeze")
    ap.add_argument("folder", help="shot folder (contains brief.md + shot.json)")
    ap.add_argument("--label", default="", help="short tag, e.g. before-context-editing")
    ap.add_argument("--note", default="", help="what this baseline is for")
    ap.add_argument("--no-script-text", action="store_true",
                    help="store script hashes only (smaller; not replayable)")
    args = ap.parse_args(argv)
    shot = load_shot(args.folder)
    rec = freeze(shot, label=args.label, note=args.note,
                 with_script_text=not args.no_script_text)
    out = save(rec, label=args.label)
    print(summarise(rec))
    print(f"\n→ {out}")
    return 0
