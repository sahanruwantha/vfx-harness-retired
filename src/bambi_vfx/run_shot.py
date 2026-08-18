"""Drive a whole shot: every layer, then acceptance, then the mp4.

    python -m bambi_vfx.run_shot <shot-folder> [--from 1] [--upto 8] [--rounds 2]
                                              [--skip-render] [--dry-run]

This existed only as a shell script in a scratchpad, which meant two things that matter
were not part of the pipeline: the run id was minted per-layer, so a shot's eight layers
recorded themselves as eight unrelated runs and could not be compared as one; and the
halt-on-failure lived outside the code, so anyone running layers by hand got none of it.

Each layer is a SEPARATE PROCESS on purpose. A layer holds a warm Blender session, a
long SDK conversation and a growing context; recycling the process between layers is what
keeps one layer's leak from becoming the next layer's problem. They share a run id through
the environment (see bambi_vfx/runid.py).

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
from .config import Settings
from .ledger import Ledger, load_layers
from .log import log
from .preflight import warn_if_broken
from .runid import RUN_ID

_MEANING = {
    0: "ok", 1: "crashed", 3: "TRUNCATED — raise the budget or split the layer",
    4: "CHAIN BROKEN — a prior layer's script no longer composes",
    5: "unanswered plan questions — settle them first",
    6: "UNACCEPTED PRIOR — a lower layer must pass first",
    7: "INCOMPLETE CHAIN", 8: "plan is STALE against brief.md — re-plan",
    9: "layer ran cleanly but its VERDICT was not a pass",
}


def _run(args: list[str], *, dry: bool, tee: Path | None = None) -> int:
    """Run a stage, mirroring its console output to `tee` as it happens.

    Every stage inherited this process's stdout, so the run's whole narrative — the
    critic's per-axis lines, the chain guard, the end-of-layer report block — existed only
    in a terminal scrollback. `logs/run_layer*.json` holds the aggregates and
    `logs/transcript/` now holds the structured record, but neither is the thing you
    actually re-read after a bad run, which is the pretty-printed sequence in order.

    Mirrored rather than redirected: watching a live run is how you notice a Blender
    session wedged on one frame, and a run you cannot watch is worse than one you cannot
    re-read. PYTHONUNBUFFERED because a pipe makes the child block-buffer, and a stage
    whose output arrives in 8KB bursts is not watchable.
    """
    log(f"$ {' '.join(args)}")
    if dry:
        return 0
    env = {**os.environ, "BVFX_RUN_ID": RUN_ID, "PYTHONUNBUFFERED": "1",
           "BVFX_STAGE_ARGV": " ".join(args)}
    if tee is None:
        return subprocess.call(args, env=env)
    tee.parent.mkdir(parents=True, exist_ok=True)
    with tee.open("a", encoding="utf-8") as fh:
        fh.write(f"\n{'=' * 78}\n$ {' '.join(args)}\nrun {RUN_ID}\n{'=' * 78}\n")
        fh.flush()
        proc = subprocess.Popen(args, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                errors="replace", bufsize=1)
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            fh.write(line)
            fh.flush()
        return proc.wait()


def main() -> None:
    ap = argparse.ArgumentParser(description="Run every stage of a shot under one run id.")
    ap.add_argument("folder")
    ap.add_argument("--from", dest="start", type=int, default=1, help="first layer id")
    ap.add_argument("--upto", type=int, default=None, help="last layer id")
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--blender", default=Settings.from_environment().blender_bin)
    ap.add_argument("--scale", type=float, default=1.0, help="render scale for the mp4")
    ap.add_argument("--skip-render", action="store_true")
    ap.add_argument("--skip-accept", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the plan and the layers that would run, execute nothing")
    a = ap.parse_args()

    warn_if_broken()
    shot = load_shot(a.folder)
    layers = load_layers(shot)
    ids = sorted(layers, key=lambda k: str(layers[k].script))
    ids = [i for i in ids if a.start <= int(i) <= (a.upto or 10 ** 6)]
    if not ids:
        raise SystemExit(f"no layers in range {a.start}..{a.upto} (have: {', '.join(layers)})")

    ledger = Ledger(shot)
    done = [i for i in ids if ledger.status(layers[i].as_milestone()) == "passed"]
    py = sys.executable
    console = shot.folder / "logs" / "console" / f"{RUN_ID}.log"
    log(f"run {RUN_ID} · shot '{shot.id}' · layers {ids[0]}–{ids[-1]} "
        f"({len(done)} already passed) · rounds {a.rounds}")
    if done:
        log(f"  already passed, will be SKIPPED: {', '.join(done)}", 1)
    if not a.dry_run:
        log(f"  console → {console.relative_to(shot.folder)} · structured → "
            f"logs/transcript/ · digest: python -m bambi_vfx.inspect_run {shot.folder}", 1)

    t0 = time.monotonic()
    for lid in ids:
        if lid in done:
            continue
        log(f"════ LAYER {lid} — {layers[lid].title} ════")
        rc = _run([py, "-m", "bambi_vfx.agents.builder", str(shot.folder),
                   "--layer", lid, "--rounds", str(a.rounds), "--blender", a.blender],
                  dry=a.dry_run, tee=console)
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
        rc = _run([py, "-m", "bambi_vfx.agents.acceptance", str(shot.folder),
                   "--blender", a.blender], dry=a.dry_run, tee=console)
        if rc:
            log(f"✗ acceptance exited {rc}: {_MEANING.get(rc, 'unknown')}")
            raise SystemExit(rc)

    if not a.skip_render:
        log("════ RENDER ════")
        rc = _run([py, "-m", "bambi_vfx.render_shot", str(shot.folder),
                   "--scale", str(a.scale), "--blender", a.blender],
                  dry=a.dry_run, tee=console)
        if rc:
            log(f"✗ render exited {rc}: {_MEANING.get(rc, 'unknown')}")
            raise SystemExit(rc)

    # Harvest recipes only now — nothing in this run consumes them, and doing it between
    # layers made the run wait on a model writing prose.
    if (shot.folder / "logs" / "distill_queue.jsonl").is_file():
        log("════ DISTILL (queued during the run) ════")
        _run([py, "-m", "bambi_vfx.distill", str(shot.folder)], dry=a.dry_run, tee=console)

    log(f"run {RUN_ID} finished in {(time.monotonic() - t0) / 60:.0f} min")
    log(f"  per-layer reports: {shot.folder / 'logs'}/run_layer*.json")
    log(f"  console log:       {console}")
    log(f"  transcripts:       {shot.folder / 'logs' / 'transcript'}/*.jsonl")
    log(f"  digest:            python -m bambi_vfx.inspect_run {shot.folder}")


if __name__ == "__main__":
    main()
