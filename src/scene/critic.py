"""Adapt the scene critic to the scheduler seam, loading references from the beat's refs folder.

``make_scene_critic`` produces a ``CritiqueFootage``-shaped leaf (``entry -> [Verdict]``), the same
seam the footage critic uses, so a 3D-only pipeline sets ``critique_footage=make_scene_critic(...)``
and the scheduler's LOCK-pass critique + RE_SOURCE routing work unchanged. It reads the render from
``entry.clip.frames`` and the reference image(s) from ``out_dir/<beat>/refs/`` (the same per-beat
folder the reconstruction builder reads to condition generation). No reference → no verdict (the
match gate only applies when there is a target to match).
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from pathlib import Path

from agents.scene_critic import ReferenceImage, critique_scene
from develop.agents import CritiqueFootage
from develop.ledger import BeatEntry

_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def load_reference_images(refs_dir: str | Path, *, max_images: int = 3) -> list[ReferenceImage]:
    """Load up to *max_images* reference images from *refs_dir* as (media_type, base64) pairs."""
    directory = Path(refs_dir)
    if not directory.is_dir():
        return []
    images: list[ReferenceImage] = []
    for path in sorted(directory.iterdir()):
        media_type = _MEDIA_TYPES.get(path.suffix.lower())
        if not path.is_file() or media_type is None:
            continue
        images.append((media_type, base64.b64encode(path.read_bytes()).decode("ascii")))
        if len(images) >= max_images:
            break
    return images


def make_scene_critic(
    *,
    out_dir: str | Path,
    refs_subdir: str = "refs",
    on_message: Callable[[object], None] | None = None,
) -> CritiqueFootage:
    """A sighted reference-match critic bound to the per-beat refs under *out_dir*."""

    async def critic(entry: BeatEntry):
        clip = entry.clip
        if clip is None or not clip.frames:
            return []  # nothing rendered to judge (an empty render is handled upstream)
        references = load_reference_images(Path(out_dir) / entry.id / refs_subdir)
        if not references:
            return []  # no reference to match against → do not gate this beat
        result = await critique_scene(
            beat_id=entry.id,
            subject=entry.intent.subject or entry.intent.heading,
            reference_images=references,
            render_frames=list(clip.frames),
            on_message=on_message,
        )
        return [result.verdict]

    return critic
