"""Stage 4 — render a built milestone to mp4.

Reconstructs the deterministic `build/<milestone>.py` in a warm session, renders the
shot's frame range in EEVEE, and encodes to mp4 with ffmpeg. Run as a module so the
`pipeline` package imports resolve:

    python -m pipeline.render_shot <shot-folder> [--milestone M1] [--scale 1.0]
"""

from __future__ import annotations

import argparse
import subprocess
import time
from pathlib import Path

from .blender.session import BlenderSession
from .brief import Shot, load_shot
from .build_agent import _RESET, _preamble, _run_priors
from .ledger import load_milestones
from .log import log


def render_mp4(shot: Shot, milestone: str = "M1", *, scale: float = 1.0,
               blender: str = "blender", out: str | Path | None = None) -> Path:
    build = shot.folder / "build" / f"{milestone.lower()}.py"
    if not build.is_file():  # tolerate case slips (build/M1.py)
        alt = [p for p in (shot.folder / "build").glob("*.py")
               if p.stem.lower() == milestone.lower()]
        if not alt:
            raise FileNotFoundError(f"no build script for {milestone} in {shot.folder / 'build'}")
        build = alt[0]
    out = Path(out) if out else shot.folder / "renders" / f"{milestone}.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)

    s = BlenderSession(blender=blender, blend_file=None,
                       assets_dir=shot.folder / "assets").start()
    try:
        s.run(_RESET)
        s.run(_preamble(shot))
        # milestone scripts are DELTAS — run the earlier chain first (m1 → … → this one)
        milestones = load_milestones(shot)
        if milestone.upper() in milestones:
            _run_priors(s, shot, milestones[milestone.upper()])
        s.run(build.read_text(encoding="utf-8"))
        log(f"rendering {shot.frames} frames of {build.name} @ scale {scale}…")
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
    ap = argparse.ArgumentParser(description="Render a built milestone to mp4.")
    ap.add_argument("folder", help="shot folder (contains build/<milestone>.py)")
    ap.add_argument("--milestone", default="M1")
    ap.add_argument("--scale", type=float, default=1.0, help="0..1 render resolution")
    ap.add_argument("--blender", default="blender")
    ap.add_argument("--out", help="output mp4 path (default renders/<M>.mp4)")
    args = ap.parse_args()
    shot = load_shot(args.folder)
    render_mp4(shot, args.milestone, scale=args.scale, blender=args.blender, out=args.out)


if __name__ == "__main__":
    main()
