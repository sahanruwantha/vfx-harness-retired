"""Drive a whole shot: every layer, then acceptance, then the mp4.

    python -m pipeline.run_shot <shot-folder> [--from 1] [--upto 8] [--rounds 2]
                                              [--skip-render] [--dry-run]

This existed only as a shell script in a scratchpad, which meant two things that matter
were not part of the pipeline: the run id was minted per-layer, so a shot's eight layers
recorded themselves as eight unrelated runs and could not be compared as one; and the
halt-on-failure lived outside the code, so anyone running layers by hand got none of it.

Each layer is a SEPARATE PROCESS on purpose. A layer holds a warm Blender session, a
long SDK conversation and a growing context; recycling the process between layers is what
keeps one layer's leak from becoming the next layer's problem. They share a run id through
the environment (see pipeline/runid.py).

Exit codes are propagated, not flattened, because they say different things:
    3 truncated (budget)   4 chain broken       5 unanswered questions
    6 unaccepted prior     7 incomplete chain   8 plan is stale vs the brief
    9 ran cleanly but the VERDICT was not a pass
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

from .brief import load_shot
from .ledger import Ledger, load_layers
from .log import log
from .runid import RUN_ID

_MEANING = {
    0: "ok", 1: "crashed", 3: "TRUNCATED — raise the budget or split the layer",
    4: "CHAIN BROKEN — a prior layer's script no longer composes",
    5: "unanswered plan questions — settle them first",
    6: "UNACCEPTED PRIOR — a lower layer must pass first",
    7: "INCOMPLETE CHAIN", 8: "plan is STALE against brief.md — re-plan",
    9: "layer ran cleanly but its VERDICT was not a pass",
}


def _run(args: list[str], *, dry: bool) -> int:
    log(f"$ {' '.join(args)}")
    if dry:
        return 0
    return subprocess.call(args, env={**os.environ, "BVFX_RUN_ID": RUN_ID})


def main() -> None:
    ap = argparse.ArgumentParser(description="Run every stage of a shot under one run id.")
    ap.add_argument("folder")
    ap.add_argument("--from", dest="start", type=int, default=1, help="first layer id")
    ap.add_argument("--upto", type=int, default=None, help="last layer id")
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--blender", default=os.environ.get("BLENDER_BIN", "blender"))
    ap.add_argument("--scale", type=float, default=1.0, help="render scale for the mp4")
    ap.add_argument("--skip-render", action="store_true")
    ap.add_argument("--skip-accept", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the plan and the layers that would run, execute nothing")
    a = ap.parse_args()

    shot = load_shot(a.folder)
    layers = load_layers(shot)
    ids = sorted(layers, key=lambda k: str(layers[k].script))
    ids = [i for i in ids if a.start <= int(i) <= (a.upto or 10 ** 6)]
    if not ids:
        raise SystemExit(f"no layers in range {a.start}..{a.upto} (have: {', '.join(layers)})")

    ledger = Ledger(shot)
    done = [i for i in ids if ledger.status(layers[i].as_milestone()) == "passed"]
    py = sys.executable
    log(f"run {RUN_ID} · shot '{shot.id}' · layers {ids[0]}–{ids[-1]} "
        f"({len(done)} already passed) · rounds {a.rounds}")
    if done:
        log(f"  already passed, will be SKIPPED: {', '.join(done)}", 1)

    t0 = time.monotonic()
    for lid in ids:
        if lid in done:
            continue
        log(f"════ LAYER {lid} — {layers[lid].title} ════")
        rc = _run([py, "-m", "pipeline.build_agent", str(shot.folder),
                   "--layer", lid, "--rounds", str(a.rounds), "--blender", a.blender],
                  dry=a.dry_run)
        if rc:
            # Stop. Building layer N+1 on a layer N that never passed is the failure this
            # whole chain of guards exists to prevent; carrying on would just bury it.
            log(f"✗ layer {lid} exited {rc}: {_MEANING.get(rc, 'unknown')}")
            log(f"   stopping after {(time.monotonic() - t0) / 60:.0f} min. "
                f"Fix, then resume with --from {lid}")
            raise SystemExit(rc)

        # An exit code says the PROCESS completed; the ledger says the WORK was accepted.
        # Conflating them is why this driver announced "✓ layer 2 passed" for a layer whose
        # own report read FAILED and whose four judged frames all scored 2.0: build_agent
        # exits 0 for a layer that builds fine and then fails its verdict — only crashes,
        # truncation and chain breaks raise. The chain guard caught it 0.2s into layer 3,
        # which is the system working, but the driver should not have needed rescuing.
        status = Ledger(shot).status(layers[lid].as_milestone())
        if status != "passed":
            log(f"✗ layer {lid} finished cleanly but its verdict is '{status}' — not "
                f"building on it")
            log(f"   stopping after {(time.monotonic() - t0) / 60:.0f} min. "
                f"See {shot.folder}/logs/run_layer{lid}.json, then resume with --from {lid}")
            raise SystemExit(9)
        log(f"✓ layer {lid} passed ({(time.monotonic() - t0) / 60:.0f} min elapsed)")

    if not a.skip_accept:
        log("════ ACCEPTANCE ════")
        rc = _run([py, "-m", "pipeline.accept_agent", str(shot.folder),
                   "--blender", a.blender], dry=a.dry_run)
        if rc:
            log(f"✗ acceptance exited {rc}: {_MEANING.get(rc, 'unknown')}")
            raise SystemExit(rc)

    if not a.skip_render:
        log("════ RENDER ════")
        rc = _run([py, "-m", "pipeline.render_shot", str(shot.folder),
                   "--scale", str(a.scale), "--blender", a.blender], dry=a.dry_run)
        if rc:
            log(f"✗ render exited {rc}: {_MEANING.get(rc, 'unknown')}")
            raise SystemExit(rc)

    # Harvest recipes only now — nothing in this run consumes them, and doing it between
    # layers made the run wait on a model writing prose.
    if (shot.folder / "logs" / "distill_queue.jsonl").is_file():
        log("════ DISTILL (queued during the run) ════")
        _run([py, "-m", "pipeline.distill", str(shot.folder)], dry=a.dry_run)

    log(f"run {RUN_ID} finished in {(time.monotonic() - t0) / 60:.0f} min")
    log(f"  per-layer reports: {shot.folder / 'logs'}/run_layer*.json")


if __name__ == "__main__":
    main()
