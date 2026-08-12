"""Stage 4 — render the built shot to mp4.

Runs the shot's gate delta scripts (build/NN_*.py) in numeric order in a warm
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


def _chain_scripts(shot: Shot, upto: str | None = None) -> list[Path]:
    """The gate delta scripts to run, in numeric order. `upto` stops after that gate's
    script (e.g. '40' or '40_seam.py') so you can render a partially-built shot."""
    build_dir = shot.folder / "build"
    if not build_dir.is_dir():
        raise FileNotFoundError(f"no build/ in {shot.folder} — run the build stage first")

    def num(p: Path):
        m = re.match(r"(\d+)", p.name)
        return (int(m.group(1)) if m else 10_000, p.name)

    scripts = sorted(build_dir.glob("*.py"), key=num)
    if not scripts:
        raise FileNotFoundError(f"no gate scripts in {build_dir} — run the build stage first")
    if upto:
        keep = [p for p in scripts if p.name.startswith(upto) or p.stem == upto]
        if not keep:
            raise FileNotFoundError(f"no gate script matching {upto!r} in {build_dir}")
        cut = num(keep[-1])
        scripts = [p for p in scripts if num(p) <= cut]
    return scripts


def render_mp4(shot: Shot, upto: str | None = None, *, scale: float = 1.0,
               blender: str = "blender", out: str | Path | None = None) -> Path:
    scripts = _chain_scripts(shot, upto)
    out = Path(out) if out else shot.folder / "renders" / f"{shot.id}_full.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)

    s = BlenderSession(blender=blender, blend_file=None,
                       assets_dir=shot.folder / "assets").start()
    try:
        s.run(_RESET)
        s.run(_preamble(shot))
        for p in scripts:
            log(f"running {p.name}")
            s.run(p.read_text(encoding="utf-8"))
        log(f"rendering {shot.frames} frames @ scale {scale}…")
        t0 = time.monotonic()
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
        description="Render the shot to mp4 — default runs ALL gate scripts in order.")
    ap.add_argument("folder", help="shot folder (contains build/)")
    ap.add_argument("--upto", default=None,
                    help="stop after this gate script (e.g. 40 or 40_seam.py)")
    ap.add_argument("--scale", type=float, default=1.0, help="0..1 render resolution")
    ap.add_argument("--blender", default="blender")
    ap.add_argument("--out", help="output mp4 path (default renders/<shot>_full.mp4)")
    args = ap.parse_args()
    shot = load_shot(args.folder)
    render_mp4(shot, args.upto, scale=args.scale, blender=args.blender, out=args.out)


if __name__ == "__main__":
    main()
