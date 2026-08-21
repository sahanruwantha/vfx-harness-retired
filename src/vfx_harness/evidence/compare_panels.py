"""Aligned reference/candidate focus panels for builders and critics.

A zoom without context invites cherry-picking; a side-by-side without alignment makes the
eye spend its effort remapping two images.  These helpers always use one normalized,
top-left crop for both images and can produce both a full-frame context map and aligned
detail views (pair, wipe, overlay, difference).
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageStat

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
    box = (
        round(x0 * width),
        round(y0 * height),
        max(round(x0 * width) + 1, round(x1 * width)),
        max(round(y0 * height) + 1, round(y1 * height)),
    )
    return image.crop(box)


def focus_signal(image: Image.Image) -> dict:
    """Conservative content test used to reject genuinely empty optical crops."""
    gray = image.convert("L")
    if max(gray.size) > 320:
        scale = 320 / max(gray.size)
        gray = gray.resize(
            (max(1, round(gray.width * scale)), max(1, round(gray.height * scale))),
            Image.Resampling.LANCZOS,
        )
    stats = ImageStat.Stat(gray)
    lo, hi = gray.getextrema()
    edges = gray.filter(ImageFilter.FIND_EDGES)
    if edges.width > 4 and edges.height > 4:
        edges = edges.crop((2, 2, edges.width - 2, edges.height - 2))
    edge_mean = ImageStat.Stat(edges).mean[0]
    stddev = stats.stddev[0]
    dynamic_range = hi - lo
    return {
        "stddev": round(stddev, 3),
        "edge_mean": round(edge_mean, 3),
        "dynamic_range": int(dynamic_range),
        "has_signal": bool(stddev >= 2.0 or edge_mean >= 1.0 or dynamic_range >= 8),
    }


def _fit_pair(
    candidate: Image.Image, reference: Image.Image, max_height: int = 900, max_pair_width: int = 3000
) -> tuple[Image.Image, Image.Image]:
    """Downsample both to one geometry; never upscale either source."""
    aspect = min(candidate.width / max(candidate.height, 1), reference.width / max(reference.height, 1))
    height = min(candidate.height, reference.height, max_height, max(1, int((max_pair_width / 2) / max(aspect, 1e-6))))
    width = max(1, round(height * aspect))
    return (
        candidate.resize((width, height), Image.Resampling.LANCZOS),
        reference.resize((width, height), Image.Resampling.LANCZOS),
    )


def _font(image: Image.Image, divisor: int = 24):
    size = max(18, min(46, image.width // divisor))
    try:
        return ImageFont.truetype("DejaVuSans-Bold.ttf", size=size)
    except OSError:
        return ImageFont.load_default()


def _label(image: Image.Image, text: str, color: tuple[int, int, int]) -> Image.Image:
    font = _font(image)
    bar = max(42, min(76, image.height // 7))
    labelled = Image.new("RGB", (image.width, image.height + bar), (4, 4, 8))
    labelled.paste(image, (0, bar))
    draw = ImageDraw.Draw(labelled)
    draw.rectangle((0, bar - 7, image.width, bar), fill=color)
    font_size = getattr(font, "size", 16)
    draw.text(
        (14, max(5, (bar - font_size) // 2 - 2)),
        text,
        fill=(255, 255, 255),
        font=font,
        stroke_width=2,
        stroke_fill=(0, 0, 0),
    )
    return labelled


def mark_pair(
    image: Image.Image, seam: int, left: str = "CANDIDATE — LEFT", right: str = "REFERENCE — RIGHT"
) -> Image.Image:
    """Embed unmistakable, color-coded source identity into comparison pixels."""
    font = _font(image, 44)
    bar = max(48, min(82, image.height // 6))
    labelled = Image.new("RGB", (image.width, image.height + bar), (4, 4, 8))
    labelled.paste(image, (0, bar))
    draw = ImageDraw.Draw(labelled)
    yellow, cyan = (255, 212, 0), (0, 220, 255)
    draw.rectangle((0, 0, seam - 1, bar), fill=(24, 20, 0))
    draw.rectangle((seam, 0, image.width, bar), fill=(0, 20, 26))
    draw.rectangle((0, bar - 8, seam - 1, bar), fill=yellow)
    draw.rectangle((seam, bar - 8, image.width, bar), fill=cyan)
    draw.line((seam, 0, seam, labelled.height), fill=(255, 60, 255), width=6)
    y = max(5, (bar - getattr(font, "size", 16)) // 2 - 2)
    draw.text((14, y), left, fill=(255, 255, 255), font=font, stroke_width=2, stroke_fill=(0, 0, 0))
    box = draw.textbbox((0, 0), right, font=font, stroke_width=2)
    rw = box[2] - box[0]
    draw.text(
        (max(seam + 14, image.width - rw - 14), y),
        right,
        fill=(255, 255, 255),
        font=font,
        stroke_width=2,
        stroke_fill=(0, 0, 0),
    )
    return labelled


def focus_views(
    candidate_crop: str | Path,
    reference_full: str | Path,
    crop,
    views: list[str] | tuple[str, ...] = ("side_by_side", "wipe"),
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
    candidate_signal = focus_signal(candidate_raw)
    reference_signal = focus_signal(reference_crop)
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
            image = mark_pair(image, width)
        elif view == "wipe":
            image = candidate.copy()
            seam = width // 2
            image.paste(reference.crop((seam, 0, width, height)), (seam, 0))
            draw = ImageDraw.Draw(image)
            draw.line((seam, 0, seam, height), fill=(255, 80, 255), width=3)
            image = mark_pair(image, seam, "CANDIDATE — LEFT OF WIPE", "REFERENCE — RIGHT OF WIPE")
        elif view == "overlay":
            image = Image.blend(candidate, reference, 0.5)
            image = _label(image, "50/50 OVERLAY", (255, 255, 255))
        else:
            raw = ImageChops.difference(candidate, reference)
            image = ImageEnhance.Contrast(raw).enhance(2.0)
            image = _label(image, "2x DIFFERENCE", (255, 255, 255))
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
        "upscaled": (
            width > candidate_raw.width
            or height > candidate_raw.height
            or width > reference_crop.width
            or height > reference_crop.height
        ),
        "signal": {"candidate": candidate_signal, "reference": reference_signal},
        "has_signal": candidate_signal["has_signal"] or reference_signal["has_signal"],
    }
    return out, meta


def save_focus_sheet(
    candidate_crop: str | Path,
    reference_full: str | Path,
    crop,
    dest: str | Path,
    views: list[str] | tuple[str, ...] = ("side_by_side", "wipe"),
) -> dict:
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


def save_context_sheet(candidate_full: str | Path, reference_full: str | Path, crop, dest: str | Path) -> dict:
    crop = validate_crop(crop)
    candidate = Image.open(candidate_full).convert("RGB")
    reference = Image.open(reference_full).convert("RGB")
    candidate_aspect = candidate.width / max(candidate.height, 1)
    reference_aspect = reference.width / max(reference.height, 1)
    if abs(candidate_aspect - reference_aspect) / max(reference_aspect, 1e-6) > 0.02:
        raise ValueError(
            f"context comparison requires matching aspect; candidate is {candidate.size}, reference is {reference.size}"
        )
    candidate, reference = _fit_pair(candidate, reference, max_height=500)
    width, height = candidate.size
    sheet = Image.new("RGB", (2 * width, height), _BG)
    sheet.paste(candidate, (0, 0))
    sheet.paste(reference, (width, 0))
    draw = ImageDraw.Draw(sheet)
    x0, y0, x1, y1 = crop
    for offset in (0, width):
        box = (offset + round(x0 * width), round(y0 * height), offset + round(x1 * width), round(y1 * height))
        draw.rectangle(box, outline=(255, 80, 255), width=4)
    sheet = mark_pair(sheet, width, "CANDIDATE CONTEXT — LEFT", "REFERENCE CONTEXT — RIGHT")
    destination = Path(dest)
    destination.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(destination, quality=90)
    return {"image_path": str(destination), "crop": list(crop), "sheet_px": list(sheet.size)}
