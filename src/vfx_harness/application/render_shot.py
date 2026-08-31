"""Stage 4 — render the built shot to mp4.

Runs the selected layer delta scripts in the global DAG's stable topological order in
a warm session, renders the frame range in EEVEE, and encodes to mp4 with ffmpeg. Run as
a module so the `vfx_harness` package imports resolve:

    python -m vfx_harness.application.render_shot <shot-folder> [--upto 40] [--scale 1.0]
"""

from __future__ import annotations

import argparse
import re
import subprocess
import time
from pathlib import Path

from vfx_harness.agents.acceptance_stop import require_current_accepted_outcome
from vfx_harness.agents.builder import _RESET, _preamble, _run_artifact_script
from vfx_harness.blender.session import BlenderSession
from vfx_harness.domain.brief import Shot, load_shot
from vfx_harness.observability import run_artifacts
from vfx_harness.observability.log import log
from vfx_harness.orchestration.ledger import Ledger
from vfx_harness.orchestration.selected_layer_chain import selected_layer_chain


class IncompleteRender(RuntimeError):
    """The deliverable was asked for before the chain that produces it is accepted."""


def _render_output_path(
    shot: Shot,
    *,
    upto: str | None,
    force: bool,
    out: str | Path | None,
) -> Path:
    if out is not None:
        return Path(out)
    if force or upto is not None:
        label = (
            "forced"
            if force and upto is None
            else "upto-" + re.sub(r"[^A-Za-z0-9._-]+", "-", str(upto)).strip("-")
        )
        return (
            run_artifacts.ensure(shot.folder).scratch
            / "previews"
            / f"{shot.id}_{label}.mp4"
        )
    return run_artifacts.deliverables_dir(shot.folder) / f"{shot.id}_full.mp4"


def _chain_scripts(
    shot: Shot,
    upto: str | None = None,
    *,
    force: bool = False,
    expected_bundle_digest: str | None = None,
) -> list[Path]:
    """The layer delta scripts to run, in order, taken from the LEDGER's manifest.

    This used to glob build/ and run whatever it found. A glob answers "what files are
    here", but the deliverable is defined by "what did the pipeline accept" — so a stray
    experiment or a half-written 09_*.py silently entered the mp4, and a real layer that
    was misnamed silently did not. The ledger is the record; the directory is a cache.
    """

    build_dir = shot.folder / "build"
    if not build_dir.is_dir():
        raise FileNotFoundError(f"no build/ in {shot.folder} — run the build stage first")

    layers = list(
        selected_layer_chain(
            shot,
            expected_bundle_digest=expected_bundle_digest,
        )
    )
    if upto:
        matching = [
            index
            for index, layer in enumerate(layers)
            if Path(layer.script).name.startswith(upto)
            or Path(layer.script).stem == upto
            or str(layer.id) == upto
        ]
        if not matching:
            raise FileNotFoundError(f"no layer matching {upto!r} in the plan")
        layers = layers[: matching[-1] + 1]

    ledger = Ledger(shot)
    scripts, problems = [], []
    for g in layers:
        p = shot.folder / g.script
        if not p.is_file():
            problems.append(f"layer {g.id}: {g.script} missing")
            continue
        st = ledger.status(g.as_milestone())
        if st != "passed":
            problems.append(f"layer {g.id} ({g.script}) is '{st}', not passed")
        scripts.append(p)
    if problems and not force:
        raise IncompleteRender(
            "refusing to render an unaccepted chain — " + "; ".join(problems)
            + ". Finish those layers, or pass --force for a preview render.")
    if problems:
        log(f"! --force: rendering an unaccepted chain — {'; '.join(problems)}")
    if not scripts:
        raise FileNotFoundError(f"no layer scripts in {build_dir} — run the build stage first")

    # Say what is being LEFT OUT. Silent truncation reads as "we rendered everything".
    named = {p.name for p in scripts}
    strays = sorted(p.name for p in build_dir.glob("[0-9]*.py") if p.name not in named)
    if strays:
        log(f"! ignoring {len(strays)} script(s) in build/ that the plan does not list: "
            + ", ".join(strays))
    return scripts


def render_mp4(shot: Shot, upto: str | None = None, *, scale: float = 1.0,
               blender: str = "blender", out: str | Path | None = None,
               force: bool = False) -> Path:
    acceptance_outcome = None
    if upto is None and not force:
        try:
            acceptance_outcome = require_current_accepted_outcome(shot)
        except ValueError as exc:
            raise IncompleteRender(str(exc)) from exc
    elif force:
        log("! --force: final acceptance is not being used as publication authority")
    else:
        log("! --upto: partial-chain output is a preview, not a deliverable")
    scripts = _chain_scripts(
        shot,
        upto,
        force=force,
        expected_bundle_digest=(
            acceptance_outcome.bundle_digest
            if acceptance_outcome is not None
            else None
        ),
    )
    out = _render_output_path(shot, upto=upto, force=force, out=out)
    out.parent.mkdir(parents=True, exist_ok=True)

    s = BlenderSession(blender=blender, blend_file=None,
                       assets_dir=shot.folder / "assets", cwd=shot.folder).start()
    try:
        s.run(_RESET)
        s.run(_preamble(shot))
        for p in scripts:
            log(f"running {p.name}")
            _run_artifact_script(s, p)
        log(f"rendering {shot.frames} frames @ scale {scale}…")
        t0 = time.monotonic()
        for f in range(1, shot.frames + 1):
            s.render(frame=f, mode="eevee", scale=scale)
            if f % 8 == 0:
                log(f"  {f}/{shot.frames} ({time.monotonic() - t0:.0f}s)")
        seq = str(s.artifacts / "eevee_f%04d.png")
        subprocess.run(["ffmpeg", "-y", "-framerate", str(shot.fps), "-i", seq,
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18", str(out)],
                       check=True, capture_output=True, text=True)
    finally:
        s.close()
    log(f"wrote {out} ({out.stat().st_size // 1024} KB)")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Render the shot to mp4 — default runs ALL layer scripts in order.")
    ap.add_argument("folder", help="shot folder (contains build/)")
    ap.add_argument("--upto", default=None,
                    help="stop after this layer script (e.g. 40 or 40_seam.py)")
    ap.add_argument("--scale", type=float, default=1.0, help="0..1 render resolution")
    ap.add_argument("--blender", default="blender")
    ap.add_argument("--out", help="output mp4 path (default renders/<shot>_full.mp4)")
    ap.add_argument("--force", action="store_true",
                    help="render even if layers are missing or unaccepted (preview only)")
    args = ap.parse_args()
    shot = load_shot(args.folder)
    with run_artifacts.invocation(shot.folder, "render", shot_id=shot.id,
                                  parameters={"upto": args.upto, "scale": args.scale}):
        try:
            render_mp4(shot, args.upto, scale=args.scale, blender=args.blender,
                       out=args.out, force=args.force)
        except IncompleteRender as e:
            log(f"INCOMPLETE CHAIN — {e}")
            raise run_artifacts.RequestedExit(7, f"INCOMPLETE CHAIN — {e}") from None


if __name__ == "__main__":
    main()
