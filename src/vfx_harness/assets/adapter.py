"""Image→3D backend: Meshy (via Meshy's own API).

Generation is NON-deterministic (seeds, model drift), so it never runs inside a
build script — it runs once in the asset stage and freezes a `raw.glb` that
`normalize` turns into the committed, deterministic `model.glb`.

Kept behind a thin `get_backend`/protocol seam so a second engine could slot back
in later, but Meshy is the only wired backend.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from . import meshy as _meshy


@runtime_checkable
class ImageTo3D(Protocol):
    name: str

    def to_glb(self, images: list[Path], out: Path) -> dict:
        """Turn one isolated view into a raw GLB at `out`."""
        ...


class MeshyBackend:
    """Meshy image→3D via Meshy's own API (needs MESHY_API_KEY). Single view."""

    name = "meshy"
    poly_param = "target_polycount"

    def __init__(self):
        self.params: dict = {}

    def to_glb(self, images: list[Path], out: Path) -> dict:
        pc = int(self.params.get("target_polycount", 250000))
        return _meshy.image_to_3d(images[0], out, target_polycount=pc)


BACKENDS: dict[str, type] = {"meshy": MeshyBackend}


def get_backend(name: str) -> ImageTo3D:
    try:
        return BACKENDS[name]()
    except KeyError:
        raise ValueError(f"unknown backend {name!r}; known: {', '.join(BACKENDS)}") from None
