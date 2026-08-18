"""Asset acquisition: isolate (Codex gpt-image-2) → image→3D (Meshy) → normalize → cache.

Non-deterministic generation runs once here and freezes `assets/<name>/model.glb`;
the build stage imports that frozen mesh deterministically via the `import_asset`
tool. See `normalize.prepare_asset`, `adapter` (image→3D) and `images` (image gen).
"""

from .adapter import BACKENDS, ImageTo3D, MeshyBackend, get_backend
from .codex_images import generate_image, isolate_regen, remove_background
from .images import get_image_backend
from .meshy import image_to_3d
from .normalize import normalize_glb, prepare_asset

__all__ = [
    "BACKENDS",
    "ImageTo3D",
    "MeshyBackend",
    "generate_image",
    "get_backend",
    "get_image_backend",
    "image_to_3d",
    "isolate_regen",
    "normalize_glb",
    "prepare_asset",
    "remove_background",
]
