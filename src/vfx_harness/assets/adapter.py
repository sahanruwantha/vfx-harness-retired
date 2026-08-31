"""Image→3D backend: Meshy (via Meshy's own API).

Generation is NON-deterministic (seeds, model drift), so it never runs inside a
build script — it runs once for a generate source unit and freezes a candidate
GLB that qualification and promotion turn into replay authority under ``build/``.

Kept behind a thin `get_backend`/protocol seam so a second engine could slot back
in later, but Meshy is the only wired backend. ``to_glb`` posts every supplied
view (1–4); dropping extras onto a single ``image_url`` is a contract violation
(HIR-0162).
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

import httpx

from . import meshy as _meshy


@runtime_checkable
class ImageTo3D(Protocol):
    name: str

    def to_glb(self, images: list[Path], out: Path) -> dict:
        """Turn 1 to 4 isolated views of the same object into a raw GLB at `out`."""
        ...


class MeshyBackend:
    """Meshy multi-image→3D via Meshy's own API (needs MESHY_API_KEY)."""

    name = "meshy"
    poly_param = "target_polycount"

    def __init__(self):
        self.params: dict = {}

    def to_glb(
        self,
        images: list[Path],
        out: Path,
        *,
        client: httpx.Client | None = None,
        api_key: str | None = None,
    ) -> dict:
        pc = int(self.params.get("target_polycount", 250000))
        return _meshy.multi_image_to_3d(
            images,
            out,
            target_polycount=pc,
            should_texture=False,
            enable_pbr=False,
            client=client,
            api_key=api_key,
        )


BACKENDS: dict[str, type] = {"meshy": MeshyBackend}


def get_backend(name: str) -> ImageTo3D:
    try:
        return BACKENDS[name]()
    except KeyError:
        raise ValueError(f"unknown backend {name!r}; known: {', '.join(BACKENDS)}") from None
