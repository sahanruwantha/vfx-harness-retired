"""Stage 4 — render a built milestone to mp4.

Reconstructs the deterministic `build/<milestone>.py` in a warm session, renders the
shot's frame range in EEVEE, and encodes to mp4 with ffmpeg. Run as a module so the
`pipeline` package imports resolve:

    python -m pipeline.render_shot <shot-folder> [--milestone M1] [--scale 1.0]
"""

from __future__ import annotations

import argparse
import re
import subprocess
import time
from pathlib import Path

from .blender.session import BlenderSession
from .brief import Shot, load_shot
from .build_agent import _RESET, _preamble, _run_priors
from .ledger import load_milestones
from .log import log


def _chain_scripts(shot: Shot, milestone: str | None) -> list[Path]:
    """The delta scripts to run, in order. Default (no milestone): ALL gate scripts in
    numeric order — the full shot. Legacy milestone mode: m1..m<N> chain."""
    build_dir = shot.folder / "build"
    if not build_dir.is_dir():
        raise FileNotFoundError(f"no build/ in {shot.folder} — run the build stage first")
    if milestone is None:
        def num(p: Path):
            m = re.match(r"(\d+)", p.name)
            return (int(m.group(1)) if m else 10_000, p.name)
        scripts = sorted(build_dir.glob("*.py"), key=num)
        if not scripts:
            raise FileNotFoundError(f"no build scripts in {build_dir}")
        return scripts
    build = build_dir / f"{milestone.lower()}.py"
    if not build.is_file():
        alt = [p for p in build_dir.glob("*.py") if p.stem.lower() == milestone.lower()]
        if not alt:
            raise FileNotFoundError(f"no build script for {milestone} in {build_dir}")
        build = alt[0]
    milestones = load_milestones(shot)
    priors = []
    if milestone.upper() in milestones:
        from .build_agent import _prior_scripts
        priors = _prior_scripts(shot, milestones[milestone.upper()])
    return priors + [build]


def render_mp4(shot: Shot, milestone: str | None = None, *, scale: float = 1.0,
               blender: str = "blender", out: str | Path | None = None) -> Path:
    scripts = _chain_scripts(shot, milestone)
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
    ap.add_argument("--milestone", default=None,
                    help="legacy: render up to a milestone script chain (m1..mN)")
    ap.add_argument("--scale", type=float, default=1.0, help="0..1 render resolution")
    ap.add_argument("--blender", default="blender")
    ap.add_argument("--out", help="output mp4 path (default renders/<shot>_full.mp4)")
    args = ap.parse_args()
    shot = load_shot(args.folder)
    render_mp4(shot, args.milestone, scale=args.scale, blender=args.blender, out=args.out)


if __name__ == "__main__":
    main()
