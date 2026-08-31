"""Phase 0.1 — blank-frame control.

Score each axis with the reference and a black frame. That is the language-prior
floor: if `lighting_and_form` scores 2.0 against a black rectangle, sixteen rounds
of renders carried no visual information and Phase 2 is premature for that axis.

Writes under `artifacts/evaluations/blank/`, never into the shot folder.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

import anyio
from PIL import Image

from vfx_harness.domain.brief import Shot, load_shot
from vfx_harness.observability.log import log
from vfx_harness.orchestration import authority_selection
from vfx_harness.orchestration.ledger import Milestone, load_axes, load_layers

from ..agents.builder import _critique
from . import STORE
from .variance import _NoSession, layer_scope

BLANK = STORE / "blank"


def _black_png(ref_path: Path, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(ref_path) as im:
        Image.new("RGB", im.size, (0, 0, 0)).save(dest)
    return dest


def language_prior(axis: dict) -> bool:
    """An axis that scores ≥2.0 on a black frame is not image-dependent at the pass line."""
    vals = axis.get("values") or []
    if not vals:
        return False
    return sum(vals) / len(vals) >= 2.0


async def measure(shot: Shot, *, layer_id: str | None = None,
                  ref_rel: str | None = None, verbose: bool = True) -> dict:

    selected_authority = authority_selection.resolve_selected_authority(shot.folder)
    axes = load_axes(shot, selected_authority)
    scope = None
    reads = ""
    frame = 1
    if layer_id:
        layer = load_layers(
            shot,
            selected_authority=selected_authority,
        )[layer_id]
        scope = layer_scope(
            shot,
            layer,
            selected_authority=selected_authority,
        )
        frame = layer.judge_frame
        reads = layer.reads
        ref_rel = ref_rel or layer.judge_ref
    if not ref_rel:
        raise FileNotFoundError("pass --ref or --layer so there is a reference to pair")
    ref_abs = shot.folder / ref_rel
    if not ref_abs.is_file():
        raise FileNotFoundError(f"{ref_abs} does not exist")

    dest = BLANK / shot.id / "black.png"
    _black_png(ref_abs, dest)
    # The black frame is written under artifacts/evaluations/, never into the shot — nothing in this
    # package may leave state a build could later read as its own work. `_critique`
    # resolves the candidate as `shot.folder / candidate_rel`, and on POSIX an absolute
    # right-hand side wins that join, so an absolute path reaches the critic unchanged.
    cand_abs = str(dest.resolve())
    m = Milestone("BLANK", frame, ref_rel, reads or "(blank-frame control)", ())
    log(f"blank-frame control: black {dest.name} vs {ref_rel}"
        + (f" under layer {layer_id} scope" if layer_id else " on the FULL rubric"))
    verdict = await _critique(
        shot,
        m,
        cand_abs,
        axes,
        _NoSession(),
        verbose,
        scope,
        selected_authority=selected_authority,
    )

    per_axis = {}
    for key, _desc in axes:
        raw = verdict.get("scores", {}).get(key)
        scored = isinstance(raw, (int, float)) and not isinstance(raw, bool)
        per_axis[key] = {
            "values": [float(raw)] if scored else [],
            "n_a": 0 if scored else 1,
            "language_prior": language_prior({"values": [float(raw)]}) if scored else False,
        }

    priors = [k for k, d in per_axis.items() if d["language_prior"]]
    rec = {
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "shot": shot.id,
        "ref": ref_rel,
        "layer": layer_id,
        "scope": "layer" if layer_id else "full-rubric",
        "candidate": "black",
        "mean": verdict.get("mean"),
        "pass": bool(verdict.get("pass")),
        "scores": verdict.get("scores", {}),
        # The issue text is the point of this experiment, not a side effect. A score that
        # drops on black proves the critic READ the image; it says nothing about whether
        # the ADVICE was read off it. On this shot the black frame drew six confident,
        # specific fixes — "3/4 front-left, 60-70% of hero height, fill at 1/6-1/8 the key,
        # cooler in hue" — which are the same numbers layer 5 chased for sixteen rounds
        # against real renders. Persist them so that comparison can be made rather than
        # remembered.
        "issues": verdict.get("issues", []),
        "per_axis": per_axis,
        "language_prior_axes": priors,
        "gate": ("Phase 2 is premature for: " + ", ".join(priors)
                 if priors else
                 "every scored axis dropped on a black frame — image-dependent"),
    }
    return rec


def report(rec: dict) -> str:
    lines = [
        f"── blank-frame control · {rec['shot']} · black vs {rec['ref']} ──",
        f"   scope {rec['scope']}" + (f" (layer {rec['layer']})" if rec["layer"] else ""),
        f"   mean {rec['mean']} · critic said "
        + ("PASS — the language prior cleared the bar" if rec["pass"] else "REVISE"),
        "",
        "   per axis (a score ≥2.0 on black is the language-prior floor):",
    ]
    for key, d in rec["per_axis"].items():
        if not d["values"]:
            lines.append(f"     {key:<26} n/a")
            continue
        flag = "  ⚠ LANGUAGE PRIOR — this axis is a rubric problem, not a pixel problem" \
            if d["language_prior"] else ""
        lines.append(f"     {key:<26} {d['values'][0]}{flag}")
    lines += ["", f"   gate: {rec['gate']}"]
    return "\n".join(lines)


def save(rec: dict) -> Path:
    stamp = rec["at"].replace(":", "").replace("-", "")
    out = BLANK / rec["shot"] / f"{stamp}_{rec['scope']}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rec, indent=2) + "\n", encoding="utf-8")
    return out


def main(argv: list[str]) -> int:


    ap = argparse.ArgumentParser(prog="vfx_harness.evaluation.cli blank")
    ap.add_argument("folder", help="shot folder")
    ap.add_argument("--layer", help="score under this layer's production scope")
    ap.add_argument("--ref", help="reference path relative to the shot folder")
    ap.add_argument("--no-save", action="store_true")
    args = ap.parse_args(argv)

    shot = load_shot(args.folder)
    rec = anyio.run(lambda: measure(shot, layer_id=args.layer, ref_rel=args.ref))
    print("\n" + report(rec))
    if not args.no_save:
        print(f"\n→ {save(rec)}")
    return 0
