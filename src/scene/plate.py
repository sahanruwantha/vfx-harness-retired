"""Higgsfield plate realizer — the atmospheric / motion-graphics analog of ``render_3d``.

The 3D reconstruction agent excels at discrete objects but plateaus (~0.57) on atmospheric
ENVIRONMENT shots — volumetric storm skies, heavy bloom, a sea of city lights. Four independent
approaches agreed that look is unreachable from procedural geometry: it is an AI-*plate* look, not
a reconstruction look (the Silk Road finding, ``docs/3d-agent-architecture.md``). This realizes
such beats as a generated plate: Higgsfield makes a cinematic frame from the beat's intent, which
becomes the beat's ``Clip`` — judged by the same sighted critic, with no Blender at all.

It is the MOTION_GRAPHICS twin of :func:`contracts.lock.make_footage_acquirer` and
:func:`scene.realizer.make_scene_builder`: same seam, same ``Clip`` output. ``generate_image`` is
injected so the leaf is testable without the CLI. Plates are cached per beat, and the PNG Higgsfield
returns is transcoded to JPEG (metadata stripped) so it drops cleanly into the sighted critic's
image blocks.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import subprocess
from collections.abc import Sequence
from pathlib import Path

from contracts.agents import GeneratePlate
from contracts.ledger import BeatEntry, Clip
from contracts.ledger import FrameSample
from scene.higgsfield import generate_image as _default_generate_image

DEFAULT_STYLE = (
    "cinematic wide establishing shot, dramatic atmospheric lighting, volumetric haze and glow, "
    "moody cinematic colour grade, photoreal film still, highly detailed, no text, no watermark, no people"
)


def plate_prompt(subject: str, *, style: str = DEFAULT_STYLE) -> str:
    """A cinematic plate prompt anchored on the beat's subject."""
    core = subject.strip().rstrip(".")
    return f"{core}. {style}"


def _cache_key(prompt: str, references: Sequence[str]) -> str:
    digest = hashlib.sha1(prompt.encode("utf-8"))  # noqa: S324 - cache key, not security
    for ref in sorted(references):
        digest.update(b"|")
        digest.update(ref.encode("utf-8"))
    return digest.hexdigest()[:16]


def _to_jpeg(src: Path, dst: Path) -> Path:
    """Transcode to JPEG, stripping metadata (Higgsfield PNGs carry chunks that trip some decoders)
    so the frame is a clean JPEG the critic's image blocks accept."""
    subprocess.run(  # noqa: S603 - fixed executable, argv list, no shell
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-map_metadata", "-1", "-q:v", "3", str(dst)],
        check=True,
        timeout=120,
    )
    return dst


def make_plate_generator(
    *,
    out_dir: str | Path,
    generate_image=_default_generate_image,
    aspect_ratio: str = "16:9",
    quality: str | None = None,
    style: str = DEFAULT_STYLE,
    refs_subdir: str = "refs",
) -> GeneratePlate:
    """A ``generate_plate`` leaf: turn a MOTION_GRAPHICS beat into an AI image plate ``Clip``.

    Generation failure → a Clip carrying only the acquisition gap (no frames), read by the critic as
    RE_SOURCE. Reference images in ``<beat>/refs/`` condition the generation (image-to-image)."""

    def _generate_sync(entry: BeatEntry) -> Clip:
        beat_dir = Path(out_dir) / entry.id
        beat_dir.mkdir(parents=True, exist_ok=True)
        subject = entry.intent.subject or entry.intent.heading
        refs_dir = beat_dir / refs_subdir
        references = [str(p) for p in sorted(refs_dir.iterdir()) if p.is_file()] if refs_dir.is_dir() else []
        prompt = plate_prompt(subject, style=style)
        key = _cache_key(prompt, references)

        png = beat_dir / f"{key}.png"
        try:
            if not png.exists():
                generate_image(
                    prompt, png, image_references=references or None, aspect_ratio=aspect_ratio, quality=quality
                )
        except Exception as exc:  # generation failure is an acquisition gap, not a crash
            return Clip(licence="KNOWN", acquisition_gap=f"plate generation failed for {subject!r}: {exc}")

        jpg = beat_dir / f"{key}.jpg"
        try:
            _to_jpeg(png, jpg)
        except (subprocess.SubprocessError, OSError) as exc:
            return Clip(licence="KNOWN", acquisition_gap=f"plate transcode failed for {subject!r}: {exc}")

        frames = (FrameSample(timecode="00:00", seconds=0.0, jpeg_b64=base64.b64encode(jpg.read_bytes()).decode("ascii")),)
        return Clip(
            fetched_path=jpg,
            licence="KNOWN",  # a self-generated plate is clearable by construction
            frames=frames,
            render_meta={"kind": "plate", "prompt": prompt, "subject": subject},
        )

    async def generate(entry: BeatEntry) -> Clip:
        return await asyncio.to_thread(_generate_sync, entry)

    return generate
