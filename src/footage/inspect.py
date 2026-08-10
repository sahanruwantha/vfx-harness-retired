"""Sample keyframes from a downloaded clip so the agent can WATCH it.

This is the capability that turns footage from a blind fetch into a feedback loop: the
agent doesn't judge a clip by its title, it looks at real frames and decides whether the
footage shows the thing, is faceless-compatible, and beats a motion-graphic for that beat.

`sample_frames` uses ffmpeg to pull evenly-spaced JPEG keyframes (base64, ready for image
content blocks). The command construction is pure and the subprocess runner is injectable,
so the timing logic is testable without ffmpeg or a real video.
"""

from __future__ import annotations

import base64
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

Runner = Callable[[list[str]], subprocess.CompletedProcess]

EXTRACT_TIMEOUT = 120
DEFAULT_COUNT = 6
MAX_COUNT = 12
DEFAULT_WIDTH = 640
# Fallback probe points (seconds) when a clip's duration can't be read.
_FALLBACK_TIMES = [0.5, 1.5, 3.0, 5.0, 8.0, 12.0, 18.0, 25.0, 35.0, 45.0, 60.0, 90.0]


class InspectError(RuntimeError):
    """Frame sampling failed (bad path, ffmpeg error, or no frames produced)."""


@dataclass(frozen=True)
class FrameSample:
    timecode: str  # mm:ss
    seconds: float
    jpeg_b64: str  # base64-encoded JPEG, for an image content block


def _default_runner(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 - argv list, no shell, fixed executables
        cmd, capture_output=True, text=True, timeout=EXTRACT_TIMEOUT, check=False
    )


def _fmt_tc(seconds: float) -> str:
    total = int(round(seconds))
    return f"{total // 60:02d}:{total % 60:02d}"


def frame_timestamps(duration: float | None, count: int) -> list[float]:
    """Evenly-spaced sample points across the clip (mid-bucket, avoiding the exact ends)."""
    count = max(1, min(count, MAX_COUNT))
    if not duration or duration <= 0:
        return _FALLBACK_TIMES[:count]
    return [round((i + 0.5) / count * duration, 3) for i in range(count)]


def probe_duration(clip_path: str | Path, *, ffprobe: str = "ffprobe", runner: Runner | None = None) -> float | None:
    run = runner or _default_runner
    result = run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(clip_path),
        ]
    )
    try:
        return float((result.stdout or "").strip())
    except ValueError:
        return None


def build_extract_command(
    clip_path: str | Path,
    seconds: float,
    out_path: str | Path,
    *,
    ffmpeg: str = "ffmpeg",
    width: int = DEFAULT_WIDTH,
) -> list[str]:
    """One-frame grab at `seconds`. Input-side `-ss` for a fast seek."""
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        str(seconds),
        "-i",
        str(clip_path),
        "-frames:v",
        "1",
        "-q:v",
        "3",
        "-vf",
        f"scale={width}:-1:force_original_aspect_ratio=decrease",
        "-y",
        str(out_path),
    ]


def sample_frames(
    clip_path: str | Path,
    *,
    count: int = DEFAULT_COUNT,
    width: int = DEFAULT_WIDTH,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
    runner: Runner | None = None,
) -> list[FrameSample]:
    """Extract evenly-spaced keyframes from `clip_path` as base64 JPEGs."""
    run = runner or _default_runner
    path = Path(clip_path)
    if not str(path).strip():
        raise InspectError("clip_path is required")
    duration = probe_duration(path, ffprobe=ffprobe, runner=run)
    timestamps = frame_timestamps(duration, count)

    samples: list[FrameSample] = []
    with tempfile.TemporaryDirectory(prefix="bambi-frames-") as tmp:
        for i, seconds in enumerate(timestamps):
            out = Path(tmp) / f"frame-{i:02d}.jpg"
            result = run(build_extract_command(path, seconds, out, ffmpeg=ffmpeg, width=width))
            if result.returncode != 0 or not out.exists():
                continue  # a single unreadable seek shouldn't sink the whole inspection
            data = out.read_bytes()
            if not data:
                continue
            samples.append(
                FrameSample(
                    timecode=_fmt_tc(seconds),
                    seconds=seconds,
                    jpeg_b64=base64.b64encode(data).decode("ascii"),
                )
            )
    if not samples:
        raise InspectError(f"no frames could be extracted from {path}")
    return samples
