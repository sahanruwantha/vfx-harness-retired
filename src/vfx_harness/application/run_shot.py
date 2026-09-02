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
    9 ran cleanly but no passing terminal layer publication exists

They are never recovery authority. A failed/interrupted run selects one immutable
typed stop envelope; a missing child envelope fails closed as a harness defect.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from vfx_harness.application.inspect_run import collect
from vfx_harness.application.preflight import environment_result, environment_stop
from vfx_harness.application.preflight import probe as preflight_probe
from vfx_harness.application.preflight import report as preflight_report
from vfx_harness.domain.brief import load_shot
from vfx_harness.domain.run_status import RUN_SUMMARY_SCHEMA
from vfx_harness.domain.stop_envelopes import StopEnvelope
from vfx_harness.observability import run_artifacts
from vfx_harness.observability.log import log
from vfx_harness.observability.runid import RUN_ID
from vfx_harness.orchestration import authority_selection, layer_publication, run_owner_boundary, run_terminalizer
from vfx_harness.orchestration.selected_layer_chain import (
    selected_layer_chain,
)

_MEANING = {
    0: "ok", 1: "crashed", 3: "TRUNCATED — raise the budget or split the layer",
    4: "CHAIN BROKEN — a prior layer's script no longer composes",
    5: "unanswered plan questions — settle them first",
    6: "UNACCEPTED PRIOR — a lower layer must pass first",
    7: "INCOMPLETE CHAIN", 8: "plan is STALE against brief.md — re-plan",
    9: "layer ran cleanly but has no passing terminal publication",
}


def _can_advance(status: str, *, dry_run: bool) -> bool:
    """Whether the driver may continue after a layer command.

    A dry run deliberately executes no builder, so it cannot change a pending ledger
    verdict into ``passed``.  Treating that unchanged verdict like a real build failure
    made ``--dry-run`` stop after its first unbuilt layer instead of previewing the run.
    """
    return dry_run or status == "passed"


def _receipt_backed_passed_layers(shot, layers, selected_authority) -> set[str]:
    """Return only layers with one complete current public finalization."""

    completed: set[str] = set()
    for layer_id, layer in layers.items():
        try:
            layer_publication.require_current_layer_publication(
                shot.folder,
                layer,
                selected_authority,
            )
        except layer_publication.LayerPublicationConflict:
            continue
        completed.add(str(layer_id))
    return completed


def _selected_run_layers(shot):
    """Resolve one authority snapshot and retain its selected-DAG order."""

    selected_authority = authority_selection.resolve_selected_authority(shot.folder)
    chain = selected_layer_chain(shot, selected_authority=selected_authority)
    return (
        selected_authority,
        chain,
        {str(layer.id): layer for layer in chain},
    )


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _publish_summary(layout: run_artifacts.RunLayout, *, state: str, exit_code: int) -> dict:
    """Publish the run summary the terminal status selects; the digest binds these bytes."""

    try:
        collected = collect(layout.shot, run_id=layout.run_id)
    except Exception as exc:
        log(f"! run summary could not be collected: {str(exc)[:160]}", 1)
        collected = {"collection_error": str(exc)[:1000]}
    summary = {
        **collected,
        "schema": RUN_SUMMARY_SCHEMA,
        "run_id": layout.run_id,
        "command": "run",
        "state": state,
        "exit_code": exit_code,
    }
    layout.write_summary(summary)
    return summary


def _stop(
    layout: run_artifacts.RunLayout,
    lease: run_owner_boundary.RunOwnerFenceLease,
    code: int,
    envelope: StopEnvelope,
) -> None:
    layout.write_stop_envelope(envelope)
    detail = f"{envelope.stop_class}: {envelope.found} {envelope.next_action}"
    summary = _publish_summary(layout, state="failed", exit_code=code)
    summary.update(
        {
            "detail": detail[:1000],
            "terminal_cause": envelope.stop_class,
            "stop_envelope": "reports/stop-envelope.json",
            "stop_envelope_digest": envelope.digest,
            "stop_class": envelope.stop_class,
            "stop_stage": envelope.stage,
            "cause_fingerprint": envelope.cause_fingerprint,
        }
    )
    layout.write_summary(summary)
    run_terminalizer.publish_failed_status(
        layout.root,
        lease=lease,
        envelope=envelope,
        exit_code=code,
        updated_at=_now(),
        detail=detail[:1000],
    )
    layout.write_inventory()
    raise SystemExit(code)


def _stop_after_stage(
    layout: run_artifacts.RunLayout,
    lease: run_owner_boundary.RunOwnerFenceLease,
    code: int,
    boundary: str,
) -> None:
    """Consume only the child-prepared envelope; never dispatch from its exit code."""
    try:
        envelope = layout.read_prepared_stop()
    except ValueError:
        envelope = run_artifacts.missing_boundary_stop(
            layout,
            boundary,
            exit_code=code,
        )
    _stop(layout, lease, code, envelope)


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

    layout = run_artifacts.create(
        shot.folder,
        RUN_ID,
        command="run",
        dispatch_kind="driver",
        shot_id=shot.id,
        arguments=[a.folder],
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

    with (
        run_owner_boundary.owned_root_run(layout, command="run", owner_kind="driver") as lease,
        run_owner_boundary.signal_intent_scope(),
    ):
        try:
            _drive(a, shot, layout, lease)
        except run_owner_boundary.RunCancellation as cancellation:
            raise run_owner_boundary.terminalize_cancellation(
                shot.folder,
                layout,
                lease,
                cancellation,
            ) from None


def _drive(
    a: argparse.Namespace,
    shot,
    layout: run_artifacts.RunLayout,
    lease: run_owner_boundary.RunOwnerFenceLease,
) -> None:
    preflight_raw = preflight_probe(a.blender)
    preflight = environment_result(preflight_raw)
    if not preflight.ok:
        log(preflight_report(preflight_raw))
        _stop(layout, lease, 1, environment_stop(layout, preflight))
    a.blender = str(preflight_raw["blender"]["resolved"])

    selected_authority, chain, layers = _selected_run_layers(shot)
    ids = [
        str(layer.id)
        for layer in chain
        if a.start <= int(layer.id) <= (a.upto or 10**6)
    ]
    if not ids:
        raise SystemExit(
            f"no layers in range {a.start}..{a.upto} "
            f"(have: {', '.join(layers)})"
        )

    verified_passed = _receipt_backed_passed_layers(
        shot,
        layers,
        selected_authority,
    )
    done = [i for i in ids if i in verified_passed]
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
        # The initial summary is not skip authority.  Reverify the receipt and
        # its causal source closure at the actual dispatch boundary.
        if lid in done:
            dispatch_authority, _dispatch_chain, dispatch_layers = (
                _selected_run_layers(shot)
            )
            dispatch_layer = dispatch_layers.get(lid)
            if dispatch_layer is not None and lid in _receipt_backed_passed_layers(
                shot,
                {lid: dispatch_layer},
                dispatch_authority,
            ):
                continue
        log(f"════ LAYER {lid} — {layers[lid].title} ════")
        log(f"──── just-in-time plan · layer {lid} ────")
        rc = _run([py, "-m", "vfx_harness.agents.planner", str(shot.folder),
                   "--layer", lid, "--blender", a.blender], dry=a.dry_run, tee=console)
        if rc:
            log(f"✗ layer {lid} planning exited {rc}; build was not started")
            _stop_after_stage(layout, lease, rc, f"layer-{lid}-planning")
        rc = _run([py, "-m", "vfx_harness.evaluation.cli", "plan", str(shot.folder)],
                  dry=a.dry_run, tee=console)
        if rc:
            log(f"✗ layer {lid} plan did not clear the deterministic gate")
            _stop_after_stage(layout, lease, rc, f"layer-{lid}-plan-gate")
        rc = _run([py, "-m", "vfx_harness.agents.builder", str(shot.folder),
                   "--layer", lid, "--rounds", str(a.rounds), "--blender", a.blender],
                  dry=a.dry_run, tee=console)
        if rc:
            # Stop. Building layer N+1 on a layer N that never passed is the failure this
            # whole chain of guards exists to prevent; carrying on would just bury it.
            log(f"✗ layer {lid} exited {rc}: {_MEANING.get(rc, 'unknown')}")
            log(f"   stopping after {(time.monotonic() - t0) / 60:.0f} min. "
                f"Fix, then resume with --from {lid}")
            _stop_after_stage(layout, lease, rc, f"layer-{lid}-builder")

        if a.dry_run:
            status = "pending"
        else:
            # The child process finishing is not publication authority. Re-resolve the
            # selected view it may have materialized, then require the terminal receipt,
            # sealed outcome, ledger projection, and exact composed source to agree.
            completed_authority, _completed_chain, completed_layers = (
                _selected_run_layers(shot)
            )
            completed_layer = completed_layers.get(lid)
            publication_error = None
            if completed_layer is None:
                publication_error = (
                    f"selected layer DAG no longer contains completed layer {lid}"
                )
            else:
                try:
                    publication = layer_publication.require_current_layer_publication(
                        shot.folder,
                        completed_layer,
                        completed_authority,
                    )
                except layer_publication.LayerPublicationConflict as exc:
                    publication_error = str(exc)
            if publication_error is not None:
                log(
                    f"✗ layer {lid} process finished but no current terminal "
                    f"publication exists: {publication_error}"
                )
                log(
                    f"   stopping after {(time.monotonic() - t0) / 60:.0f} min. "
                    f"See {layout.reports}/layers/layer-{lid}.json, then resume with "
                    f"--from {lid}"
                )
                _stop_after_stage(layout, lease, 9, f"layer-{lid}-finalization-publication")
            status = publication.ledger_status
        if not _can_advance(status, dry_run=a.dry_run):
            log(f"✗ layer {lid} finished cleanly but its verdict is '{status}' — not "
                f"building on it")
            log(f"   stopping after {(time.monotonic() - t0) / 60:.0f} min. "
                f"See {layout.reports}/layers/layer-{lid}.json, then resume with --from {lid}")
            _stop_after_stage(layout, lease, 9, f"layer-{lid}-ledger-verdict")
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
            _stop_after_stage(layout, lease, rc, "acceptance")

    if not a.skip_render:
        log("════ RENDER ════")
        rc = _run([py, "-m", "vfx_harness.application.render_shot", str(shot.folder),
                   "--scale", str(a.scale), "--blender", a.blender],
                  dry=a.dry_run, tee=console)
        if rc:
            log(f"✗ render exited {rc}: {_MEANING.get(rc, 'unknown')}")
            _stop_after_stage(layout, lease, rc, "render")

    # Legacy queue rows are deliberately inert: they do not bind an immutable unit
    # completion receipt, canonical script digest, or selected authority.  Never invoke
    # the retired consumer from a successful run.
    if (layout.logs / "distill_queue.jsonl").is_file():
        log(
            "↷ ignoring unsupported legacy distillation queue; recipe publication "
            "requires a receipt-bound staged diff"
        )

    log(f"run {RUN_ID} finished in {(time.monotonic() - t0) / 60:.0f} min")
    terminal_state = "dry-run" if a.dry_run else "passed"
    summary = _publish_summary(layout, state=terminal_state, exit_code=0)
    run_terminalizer.publish_passed_status(
        layout.root,
        lease=lease,
        summary=summary,
        updated_at=_now(),
        state=terminal_state,
    )
    layout.write_inventory()
    log(f"  manifest:          {layout.manifest}")
    log(f"  artifact index:    {layout.inventory}")
    log(f"  per-layer reports: {layout.reports / 'layers'}/*.json")
    log(f"  console log:       {console}")
    log(f"  transcripts:       {layout.logs / 'transcripts'}/*/*.jsonl")
    log(f"  digest:            vfx inspect {shot.folder}")


if __name__ == "__main__":
    main()
