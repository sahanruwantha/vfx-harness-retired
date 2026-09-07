"""Closed Higgsfield plate operations for a generate construction route.

Cursor MCP (https://mcp.higgsfield.ai/mcp) is a coding-agent connector. Source-unit
plate prep at runtime uses the authenticated Higgsfield CLI, with a parent crop
handle on every image-edit. Text-only generate is not a Meshy input (ADR-0009).
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageStat

from vfx_harness.assets import higgsfield as _higgsfield
from vfx_harness.observability.console import log

ORBIT_CAMERAS = frozenset({"front", "three_quarter", "left", "right", "back"})
PLATE_PARENT_RULE = (
    "a generate-construction plate names a parent crop or isolate handle; "
    "text-only generate, whole frames, and prose isolate_regen are not legal "
    "Meshy inputs"
)
IDENTITY_EMPTY_RULE = (
    "the derived plate is an empty field relative to its parent crop; retry the "
    "operation once, try isolate_cutout, or abstain"
)

_RELIGHT_PROMPT = (
    "Reproduce THIS EXACT subject from the reference: same silhouette, proportions, "
    "and surface markings. Even studio lighting, plain solid white background, "
    "subject complete and centered in frame. Do not restyle, add ornament, or "
    "change identity."
)

_ORBIT_PROMPT = (
    "The same object as the reference, viewed from the {camera} camera. Preserve "
    "identity, proportions, and markings. Plain solid white background, even studio "
    "lighting, subject complete in frame. Do not restyle or add parts."
)


def _require_parent(parent: str | Path, where: str) -> Path:
    path = Path(parent)
    if not path.is_file():
        raise FileNotFoundError(f"{where} parent plate not found: {path}. " + PLATE_PARENT_RULE)
    return path


def isolate_cutout(parent: str | Path, out: str | Path, **kwargs) -> Path:
    """Background removal of crop pixels. Preserves shot photometry."""
    src = _require_parent(parent, "isolate_cutout")
    log(f"plate isolate_cutout: {src.name}", 2)
    return _higgsfield.remove_background(src, out, **kwargs)


def isolate_relight(parent: str | Path, out: str | Path, **kwargs) -> Path:
    """Same silhouette and markings, even studio light, plain background."""
    src = _require_parent(parent, "isolate_relight")
    log(f"plate isolate_relight: {src.name}", 2)
    return _higgsfield.edit_image([src], _RELIGHT_PROMPT, out, **kwargs)


def orbit_view(parent: str | Path, out: str | Path, *, camera: str, **kwargs) -> Path:
    """Mint one additional declared camera of the same isolated object."""
    if camera not in ORBIT_CAMERAS:
        raise ValueError(
            f"orbit_view camera {camera!r} is unknown; declare one of "
            + ", ".join(sorted(ORBIT_CAMERAS))
        )
    src = _require_parent(parent, "orbit_view")
    log(f"plate orbit_view {camera}: {src.name}", 2)
    return _higgsfield.edit_image(
        [src],
        _ORBIT_PROMPT.format(camera=camera.replace("_", " ")),
        out,
        **kwargs,
    )


def plate_identity_gaps(parent: str | Path, plate: str | Path) -> tuple[str, ...]:
    """Cheap identity card: parent exists, plate is non-empty and not an all-white field."""
    parent_path = Path(parent)
    plate_path = Path(plate)
    gaps: list[str] = []
    if not parent_path.is_file():
        gaps.append(f"parent crop missing: {parent_path}")
        return tuple(gaps)
    if not plate_path.is_file() or plate_path.stat().st_size == 0:
        gaps.append(f"derived plate missing or empty: {plate_path}")
        return tuple(gaps)
    image = Image.open(plate_path).convert("L")
    extrema = image.getextrema()
    if extrema is None:
        gaps.append(f"derived plate unreadable: {plate_path}")
        return tuple(gaps)
    lo, hi = extrema
    mean = ImageStat.Stat(image).mean[0]
    if lo >= 250 and hi >= 250 and mean >= 250:
        gaps.append(
            f"derived plate is an empty white field versus parent {parent_path.name}. "
            + IDENTITY_EMPTY_RULE
        )
    return tuple(gaps)
