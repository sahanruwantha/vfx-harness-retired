"""Drive a whole shot: every layer, then acceptance, then the mp4.

    python -m vfx_harness.application.run_shot <shot-folder> [--from 1] [--upto 8] [--rounds 2]
                                              [--skip-render] [--dry-run]

This existed only as a shell script in a scratchpad, which meant two things that matter
were not part of the pipeline: the run id was minted per-layer, so a shot's eight layers
recorded themselves as eight unrelated runs and could not be compared as one; and the
halt-on-failure lived outside the code, so anyone running layers by hand got none of it.

Each layer is a SEPARATE PROCESS on purpose. A layer holds a warm Blender session, a
long SDK conversation and a growing context; recycling the process between layers is what
keeps one layer's leak from becoming the next layer's problem. They share a run id through
the environment (see vfx_harness/observability/runid.py).

Exit codes are propagated for CLI compatibility and operator summaries only:
    3 truncated (budget)   4 chain broken       5 unanswered questions
    6 unaccepted prior     7 incomplete chain   8 plan is stale vs the brief
    9 ran cleanly but the VERDICT was not a pass

They are never recovery authority. A failed/interrupted run selects one immutable
typed stop envelope; a missing child envelope fails closed as a harness defect.
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from vfx_harness.application.inspect_run import collect
from vfx_harness.application.preflight import environment_result, environment_stop
from vfx_harness.application.preflight import probe as preflight_probe
from vfx_harness.application.preflight import report as preflight_report
from vfx_harness.domain.brief import load_shot
from vfx_harness.domain.stop_envelopes import StopEnvelope
from vfx_harness.observability import run_artifacts
from vfx_harness.observability.log import log
from vfx_harness.observability.runid import RUN_ID
from vfx_harness.orchestration.ledger import Ledger, load_layers

_MEANING = {
    0: "ok", 1: "crashed", 3: "TRUNCATED — raise the budget or split the layer",
    4: "CHAIN BROKEN — a prior layer's script no longer composes",
    5: "unanswered plan questions — settle them first",
    6: "UNACCEPTED PRIOR — a lower layer must pass first",
    7: "INCOMPLETE CHAIN", 8: "plan is STALE against brief.md — re-plan",
    9: "layer ran cleanly but its VERDICT was not a pass",
}


def _can_advance(status: str, *, dry_run: bool) -> bool:
    """Whether the driver may continue after a layer command.

    A dry run deliberately executes no builder, so it cannot change a pending ledger
    verdict into ``passed``.  Treating that unchanged verdict like a real build failure
    made ``--dry-run`` stop after its first unbuilt layer instead of previewing the run.
    """
    return dry_run or status == "passed"


def _publish_summary(layout: run_artifacts.RunLayout) -> None:
    try:
        layout.write_summary(collect(layout.shot, run_id=layout.run_id))
    except Exception as exc:
        log(f"! run summary could not be published: {str(exc)[:160]}", 1)


def _stop(layout: run_artifacts.RunLayout, code: int, envelope: StopEnvelope) -> None:
    layout.write_stop_envelope(envelope)
    detail = f"{envelope.stop_class}: {envelope.found} {envelope.next_action}"
    layout.set_status(
        "failed",
        exit_code=code,
        detail=detail,
        metadata={
            "terminal_cause": envelope.stop_class,
            "stop_envelope": "reports/stop-envelope.json",
            "stop_envelope_digest": envelope.digest,
            "stop_class": envelope.stop_class,
            "stop_stage": envelope.stage,
            "cause_fingerprint": envelope.cause_fingerprint,
        },
    )
    _publish_summary(layout)
    layout.write_inventory()
    raise SystemExit(code)


def _stop_after_stage(
    layout: run_artifacts.RunLayout,
    code: int,
    boundary: str,
) -> None:
    """Consume only the child-selected envelope; never dispatch from its exit code."""
    try:
        envelope = layout.read_terminal_stop()
    except ValueError:
        envelope = run_artifacts.missing_boundary_stop(
            layout,
            boundary,
            exit_code=code,
        )
    _stop(layout, code, envelope)


def _mark_interrupted(layout: run_artifacts.RunLayout) -> None:
    """Seal an otherwise-unclassified driver exit before the atexit boundary closes."""
    try:
        current = json.loads(layout.status.read_text(encoding="utf-8"))
        if current.get("state") != "running":
            return
    except (OSError, json.JSONDecodeError):
        pass
    try:
        envelope = run_artifacts.missing_boundary_stop(
            layout,
            "run-driver-interruption",
            exit_code=130,
        )
        layout.write_stop_envelope(envelope)
    except Exception as exc:
        layout.set_status(
            "failed",
            exit_code=1,
            detail=f"stop-envelope publication failed during driver interruption: {exc}",
            metadata={
                "terminal_cause": "stop_envelope_publication_failure",
                "stop_envelope_state": "unavailable",
            },
        )
    else:
        layout.set_status(
            "interrupted",
            exit_code=130,
            detail="driver exited without a terminal result",
            metadata={
                "terminal_cause": "interrupted",
                "stop_envelope": "reports/stop-envelope.json",
                "stop_envelope_digest": envelope.digest,
                "stop_class": envelope.stop_class,
                "stop_stage": envelope.stage,
                "cause_fingerprint": envelope.cause_fingerprint,
            },
        )
    _publish_summary(layout)
    layout.write_inventory()


def _run(args: list[str], *, dry: bool, tee: Path | None = None) -> int:
    """Run a stage, mirroring its console output to `tee` as it happens.

    Every stage inherited this process's stdout, so the run's whole narrative — the
    critic's per-axis lines, the chain guard, the end-of-layer report block — existed only
    in a terminal scrollback. Run-scoped layer reports hold the aggregates and
    `logs/transcripts/` holds the structured record, but neither is the thing you
    actually re-read after a bad run, which is the pretty-printed sequence in order.

    Mirrored rather than redirected: watching a live run is how you notice a Blender
    session wedged on one frame, and a run you cannot watch is worse than one you cannot
    re-read. PYTHONUNBUFFERED because a pipe makes the child block-buffer, and a stage
    whose output arrives in 8KB bursts is not watchable.
    """
    log(f"$ {' '.join(args)}")
    if dry:
        return 0
    env = {**os.environ, "VFXH_RUN_ID": RUN_ID, "PYTHONUNBUFFERED": "1",
           "VFXH_STAGE_ARGV": " ".join(args)}
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
    ap.add_argument("--blender", default=None)
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

    layout = run_artifacts.create(
        shot.folder,
        RUN_ID,
        shot_id=shot.id,
        parameters={
            "from_layer": a.start,
            "upto_layer": a.upto,
            "rounds": a.rounds,
            "scale": a.scale,
            "skip_accept": a.skip_accept,
            "skip_render": a.skip_render,
            "dry_run": a.dry_run,
        },
    )

    preflight_raw = preflight_probe(a.blender)
    preflight = environment_result(preflight_raw)
    if not preflight.ok:
        log(preflight_report(preflight_raw))
        _stop(layout, 1, environment_stop(layout, preflight))
    a.blender = str(preflight_raw["blender"]["resolved"])

    atexit.register(_mark_interrupted, layout)
    ledger = Ledger(shot)
    done = [i for i in ids if ledger.status(layers[i].as_milestone()) == "passed"]
    py = sys.executable
    console = layout.logs / "console.log"
    log(f"run {RUN_ID} · shot '{shot.id}' · layers {ids[0]}–{ids[-1]} "
        f"({len(done)} already passed) · rounds {a.rounds}")
    if done:
        log(f"  already passed, will be SKIPPED: {', '.join(done)}", 1)
    if not a.dry_run:
        log(f"  run output → {layout.root.relative_to(shot.folder)} · "
            f"manifest → {layout.manifest.relative_to(shot.folder)} · "
            f"digest: vfx inspect {shot.folder}", 1)

    t0 = time.monotonic()
    for lid in ids:
        if lid in done:
            continue
        log(f"════ LAYER {lid} — {layers[lid].title} ════")
        log(f"──── just-in-time plan · layer {lid} ────")
        rc = _run([py, "-m", "vfx_harness.agents.planner", str(shot.folder),
                   "--layer", lid, "--blender", a.blender], dry=a.dry_run, tee=console)
        if rc:
            log(f"✗ layer {lid} planning exited {rc}; build was not started")
            _stop_after_stage(layout, rc, f"layer-{lid}-planning")
        rc = _run([py, "-m", "vfx_harness.evaluation.cli", "plan", str(shot.folder)],
                  dry=a.dry_run, tee=console)
        if rc:
            log(f"✗ layer {lid} plan did not clear the deterministic gate")
            _stop_after_stage(layout, rc, f"layer-{lid}-plan-gate")
        rc = _run([py, "-m", "vfx_harness.agents.builder", str(shot.folder),
                   "--layer", lid, "--rounds", str(a.rounds), "--blender", a.blender],
                  dry=a.dry_run, tee=console)
        if rc:
            # Stop. Building layer N+1 on a layer N that never passed is the failure this
            # whole chain of guards exists to prevent; carrying on would just bury it.
            log(f"✗ layer {lid} exited {rc}: {_MEANING.get(rc, 'unknown')}")
            log(f"   stopping after {(time.monotonic() - t0) / 60:.0f} min. "
                f"Fix, then resume with --from {lid}")
            _stop_after_stage(layout, rc, f"layer-{lid}-builder")

        # An exit code says the PROCESS completed; the ledger says the WORK was accepted.
        # Conflating them is why this driver announced "✓ layer 2 passed" for a layer whose
        # own report read FAILED and whose four judged frames all scored 2.0: build_agent
        # exits 0 for a layer that builds fine and then fails its verdict — only crashes,
        # truncation and chain breaks raise. The chain guard caught it 0.2s into layer 3,
        # which is the system working, but the driver should not have needed rescuing.
        status = Ledger(shot).status(layers[lid].as_milestone())
        if not _can_advance(status, dry_run=a.dry_run):
            log(f"✗ layer {lid} finished cleanly but its verdict is '{status}' — not "
                f"building on it")
            log(f"   stopping after {(time.monotonic() - t0) / 60:.0f} min. "
                f"See {layout.reports}/layers/layer-{lid}.json, then resume with --from {lid}")
            _stop_after_stage(layout, 9, f"layer-{lid}-ledger-verdict")
        if a.dry_run:
            log(f"↷ layer {lid} would run (ledger remains '{status}')")
            continue
        log(f"✓ layer {lid} passed ({(time.monotonic() - t0) / 60:.0f} min elapsed)")

    if not a.skip_accept:
        log("════ ACCEPTANCE ════")
        rc = _run([py, "-m", "vfx_harness.agents.acceptance", str(shot.folder),
                   "--blender", a.blender], dry=a.dry_run, tee=console)
        if rc:
            log(f"✗ acceptance exited {rc}: {_MEANING.get(rc, 'unknown')}")
            _stop_after_stage(layout, rc, "acceptance")

    if not a.skip_render:
        log("════ RENDER ════")
        rc = _run([py, "-m", "vfx_harness.application.render_shot", str(shot.folder),
                   "--scale", str(a.scale), "--blender", a.blender],
                  dry=a.dry_run, tee=console)
        if rc:
            log(f"✗ render exited {rc}: {_MEANING.get(rc, 'unknown')}")
            _stop_after_stage(layout, rc, "render")

    # Harvest recipes only now — nothing in this run consumes them, and doing it between
    # layers made the run wait on a model writing prose.
    if (layout.logs / "distill_queue.jsonl").is_file():
        log("════ DISTILL (queued during the run) ════")
        _run([py, "-m", "vfx_harness.agents.distill", str(shot.folder)], dry=a.dry_run, tee=console)

    log(f"run {RUN_ID} finished in {(time.monotonic() - t0) / 60:.0f} min")
    terminal_state = "dry-run" if a.dry_run else "passed"
    layout.set_status(terminal_state, exit_code=0)
    _publish_summary(layout)
    layout.write_inventory()
    atexit.unregister(_mark_interrupted)
    log(f"  manifest:          {layout.manifest}")
    log(f"  artifact index:    {layout.inventory}")
    log(f"  per-layer reports: {layout.reports / 'layers'}/*.json")
    log(f"  console log:       {console}")
    log(f"  transcripts:       {layout.logs / 'transcripts'}/*/*.jsonl")
    log(f"  digest:            vfx inspect {shot.folder}")


if __name__ == "__main__":
    main()
