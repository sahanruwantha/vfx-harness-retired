"""Backend seam for the image steps (isolation + concept art).

`codex` (default) drives `gpt-image-2` through the Codex CLI's own ChatGPT
credentials; `higgsfield` drives the authenticated `higgsfield` CLI. Both expose
`generate_image` / `isolate_regen` / `remove_background` with the same call shape,
so `normalize._isolate_views` doesn't care which is wired.

Pick with `ASSET_IMAGE_BACKEND=codex|higgsfield`.
"""

from __future__ import annotations

from types import ModuleType

from vfx_harness.infrastructure.config import Settings

from . import codex_images, higgsfield

DEFAULT_BACKEND = "codex"


def get_image_backend(name: str | None = None) -> ModuleType:
    name = (name or Settings.from_environment().asset_image_backend or DEFAULT_BACKEND).lower()
    if name == "codex":
        return codex_images
    if name == "higgsfield":
        return higgsfield
    raise ValueError(f"unknown image backend {name!r}; known: codex, higgsfield")
