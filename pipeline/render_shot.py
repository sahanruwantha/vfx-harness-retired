"""Stage 4 — render the built shot to mp4.

Runs the shot's layer delta scripts (build/NN_*.py) in numeric order in a warm
session, renders the frame range in EEVEE, and encodes to mp4 with ffmpeg. Run as a module so the
`pipeline` package imports resolve:

    python -m pipeline.render_shot <shot-folder> [--upto 40] [--scale 1.0]
"""

from __future__ import annotations

import argparse
import re
import subprocess
import time
from pathlib import Path

from .blender.session import BlenderSession
from .brief import Shot, load_shot
from .build_agent import _RESET, _preamble
from .log import log


class IncompleteRender(RuntimeError):
    """The deliverable was asked for before the chain that produces it is accepted."""


def _chain_scripts(shot: Shot, upto: str | None = None, *,
                   force: bool = False) -> list[Path]:
    """The layer delta scripts to run, in order, taken from the LEDGER's manifest.

    This used to glob build/ and run whatever it found. A glob answers "what files are
    here", but the deliverable is defined by "what did the pipeline accept" — so a stray
    experiment or a half-written 09_*.py silently entered the mp4, and a real layer that
    was misnamed silently did not. The ledger is the record; the directory is a cache.
    """
    from .ledger import Ledger, load_layers

    build_dir = shot.folder / "build"
    if not build_dir.is_dir():
        raise FileNotFoundError(f"no build/ in {shot.folder} — run the build stage first")

    def num(p: Path):
        m = re.match(r"(\d+)", p.name)
        return (int(m.group(1)) if m else 10_000, p.name)

    layers = sorted(load_layers(shot).values(), key=lambda g: num(Path(g.script)))
    if upto:
        keep = [g for g in layers
                if Path(g.script).name.startswith(upto) or Path(g.script).stem == upto
                or str(g.id) == upto]
        if not keep:
            raise FileNotFoundError(f"no layer matching {upto!r} in the plan")
        cut = num(Path(keep[-1].script))
        layers = [g for g in layers if num(Path(g.script)) <= cut]

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
    scripts = _chain_scripts(shot, upto, force=force)
    out = Path(out) if out else shot.folder / "renders" / f"{shot.id}_full.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)

    s = BlenderSession(blender=blender, blend_file=None,
                       assets_dir=shot.folder / "assets", cwd=shot.folder).start()
    try:
        s.run(_RESET)
        s.run(_preamble(shot))
        for p in scripts:
            log(f"running {p.name}")
            s.run(p.read_text(encoding="utf-8"))
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
    try:
        render_mp4(shot, args.upto, scale=args.scale, blender=args.blender,
                   out=args.out, force=args.force)
    except IncompleteRender as e:
        log(f"INCOMPLETE CHAIN — {e}")
        raise SystemExit(7)


if __name__ == "__main__":
    main()
