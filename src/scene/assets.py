"""Asset library — the studio's growing, reusable stock, so the desk ASSEMBLES instead of sculpting
cubes from primitives.

The honest ceiling on "build anything" quality isn't the pipeline — it's that every shot is built
from `primitive_cube_add`. Real artists don't sculpt from scratch; they pull from libraries and
generate raw material, then integrate it. This is that sourcing layer:

* :class:`AssetLibrary` — an on-disk, manifest-backed cache of acquired assets, keyed by description
  (+ any reference images), so an asset generated once is reused across shots — it GROWS like the
  LessonBook. Persistent under $HOME (the snap-Blender-safe location).
* :func:`acquire_asset` — generate a real directable mesh for a described object: an AI reference
  image (Higgsfield) → image→3D (Tripo) → a GLB, registered in the library. The desk then imports
  the GLB (``bridge.import_glb``) as its subject instead of hand-building it.

Generation steps are injected (defaults wire the real Higgsfield/Tripo clients) so the orchestration
is unit-tested without the network or credits — the project's "control plane is testable" rule.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from scene.higgsfield import generate_image as _default_generate_image
from scene.tripo import image_to_glb as _default_image_to_glb


def default_library_root() -> Path:
    """The durable asset library under $HOME (persists + grows across runs, like the lessons file)."""
    return Path.home() / ".cache" / "bambi" / "assets"


def asset_image_prompt(description: str) -> str:
    """A clean single-subject reference-image prompt — what image→3D needs (isolated, full view)."""
    return (
        f"{description.strip()} — a SINGLE isolated object, centred, full view, on a plain neutral grey "
        "background, even studio lighting, sharp product-reference photo. No scene, no other objects, "
        "no ground shadow, nothing cropped."
    )


@dataclass(frozen=True)
class Asset:
    key: str
    description: str
    path: str  # the .glb (or .blend) on disk
    kind: str = "mesh"


class AssetLibrary:
    """A manifest-backed cache of acquired assets. Keyed by description(+refs) so gen happens once."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root else default_library_root()
        self.root.mkdir(parents=True, exist_ok=True)
        self._manifest = self.root / "manifest.json"
        self._items: dict[str, Asset] = {}
        if self._manifest.exists():
            try:
                for k, v in json.loads(self._manifest.read_text()).items():
                    self._items[k] = Asset(**v)
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

    @staticmethod
    def key(description: str, refs: Sequence[str] = ()) -> str:
        h = hashlib.sha1(description.strip().lower().encode("utf-8"))
        for r in refs:
            h.update(b"|")
            h.update(str(r).encode("utf-8"))
        return h.hexdigest()[:16]

    def get(self, description: str, refs: Sequence[str] = ()) -> Asset | None:
        """A cached asset for this description — only if its file still exists on disk."""
        asset = self._items.get(self.key(description, refs))
        return asset if (asset and Path(asset.path).exists()) else None

    def add(self, description: str, path: str | Path, kind: str = "mesh", refs: Sequence[str] = ()) -> Asset:
        asset = Asset(key=self.key(description, refs), description=description.strip(), path=str(path), kind=kind)
        self._items[asset.key] = asset
        self._save()
        return asset

    def list(self) -> list[Asset]:
        """Every asset whose file is still present — what the desk can pull in."""
        return [a for a in self._items.values() if Path(a.path).exists()]

    def _save(self) -> None:
        self._manifest.write_text(json.dumps({k: asdict(v) for k, v in self._items.items()}, indent=2))


async def acquire_asset(
    *,
    description: str,
    library: AssetLibrary | None = None,
    refs: Sequence[str] = (),
    generate_image: Callable[..., object] = _default_generate_image,
    image_to_glb: Callable[..., object] = _default_image_to_glb,
) -> Asset:
    """Get a real directable mesh for *description* — from the library if cached, else generate it
    (AI reference image → image→3D GLB) and register it. Reference images condition the generation.

    Blocking gen/convert run in a thread. Raises on generation failure (the caller decides to fall
    back to hand-building)."""
    lib = library or AssetLibrary()
    hit = lib.get(description, refs)
    if hit is not None:
        return hit
    key = AssetLibrary.key(description, refs)
    png = lib.root / f"{key}.png"
    glb = lib.root / f"{key}.glb"
    await asyncio.to_thread(generate_image, asset_image_prompt(description), png, image_references=list(refs) or None)
    await asyncio.to_thread(image_to_glb, png, glb)
    return lib.add(description, glb, kind="mesh", refs=refs)
