"""Aligned reference/candidate focus panels for builders and critics.

A zoom without context invites cherry-picking; a side-by-side without alignment makes the
eye spend its effort remapping two images.  These helpers always use one normalized,
top-left crop for both images and can produce both a full-frame context map and aligned
detail views (pair, wipe, overlay, difference).
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageEnhance

VIEWS = ("side_by_side", "wipe", "overlay", "difference")
_BG = (18, 18, 22)


def validate_crop(crop) -> tuple[float, float, float, float]:
    if not isinstance(crop, (list, tuple)) or len(crop) != 4:
        raise ValueError("crop must be [x0,y0,x1,y1]")
    try:
        x0, y0, x1, y1 = (float(value) for value in crop)
    except (TypeError, ValueError) as exc:
        raise ValueError("crop coordinates must be numbers") from exc
    if not (0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0):
        raise ValueError("crop must be ordered inside 0..1, origin top-left")
    if x1 - x0 < 0.01 or y1 - y0 < 0.01:
        raise ValueError("crop must span at least 1% of frame width and height")
    return x0, y0, x1, y1


def crop_pixels(image: Image.Image, crop) -> Image.Image:
    x0, y0, x1, y1 = validate_crop(crop)
    width, height = image.size
    box = (round(x0 * width), round(y0 * height),
           max(round(x0 * width) + 1, round(x1 * width)),
           max(round(y0 * height) + 1, round(y1 * height)))
    return image.crop(box)


def _fit_pair(candidate: Image.Image, reference: Image.Image,
              max_height: int = 900, max_pair_width: int = 3000
              ) -> tuple[Image.Image, Image.Image]:
    """Downsample both to one geometry; never upscale either source."""
    aspect = min(candidate.width / max(candidate.height, 1),
                 reference.width / max(reference.height, 1))
    height = min(candidate.height, reference.height, max_height,
                 max(1, int((max_pair_width / 2) / max(aspect, 1e-6))))
    width = max(1, round(height * aspect))
    return (candidate.resize((width, height), Image.Resampling.LANCZOS),
            reference.resize((width, height), Image.Resampling.LANCZOS))


def _label(image: Image.Image, text: str, color: tuple[int, int, int]) -> None:
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, min(image.width, 12 + 7 * len(text)), 25), fill=(0, 0, 0))
    draw.text((6, 6), text, fill=color)


def focus_views(candidate_crop: str | Path, reference_full: str | Path, crop,
                views: list[str] | tuple[str, ...] = ("side_by_side", "wipe")
                ) -> tuple[list[tuple[str, Image.Image]], dict]:
    """Return aligned view images plus provenance/quality metadata."""
    crop = validate_crop(crop)
    selected = list(dict.fromkeys(views))[:4]
    invalid = [view for view in selected if view not in VIEWS]
    if invalid:
        raise ValueError(f"unsupported comparison view(s): {', '.join(invalid)}")
    if not selected:
        raise ValueError("at least one comparison view is required")

    candidate_raw = Image.open(candidate_crop).convert("RGB")
    reference_raw = Image.open(reference_full).convert("RGB")
    reference_crop = crop_pixels(reference_raw, crop)
    candidate_aspect = candidate_raw.width / max(candidate_raw.height, 1)
    reference_aspect = reference_crop.width / max(reference_crop.height, 1)
    if abs(candidate_aspect - reference_aspect) / max(reference_aspect, 1e-6) > 0.02:
        raise ValueError(
            f"aligned focus requires matching aspect; candidate crop is "
            f"{candidate_raw.size}, reference crop is {reference_crop.size}"
        )
    candidate, reference = _fit_pair(candidate_raw, reference_crop)
    width, height = candidate.size
    out: list[tuple[str, Image.Image]] = []
    for view in selected:
        if view == "side_by_side":
            image = Image.new("RGB", (2 * width, height), _BG)
            image.paste(candidate, (0, 0))
            image.paste(reference, (width, 0))
            _label(image, "CANDIDATE", (255, 255, 0))
            draw = ImageDraw.Draw(image)
            draw.rectangle((width, 0, min(2 * width, width + 82), 25), fill=(0, 0, 0))
            draw.text((width + 6, 6), "REFERENCE", fill=(0, 255, 255))
        elif view == "wipe":
            image = candidate.copy()
            seam = width // 2
            image.paste(reference.crop((seam, 0, width, height)), (seam, 0))
            draw = ImageDraw.Draw(image)
            draw.line((seam, 0, seam, height), fill=(255, 80, 255), width=3)
            _label(image, "CANDIDATE | REFERENCE WIPE", (255, 255, 255))
        elif view == "overlay":
            image = Image.blend(candidate, reference, 0.5)
            _label(image, "50/50 OVERLAY", (255, 255, 255))
        else:
            raw = ImageChops.difference(candidate, reference)
            image = ImageEnhance.Contrast(raw).enhance(2.0)
            _label(image, "2x DIFFERENCE", (255, 255, 255))
        out.append((view, image))

    diff = ImageChops.difference(candidate, reference).convert("L")
    pixels = list(diff.getdata())
    meta = {
        "crop": [round(value, 5) for value in crop],
        "candidate_source_px": list(candidate_raw.size),
        "reference_crop_px": list(reference_crop.size),
        "comparison_px": [width, height],
        "views": selected,
        "mean_abs_diff": round(sum(pixels) / max(1, len(pixels)), 3),
        "upscaled": (width > candidate_raw.width or height > candidate_raw.height
                     or width > reference_crop.width or height > reference_crop.height),
    }
    return out, meta


def save_focus_sheet(candidate_crop: str | Path, reference_full: str | Path, crop,
                     dest: str | Path,
                     views: list[str] | tuple[str, ...] = ("side_by_side", "wipe")) -> dict:
    panels, meta = focus_views(candidate_crop, reference_full, crop, views)
    gap = 8
    width = max(image.width for _name, image in panels)
    height = sum(image.height for _name, image in panels) + gap * (len(panels) - 1)
    sheet = Image.new("RGB", (width, height), _BG)
    y = 0
    for _name, image in panels:
        sheet.paste(image, ((width - image.width) // 2, y))
        y += image.height + gap
    destination = Path(dest)
    destination.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(destination, quality=92)
    return {**meta, "image_path": str(destination), "sheet_px": list(sheet.size)}


def save_context_sheet(candidate_full: str | Path, reference_full: str | Path, crop,
                       dest: str | Path) -> dict:
    crop = validate_crop(crop)
    candidate = Image.open(candidate_full).convert("RGB")
    reference = Image.open(reference_full).convert("RGB")
    candidate_aspect = candidate.width / max(candidate.height, 1)
    reference_aspect = reference.width / max(reference.height, 1)
    if abs(candidate_aspect - reference_aspect) / max(reference_aspect, 1e-6) > 0.02:
        raise ValueError(
            f"context comparison requires matching aspect; candidate is {candidate.size}, "
            f"reference is {reference.size}"
        )
    candidate, reference = _fit_pair(candidate, reference, max_height=500)
    width, height = candidate.size
    sheet = Image.new("RGB", (2 * width, height), _BG)
    sheet.paste(candidate, (0, 0))
    sheet.paste(reference, (width, 0))
    draw = ImageDraw.Draw(sheet)
    x0, y0, x1, y1 = crop
    for offset in (0, width):
        box = (offset + round(x0 * width), round(y0 * height),
               offset + round(x1 * width), round(y1 * height))
        draw.rectangle(box, outline=(255, 80, 255), width=4)
    _label(sheet, "CANDIDATE CONTEXT", (255, 255, 0))
    draw.rectangle((width, 0, min(2 * width, width + 126), 25), fill=(0, 0, 0))
    draw.text((width + 6, 6), "REFERENCE CONTEXT", fill=(0, 255, 255))
    destination = Path(dest)
    destination.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(destination, quality=90)
    return {"image_path": str(destination), "crop": list(crop),
            "sheet_px": list(sheet.size)}
